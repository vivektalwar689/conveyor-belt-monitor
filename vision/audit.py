"""Audit the belt-damage dataset and build a leakage-free split.

Why this exists
---------------
The dataset was assembled from several public sources that *already contained
augmented copies* (flips, rotations, brightness changes, video frames of the
same scene). If near-identical images end up in both train and test, test
metrics measure memorisation, not generalisation. This script:

1. checks label sanity (class ids, box geometry, empty/missing label files);
2. finds exact duplicates (MD5), near-duplicates that survive flips/90-degree
   rotations (dihedral-invariant perceptual hash) and same-scene variants
   (cosine similarity of CNN embeddings, if ultralytics is installed);
3. merges them into duplicate *groups* and measures how much of the published
   valid/test split leaks from train;
4. re-splits by group (a group never straddles splits), stratified by the
   group's dominant class, and writes a YOLO dataset + manifest + report.

Usage:
    python -m vision.audit                 # full audit + re-split
    python -m vision.audit --explore       # also dump pair contact sheets per similarity band
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.fft import dctn

from vision.fetch_dataset import DEFAULT_DEST

ROOT = Path(__file__).resolve().parents[1]
SRC = DEFAULT_DEST / "Dataset"
OUT_DATA = ROOT / "data" / "processed" / "belt_damage_clean"
ORIG_DATA = ROOT / "data" / "processed" / "belt_damage_original"
MANIFEST = ROOT / "vision" / "artifacts" / "data_manifest.csv"
REPORT_JSON = ROOT / "vision" / "artifacts" / "audit_report.json"
DOC_IMG = ROOT / "docs" / "images"

CLASSES = ["Hole", "Impact Damage", "Puncture", "Tear", "Wear"]
SPLITS_SRC = ["train", "valid", "test"]
TARGET = {"train": 0.70, "val": 0.15, "test": 0.15}

# Similarity thresholds (chosen by visually inspecting pairs in each band with --explore;
# see docs/vision_audit.md).
PHASH_MAX_DIST = 8         # of 64 bits, min over the 8 dihedral transforms
EMBED_MIN_COS = 0.97       # cosine similarity of YOLO11n-cls global embeddings (same scene / video)


@dataclass
class Item:
    idx: int
    split: str
    stem: str
    img_path: Path
    lbl_path: Path
    boxes: list = field(default_factory=list)   # [cls, xc, yc, w, h]
    issues: list = field(default_factory=list)
    md5: str = ""

    @property
    def uid(self) -> str:
        return f"{self.split}/{self.stem}"

    @property
    def classes(self) -> set[int]:
        return {int(b[0]) for b in self.boxes}


# ----------------------------------------------------------------------------- labels
def parse_labels(item: Item) -> None:
    if not item.lbl_path.exists():
        item.issues.append("missing_label_file")
        return
    seen = set()
    for ln, line in enumerate(item.lbl_path.read_text().splitlines()):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            item.issues.append(f"malformed_line:{ln}")
            continue
        try:
            c = int(float(parts[0]))
            xc, yc, w, h = map(float, parts[1:])
        except ValueError:
            item.issues.append(f"non_numeric:{ln}")
            continue
        if not 0 <= c < len(CLASSES):
            item.issues.append(f"bad_class:{c}")
            continue
        if w <= 0 or h <= 0:
            item.issues.append("zero_area_box")
            continue
        if line.strip() in seen:
            item.issues.append("duplicate_box")
            continue
        seen.add(line.strip())
        x0, y0, x1, y1 = xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2
        if min(x0, y0) < -0.01 or max(x1, y1) > 1.01:
            item.issues.append("box_out_of_bounds_clipped")
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, 1), min(y1, 1)
            xc, yc, w, h = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
        if w * h < 1e-5:
            item.issues.append("tiny_box_dropped")
            continue
        item.boxes.append([c, xc, yc, w, h])


# ----------------------------------------------------------------------------- hashing
def _phash_bits(gray32: np.ndarray) -> int:
    coeffs = dctn(gray32.astype(np.float64), norm="ortho")[:8, :8].flatten()
    med = np.median(coeffs[1:])
    bits = coeffs > med
    bits[0] = False
    return int("".join("1" if b else "0" for b in bits), 2)


def dihedral_phashes(img: Image.Image) -> list[int]:
    g = np.asarray(img.convert("L").resize((32, 32), Image.Resampling.LANCZOS))
    out = []
    for k in range(4):
        r = np.rot90(g, k)
        out.append(_phash_bits(r))
        out.append(_phash_bits(np.fliplr(r)))
    return out


_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def popcount64(a: np.ndarray) -> np.ndarray:
    return _POP8[a.view(np.uint8).reshape(*a.shape, 8)].sum(-1)


def min_phash_dist(hashes: np.ndarray) -> np.ndarray:
    """hashes: (N, 8) uint64 of dihedral variants. Returns (N, N) min Hamming distance
    between image i (identity transform) and any transform of image j."""
    n = len(hashes)
    out = np.empty((n, n), dtype=np.uint8)
    for i in range(n):
        x = np.bitwise_xor(hashes[:, :], hashes[i, 0])
        out[i] = popcount64(x).min(axis=1)
    return np.minimum(out, out.T)


def cnn_embeddings(paths: list[Path]) -> np.ndarray | None:
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ultralytics not installed: skipping embedding-based near-duplicate search")
        return None
    model = YOLO(str(ROOT / "models" / "yolo11n-cls.pt") if (ROOT / "models" / "yolo11n-cls.pt").exists() else "yolo11n-cls.pt")
    embs = []
    batch = 64
    for i in range(0, len(paths), batch):
        e = model.embed([str(p) for p in paths[i:i + batch]], imgsz=224, verbose=False)
        embs.extend([t.detach().cpu().numpy().ravel() for t in e])
        print(f"  embedded {min(i + batch, len(paths))}/{len(paths)}", end="\r")
    print()
    m = np.stack(embs).astype(np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


# ----------------------------------------------------------------------------- grouping
class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def dominant_class(items: list[Item]) -> str:
    cnt = Counter(int(b[0]) for it in items for b in it.boxes)
    return CLASSES[cnt.most_common(1)[0][0]] if cnt else "Healthy"


def group_split(groups: dict[int, list[Item]], seed: int = 7) -> dict[int, str]:
    """Greedy multi-label stratification of whole groups.

    Each group is a vector of per-class box counts (+ healthy images + images).
    Groups are placed largest-first into the split whose *relative* need for the
    group's classes is largest, so every class ends up in every split in roughly
    70/15/15 proportion and no group straddles two splits.
    """
    rng = random.Random(seed)
    keys = CLASSES + ["Healthy", "_images"]

    def vec(its: list[Item]) -> np.ndarray:
        v = np.zeros(len(keys))
        for it in its:
            for b in it.boxes:
                v[int(b[0])] += 1
            v[-2] += not it.boxes
            v[-1] += 1
        return v

    gv = {g: vec(its) for g, its in groups.items()}
    total = sum(gv.values())
    filled = {s: np.zeros(len(keys)) for s in TARGET}
    order = list(groups)
    rng.shuffle(order)
    order.sort(key=lambda g: -gv[g][-1])
    assign: dict[int, str] = {}
    for g in order:
        v = gv[g]
        w = v / np.maximum(total, 1)                      # this group's share of each class
        def need(s: str) -> float:
            deficit = (TARGET[s] * total - filled[s]) / np.maximum(total, 1)
            return float((w * deficit).sum() / max(w.sum(), 1e-9))
        s = max(TARGET, key=need)
        assign[g] = s
        filled[s] += v
    return assign


# ----------------------------------------------------------------------------- outputs
def write_yolo(items: list[Item], split_of: dict[int, str], out: Path) -> None:
    for s in TARGET:
        (out / s / "images").mkdir(parents=True, exist_ok=True)
        (out / s / "labels").mkdir(parents=True, exist_ok=True)
    for it in items:
        s = split_of[it.idx]
        name = f"{it.split}_{it.stem}"
        dst_img = out / s / "images" / f"{name}{it.img_path.suffix}"
        if not dst_img.exists():
            os.symlink(it.img_path.resolve(), dst_img)
        (out / s / "labels" / f"{name}.txt").write_text(
            "".join(f"{int(b[0])} {b[1]:.6f} {b[2]:.6f} {b[3]:.6f} {b[4]:.6f}\n" for b in it.boxes))
    (out / "data.yaml").write_text(
        f"path: {out}\ntrain: train/images\nval: val/images\ntest: test/images\n"
        f"nc: {len(CLASSES)}\nnames: {json.dumps(CLASSES)}\n")


def pair_sheet(items: list[Item], pairs: list[tuple[int, int, str]], path: Path, title: str, n: int = 8,
               per_row: int = 2) -> None:
    """Grid of image pairs: [left | right] with uids underneath and the similarity above."""
    W, GAP, TXT = 200, 8, 16
    pairs = pairs[:n]
    if not pairs:
        return
    rows = (len(pairs) + per_row - 1) // per_row
    cell_w, cell_h = 2 * W + GAP, W + 2 * TXT
    sheet = Image.new("RGB", (per_row * cell_w + (per_row - 1) * 3 * GAP, rows * (cell_h + GAP) + 26), "white")
    d = ImageDraw.Draw(sheet)
    d.text((6, 6), title, fill="black")
    for k, (i, j, note) in enumerate(pairs):
        x0 = (k % per_row) * (cell_w + 3 * GAP)
        y0 = 26 + (k // per_row) * (cell_h + GAP)
        d.text((x0 + 2, y0), note, fill=(180, 0, 0))
        for c, idx in enumerate((i, j)):
            im = Image.open(items[idx].img_path).convert("RGB").resize((W, W))
            sheet.paste(im, (x0 + c * (W + GAP), y0 + TXT))
            d.text((x0 + c * (W + GAP) + 2, y0 + TXT + W + 2), items[idx].uid, fill="black")
    sheet.save(path, quality=88)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--explore", action="store_true", help="dump contact sheets per similarity band")
    ap.add_argument("--no-embed", action="store_true")
    args = ap.parse_args()

    # 1. index + label sanity
    items: list[Item] = []
    for split in SPLITS_SRC:
        for f in sorted(os.listdir(SRC / split / "images")):
            stem, ext = os.path.splitext(f)
            if ext.lower() not in {".jpg", ".jpeg", ".png"}:
                continue
            it = Item(len(items), split, stem, SRC / split / "images" / f, SRC / split / "labels" / f"{stem}.txt")
            parse_labels(it)
            items.append(it)
    print(f"{len(items)} images indexed")

    # 2. hashes
    hashes = np.zeros((len(items), 8), dtype=np.uint64)
    sizes = Counter()
    for it in items:
        raw = it.img_path.read_bytes()
        it.md5 = hashlib.md5(raw).hexdigest()
        try:
            with Image.open(it.img_path) as im:
                im.load()
                sizes[im.size] += 1
                hashes[it.idx] = np.array(dihedral_phashes(im), dtype=np.uint64)
        except Exception as e:  # corrupt file
            it.issues.append(f"unreadable:{type(e).__name__}")
    dist = min_phash_dist(hashes)
    np.fill_diagonal(dist, 255)

    cache = ROOT / "vision" / "artifacts" / "embeddings.npy"
    if args.no_embed:
        emb = None
    elif cache.exists() and np.load(cache).shape[0] == len(items):
        emb = np.load(cache)
    else:
        emb = cnn_embeddings([it.img_path for it in items])
        if emb is not None:
            np.save(cache, emb)
    cos = emb @ emb.T if emb is not None else None
    if cos is not None:
        np.fill_diagonal(cos, -1)

    # 3. groups
    uf = UnionFind(len(items))
    by_md5 = defaultdict(list)
    for it in items:
        by_md5[it.md5].append(it.idx)
    exact_dupe_sets = [v for v in by_md5.values() if len(v) > 1]
    for v in exact_dupe_sets:
        for k in v[1:]:
            uf.union(v[0], k)
    ii, jj = np.where(np.triu(dist <= PHASH_MAX_DIST, 1))
    for i, j in zip(ii, jj):
        uf.union(int(i), int(j))
    n_phash_pairs = len(ii)
    n_embed_pairs = 0
    if cos is not None:
        ei, ej = np.where(np.triu(cos >= EMBED_MIN_COS, 1))
        n_embed_pairs = len(ei)
        for i, j in zip(ei, ej):
            uf.union(int(i), int(j))
    groups: dict[int, list[Item]] = defaultdict(list)
    for it in items:
        groups[uf.find(it.idx)].append(it)

    # 4. leakage of the published split
    def leaks(split: str) -> int:
        n = 0
        for it in items:
            if it.split != split:
                continue
            if any(o.split == "train" for o in groups[uf.find(it.idx)] if o.idx != it.idx):
                n += 1
        return n
    n_split = Counter(it.split for it in items)
    leak = {s: {"images": n_split[s], "with_near_duplicate_in_train": leaks(s)} for s in ("valid", "test")}
    for s in leak:
        leak[s]["fraction"] = round(leak[s]["with_near_duplicate_in_train"] / leak[s]["images"], 3)

    # 5. re-split by group
    gsplit = group_split(groups)
    split_of = {it.idx: gsplit[uf.find(it.idx)] for it in items}
    gsizes = sorted((len(v) for v in groups.values()), reverse=True)

    # 6. outputs
    if OUT_DATA.exists():
        import shutil
        shutil.rmtree(OUT_DATA)
    write_yolo(items, split_of, OUT_DATA)
    # original split as a YOLO dataset too (for the leaky-vs-clean comparison)
    if ORIG_DATA.exists():
        import shutil
        shutil.rmtree(ORIG_DATA)
    write_yolo(items, {it.idx: {"train": "train", "valid": "val", "test": "test"}[it.split] for it in items}, ORIG_DATA)

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w") as fh:
        fh.write("uid,original_split,new_split,group_id,group_size,md5,n_boxes,classes,issues\n")
        for it in items:
            g = uf.find(it.idx)
            fh.write(f"{it.uid},{it.split},{split_of[it.idx]},{g},{len(groups[g])},{it.md5},{len(it.boxes)},"
                     f"{'|'.join(CLASSES[c] for c in sorted(it.classes)) or 'Healthy'},{'|'.join(it.issues)}\n")

    # Residual similarity after the re-split: nearest TRAIN neighbour of each TEST image
    residual = {}
    if cos is not None:
        tr = np.array([split_of[it.idx] == "train" for it in items])
        for s_ in ("val", "test"):
            m = np.array([split_of[it.idx] == s_ for it in items])
            nn = cos[np.ix_(m, tr)].max(axis=1)
            residual[s_] = {"median_max_cos_to_train": round(float(np.median(nn)), 3),
                            "frac_max_cos_ge_0.95": round(float((nn >= 0.95).mean()), 3),
                            "frac_max_cos_ge_0.97": round(float((nn >= 0.97).mean()), 3)}
        for s_, orig in (("valid", "valid"), ("test", "test")):
            m = np.array([it.split == orig for it in items])
            tro = np.array([it.split == "train" for it in items])
            nn = cos[np.ix_(m, tro)].max(axis=1)
            residual[f"published_{s_}"] = {"median_max_cos_to_train": round(float(np.median(nn)), 3),
                                           "frac_max_cos_ge_0.95": round(float((nn >= 0.95).mean()), 3),
                                           "frac_max_cos_ge_0.97": round(float((nn >= 0.97).mean()), 3)}

    # Cheap, model-free proof of inflation: a 1-nearest-neighbour "classifier" on scene
    # embeddings predicting each image's dominant damage class. If test images have
    # near-twins in train, 1-NN looks brilliant without learning anything about damage.
    knn = {}
    if emb is not None:
        lab = np.array([dominant_class([it]) for it in items])
        for name, tr_m, te_m in (
            ("published_split", np.array([it.split == "train" for it in items]), np.array([it.split == "test" for it in items])),
            ("clean_group_split", np.array([split_of[it.idx] == "train" for it in items]),
             np.array([split_of[it.idx] == "test" for it in items])),
        ):
            sim = emb[te_m] @ emb[tr_m].T
            pred = lab[tr_m][sim.argmax(axis=1)]
            knn[name] = round(float((pred == lab[te_m]).mean()), 3)

    issue_counts = Counter(i.split(":")[0] for it in items for i in it.issues)
    new_counts = {s: Counter() for s in TARGET}
    for it in items:
        s = split_of[it.idx]
        new_counts[s]["images"] += 1
        new_counts[s]["healthy_images"] += int(not it.boxes)
        for b in it.boxes:
            new_counts[s][CLASSES[int(b[0])]] += 1
    report = {
        "source": {"repo": "FernandoN23/Implementation-of-Object-Detection-for-Conveyor-Belt-Defect-Detection",
                   "commit": "b3e7f2e1880084724e0080247f679023020aeef7"},
        "n_images": len(items),
        "image_sizes": {f"{w}x{h}": c for (w, h), c in sizes.items()},
        "label_issues": dict(issue_counts),
        "exact_duplicate_sets": len(exact_dupe_sets),
        "exact_duplicate_images": sum(len(v) for v in exact_dupe_sets),
        "near_dup_pairs_phash": int(n_phash_pairs),
        "near_dup_pairs_embedding": int(n_embed_pairs),
        "thresholds": {"phash_max_dist": PHASH_MAX_DIST, "embed_min_cos": EMBED_MIN_COS if cos is not None else None},
        "n_groups": len(groups),
        "largest_groups": gsizes[:10],
        "singletons": sum(1 for s in gsizes if s == 1),
        "published_split_leakage": leak,
        "nearest_train_neighbour_similarity": residual,
        "one_nn_dominant_class_accuracy": knn,
        "new_split": {s: dict(c) for s, c in new_counts.items()},
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

    # Example leaked pairs for the README
    DOC_IMG.mkdir(parents=True, exist_ok=True)
    leaked_pairs = []
    for it in items:
        if it.split != "test":
            continue
        cands = [o for o in groups[uf.find(it.idx)] if o.split == "train"
                 and (dist[it.idx, o.idx] <= PHASH_MAX_DIST or (cos is not None and cos[it.idx, o.idx] >= EMBED_MIN_COS))]
        if cands:
            o = max(cands, key=lambda o: (cos[it.idx, o.idx] if cos is not None else -dist[it.idx, o.idx]))
            note = f"pHash d={dist[it.idx, o.idx]}" + (f" cos={cos[it.idx, o.idx]:.2f}" if cos is not None else "")
            leaked_pairs.append((it.idx, o.idx, note))
    random.Random(1).shuffle(leaked_pairs)
    pair_sheet(items, leaked_pairs, DOC_IMG / "vision_leakage_examples.jpg",
               "Published split: TEST image (left of each pair) vs its near-duplicate in TRAIN (right)", n=6, per_row=3)

    if args.explore:
        expl = ROOT / "vision" / "artifacts" / "explore"
        expl.mkdir(parents=True, exist_ok=True)
        rng = random.Random(0)
        for lo, hi in [(0, 4), (5, 8), (9, 12), (13, 16), (17, 20)]:
            ii, jj = np.where(np.triu((dist >= lo) & (dist <= hi), 1))
            sel = rng.sample(list(zip(ii, jj)), min(8, len(ii)))
            pair_sheet(items, [(int(i), int(j), f"d={dist[i, j]}") for i, j in sel], expl / f"phash_{lo:02d}_{hi:02d}.jpg",
                       f"pHash distance {lo}-{hi}: {len(ii)} pairs")
        if cos is not None:
            for lo, hi in [(0.97, 1.01), (0.95, 0.97), (0.93, 0.95), (0.91, 0.93), (0.89, 0.91), (0.86, 0.89)]:
                ii, jj = np.where(np.triu((cos >= lo) & (cos < hi) & (dist > PHASH_MAX_DIST), 1))
                sel = rng.sample(list(zip(ii, jj)), min(8, len(ii)))
                pair_sheet(items, [(int(i), int(j), f"cos={cos[i, j]:.3f}") for i, j in sel],
                           expl / f"cos_{lo:.2f}_{hi:.2f}.jpg", f"cosine {lo}-{hi} (pHash>{PHASH_MAX_DIST}): {len(ii)} pairs")


if __name__ == "__main__":
    main()
