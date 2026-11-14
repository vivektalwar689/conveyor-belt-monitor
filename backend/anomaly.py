"""Anomaly detection with an alarm lifecycle (open -> active -> cleared).

Detection is deliberately simple and explainable; the work is in not crying wolf:

* conditions are evaluated on 1-hour trailing means of peer- and self-normalised
  signals (features.py), plus absolute limits operators already know
  (ISO 10816 vibration zones, temperature alarms, belt-offset limits);
* debounce: an anomaly opens only when its condition holds in 3 of the last 4
  samples, and clears only after 12 consecutive normal samples (1 h hysteresis);
* equipment anomalies are frozen while the belt is stopped (a bad bearing does
  not "recover" because the belt stopped for the night);
* sensor problems are a different category from equipment problems: a stuck or
  offline sensor is not a belt fault, and a temperature rise with no vibration
  change at all is labelled "suspected sensor drift", not "bearing failing".
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backend.features import PER_H, Signals
from backend.quality import Cleaned
from simulator import config as C

CH = C.CHANNEL_INDEX
SEV_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
OPEN_K, OPEN_N, CLEAR_N = 3, 4, 12


def _sev(x: np.ndarray, cuts: list[tuple[float, str]]) -> np.ndarray:
    out = np.full(x.shape, "", dtype=object)
    for c, name in cuts:                       # ascending cuts
        out[np.nan_to_num(x, nan=-np.inf) >= c] = name
    return out


@dataclass
class Condition:
    type: str
    category: str                  # equipment | data_quality
    active: np.ndarray             # (T, S) bool
    severity: np.ndarray           # (T, S) str
    score: np.ndarray              # (T, S) float
    message: callable              # (t, s) -> str
    needs_running: bool = True
    channel: str | None = None
    hold: np.ndarray | None = None   # (T, S) bool: level at which an *open* anomaly stays open (hysteresis)


def conditions(cl: Cleaned, sig: Signals) -> list[Condition]:
    v, run = cl.clean, cl.running
    T, S = run.shape[0], len(C.STATIONS)
    roll = lambda a, w=PER_H, mp=3: pd.DataFrame(a).rolling(w, min_periods=mp).mean().to_numpy()
    z = sig.z
    rms1 = roll(sig.raw["rms"])
    trise1 = roll(sig.raw["trise"])
    temp1 = roll(np.where(run[:, None], v[:, :, CH["bearing_temp"]], np.nan))
    off1 = roll(sig.raw["off"])
    out: list[Condition] = []

    env_z = z["env"]
    out.append(Condition("bearing_hf_envelope", "equipment", env_z >= 4,
                         _sev(env_z, [(4, "low"), (7, "medium"), (12, "high")]), env_z,
                         lambda t, s: f"High-frequency bearing envelope {env_z[t, s]:+.1f}σ above this idler's normal "
                                      f"(early-stage bearing defect signature)", hold=env_z >= 3))
    rms_act = (z["rms"] >= 4) | (rms1 >= 4.5)
    rms_score = np.fmax(z["rms"], rms1 / 4.5 * 4)
    out.append(Condition("bearing_vibration", "equipment", rms_act,
                         np.where(rms1 >= 7.1, "high", np.where((rms1 >= 4.5) | (z["rms"] >= 8), "medium", "low")),
                         rms_score,
                         lambda t, s: f"Vibration {rms1[t, s]:.1f} mm/s ({z['rms'][t, s]:+.1f}σ vs normal; "
                                      f"ISO 10816 zone {'D' if rms1[t, s] >= 7.1 else 'C' if rms1[t, s] >= 4.5 else 'B'})",
                         hold=(z["rms"] >= 3) | (rms1 >= 4.0)))
    temp_act = (z["trise"] >= 4) | (temp1 >= 75)
    corroborated = (env_z >= 2) | (z["rms"] >= 2) | (rms1 >= 4.5)
    out.append(Condition("bearing_temperature", "equipment", temp_act & corroborated,
                         np.where((temp1 >= 85) | (trise1 >= 35), "high",
                                  np.where((temp1 >= 75) | (trise1 >= 25) | (z["trise"] >= 8), "medium", "low")),
                         z["trise"],
                         lambda t, s: f"Bearing {temp1[t, s]:.0f}°C, {trise1[t, s]:.0f}°C over ambient "
                                      f"({z['trise'][t, s]:+.1f}σ), with vibration also elevated",
                         hold=(z["trise"] >= 3) | (temp1 >= 70)))
    out.append(Condition("temperature_uncorroborated", "data_quality", temp_act & ~corroborated & (z["trise"] >= 5),
                         np.full((T, S), "low", dtype=object), z["trise"],
                         lambda t, s: f"Bearing temperature {z['trise'][t, s]:+.1f}σ above normal but vibration "
                                      f"unchanged - suspected sensor drift; verify with a handheld IR gun"))
    aoff = np.abs(off1)
    out.append(Condition("belt_mistracking", "equipment", aoff >= 40,
                         _sev(aoff, [(40, "low"), (60, "medium"), (80, "high"), (95, "critical")]), aoff,
                         lambda t, s: f"Belt running {off1[t, s]:+.0f} mm off centre (sway switch trips at 110 mm)",
                         hold=aoff >= 30))

    # Drive station (absolute limits; trends are handled by the trend predictor)
    d = C.STATION_INDEX["S24"]

    def drive_cond(name, series, cuts, msg):
        a = np.zeros((T, S), dtype=bool)
        sev = np.full((T, S), "", dtype=object)
        sc = np.full((T, S), np.nan)
        x = roll(np.where(run, series, np.nan)[:, None])[:, 0]
        a[:, d] = np.nan_to_num(x, nan=-np.inf) >= cuts[0][0]
        sev[:, d] = _sev(x, cuts)
        sc[:, d] = x
        out.append(Condition(name, "equipment", a, sev, sc, lambda t, s: msg(x[t])))

    drive_cond("gearbox_vibration", v[:, d, CH["gearbox_vib"]], [(4.5, "medium"), (7.1, "high")],
               lambda x: f"Gearbox vibration {x:.1f} mm/s (ISO 10816 zone {'D' if x >= 7.1 else 'C'})")
    drive_cond("gearbox_temperature", v[:, d, CH["gearbox_temp"]], [(85, "medium"), (95, "high")],
               lambda x: f"Gearbox oil {x:.0f}°C")
    ds, bs = v[:, d, CH["drive_speed"]], v[:, d, CH["belt_speed"]]
    with np.errstate(all="ignore"):
        slip = np.where(ds > 0.5, (ds - bs) / ds * 100, np.nan)
    drive_cond("belt_slip", slip, [(1.0, "medium"), (2.0, "high")],
               lambda x: f"Belt slipping {x:.2f}% on the drive pulley (lagging wear?)")
    drive_cond("motor_overtemperature", v[:, d, CH["motor_temp"]], [(130, "medium"), (150, "high")],
               lambda x: f"Motor winding {x:.0f}°C")

    # Data-quality conditions
    f = cl.frame
    any_present = f.present.any(axis=2)                                   # (T, S)
    recent = pd.DataFrame(any_present.astype(float)).rolling(3, min_periods=1).sum().to_numpy()
    fleet_up = any_present.sum(axis=1, keepdims=True) >= S // 2
    offline = (recent == 0) & fleet_up
    out.append(Condition("station_offline", "data_quality", offline,
                         np.full((T, S), "medium", dtype=object), np.zeros((T, S)),
                         lambda t, s: "No data for >= 15 min (radio/gateway outage?); readings may arrive late "
                                      "from the gateway buffer", needs_running=False))
    for ch in C.CHANNELS:
        c = CH[ch]
        stuck = cl.stuck[:, :, c]
        if stuck.any():
            out.append(Condition("sensor_stuck", "data_quality", stuck, np.full((T, S), "low", dtype=object),
                                 np.zeros((T, S)),
                                 (lambda ch_: lambda t, s: f"{ch_} flat-lined (identical value for >= 1 h); "
                                                           f"excluded from analytics")(ch),
                                 needs_running=False, channel=ch))
        errs = pd.DataFrame((cl.bad_ingest[:, :, c] | cl.spike[:, :, c]).astype(float)).rolling(
            6 * PER_H, min_periods=1).sum().to_numpy()
        if (errs >= 4).any():
            out.append(Condition("sensor_noisy", "data_quality", errs >= 4, np.full((T, S), "low", dtype=object), errs,
                                 (lambda ch_, e: lambda t, s: f"{ch_}: {int(e[t, s])} error codes/spikes in 6 h")(ch, errs),
                                 needs_running=False, channel=ch))
    return out


@dataclass
class _State:
    id: int
    started: int
    false_run: int = 0
    peak: float = 0.0
    severity: str = "low"


class AnomalyEngine:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.state: dict[tuple, _State] = {}
        for r in con.execute("SELECT id, station_id, type, evidence, started_ts, peak_score, severity FROM anomalies "
                             "WHERE status='active'"):
            ch = json.loads(r["evidence"] or "{}").get("channel")
            self.state[(r["station_id"], r["type"], ch)] = _State(r["id"], r["started_ts"], 0, r["peak_score"] or 0,
                                                                  r["severity"])

    def update(self, cl: Cleaned, sig: Signals, i0: int) -> None:
        ts = cl.frame.ts
        run = cl.running
        for cond in conditions(cl, sig):
            act = cond.active
            stations = np.flatnonzero(act[i0:].any(axis=0) | np.array(
                [(st.id, cond.type, cond.channel) in self.state for st in C.STATIONS]))
            for s in stations:
                sid = C.STATIONS[s].id
                key = (sid, cond.type, cond.channel)
                for t in range(i0, len(ts)):
                    if cond.needs_running and not run[t]:
                        continue                          # frozen while stopped
                    st = self.state.get(key)
                    if st is None:
                        idx = np.flatnonzero(run[: t + 1])[-OPEN_N:] if cond.needs_running else np.arange(max(0, t - OPEN_N + 1), t + 1)
                        if act[t, s] and act[idx, s].sum() >= OPEN_K:
                            self._open(key, cond, t, s, ts)
                    else:
                        if (cond.hold if cond.hold is not None else act)[t, s]:
                            st.false_run = 0
                            self._touch(st, cond, t, s, ts)
                        else:
                            st.false_run += 1
                            if st.false_run >= CLEAR_N:
                                self.con.execute("UPDATE anomalies SET status='cleared', cleared_ts=? WHERE id=?",
                                                 (int(ts[t]), st.id))
                                del self.state[key]

    def _open(self, key, cond: Condition, t: int, s: int, ts) -> None:
        sev = cond.severity[t, s] or "low"
        score = float(np.nan_to_num(cond.score[t, s]))
        cur = self.con.execute(
            "INSERT INTO anomalies(station_id, category, type, severity, status, started_ts, last_ts, score, peak_score,"
            " message, evidence) VALUES (?,?,?,?, 'active', ?,?,?,?,?,?)",
            (key[0], cond.category, cond.type, sev, int(ts[t]), int(ts[t]), score, score, cond.message(t, s),
             json.dumps({"channel": cond.channel})))
        self.state[key] = _State(cur.lastrowid, int(ts[t]), 0, score, sev)

    def _touch(self, st: _State, cond: Condition, t: int, s: int, ts) -> None:
        sev = cond.severity[t, s] or st.severity
        score = float(np.nan_to_num(cond.score[t, s]))
        st.peak = max(st.peak, score)
        # severity follows the current reading, but never drops below the peak minus one level
        if SEV_ORDER.get(sev, 0) < SEV_ORDER.get(st.severity, 0) - 1:
            sev = [k for k, v in SEV_ORDER.items() if v == SEV_ORDER[st.severity] - 1][0]
        st.severity = sev
        self.con.execute("UPDATE anomalies SET last_ts=?, score=?, peak_score=?, severity=?, message=? WHERE id=?",
                         (int(ts[t]), score, st.peak, sev, cond.message(t, s), st.id))
