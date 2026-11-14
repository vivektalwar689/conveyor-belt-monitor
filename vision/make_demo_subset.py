"""Copy a small subset of the held-out TEST split into vision/demo_images/ (committed),
so `make demo` works without downloading the full dataset. Images keep their original
MIT licence (see vision/demo_images/NOTICE.md)."""
from __future__ import annotations

import random
import shutil
from pathlib import Path

from vision.edge_sim import CLASSES, CLEAN, DEMO_IMG

PER_CLASS = 8


def main() -> None:
    src = CLEAN / "test"
    rng = random.Random(0)
    by_cls: dict[str, list[Path]] = {}
    healthy = []
    for img in sorted((src / "images").glob("*.jpg")):
        lines = [l.split() for l in (src / "labels" / f"{img.stem}.txt").read_text().splitlines() if l.strip()]
        if not lines:
            healthy.append(img)
            continue
        cls = [int(l[0]) for l in lines]
        by_cls.setdefault(CLASSES[max(set(cls), key=cls.count)], []).append(img)
    pick = healthy[:]
    for c, imgs in by_cls.items():
        pick += rng.sample(imgs, min(PER_CLASS, len(imgs)))
    if DEMO_IMG.exists():
        shutil.rmtree(DEMO_IMG)
    (DEMO_IMG / "images").mkdir(parents=True)
    (DEMO_IMG / "labels").mkdir(parents=True)
    for img in pick:
        shutil.copy(img.resolve(), DEMO_IMG / "images" / img.name)
        shutil.copy(src / "labels" / f"{img.stem}.txt", DEMO_IMG / "labels" / f"{img.stem}.txt")
    (DEMO_IMG / "NOTICE.md").write_text(
        "Images in this folder are a subset of the held-out test split of the dataset in\n"
        "https://github.com/FernandoN23/Implementation-of-Object-Detection-for-Conveyor-Belt-Defect-Detection\n"
        "(commit b3e7f2e1880084724e0080247f679023020aeef7), MIT licence, Copyright (c) 2025 Fernando Navarrete.\n"
        "That dataset aggregates public Roboflow Universe and academic conveyor-belt damage datasets.\n")
    print(f"copied {len(pick)} images ({len(healthy)} healthy) to {DEMO_IMG}")


if __name__ == "__main__":
    main()
