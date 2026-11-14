"""The analysis loop: everything the backend does with stored data, once per cycle.

Stateless where possible: each cycle re-reads the last 8 days and recomputes
cleaning + features from scratch. That is what makes late data (store-and-forward
buffers flushing hours later) harmless: the next cycle simply sees it. Only the
alarm lifecycle and "what has already been evaluated" are stateful.
"""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

from backend.anomaly import SEV_ORDER, AnomalyEngine
from backend.db import get_state, set_state
from backend.features import compute_signals, feature_table
from backend.predict import SensorModels, drive_predictions
from backend.quality import build_frame, clean, sensor_health
from simulator import config as C

WINDOW_DAYS = 8
WARMUP_S = 2 * 86400        # baselines need ~2 days of history before alarms/predictions are trusted
GRID = C.SAMPLE_PERIOD_S


class Analyzer:
    def __init__(self, con: sqlite3.Connection, models: SensorModels | None = None):
        self.con = con
        self.models = models or SensorModels()
        self.anoms = AnomalyEngine(con)
        self.last_cycle_s = 0.0

    def cycle(self) -> dict | None:
        con = self.con
        newest = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
        # Watermark: the newest step may still be arriving (gateways post independently), so analyse up to
        # one sample behind it. Later arrivals for older steps are picked up by the 8-day recompute.
        now = newest - GRID if newest else None
        last = int(get_state(con, "last_eval_ts", 0) or 0)
        if not now or now <= last:
            return None
        t_start = time.time()
        first_ts = con.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
        t0 = max(first_ts, now - WINDOW_DAYS * 86400)
        df = pd.read_sql_query("SELECT station_id, channel, ts, value, quality FROM readings WHERE ts >= ? AND ts <= ?",
                               con, params=(t0, now))
        frame = build_frame(df, t0, now)
        cl = clean(frame)
        sig = compute_signals(cl)
        ts = frame.ts
        warm_idx = int(np.searchsorted(ts, first_ts + WARMUP_S))
        i0 = max(warm_idx, int(np.searchsorted(ts, last, side="right")))
        if i0 < len(ts):
            self.anoms.update(cl, sig, i0)
        con.commit()                       # keep write transactions short: ingestion shares this database
        rows = self._predictions(cl, sig, last, warm_idx)
        self._store_predictions(rows)
        self._status(cl, rows, int(now))
        set_state(con, "last_eval_ts", int(now))
        set_state(con, "running_now", int(bool(cl.running[-1])))
        set_state(con, "running_series", json.dumps({"t0": int(ts[0]), "step": GRID,
                                                     "running": cl.running[-288:].astype(int).tolist()}))
        self.last_cycle_s = time.time() - t_start
        info = {"now": int(now), "steps": len(ts) - i0, "seconds": round(self.last_cycle_s, 2)}
        set_state(con, "analysis_last", json.dumps(info))      # read by /api/overview, whichever process runs this
        con.commit()
        return info

    # ------------------------------------------------------------------ predictions
    def _predictions(self, cl, sig, last: int, warm_idx: int) -> list[dict]:
        ts = cl.frame.ts
        run = cl.running
        # decision points: every running hour since the last cycle, plus "now"
        hourly = [i for i in range(max(warm_idx, 0), len(ts)) if ts[i] > last and ts[i] % 3600 == 0 and run[i]]
        idx = sorted(set(hourly + ([len(ts) - 1] if run[-1] and len(ts) - 1 >= warm_idx else [])))
        # require >= 3 consecutive running samples so 1-h means are not one-sample artefacts
        idx = [i for i in idx if i >= 2 and run[i - 2: i + 1].all()]
        rows: list[dict] = []
        if idx and self.models.ok:
            feats = feature_table(sig, np.array(idx))
            for t_idx, g in feats.groupby("t_idx"):
                p = self.models.predict(g)
                p["ts"] = int(ts[t_idx])
                rows.extend(p.to_dict("records"))
        for i in idx:
            sub = SimpleNamespace(clean=cl.clean[: i + 1], running=cl.running[: i + 1])
            for r in drive_predictions(sub, float(ts[i]), seed=int(ts[i]) % 100000):
                r["ts"] = int(ts[i])
                rows.append(r)
        return rows

    def _store_predictions(self, rows: list[dict]) -> None:
        if rows:
            self.con.executemany(
                "INSERT OR REPLACE INTO predictions(station_id, mode, ts, probability, ttf_p10_h, ttf_p50_h, ttf_p90_h,"
                " method, confidence, evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r["station_id"], r["mode"], r["ts"], r["probability"], _f(r.get("ttf_p10_h")), _f(r.get("ttf_p50_h")),
                  _f(r.get("ttf_p90_h")), r["method"], r["confidence"],
                  json.dumps({"text": r["evidence"], "threshold": r.get("threshold", 0.5)})) for r in rows])

    # ------------------------------------------------------------------ station status
    def _status(self, cl, preds: list[dict], now: int) -> None:
        con = self.con
        latest = {}
        for r in con.execute("SELECT p.* FROM predictions p JOIN (SELECT station_id, mode, MAX(ts) mts FROM predictions "
                             "GROUP BY station_id, mode) m ON p.station_id=m.station_id AND p.mode=m.mode AND p.ts=m.mts"):
            latest[(r["station_id"], r["mode"])] = r
        active = {}
        for r in con.execute("SELECT * FROM anomalies WHERE status='active'"):
            active.setdefault(r["station_id"], []).append(r)
        health = sensor_health(cl)
        by_station: dict[str, list[dict]] = {}
        for h in health:
            by_station.setdefault(h["station_id"], []).append(h)
        rows = []
        for st in C.STATIONS:
            reasons, status = [], "normal"
            anoms = active.get(st.id, [])
            offline = any(a["type"] == "station_offline" for a in anoms)
            fails = []
            for mode in st.modes:
                p = latest.get((st.id, mode))
                if p is None:
                    continue
                if alert_state(con, st.id, mode):
                    fails.append((mode, p))
            eq = [a for a in anoms if a["category"] == "equipment"]
            if offline:
                status = "no_data"
                reasons.append("No data for >= 15 min")
            if fails:
                status = "predicted_failure" if not offline else status
                for mode, p in fails:
                    when = f", ~{p['ttf_p50_h']:.0f} h" if p["ttf_p50_h"] is not None else ""
                    reasons.append(f"{C.FAILURE_MODES[mode]}: P={p['probability']:.0%}{when}")
            elif eq and not offline:
                status = "abnormal"
            for a in sorted(eq, key=lambda a: -SEV_ORDER[a["severity"]]):
                reasons.append(f"{a['severity']}: {a['message']}")
            sh = by_station.get(st.id, [])
            issues = [f"{h['channel']} stuck" for h in sh if h["stuck_now"]]
            issues += [a["message"] for a in anoms if a["category"] == "data_quality" and a["type"] != "station_offline"]
            completeness = float(np.mean([h["completeness"] for h in sh])) if sh else 0.0
            rows.append((st.id, now, status, json.dumps(reasons),
                         json.dumps({"completeness_24h": round(completeness, 3), "issues": issues, "channels": sh})))
        con.executemany("INSERT OR REPLACE INTO station_status(station_id, ts, status, reasons, sensor_health) "
                        "VALUES (?,?,?,?,?)", rows)


PERSIST = 2   # same rule as the offline evaluation (ml/evaluate.py): 2 consecutive hourly decisions


def alert_state(con, station_id: str, mode: str) -> bool:
    """A failure alert is raised only when the last PERSIST hourly decisions are all above the
    model's threshold - exactly the rule the false-alarm rate was measured with."""
    rows = con.execute("SELECT probability, evidence FROM predictions WHERE station_id=? AND mode=? AND ts % 3600 = 0 "
                       "ORDER BY ts DESC LIMIT ?", (station_id, mode, PERSIST)).fetchall()
    if len(rows) < PERSIST:
        return False
    return all((r["probability"] or 0) >= json.loads(r["evidence"] or "{}").get("threshold", 0.5) for r in rows)


def _f(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else float(x)
