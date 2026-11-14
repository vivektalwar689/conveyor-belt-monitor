"""End-to-end evaluation of the continuous-loop handling, against ground truth.

    python -m vision.loop_eval --passes 30

Runs the simulated edge node (real detector on held-out images) for N belt
revolutions and feeds the same reports to three localisers:

    tags        re-zero + re-scale at every RFID-tagged splice   (the design)
    pass_start  dead reckoning from the first tag of each pass
    none        dead reckoning from the very first tag ever seen  (no fiducials)

and reports: localisation error, how many alerts a naive per-detection system
would raise vs. how many tracked defects we raise, precision/recall of confirmed
defects, time to confirm, whether transient dirt is rejected, whether the
repaired defect is retired, and the detector's per-frame false-positive rate on
healthy belt (which decides how the system scales to all ~12,000 frames/pass).
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
from PIL import Image

from vision.edge_sim import DETECTOR, Detector, EdgeNodeSim, default_defects, healthy_crops, image_pool
from vision.loop import BeltRegister, BeltTracker, Detection, Localizer, circ_dist

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "vision" / "artifacts"
DOC_IMG = ROOT / "docs" / "images"
MODES = ("tags", "pass_start", "none")


def run(passes: int, weights: Path, seed: int = 1) -> dict:
    reg = BeltRegister.default()
    defects = default_defects(reg)
    pool, healthy = image_pool()
    rng = random.Random(seed)
    healthy_imgs = [Image.open(p).convert("RGB") for p in healthy] + healthy_crops(pool, 80, rng)
    edge = EdgeNodeSim(reg, defects, healthy_imgs, Detector(weights), seed=seed, n_healthy=40, n_dirt=4)
    loc = {m: Localizer(reg, m) for m in MODES}
    trk = {m: BeltTracker(reg.L) for m in MODES}
    err = {m: [] for m in MODES}             # (pass, abs error) for detections on real defects
    raw = 0
    confirm_pass: dict[int, int] = {}
    for p in range(passes):
        rep = edge.run_pass()
        reads = [(r["tag"], r["odo_m"]) for r in rep["tag_reads"]]
        odos = np.array([d["odo_m"] for d in rep["detections"]])
        raw += len(rep["detections"])
        for m in MODES:
            pos = loc[m].map_pass(reads, odos) if len(odos) else np.array([])
            cov = []
            for a, b in rep["imaged_odo_ranges"]:
                pa, pb = loc[m].map_pass(reads, np.array([a, b]))
                cov.append((float(pa), float(pb)))
            dets = []
            for d, x in zip(rep["detections"], pos):
                dets.append(Detection(float(x), d["lateral_m"], d["cls"], d["conf"], d["length_m"], d["width_m"],
                                      d["ts"], p))
                if d["_truth"]["kind"] == "defect":
                    err[m].append((p, circ_dist(float(x), d["_truth"]["true_pos"], reg.L)))
            res = trk[m].process_pass(p, dets, cov)
        print(f"pass {p:2d}: {len(rep['detections']):3d} detections, tags read {len(reads)}/{len(reg.tag_pos)}, "
              f"gap {'yes' if rep['_gap'] else 'no '} | tags-tracker confirmed={sum(t.status == 'confirmed' for t in trk['tags'].tracks.values())}")

    # ---- per-frame detector behaviour
    log = edge.log
    def frame_rate(kind):
        fr = [f for f in log if f["kind"] == kind]
        return {"frames": len(fr), "with_detection": round(sum(f["n_det"] > 0 for f in fr) / max(len(fr), 1), 3)}
    frames = {k: frame_rate(k) for k in ("defect", "healthy", "dirt")}

    # ---- track quality vs. ground truth
    def evaluate(tr: BeltTracker) -> dict:
        tracks = list(tr.tracks.values())
        conf = [t for t in tracks if t.status in ("confirmed", "resolved")]
        should = [d for d in defects if d.appear_pass <= passes - 3]
        matched_def, dup, false_tracks, ttc = set(), 0, 0, []
        for t in conf:
            hits = [d for d in should if circ_dist(t.pos, d.pos, reg.L) <= 4.0 and abs(t.lateral - d.lateral) <= 0.6]
            if hits:
                d = hits[0]
                if d.id in matched_def:
                    dup += 1
                matched_def.add(d.id)
                ttc.append(sorted(set(t.passes_seen))[1] - d.appear_pass if len(set(t.passes_seen)) > 1 else None)
            else:
                false_tracks += 1
        repaired = [d for d in defects if d.repair_pass is not None and d.repair_pass <= passes - 4]
        retired = 0
        for d in repaired:
            near = [t for t in tracks if circ_dist(t.pos, d.pos, reg.L) <= 4.0]
            retired += any(t.status == "resolved" for t in near)
        return {
            "confirmed_tracks": len(conf), "true_defects": len(should),
            "recall": round(len(matched_def) / max(len(should), 1), 3),
            "precision": round((len(conf) - false_tracks) / max(len(conf), 1), 3),
            "duplicate_tracks": dup, "false_confirmed_tracks": false_tracks,
            "median_passes_to_confirm": float(np.median([x for x in ttc if x is not None])) if ttc else None,
            "dropped_unconfirmed": sum(t.status == "dropped" for t in tracks),
            "repaired_defects_retired": f"{retired}/{len(repaired)}",
        }

    out = {
        "passes": passes, "raw_detections": raw,
        "naive_alerts_per_true_defect": round(raw / max(len([d for d in defects]), 1), 1),
        "frames": frames,
        "localisation_error_m": {m: {"median": round(float(np.median([e for _p, e in err[m]])), 2),
                                     "p95": round(float(np.percentile([e for _p, e in err[m]], 95)), 2),
                                     "last_pass_median": round(float(np.median([e for p_, e in err[m] if p_ == passes - 1] or [np.nan])), 1)}
                                 for m in MODES if err[m]},
        "tracking": {m: evaluate(trk[m]) for m in MODES},
    }
    # Operating point: the loop lets the per-frame threshold favour precision, because
    # recall is recovered by seeing the same defect again on later revolutions.
    sweep = []
    gate_area = (2 * BeltTracker.GATE_POS_M) * (2 * BeltTracker.GATE_LAT_M)
    belt_area = reg.L * 1.6
    for c in (0.25, 0.35, 0.45, 0.55, 0.65, 0.75):
        dfr = [f for f in log if f["kind"] == "defect"]
        hfr = [f for f in log if f["kind"] == "healthy"]
        tpr = sum(f["max_conf"] >= c for f in dfr) / max(len(dfr), 1)
        fpr = sum(f["max_conf"] >= c for f in hfr) / max(len(hfr), 1)
        per_def = {}
        for f in dfr:
            per_def.setdefault(f["defect"], []).append(f["max_conf"] >= c)
        # passes until the 2nd sighting, per defect (None if never confirmed in the run)
        p2 = []
        for hits in per_def.values():
            idx = [i for i, h in enumerate(hits) if h]
            p2.append(idx[1] + 1 if len(idx) >= 2 else None)
        n_fp = fpr * int(reg.L)
        sweep.append({"conf": c, "frame_recall": round(tpr, 3), "frame_fp_rate": round(fpr, 4),
                      "defects_confirmed_in_run": f"{sum(x is not None for x in p2)}/{len(p2)}",
                      "median_passes_to_confirm": float(np.median([x for x in p2 if x is not None])) if any(p2) else None,
                      "false_detections_per_pass_full_coverage": round(n_fp),
                      "chance_false_confirmations_per_pass_k2": round(n_fp * n_fp * gate_area / belt_area, 3)})
    out["confidence_sweep"] = sweep
    # Scaling to every frame: expected false detections per revolution and chance coincidences
    fp = frames["healthy"]["with_detection"]
    n_frames = int(reg.L)
    n_fp = fp * n_frames
    out["full_coverage_extrapolation"] = {
        "frames_per_pass": n_frames, "false_detections_per_pass": round(n_fp),
        "chance_false_confirmations_per_pass_k2": round(n_fp * n_fp * gate_area / belt_area, 2),
        "chance_false_confirmations_per_pass_k3": round(n_fp ** 3 * (gate_area / belt_area) ** 2, 4),
    }
    _plot_error(err, passes)
    return out


def _plot_error(err: dict, passes: int) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ml.evaluate import INK, INK2, S1, S2, S3, SURF, _style

    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150, facecolor=SURF)
    _style(ax)
    labels = {"tags": "RFID splice tags (re-zero + re-scale per segment)", "pass_start": "Dead reckoning from pass start",
              "none": "Dead reckoning, no fiducials"}
    for m, col in zip(MODES, (S1, S2, S3)):
        if not err[m]:
            continue
        per = {}
        for p, e in err[m]:
            per.setdefault(p, []).append(e)
        xs = sorted(per)
        ys = [np.median(per[p]) for p in xs]
        ax.plot(xs, ys, color=col, lw=2, label=labels[m])
    ax.set_yscale("log")
    ax.set_xlabel("Belt revolution (camera pass)", color=INK2, fontsize=9)
    ax.set_ylabel("Median position error (m, log scale)", color=INK2, fontsize=9)
    ax.set_title("Where on the belt is the defect? Localisation error per pass", color=INK, fontsize=10, loc="left")
    ax.legend(frameon=False, fontsize=7.5, labelcolor=INK2, loc="upper left")
    fig.tight_layout()
    DOC_IMG.mkdir(parents=True, exist_ok=True)
    fig.savefig(DOC_IMG / "loop_localisation_error.png", facecolor=SURF)
    plt.close(fig)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=30)
    ap.add_argument("--weights", type=Path, default=DETECTOR)
    a = ap.parse_args()
    res = run(a.passes, a.weights)
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "loop_eval.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
