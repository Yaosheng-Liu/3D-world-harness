#!/usr/bin/env python3
"""SAM 3 instance masks for a scene plan; no object is dropped on a missed detection.

Image inference only. The following geometry stage checks cross-view identity;
the SAM score and a successful box match do not certify object existence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def project_file(project, relative):
    path = Path(relative)
    target = (project / path).resolve()
    if path.is_absolute() or ".." in path.parts or not target.is_relative_to(project):
        raise ValueError("Artifact path must stay within the project")
    return target


def read_inputs(project, plan_path):
    plan = json.loads(plan_path.read_text())
    manifest_path = project / "observations/manifest.json"
    if plan["source_manifest_sha256"] != digest(manifest_path):
        raise ValueError("Plan belongs to different prepared observations")
    manifest = json.loads(manifest_path.read_text())
    frames = {row["index"]: row for row in manifest["train"]}
    ids = set()
    for obj in plan["objects"]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", obj["id"]) or obj["id"] in ids:
            raise ValueError("Object IDs must be unique safe identifiers")
        ids.add(obj["id"])
        seen = set()
        for obs in obj["observations"]:
            index = obs["frame_index"]
            if index not in frames or index in seen:
                raise ValueError("Plan observations must reference distinct training frames")
            seen.add(index)
            box = np.asarray(obs["box"], dtype=float)
            if box.shape != (4,) or not np.isfinite(box).all() or not (
                    0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
                raise ValueError("Plan boxes must be normalized nonempty XYXY")
    for index in {obs["frame_index"] for obj in plan["objects"] for obs in obj["observations"]}:
        row = frames[index]
        if digest(project_file(project, row["path"])) != row["sha256"]:
            raise ValueError(f"Prepared image changed: {index}")
    return plan, frames, digest(manifest_path)


def mask_match(mask, box):
    """Return bbox IoU and foreground containment for one planned instance."""
    y, x = np.nonzero(mask)
    if len(x) < 32:
        return 0., 0.
    height, width = mask.shape
    actual = np.array([x.min(), y.min(), x.max() + 1, y.max() + 1], dtype=float)
    expected = np.asarray(box) * [width, height, width, height]
    overlap = np.maximum(0, np.minimum(actual[2:], expected[2:]) - np.maximum(actual[:2], expected[:2])).prod()
    union = (actual[2:] - actual[:2]).prod() + (expected[2:] - expected[:2]).prod() - overlap
    contained = ((x >= expected[0]) & (x < expected[2]) & (y >= expected[1]) & (y < expected[3])).mean()
    return float(overlap / union), float(contained)


def select_mask(masks, scores, box, *, threshold=.35):
    """Choose by localization, not score order; close alternatives stay ambiguous."""
    matches = []
    for index, (mask, score) in enumerate(zip(masks, scores)):
        iou, contained = mask_match(mask, box)
        if np.isfinite(score) and score >= threshold and iou >= .25 and contained >= .65:
            matches.append((iou, index))
    matches.sort(reverse=True)
    if not matches:
        return None, "no candidate satisfies the planned instance box"
    if len(matches) > 1 and matches[0][0] - matches[1][0] < .1:
        return None, "multiple candidates overlap the planned instance box"
    return matches[0][1], "selected by box overlap and foreground containment"


def segment_observation(processor, state, obj, obs, threshold):
    """Try text, then one bounded SAM 3 box refinement; inference errors propagate."""
    processor.reset_all_prompts(state)
    result = processor.set_text_prompt(prompt=obj["segmentation_prompt"], state=state)
    masks = result["masks"].squeeze(1).cpu().numpy().astype(bool)
    scores = result["scores"].float().cpu().numpy()
    index, reason = select_mask(masks, scores, obs["box"], threshold=threshold)
    attempts = [{"method": "text", "candidate_count": len(masks), "reason": reason}]
    if index is not None:
        return masks[index], float(scores[index]), "text", attempts
    box = np.asarray(obs["box"], dtype=np.float32) * np.tile(
        [state["original_width"], state["original_height"]], 2)
    masks, scores, _ = processor.model.predict_inst(
        state, box=box, multimask_output=False, normalize_coords=True)
    masks, scores = np.asarray(masks, dtype=bool), np.asarray(scores, dtype=float)
    index, reason = select_mask(masks, scores, obs["box"], threshold=threshold)
    attempts.append({"method": "box", "candidate_count": len(masks), "reason": reason})
    if index is None:
        return None, None, None, attempts
    return masks[index], float(scores[index]), "box", attempts


def save_overlay(image, mask, box, output):
    pixels = np.asarray(image).copy()
    pixels[mask] = (pixels[mask] * .55 + np.array([38, 206, 144]) * .45).astype(np.uint8)
    overlay = Image.fromarray(pixels)
    draw = ImageDraw.Draw(overlay)
    draw.rectangle(tuple(np.asarray(box) * [image.width, image.height, image.width, image.height]),
                   outline=(255, 192, 45), width=max(2, image.width // 350))
    overlay.thumbnail((1280, 1280))
    overlay.save(output)


def run(options):
    project = options.project.resolve(strict=True)
    plan_path, output = project_file(project, options.plan), project_file(project, options.output)
    plan_hash = digest(plan_path)
    plan, frames, observations_hash = read_inputs(project, plan_path)
    if output.exists():
        raise ValueError("Segmentation output already exists; use a new version directory")
    checkpoint = options.checkpoint.resolve(strict=True)
    import torch
    import sam3
    from sam3.model_builder import build_sam3_image_model
    from sam3.model.sam3_image_processor import Sam3Processor

    started = time.monotonic()
    model = build_sam3_image_model(checkpoint_path=str(checkpoint), load_from_HF=False,
                                  device="cuda", enable_inst_interactivity=True)
    processor = Sam3Processor(model, device="cuda", confidence_threshold=options.threshold)
    source = Path(sam3.__file__).resolve().parents[1]
    revision = subprocess.run(["git", "-c", f"safe.directory={source}", "-C", str(source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=False)
    output.mkdir(parents=True)
    rows = {obj["id"]: {"id": obj["id"], "status": "not_required" if obj["kind"] == "group" else "missing",
                        "observations": [], "attempts": [], "reason": ""} for obj in plan["objects"]}
    jobs = {}
    for obj in plan["objects"]:
        if obj["kind"] != "group":
            for obs in obj["observations"]:
                jobs.setdefault(obs["frame_index"], []).append((obj, obs))
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        for frame_index, tasks in sorted(jobs.items()):
            image = Image.open(project_file(project, frames[frame_index]["path"])).convert("RGB")
            state = processor.set_image(image)
            for obj, obs in tasks:
                mask, score, method, attempts = segment_observation(processor, state, obj, obs, options.threshold)
                record = rows[obj["id"]]
                record["attempts"].append({"frame_index": frame_index, "prompts": attempts,
                                           "status": "segmented" if mask is not None else "missing"})
                if mask is None:
                    print(json.dumps({"object_id": obj["id"], "frame_index": frame_index, "status": "missing"}), flush=True)
                    continue
                mask_path = output / "masks" / obj["id"] / f"{frame_index:06d}.png"
                mask_path.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(mask.astype(np.uint8) * 255).save(mask_path)
                overlay_path = mask_path.with_name(f"{frame_index:06d}-overlay.jpg")
                save_overlay(image, mask, obs["box"], overlay_path)
                record["observations"].append({"frame_index": frame_index, "mask_path": str(mask_path.relative_to(project)),
                    "mask_sha256": digest(mask_path), "score": score, "prompt_method": method,
                    "overlay_path": str(overlay_path.relative_to(project)), "image_sha256": frames[frame_index]["sha256"]})
                print(json.dumps({"object_id": obj["id"], "frame_index": frame_index,
                                  "status": "segmented", "method": method, "score": score}), flush=True)
            del state
    for obj in plan["objects"]:
        row = rows[obj["id"]]
        if obj["kind"] == "group":
            row["reason"] = "Hierarchy group; no independent visible surface requested"
        elif not row["observations"]:
            row["reason"] = "No usable masks; retain object for planning correction or reference-based modeling"
        elif len(row["observations"]) < len(obj["observations"]):
            row.update(status="uncertain", reason="Some planned observations could not be segmented; inspect attempts")
        else:
            row.update(status="segmented", reason="2D masks obtained; cross-view identity and semantic boundaries still require checking")
    if digest(plan_path) != plan_hash or digest(project / "observations/manifest.json") != observations_hash:
        raise ValueError("Plan or observation manifest changed during segmentation")
    read_inputs(project, plan_path)
    report = {"schema_version": 1, "plan_sha256": plan_hash, "observations_manifest_sha256": observations_hash,
        "created_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": time.monotonic() - started,
        "model": {"name": "SAM 3", "mode": "image_concept_and_instance", "checkpoint_sha256": digest(checkpoint),
                  "source_commit": revision.stdout.strip() if revision.returncode == 0 else None,
                  "torch_version": torch.__version__, "confidence_threshold": options.threshold},
        "objects": list(rows.values()),
        "limitations": ["Independent image masks; this is not video tracking or verified 3D identity.",
                        "Model scores and box overlap are not calibrated probabilities of correct object identity.",
                        "Objects with missed detections remain in this report and must not be silently omitted."]}
    (output / "segments.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"report": str((output / "segments.json").relative_to(project)),
                      "object_count": len(rows), "elapsed_seconds": report["elapsed_seconds"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=.35)
    options = parser.parse_args()
    if not 0 < options.threshold < 1:
        parser.error("threshold must be between zero and one")
    run(options)


if __name__ == "__main__":
    main()
