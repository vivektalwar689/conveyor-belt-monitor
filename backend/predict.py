"""Failure prediction: what will fail, where, and when.

Two methods, chosen by how much failure history a component type can have:

* ML (bearing, misalignment): ~24 stations share the same physics, so failures
  anywhere on the belt are training examples for every station. A gradient-boosted
  classifier gives P(failure within 72 h); quantile regressors give the time to
  failure as a P10/P50/P90 band.
* Trend-to-threshold (drive gearbox, drive pulley slip): there is ONE drive. It
  will not produce enough failures to learn from in years, so we extrapolate a
  physically meaningful health indicator (gearbox vibration vs. ISO 10816 zone D,
  oil temperature vs. its alarm limit, slip vs. the slip-switch setting) using an
  exponential fit with bootstrap uncertainty. No labels needed.

Degradation only advances while the belt runs, so trends are fitted in *running*
hours and converted back to calendar time with the duty schedule (18 h/day,
planned stop 00:00-06:00 local).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from backend.features import BEARING_FEATURES, MISALIGN_FEATURES, PER_H, Signals
from backend.quality import Cleaned
from simulator import config as C

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "models" / "sensor_models.joblib"
HORIZON_H = 72
CH = C.CHANNEL_INDEX


# --------------------------------------------------------------------------- schedule
def running_to_calendar(now_ts: float, running_h: float) -> float:
    """Calendar hours until ``running_h`` hours of belt running have elapsed."""
    if not np.isfinite(running_h):
        return float("inf")
    t = datetime.fromtimestamp(now_ts, timezone.utc).astimezone(C.SITE_TZ)
    remaining, cal = running_h, 0.0
    a, b = C.PLANNED_STOP_LOCAL_HOURS
    while remaining > 1e-9 and cal < 24 * 365:
        h = t.hour + t.minute / 60
        if a <= h < b:                      # in planned stop: jump to its end
            step = b - h
            cal += step
            t += timedelta(hours=step)
            continue
        until_stop = (24 - h) if h >= b else (a - h)
        step = min(until_stop, remaining)
        cal += step
        remaining -= step
        t += timedelta(hours=step)
    return cal


def calendar_to_running(now_ts: float, cal_h: float) -> float:
    lo, hi = 0.0, cal_h
    for _ in range(40):
        mid = (lo + hi) / 2
        if running_to_calendar(now_ts, mid) < cal_h:
            lo = mid
        else:
            hi = mid
    return lo


# --------------------------------------------------------------------------- ML
@dataclass
class ModeModel:
    features: list[str]
    clf: object
    q10: object
    q50: object
    q90: object
    threshold: float
    cqr: float = 0.0        # conformal widening of the P10-P90 band (log-hours), fitted on the held-out validation run

    def ttf_quantiles(self, X: np.ndarray) -> np.ndarray:
        q = np.column_stack([self.q10.predict(X), self.q50.predict(X), self.q90.predict(X)])
        q.sort(axis=1)
        q[:, 0] -= self.cqr
        q[:, 2] += self.cqr
        return np.exp(q)


class SensorModels:
    def __init__(self, path: Path = MODEL_PATH):
        self.ok = path.exists()
        self.meta: dict = {}
        self.models: dict[str, ModeModel] = {}
        if self.ok:
            blob = joblib.load(path)
            self.meta = blob["meta"]
            self.models = blob["models"]

    def predict(self, feats: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for mode, mm in self.models.items():
            kinds = {s.id for s in C.STATIONS if mode in s.modes}
            f = feats[feats.station_id.isin(kinds)]
            if f.empty:
                continue
            X = f[mm.features].to_numpy(dtype=float)
            p = mm.clf.predict_proba(X)[:, 1]
            q = mm.ttf_quantiles(X)
            for (_, r), pi, qi in zip(f.iterrows(), p, q):
                conf = "high" if r.valid_frac_24h >= 0.9 else ("reduced" if r.valid_frac_24h >= 0.6 else "low")
                rows.append({"station_id": r.station_id, "mode": mode, "probability": float(pi),
                             "ttf_p10_h": float(qi[0]), "ttf_p50_h": float(qi[1]), "ttf_p90_h": float(qi[2]),
                             "method": "ml", "confidence": conf, "threshold": mm.threshold,
                             "evidence": _evidence(mode, r)})
        return pd.DataFrame(rows)


def _evidence(mode: str, r) -> str:
    if mode == "bearing":
        parts = [f"HF envelope {r.env_z:+.1f}σ", f"vibration {r.rms_z:+.1f}σ ({r.rms_abs_1h:.1f} mm/s)",
                 f"temp rise {r.trise_z:+.1f}σ ({r.trise_abs_1h:.0f}°C over ambient)"]
        return "; ".join(p for p in parts if "nan" not in p)
    return f"belt offset {r.off_abs_1h:.0f} mm, growing {r.off_growth_24h:+.0f} mm/day"


# --------------------------------------------------------------------------- trend-to-threshold
def _hourly_running(x: np.ndarray, running: np.ndarray, hours: int) -> np.ndarray:
    """Hourly means of x over running samples only, last ``hours`` running hours."""
    xs = x[running & ~np.isnan(x)]
    n = (len(xs) // PER_H) * PER_H
    if n == 0:
        return np.array([])
    hm = xs[len(xs) - n:].reshape(-1, PER_H).mean(axis=1)
    return hm[-hours:]


def _hourly_pairs(x: np.ndarray, lf: np.ndarray, running: np.ndarray, hours: int) -> tuple[np.ndarray, np.ndarray]:
    m = running & ~np.isnan(x) & ~np.isnan(lf)
    xs, ls = x[m], lf[m]
    n = (len(xs) // PER_H) * PER_H
    if n == 0:
        return np.array([]), np.array([])
    return (xs[len(xs) - n:].reshape(-1, PER_H).mean(axis=1)[-hours:],
            ls[len(ls) - n:].reshape(-1, PER_H).mean(axis=1)[-hours:])


def load_normalised(x: np.ndarray, lf: np.ndarray, recent_h: int = 48) -> np.ndarray | None:
    """Residual of x after removing its load dependence, fitted on a known-good period
    (hourly points older than ``recent_h`` running hours). Returns None if too little history."""
    if len(x) < recent_h + 36:
        return None
    xb, lb = x[:-recent_h], lf[:-recent_h]
    A = np.column_stack([lb, np.ones_like(lb)])
    keep = np.ones(len(xb), dtype=bool)
    for _ in range(3):                               # simple iterative outlier rejection
        coef, *_ = np.linalg.lstsq(A[keep], xb[keep], rcond=None)
        r = xb - A @ coef
        s = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep]))) + 1e-9
        keep = np.abs(r) < 3 * s
    return x - np.column_stack([lf, np.ones_like(lf)]) @ coef


def trend_to_threshold(series: np.ndarray, limit: float, rng: np.random.Generator,
                       fit_hours: int = 48, base_hours: int = 72) -> dict | None:
    """Exponential extrapolation of excess-over-baseline to a limit.

    series: hourly running values (oldest first), ideally already load-normalised.
    Requires a statistically significant, persistent upward trend; returns running
    hours to the limit (p10/p50/p90 from a residual bootstrap) or None.
    """
    if len(series) < fit_hours + 24:
        return None
    base = float(np.median(series[: max(24, len(series) - fit_hours - 24)][-base_hours:]))
    recent = series[-fit_hours:]
    exc = recent - base
    now_exc = float(np.mean(exc[-6:]))
    gap = limit - base
    if gap <= 0:
        return None
    if now_exc >= gap:
        return {"p10": 0.0, "p50": 0.0, "p90": 0.0, "base": base, "now": base + now_exc}
    if now_exc < 0.08 * gap or np.median(exc[-12:]) < 0.08 * gap:     # must be persistent, not one spike
        return None
    m = exc > 0.02 * gap
    if m.sum() < 12:
        return None
    t = np.arange(len(exc))[m].astype(float)
    y = np.log(exc[m])
    A = np.column_stack([t, np.ones_like(t)])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    dof = max(len(t) - 2, 1)
    se = math.sqrt(float(resid @ resid) / dof / max(float(((t - t.mean()) ** 2).sum()), 1e-9))
    if coef[0] <= 0 or coef[0] / max(se, 1e-12) < 3:                    # growth must be significant
        return None
    sims = []
    for _ in range(200):
        yb = A @ coef + rng.choice(resid, size=len(resid), replace=True)
        g, c0 = np.linalg.lstsq(A, yb, rcond=None)[0]
        sims.append(np.inf if g <= 1e-4 else max(0.0, (math.log(gap) - (c0 + g * t[-1])) / g))
    sims = np.array(sims)
    if np.mean(np.isinf(sims)) > 0.5:
        return None
    p10, p50, p90 = np.percentile(sims, [10, 50, 90])
    return {"p10": float(p10), "p50": float(p50), "p90": float(p90), "sims": sims, "base": base,
            "now": base + now_exc}


# Limits on the *excess* over what load and ambient explain (alarm settings an engineer would choose)
DRIVE_LIMITS = {"gearbox_vib": 4.5, "gearbox_oil": 25.0, "slip": 2.5}


def drive_predictions(cl: Cleaned, now_ts: float, seed: int = 0) -> list[dict]:
    v, run = cl.clean, cl.running
    d = C.STATION_INDEX["S24"]
    rng = np.random.default_rng(seed)
    out = []
    horizon_run = calendar_to_running(now_ts, HORIZON_H)
    feed = v[:, C.STATION_INDEX["S02"], CH["load_tph"]] / C.DESIGN_CAPACITY_TPH
    # the drive carries the whole belt: ~20 min of feed history is on the belt at any time
    lf = pd.Series(feed).rolling(4, min_periods=1).mean().to_numpy()
    with np.errstate(all="ignore"):
        oil_rise = v[:, d, CH["gearbox_temp"]] - v[:, d, CH["ambient_temp"]]
        ds, bs = v[:, d, CH["drive_speed"]], v[:, d, CH["belt_speed"]]
        slip = np.where(ds > 0.5, (ds - bs) / ds * 100, np.nan)
    his = {}
    for name, x in (("gearbox_vib", v[:, d, CH["gearbox_vib"]]), ("gearbox_oil", oil_rise), ("slip", slip)):
        hx, hl = _hourly_pairs(x, lf, run, 168)
        r = load_normalised(hx, hl) if len(hx) else None
        if r is not None and name == "slip":
            r = r / np.maximum(hl, 0.3) ** 1.5          # slip excess grows ~ load^1.5: express at full load
        his[name] = r
    for mode, items in (("gearbox", [("gearbox vibration excess", "gearbox_vib", "mm/s"),
                                     ("gearbox oil temperature excess", "gearbox_oil", "°C")]),
                        ("slip", [("belt slip excess at full load", "slip", "%")])):
        best, why = None, ""
        for label, key, unit in items:
            series = his.get(key)
            if series is None:
                continue
            r = trend_to_threshold(series, DRIVE_LIMITS[key], rng)
            if r and (best is None or r["p50"] < best["p50"]):
                best = r
                why = (f"{label} {r['now'] - r['base']:+.2f} {unit} over what load and ambient explain, "
                       f"trending to the {DRIVE_LIMITS[key]} {unit} limit")
        if best is None:
            out.append({"station_id": "S24", "mode": mode, "probability": 0.0, "ttf_p10_h": None, "ttf_p50_h": None,
                        "ttf_p90_h": None, "method": "trend", "confidence": "high", "threshold": 0.5,
                        "evidence": "no significant upward trend"})
            continue
        sims = best.get("sims", np.array([best["p50"]]))
        prob = float(np.mean(sims <= horizon_run))
        out.append({"station_id": "S24", "mode": mode, "probability": prob,
                    "ttf_p10_h": running_to_calendar(now_ts, best["p10"]),
                    "ttf_p50_h": running_to_calendar(now_ts, best["p50"]),
                    "ttf_p90_h": running_to_calendar(now_ts, best["p90"]) if np.isfinite(best["p90"]) else None,
                    "method": "trend", "confidence": "high", "threshold": 0.5, "evidence": why})
    return out
