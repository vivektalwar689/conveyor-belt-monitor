"""Record a run of the real system into a static, replayable dashboard (for GitHub Pages).

    python -m scripts.record_replay              # -> docs/demo/ (open docs/demo/index.html via any static server)

Runs the actual backend + simulator + camera edge node on a fixed simulated clock,
and every ``--frame-min`` simulated minutes saves the responses of every API the
dashboard uses. The dashboard's own code then replays those frames (window.BELT_REPLAY),
so the hosted page is the real dashboard, not a mock-up. It is labelled as a recording.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
STATIONS = [f"S{i:02d}" for i in range(1, 25)]


def wait_for(cond, timeout: float, what: str, every: float = 0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = cond()
            if r:
                return r
        except httpx.HTTPError:
            pass
        time.sleep(every)
    raise SystemExit(f"timed out waiting for {what}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=30, help="simulated hours to record after the backfill")
    ap.add_argument("--frame-min", type=int, default=30, help="simulated minutes between frames")
    ap.add_argument("--speed", type=float, default=150)
    ap.add_argument("--live-start", default="2026-11-14T04:30:00", help="UTC; 10:00 site time")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "demo")
    a = ap.parse_args()

    db = ROOT / "data" / "replay.db"
    for suf in ("", "-wal", "-shm"):
        Path(str(db) + suf).unlink(missing_ok=True)
    env = {**os.environ, "BELT_DB": str(db), "OMP_NUM_THREADS": "2", "PYTHONUNBUFFERED": "1"}
    url = f"http://127.0.0.1:{a.port}"
    out = a.out
    if out.exists():
        shutil.rmtree(out)
    for sub in ("data/frames", "data/series", "data/crops"):
        (out / sub).mkdir(parents=True)
    procs = []
    c = httpx.Client(base_url=url, timeout=60)
    try:
        env["BELT_INPROCESS_ANALYSIS"] = "0"
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app:app", "--port", str(a.port),
                                       "--log-level", "warning"], cwd=ROOT, env=env))
        procs.append(subprocess.Popen([sys.executable, "-m", "backend.worker"], cwd=ROOT, env=env))
        wait_for(lambda: c.get("/api/config").status_code == 200, 60, "backend")
        procs.append(subprocess.Popen([sys.executable, "-m", "simulator.stream", "--url", url, "--speed", str(a.speed),
                                       "--live-start", a.live_start], cwd=ROOT, env=env))
        live_start = int(datetime.fromisoformat(a.live_start).replace(tzinfo=timezone.utc).timestamp())
        # backfill done and the camera's history posted
        wait_for(lambda: (c.get("/api/vision/summary").json().get("passes") or 0) >= 8, 600, "camera backfill")
        frames, step = [], a.frame_min * 60
        n = int(a.hours * 60 / a.frame_min) + 1
        for i in range(n):
            target = live_start + i * step
            wait_for(lambda: ((c.get("/api/overview").json().get("analysis") or {}).get("last") or {}).get("now", 0)
                     >= target, 900, f"frame {i}")
            vis = c.get("/api/vision/tracks").json()
            for t in vis["tracks"]:
                t["history"] = t["history"][-8:]               # keep frames small
            f = {
                "overview": c.get("/api/overview").json(),
                "stations": c.get("/api/stations").json(),
                "predictions": c.get("/api/predictions").json(),
                "anomalies": c.get("/api/anomalies?status=active").json(),
                "vision_tracks": vis,
                "vision_summary": c.get("/api/vision/summary").json(),
                "dq": c.get("/api/data-quality").json(),
                "maintenance": c.get("/api/maintenance?limit=30").json(),
            }
            if frames and frames[-1]["now"] == f["overview"]["now"]:
                continue                                   # stream ran ahead: same moment, don't store it twice
            k = len(frames)
            (out / "data" / "frames" / f"{k:04d}.json").write_text(json.dumps(f, separators=(",", ":")))
            frames.append({"i": k, "now": f["overview"]["now"]})
            print(f"  frame {i + 1}/{n}  {f['overview']['now_iso']}  status={f['overview']['status_counts']}", flush=True)
        # whole-run histories per station (the page slices the 72 h window for each frame)
        span_h = int((frames[-1]["now"] - live_start) / 3600) + 8 * 24 + 1
        for sid in STATIONS:
            s = c.get(f"/api/stations/{sid}/series?hours={span_h}").json()
            s.pop("anomalies", None)
            (out / "data" / "series" / f"{sid}.json").write_text(json.dumps(s, separators=(",", ":")))
        for t in c.get("/api/vision/tracks?status=all").json()["tracks"]:
            if t["has_crop"]:
                r = c.get(f"/api/vision/tracks/{t['track_id']}/crop.jpg")
                if r.status_code == 200:
                    (out / "data" / "crops" / f"{t['track_id']}.jpg").write_bytes(r.content)
        (out / "data" / "config.json").write_text(json.dumps(c.get("/api/config").json()))
        (out / "data" / "index.json").write_text(json.dumps({
            "frames": frames, "frame_minutes": a.frame_min, "hours": a.hours,
            "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "Recorded run of the simulator; see README for how to run the live system."}))
    finally:
        for p in reversed(procs):          # stop producers first, the API server last
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    # the dashboard itself, pointed at the recording
    html = (ROOT / "dashboard" / "index.html").read_text()
    html = html.replace('href="/static/styles.css"', 'href="styles.css"')
    html = html.replace('<script type="module" src="/static/app.js"></script>',
                        '<script>window.BELT_REPLAY = {base: "data/"};</script>\n<script type="module" src="app.js"></script>')
    html = html.replace("<title>CV-01 belt health</title>", "<title>CV-01 belt health (recorded replay)</title>")
    (out / "index.html").write_text(html)
    for f in ("app.js", "styles.css"):
        shutil.copy(ROOT / "dashboard" / f, out / f)
    print(f"replay written to {out} ({sum(p.stat().st_size for p in out.rglob('*') if p.is_file()) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
