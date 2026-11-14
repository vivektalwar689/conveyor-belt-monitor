"""Live mode: stream the simulated belt to the backend over HTTP, in accelerated time.

    python -m simulator.stream --url http://localhost:8000 --backfill-days 8 --speed 120

1. Backfill: simulate the last N days as fast as possible and POST them through
   the normal ingest API (so the dashboard has history and baselines).
2. Live: one 5-minute step every 300/speed wall seconds (speed 120 -> one simulated
   hour every 30 s), forever.

The demo scenario is scripted so there is something to see (a bearing in late-stage
failure that fails during the live phase, a mistracking belt on a
curve, an early-stage gearbox problem, a stuck and a drifting sensor, an offline
station, a dust storm, ...) on top of random background faults. Failures and repairs are
also posted to /api/maintenance, standing in for the CMMS work-order feed.
The camera edge node runs in a thread on the same simulated clock and only
produces passes while the belt runs.
"""
from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

from simulator import config as C
from simulator.engine import BeltSimulator, ScheduledFault, Scenario
from simulator.faults import DataQuality, ScheduledDQ, MessyTransport, to_batches


def demo_scenario(backfill_h: float) -> tuple[Scenario, DataQuality]:
    b = backfill_h
    sc = Scenario(name="demo", random_degradation=False, trip_rate_per_day=0.3, scheduled=[
        ScheduledFault(0, "S05", "bearing", tpf_running_h=48),                 # failed on day ~3 (history)
        ScheduledFault(b - 150, "S18", "bearing", tpf_running_h=125),         # late-stage at the start of the live demo
        ScheduledFault(b - 96, "S07", "bearing", tpf_running_h=112),          # predicted failure ~2 days out
        ScheduledFault(b - 40, "S12", "misalignment", 0, misalign_rate_mm_h=1.9, direction=1),
        ScheduledFault(b - 80, "S24", "gearbox", tpf_running_h=150),          # early stage, trend predictor
    ])
    dq = DataQuality(station_outage_rate=1 / 200, gateway_outage_rate=1 / 80, stuck_rate=1 / 6000,
                     drift_rate=0.0, storm_rate_per_day=1 / 30, scheduled=[
        ScheduledDQ(b - 30, "stuck", "S03", "bearing_temp", duration_h=60),
        ScheduledDQ(b - 70, "drift", "S10", "bearing_temp", duration_h=200, drift_c_per_day=3.5),
        ScheduledDQ(b - 60, "gateway_outage", "GW-2", duration_h=3, buffered=True),
        ScheduledDQ(b - 26, "storm", "", duration_h=5),
        ScheduledDQ(b - 1.5, "station_outage", "S20", duration_h=8, buffered=False),
    ])
    return sc, dq


class Streamer:
    def __init__(self, url: str, backfill_days: float, speed: float, seed: int, vision: bool):
        self.url, self.speed = url.rstrip("/"), speed
        self.client = httpx.Client(timeout=120)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % 5)
        self.start = now - timedelta(days=backfill_days)
        self.backfill_steps = int(backfill_days * 86400 / C.SAMPLE_PERIOD_S)
        sc, dq = demo_scenario(backfill_days * 24)
        self.sim = BeltSimulator(self.start, seed=seed, scenario=sc)
        self.tr = MessyTransport(seed, dq, self.start)
        self.running_s = 0.0          # cumulative running seconds (drives the camera passes)
        self.lock = threading.Lock()
        self.vision = vision
        self.sim_now = self.start
        self.run_log: list[tuple[float, bool]] = []   # (ts, running) per step, for placing camera passes

    def _post(self, path: str, payload) -> dict:
        for attempt in range(5):
            try:
                r = self.client.post(self.url + path, json=payload)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise
                print(f"  POST {path} failed ({e}); retrying")
                time.sleep(1 + attempt)
        return {}

    def _maintenance(self, events: list[dict]) -> None:
        log = []
        for e in events:
            if e["type"] == "failure":
                log.append({"ts": int(e["t"].timestamp()), "station_id": e["station"], "mode": e["mode"],
                            "kind": "failure", "note": f"{C.FAILURE_MODES[e['mode']]} - unplanned stop, "
                                                       f"repair {e['repair_hours']} h"})
            elif e["type"] == "repaired":
                log.append({"ts": int(e["t"].timestamp()), "station_id": e["station"], "mode": e["mode"],
                            "kind": "repaired", "note": "Component replaced, belt restarted"})
        if log:
            self._post("/api/maintenance", log)

    def step(self) -> tuple[list, datetime]:
        out = self.sim.step()
        recs, _dq = self.tr.process(out.t, out.values, self.sim.has)
        with self.lock:
            self.sim_now = out.t
            self.run_log.append((out.t.timestamp(), out.running))
            if out.running:
                self.running_s += C.SAMPLE_PERIOD_S
        self._maintenance(out.events)
        return recs, out.t

    def backfill(self) -> None:
        print(f"Backfilling {self.backfill_steps * C.SAMPLE_PERIOD_S / 86400:.1f} days from {self.start:%Y-%m-%d %H:%M} UTC")
        buf, n, t0 = [], 0, time.time()
        for k in range(self.backfill_steps):
            recs, t = self.step()
            buf.extend(to_batches(recs, t))          # one gateway batch per step, stamped when it was sent
            n += len(recs)
            if n > 25_000 or k == self.backfill_steps - 1:
                self._post("/api/ingest/bulk", buf)
                buf, n = [], 0
                print(f"  {t:%Y-%m-%d %H:%M} ({k + 1}/{self.backfill_steps}) {time.time() - t0:.0f}s", end="\r")
        print(f"\nBackfill done in {time.time() - t0:.0f}s")

    def live(self) -> None:
        period = C.SAMPLE_PERIOD_S / self.speed
        print(f"Live: one 5-min step every {period:.1f}s (x{self.speed:g})")
        nxt = time.time()
        while True:
            recs, t = self.step()
            for b in to_batches(recs, t):
                self._post("/api/ingest", b)
            nxt += period
            time.sleep(max(0.0, nxt - time.time()))


