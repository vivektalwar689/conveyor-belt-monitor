"""Qualitative figure: held-out test images with ground truth vs. detector predictions.

    python -m vision.plot_predictions      # -> docs/images/detector_examples.jpg
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

from vision.edge_sim import CLASSES, CLEAN, DETECTOR, Detector

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "detector_examples.jpg"
GT, PRED = (11, 163, 12), (208, 59, 59)     # status green for truth, critical red for predictions


def main(n: int = 10, seed: int = 4) -> None:
    root = CLEAN / "test"
    imgs = sorted((root / "images").glob("*.jpg"))
    rng = random.Random(seed)
    pick = rng.sample(imgs, n)
    det = Detector(DETECTOR, conf=0.35)
    preds = det([Image.open(p).convert("RGB") for p in pick])
    W = 300
    cols = 5
    sheet = Image.new("RGB", (cols * W, ((n + cols - 1) // cols) * (W + 34) + 28), "white")
    d = ImageDraw.Draw(sheet)
    d.text((6, 6), "Held-out test scenes. Green = ground truth, red = detector (conf >= 0.35)", fill="black")
    for i, (p, pr) in enumerate(zip(pick, preds)):
        im = Image.open(p).convert("RGB")
        dr = ImageDraw.Draw(im)
        lab = root / "labels" / f"{p.stem}.txt"
        gts = [l.split() for l in lab.read_text().splitlines() if l.strip()]
        for c, x, y, w, h in gts:
            x, y, w, h = (float(v) * 640 for v in (x, y, w, h))
            dr.rectangle([x - w / 2, y - h / 2, x + w / 2, y + h / 2], outline=GT, width=6)
        for r in pr:
            dr.rectangle(r["box"], outline=PRED, width=3)
            dr.text((r["box"][0] + 4, r["box"][1] + 4), f"{r['cls']} {r['conf']:.2f}", fill=PRED)
        im = im.resize((W, W))
        x0, y0 = (i % cols) * W, 28 + (i // cols) * (W + 34)
        sheet.paste(im, (x0, y0))
        truth = ", ".join(sorted({CLASSES[int(g[0])] for g in gts})) or "healthy"
        d.text((x0 + 4, y0 + W + 3), f"truth: {truth}", fill="black")
        d.text((x0 + 4, y0 + W + 17), f"pred: {', '.join(sorted({r['cls'] for r in pr})) or 'none'}", fill=(120, 30, 30))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT, quality=85)
    print(OUT)


if __name__ == "__main__":
    main()
