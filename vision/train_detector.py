"""Train the belt-damage detector (YOLO11n, CPU-friendly) and evaluate it.

    python -m vision.train_detector --split clean      # leakage-free group split (the real model)
    python -m vision.train_detector --split original   # published split, only to measure inflation

Why YOLO11n: the edge node must keep up with ~5 frames/s (1 m of belt per frame at
5 m/s) on modest hardware; the nano model runs at ~20-40 ms/frame on a laptop CPU,
and the dataset already ships YOLO-format boxes. Licence note: Ultralytics is
AGPL-3.0, fine for an assessment, but a product would need a licence or a swap to
an Apache-licensed detector (e.g. RT-DETR in another framework).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = {"clean": ROOT / "data/processed/belt_damage_clean/data.yaml",
        "clean_neg": ROOT / "data/processed/belt_damage_clean_neg/data.yaml",   # + mined hard negatives
        "original": ROOT / "data/processed/belt_damage_original/data.yaml"}
WEIGHTS_OUT = ROOT / "models"
METRICS_OUT = ROOT / "vision" / "artifacts"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=list(DATA), default="clean")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--imgsz", type=int, default=512)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--init", type=Path, default=None, help="start from these weights (e.g. stage-2 fine-tuning)")
    ap.add_argument("--lr0", type=float, default=0.01)
    ap.add_argument("--name", default=None)
    a = ap.parse_args()

    from ultralytics import YOLO

    base = ROOT / "models" / "yolo11n.pt"
    init = a.init or (base if base.exists() else Path("yolo11n.pt"))   # default: COCO-pretrained
    model = YOLO(str(init))
    run_name = a.name or f"det_{a.split}"
    model.train(data=str(DATA[a.split]), epochs=a.epochs, imgsz=a.imgsz, batch=a.batch, device="cpu",
                workers=4, patience=12, project=str(ROOT / "vision" / "runs"), name=run_name, exist_ok=True,
                seed=0, cache="ram", flipud=0.5, plots=True, verbose=False, lr0=a.lr0,
                warmup_epochs=0 if a.init else 3)
    best = ROOT / "vision" / "runs" / run_name / "weights" / "best.pt"
    trained = YOLO(str(best))
    res = trained.val(data=str(DATA[a.split]), split="test", imgsz=a.imgsz, device="cpu", plots=False, verbose=False)
    names = trained.names
    metrics = {
        "split": a.split, "epochs": a.epochs, "imgsz": a.imgsz,
        "test_mAP50": round(float(res.box.map50), 4),
        "test_mAP50_95": round(float(res.box.map), 4),
        "test_precision": round(float(res.box.mp), 4),
        "test_recall": round(float(res.box.mr), 4),
        "per_class_AP50": {names[int(c)]: round(float(ap50), 4) for c, ap50 in zip(res.box.ap_class_index, res.box.ap50)},
    }
    METRICS_OUT.mkdir(parents=True, exist_ok=True)
    metrics["init"] = str(a.init) if a.init else "COCO-pretrained yolo11n"
    (METRICS_OUT / f"detector_metrics_{run_name}.json").write_text(json.dumps(metrics, indent=2))
    if a.split in ("clean", "clean_neg"):
        shutil.copy(best, WEIGHTS_OUT / f"belt_damage_yolo11n_{run_name}.pt")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
