"""SQLite storage. One file, WAL mode, no server to install.

Why SQLite: the whole system must run on a laptop with one command. The schema is
written so it ports to TimescaleDB/Postgres unchanged in spirit (readings is a
narrow time-series table keyed by (station, channel, ts)). See DESIGN.md for the
production path.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get("BELT_DB", ROOT / "data" / "belt.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Narrow time-series table. The primary key makes ingestion idempotent:
-- a retried / duplicated batch is ignored (INSERT OR IGNORE).
CREATE TABLE IF NOT EXISTS readings (
    station_id  TEXT    NOT NULL,
    channel     TEXT    NOT NULL,
    ts          INTEGER NOT NULL,          -- epoch seconds UTC, snapped to the 5-min grid
    value       REAL,                      -- canonical SI-ish unit; NULL if missing/error code
    quality     INTEGER NOT NULL DEFAULT 0,-- bit flags, see backend/ingest.py
    seq         INTEGER,
    received_ts INTEGER NOT NULL,
    PRIMARY KEY (station_id, channel, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts INTEGER NOT NULL,
    gateway_id  TEXT,
    reason      TEXT NOT NULL,
    payload     TEXT
);

CREATE TABLE IF NOT EXISTS ingest_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts INTEGER NOT NULL,          -- simulated "now" when the batch arrived
    gateway_id  TEXT,
    n_received INTEGER, n_accepted INTEGER, n_duplicate INTEGER, n_rejected INTEGER,
    n_error_code INTEGER, n_out_of_range INTEGER, n_unit_converted INTEGER, n_alias INTEGER, n_late INTEGER
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    category   TEXT NOT NULL,              -- equipment | data_quality
    type       TEXT NOT NULL,
    severity   TEXT NOT NULL,              -- low | medium | high | critical
    status     TEXT NOT NULL,              -- active | cleared
    started_ts INTEGER NOT NULL,
    last_ts    INTEGER NOT NULL,
    cleared_ts INTEGER,
    score      REAL,
    peak_score REAL,
    message    TEXT,
    evidence   TEXT
);
CREATE INDEX IF NOT EXISTS anomalies_status ON anomalies(status, station_id);

CREATE TABLE IF NOT EXISTS predictions (
    station_id TEXT NOT NULL,
    mode       TEXT NOT NULL,
    ts         INTEGER NOT NULL,           -- decision time
    probability REAL,                      -- P(failure within horizon)
    ttf_p10_h REAL, ttf_p50_h REAL, ttf_p90_h REAL,
    method     TEXT,                       -- ml | trend | ml+trend
    confidence TEXT,                       -- high | reduced (data gaps) | low
    evidence   TEXT,
    PRIMARY KEY (station_id, mode, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS station_status (
    station_id TEXT PRIMARY KEY,
    ts INTEGER, status TEXT, reasons TEXT, sensor_health TEXT
);

CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);

-- CMMS stand-in: failures and repairs logged by maintenance (comes from the simulator).
CREATE TABLE IF NOT EXISTS maintenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, station_id TEXT, mode TEXT, kind TEXT, note TEXT
);

-- Vision: one row per camera pass, per raw detection, per persistent defect track.
CREATE TABLE IF NOT EXISTS vision_passes (
    pass_id INTEGER PRIMARY KEY,
    ts_start INTEGER, ts_end INTEGER,
    frames_imaged INTEGER, tags_read INTEGER, tags_expected INTEGER,
    n_detections INTEGER, scale_factor REAL, coverage TEXT
);
CREATE TABLE IF NOT EXISTS vision_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id INTEGER, ts INTEGER, segment INTEGER, seg_offset_m REAL, belt_pos_m REAL,
    lateral_m REAL, cls TEXT, conf REAL, length_m REAL, width_m REAL, track_id INTEGER
);
CREATE TABLE IF NOT EXISTS vision_tracks (
    track_id INTEGER PRIMARY KEY,
    status TEXT,                    -- tentative | confirmed | resolved
    cls TEXT, cls_votes TEXT,
    segment INTEGER, seg_offset_m REAL, belt_pos_m REAL, lateral_m REAL,
    first_ts INTEGER, last_ts INTEGER, n_seen INTEGER, n_imaged INTEGER, consecutive_miss INTEGER,
    conf_mean REAL, length_m REAL, width_m REAL, growth_mm_per_day REAL, severity TEXT,
    history TEXT, crop BLOB
);
"""

_local = threading.local()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get_state(con: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_state(con: sqlite3.Connection, k: str, v) -> None:
    con.execute("INSERT INTO state(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
