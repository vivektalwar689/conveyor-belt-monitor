"""Build the assets of the in-browser inspection page (docs/inspect/) and check its maths.

    python -m vision.export_web            # export both detectors to ONNX, copy the samples + their labels
    python -m vision.export_web --check    # verify the page's pre/post-processing against Ultralytics

The page runs the detector with ONNX Runtime Web, so it re-implements Ultralytics' pre- and post-processing in
JavaScript: letterbox to 512 with grey (114) padding, best class per anchor, per-class greedy NMS at IoU 0.5
(Ultralytics' predict default), rescale, clip. `--check` runs the same steps in numpy (`reference()` below mirrors
docs/inspect/inspect.js line by line) and compares them with Ultralytics' own predictions on the same ONNX file,
on every committed demo image, square and cropped to 4:3 so the padding path is exercised.

Needs `pip install onnx onnxslim onnxruntime` on top of requirements-vision.txt.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

from vision.edge_sim import CLASSES, DEMO_IMG

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "docs" / "inspect"
DETECTORS = {"deployed": ROOT / "models" / "belt_damage_yolo11n.pt",          # hard-negative fine-tuned
             "baseline": ROOT / "models" / "belt_damage_yolo11n_baseline.pt"}
IMGSZ, IOU = 512, 0.5
# held-out test scenes: tear x2, hole, puncture+tear, impact damage+tear, wear, and two defect-free belts
SAMPLES = ["train_0598.jpg", "valid_1982.jpg", "test_2264.jpg", "train_0293.jpg", "train_0087.jpg",
           "test_2261.jpg", "train_0560.jpg", "valid_1787.jpg"]


def export() -> None:
    from PIL import Image
    from ultralytics import YOLO

    (WEB / "models").mkdir(parents=True, exist_ok=True)
    for name, src in DETECTORS.items():
        out = YOLO(str(src)).export(format="onnx", imgsz=IMGSZ, opset=17, simplify=True, dynamic=False,
                                    verbose=False)
        dst = WEB / "models" / f"{name}.onnx"
        shutil.move(out, dst)
        print(f"{dst.relative_to(ROOT)}  {dst.stat().st_size / 1e6:.1f} MB")

    (WEB / "samples").mkdir(exist_ok=True)
    labels = {}
    for f in SAMPLES:
        img = DEMO_IMG / "images" / f
        shutil.copy(img, WEB / "samples" / f)
        W, H = Image.open(img).size
        boxes = []
        for line in (DEMO_IMG / "labels" / f"{img.stem}.txt").read_text().splitlines():
            if not line.strip():
                continue
            c, cx, cy, w, h = line.split()[:5]
            cx, cy, w, h = float(cx) * W, float(cy) * H, float(w) * W, float(h) * H
            boxes.append([CLASSES[int(c)], round(cx - w / 2, 1), round(cy - h / 2, 1),
                          round(cx + w / 2, 1), round(cy + h / 2, 1)])
        labels[f] = boxes
    (WEB / "samples" / "labels.json").write_text(json.dumps(labels, indent=1) + "\n")
    print(f"copied {len(SAMPLES)} samples and their labels to {(WEB / 'samples').relative_to(ROOT)}")


# ------------------------------------------------------------------ numpy mirror of inspect.js
def letterbox(bgr):
    import cv2

    h, w = bgr.shape[:2]
    r = min(IMGSZ / h, IMGSZ / w)
    nw, nh = round(w * r), round(h * r)
    dw, dh = (IMGSZ - nw) / 2, (IMGSZ - nh) / 2
    img = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR) if (w, h) != (nw, nh) else bgr
    top, left = round(dh - 0.1), round(dw - 0.1)
    img = cv2.copyMakeBorder(img, top, round(dh + 0.1), left, round(dw + 0.1), cv2.BORDER_CONSTANT,
                             value=(114, 114, 114))
    x = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(x), r, left, top


def _iou(a, b):
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def reference(out, conf, r, left, top, W, H):
    """Same steps as decode() in docs/inspect/inspect.js. Returns [(cls, conf, x0, y0, x1, y1)]."""
    o = out[0].T                                        # (anchors, 4 + classes)
    score, cls = o[:, 4:].max(1), o[:, 4:].argmax(1)
    keep = score >= conf
    o, score, cls = o[keep], score[keep], cls[keep]
    xyxy = np.column_stack([o[:, 0] - o[:, 2] / 2, o[:, 1] - o[:, 3] / 2, o[:, 0] + o[:, 2] / 2, o[:, 1] + o[:, 3] / 2])
    kept = []
    for i in np.argsort(-score, kind="stable"):
        if not any(cls[j] == cls[i] and _iou(xyxy[j], xyxy[i]) > IOU for j in kept):
            kept.append(i)
    res = []
    for i in kept:
        x0, y0, x1, y1 = xyxy[i]
        res.append((int(cls[i]), float(score[i]), float(np.clip((x0 - left) / r, 0, W)),
                    float(np.clip((y0 - top) / r, 0, H)), float(np.clip((x1 - left) / r, 0, W)),
                    float(np.clip((y1 - top) / r, 0, H))))
    return res


def check(conf: float = 0.25) -> bool:
    import cv2
    import onnxruntime as ort
    from ultralytics import YOLO

    ok = True
    for name in DETECTORS:
        path = WEB / "models" / f"{name}.onnx"
        sess = ort.InferenceSession(str(path))
        yolo = YOLO(str(path), task="detect")
        n = mism = 0
        worst_box = worst_conf = 0.0
        for p in sorted((DEMO_IMG / "images").glob("*.jpg")):
            full = cv2.imread(str(p))
            for crop in (full, full[full.shape[0] // 8: full.shape[0] * 7 // 8]):     # as is, and wider (padding)
                H, W = crop.shape[:2]
                x, r, left, top = letterbox(crop)
                ours = reference(sess.run(None, {sess.get_inputs()[0].name: x})[0], conf, r, left, top, W, H)
                ref = yolo.predict(crop, imgsz=IMGSZ, conf=conf, verbose=False)[0].boxes
                theirs = [(int(c), float(s), *map(float, b)) for c, s, b in
                          zip(ref.cls.numpy(), ref.conf.numpy(), ref.xyxy.numpy())]
                n += 1
                if sorted(d[0] for d in ours) != sorted(d[0] for d in theirs):
                    mism += 1
                    continue
                for t in theirs:
                    o = min(ours, key=lambda d: max(abs(a - b) for a, b in zip(d[2:], t[2:])))
                    worst_box = max(worst_box, max(abs(a - b) for a, b in zip(o[2:], t[2:])))
                    worst_conf = max(worst_conf, abs(o[1] - t[1]))
        print(f"{name}: {n} images, {mism} with a different set of detections; "
              f"worst box difference {worst_box:.3f} px, worst confidence difference {worst_conf:.5f}")
        ok &= mism == 0 and worst_box < 0.5 and worst_conf < 1e-3
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="only verify the page's maths against Ultralytics")
    a = ap.parse_args()
    if not a.check:
        export()
    raise SystemExit(0 if check() else 1)


if __name__ == "__main__":
    main()
