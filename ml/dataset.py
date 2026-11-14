"""Build model-ready tables from simulated history.

The raw simulated readings go through the *production* code path
(backend.ingest.normalize -> backend.quality.clean -> backend.features), so the
model is trained on data that was damaged and repaired exactly like live data.
Labels come from the simulator's ground-truth failure log - in a real plant they
would come from CMMS work orders, which is the main thing that is hard to get
(see README, "What I would do differently").
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backend.features import compute_signals, feature_table
from backend.ingest import normalize
from backend.quality import build_frame, clean
from simulator import config as C

ROOT = Path(__file__).resolve().parents[1]
SIM_DIR = ROOT / "data" / "sim"
HORIZON_H = 72          # "will it fail within the next 3 days?" -> fix at the next planned stop
TTF_CAP_H = 240


def load_run(name: str) -> tuple[pd.DataFrame, list[dict], dict]:
    d = SIM_DIR / name
    readings = pd.read_parquet(d / "readings.parquet")
    events = json.loads((d / "events.json").read_text())
    truth = dict(np.load(d / "truth.npz"))
    return readings, events, truth


def signals_for_run(readings: pd.DataFrame):
    acc, _rej, _cnt = normalize(readings, readings.received_ts)
    t0, t1 = int(acc.ts.min()), int(acc.ts.max())
    frame = build_frame(acc, t0, t1)
    cl = clean(frame)
    return cl, compute_signals(cl)


def failure_times(events: list[dict]) -> dict[tuple[str, str], np.ndarray]:
    out: dict[tuple[str, str], list[float]] = {}
    for e in events:
        if e["type"] == "failure":
            out.setdefault((e["station"], e["mode"]), []).append(pd.Timestamp(e["t"]).timestamp())
    return {k: np.array(sorted(v)) for k, v in out.items()}


def label(df: pd.DataFrame, fails: dict[tuple[str, str], np.ndarray], mode: str) -> pd.DataFrame:
    ttf = np.full(len(df), np.inf)
    for sid in df.station_id.unique():
        ft = fails.get((sid, mode))
        if ft is None or len(ft) == 0:
            continue
        m = (df.station_id == sid).to_numpy()
        ts = df.ts.to_numpy()[m]
        j = np.searchsorted(ft, ts, side="right")
        nxt = np.where(j < len(ft), ft[np.minimum(j, len(ft) - 1)], np.inf)
        ttf[m] = (nxt - ts) / 3600
    return df.assign(ttf_h=ttf, y=(ttf <= HORIZON_H).astype(int))


def build(name: str, every_h: int = 1) -> dict[str, pd.DataFrame]:
    t = time.time()
    readings, events, _truth = load_run(name)
    cl, sig = signals_for_run(readings)
    idx = np.arange(0, len(sig.ts), every_h * 3600 // C.SAMPLE_PERIOD_S)
    ft = feature_table(sig, idx)
    ft = ft[ft.running.astype(bool)]                      # decisions are made on fresh data
    warm = sig.ts[0] + 2 * 86400                          # baselines need ~2 days to settle
    ft = ft[ft.ts >= warm].reset_index(drop=True)
    fails = failure_times(events)
    out = {}
    for mode in ("bearing", "misalignment"):
        kinds = {s.id for s in C.STATIONS if mode in s.modes}
        out[mode] = label(ft[ft.station_id.isin(kinds)].reset_index(drop=True), fails, mode).assign(run=name)
    print(f"  built {name}: {len(ft)} rows in {time.time() - t:.0f}s")
    return out
