"""FastAPI backend: receive, store, analyse, serve.

    uvicorn backend.app:app --port 8000

Write path:  POST /api/ingest (sensor gateways), POST /api/vision/pass (camera edge
             node), POST /api/maintenance (CMMS stand-in).
Read path:   GET /api/* for the dashboard (served at /).
Analysis:    a background thread runs Analyzer.cycle() whenever new data arrived.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.analysis import Analyzer, alert_state
from backend.db import DEFAULT_DB, connect, get_state
from backend.ingest import normalize, store
from backend.vision_service import VisionService
from simulator import config as C

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("BELT_DB", DEFAULT_DB))
STATION = {s.id: s for s in C.STATIONS}


class IngestBatch(BaseModel):
    gateway_id: str | None = None
    sent_at: str | None = None
    readings: list[dict] = Field(default_factory=list)   # validated record-by-record in backend.ingest


class _Worker:
    def __init__(self):
        self.stop = threading.Event()
        self.last: dict | None = None
        self.error: str | None = None
        self.vision_lock = threading.Lock()
        self.vision: VisionService | None = None

    def run(self):
        con = connect(DB_PATH)
        an = Analyzer(con)
        while not self.stop.is_set():
            try:
                r = an.cycle()
                if r:
                    self.last = r
                    self.error = None
            except Exception as e:  # keep serving; surface the error in /api/overview
                import traceback
                self.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            self.stop.wait(1.0)


W = _Worker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    W.vision = VisionService(connect(DB_PATH))
    th = threading.Thread(target=W.run, daemon=True)
    th.start()
    yield
    W.stop.set()


app = FastAPI(title="Conveyor belt health", lifespan=lifespan)


def db():
    return connect(DB_PATH)


def _iso(ts) -> str | None:
    return None if ts is None else datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


# ============================================================================ write path
def _received(batch: IngestBatch) -> float:
    try:
        return pd.Timestamp(batch.sent_at).timestamp() if batch.sent_at else time.time()
    except ValueError:
        return time.time()


@app.post("/api/ingest")
def ingest(batch: IngestBatch):
    return _ingest([batch])


@app.post("/api/ingest/bulk")
def ingest_bulk(batches: list[IngestBatch]):
    """Many gateway batches in one request (used for backfill); each keeps its own sent_at,
    so 'late' still means late."""
    return _ingest(batches)


def _ingest(batches: list[IngestBatch]):
    con = db()
    frames = [pd.DataFrame(b.readings).assign(_rec=_received(b), _gw=b.gateway_id) for b in batches if b.readings]
    if not frames:
        return {"received": 0}
    df = pd.concat(frames, ignore_index=True)
    received = float(df._rec.max())
    acc, rej, cnt = normalize(df.drop(columns=["_rec"]), df._rec)
    inserted = store(con, acc)
    dup = cnt["duplicate_in_batch"] + (len(acc) - inserted)
    if len(rej):
        con.executemany("INSERT INTO dead_letter(received_ts, gateway_id, reason, payload) VALUES (?,?,?,?)",
                        [(int(received), r.get("_gw"), r["reason"],
                          json.dumps({k: (None if pd.isna(v) else v) for k, v in r.items()
                                      if k in ("station_id", "sensor", "ts", "value", "unit")}, default=str))
                         for r in rej.to_dict("records")])
    con.execute("INSERT INTO ingest_batches(received_ts, gateway_id, n_received, n_accepted, n_duplicate, n_rejected, "
                "n_error_code, n_out_of_range, n_unit_converted, n_alias, n_late) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (int(received), ",".join(sorted({b.gateway_id or "?" for b in batches})), cnt["received"], inserted, dup,
                 cnt["rejected"], cnt["error_code"],
                 cnt["out_of_range"], cnt["unit_converted"], cnt["alias"], cnt["late"]))
    con.commit()
    return {"received": cnt["received"], "accepted": inserted, "duplicates": dup, "rejected": cnt["rejected"],
            "reject_reasons": cnt["reject_reasons"]}


@app.post("/api/maintenance")
def maintenance(events: list[dict] = Body(...)):
    con = db()
    con.executemany("INSERT INTO maintenance_log(ts, station_id, mode, kind, note) VALUES (?,?,?,?,?)",
                    [(int(e["ts"]), e.get("station_id"), e.get("mode"), e["kind"], e.get("note")) for e in events])
    con.commit()
    return {"stored": len(events)}


@app.post("/api/vision/pass")
def vision_pass(report: dict = Body(...)):
    with W.vision_lock:
        return W.vision.process(report)


# ============================================================================ read path
@app.get("/api/config")
def config():
    return C.describe()


def _latest_values(con, now: int) -> dict:
    rows = con.execute("SELECT station_id, channel, value FROM readings WHERE ts > ? AND value IS NOT NULL "
                       "AND (quality & 15) = 0 ORDER BY ts", (now - 1800,)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["station_id"], {})[r["channel"]] = r["value"]
    return out


@app.get("/api/overview")
def overview():
    con = db()
    now = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    if now is None:
        return {"now": None, "message": "waiting for data"}
    st = {r["station_id"]: r["status"] for r in con.execute("SELECT station_id, status FROM station_status")}
    counts = {k: sum(1 for v in st.values() if v == k) for k in ("normal", "abnormal", "predicted_failure", "no_data")}
    sev = {r[0]: r[1] for r in con.execute("SELECT severity, COUNT(*) FROM anomalies WHERE status='active' AND "
                                           "category='equipment' GROUP BY severity")}
    lv = _latest_values(con, now)
    tot = con.execute("SELECT SUM(n_received), SUM(n_accepted), SUM(n_duplicate), SUM(n_rejected), SUM(n_late) "
                      "FROM ingest_batches").fetchone()
    vt = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM vision_tracks GROUP BY status")}
    return {
        "now": int(now), "now_iso": _iso(now),
        "running": bool(int(get_state(con, "running_now", 0) or 0)),
        "throughput_tph": lv.get("S02", {}).get("load_tph"),
        "belt_speed_m_s": lv.get("S24", {}).get("belt_speed"),
        "status_counts": counts, "active_anomalies_by_severity": sev,
        "vision_tracks": vt,
        "ingest": {"received": tot[0], "accepted": tot[1], "duplicates": tot[2], "rejected": tot[3], "late": tot[4]},
        "analysis": {"last": W.last, "error": W.error},
    }


@app.get("/api/stations")
def stations():
    con = db()
    now = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0] or 0
    lv = _latest_values(con, now)
    st = {r["station_id"]: r for r in con.execute("SELECT * FROM station_status")}
    risk = {}
    for r in con.execute("SELECT p.station_id, p.mode, p.probability, p.ttf_p50_h FROM predictions p JOIN "
                         "(SELECT station_id, mode, MAX(ts) mts FROM predictions GROUP BY station_id, mode) m "
                         "ON p.station_id=m.station_id AND p.mode=m.mode AND p.ts=m.mts"):
        cur = risk.get(r["station_id"])
        if cur is None or (r["probability"] or 0) > cur["probability"]:
            risk[r["station_id"]] = {"mode": r["mode"], "probability": r["probability"] or 0, "ttf_p50_h": r["ttf_p50_h"]}
    out = []
    for s in C.STATIONS:
        row = st.get(s.id)
        sh = json.loads(row["sensor_health"]) if row else {}
        out.append({
            "id": s.id, "name": s.name, "chainage_m": s.chainage_m, "kind": s.kind, "zone": s.zone,
            "elevation_m": round(C.elevation_at(s.chainage_m), 1),
            "status": row["status"] if row else "no_data",
            "reasons": json.loads(row["reasons"]) if row else ["No analysis yet"],
            "completeness_24h": sh.get("completeness_24h"), "sensor_issues": sh.get("issues", []),
            "latest": lv.get(s.id, {}), "top_risk": risk.get(s.id),
        })
    return out


@app.get("/api/stations/{sid}/series")
def station_series(sid: str, hours: int = 72):
    if sid not in STATION:
        raise HTTPException(404, "unknown station")
    con = db()
    now = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0] or 0
    t0 = now - hours * 3600
    df = pd.read_sql_query("SELECT channel, ts, value, quality FROM readings WHERE station_id=? AND ts>=? "
                           "AND value IS NOT NULL AND (quality & 3)=0", con, params=(sid, t0))
    series = {}
    # belt running per 30-min bucket (drive pulley speed, else tail belt speed)
    rs = pd.read_sql_query("SELECT station_id, ts, value FROM readings WHERE ts>=? AND value IS NOT NULL AND "
                           "((station_id='S24' AND channel='drive_speed') OR (station_id='S01' AND channel='belt_speed'))",
                           con, params=(t0,))
    running_b: dict[int, bool] = {}
    stopped = []
    if not rs.empty:
        rs["h"] = (rs.ts // 1800) * 1800
        frac = rs.assign(r=rs.value > 0.5).groupby("h").r.mean()
        running_b = {int(h): bool(f >= 0.5) for h, f in frac.items()}
        cur = None
        for h in sorted(running_b):
            if not running_b[h] and cur is None:
                cur = [h, h + 1800]
            elif not running_b[h]:
                cur[1] = h + 1800
            elif cur is not None:
                stopped.append(cur)
                cur = None
        if cur is not None:
            stopped.append(cur)
    running_only = {"vib_rms", "vib_env", "gearbox_vib", "motor_current", "drive_speed", "belt_speed", "load_tph"}
    if not df.empty:
        df["h"] = (df.ts // 1800) * 1800
        for ch, g in df.groupby("channel"):
            m = g.groupby("h").value.median()
            vals = [None if (ch in running_only and not running_b.get(int(h), True)) else round(float(x), 3)
                    for h, x in zip(m.index, m.to_numpy())]
            series[ch] = {"t": m.index.astype(int).tolist(), "v": vals}
    preds = {}
    for r in con.execute("SELECT mode, ts, probability FROM predictions WHERE station_id=? AND ts>=? ORDER BY ts",
                         (sid, t0)):
        p = preds.setdefault(r["mode"], {"t": [], "v": []})
        p["t"].append(r["ts"])
        p["v"].append(round(r["probability"] or 0, 3))
    anomalies = [dict(r) for r in con.execute(
        "SELECT * FROM anomalies WHERE station_id=? AND (status='active' OR last_ts>=?) ORDER BY started_ts DESC",
        (sid, t0))]
    return {"station": C.describe()["stations"][C.STATION_INDEX[sid]], "now": now, "series": series,
            "predictions": preds, "anomalies": anomalies, "stopped": stopped}


@app.get("/api/anomalies")
def anomalies(status: str = "active", category: str | None = None, limit: int = 200):
    con = db()
    q, args = "SELECT * FROM anomalies WHERE 1=1", []
    if status != "all":
        q += " AND status=?"
        args.append(status)
    if category:
        q += " AND category=?"
        args.append(category)
    q += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, " \
         "started_ts DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in con.execute(q, args):
        d = dict(r)
        s = STATION[d["station_id"]]
        d.update(station_name=s.name, chainage_m=s.chainage_m, evidence=json.loads(d["evidence"] or "{}"))
        out.append(d)
    return out


@app.get("/api/predictions")
def predictions(min_probability: float = 0.05):
    con = db()
    now = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0] or 0
    out = []
    for r in con.execute("SELECT p.* FROM predictions p JOIN (SELECT station_id, mode, MAX(ts) mts FROM predictions "
                         "GROUP BY station_id, mode) m ON p.station_id=m.station_id AND p.mode=m.mode AND p.ts=m.mts "
                         "WHERE p.probability >= ? ORDER BY p.probability DESC", (min_probability,)):
        d = dict(r)
        ev = json.loads(d.pop("evidence") or "{}")
        s = STATION[d["station_id"]]
        d.update(station_name=s.name, chainage_m=s.chainage_m, mode_label=C.FAILURE_MODES[d["mode"]],
                 evidence=ev.get("text"), threshold=ev.get("threshold", 0.5), age_h=round((now - d["ts"]) / 3600, 1),
                 alert=alert_state(con, d["station_id"], d["mode"]))
        if d["ttf_p50_h"] is not None:
            d["eta_iso"] = _iso(d["ts"] + d["ttf_p50_h"] * 3600)
            d["eta_p10_iso"] = _iso(d["ts"] + (d["ttf_p10_h"] or 0) * 3600)
            d["eta_p90_iso"] = _iso(d["ts"] + d["ttf_p90_h"] * 3600) if d["ttf_p90_h"] else None
        out.append(d)
    return out


@app.get("/api/vision/tracks")
def vision_tracks(status: str = "active"):
    con = db()
    if status == "active":
        rows = con.execute("SELECT * FROM vision_tracks WHERE status IN ('confirmed','tentative') ORDER BY "
                           "CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, last_ts DESC").fetchall()
    else:
        rows = con.execute("SELECT * FROM vision_tracks ORDER BY last_ts DESC").fetchall()
    running = bool(int(get_state(con, "running_now", 0) or 0))
    cam_ts = get_state(con, "vision_cam_ts")
    now = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0] or 0
    cam_now = None
    if cam_ts is not None and W.vision is not None:
        rs = json.loads(get_state(con, "running_series", "{}") or "{}")
        run_s = 0.0
        if rs:
            ts = rs["t0"] + rs["step"] * np.arange(len(rs["running"]))
            run_s = float(np.sum(np.array(rs["running"])[ts > int(cam_ts)]) * rs["step"])
        cam_now = W.vision.locate_now(C.BELT_SPEED_M_S, run_s)
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k != "crop"}
        d["history"] = json.loads(d["history"])
        d["cls_votes"] = json.loads(d["cls_votes"])
        d["has_crop"] = r["crop"] is not None
        d["where_if_stopped"] = W.vision.where_is(r["belt_pos_m"], cam_now) if W.vision else None
        out.append(d)
    return {"running": running, "tracks": out}


@app.get("/api/vision/tracks/{tid}/crop.jpg")
def vision_crop(tid: int):
    r = db().execute("SELECT crop FROM vision_tracks WHERE track_id=?", (tid,)).fetchone()
    if not r or r["crop"] is None:
        raise HTTPException(404)
    return Response(r["crop"], media_type="image/jpeg")


@app.get("/api/vision/summary")
def vision_summary():
    con = db()
    p = con.execute("SELECT COUNT(*), SUM(n_detections), SUM(tags_read), SUM(tags_expected) FROM vision_passes").fetchone()
    last = con.execute("SELECT * FROM vision_passes ORDER BY pass_id DESC LIMIT 1").fetchone()
    st = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM vision_tracks GROUP BY status")}
    reg = W.vision.reg if W.vision else None
    return {"passes": p[0], "raw_detections": p[1] or 0, "tag_read_rate": (p[2] / p[3]) if p[3] else None,
            "tracks": st, "loop_length_m": reg.L if reg else C.LOOP_LENGTH_M,
            "splices": [round(x, 1) for x in reg.tag_pos] if reg else [],
            "last_pass": dict(last) if last else None}


@app.get("/api/data-quality")
def data_quality():
    con = db()
    tot = con.execute("SELECT SUM(n_received), SUM(n_accepted), SUM(n_duplicate), SUM(n_rejected), SUM(n_error_code),"
                      " SUM(n_out_of_range), SUM(n_unit_converted), SUM(n_alias), SUM(n_late) FROM ingest_batches").fetchone()
    keys = ["received", "accepted", "duplicates", "rejected", "error_codes", "out_of_range", "unit_converted",
            "legacy_tag_mapped", "late"]
    reasons = {r[0]: r[1] for r in con.execute("SELECT reason, COUNT(*) FROM dead_letter GROUP BY reason")}
    stations = []
    for r in con.execute("SELECT station_id, sensor_health FROM station_status ORDER BY station_id"):
        sh = json.loads(r["sensor_health"])
        stations.append({"station_id": r["station_id"], "completeness_24h": sh.get("completeness_24h"),
                         "issues": sh.get("issues", [])})
    dq = [dict(r) for r in con.execute("SELECT * FROM anomalies WHERE category='data_quality' AND status='active' "
                                       "ORDER BY started_ts DESC")]
    return {"ingest": dict(zip(keys, tot)), "reject_reasons": reasons, "stations": stations, "active_issues": dq}


@app.get("/api/maintenance")
def maintenance_log(limit: int = 50):
    return [dict(r) for r in db().execute("SELECT * FROM maintenance_log ORDER BY ts DESC LIMIT ?", (limit,))]


DASH = ROOT / "dashboard"
if DASH.exists():
    app.mount("/static", StaticFiles(directory=DASH), name="static")


@app.get("/")
def index():
    return FileResponse(DASH / "index.html")
