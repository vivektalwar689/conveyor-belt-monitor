"""Time-series data-quality checks that need context (run on every analysis cycle).

Ingestion already removed error codes and impossible values. Here we catch the
problems that only show up in a series:

* spikes   - isolated samples far from BOTH neighbours (EMI). Needs the next
             sample, so the newest point is judged one cycle later. A start/stop
             step is *not* a spike because the following samples agree with it.
* stuck    - >= 1 h of bit-identical values on a channel that always carries
             noise (temperatures, vibration, offset). Speed/current/load are
             legitimately exactly 0 while the belt is stopped, so they are only
             judged while running.
* offline  - station/channel silent for >= 15 min while its peers report.

Everything works on dense (T, S, C) arrays on the 5-minute grid.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from simulator import config as C

GRID = C.SAMPLE_PERIOD_S
CH = C.CHANNEL_INDEX
STUCK_SAMPLES = 12
ZERO_WHEN_STOPPED = {"belt_speed", "drive_speed", "motor_current", "load_tph"}
# floors for the local noise scale used by the spike test (canonical units)
SCALE_FLOOR = {"vib_rms": 0.15, "vib_env": 0.08, "bearing_temp": 1.0, "ambient_temp": 1.0, "belt_offset": 4.0,
               "belt_speed": 0.05, "load_tph": 150, "belt_tension": 4.0, "takeup_pos": 0.03, "motor_current": 15,
               "motor_temp": 1.5, "gearbox_temp": 1.0, "gearbox_vib": 0.2, "drive_speed": 0.05}


@dataclass
class Frame:
    t0: int                  # epoch seconds of index 0
    values: np.ndarray       # (T, S, C) canonical values, NaN = missing
    quality: np.ndarray      # (T, S, C) ingest quality flags
    present: np.ndarray      # (T, S, C) a row exists (even if value NULL)

    @property
    def T(self) -> int:
        return self.values.shape[0]

    @property
    def ts(self) -> np.ndarray:
        return self.t0 + GRID * np.arange(self.T)


def build_frame(df: pd.DataFrame, t0: int, t1: int) -> Frame:
    """df: normalised readings (station_id, channel, ts, value, quality). Inclusive [t0, t1]."""
    T = int((t1 - t0) // GRID) + 1
    S, Cn = len(C.STATIONS), len(C.CHANNELS)
    values = np.full((T, S, Cn), np.nan)
    quality = np.zeros((T, S, Cn), dtype=np.int64)
    present = np.zeros((T, S, Cn), dtype=bool)
    d = df[(df.ts >= t0) & (df.ts <= t1)]
    ti = ((d.ts.to_numpy() - t0) // GRID).astype(int)
    si = d.station_id.map(C.STATION_INDEX).to_numpy()
    ci = d.channel.map(CH).to_numpy()
    values[ti, si, ci] = d.value.to_numpy(dtype=float)
    quality[ti, si, ci] = d.quality.to_numpy()
    present[ti, si, ci] = True
    return Frame(t0, values, quality, present)


def running_state(v: np.ndarray) -> np.ndarray:
    """Belt running per time step, robust to the drive station being offline.

    Priority: drive pulley speed -> tail belt speed -> fleet median vibration.
    """
    drive = v[:, C.STATION_INDEX["S24"], CH["drive_speed"]]
    tail = v[:, C.STATION_INDEX["S01"], CH["belt_speed"]]
    import warnings
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        vib = np.nanmedian(v[:, :, CH["vib_rms"]], axis=1)
    run = np.where(~np.isnan(drive), drive > 0.5,
                   np.where(~np.isnan(tail), tail > 0.5, np.where(~np.isnan(vib), vib > 0.3, False)))
    return run.astype(bool)


def _prev_next(x: np.ndarray, maxgap: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Last valid value before t and first valid after t (within maxgap), along axis 0."""
    T = x.shape[0]
    prev = np.full_like(x, np.nan)
    nxt = np.full_like(x, np.nan)
    for k in range(1, maxgap + 1):
        sh = np.full_like(x, np.nan)
        sh[k:] = x[:-k]
        prev = np.where(np.isnan(prev), sh, prev)
        sh = np.full_like(x, np.nan)
        sh[:T - k] = x[k:]
        nxt = np.where(np.isnan(nxt), sh, nxt)
    return prev, nxt


