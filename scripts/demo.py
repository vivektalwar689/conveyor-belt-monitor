"""One command for the whole system: fresh database, backend, simulated sensors + camera.

    python -m scripts.demo                # dashboard on http://localhost:8000
    python -m scripts.demo --speed 300    # faster simulated clock
    python -m scripts.demo --no-vision    # skip the camera (no torch/ultralytics needed)

What happens:
1. starts the FastAPI backend (uvicorn) on a fresh SQLite database;
2. backfills 8 simulated days through the normal ingest API (~20 s), so baselines exist;
3. streams live data at 120x real time (one simulated hour every 30 s), with the
   camera edge node posting one pass per belt revolution.
Ctrl+C stops everything.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--backfill-days", type=float, default=8)
    ap.add_argument("--speed", type=float, default=120)
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--db", default=str(ROOT / "data" / "belt.db"))
    a = ap.parse_args()

    db = Path(a.db)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    env = {**os.environ, "BELT_DB": str(db), "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "2"),
           "PYTHONUNBUFFERED": "1"}
    url = f"http://127.0.0.1:{a.port}"
    procs = []
    try:
        env["BELT_INPROCESS_ANALYSIS"] = "0"      # analysis runs as its own process (no GIL contention)
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app:app", "--port", str(a.port),
                                       "--log-level", "warning"], cwd=ROOT, env=env))
        procs.append(subprocess.Popen([sys.executable, "-m", "backend.worker"], cwd=ROOT, env=env))
        for _ in range(60):
            try:
                if httpx.get(url + "/api/config", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            raise SystemExit("backend did not start")
        print(f"\n  Dashboard: http://localhost:{a.port}\n")
        cmd = [sys.executable, "-m", "simulator.stream", "--url", url, "--backfill-days", str(a.backfill_days),
               "--speed", str(a.speed)]
        if a.no_vision:
            cmd.append("--no-vision")
        procs.append(subprocess.Popen(cmd, cwd=ROOT, env=env))
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
