"""Backend side of the vision component: pass reports in, persistent defect tracks out.

The edge node (vision/edge_sim.py) runs the detector next to the camera and sends
one compact report per belt revolution: odometer readings of RFID tag reads, the
odometer ranges it actually imaged, and detections (+ small JPEG crops). Images
never leave the edge - at 5 frames/s they would swamp a mine-site radio link.

Localisation and multi-pass association happen here, centrally, because they need
history across revolutions and are cheap.
"""
from __future__ import annotations

import base64
import json
import sqlite3

import numpy as np

from backend.db import get_state, set_state
from vision.loop import BeltRegister, BeltTracker, Detection, Localizer, Track, world_location


class VisionService:
    def __init__(self, con: sqlite3.Connection, register: BeltRegister | None = None):
        self.con = con
        self.reg = register or BeltRegister.default()
        self.loc = Localizer(self.reg, mode="tags")
        self.tracker = BeltTracker(self.reg.L)
        self._restore()

    def _restore(self) -> None:
        for r in self.con.execute("SELECT * FROM vision_tracks"):
            t = Track(r["track_id"], r["belt_pos_m"], r["lateral_m"], json.loads(r["cls_votes"]), r["first_ts"],
                      r["last_ts"], r["status"], r["n_seen"], r["n_imaged"], r["consecutive_miss"],
                      [h["pass"] for h in json.loads(r["history"])], json.loads(r["history"]), r["crop"])
            self.tracker.tracks[t.id] = t
            self.tracker.next_id = max(self.tracker.next_id, t.id + 1)
        self.tracker.n_raw = self.con.execute("SELECT COUNT(*) FROM vision_detections").fetchone()[0]

    def process(self, rep: dict) -> dict:
        con, reg = self.con, self.reg
        reads = [(int(r["tag"]), float(r["odo_m"])) for r in rep.get("tag_reads", [])]
        dets_raw = rep.get("detections", [])
        odos = np.array([float(d["odo_m"]) for d in dets_raw])
        pos = self.loc.map_pass(reads, odos) if len(odos) else np.array([])
        cov = []
        for a, b in rep.get("imaged_odo_ranges", []):
            pa, pb = self.loc.map_pass(reads, np.array([float(a), float(b)]))
            if np.isfinite(pa) and np.isfinite(pb):
                cov.append((float(pa), float(pb)))
        dets = []
        for d, p in zip(dets_raw, pos):
            if not np.isfinite(p):
                continue
            crop = base64.b64decode(d["crop_jpeg_b64"]) if d.get("crop_jpeg_b64") else None
            dets.append(Detection(float(p), float(d["lateral_m"]), d["cls"], float(d["conf"]), float(d["length_m"]),
                                  float(d["width_m"]), float(d["ts"]), int(rep["pass_id"]), crop))
        res = self.tracker.process_pass(int(rep["pass_id"]), dets, cov)
        con.execute("INSERT OR REPLACE INTO vision_passes(pass_id, ts_start, ts_end, frames_imaged, tags_read, "
                    "tags_expected, n_detections, scale_factor, coverage) VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(rep["pass_id"]), int(rep["ts_start"]), int(rep["ts_end"]), int(rep.get("frames_imaged", 0)),
                     len(reads), len(reg.segment_lengths), len(dets_raw), self.loc.last_scale, json.dumps(cov)))
        con.executemany("INSERT INTO vision_detections(pass_id, ts, segment, seg_offset_m, belt_pos_m, lateral_m, cls,"
                        " conf, length_m, width_m) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(d.pass_id, int(d.ts), *reg.segment_of(d.pos), d.pos, d.lateral, d.cls, d.conf, d.length_m,
                          d.width_m) for d in dets])
        for t in self.tracker.tracks.values():
            self._save(t)
        # Which belt coordinate was under the camera at the end of the pass (to locate defects when stopped)
        if rep.get("odo_end") is not None and reads:
            b_end = float(self.loc.map_pass(reads, np.array([float(rep["odo_end"])]))[0])
            set_state(con, "vision_cam_belt_pos", b_end)
            set_state(con, "vision_cam_ts", int(rep["ts_end"]))
        con.commit()
        return res

    def _save(self, t: Track) -> None:
        seg, off = self.reg.segment_of(t.pos)
        self.con.execute(
            "INSERT OR REPLACE INTO vision_tracks(track_id, status, cls, cls_votes, segment, seg_offset_m, belt_pos_m, "
            "lateral_m, first_ts, last_ts, n_seen, n_imaged, consecutive_miss, conf_mean, length_m, width_m, "
            "growth_mm_per_day, severity, history, crop) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.id, t.status, t.cls, json.dumps(t.cls_votes), seg, off, t.pos, t.lateral, int(t.first_ts),
             int(t.last_ts), t.n_seen, t.n_imaged, t.consecutive_miss, t.conf_mean, t.length_m, None,
             t.growth_mm_per_day(), t.severity(), json.dumps(t.history[-50:]), t.crop))

    def locate_now(self, belt_speed_m_s: float, running_seconds_since: float) -> float | None:
        b = get_state(self.con, "vision_cam_belt_pos")
        if b is None:
            return None
        return (float(b) + belt_speed_m_s * running_seconds_since) % self.reg.L

    def where_is(self, belt_pos: float, cam_pos_now: float | None) -> dict | None:
        if cam_pos_now is None:
            return None
        strand, ch = world_location(belt_pos, cam_pos_now, self.reg.L)
        return {"strand": strand, "chainage_m": round(ch)}
