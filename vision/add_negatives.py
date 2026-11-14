"""Hard-negative mining: add defect-free crops to the TRAIN split.

The dataset has 2,312 images but only 27 healthy ones in train, while a belt
camera sees healthy belt ~99 % of the time. A detector that has (almost) never
seen healthy belt fires on it. Here we cut crops from *training* images in
regions that contain no labelled box (with a margin) and add them as background
images (empty label files). Val/test are untouched, so the comparison with the
baseline detector is on the same held-out scenes.

Caveat: an unlabelled defect inside a crop becomes a wrong negative. The margin
around labelled boxes and using crops (not whole images) limit this, but do not
eliminate it.

    python -m vision.add_negatives        # -> data/processed/belt_damage_clean_neg/
"""
from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "processed" / "belt_damage_clean"
DST = ROOT / "data" / "processed" / "belt_damage_clean_neg"
MARGIN = 0.04


def free_crop(boxes: list[list[float]], rng: random.Random, tries: int = 40) -> tuple[float, float, float] | None:
    for _ in range(tries):
        s = rng.uniform(0.3, 0.5)
        x0, y0 = rng.uniform(0, 1 - s), rng.uniform(0, 1 - s)
        if all(x0 + s < b[0] - b[2] / 2 - MARGIN or x0 > b[0] + b[2] / 2 + MARGIN or
               y0 + s < b[1] - b[3] / 2 - MARGIN or y0 > b[1] + b[3] / 2 + MARGIN for b in boxes):
            return x0, y0, s
    return None


def main(seed: int = 0) -> None:
    rng = random.Random(seed)
    if DST.exists():
        shutil.rmtree(DST)
    for split in ("train", "val", "test"):
        for sub in ("images", "labels"):
            (DST / split / sub).mkdir(parents=True)
            for f in (SRC / split / sub).iterdir():
                os.symlink(f.resolve() if sub == "images" else f.resolve(), DST / split / sub / f.name)
    n = 0
    for img in sorted((SRC / "train" / "images").iterdir()):
        lab = SRC / "train" / "labels" / f"{img.stem}.txt"
        boxes = [list(map(float, l.split()[1:])) for l in lab.read_text().splitlines() if l.strip()]
        c = free_crop(boxes, rng)
        if c is None:
            continue
        x0, y0, s = c
        im = Image.open(img).convert("RGB")
        W, H = im.size
        crop = im.crop((int(x0 * W), int(y0 * H), int((x0 + s) * W), int((y0 + s) * H))).resize((640, 640))
        name = f"neg_{img.stem}"
        crop.save(DST / "train" / "images" / f"{name}.jpg", quality=92)
        (DST / "train" / "labels" / f"{name}.txt").write_text("")
        n += 1
    (DST / "data.yaml").write_text((SRC / "data.yaml").read_text().replace(str(SRC), str(DST)))
    print(f"added {n} hard-negative crops to train -> {DST}")


if __name__ == "__main__":
    main()
