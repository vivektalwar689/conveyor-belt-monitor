"""Physics-flavoured simulator of a long overland conveyor.

What it models (all deliberately simple, but with the *structure* that matters for
monitoring):

* Operating regime: 18 h/day duty with a planned 00:00-06:00 local stop, random
  trips, crusher "no feed" gaps, a mean-reverting feed rate, and the transport
  delay of material along the belt (a load surge reaches km 6 twenty minutes
  after it was loaded). Degradation only progresses while the belt runs.
* Environment: diurnal ambient cycle + weather drift + per-station microclimate,
  site-wide dust storms (which hurt the radio links, see ``faults.py``).
* Thermal lag: bearing / motor / gearbox temperatures are first-order systems.
* Failure modes, each with a hidden health index h in [0, 1]:
    - bearing (any station): exponential degradation. Its signatures follow the
      classic P-F curve: HF envelope rises first (h^0.7), vibration RMS later
      (h^1.5), temperature last (h^3). Failure = seizure at h >= 1.
    - misalignment (stations with a belt-edge sensor): lateral drift of the belt;
      failure when the belt hits the sway switch (|offset| >= 110 mm).
    - gearbox (drive): exponential degradation -> gearbox vibration, oil
      temperature, motor current (efficiency loss).
    - slip (drive): lagging wear; slip grows with h^2 and *only shows under load*.
  Every failure trips the whole conveyor for a mode-dependent repair time.

The simulator returns *true* values. Sensor faults and the messy transport layer
are applied afterwards (``faults.py``) so the ground truth stays available.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from simulator import config as C

DT = C.SAMPLE_PERIOD_S
MODES = list(C.FAILURE_MODES)                 # bearing, misalignment, gearbox, slip
MODE_INDEX = {m: i for i, m in enumerate(MODES)}
H0 = 0.02                                       # health index at detectable onset
MISALIGN_TRIP_MM = 110.0
REPAIR_HOURS = {"bearing": (2.0, 4.0), "misalignment": (1.0, 3.0), "gearbox": (10.0, 16.0), "slip": (8.0, 12.0)}


@dataclass
class ScheduledFault:
    """Force a degradation to start at ``start_h`` hours after simulation start."""
    start_h: float
    station: str
    mode: str
    tpf_running_h: float          # running hours from onset to failure (bearing/gearbox/slip)
    misalign_rate_mm_h: float = 1.4
    direction: int = 1


@dataclass
class Scenario:
    name: str = "training"
    # Degradation onset hazards per station-day (multiplied by the station stress factor).
    bearing_rate: float = 1 / 110
    misalignment_rate: float = 1 / 260
    gearbox_rate: float = 1 / 70
    slip_rate: float = 1 / 70
    # Running hours from onset (h=0.02) to failure: lognormal(median, sigma)
    bearing_tpf_h: tuple[float, float] = (110.0, 0.35)
    gearbox_tpf_h: tuple[float, float] = (150.0, 0.30)
    slip_tpf_h: tuple[float, float] = (130.0, 0.30)
    misalign_rate_mm_h: tuple[float, float] = (0.8, 2.2)   # uniform
    # Real fleets also have *fast* failures (lubrication loss, contamination) with little warning
    bearing_fast_frac: float = 0.2
    bearing_fast_tpf_h: tuple[float, float] = (20.0, 0.4)
    # Per-degradation signature strength (env, rms, temp): not every bad bearing "sings" the same
    signature_range: tuple[float, float] = (0.35, 1.3)
    trip_rate_per_day: float = 0.4
    no_feed_rate_per_day: float = 1.0
    scheduled: list[ScheduledFault] = field(default_factory=list)
    random_degradation: bool = True


@dataclass
class Degradation:
    station: int
    mode: str
    onset: datetime
    h: float = H0
    beta: float = 0.0           # per running second (exponential modes)
    rate_mm_s: float = 0.0      # misalignment drift rate per running second
    direction: int = 1
    drift_mm: float = 0.0
    failed: bool = False
    amp: tuple[float, float, float] = (1.0, 1.0, 1.0)    # signature strength: HF envelope, RMS, temperature


@dataclass
class StepOutput:
    t: datetime
    running: bool
    values: np.ndarray            # (n_stations, n_channels) true values, NaN where no sensor
    health: np.ndarray            # (n_stations, n_modes) hidden health index
    feed_tph: float
    events: list[dict]


class BeltSimulator:
    def __init__(self, start: datetime, seed: int = 0, scenario: Scenario | None = None):
        self.t = start
        self.start = start
        self.sc = scenario or Scenario()
        ss = np.random.SeedSequence(seed)
        self.rng_ops, self.rng_deg, self.rng_sig, self.rng_env = (np.random.default_rng(s) for s in ss.spawn(4))
        S = len(C.STATIONS)
        self.S = S
        st = C.STATIONS
        self.stress = np.array([s.stress for s in st])
        self.chainage = np.array([s.chainage_m for s in st])
        self.elev = np.array([C.elevation_at(s.chainage_m) for s in st])
        self.is_curve = np.array([s.zone == "curve" for s in st], dtype=float)
        self.has = np.zeros((S, len(C.CHANNELS)), dtype=bool)
        for i, s in enumerate(st):
            for ch in s.channels:
                self.has[i, C.CHANNEL_INDEX[ch]] = True
        r = self.rng_sig
        # Per-station installation differences (why per-station baselines are needed).
        self.vib_base = r.uniform(0.9, 1.6, S) * np.sqrt(self.stress)
        self.env_base = r.uniform(0.25, 0.45, S) * np.sqrt(self.stress)
        self.offset_bias = r.normal(0, 8, S)
        self.amb_offset = r.normal(0, 1.2, S) - 0.0065 * self.elev
        # Dynamic states
        self.bearing_rise = np.zeros(S)
        self.motor_rise = 0.0
        self.gearbox_rise = 0.0
        self.last_offset = self.offset_bias.copy()
        self.feed = 4300.0
        n_hist = int(math.ceil(C.ROUTE_LENGTH_M / C.BELT_SPEED_M_S / DT)) + 2
        self.load_hist = np.zeros(n_hist)       # indexed by belt displacement, not time
        self.delay_steps = np.minimum((self.chainage / C.BELT_SPEED_M_S / DT).round().astype(int), n_hist - 1)
        self.day_offset = 0.0
        self.perm_stretch = 0.0
        self.trip_until: datetime | None = None
        self.repair_until: datetime | None = None
        self.no_feed_until: datetime | None = None
        self.was_running = False
        self.run_steps = 0
        self.degr: dict[tuple[int, str], Degradation] = {}
        self.pending_scheduled = sorted(self.sc.scheduled, key=lambda f: f.start_h)

    # ------------------------------------------------------------------ helpers
    def _local_hour(self, t: datetime) -> float:
        lt = t.astimezone(C.SITE_TZ)
        return lt.hour + lt.minute / 60

    def _planned_stop(self, t: datetime) -> bool:
        a, b = C.PLANNED_STOP_LOCAL_HOURS
        return a <= self._local_hour(t) < b

    def _lognormal(self, median_sigma: tuple[float, float]) -> float:
        med, sig = median_sigma
        return float(med * math.exp(self.rng_deg.normal(0, sig)))

    def _start_degradation(self, i: int, mode: str, tpf_h: float | None = None,
                           rate_mm_h: float | None = None, direction: int | None = None) -> dict:
        d = Degradation(station=i, mode=mode, onset=self.t)
        if mode == "misalignment":
            rate = rate_mm_h if rate_mm_h is not None else self.rng_deg.uniform(*self.sc.misalign_rate_mm_h)
            d.rate_mm_s = rate / 3600
            d.direction = direction if direction is not None else int(self.rng_deg.choice([-1, 1]))
            tpf = MISALIGN_TRIP_MM / rate
        else:
            if tpf_h is not None:
                tpf = tpf_h
            elif mode == "bearing" and self.rng_deg.random() < self.sc.bearing_fast_frac:
                tpf = self._lognormal(self.sc.bearing_fast_tpf_h)
            else:
                tpf = self._lognormal(
                    {"bearing": self.sc.bearing_tpf_h, "gearbox": self.sc.gearbox_tpf_h, "slip": self.sc.slip_tpf_h}[mode])
            if tpf_h is None:
                lo, hi = self.sc.signature_range
                d.amp = tuple(float(x) for x in self.rng_deg.uniform(lo, hi, 3))
            # mean load multiplier of (0.7 + 0.6*lf) at lf~0.7 is ~1.12
            d.beta = math.log(1 / H0) / (tpf * 3600 * 1.12)
        self.degr[(i, mode)] = d
        return {"type": "degradation_onset", "t": self.t, "station": C.STATIONS[i].id, "mode": mode,
                "nominal_running_h_to_failure": round(tpf, 1)}

    # ------------------------------------------------------------------ main step
    def step(self) -> StepOutput:
        self.t = self.t + timedelta(seconds=DT)
        t, events = self.t, []
        S, ro = self.S, self.rng_ops

        # ---- scheduled + random degradation onsets
        elapsed_h = (t - self.start).total_seconds() / 3600
        while self.pending_scheduled and self.pending_scheduled[0].start_h <= elapsed_h:
            f = self.pending_scheduled.pop(0)
            i = C.STATION_INDEX[f.station]
            if (i, f.mode) not in self.degr:
                events.append(self._start_degradation(i, f.mode, f.tpf_running_h, f.misalign_rate_mm_h, f.direction))
        if self.sc.random_degradation:
            p_day = DT / 86400
            for i, s in enumerate(C.STATIONS):
                for mode in s.modes:
                    if (i, mode) in self.degr:
                        continue
                    rate = {"bearing": self.sc.bearing_rate * self.stress[i],
                            "misalignment": self.sc.misalignment_rate * (1 + 1.5 * self.is_curve[i]) * self.stress[i],
                            "gearbox": self.sc.gearbox_rate, "slip": self.sc.slip_rate}[mode]
                    if self.rng_deg.random() < rate * p_day:
                        events.append(self._start_degradation(i, mode))

        # ---- operating state
        if self.repair_until and t >= self.repair_until:
            self.repair_until = None
        if self.trip_until and t >= self.trip_until:
            self.trip_until = None
        planned = self._planned_stop(t)
        if not planned and not self.repair_until and not self.trip_until and ro.random() < self.sc.trip_rate_per_day * DT / 86400:
            self.trip_until = t + timedelta(minutes=float(ro.uniform(10, 60)))
            events.append({"type": "trip", "t": t, "minutes": round((self.trip_until - t).total_seconds() / 60)})
        running = not (planned or self.repair_until or self.trip_until)
        r = 1.0 if running else 0.0
        if running and not self.was_running:
            self.run_steps = 0
        self.run_steps = self.run_steps + 1 if running else 0
        self.was_running = running

        # ---- feed (mean-reverting) and transport along the belt
        if running:
            if self.no_feed_until and t >= self.no_feed_until:
                self.no_feed_until = None
            if not self.no_feed_until and ro.random() < self.sc.no_feed_rate_per_day * DT / 86400:
                self.no_feed_until = t + timedelta(minutes=float(ro.uniform(15, 45)))
            theta = DT / 7200
            self.feed += theta * (4300 - self.feed) + 600 * math.sqrt(2 * theta) * ro.normal()
            self.feed = float(np.clip(self.feed, 1500, 6200))
            ramp = min(1.0, self.run_steps / 3)
            feed_now = 0.0 if self.no_feed_until else self.feed * ramp
            self.load_hist = np.roll(self.load_hist, 1)
            self.load_hist[0] = feed_now
        else:
            feed_now = 0.0
        lf = self.load_hist[self.delay_steps] / C.DESIGN_CAPACITY_TPH * r      # local load fraction
        lf_total = float(self.load_hist[: self.delay_steps.max() + 1].mean() / C.DESIGN_CAPACITY_TPH * r)

        # ---- environment
        hour = self._local_hour(t)
        if t.minute == 0 and t.hour == 0:
            self.day_offset += -self.day_offset / 3 + 2.5 * math.sqrt(2 / 3) * self.rng_env.normal()
        amb_site = 29 + 8.5 * math.sin(2 * math.pi * (hour - 9) / 24) + self.day_offset
        amb = amb_site + self.amb_offset + self.rng_env.normal(0, 0.25, S)

        # ---- degradation progress (only while running) and failures
        h = np.zeros((S, len(MODES)))
        for (i, mode), d in list(self.degr.items()):
            if running and not d.failed:
                if mode == "misalignment":
                    d.drift_mm += d.direction * d.rate_mm_s * DT * float(self.rng_deg.uniform(0.6, 1.4))
                    d.h = min(1.0, abs(d.drift_mm) / MISALIGN_TRIP_MM)
                    eff = self.offset_bias[i] + d.drift_mm * (1.15 - 0.3 * lf[i])
                    if abs(eff) >= MISALIGN_TRIP_MM:
                        d.failed = True
                else:
                    load_mult = 0.7 + 0.6 * (lf[i] if mode == "bearing" else lf_total)
                    d.h *= math.exp(d.beta * DT * load_mult + self.rng_deg.normal(0, 0.015))
                    if d.h >= 1.0:
                        d.h, d.failed = 1.0, True
                if d.failed:
                    lo, hi = REPAIR_HOURS[mode]
                    rep = float(self.rng_ops.uniform(lo, hi))
                    self.repair_until = max(self.repair_until or t, t + timedelta(hours=rep))
                    events.append({"type": "failure", "t": t, "station": C.STATIONS[i].id, "mode": mode,
                                   "repair_hours": round(rep, 1)})
            h[i, MODE_INDEX[mode]] = d.h
        # Repairs complete when the conveyor restarts after the repair window.
        if not self.repair_until:
            for key, d in list(self.degr.items()):
                if d.failed:
                    del self.degr[key]
                    events.append({"type": "repaired", "t": t, "station": C.STATIONS[key[0]].id, "mode": key[1]})
        # Signals respond to damage accumulated *since onset* (0 at onset), so there is no
        # artificial step change when a degradation starts.
        dmg = np.clip((h - H0) / (1 - H0), 0.0, 1.0)
        hb = dmg[:, MODE_INDEX["bearing"]]
        amp = np.ones((S, 3))
        for (i, mode), d in self.degr.items():
            if mode == "bearing":
                amp[i] = d.amp
        drift = np.zeros(S)
        for (i, mode), d in self.degr.items():
            if mode == "misalignment":
                drift[i] = d.drift_mm
        hg = dmg[C.STATION_INDEX["S24"], MODE_INDEX["gearbox"]]
        hs = dmg[C.STATION_INDEX["S24"], MODE_INDEX["slip"]]

        # ---- signals
        rs = self.rng_sig
        V = np.full((S, len(C.CHANNELS)), np.nan)
        ci = C.CHANNEL_INDEX

        def first_order(x, target, tau_s):
            return x + (target - x) * (1 - math.exp(-DT / tau_s))

        target = r * (self.stress * (5 + 7 * lf) + 45 * amp[:, 2] * hb ** 3)
        self.bearing_rise = first_order(self.bearing_rise, target, 2400 if running else 3600)
        V[:, ci["bearing_temp"]] = amb + self.bearing_rise + rs.normal(0, 0.35, S)
        V[:, ci["ambient_temp"]] = amb
        V[:, ci["vib_rms"]] = (r * (self.vib_base * (0.85 + 0.35 * lf) * (1 + rs.normal(0, 0.06, S)) + 8 * amp[:, 1] * hb ** 1.5)
                               + (1 - r) * (0.04 + np.abs(rs.normal(0, 0.015, S))))
        V[:, ci["vib_env"]] = (r * (self.env_base * (0.9 + 0.2 * lf) * (1 + rs.normal(0, 0.10, S)) + 3.0 * amp[:, 0] * hb ** 0.7)
                               + (1 - r) * (0.02 + np.abs(rs.normal(0, 0.01, S))))
        if running:
            off = (self.offset_bias + drift * (1.15 - 0.3 * lf) + self.is_curve * (0.6 - lf) * 18
                   + rs.normal(0, 1, S) * (2.5 + 2 * self.stress))
            self.last_offset = off
        else:
            off = self.last_offset + rs.normal(0, 0.5, S)
        V[:, ci["belt_offset"]] = off

        # drive station
        d_i = C.STATION_INDEX["S24"]
        current = r * (180 + 440 * (0.25 + 0.75 * lf_total)) * (1 + 0.06 * hg) * (1 + rs.normal(0, 0.015))
        self.motor_rise = first_order(self.motor_rise, r * (25 + 45 * (current / 620) ** 2), 3600 if running else 5400)
        self.gearbox_rise = first_order(self.gearbox_rise, r * (18 + 22 * lf_total + 35 * hg ** 2), 7200 if running else 9000)
        slip_pct = r * max(0.0, 0.3 + 0.2 * lf_total + 4.0 * hs ** 2 * lf_total ** 1.5 + rs.normal(0, 0.04))
        drive_speed = r * C.BELT_SPEED_M_S * (1 + rs.normal(0, 0.002))
        belt_speed = drive_speed * (1 - slip_pct / 100)
        V[d_i, ci["motor_current"]] = current
        V[d_i, ci["motor_temp"]] = amb[d_i] + self.motor_rise + rs.normal(0, 0.5)
        V[d_i, ci["gearbox_temp"]] = amb[d_i] + self.gearbox_rise + rs.normal(0, 0.4)
        V[d_i, ci["gearbox_vib"]] = (r * (2.2 * (0.9 + 0.2 * lf_total) * (1 + rs.normal(0, 0.05)) + 6 * hg ** 1.5)
                                     + (1 - r) * (0.05 + abs(rs.normal(0, 0.02))))
        V[d_i, ci["drive_speed"]] = drive_speed
        V[d_i, ci["belt_speed"]] = belt_speed + r * rs.normal(0, 0.003)
        # tail: belt speed from the tail pulley encoder
        V[C.STATION_INDEX["S01"], ci["belt_speed"]] = belt_speed * (1 + r * rs.normal(0, 0.001))
        # loading zone belt scale
        V[C.STATION_INDEX["S02"], ci["load_tph"]] = feed_now * (1 + r * rs.normal(0, 0.01))
        # take-up: gravity take-up keeps tension ~constant; the carriage absorbs belt stretch
        if running:
            self.perm_stretch += 0.004 * DT / 86400 * 24 / 18
        tu = C.STATION_INDEX["S23"]
        V[tu, ci["takeup_pos"]] = (6.0 + self.perm_stretch + 0.8 * lf_total * r + 0.015 * (amb[tu] - 25)
                                   + rs.normal(0, 0.01))
        V[tu, ci["belt_tension"]] = r * (205 + 25 * lf_total + rs.normal(0, 3)) + (1 - r) * (195 + rs.normal(0, 2))

        V[~self.has] = np.nan
        return StepOutput(t=t, running=running, values=V, health=h, feed_tph=feed_now, events=events)
