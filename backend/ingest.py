"""Validation and normalisation of incoming readings.

Policy (the short version): *never silently guess*. Every reading ends up in
exactly one of three places:

* rejected  -> dead-letter table with a reason (unknown station/sensor, no
               timezone, future timestamp, incompatible unit, ...);
* stored with a "bad" quality flag -> kept for audit, excluded from analytics
               (error codes, physically impossible values);
* stored clean (possibly with informational flags: unit converted, legacy tag
               name mapped, arrived late).

This module is pure pandas and is used by both the live API and the offline
training pipeline, so the model sees data cleaned exactly like production data.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from simulator import config as C

# ---- quality bit flags
Q_ERROR_CODE = 1        # null / "ERR" / sentinel (-999, 9999, 0xFFFF/10 ...) -> value set to NULL
Q_OUT_OF_RANGE = 2      # outside the physical range of the channel
Q_SPIKE = 4             # analysis-time: Hampel outlier
Q_STUCK = 8             # analysis-time: flat-lined sensor
Q_UNIT_CONVERTED = 16   # info
Q_LATE = 32             # info: arrived > 15 min after it was measured (store-and-forward)
Q_UNIT_ASSUMED = 64     # info: no unit supplied, canonical unit assumed
Q_ALIAS = 128           # info: legacy tag name mapped to canonical channel
BAD = Q_ERROR_CODE | Q_OUT_OF_RANGE | Q_SPIKE | Q_STUCK

SENTINELS = {-999.0, -9999.0, 9999.0, 6553.5, 65535.0, -32768.0, 32767.0}
SENSOR_ALIASES = {
    "MTR_CURRENT_A": "motor_current", "GBX_OIL_T": "gearbox_temp", "GBX_VIB_MMS": "gearbox_vib",
    "PULLEY_SPD": "drive_speed", "BRG_TEMP": "bearing_temp", "AMB_TEMP": "ambient_temp",
}
UNIT_ALIASES = {
    "degc": "degC", "c": "degC", "°c": "degC", "celsius": "degC",
    "degf": "degF", "f": "degF", "°f": "degF", "fahrenheit": "degF",
    "k": "K", "kelvin": "K",
    "mm/s": "mm/s", "in/s": "in/s", "ips": "in/s",
    "g": "g", "m/s2": "m/s2", "m/s^2": "m/s2",
    "mm": "mm", "m": "m", "m/s": "m/s", "t/h": "t/h", "tph": "t/h", "kn": "kN", "a": "A",
}
CONVERSIONS = {
    ("degF", "degC"): lambda v: (v - 32) * 5 / 9,
    ("K", "degC"): lambda v: v - 273.15,
    ("in/s", "mm/s"): lambda v: v * 25.4,
    ("m/s2", "g"): lambda v: v / 9.80665,
    ("m", "mm"): lambda v: v * 1000,
}
GRID_S = C.SAMPLE_PERIOD_S
LATE_AFTER_S = 900
MAX_FUTURE_S = 600
MAX_AGE_S = 30 * 86400
_TZ_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")
_CANON_UNIT = {ch: u for ch, (u, *_rest) in C.CHANNELS.items()}
_LO = {ch: lo for ch, (_u, lo, _hi, _d) in C.CHANNELS.items()}
_HI = {ch: hi for ch, (_u, _lo, hi, _d) in C.CHANNELS.items()}
_INSTALLED = {(s.id, ch) for s in C.STATIONS for ch in s.channels}


def _parse_ts(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Mixed epoch-ms / epoch-s / ISO-8601 -> (epoch seconds float, reason-if-invalid)."""
    out = pd.Series(np.nan, index=raw.index)
    reason = pd.Series("", index=raw.index, dtype=object)
    num = pd.to_numeric(raw, errors="coerce")
    is_num = num.notna()
    ms = is_num & (num > 1e12)
    s = is_num & (num > 1e9) & (num <= 1e12)
    out[ms] = num[ms] / 1000.0
    out[s] = num[s]
    reason[is_num & ~(ms | s)] = "bad_timestamp"
    strs = raw[~is_num & raw.notna()].astype(str)
    if len(strs):
        has_tz = strs.str.contains(_TZ_RE)
        reason[strs.index[~has_tz]] = "timestamp_without_timezone"
        ok = strs[has_tz]
        parsed = pd.to_datetime(ok, utc=True, errors="coerce", format="ISO8601")
        good = parsed.notna()
        # resolution-independent (pandas 3 may parse to "us" or "s" resolution, not "ns")
        out[ok.index[good]] = (parsed[good] - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)
        reason[ok.index[~good]] = "bad_timestamp"
    reason[raw.isna()] = "missing_field"
    return out, reason


