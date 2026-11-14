import math
from datetime import datetime, timezone

import pandas as pd

from backend.db import connect
from backend.ingest import (Q_ALIAS, Q_ERROR_CODE, Q_OUT_OF_RANGE, Q_UNIT_ASSUMED, Q_UNIT_CONVERTED, normalize,
                            store)

NOW = datetime(2026, 11, 14, 10, 1, tzinfo=timezone.utc).timestamp()
T = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc).timestamp()


def one(**kw):
    base = {"station_id": "S03", "sensor": "vib_rms", "ts": "2026-11-14T10:00:00Z", "value": 1.0, "unit": "mm/s"}
    base.update(kw)
    acc, rej, cnt = normalize(pd.DataFrame([base]), NOW)
    return acc, rej


def test_three_timestamp_formats_agree():
    for ts in ("2026-11-14T10:00:00Z", int(T * 1000), "2026-11-14T15:30:00+05:30", int(T)):
        acc, rej = one(ts=ts)
        assert rej.empty and int(acc.ts.iloc[0]) == int(T)


def test_naive_and_future_timestamps_are_rejected_not_guessed():
    assert one(ts="2026-11-14T10:00:00")[1].reason.iloc[0] == "timestamp_without_timezone"
    assert one(ts="2026-11-14T12:00:00Z")[1].reason.iloc[0] == "future_timestamp"


def test_unit_conversion_to_canonical():
    acc, _ = one(sensor="bearing_temp", value=104.0, unit="degF")
    assert math.isclose(acc.value.iloc[0], 40.0, abs_tol=1e-6) and acc.quality.iloc[0] & Q_UNIT_CONVERTED
    acc, _ = one(sensor="motor_temp", station_id="S24", value=353.15, unit="K")
    assert math.isclose(acc.value.iloc[0], 80.0, abs_tol=1e-6)
    acc, _ = one(value=0.05, unit="in/s")
    assert math.isclose(acc.value.iloc[0], 1.27, abs_tol=1e-6)


def test_incompatible_unit_rejected():
    assert one(sensor="bearing_temp", value=40, unit="mm")[1].reason.iloc[0] == "unit_mismatch"


def test_error_codes_become_null_with_flag():
    for v in (-999, 9999, 6553.5, "ERR", None):
        acc, rej = one(value=v)
        assert rej.empty and pd.isna(acc.value.iloc[0]) and acc.quality.iloc[0] & Q_ERROR_CODE


def test_out_of_range_kept_but_flagged():
    acc, _ = one(sensor="bearing_temp", value=400.0, unit="degC")
    assert acc.value.iloc[0] == 400.0 and acc.quality.iloc[0] & Q_OUT_OF_RANGE


def test_legacy_alias_and_missing_unit():
    acc, rej = one(station_id="s24", sensor="GBX_OIL_T", value=60.0, unit=None)
    assert rej.empty and acc.channel.iloc[0] == "gearbox_temp" and acc.station_id.iloc[0] == "S24"
    assert acc.quality.iloc[0] & Q_ALIAS and acc.quality.iloc[0] & Q_UNIT_ASSUMED


def test_unknown_station_and_uninstalled_channel_rejected():
    assert one(station_id="S99")[1].reason.iloc[0] == "unknown_station"
    assert one(sensor="load_tph", unit="t/h")[1].reason.iloc[0] == "channel_not_installed"


def test_duplicates_dropped_in_batch_and_on_store(tmp_path):
    rows = [{"station_id": "S03", "sensor": "vib_rms", "ts": "2026-11-14T10:00:00Z", "value": 1.0, "unit": "mm/s"}] * 3
    acc, _rej, cnt = normalize(pd.DataFrame(rows), NOW)
    assert len(acc) == 1 and cnt["duplicate_in_batch"] == 2
    con = connect(tmp_path / "t.db")
    assert store(con, acc) == 1
    assert store(con, acc) == 0          # at-least-once delivery: the retry is ignored
