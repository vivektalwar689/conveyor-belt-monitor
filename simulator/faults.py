"""Everything that makes the data messy, applied *after* the physics.

Three layers, mirroring where problems come from in a real plant:

1. Sensor faults (the transducer lies): stuck-at values, slow calibration drift,
   EMI spikes, firmware error codes (-999, 9999, 6553.5 = 0xFFFF/10, "ERR", null).
2. Transport faults (the radio/gateway loses or reorders data): random packet
   loss, station and gateway outages (whole radio segment down at once), dust
   storms that multiply both, store-and-forward buffers that deliver hours-old
   data late and out of order, at-least-once delivery that duplicates batches.
3. Format heterogeneity (the integration is inconsistent): three gateways with
   three timestamp formats, legacy PLC tag names on the drive, and a few
   channels configured in non-SI units.

Ground-truth events for all of these are emitted so the backend's data-quality
checks can be evaluated, not just eyeballed.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from simulator import config as C

GATEWAYS = {
    "GW-1": [s.id for s in C.STATIONS[0:8]],
    "GW-2": [s.id for s in C.STATIONS[8:16]],
    "GW-3": [s.id for s in C.STATIONS[16:24]],
}
GATEWAY_OF = {sid: gw for gw, sids in GATEWAYS.items() for sid in sids}
TS_FORMAT = {"GW-1": "iso_z", "GW-2": "epoch_ms", "GW-3": "iso_local"}

# Legacy PLC tag names used by the drive's controller (mapped back by the backend).
ALIASES_OUT = {("S24", "motor_current"): "MTR_CURRENT_A", ("S24", "gearbox_temp"): "GBX_OIL_T",
               ("S24", "gearbox_vib"): "GBX_VIB_MMS", ("S24", "drive_speed"): "PULLEY_SPD"}
# Channels configured in non-canonical units on site.
UNIT_OVERRIDES = {("S07", "bearing_temp"): "degF", ("S15", "vib_rms"): "in/s", ("S24", "motor_temp"): "K"}
TEMP_CHANNELS = ("bearing_temp", "motor_temp", "gearbox_temp")
SENTINELS = (-999.0, 9999.0, 6553.5)


def convert_out(value: float, unit: str) -> float:
    if unit == "degF":
        return value * 9 / 5 + 32
    if unit == "in/s":
        return value / 25.4
    if unit == "K":
        return value + 273.15
    return value


@dataclass
class ScheduledDQ:
    start_h: float
    kind: str               # stuck | drift | station_outage | gateway_outage | storm
    target: str = ""        # station id or gateway id
    channel: str = ""
    duration_h: float = 4.0
    buffered: bool = True
    drift_c_per_day: float = 3.0


@dataclass
class DataQuality:
    packet_loss: float = 0.004
    storm_packet_loss: float = 0.06
    station_outage_rate: float = 1 / 90      # per station-day
    gateway_outage_rate: float = 1 / 30      # per gateway-day
    storm_outage_mult: float = 5.0
    outage_median_h: float = 1.5
    outage_buffered_frac: float = 0.7
    buffer_capacity_h: float = 8.0
    stuck_rate: float = 1 / 2500             # per channel-day
    stuck_median_h: float = 14.0
    drift_rate: float = 1 / 1500             # per temperature-channel-day
    drift_c_per_day: tuple[float, float] = (1.5, 4.0)
    drift_days: tuple[float, float] = (2.0, 6.0)
    spike_prob: float = 4e-4
    sentinel_prob: float = 2e-4
    duplicate_prob: float = 0.02
    storm_rate_per_day: float = 1 / 12
    storm_hours: tuple[float, float] = (2.0, 8.0)
    scheduled: list[ScheduledDQ] = field(default_factory=list)
    random_faults: bool = True


@dataclass
class Record:
    station_id: str
    channel: str
    ts: float          # epoch seconds (UTC) of the measurement
    value: float | str | None
    seq: int


class MessyTransport:
    """Stateful sensor-fault + transport model. Call ``process`` once per simulator step."""

    def __init__(self, seed: int, dq: DataQuality | None = None, start: datetime | None = None):
        self.dq = dq or DataQuality()
        self.rng = np.random.default_rng(np.random.SeedSequence([seed, 77]))
        S, Cn = len(C.STATIONS), len(C.CHANNELS)
        self.start_ts = start.timestamp() if start else None
        self.stuck_until = np.zeros((S, Cn))
        self.stuck_val = np.zeros((S, Cn))
        self.drift_until = np.zeros((S, Cn))
        self.drift_rate = np.zeros((S, Cn))           # degC per second
        self.drift_offset = np.zeros((S, Cn))
        self.station_out_until = np.zeros(S)
        self.station_out_buffered = np.zeros(S, dtype=bool)
        self.gw_out_until = {gw: 0.0 for gw in GATEWAYS}
        self.gw_out_buffered = {gw: True for gw in GATEWAYS}
        self.storm_until = 0.0
        self.buffers: dict[str, deque] = {s.id: deque() for s in C.STATIONS}
        self.seq = {s.id: 0 for s in C.STATIONS}
        self.pending_dupes: list[Record] = []
        self.pending_sched = sorted(self.dq.scheduled, key=lambda e: e.start_h)

    # -------------------------------------------------------------- fault state machines
    def _sched(self, now: float, events: list) -> None:
        if self.start_ts is None:
            self.start_ts = now
        while self.pending_sched and self.pending_sched[0].start_h * 3600 <= now - self.start_ts:
            e = self.pending_sched.pop(0)
            until = now + e.duration_h * 3600
            if e.kind in ("stuck", "drift"):
                i, c = C.STATION_INDEX[e.target], C.CHANNEL_INDEX[e.channel]
                if e.kind == "stuck":
                    self.stuck_until[i, c] = until
                    self.stuck_val[i, c] = np.nan     # latch on next value
                else:
                    self.drift_until[i, c] = until
                    self.drift_rate[i, c] = e.drift_c_per_day / 86400
            elif e.kind == "station_outage":
                i = C.STATION_INDEX[e.target]
                self.station_out_until[i], self.station_out_buffered[i] = until, e.buffered
            elif e.kind == "gateway_outage":
                self.gw_out_until[e.target], self.gw_out_buffered[e.target] = until, e.buffered
            elif e.kind == "storm":
                self.storm_until = until
            events.append({"type": f"dq_{e.kind}", "t": datetime.fromtimestamp(now, timezone.utc),
                           "target": e.target, "channel": e.channel, "hours": e.duration_h, "scheduled": True})

    def _random(self, now: float, has: np.ndarray, events: list) -> None:
        dq, rng = self.dq, self.rng
        p_day = C.SAMPLE_PERIOD_S / 86400
        storm = now < self.storm_until
        t = datetime.fromtimestamp(now, timezone.utc)
        if not storm and rng.random() < dq.storm_rate_per_day * p_day:
            self.storm_until = now + rng.uniform(*dq.storm_hours) * 3600
            events.append({"type": "dq_storm", "t": t, "hours": round((self.storm_until - now) / 3600, 1)})
        mult = dq.storm_outage_mult if storm else 1.0
        for gw in GATEWAYS:
            if now >= self.gw_out_until[gw] and rng.random() < dq.gateway_outage_rate * mult * p_day:
                hours = dq.outage_median_h * math.exp(rng.normal(0, 0.9))
                self.gw_out_until[gw] = now + hours * 3600
                self.gw_out_buffered[gw] = rng.random() < dq.outage_buffered_frac
                events.append({"type": "dq_gateway_outage", "t": t, "target": gw, "hours": round(hours, 2),
                               "buffered": bool(self.gw_out_buffered[gw])})
        for i, s in enumerate(C.STATIONS):
            if now >= self.station_out_until[i] and rng.random() < dq.station_outage_rate * mult * p_day:
                hours = dq.outage_median_h * math.exp(rng.normal(0, 0.9))
                self.station_out_until[i] = now + hours * 3600
                self.station_out_buffered[i] = rng.random() < dq.outage_buffered_frac
                events.append({"type": "dq_station_outage", "t": t, "target": s.id, "hours": round(hours, 2),
                               "buffered": bool(self.station_out_buffered[i])})
        free = has & (now >= self.stuck_until)
        new_stuck = free & (rng.random(has.shape) < dq.stuck_rate * p_day)
        for i, c in zip(*np.where(new_stuck)):
            hours = dq.stuck_median_h * math.exp(rng.normal(0, 0.7))
            self.stuck_until[i, c] = now + hours * 3600
            self.stuck_val[i, c] = np.nan
            events.append({"type": "dq_stuck", "t": t, "target": C.STATIONS[i].id,
                           "channel": list(C.CHANNELS)[c], "hours": round(hours, 1)})
        for ch in TEMP_CHANNELS:
            c = C.CHANNEL_INDEX[ch]
            for i in np.where(has[:, c] & (now >= self.drift_until[:, c]))[0]:
                if rng.random() < dq.drift_rate * p_day:
                    days = rng.uniform(*dq.drift_days)
                    self.drift_until[i, c] = now + days * 86400
                    self.drift_rate[i, c] = rng.uniform(*dq.drift_c_per_day) / 86400
                    events.append({"type": "dq_drift", "t": t, "target": C.STATIONS[i].id, "channel": ch,
                                   "days": round(days, 1), "c_per_day": round(self.drift_rate[i, c] * 86400, 2)})

    # -------------------------------------------------------------- main
    def process(self, t: datetime, values: np.ndarray, has: np.ndarray) -> tuple[list[Record], list[dict]]:
        """Apply faults to one step of true values; return (records delivered now, dq events)."""
        now = t.timestamp()
        events: list[dict] = []
        self._sched(now, events)
        if self.dq.random_faults:
            self._random(now, has, events)
        dq, rng = self.dq, self.rng
        v = values.copy()

        # sensor-level faults
        active_drift = now < self.drift_until
        self.drift_offset = np.where(active_drift, self.drift_offset + self.drift_rate * C.SAMPLE_PERIOD_S, 0.0)
        v = v + self.drift_offset
        stuck = (now < self.stuck_until) & has
        latch = stuck & np.isnan(self.stuck_val)
        self.stuck_val[latch] = v[latch]
        v = np.where(stuck, self.stuck_val, v)
        spikes = has & ~stuck & (rng.random(v.shape) < dq.spike_prob)
        v = np.where(spikes, v * rng.uniform(3, 8, v.shape) + np.sign(v + 1e-9) * 25, v)
        sentinel_mask = has & (rng.random(v.shape) < dq.sentinel_prob)

        storm = now < self.storm_until
        loss_p = dq.storm_packet_loss if storm else dq.packet_loss
        out: list[Record] = []
        # duplicates from the previous step (gateway retry after a missed ACK)
        out.extend(self.pending_dupes)
        self.pending_dupes = []
        cap = int(dq.buffer_capacity_h * 3600 / C.SAMPLE_PERIOD_S) * 14
        for i, s in enumerate(C.STATIONS):
            gw = GATEWAY_OF[s.id]
            recs = []
            for c, ch in enumerate(C.CHANNELS):
                if not has[i, c]:
                    continue
                self.seq[s.id] += 1
                val: float | str | None = float(v[i, c])
                if sentinel_mask[i, c]:
                    val = [SENTINELS[0], SENTINELS[1], SENTINELS[2], None, "ERR"][int(rng.integers(0, 5))]
                recs.append(Record(s.id, ch, now, val, self.seq[s.id]))
            st_out = now < self.station_out_until[i]
            gw_out = now < self.gw_out_until[gw]
            if st_out or gw_out:
                buffered = (self.station_out_buffered[i] if st_out else True) and (self.gw_out_buffered[gw] if gw_out else True)
                if buffered:
                    buf = self.buffers[s.id]
                    buf.extend(recs)
                    while len(buf) > cap:       # buffer overflow: oldest data lost
                        buf.popleft()
                continue
            # link is up: flush anything buffered first (late, out-of-order arrivals)
            buf = self.buffers[s.id]
            while buf:
                r = buf.popleft()
                if rng.random() >= loss_p:
                    out.append(r)
            kept = [r for r in recs if rng.random() >= loss_p]
            out.extend(kept)
            if kept and rng.random() < dq.duplicate_prob:
                self.pending_dupes.extend(kept)
        return out, events


# ------------------------------------------------------------------ wire format
def _fmt_ts(ts: float, gw: str) -> str | int:
    fmt = TS_FORMAT[gw]
    if fmt == "epoch_ms":
        return int(round(ts * 1000))
    dt = datetime.fromtimestamp(ts, timezone.utc)
    if fmt == "iso_local":
        return dt.astimezone(C.SITE_TZ).isoformat(timespec="seconds")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_wire(rec: Record) -> dict:
    """Serialise one record the way the (heterogeneous) field devices would."""
    gw = GATEWAY_OF[rec.station_id]
    unit = UNIT_OVERRIDES.get((rec.station_id, rec.channel), C.CHANNELS[rec.channel][0])
    val = rec.value
    if isinstance(val, float) and not math.isnan(val) and val not in SENTINELS:
        val = round(convert_out(val, unit), 4)
    elif isinstance(val, float) and math.isnan(val):
        val = None
    return {"station_id": rec.station_id,
            "sensor": ALIASES_OUT.get((rec.station_id, rec.channel), rec.channel),
            "ts": _fmt_ts(rec.ts, gw), "value": val, "unit": unit, "seq": rec.seq}


def to_batches(records: list[Record], sent_at: datetime) -> list[dict]:
    by_gw: dict[str, list[dict]] = {}
    for r in records:
        by_gw.setdefault(GATEWAY_OF[r.station_id], []).append(to_wire(r))
    return [{"gateway_id": gw, "sent_at": sent_at.isoformat(), "readings": rs} for gw, rs in by_gw.items()]
