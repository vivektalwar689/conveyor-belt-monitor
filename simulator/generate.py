"""Offline generation of simulated history (for model training / evaluation).

Runs exactly the same engine + messy transport as the live stream, but writes
the *delivered* readings (with duplicates, gaps, unit/alias variants, sentinels)
to Parquet instead of POSTing them, together with the ground truth:

    data/sim/<name>/readings.parquet   what the backend would have received
    data/sim/<name>/events.json        failures, onsets, repairs, trips, data-quality faults
    data/sim/<name>/truth.npz          hidden health index per step/station/mode, running flag

Usage:
    python -m simulator.generate --name train_s1 --days 120 --seed 1
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from simulator import config as C
from simulator.engine import BeltSimulator, Scenario
from simulator.faults import ALIASES_OUT, UNIT_OVERRIDES, DataQuality, MessyTransport, convert_out

ROOT = Path(__file__).resolve().parents[1]
SIM_DIR = ROOT / "data" / "sim"


def run(days: float, seed: int, scenario: Scenario | None = None, dq: DataQuality | None = None,
        start: datetime | None = None, progress: bool = True) -> dict:
    start = start or datetime(2026, 6, 1, tzinfo=timezone.utc)
    sim = BeltSimulator(start, seed=seed, scenario=scenario)
    tr = MessyTransport(seed, dq, start)
    n = int(days * 86400 / C.SAMPLE_PERIOD_S)
    S, M = len(C.STATIONS), 4
    health = np.zeros((n, S, M), dtype=np.float32)
    running = np.zeros(n, dtype=bool)
    feed = np.zeros(n, dtype=np.float32)
    ts = np.zeros(n, dtype=np.float64)
    events: list[dict] = []
    cols = {k: [] for k in ("station_id", "channel", "ts", "value", "seq", "received_ts")}
    t0 = time.time()
    for k in range(n):
        out = sim.step()
        recs, dq_events = tr.process(out.t, out.values, sim.has)
        now = out.t.timestamp()
        for r in recs:
            cols["station_id"].append(r.station_id)
            cols["channel"].append(r.channel)
            cols["ts"].append(r.ts)
            cols["value"].append(r.value if isinstance(r.value, float) else np.nan)
            cols["seq"].append(r.seq)
            cols["received_ts"].append(now)
        health[k], running[k], feed[k], ts[k] = out.health, out.running, out.feed_tph, now
        events.extend(out.events)
        events.extend(dq_events)
        if progress and k % 5000 == 0:
            print(f"  step {k}/{n}  ({time.time() - t0:.0f}s)", end="\r")
    if progress:
        print(f"  simulated {days} days in {time.time() - t0:.0f}s, {len(cols['ts'])} readings delivered")
    df = pd.DataFrame(cols)
    # Wire-format heterogeneity, applied vectorised: units and legacy tag names.
    df["unit"] = df["channel"].map(lambda c: C.CHANNELS[c][0])
    for (sid, ch), unit in UNIT_OVERRIDES.items():
        m = (df.station_id == sid) & (df.channel == ch) & ~df.value.isin([-999.0, 9999.0, 6553.5])
        df.loc[m, "value"] = convert_out(df.loc[m, "value"], unit)
        df.loc[(df.station_id == sid) & (df.channel == ch), "unit"] = unit
    df["sensor"] = df["channel"]
    for (sid, ch), alias in ALIASES_OUT.items():
        df.loc[(df.station_id == sid) & (df.channel == ch), "sensor"] = alias
    df = df.drop(columns=["channel"])
    return {"readings": df, "events": events, "health": health, "running": running, "feed": feed, "ts": ts}


def save(name: str, res: dict) -> Path:
    d = SIM_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    res["readings"].to_parquet(d / "readings.parquet", index=False)
    ev = [{**e, "t": e["t"].isoformat()} for e in res["events"]]
    (d / "events.json").write_text(json.dumps(ev, indent=1, default=str))
    np.savez_compressed(d / "truth.npz", health=res["health"], running=res["running"], feed=res["feed"], ts=res["ts"])
    return d


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--days", type=float, default=120)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    res = run(a.days, a.seed)
    print(save(a.name, res))
    ev = pd.DataFrame(res["events"])
    print(ev.groupby(["type"] + (["mode"] if "mode" in ev else [])).size())
