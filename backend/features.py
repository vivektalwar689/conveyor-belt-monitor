"""Signal normalisation and features, shared by live scoring and offline training.

The core idea is *double normalisation*:

1. Peer-relative: at every 5-minute step, compare a station with the median of
   all stations. Weather, dust, load surges and start-ups hit every station at
   once, so they cancel out. (Vibration is compared in log space, i.e. as a ratio.)
2. Self-baseline: compare that peer-relative signal with the station's *own*
   lagged baseline (median of the previous 6 days, excluding the last 24 h so a
   slow degradation does not hide itself in its own baseline). Different stations
   have different "normal" levels (installation, location), so a fleet-wide
   threshold would be wrong for half of them.

What remains is "how unusual is this station, compared with itself and with the
others right now" - a quantity that is comparable across stations and therefore
lets one model learn from failures anywhere on the belt.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

from backend.quality import Cleaned
from simulator import config as C

GRID = C.SAMPLE_PERIOD_S
PER_H = 3600 // GRID
CH = C.CHANNEL_INDEX
STRESS = np.array([s.stress for s in C.STATIONS])
IS_CURVE = np.array([s.zone == "curve" for s in C.STATIONS], dtype=float)
SCALE_FLOOR = {"env": 0.06, "rms": 0.05, "trise": 0.6, "off": 3.0}
BASELINE_DAYS, BASELINE_LAG_H = 6, 24


def _roll(a: np.ndarray, w: int, fn: str, minp: int) -> np.ndarray:
    r = pd.DataFrame(a).rolling(w, min_periods=minp)
    return getattr(r, fn)().to_numpy()


def _rolling_slope(y: np.ndarray, w: int, minp: int) -> np.ndarray:
    """OLS slope per day of y vs time over a trailing window, NaN-aware. y: (T, S)."""
    T = y.shape[0]
    t = (np.arange(T, dtype=float) / (86400 / GRID))[:, None] * np.ones_like(y)
    m = ~np.isnan(y)
    yz, tz = np.where(m, y, 0.0), np.where(m, t, 0.0)
    n = _roll(m.astype(float), w, "sum", 1)
    st, sy = _roll(tz, w, "sum", 1), _roll(yz, w, "sum", 1)
    stt, sty = _roll(tz * tz, w, "sum", 1), _roll(tz * yz, w, "sum", 1)
    with np.errstate(all="ignore"):
        den = n * stt - st * st
        slope = (n * sty - st * sy) / den
    slope[(n < minp) | (np.abs(den) < 1e-12)] = np.nan
    return slope


@dataclass
class Signals:
    """Normalised per-station signals on the 5-min grid, all (T, S)."""
    ts: np.ndarray
    running: np.ndarray
    raw: dict[str, np.ndarray]       # running-only raw signals (env, rms, trise, off)
    rel: dict[str, np.ndarray]       # peer-relative
    sm: dict[str, np.ndarray]        # 1-h trailing mean of rel
    base: dict[str, np.ndarray]      # lagged self-baseline of sm
    scale: dict[str, np.ndarray]     # robust scale of the baseline window
    z: dict[str, np.ndarray]         # (sm - base) / scale
    load_frac: np.ndarray            # (T,) belt load fraction (context)
    valid_frac_24h: np.ndarray       # (T, S) data completeness of the key channels


BASELINE_WINDOW_H = 144     # accepted running hours in the baseline (~8 calendar days at 18 h/day)
BASELINE_MIN_H = 12
FREEZE_RELEARN_H = 168      # if a station has looked "anomalous" for 7 days, accept the new normal


def frozen_baseline(x: np.ndarray, kind: str, sig_ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Self-baseline that cannot absorb a degradation.

    Sequential over hourly values: an hour is added to the station's baseline only if
    it is *not* anomalous against the baseline at that time (and only after a 24 h lag).
    While a station degrades, its baseline therefore freezes at the last healthy level
    instead of drifting up with the fault (the failure mode of a plain rolling median).
    After FREEZE_RELEARN_H of continuous "anomaly" the baseline re-learns, which covers
    a component swap nobody logged. Returns (baseline, scale) on the 5-min grid.
    """
    from collections import deque

    T, S = x.shape
    hour_of = ((sig_ts - sig_ts[0]) // 3600).astype(int)
    n_h = int(hour_of[-1]) + 1
    first = np.searchsorted(hour_of, np.arange(n_h))
    hv = np.full((n_h, S), np.nan)
    ok = first < T
    hv[ok] = x[np.minimum(first[ok] + PER_H - 1, T - 1)]       # 1-h mean at the end of each hour
    floor = SCALE_FLOOR[kind]
    bh = np.full((n_h, S), np.nan)
    sh = np.full((n_h, S), floor)
    for s in range(S):
        buf: deque = deque(maxlen=BASELINE_WINDOW_H)
        pending: deque = deque()
        frozen_since = None
        b, sc = np.nan, floor
        for h in range(n_h):
            changed = False
            while pending and pending[0][0] <= h - BASELINE_LAG_H:
                buf.append(pending.popleft()[1])
                changed = True
            if changed and len(buf) >= BASELINE_MIN_H:
                arr = np.fromiter(buf, float)
                b = float(np.median(arr))
                sc = max(1.4826 * float(np.median(np.abs(arr - b))), floor)
            bh[h, s], sh[h, s] = b, sc
            v = hv[h, s]
            if np.isnan(v):
                continue
            if np.isnan(b):
                pending.append((h, v))
                continue
            dev = v - b
            anomalous = (abs(dev) > max(3 * sc, 20.0)) if kind == "off" else (dev / sc > 2.5)
            if not anomalous:
                pending.append((h, v))
                frozen_since = None
            elif frozen_since is None:
                frozen_since = h
            elif h - frozen_since > FREEZE_RELEARN_H:
                pending.append((h, v))
    base = bh[hour_of]
    scale = sh[hour_of]
    # before any baseline exists, fall back to a robust level of what has been seen so far
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        early = pd.DataFrame(x).expanding(min_periods=3 * PER_H).median().to_numpy()
    base = np.where(np.isnan(base), early, base)
    return base, np.fmax(np.nan_to_num(scale, nan=floor), floor)


def compute_signals(cl: Cleaned) -> Signals:
    v, run = cl.clean, cl.running
    T, S = v.shape[:2]
    rmask = run[:, None]
    # nanmedian over a step where the belt is stopped (no running peers) is expected to be all-NaN
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        raw = {
            "env": np.where(rmask, v[:, :, CH["vib_env"]], np.nan),
            "rms": np.where(rmask, v[:, :, CH["vib_rms"]], np.nan),
            "trise": np.where(rmask, v[:, :, CH["bearing_temp"]] - v[:, :, CH["ambient_temp"]], np.nan),
            "off": np.where(rmask, v[:, :, CH["belt_offset"]], np.nan),
        }
        rel = {}
        for k in ("env", "rms"):
            lg = np.log(np.maximum(raw[k], 1e-3))
            n_ok = (~np.isnan(lg)).sum(axis=1, keepdims=True)
            med = np.nanmedian(lg, axis=1, keepdims=True)
            rel[k] = np.where(n_ok >= 6, lg - med, np.nan)
        n_ok = (~np.isnan(raw["trise"])).sum(axis=1, keepdims=True)
        rel["trise"] = np.where(n_ok >= 6, raw["trise"] - np.nanmedian(raw["trise"], axis=1, keepdims=True), np.nan)
        rel["off"] = raw["off"]                              # offset is meaningful in absolute terms
    sm, base, scale, z = {}, {}, {}, {}
    for k in rel:
        sm[k] = _roll(rel[k], PER_H, "mean", 3)
        base[k], scale[k] = frozen_baseline(sm[k], k, sig_ts=cl.frame.ts)
        z[k] = (sm[k] - base[k]) / scale[k]
    load = v[:, C.STATION_INDEX["S02"], CH["load_tph"]] / C.DESIGN_CAPACITY_TPH
    key = [CH[c] for c in ("vib_env", "vib_rms", "bearing_temp", "belt_offset")]
    valid = (~np.isnan(v[:, :, key])).mean(axis=2)
    valid_frac = _roll(valid, 24 * PER_H, "mean", 1)
    return Signals(cl.frame.ts, run, raw, rel, sm, base, scale, z, load, valid_frac)


BEARING_FEATURES = [
    "env_dev_1h", "env_dev_6h", "env_dev_24h", "env_z", "env_slope_24h", "env_slope_72h",
    "rms_dev_1h", "rms_dev_6h", "rms_dev_24h", "rms_z", "rms_slope_24h", "rms_slope_72h",
    "trise_dev_1h", "trise_dev_6h", "trise_dev_24h", "trise_z", "trise_slope_24h", "trise_slope_72h",
    "rms_abs_1h", "trise_abs_1h", "load_6h", "run_frac_24h", "valid_frac_24h", "stress",
]
MISALIGN_FEATURES = [
    "off_abs_1h", "off_abs_6h", "off_abs_24h", "off_dev_24h", "off_growth_24h", "off_growth_72h",
    "off_std_6h", "load_6h", "run_frac_24h", "valid_frac_24h", "stress", "is_curve",
]


def feature_table(sig: Signals, idx: np.ndarray | None = None) -> pd.DataFrame:
    """Features for every station at the time indices ``idx`` (default: last step).

    Returns a long DataFrame with one row per (time, station).
    """
    T, S = sig.running.shape[0], len(C.STATIONS)
    if idx is None:
        idx = np.array([T - 1])
    f: dict[str, np.ndarray] = {}

    def at(a: np.ndarray) -> np.ndarray:
        return a[idx]

    for k in ("env", "rms", "trise"):
        dev = sig.sm[k] - sig.base[k]
        f[f"{k}_dev_1h"] = at(dev)
        f[f"{k}_dev_6h"] = at(_roll(sig.rel[k], 6 * PER_H, "mean", PER_H) - sig.base[k])
        f[f"{k}_dev_24h"] = at(_roll(sig.rel[k], 24 * PER_H, "mean", 2 * PER_H) - sig.base[k])
        f[f"{k}_z"] = at(sig.z[k])
        f[f"{k}_slope_24h"] = at(_rolling_slope(sig.rel[k], 24 * PER_H, 3 * PER_H))
        f[f"{k}_slope_72h"] = at(_rolling_slope(sig.rel[k], 72 * PER_H, 6 * PER_H))
    f["rms_abs_1h"] = at(_roll(sig.raw["rms"], PER_H, "mean", 3))
    f["trise_abs_1h"] = at(_roll(sig.raw["trise"], PER_H, "mean", 3))
    off = sig.raw["off"]
    off1 = _roll(off, PER_H, "mean", 3)
    sign = np.sign(np.nan_to_num(off1))
    f["off_abs_1h"] = at(np.abs(off1))
    f["off_abs_6h"] = at(np.abs(_roll(off, 6 * PER_H, "mean", PER_H)))
    f["off_abs_24h"] = at(np.abs(_roll(off, 24 * PER_H, "mean", 2 * PER_H)))
    f["off_dev_24h"] = at(np.abs(off1 - sig.base["off"]))
    f["off_growth_24h"] = at(_rolling_slope(off, 24 * PER_H, 3 * PER_H) * sign)
    f["off_growth_72h"] = at(_rolling_slope(off, 72 * PER_H, 6 * PER_H) * sign)
    f["off_std_6h"] = at(_roll(off, 6 * PER_H, "std", PER_H))
    load6 = _roll(sig.load_frac[:, None], 6 * PER_H, "mean", 1)[:, 0]
    runf = _roll(sig.running[:, None].astype(float), 24 * PER_H, "mean", 1)[:, 0]
    n = len(idx)
    out = pd.DataFrame({
        "t_idx": np.repeat(idx, S),
        "ts": np.repeat(sig.ts[idx], S),
        "station": np.tile(np.arange(S), n),
        "station_id": np.tile([s.id for s in C.STATIONS], n),
        "running": np.repeat(sig.running[idx], S),
        "load_6h": np.repeat(load6[idx], S),
        "run_frac_24h": np.repeat(runf[idx], S),
        "valid_frac_24h": at(sig.valid_frac_24h).ravel(),
        "stress": np.tile(STRESS, n),
        "is_curve": np.tile(IS_CURVE, n),
    })
    for k, a in f.items():
        out[k] = a.ravel()
    return out