def detect_spikes(v: np.ndarray) -> np.ndarray:
    T, S, Cn = v.shape
    flat = v.reshape(T, S * Cn)
    prev, nxt = _prev_next(flat)
    # local scale: rolling median absolute successive difference over 2 h
    diff = np.abs(np.diff(flat, axis=0, prepend=np.nan))
    scale = pd.DataFrame(diff).rolling(24, min_periods=4).median().to_numpy() * 1.4826
    floor = np.tile(np.array([SCALE_FLOOR[c] for c in C.CHANNELS]), S)
    scale = np.fmax(np.nan_to_num(scale, nan=0.0), floor)
    k = 10.0
    with np.errstate(invalid="ignore"):
        a = np.abs(flat - prev) > k * scale
        b = np.abs(flat - nxt) > k * scale
        agree = np.abs(prev - nxt) < 0.5 * k * scale
        both = a & b & agree
        # newest samples have no successor yet: only flag if absurdly far from the predecessor
        only_prev = a & np.isnan(nxt) & (np.abs(flat - prev) > 4 * k * scale)
    return (both | only_prev).reshape(T, S, Cn)


def detect_stuck(v: np.ndarray, running: np.ndarray) -> np.ndarray:
    T, S, Cn = v.shape
    out = np.zeros_like(v, dtype=bool)
    same = np.zeros_like(v, dtype=bool)
    same[1:] = (v[1:] == v[:-1]) & ~np.isnan(v[1:])
    for c, name in enumerate(C.CHANNELS):
        sc = same[:, :, c].copy()
        if name in ZERO_WHEN_STOPPED:
            sc &= running[:, None]
        for s in range(S):
            col = sc[:, s]
            if not col.any():
                continue
            # run lengths of consecutive "same as previous"
            idx = np.flatnonzero(np.diff(np.concatenate(([0], col.view(np.int8), [0]))))
            for start, end in zip(idx[::2], idx[1::2]):
                if end - start >= STUCK_SAMPLES - 1:
                    out[start - 1:end, s, c] = True
    return out


@dataclass
class Cleaned:
    frame: Frame
    clean: np.ndarray        # (T, S, C) values usable for analytics (NaN otherwise)
    running: np.ndarray      # (T,)
    spike: np.ndarray
    stuck: np.ndarray
    bad_ingest: np.ndarray


def clean(frame: Frame) -> Cleaned:
    from backend.ingest import Q_ERROR_CODE, Q_OUT_OF_RANGE

    v = frame.values.copy()
    bad_ingest = (frame.quality & (Q_ERROR_CODE | Q_OUT_OF_RANGE)) > 0
    v[bad_ingest] = np.nan
    spike = detect_spikes(v)
    v[spike] = np.nan
    running = running_state(v)
    stuck = detect_stuck(v, running)
    v[stuck] = np.nan
    return Cleaned(frame, v, running, spike, stuck, bad_ingest)


def sensor_health(cl: Cleaned, window_samples: int = 288) -> list[dict]:
    """Per station/channel health over the last window (default 24 h)."""
    f = cl.frame
    w = slice(max(0, f.T - window_samples), f.T)
    out = []
    for si, st in enumerate(C.STATIONS):
        for ch in st.channels:
            c = CH[ch]
            pres = f.present[w, si, c]
            valid = ~np.isnan(cl.clean[w, si, c])
            last_valid = np.flatnonzero(~np.isnan(cl.clean[:, si, c]))
            out.append({
                "station_id": st.id, "channel": ch,
                "completeness": round(float(valid.mean()), 3),
                "received": round(float(pres.mean()), 3),
                "spikes": int(cl.spike[w, si, c].sum()),
                "stuck_samples": int(cl.stuck[w, si, c].sum()),
                "error_codes": int(cl.bad_ingest[w, si, c].sum()),
                "stuck_now": bool(cl.stuck[-STUCK_SAMPLES:, si, c].any()),
                "last_valid_ts": int(f.ts[last_valid[-1]]) if len(last_valid) else None,
            })
    return out
