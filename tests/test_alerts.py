import json

from backend.analysis import alert_state
from backend.db import connect


def add(con, ts, p, thr=0.1):
    con.execute("INSERT INTO predictions(station_id, mode, ts, probability, method, confidence, evidence) "
                "VALUES ('S11', 'bearing', ?, ?, 'ml', 'high', ?)", (ts, p, json.dumps({"threshold": thr})))


def test_single_spike_above_threshold_is_not_an_alert(tmp_path):
    con = connect(tmp_path / "a.db")
    add(con, 3600 * 10, 0.05)
    add(con, 3600 * 11, 0.26)            # one hourly decision above threshold
    add(con, 3600 * 11 + 300, 0.30)      # intermediate (non-hourly) decisions do not count
    assert not alert_state(con, "S11", "bearing")


def test_two_consecutive_hourly_decisions_raise_an_alert(tmp_path):
    con = connect(tmp_path / "b.db")
    add(con, 3600 * 10, 0.12)
    add(con, 3600 * 11, 0.40)
    assert alert_state(con, "S11", "bearing")