class VisionThread(threading.Thread):
    """Camera edge node on the streamer's clock: one pass per revolution of *running* belt."""

    def __init__(self, streamer: Streamer, backfill_passes: int = 10):
        super().__init__(daemon=True)
        import random

        from PIL import Image

        from vision.edge_sim import Detector, EdgeNodeSim, default_defects, healthy_crops, image_pool
        from vision.loop import BeltRegister

        self.s = streamer
        reg = BeltRegister.default()
        pool, healthy = image_pool()
        healthy_imgs = [Image.open(p).convert("RGB") for p in healthy] + healthy_crops(pool, 40, random.Random(5))
        self.pass_s = reg.L * 1.0015 / C.BELT_SPEED_M_S
        self.edge = EdgeNodeSim(reg, default_defects(reg), healthy_imgs, Detector(), seed=7,
                                start_ts=streamer.start.timestamp(), n_healthy=16, n_dirt=3)
        self.backfill_passes = backfill_passes

    def _backfill_starts(self) -> list[float]:
        """Start times of the last N passes, placed on *running* time (no passes during stops)."""
        with self.s.lock:
            log = list(self.s.run_log)
        ts = np.array([t for t, _ in log])
        run = np.array([r for _, r in log], dtype=float) * C.SAMPLE_PERIOD_S
        cum = np.cumsum(run)
        starts = []
        for k in range(self.backfill_passes, 0, -1):
            target = cum[-1] - k * self.pass_s
            if target <= 0:
                continue
            starts.append(float(ts[np.searchsorted(cum, target)]))
        return starts

    def _send(self, rep: dict) -> None:
        rep = {k: v for k, v in rep.items() if not k.startswith("_")}
        for d in rep["detections"]:
            d.pop("_truth", None)
        r = self.s._post("/api/vision/pass", rep)
        print(f"  [vision] pass {rep['pass_id']}: {len(rep['detections'])} detections -> "
              f"confirmed {r.get('confirmed')}, resolved {r.get('resolved')}, dropped {r.get('dropped')}")

    def run(self) -> None:
        # camera history: the last N revolutions of *running* belt before the live phase
        for t0 in self._backfill_starts():
            self.edge.t = t0
            self._send(self.edge.run_pass())
        done_running = self.s.running_s
        while True:
            time.sleep(1.0)
            with self.s.lock:
                ran = self.s.running_s - done_running
                now = self.s.sim_now.timestamp()
            if ran >= self.pass_s:
                done_running += self.pass_s
                self.edge.t = now - self.pass_s
                self._send(self.edge.run_pass())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--backfill-days", type=float, default=8)
    ap.add_argument("--speed", type=float, default=120)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--backfill-only", action="store_true")
    a = ap.parse_args()
    s = Streamer(a.url, a.backfill_days, a.speed, a.seed, not a.no_vision)
    s.backfill()
    if not a.no_vision:
        v = VisionThread(s)
        v.start()
    if a.backfill_only:
        if not a.no_vision:
            while v.edge.pass_id < v.backfill_passes:
                time.sleep(1)
        return
    s.live()


if __name__ == "__main__":
    main()
