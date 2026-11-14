import numpy as np

from backend.predict import running_to_calendar, trend_to_threshold
from backend.quality import detect_spikes, detect_stuck
from simulator import config as C
from vision.loop import BeltRegister, BeltTracker, Detection, Localizer, circ_dist, world_location

S, Cn = len(C.STATIONS), len(C.CHANNELS)
CH = C.CHANNEL_INDEX


def series(x, station=2, ch="vib_rms"):
    v = np.full((len(x), S, Cn), np.nan)
    v[:, station, CH[ch]] = x
    return v


# ---------------------------------------------------------------- data quality
def test_isolated_spike_flagged_but_start_stop_step_is_not():
    rng = np.random.default_rng(0)
    x = 1.2 + rng.normal(0, 0.03, 60)
    x[30] = 25.0                          # EMI spike
    x[45:] = 0.05                         # belt stops: a genuine step
    sp = detect_spikes(series(x))[:, 2, CH["vib_rms"]]
    assert sp[30] and sp.sum() == 1


def test_stuck_sensor_flagged_but_zero_load_when_stopped_is_not():
    rng = np.random.default_rng(1)
    temp = 40 + rng.normal(0, 0.3, 40)
    temp[10:30] = 41.234                  # flat-lined for 20 samples
    v = series(temp, ch="bearing_temp")
    v[:, 1, CH["load_tph"]] = 0.0          # stopped belt: legitimately zero for the whole window
    running = np.zeros(40, dtype=bool)
    st = detect_stuck(v, running)
    assert st[10:30, 2, CH["bearing_temp"]].all() and not st[:, 1, CH["load_tph"]].any()


# ---------------------------------------------------------------- schedule & trends
def test_running_hours_skip_the_planned_stop():
    # 23:00 IST: 1 running hour until midnight, 6 h planned stop, then 1 more running hour
    t = C.SITE_TZ.utcoffset(None)
    from datetime import datetime
    ts = datetime(2026, 11, 14, 23, 0, tzinfo=C.SITE_TZ).timestamp()
    assert abs(running_to_calendar(ts, 2.0) - 8.0) < 1e-6


def test_trend_to_threshold_finds_exponential_growth_and_ignores_noise():
    rng = np.random.default_rng(2)
    t = np.arange(150, dtype=float)
    grow = 2.0 + 0.05 * np.exp(0.04 * np.clip(t - 60, 0, None)) + rng.normal(0, 0.02, 150)
    r = trend_to_threshold(grow, limit=6.0, rng=rng)
    true_cross = 60 + np.log((6.0 - 2.0) / 0.05) / 0.04 - 149
    assert r is not None and abs(r["p50"] - true_cross) < 0.35 * true_cross
    assert trend_to_threshold(2.0 + rng.normal(0, 0.1, 150), limit=6.0, rng=rng) is None


# ---------------------------------------------------------------- belt loop
def test_circular_distance_wraps():
    assert circ_dist(12_049.0, 1.0, 12_050.0) == 2.0


def test_tag_localisation_beats_dead_reckoning():
    reg = BeltRegister.default()
    k = 1.004                                            # +0.4 % odometer error
    reads = [(i, p * k) for i, p in enumerate(reg.tag_pos) if i != 7]   # one missed read
    true = np.array([500.0, 3_333.3, 11_900.0])
    odo = true * k
    tags = Localizer(reg, "tags").map_pass(reads, odo)
    dead = Localizer(reg, "pass_start").map_pass(reads, odo)
    assert np.all(np.abs(tags - true) < 0.5)
    assert np.abs(dead - true).max() > 40


def det(pos, p, conf=0.8, lat=0.1):
    return Detection(pos, lat, "Tear", conf, 0.3, 0.05, float(p), p)


def test_tracker_dedups_confirms_drops_transients_and_respects_coverage():
    L = 12_050.0
    full = [(0.0, L)]
    tr = BeltTracker(L)
    tr.process_pass(0, [det(100.0, 0), det(5_000.0, 0)], full)        # real defect + a transient
    tr.process_pass(1, [det(100.6, 1)], full)                           # defect again, transient gone
    tr.process_pass(2, [det(99.7, 2)], full)
    live = {t.id: t for t in tr.tracks.values()}
    real = [t for t in live.values() if abs(t.pos - 100) < 2]
    assert len(real) == 1 and real[0].status == "confirmed" and real[0].n_seen == 3   # 3 sightings, 1 defect
    assert [t for t in live.values() if abs(t.pos - 5_000) < 2][0].status == "dropped"
    # the stretch with the defect is not imaged for 5 passes -> no evidence, nothing changes
    for p in range(3, 8):
        tr.process_pass(p, [], [(1_000.0, L)])
    assert real[0].status == "confirmed"
    # imaged 3 times without a detection -> repaired
    for p in range(8, 11):
        tr.process_pass(p, [], full)
    assert real[0].status == "resolved"


def test_tracker_associates_across_the_wrap_point():
    L = 12_050.0
    tr = BeltTracker(L)
    tr.process_pass(0, [det(12_049.5, 0)], [(0.0, L)])
    tr.process_pass(1, [det(0.7, 1)], [(0.0, L)])
    assert len(tr.tracks) == 1 and next(iter(tr.tracks.values())).status == "confirmed"


def test_world_location_downstream_of_camera():
    # a defect 50 m past the camera (smaller belt coordinate) is further along the return strand
    s1, c1 = world_location(100.0, 100.0, 12_050.0)
    s2, c2 = world_location(50.0, 100.0, 12_050.0)
    assert s1 == s2 == "return" and c2 == c1 - 50
