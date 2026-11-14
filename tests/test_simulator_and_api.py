from datetime import datetime, timezone

import numpy as np
from fastapi.testclient import TestClient

from simulator import config as C
from simulator.engine import BeltSimulator, ScheduledFault, Scenario
from simulator.faults import MessyTransport, to_batches

START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_simulator_is_deterministic():
    a, b = BeltSimulator(START, seed=4), BeltSimulator(START, seed=4)
    for _ in range(300):
        va, vb = a.step().values, b.step().values
    assert np.allclose(va, vb, equal_nan=True)


def test_bearing_signatures_follow_the_pf_curve():
    """HF envelope departs first, then vibration RMS, then temperature (P-F ordering)."""
    sc = Scenario(random_degradation=False, trip_rate_per_day=0,
                  scheduled=[ScheduledFault(1, "S10", "bearing", tpf_running_h=100)])
    sim = BeltSimulator(START, seed=3, scenario=sc)
    i, env, rms, temp, run = C.STATION_INDEX["S10"], [], [], [], []
    failed_at = None
    for k in range(12 * 24 * 12):
        o = sim.step()
        env.append(o.values[i, C.CHANNEL_INDEX["vib_env"]])
        rms.append(o.values[i, C.CHANNEL_INDEX["vib_rms"]])
        temp.append(o.values[i, C.CHANNEL_INDEX["bearing_temp"]] - o.values[i, C.CHANNEL_INDEX["ambient_temp"]])
        run.append(o.running)
        if any(e["type"] == "failure" for e in o.events):
            failed_at = k
            break
    assert failed_at is not None
    env, rms, temp, run = map(np.array, (env, rms, temp, run))

    def first_departure(x, rel):
        base = np.median(x[run][:200])
        idx = np.flatnonzero(run & (x > base * (1 + rel)))
        return idx[0] if len(idx) else failed_at

    t_env, t_rms, t_temp = first_departure(env, 0.5), first_departure(rms, 0.5), first_departure(temp, 0.5)
    assert t_env < t_rms < t_temp < failed_at


def test_api_roundtrip_with_messy_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("BELT_DB", str(tmp_path / "api.db"))
    import importlib
    import backend.app as appmod
    importlib.reload(appmod)
    sim, tr = BeltSimulator(START, seed=1), MessyTransport(1, start=START)
    with TestClient(appmod.app) as client:
        for _ in range(24):
            o = sim.step()
            recs, _ = tr.process(o.t, o.values, sim.has)
            r = client.post("/api/ingest/bulk", json=to_batches(recs, o.t))
            assert r.status_code == 200
        bad = {"gateway_id": "GW-X", "sent_at": START.isoformat(), "readings": [
            {"station_id": "S99", "sensor": "vib_rms", "ts": "2026-06-01T01:00:00Z", "value": 1, "unit": "mm/s"},
            {"station_id": "S03", "sensor": "vib_rms", "ts": "2026-06-01T01:00:00", "value": 1, "unit": "mm/s"}]}
        r = client.post("/api/ingest", json=bad).json()
        assert r["rejected"] == 2
        dq = client.get("/api/data-quality").json()
        assert dq["ingest"]["accepted"] > 2000
        assert dq["reject_reasons"] == {"unknown_station": 1, "timestamp_without_timezone": 1}
        assert len(client.get("/api/stations").json()) == len(C.STATIONS)
