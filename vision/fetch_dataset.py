"""Fetch the public conveyor-belt damage dataset at a pinned commit.

Source: https://github.com/FernandoN23/Implementation-of-Object-Detection-for-Conveyor-Belt-Defect-Detection
(MIT licence; images aggregated by the author from Roboflow Universe and academic
sources, homogenised to 5 classes, 640x640, YOLO labels).

Only the ``Dataset/`` folder is checked out (sparse, blob-filtered clone), so the
download is ~200 MB instead of the full ~765 MB repository.

Usage:
    python -m vision.fetch_dataset            # clones into data/raw/belt_damage
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

REPO_URL = "https://github.com/FernandoN23/Implementation-of-Object-Detection-for-Conveyor-Belt-Defect-Detection"
PINNED_COMMIT = "b3e7f2e1880084724e0080247f679023020aeef7"  # 2026-09-14, "Update README.md"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = ROOT / "data" / "raw" / "belt_damage"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def fetch(dest: Path = DEFAULT_DEST) -> Path:
    dataset_dir = dest / "Dataset"
    if (dataset_dir / "data.yaml").exists():
        print(f"Dataset already present at {dataset_dir}")
        return dataset_dir
    dest.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=dest)
    run(["git", "remote", "add", "origin", REPO_URL], cwd=dest)
    run(["git", "sparse-checkout", "set", "--no-cone", "/Dataset/"], cwd=dest)
    run(["git", "fetch", "--depth", "1", "--filter=blob:none", "origin", PINNED_COMMIT], cwd=dest)
    run(["git", "-c", "advice.detachedHead=false", "checkout", PINNED_COMMIT], cwd=dest)
    return dataset_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = ap.parse_args()
    print(fetch(args.dest))
