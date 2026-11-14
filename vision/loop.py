"""Belt-loop geometry and multi-pass defect tracking (pure logic, no I/O).

Why this is the heart of the vision component
---------------------------------------------
A fixed camera watches a belt that is a *loop* (~12 km here, one revolution every
~40 min). Consequences, and how this module handles each:

1. The same defect comes back every revolution. Per-frame alerting would raise
   ~27 duplicate alerts per defect per 18-h day. -> Detections are associated to a
   persistent *defect track* in belt coordinates; one defect = one alert.
2. "Where" is not a place on the structure, it is a place on the belt.
   Defects are stored in belt coordinates (metres from splice-tag 0), never in
   camera/world coordinates.
3. Dead reckoning from belt speed is not good enough: a 0.4 % encoder error is
   ~48 m per revolution and grows without bound. -> The belt's splices carry RFID
   tags (common practice on steel-cord belts); each tag read re-zeroes position
   and the measured tag-to-tag distance re-scales the odometer per segment, which
   also absorbs belt stretch and pulley slip.
4. Coordinates wrap around. A defect at 12,049 m and one at 1 m are 2 m apart. ->
   all distances are circular.
5. One look is not proof. Dirt, water and carry-back look like damage once and
   are gone next revolution; a real defect comes back at the same belt position.
   -> A track is *confirmed* only after it is seen on >= 2 different passes;
   unconfirmed tracks that are missed twice are dropped as transients.
6. "Not seen" only counts if that stretch was imaged. If the edge node skipped
   frames (compute limits, belt stopped, camera fouled), absence of a detection
   is not evidence of repair. -> Misses are counted only inside the pass's
   coverage; a confirmed track that is imaged 3 times without a detection is
   marked resolved (repaired), not deleted.
7. Repeated sightings are a time series: size per sighting gives a growth rate,
   which feeds severity (a growing tear is worse than a large stable patch).

Out of scope (see README): carry-side vs return-side faces, belt flip/turnover,
re-splicing that renumbers segments, motion blur at 5 m/s, and growth estimated
from real pixels (the public dataset has no image sequences of the same defect).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SEVERITY_BY_CLASS = {"Tear": "high", "Hole": "high", "Puncture": "medium", "Impact Damage": "medium", "Wear": "low"}
SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def circ_dist(a: float, b: float, L: float) -> float:
    d = abs(a - b) % L
    return min(d, L - d)


def circ_mean(xs: list[float], ws: list[float], L: float) -> float:
    ang = np.array(xs) / L * 2 * np.pi
    w = np.array(ws)
    return float((np.arctan2((w * np.sin(ang)).sum(), (w * np.cos(ang)).sum()) / (2 * np.pi) * L) % L)


# --------------------------------------------------------------------------- belt register
@dataclass
class BeltRegister:
    """Installation record: segment lengths between vulcanised splices, each with an RFID tag."""
    segment_lengths: list[float]

    @property
    def L(self) -> float:
        return float(sum(self.segment_lengths))

    @property
    def tag_pos(self) -> list[float]:
        return [0.0] + list(np.cumsum(self.segment_lengths)[:-1])

    def segment_of(self, s: float) -> tuple[int, float]:
        s %= self.L
        pos = self.tag_pos
        k = int(np.searchsorted(pos, s, side="right") - 1)
        return k, s - pos[k]

    @staticmethod
    def default(seed: int = 3, n: int = 25, loop_m: float = 12_050.0) -> "BeltRegister":
        rng = np.random.default_rng(seed)
        raw = rng.uniform(0.9, 1.1, n)
        return BeltRegister(list(raw / raw.sum() * loop_m))


# --------------------------------------------------------------------------- localisation
class Localizer:
    """Odometer metres -> belt coordinate, re-zeroed and re-scaled at every tag read.

    mode: "tags"         per-segment correction using bracketing tag reads (default)
          "pass_start"   dead reckoning from the first tag read of the pass
          "none"         dead reckoning from the very first tag ever seen (no fiducials)
    """

    def __init__(self, reg: BeltRegister, mode: str = "tags"):
        self.reg, self.mode = reg, mode
        self.anchor: tuple[float, float] | None = None   # (odo, belt pos) for mode "none"
        self.last_scale = 1.0

    def map_pass(self, tag_reads: list[tuple[int, float]], odos: np.ndarray) -> np.ndarray:
        reg, L = self.reg, self.reg.L
        reads = sorted(tag_reads, key=lambda r: r[1])
        if not reads:
            if self.anchor is None:
                return np.full(len(odos), np.nan)
            o0, p0 = self.anchor
            return (p0 + (odos - o0) * self.last_scale) % L
        if self.mode == "none":
            if self.anchor is None:
                self.anchor = (reads[0][1], reg.tag_pos[reads[0][0]])
            o0, p0 = self.anchor
            return (p0 + (odos - o0)) % L
        if self.mode == "pass_start":
            k0, o0 = reads[0]
            return (reg.tag_pos[k0] + (odos - o0)) % L
        # per-segment scale from consecutive reads (handles missed reads: nominal distance spans them)
        tag_o = np.array([o for _k, o in reads])
        tag_p = np.array([reg.tag_pos[k] for k, _o in reads])
        scales = []
        for i in range(len(reads) - 1):
            dp = (tag_p[i + 1] - tag_p[i]) % L
            do = tag_o[i + 1] - tag_o[i]
            if do > 1 and dp > 1:
                scales.append(dp / do)
        if scales:
            self.last_scale = float(np.median(scales))
        out = np.empty(len(odos))
        for j, x in enumerate(odos):
            i = int(np.searchsorted(tag_o, x, side="right") - 1)
            if i < 0:                                  # before the first read: extrapolate backwards
                out[j] = (tag_p[0] - (tag_o[0] - x) * self.last_scale) % L
                continue
            if i < len(reads) - 1:
                dp = (tag_p[i + 1] - tag_p[i]) % L
                do = tag_o[i + 1] - tag_o[i]
                sc = dp / do if do > 1 and dp > 1 else self.last_scale
            else:
                sc = self.last_scale
            out[j] = (tag_p[i] + (x - tag_o[i]) * sc) % L
        return out


# --------------------------------------------------------------------------- tracking
@dataclass
class Detection:
    pos: float           # belt coordinate (m)
    lateral: float       # m from belt centreline
    cls: str
    conf: float
    length_m: float
    width_m: float
    ts: float
    pass_id: int
    crop: bytes | None = None


@dataclass
class Track:
    id: int
    pos: float
    lateral: float
    cls_votes: dict[str, float]
    first_ts: float
    last_ts: float
    status: str = "tentative"        # tentative | confirmed | resolved | dropped
    n_seen: int = 0
    n_imaged: int = 0
    consecutive_miss: int = 0
    passes_seen: list[int] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)   # per sighting: ts, pass, conf, length
    crop: bytes | None = None

    @property
    def cls(self) -> str:
        return max(self.cls_votes, key=self.cls_votes.get)

    @property
    def conf_mean(self) -> float:
        return float(np.mean([h["conf"] for h in self.history])) if self.history else 0.0

    @property
    def length_m(self) -> float:
        return float(np.median([h["length"] for h in self.history[-3:]])) if self.history else 0.0

    def growth_mm_per_day(self) -> float | None:
        if len(self.history) < 4:
            return None
        t = np.array([h["ts"] for h in self.history]) / 86400
        y = np.array([h["length"] for h in self.history]) * 1000
        if np.ptp(t) < 0.25:
            return None
        return float(np.polyfit(t, y, 1)[0])

    def severity(self) -> str:
        sev = SEVERITY_BY_CLASS.get(self.cls, "medium")
        g = self.growth_mm_per_day()
        if self.length_m >= 0.5 or (g is not None and g > 20):
            sev = {"low": "medium", "medium": "high", "high": "critical", "critical": "critical"}[sev]
        if self.cls == "Tear" and self.length_m >= 1.0:
            sev = "critical"                         # longitudinal rip risk
        return sev


class BeltTracker:
    MIN_CONF = 0.25           # per-detection confidence floor (operating point chosen in vision/loop_eval.py)
    GATE_POS_M = 2.5          # along-belt association gate (localisation error + box-centre jitter)
    GATE_LAT_M = 0.30
    MERGE_IN_PASS_M = 0.8     # boxes of the same defect within one pass
    CONFIRM_SEEN = 2
    DROP_TENTATIVE_MISSES = 2
    RESOLVE_CONFIRMED_MISSES = 3

    def __init__(self, L: float):
        self.L = L
        self.tracks: dict[int, Track] = {}
        self.next_id = 1
        self.n_raw = 0

    def _merge_in_pass(self, dets: list[Detection]) -> list[Detection]:
        dets = sorted(dets, key=lambda d: -d.conf)
        kept: list[Detection] = []
        for d in dets:
            if any(circ_dist(d.pos, k.pos, self.L) < self.MERGE_IN_PASS_M and abs(d.lateral - k.lateral) < self.GATE_LAT_M
                   for k in kept):
                continue
            kept.append(d)
        return kept

    def process_pass(self, pass_id: int, dets: list[Detection], coverage: list[tuple[float, float]]) -> dict:
        """coverage: belt-coordinate intervals [a, b) imaged in this pass (may wrap: a > b)."""
        self.n_raw += len(dets)
        dets = self._merge_in_pass([d for d in dets if d.conf >= self.MIN_CONF])
        live = [t for t in self.tracks.values() if t.status in ("tentative", "confirmed")]
        # greedy nearest-neighbour association on a normalised distance
        pairs = []
        for i, d in enumerate(dets):
            for t in live:
                dp = circ_dist(d.pos, t.pos, self.L)
                dl = abs(d.lateral - t.lateral)
                if dp <= self.GATE_POS_M and dl <= self.GATE_LAT_M:
                    pairs.append((dp / self.GATE_POS_M + dl / self.GATE_LAT_M, i, t.id))
        pairs.sort()
        used_d, used_t, matched = set(), set(), {}
        for _c, i, tid in pairs:
            if i in used_d or tid in used_t:
                continue
            used_d.add(i)
            used_t.add(tid)
            matched[i] = tid
        new, confirmed_now = [], []
        for i, d in enumerate(dets):
            if i in matched:
                t = self.tracks[matched[i]]
                w_old = sum(h["conf"] for h in t.history)
                t.pos = circ_mean([t.pos, d.pos], [w_old, d.conf], self.L)
                t.lateral = (t.lateral * w_old + d.lateral * d.conf) / (w_old + d.conf)
            else:
                t = Track(self.next_id, d.pos, d.lateral, {}, d.ts, d.ts)
                self.tracks[t.id] = t
                self.next_id += 1
                new.append(t.id)
            t.cls_votes[d.cls] = t.cls_votes.get(d.cls, 0) + d.conf
            t.last_ts = d.ts
            t.n_seen += 1
            t.n_imaged += 1
            t.consecutive_miss = 0
            t.passes_seen.append(pass_id)
            t.history.append({"ts": d.ts, "pass": pass_id, "conf": round(d.conf, 3), "length": round(d.length_m, 3)})
            if d.crop:
                t.crop = d.crop
            if t.status == "tentative" and len(set(t.passes_seen)) >= self.CONFIRM_SEEN:
                t.status = "confirmed"
                confirmed_now.append(t.id)
        resolved_now, dropped_now = [], []
        for t in live:
            if t.id in used_t:
                continue
            if not _covered(t.pos, coverage, self.L):
                continue                                  # not imaged: no evidence either way
            t.n_imaged += 1
            t.consecutive_miss += 1
            if t.status == "tentative" and t.consecutive_miss >= self.DROP_TENTATIVE_MISSES:
                t.status = "dropped"
                dropped_now.append(t.id)
            elif t.status == "confirmed" and t.consecutive_miss >= self.RESOLVE_CONFIRMED_MISSES:
                t.status = "resolved"
                resolved_now.append(t.id)
        return {"new": new, "confirmed": confirmed_now, "resolved": resolved_now, "dropped": dropped_now,
                "matched": len(matched), "detections_after_merge": len(dets)}


def _covered(pos: float, coverage: list[tuple[float, float]], L: float) -> bool:
    for a, b in coverage:
        if a <= b:
            if a <= pos < b:
                return True
        elif pos >= a or pos < b:                     # interval wraps through 0
            return True
    return False


# --------------------------------------------------------------------------- where is it now?
CAMERA_LOOP_POS_M = 6_080.0     # return strand, just after the head pulley and the belt cleaners


def loop_to_route(p: float, route_m: float = 6_000.0, L: float = 12_050.0) -> tuple[str, float]:
    """Loop position (0 = tail pulley, carry side) -> (strand, chainage from tail)."""
    p %= L
    wrap = (L - 2 * route_m) / 2
    if p < route_m:
        return "carry", p
    if p < route_m + wrap:
        return "head pulley", route_m
    if p < 2 * route_m + wrap:
        return "return", route_m - (p - route_m - wrap)
    return "tail pulley", 0.0


def world_location(defect_pos: float, belt_pos_at_camera: float, L: float) -> tuple[str, float]:
    """Where a defect physically is when the belt is stopped, given which belt coordinate
    is currently under the camera."""
    # Belt coordinates increase as the belt passes the camera, so a defect with a *smaller*
    # coordinate than the one under the camera has already passed it (it is downstream).
    loop_p = (CAMERA_LOOP_POS_M + (belt_pos_at_camera - defect_pos)) % L
    return loop_to_route(loop_p, L=L)
