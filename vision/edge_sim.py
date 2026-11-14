"""Simulated camera edge node watching the return strand of the belt loop.

Each revolution ("pass") it:
* advances a biased odometer (+0.4 % encoder error, slowly wandering) over a belt
  that is 0.15 % longer than its installation record (stretch);
* reads the RFID tag in each splice as it passes (5 % of reads are missed);
* images the belt; occasionally a stretch is not imaged (fouled lens / washdown),
  which is reported as a coverage gap;
* runs the real detector on what the camera "sees": the *same* held-out test image
  for a given physical defect every revolution, re-rendered with different light,
  motion blur, noise, lateral wander; carry-back dirt on random healthy frames;
* reports detections (+ JPEG crops) in odometer metres - no belt coordinates, no
  ground truth. Localisation and association are the backend's job.

Simulation shortcut (documented in the README): the detector is run on the
defect frames plus a random sample of healthy and dirty frames per pass, not on
all ~12,000 one-metre frames, to keep CPU time per pass to seconds. The healthy
sample is used to measure the per-frame false-positive rate honestly.
"""
from __future__ import annotations

import base64
import io
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from simulator import config as C
from vision.loop import BeltRegister

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / "data" / "processed" / "belt_damage_clean"
DEMO_IMG = ROOT / "vision" / "demo_images"          # small committed subset so the demo runs without the dataset
CLASSES = ["Hole", "Impact Damage", "Puncture", "Tear", "Wear"]
FRAME_LEN_M = 1.0
BELT_W_M = C.BELT_WIDTH_MM / 1000
DETECTOR = ROOT / "models" / "belt_damage_yolo11n.pt"


@dataclass
class TrueDefect:
    id: int
    pos: float                  # true belt coordinate (register metres)
    lateral: float
    image: Path
    cls: str
    appear_pass: int = 0
    repair_pass: int | None = None

    def present(self, p: int) -> bool:
        return p >= self.appear_pass and (self.repair_pass is None or p < self.repair_pass)


def image_pool(split: str = "test") -> tuple[list[tuple[Path, str]], list[Path]]:
    """(defect images with their dominant class, healthy images) from the held-out split."""
    root = CLEAN / split if (CLEAN / split).exists() else DEMO_IMG
    defects, healthy = [], []
    for img in sorted((root / "images").glob("*.jpg")):
        lab = root / "labels" / f"{img.stem}.txt"
        lines = [l.split() for l in lab.read_text().splitlines() if l.strip()] if lab.exists() else []
        if not lines:
            healthy.append(img)
            continue
        cls = [int(l[0]) for l in lines]
        defects.append((img, CLASSES[max(set(cls), key=cls.count)]))
    return defects, healthy


def healthy_crops(defects: list[tuple[Path, str]], n: int, rng: random.Random) -> list[Image.Image]:
    """Healthy belt frames cut from regions of test images that contain no labelled box."""
    out = []
    tries = 0
    while len(out) < n and tries < n * 30:
        tries += 1
        img, _ = rng.choice(defects)
        lab = img.parent.parent / "labels" / f"{img.stem}.txt"
        boxes = [list(map(float, l.split()[1:])) for l in lab.read_text().splitlines() if l.strip()]
        s = rng.uniform(0.35, 0.5)
        x0, y0 = rng.uniform(0, 1 - s), rng.uniform(0, 1 - s)
        if any(not (x0 + s < b[0] - b[2] / 2 or x0 > b[0] + b[2] / 2 or y0 + s < b[1] - b[3] / 2 or y0 > b[1] + b[3] / 2)
               for b in boxes):
            continue
        im = Image.open(img).convert("RGB")
        W, H = im.size
        out.append(im.crop((int(x0 * W), int(y0 * H), int((x0 + s) * W), int((y0 + s) * H))).resize((640, 640)))
    return out


