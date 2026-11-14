"""Static description of the simulated conveyor: route, sensor stations and channels.

Everything here is an *assumption* (documented in the README). The numbers are in
the range of real overland ore conveyors (5-10 km, 4-6 m/s, 3-10 kt/h) but they
are not taken from a specific installation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone

# --------------------------------------------------------------------------- belt
ROUTE_LENGTH_M = 6_000.0          # tail (loading) -> head (drive/discharge)
BELT_SPEED_M_S = 5.0              # nominal
BELT_WIDTH_MM = 1_600
LOOP_LENGTH_M = 12_050.0          # 2 x route + pulley wraps + take-up loop
DESIGN_CAPACITY_TPH = 6_000.0
SITE_TZ = timezone(timedelta(hours=5, minutes=30))   # site local time (IST)
SAMPLE_PERIOD_S = 300             # edge nodes report 5-min aggregates
PLANNED_STOP_LOCAL_HOURS = (0, 6) # belt runs 06:00-24:00 local = 18 h/day

# Elevation profile, piecewise linear: (chainage_m, elevation_m)
ELEVATION_PROFILE = [
    (0, 0.0), (1_200, 6.0),            # gentle start, loading zone
    (2_400, 102.0),                    # 8 % incline
    (3_000, 102.0), (3_500, 102.0),    # plateau incl. horizontal curve 1
    (4_200, 137.0),                    # 5 % incline
    (4_800, 101.0),                    # 6 % decline
    (5_400, 101.0),                    # horizontal curve 2
    (6_000, 119.0),                    # 3 % rise to the head
]
# Horizontal curves (the "sharp turns"): (start_m, end_m, radius_m)
CURVES = [(3_000, 3_500, 1_500.0), (4_800, 5_400, 1_200.0)]

# --------------------------------------------------------------------------- channels
# canonical name -> (unit, physical min, physical max, description)
CHANNELS: dict[str, tuple[str, float, float, str]] = {
    "vib_rms":       ("mm/s", 0.0, 50.0,  "Vibration velocity RMS (10-1000 Hz), idler/pulley bearing housing"),
    "vib_env":       ("g",    0.0, 20.0,  "High-frequency acceleration envelope (bearing-defect energy)"),
    "bearing_temp":  ("degC", -10.0, 150.0, "Bearing housing temperature"),
    "ambient_temp":  ("degC", -10.0, 60.0,  "Ambient air temperature at the station"),
    "belt_offset":   ("mm",   -300.0, 300.0, "Lateral belt position vs. centreline (+ = right, looking downstream)"),
    "belt_speed":    ("m/s",  0.0, 8.0,   "Belt surface speed from a non-driven pulley encoder"),
    "load_tph":      ("t/h",  0.0, 9_000.0, "Belt-scale mass flow"),
    "belt_tension":  ("kN",   0.0, 600.0, "Belt tension at the take-up"),
    "takeup_pos":    ("m",    0.0, 12.0,  "Gravity take-up carriage travel"),
    "motor_current": ("A",    0.0, 1_500.0, "Drive motor current (per motor, mean of 3)"),
    "motor_temp":    ("degC", -10.0, 180.0, "Motor winding temperature"),
    "gearbox_temp":  ("degC", -10.0, 150.0, "Gearbox oil sump temperature"),
    "gearbox_vib":   ("mm/s", 0.0, 50.0,  "Gearbox housing vibration velocity RMS"),
    "drive_speed":   ("m/s",  0.0, 8.0,   "Drive pulley surface speed"),
}
CHANNEL_INDEX = {c: i for i, c in enumerate(CHANNELS)}

COMMON = ("vib_rms", "vib_env", "bearing_temp", "ambient_temp", "belt_offset")
EXTRA_BY_KIND = {
    "tail": ("belt_speed",),
    "loading": ("load_tph",),
    "idler": (),
    "takeup": ("belt_tension", "takeup_pos"),
    "drive": ("motor_current", "motor_temp", "gearbox_temp", "gearbox_vib", "drive_speed", "belt_speed"),
}

# Failure modes the predictive layer covers, and which station kinds they apply to.
FAILURE_MODES = {
    "bearing":      "Idler / pulley bearing failure (seizure)",
    "misalignment": "Belt mistracking -> edge damage / sway-switch trip",
    "gearbox":      "Drive gearbox failure",
    "slip":         "Drive pulley lagging wear -> belt slip trip",
}
MODES_BY_KIND = {
    "tail": ("bearing", "misalignment"),
    "loading": ("bearing", "misalignment"),
    "idler": ("bearing", "misalignment"),
    "takeup": ("bearing",),
    "drive": ("bearing", "gearbox", "slip"),
}


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    chainage_m: float
    kind: str            # tail | loading | idler | takeup | drive
    zone: str            # flat | loading | incline | decline | curve | head | tail
    stress: float        # mechanical stress multiplier (baselines + failure hazard)
    channels: tuple[str, ...] = field(default=())

    @property
    def modes(self) -> tuple[str, ...]:
        return MODES_BY_KIND[self.kind]


def elevation_at(x: float) -> float:
    pts = ELEVATION_PROFILE
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


_ZONE_STRESS = {"tail": 1.2, "loading": 1.6, "flat": 1.0, "incline": 1.25, "decline": 1.1, "curve": 1.4, "head": 1.3}

_LAYOUT = [
    # id, name, chainage, kind, zone
    ("S01", "Tail pulley", 0, "tail", "tail"),
    ("S02", "Loading zone impact idlers", 60, "loading", "loading"),
    ("S03", "Carry idlers km 0.4", 400, "idler", "flat"),
    ("S04", "Carry idlers km 0.8", 800, "idler", "flat"),
    ("S05", "Incline foot", 1_200, "idler", "incline"),
    ("S06", "Incline km 1.5", 1_500, "idler", "incline"),
    ("S07", "Incline km 1.8", 1_800, "idler", "incline"),
    ("S08", "Incline km 2.1", 2_100, "idler", "incline"),
    ("S09", "Incline crest", 2_400, "idler", "incline"),
    ("S10", "Plateau km 2.7", 2_700, "idler", "flat"),
    ("S11", "Curve 1 entry", 3_000, "idler", "curve"),
    ("S12", "Curve 1 apex", 3_250, "idler", "curve"),
    ("S13", "Curve 1 exit", 3_500, "idler", "curve"),
    ("S14", "Incline km 3.85", 3_850, "idler", "incline"),
    ("S15", "Ridge km 4.2", 4_200, "idler", "flat"),
    ("S16", "Decline km 4.5", 4_500, "idler", "decline"),
    ("S17", "Curve 2 entry", 4_800, "idler", "curve"),
    ("S18", "Curve 2 apex", 5_100, "idler", "curve"),
    ("S19", "Curve 2 exit", 5_400, "idler", "curve"),
    ("S20", "Head approach km 5.6", 5_600, "idler", "flat"),
    ("S21", "Head approach km 5.8", 5_800, "idler", "flat"),
    ("S22", "Transition idlers", 5_930, "idler", "head"),
    ("S23", "Take-up & bend pulleys", 5_960, "takeup", "head"),
    ("S24", "Head drive (3 x 1 MW)", 6_000, "drive", "head"),
]

STATIONS: list[Station] = [
    Station(sid, name, float(x), kind, zone, _ZONE_STRESS[zone], COMMON + EXTRA_BY_KIND[kind])
    for sid, name, x, kind, zone in _LAYOUT
]
STATION_INDEX = {s.id: i for i, s in enumerate(STATIONS)}


def station(sid: str) -> Station:
    return STATIONS[STATION_INDEX[sid]]


def describe() -> dict:
    """JSON-friendly description used by the backend / dashboard."""
    return {
        "route_length_m": ROUTE_LENGTH_M,
        "loop_length_m": LOOP_LENGTH_M,
        "belt_speed_m_s": BELT_SPEED_M_S,
        "belt_width_mm": BELT_WIDTH_MM,
        "design_capacity_tph": DESIGN_CAPACITY_TPH,
        "sample_period_s": SAMPLE_PERIOD_S,
        "site_utc_offset_min": 330,
        "elevation_profile": ELEVATION_PROFILE,
        "curves": CURVES,
        "stations": [
            {"id": s.id, "name": s.name, "chainage_m": s.chainage_m, "kind": s.kind, "zone": s.zone,
             "elevation_m": round(elevation_at(s.chainage_m), 1), "channels": list(s.channels), "modes": list(s.modes)}
            for s in STATIONS
        ],
        "channels": {c: {"unit": u, "min": lo, "max": hi, "description": d} for c, (u, lo, hi, d) in CHANNELS.items()},
        "failure_modes": FAILURE_MODES,
    }