def normalize(df: pd.DataFrame, received_ts) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Normalise a batch of raw readings.

    df columns: station_id, sensor, ts, value, unit (optional), seq (optional).
    received_ts: scalar or Series of epoch seconds (when the backend received it).
    Returns (accepted, rejected, counts). accepted has columns
    station_id, channel, ts (int), value (float|NaN), quality (int), seq, received_ts.
    """
    df = df.copy()
    for col in ("station_id", "sensor", "ts", "value", "unit", "seq"):
        if col not in df:
            df[col] = None
    df["received_ts"] = received_ts
    n = len(df)
    reason = pd.Series("", index=df.index, dtype=object)
    q = pd.Series(0, index=df.index, dtype=np.int64)

    def reject(mask: pd.Series, why: str) -> None:
        m = mask & (reason == "")
        reason[m] = why

    reject(df.station_id.isna() | df.sensor.isna(), "missing_field")
    df["station_id"] = df.station_id.astype(str).str.strip().str.upper()
    reject(~df.station_id.isin([s.id for s in C.STATIONS]), "unknown_station")

    # sensor names: canonical (case-insensitive) or a known legacy alias
    sensor = df.sensor.astype(str).str.strip()
    channel = sensor.str.lower().where(sensor.str.lower().isin(C.CHANNELS.keys()))
    alias = sensor.str.upper().map(SENSOR_ALIASES)
    q[channel.isna() & alias.notna()] |= Q_ALIAS
    df["channel"] = channel.fillna(alias)
    reject(df.channel.isna(), "unknown_sensor")
    installed = pd.Series([(a, b) in _INSTALLED for a, b in zip(df.station_id, df.channel)], index=df.index)
    reject(~installed, "channel_not_installed")

    # timestamps
    ts, ts_reason = _parse_ts(df.ts)
    for why in ("missing_field", "bad_timestamp", "timestamp_without_timezone"):
        reject(ts_reason == why, why)
    rec = pd.Series(received_ts, index=df.index, dtype=float) if np.isscalar(received_ts) else received_ts.astype(float)
    reject(ts > rec + MAX_FUTURE_S, "future_timestamp")
    reject(ts < rec - MAX_AGE_S, "stale_timestamp")
    df["ts"] = (np.round(ts / GRID_S) * GRID_S)
    q[(rec - ts) > LATE_AFTER_S] |= Q_LATE

    # values: error codes / sentinels -> NULL with a flag (kept for audit)
    val = pd.to_numeric(df.value, errors="coerce")
    err = val.isna() | val.isin(list(SENTINELS)) | ~np.isfinite(val.fillna(0))
    q[err] |= Q_ERROR_CODE
    val[err] = np.nan

    # units
    unit_raw = df.unit.where(df.unit.notna(), None)
    unit = unit_raw.astype(str).str.strip().str.lower().map(UNIT_ALIASES)
    no_unit = unit_raw.isna() | (unit_raw.astype(str).str.strip() == "")
    canon = df.channel.map(_CANON_UNIT)
    unit = unit.where(~no_unit, canon)
    q[no_unit] |= Q_UNIT_ASSUMED
    reject(unit.isna(), "unknown_unit")
    needs = unit.notna() & canon.notna() & (unit != canon)
    for (src, dst), fn in CONVERSIONS.items():
        m = needs & (unit == src) & (canon == dst)
        val[m] = fn(val[m])
        q[m] |= Q_UNIT_CONVERTED
        needs &= ~m
    reject(needs, "unit_mismatch")

    # physical range (kept, flagged; an impossible value is a sensor problem, not a belt problem)
    lo, hi = df.channel.map(_LO), df.channel.map(_HI)
    oor = val.notna() & ((val < lo) | (val > hi))
    q[oor] |= Q_OUT_OF_RANGE

    df["value"] = val
    df["quality"] = q
    rejected = df[reason != ""].assign(reason=reason[reason != ""])
    acc = df[reason == ""]
    before = len(acc)
    acc = acc.drop_duplicates(subset=["station_id", "channel", "ts"], keep="first")
    counts = {
        "received": n,
        "rejected": int((reason != "").sum()),
        "duplicate_in_batch": before - len(acc),
        "error_code": int(((acc.quality & Q_ERROR_CODE) > 0).sum()),
        "out_of_range": int(((acc.quality & Q_OUT_OF_RANGE) > 0).sum()),
        "unit_converted": int(((acc.quality & Q_UNIT_CONVERTED) > 0).sum()),
        "alias": int(((acc.quality & Q_ALIAS) > 0).sum()),
        "late": int(((acc.quality & Q_LATE) > 0).sum()),
        "reject_reasons": reason[reason != ""].value_counts().to_dict(),
    }
    acc = acc.assign(ts=acc.ts.astype(np.int64), seq=pd.to_numeric(acc.seq, errors="coerce"))
    return acc[["station_id", "channel", "ts", "value", "quality", "seq", "received_ts"]], rejected, counts


def store(con, acc: pd.DataFrame) -> int:
    """Idempotent insert. Returns number of rows actually inserted (rest were duplicates)."""
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO readings(station_id, channel, ts, value, quality, seq, received_ts) VALUES (?,?,?,?,?,?,?)",
        [(r.station_id, r.channel, int(r.ts), None if pd.isna(r.value) else float(r.value), int(r.quality),
          None if pd.isna(r.seq) else int(r.seq), int(r.received_ts)) for r in acc.itertuples(index=False)])
    return con.total_changes - before
