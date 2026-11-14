"""Train the sensor failure-prediction models on simulated run-to-failure history.

    python -m ml.train            # simulate (if needed), build features, train, evaluate

Splits are by *simulation run* (independent seeds = independent 120-day runs), never
by random rows: neighbouring hours of the same degradation are near-duplicates and
a random split would leak them (the same lesson as the vision dataset audit).

    train : seeds 11, 12        (2 x 120 days)
    val   : seed 13             (threshold selection)
    test  : seed 21             (normal conditions)
            seed 22 "harsh"     (5x worse data quality: outages, stuck/drifting sensors, spikes)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import average_precision_score, roc_auc_score

from backend.features import BEARING_FEATURES, MISALIGN_FEATURES
from backend.predict import MODEL_PATH, ModeModel
from ml.dataset import SIM_DIR, TTF_CAP_H, build
from simulator.faults import DataQuality
from simulator.generate import run, save

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "ml" / "artifacts"
DAYS = 120
HARSH = DataQuality(packet_loss=0.02, storm_packet_loss=0.15, station_outage_rate=1 / 20, gateway_outage_rate=1 / 8,
                    stuck_rate=1 / 500, drift_rate=1 / 300, spike_prob=2e-3, sentinel_prob=1e-3,
                    storm_rate_per_day=1 / 4)
RUNS = {
    "train_s11": (11, None), "train_s12": (12, None),
    "val_s13": (13, None),
    "test_s21": (21, None), "test_s22_harsh": (22, HARSH),
}
SPLITS = {"train": ["train_s11", "train_s12"], "val": ["val_s13"], "test": ["test_s21", "test_s22_harsh"]}
FEATURES = {"bearing": BEARING_FEATURES, "misalignment": MISALIGN_FEATURES}


def ensure_sims() -> None:
    for name, (seed, dq) in RUNS.items():
        if (SIM_DIR / name / "readings.parquet").exists():
            continue
        print(f"simulating {name} ({DAYS} days, seed {seed})")
        save(name, run(DAYS, seed, dq=dq))


def tables(names: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, list[pd.DataFrame]] = {"bearing": [], "misalignment": []}
    for name in names:
        cache = {m: SIM_DIR / name / f"features_{m}.parquet" for m in out}
        if all(p.exists() for p in cache.values()):
            built = {m: pd.read_parquet(p) for m, p in cache.items()}
        else:
            built = build(name)
            for m, df in built.items():
                df.to_parquet(cache[m], index=False)
        for m in out:
            out[m].append(built[m])
    return {m: pd.concat(v, ignore_index=True) for m, v in out.items()}


def fit_mode(mode: str, tr: pd.DataFrame) -> ModeModel:
    feats = FEATURES[mode]
    X, y = tr[feats].to_numpy(float), tr.y.to_numpy()
    clf = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40,
                                         l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
                                         n_iter_no_change=30, random_state=0)
    clf.fit(X, y)
    # Time-to-failure regressors: trained where a failure is <= 10 days away (degradation in progress)
    m = np.isfinite(tr.ttf_h.to_numpy()) & (tr.ttf_h.to_numpy() <= TTF_CAP_H)
    Xr, yr = X[m], np.log(np.maximum(tr.ttf_h.to_numpy()[m], 0.25))
    regs = []
    for q in (0.1, 0.5, 0.9):
        r = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=300, learning_rate=0.05,
                                          max_leaf_nodes=31, min_samples_leaf=30, random_state=0)
        r.fit(Xr, yr)
        regs.append(r)
    return ModeModel(feats, clf, *regs, threshold=0.5)


def conformal_width(mm: ModeModel, val: pd.DataFrame, alpha: float = 0.2) -> float:
    """Conformalized quantile regression (Romano et al., 2019) on the validation run.

    The raw quantile models are overconfident (consecutive hours of one degradation are
    near-duplicates, so the trees fit trajectories too tightly). We measure how far the
    truth falls outside [q10, q90] on *alerted* validation rows - the rows for which the
    dashboard actually shows an ETA - and widen the band by the (1-alpha) quantile of
    that miss, in log-hours. Rows from one degradation are correlated, so the coverage
    guarantee is approximate (effective n ~ number of failures, not rows).
    """
    X = val[mm.features].to_numpy(float)
    al = mm.clf.predict_proba(X)[:, 1] >= mm.threshold
    y = val.ttf_h.to_numpy()[al]
    fin = y <= TTF_CAP_H
    if fin.sum() < 20:
        return 0.0
    Xa = X[al][fin]
    q = np.column_stack([mm.q10.predict(Xa), mm.q90.predict(Xa)])
    q.sort(axis=1)
    ly = np.log(np.maximum(y[fin], 0.25))
    E = np.maximum(q[:, 0] - ly, ly - q[:, 1])
    n = len(E)
    return float(max(0.0, np.quantile(E, min(1.0, np.ceil((n + 1) * (1 - alpha)) / n))))


def main() -> None:
    from ml.evaluate import choose_threshold, evaluate_all

    ART.mkdir(parents=True, exist_ok=True)
    t = time.time()
    ensure_sims()
    tr, va, te = tables(SPLITS["train"]), tables(SPLITS["val"]), tables(SPLITS["test"])
    models, summary = {}, {}
    for mode in ("bearing", "misalignment"):
        print(f"fitting {mode}: {len(tr[mode])} rows, positive rate {tr[mode].y.mean():.3%}", flush=True)
        mm = fit_mode(mode, tr[mode])
        print(f"  choosing threshold on validation run", flush=True)
        mm.threshold, thr_curve = choose_threshold(mode, mm, va[mode])
        mm.cqr = conformal_width(mm, va[mode])
        print(f"  threshold {mm.threshold}, conformal band widening x{np.exp(mm.cqr):.2f}", flush=True)
        models[mode] = mm
        p = mm.clf.predict_proba(te[mode][mm.features].to_numpy(float))[:, 1]
        summary[mode] = {
            "train_rows": int(len(tr[mode])), "train_positive_rate": round(float(tr[mode].y.mean()), 4),
            "train_failures": int(sum(1 for r in SPLITS["train"] for e in json.loads((SIM_DIR / r / "events.json").read_text())
                                      if e["type"] == "failure" and e["mode"] == mode)),
            "threshold": mm.threshold, "threshold_curve_val": thr_curve,
            "test_row_auc": round(float(roc_auc_score(te[mode].y, p)), 4),
            "test_row_average_precision": round(float(average_precision_score(te[mode].y, p)), 4),
        }
    meta = {"horizon_h": 72, "trained_on": SPLITS["train"], "val": SPLITS["val"], "days_per_run": DAYS,
            "features": {m: FEATURES[m] for m in models}, "thresholds": {m: models[m].threshold for m in models}}
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"meta": meta, "models": models}, MODEL_PATH, compress=3)
    print(f"saved {MODEL_PATH}")
    results = evaluate_all(models, te)
    (ART / "metrics.json").write_text(json.dumps({"summary": summary, "event_level": results}, indent=2, default=str))
    print(json.dumps(results, indent=1, default=str)[:4000])
    print(f"done in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