def render(im: Image.Image, rng: random.Random, harsh: bool = False) -> tuple[Image.Image, float]:
    """Per-pass appearance change. Returns (image, lateral shift as fraction of width)."""
    im = ImageEnhance.Brightness(im).enhance(rng.uniform(0.55, 1.3) if not harsh else rng.uniform(0.25, 0.5))
    im = ImageEnhance.Contrast(im).enhance(rng.uniform(0.8, 1.2))
    shift = rng.uniform(-0.03, 0.03)
    im = im.transform(im.size, Image.Transform.AFFINE, (1, 0, -shift * im.size[0], 0, 1, 0), fillcolor=(40, 40, 40))
    if rng.random() < 0.6:   # motion blur along the belt (vertical in image coordinates)
        k = rng.choice([3, 5])
        im = im.filter(ImageFilter.Kernel((k, k), [1 if i % k == k // 2 else 0 for i in range(k * k)], scale=k))
    arr = np.asarray(im).astype(np.float32)
    arr += np.random.default_rng(rng.randrange(1 << 30)).normal(0, rng.uniform(0, 7), arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)), shift


def add_dirt(im: Image.Image, rng: random.Random) -> Image.Image:
    """Carry-back lumps / mud streaks: transient, gone next revolution."""
    im = im.copy()
    d = ImageDraw.Draw(im, "RGBA")
    for _ in range(rng.randint(2, 6)):
        x, y = rng.uniform(0.1, 0.9) * 640, rng.uniform(0.1, 0.9) * 640
        rx, ry = rng.uniform(15, 70), rng.uniform(8, 40)
        c = (rng.randint(40, 90), rng.randint(30, 60), rng.randint(20, 40), rng.randint(170, 240))
        d.ellipse([x - rx, y - ry, x + rx, y + ry], fill=c)
    if rng.random() < 0.5:
        x = rng.uniform(0.2, 0.8) * 640
        d.line([(x, 0), (x + rng.uniform(-40, 40), 640)], fill=(60, 45, 30, 200), width=rng.randint(4, 12))
    return im


def jpeg_b64(im: Image.Image, box: tuple[float, float, float, float]) -> str:
    W, H = im.size
    x0, y0, x1, y1 = box
    pad = 0.15
    w, h = x1 - x0, y1 - y0
    crop = im.crop((max(0, x0 - pad * w), max(0, y0 - pad * h), min(W, x1 + pad * w), min(H, y1 + pad * h)))
    crop.thumbnail((160, 160))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


class Detector:
    def __init__(self, weights: Path = DETECTOR, conf: float = 0.25, imgsz: int = 512):
        from ultralytics import YOLO

        self.model = YOLO(str(weights))
        self.conf, self.imgsz = conf, imgsz

    def __call__(self, images: list[Image.Image]) -> list[list[dict]]:
        if not images:
            return []
        res = self.model.predict(images, imgsz=self.imgsz, conf=self.conf, device="cpu", verbose=False)
        out = []
        for r in res:
            dets = []
            for (x0, y0, x1, y1), c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
                dets.append({"box": (x0, y0, x1, y1), "conf": float(c), "cls": self.model.names[int(k)]})
            out.append(dets)
        return out


@dataclass
class EdgeNodeSim:
    reg: BeltRegister
    defects: list[TrueDefect]
    healthy: list[Image.Image]
    detector: Detector | None
    seed: int = 0
    odo_bias: float = 0.004
    stretch: float = 0.0015
    tag_miss: float = 0.05
    n_healthy: int = 24
    n_dirt: int = 3
    fouled_prob: float = 0.15
    start_ts: float = 0.0
    log: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.odo = 0.0
        self.t = self.start_ts
        self.pass_id = 0
        self.bias_walk = 0.0

    def run_pass(self, render_harsh: bool = False) -> dict:
        rng, reg = self.rng, self.reg
        L_true = reg.L * (1 + self.stretch)
        v = C.BELT_SPEED_M_S
        self.bias_walk = float(np.clip(self.bias_walk + rng.gauss(0, 0.0003), -0.002, 0.002))
        k = 1 + self.odo_bias + self.bias_walk           # odometer metres per true metre
        p = self.pass_id
        odo0, t0 = self.odo, self.t

        def odo_at(x_reg: float) -> float:              # x_reg: register coordinate within this pass
            return odo0 + x_reg * (1 + self.stretch) * k

        reads = [{"tag": i, "odo_m": round(odo_at(pos) + rng.gauss(0, 0.05), 3), "ts": t0 + pos * (1 + self.stretch) / v}
                 for i, pos in enumerate(reg.tag_pos) if rng.random() > self.tag_miss]
        # coverage: everything, unless a stretch was not imaged this pass
        gap = None
        if rng.random() < self.fouled_prob:
            a = rng.uniform(0, reg.L)
            gap = (a, a + rng.uniform(800, 2500))
        def imaged(x: float) -> bool:
            if gap is None:
                return True
            return not (gap[0] <= x < gap[1] or gap[0] <= x + reg.L < gap[1])
        ranges = [[odo0, odo_at(reg.L)]] if gap is None else (
            [[odo0, odo_at(gap[0])], [odo_at(min(gap[1], reg.L)), odo_at(reg.L)]] if gap[1] <= reg.L else
            [[odo_at(gap[1] - reg.L), odo_at(gap[0])]])
        frames: list[tuple[float, Image.Image, str, int | None]] = []   # (register pos, image, kind, defect id)
        for d in self.defects:
            if d.present(p) and imaged(d.pos):
                frames.append((d.pos, Image.open(d.image).convert("RGB"), "defect", d.id))
        for _ in range(self.n_healthy):
            x = rng.uniform(0, reg.L)
            if imaged(x):
                frames.append((x, rng.choice(self.healthy), "healthy", None))
        for _ in range(self.n_dirt):
            x = rng.uniform(0, reg.L)
            if imaged(x):
                frames.append((x, add_dirt(rng.choice(self.healthy), rng), "dirt", None))
        rendered = [render(im, rng, harsh=render_harsh) for _x, im, _k, _d in frames]
        results = self.detector([r[0] for r in rendered]) if self.detector else [[] for _ in frames]
        dets, frame_log = [], []
        for (x, _im, kind, did), (img, shift), res in zip(frames, rendered, results):
            frame_log.append({"pass": p, "kind": kind, "defect": did, "n_det": len(res),
                              "max_conf": max([r["conf"] for r in res], default=0.0)})
            for r in res:
                x0, y0, x1, y1 = r["box"]
                xc, yc = (x0 + x1) / 2 / 640, (y0 + y1) / 2 / 640
                along = x + (yc - 0.5) * FRAME_LEN_M
                dets.append({
                    "odo_m": round(odo_at(along % reg.L) if along >= 0 else odo_at(along + reg.L), 3),
                    "lateral_m": round((xc - 0.5 - shift) * BELT_W_M, 3), "cls": r["cls"], "conf": round(r["conf"], 3),
                    "length_m": round((y1 - y0) / 640 * FRAME_LEN_M, 3), "width_m": round((x1 - x0) / 640 * BELT_W_M, 3),
                    "ts": t0 + x * (1 + self.stretch) / v, "crop_jpeg_b64": jpeg_b64(img, r["box"]),
                    "_truth": {"kind": kind, "defect": did, "true_pos": along % reg.L, "pass": p},
                })
        self.log.extend(frame_log)
        self.odo = odo_at(reg.L)
        self.t = t0 + L_true / v
        self.pass_id += 1
        return {"pass_id": p, "ts_start": int(t0), "ts_end": int(self.t), "tag_reads": reads,
                "imaged_odo_ranges": ranges, "frames_imaged": int(reg.L / FRAME_LEN_M), "odo_end": self.odo,
                "detections": dets, "_gap": gap}


def default_defects(reg: BeltRegister, seed: int = 11, n: int = 10) -> list[TrueDefect]:
    rng = random.Random(seed)
    pool, _ = image_pool()
    by_cls: dict[str, list[Path]] = {}
    for img, c in pool:
        by_cls.setdefault(c, []).append(img)
    order = ["Tear", "Wear", "Puncture", "Hole", "Impact Damage", "Tear", "Wear", "Tear", "Puncture", "Hole"]
    out = []
    for i in range(n):
        cls = order[i % len(order)]
        img = rng.choice(by_cls.get(cls) or [p for p, _ in pool])
        out.append(TrueDefect(i + 1, rng.uniform(0, reg.L), rng.uniform(-0.5, 0.5), img, cls))
    # one new impact damage appears mid-run (rock through the loading chute), one defect is repaired
    img = rng.choice(by_cls.get("Impact Damage") or [p for p, _ in pool])
    out.append(TrueDefect(n + 1, rng.uniform(0, reg.L), 0.1, img, "Impact Damage", appear_pass=8))
    out[1].repair_pass = 14
    return out
