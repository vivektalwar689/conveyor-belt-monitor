"""Event-level evaluation of failure prediction (what operations actually cares about).

Row-level AUC says little about a maintenance alarm. We instead replay the hourly
decisions of each method over held-out belt-years and ask, per failure:

* was it predicted at all, and how far ahead (lead time)?
* was the warning early enough that the repair could be done in a *planned*
  00:00-06:00 stop instead of an unplanned trip ("actionable")?
and, per alert episode: did the component actually fail within 10 days? If not,
it is a false alarm (reported per 1,000 station-days).

Methods compared on identical data:
* fixed      - classic alarm limits (vibration 4.5 mm/s ~ ISO 10816 zone C,
               bearing 25 °C over ambient; belt offset 75 mm)
* anomaly_z  - our statistical anomaly detector alone (|z| >= 4 on any signal)
* ml         - gradient-boosted classifier, threshold chosen on the validation run
* trend      - (drive only) trend-to-threshold extrapolation
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ml.dataset import failure_times, load_run, signals_for_run
from simulator import config as C

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "ml" / "artifacts"
DOC_IMG = ROOT / "docs" / "images"
CREDIT_H = 240          # an alert is "true" if that component fails within 10 days
PERSIST = 2             # consecutive hourly decisions above threshold before alerting
PREP_H = 2              # time needed to plan the job before a planned stop can be used
FA_BUDGET = 4.0         # max false alarms per 1,000 station-days (~1 per 10 days on this belt)


def baseline_scores(mode: str, df: pd.DataFrame) -> dict[str, np.ndarray]:
    if mode == "bearing":
        return {
            "fixed": np.fmax(df.rms_abs_1h / 4.5, df.trise_abs_1h / 25).to_numpy(),
            "anomaly_z": np.fmax(np.fmax(df.env_z, df.rms_z), df.trise_z).to_numpy() / 4,
        }
    return {"fixed": (df.off_abs_1h / 75).to_numpy(), "anomaly_z": (df.off_abs_1h / 50).to_numpy()}


def _episodes(ts: np.ndarray, flag: np.ndarray) -> list[tuple[float, float]]:
    """Alert episodes from hourly flags (rows are running hours; a gap > 12 h ends an episode)."""
    eps, start, last, run_len = [], None, None, 0
    for t, f in zip(ts, flag):
        if last is not None and t - last > 12 * 3600:
            if start is not None:
                eps.append((start, last))
            start, run_len = None, 0
        run_len = run_len + 1 if f else 0
        if f and run_len >= PERSIST and start is None:
            start = t
        if not f and start is not None:
            eps.append((start, last))
            start = None
        last = t
    if start is not None:
        eps.append((start, last))
    return eps


def _next_planned_stop(ts: float) -> float:
    t = datetime.fromtimestamp(ts, timezone.utc).astimezone(C.SITE_TZ)
    nxt = (t + timedelta(days=1)).replace(hour=C.PLANNED_STOP_LOCAL_HOURS[0], minute=0, second=0, microsecond=0)
    if t.hour < C.PLANNED_STOP_LOCAL_HOURS[0]:
        nxt -= timedelta(days=1)
    return nxt.timestamp()


def event_metrics(df: pd.DataFrame, score: np.ndarray, thr: float, mode: str,
                  events_by_run: dict[str, list[dict]]) -> dict:
    df = df.assign(_s=score)
    n_fail = detected = det24 = det12 = actionable = 0
    leads, avoided, false_eps, station_days = [], 0.0, 0, 0.0
    for (run, sid), g in df.groupby(["run", "station_id"]):
        g = g.sort_values("ts")
        ts = g.ts.to_numpy(float)
        station_days += (ts[-1] - ts[0]) / 86400
        eps = _episodes(ts, (g._s.to_numpy() >= thr))
        fails = [e for e in events_by_run[run] if e["type"] == "failure" and e["station"] == sid and e["mode"] == mode]
        f_ts = np.array([pd.Timestamp(e["t"]).timestamp() for e in fails])
        f_ts = f_ts[(f_ts > ts[0] + 24 * 3600) & (f_ts <= ts[-1] + 12 * 3600)]
        for fe, ft in zip([e for e in fails if pd.Timestamp(e["t"]).timestamp() in set(f_ts)], f_ts):
            n_fail += 1
            starts = [s for s, _e in eps if ft - CREDIT_H * 3600 <= s < ft]
            if not starts:
                continue
            lead = (ft - min(starts)) / 3600
            detected += 1
            det24 += lead >= 24
            det12 += lead >= 12
            leads.append(lead)
            if _next_planned_stop(min(starts) + PREP_H * 3600) < ft:
                actionable += 1
                avoided += float(fe.get("repair_hours", 0))
        for s, _e in eps:
            if not np.any((f_ts > s) & (f_ts <= s + CREDIT_H * 3600)):
                false_eps += 1
    return {
        "threshold": round(float(thr), 3), "failures": n_fail,
        "recall_any": round(detected / max(n_fail, 1), 3), "recall_lead_ge_12h": round(det12 / max(n_fail, 1), 3),
        "recall_lead_ge_24h": round(det24 / max(n_fail, 1), 3),
        "median_lead_h": round(float(np.median(leads)), 1) if leads else None,
        "actionable_in_planned_stop": round(actionable / max(n_fail, 1), 3),
        "unplanned_downtime_avoidable_h": round(avoided, 1),
        "false_alarms": false_eps, "false_alarms_per_1000_station_days": round(1000 * false_eps / max(station_days, 1), 2),
        "station_days": round(station_days, 1),
    }


def _events(runs) -> dict[str, list[dict]]:
    return {r: load_run(r)[1] for r in runs}


def choose_threshold(mode: str, mm, val: pd.DataFrame) -> tuple[float, list[dict]]:
    ev = _events(val.run.unique())
    p = mm.clf.predict_proba(val[mm.features].to_numpy(float))[:, 1]
    curve = [event_metrics(val, p, thr, mode, ev) for thr in np.round(np.arange(0.05, 0.95, 0.05), 2)]
    ok = [c for c in curve if c["false_alarms_per_1000_station_days"] <= FA_BUDGET]
    best = max(ok, key=lambda c: (c["recall_lead_ge_24h"], c["recall_any"], -c["threshold"])) if ok else curve[-1]
    return float(best["threshold"]), curve


def drive_eval(run: str) -> dict:
    """Replay the trend-to-threshold drive predictor hourly over a run."""
    from backend.predict import drive_predictions

    readings, events, _ = load_run(run)
    cl, _sig = signals_for_run(readings)
    ts = cl.frame.ts
    rows = []
    for i in range(7 * 24 * 12, len(ts), 12):
        if not cl.running[i]:
            continue
        sub = SimpleNamespace(clean=cl.clean[: i + 1], running=cl.running[: i + 1])
        for p in drive_predictions(sub, float(ts[i]), seed=i):
            rows.append({"run": run, "station_id": "S24", "ts": float(ts[i]), "mode": p["mode"],
                         "p": p["probability"], "ttf_p50_h": p["ttf_p50_h"]})
    df = pd.DataFrame(rows)
    out = {}
    for mode in ("gearbox", "slip"):
        d = df[df["mode"] == mode]
        out[mode] = event_metrics(d, d.p.to_numpy(), 0.5, mode, {run: events})
    return out


def evaluate_all(models: dict, te: dict[str, pd.DataFrame]) -> dict:
    results: dict = {}
    for mode, mm in models.items():
        results[mode] = {}
        for run, df in te[mode].groupby("run"):
            ev = _events([run])
            p = mm.clf.predict_proba(df[mm.features].to_numpy(float))[:, 1]
            res = {"ml": event_metrics(df, p, mm.threshold, mode, ev)}
            # time-to-failure: when the dashboard shows an ETA (alerted rows), is the P10-P90 band honest?
            al = p >= mm.threshold
            act = df.ttf_h.to_numpy()[al]
            fin = act <= 240
            if fin.any():
                q = mm.ttf_quantiles(df[mm.features].to_numpy(float)[al][fin])
                a = act[fin]
                raw = np.exp(np.sort(np.column_stack([mm.q10.predict(df[mm.features].to_numpy(float)[al][fin]),
                                                      mm.q90.predict(df[mm.features].to_numpy(float)[al][fin])]), axis=1))
                res["ttf"] = {"alerted_rows_failing_within_10d": int(fin.sum()),
                              "coverage_p10_p90_conformal": round(float(np.mean((a >= q[:, 0]) & (a <= q[:, 2]))), 3),
                              "coverage_p10_p90_raw": round(float(np.mean((a >= raw[:, 0]) & (a <= raw[:, 1]))), 3),
                              "fails_before_p10": round(float(np.mean(a < q[:, 0])), 3),
                              "median_abs_error_h": round(float(np.median(np.abs(a - q[:, 1]))), 1)}
            for name, sc in baseline_scores(mode, df).items():
                res[name] = event_metrics(df, sc, 1.0, mode, ev)
            res["ml_threshold_sweep"] = [event_metrics(df, p, t, mode, ev) for t in (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9)]
            results[mode][run] = res
            if run == "test_s21":
                _plot_tradeoff(mode, res, res["ml"])
                _plot_example(mode, mm, df, ev[run])
    results["drive_trend"] = {run: drive_eval(run) for run in te["bearing"].run.unique()}
    return results


# ------------------------------------------------------------------ figures (palette per dataviz skill)
INK, INK2, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
WARN, CRIT = "#fab219", "#d03b3b"


def _style(ax):
    ax.set_facecolor(SURF)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c3c2b7")
        ax.spines[s].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _plot_tradeoff(mode: str, res: dict, chosen: dict | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 3.8), dpi=150, facecolor=SURF)
    _style(ax)
    sw = res["ml_threshold_sweep"]
    x = [c["false_alarms_per_1000_station_days"] for c in sw]
    y = [c["recall_lead_ge_24h"] for c in sw]
    ax.plot(x, y, color=S1, lw=2, marker="o", ms=4, mec=SURF, mew=1.5, label="ML (threshold sweep)")
    placed = []
    for c in sw:
        xy = (c["false_alarms_per_1000_station_days"], c["recall_lead_ge_24h"])
        if any(abs(xy[0] - p[0]) < 0.25 and abs(xy[1] - p[1]) < 0.05 for p in placed):
            continue                                   # skip labels that would collide
        placed.append(xy)
        ax.annotate(f"p≥{c['threshold']:g}", xy, textcoords="offset points", xytext=(4, -11), fontsize=7, color=INK2)
    if chosen is not None:
        ax.plot([chosen["false_alarms_per_1000_station_days"]], [chosen["recall_lead_ge_24h"]], ls="none", marker="o",
                ms=10, mfc="none", mec=INK, mew=1.2, label=f"Chosen on validation (p≥{chosen['threshold']:g})")
    for name, col, lab in (("fixed", S2, "Fixed alarm limits"), ("anomaly_z", S3, "Anomaly z-score only")):
        c = res[name]
        ax.plot([c["false_alarms_per_1000_station_days"]], [c["recall_lead_ge_24h"]], ls="none", marker="s", ms=7,
                color=col, mec=SURF, mew=1.5, label=lab)
    ax.set_xlabel("False alarms per 1,000 station-days", color=INK2, fontsize=9)
    ax.set_ylabel("Failures caught ≥ 24 h ahead", color=INK2, fontsize=9)
    ax.set_ylim(-0.03, 1.03)
    ax.set_title(f"{mode.capitalize()}: early warning vs. false alarms (held-out belt-year)", color=INK, fontsize=10,
                 loc="left")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="lower right")
    fig.tight_layout()
    DOC_IMG.mkdir(parents=True, exist_ok=True)
    fig.savefig(DOC_IMG / f"tradeoff_{mode}.png", facecolor=SURF)
    plt.close(fig)


def _plot_example(mode: str, mm, df: pd.DataFrame, events: list[dict]) -> None:
    """Small multiples (shared time axis, one y-scale each) leading up to one failure."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fails = [e for e in events if e["type"] == "failure" and e["mode"] == mode]
    if not fails:
        return
    best = None
    for e in fails:
        ft = pd.Timestamp(e["t"]).timestamp()
        g = df[(df.station_id == e["station"]) & (df.ts > ft - 9 * 86400) & (df.ts < ft)]
        if len(g) > 100 and (best is None or len(g) > best[2]):
            best = (e, ft, len(g), g)
    if best is None:
        return
    e, ft, _n, g = best
    g = g.sort_values("ts")
    p = mm.clf.predict_proba(g[mm.features].to_numpy(float))[:, 1]
    hrs = (g.ts.to_numpy() - ft) / 3600
    alert = np.flatnonzero(pd.Series(p >= mm.threshold).rolling(PERSIST).sum().to_numpy() >= PERSIST)
    if mode == "bearing":
        panels = [("HF envelope deviation (σ)", g.env_z), ("Vibration RMS (mm/s)", g.rms_abs_1h),
                  ("Bearing temp. over ambient (°C)", g.trise_abs_1h), ("P(failure ≤ 72 h)", p)]
    else:
        panels = [("Belt offset |mm|", g.off_abs_1h), ("Offset growth (mm/day)", g.off_growth_24h),
                  ("P(failure ≤ 72 h)", p)]
    fig, axes = plt.subplots(len(panels), 1, figsize=(7.2, 1.55 * len(panels) + 0.5), dpi=150, sharex=True,
                             facecolor=SURF)
    for ax, (lab, y) in zip(axes, panels):
        _style(ax)
        ax.plot(hrs, np.asarray(y, float), color=S1, lw=1.6)
        ax.set_ylabel(lab, color=INK2, fontsize=7.5, rotation=0, ha="right", va="center", labelpad=6)
        if len(alert):
            ax.axvline(hrs[alert[0]], color=WARN, lw=1.5)
        ax.axvline(0, color=CRIT, lw=1.5)
    axes[-1].axhline(mm.threshold, color=MUTED, lw=0.8)
    axes[-1].set_ylim(-0.02, 1.02)
    if len(alert):
        axes[0].annotate(f"▲ first alert {-hrs[alert[0]]:.0f} h before failure", (hrs[alert[0]], 1.02),
                         xycoords=("data", "axes fraction"), fontsize=7.5, color=INK, ha="left", va="bottom")
    axes[0].annotate("■ failure", (0, 1.02), xycoords=("data", "axes fraction"), fontsize=7.5, color=INK,
                     ha="right", va="bottom")
    axes[-1].set_xlabel("Hours before failure (decisions are only made while the belt runs; lines bridge the stops)",
                        color=INK2, fontsize=8)
    fig.suptitle(f"{e['station']} {mode} failure on a held-out belt-year", color=INK, fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(DOC_IMG / f"example_{mode}.png", facecolor=SURF)
    plt.close(fig)


if __name__ == "__main__":
    import joblib

    from backend.predict import MODEL_PATH
    from ml.train import SPLITS, tables

    blob = joblib.load(MODEL_PATH)
    res = evaluate_all(blob["models"], tables(SPLITS["test"]))
    print(json.dumps(res, indent=1, default=str))
