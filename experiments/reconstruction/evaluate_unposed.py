"""Evaluate frozen unposed scenes using training-camera-only Sim(3) alignment."""
from __future__ import annotations

from pathlib import Path
import time

import numpy as np
from PIL import Image

from common import REPO, command, digest, docker, read, write
from unposed import fit_similarity, transform_cameras


def aligned_references(private, frozen, output, width):
    truth = read(private / "input_cameras.json")
    estimate = read(frozen / "input_cameras.json")
    predicted = {row["index"]: row for row in estimate["frames"]}
    train = [row for row in truth["frames"] if row["index"] in truth["training_indices"]]
    a = np.asarray([row["camera_to_world"] for row in train])
    b = np.asarray([predicted[row["index"]]["camera_to_world"] for row in train])
    fit = fit_similarity(a[:, :3, 3], b[:, :3, 3])
    aligned = transform_cameras(a, fit)
    relative = aligned[:, :3, :3].transpose(0, 2, 1) @ b[:, :3, :3]
    angles = np.degrees(np.arccos(np.clip((np.trace(relative, axis1=1, axis2=2)-1)/2, -1, 1)))
    fit.update(training_frames=len(train), fit_source="48 training camera centers only; no scan or test fit",
               training_center_rmse_m=fit["camera_center_rmse_in_estimated_units"]/fit["scale"],
               training_rotation_mean_deg=float(angles.mean()), training_rotation_median_deg=float(np.median(angles)))
    write(output / "alignment.json", fit)
    tests = read(private / "transforms.json")["frames"]
    poses = transform_cameras([f["camera_to_world"] for f in tests], fit)
    reference_dir = output / "references"
    reference_dir.mkdir(exist_ok=True)
    views = []
    for frame, camera in zip(tests, poses):
        image = Image.open(private / frame["file_path"]).convert("RGB")
        height = round(width*image.height/image.width)
        path = reference_dir / f"test_{frame['source_index']}.png"
        image.resize((width, height), Image.Resampling.LANCZOS).save(path)
        k = dict(frame["intrinsics"])
        sx, sy = width/k["width"], height/k["height"]
        k.update(width=width, height=height, fx=k["fx"]*sx, fy=k["fy"]*sy, cx=k["cx"]*sx, cy=k["cy"]*sy)
        views.append({"id": path.stem, "camera_to_world": camera.tolist(), "intrinsics": k, "reference_image": str(path)})
    with np.load(private / "scan_reference/reference.npz", allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files}
    r, t, s = np.asarray(fit["rotation"]), np.asarray(fit["translation"]), fit["scale"]
    for key in ("points", "origins"):
        arrays[key] = s*(arrays[key] @ r.T)+t
    arrays["directions"] = arrays["directions"] @ r.T
    arrays["depths"] = arrays["depths"]*s
    np.savez_compressed(output / "aligned_reference.npz", **arrays)
    return reference_dir, views, fit


def evaluate(root, trial, checkpoint, runtime, protocol):
    frozen = trial / "checkpoints" / checkpoint
    output = root / "evaluation_results" / trial.name / checkpoint
    output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    manifest = read(frozen / "frozen.json")
    for name, sha in manifest["files"].items():
        if digest(frozen / name) != sha:
            raise ValueError("Frozen checkpoint changed: " + name)
    spec = read(trial / "spec.json")
    private = root / "evaluation_private" / spec["scene"]
    references, views, alignment = aligned_references(private, frozen, output, protocol["test_render_width"])
    targets = read(frozen / "edit_targets.json")
    if set(targets) != set(protocol["edit_targets"][spec["scene"]]):
        raise ValueError("Editing target declaration does not cover registered tasks")
    job = {"blend": str(frozen / "scene.blend"), "output": str(output), "views": views,
           "samples": protocol["test_render_samples"], "edit_targets": targets,
           "oidn_library": runtime["oidn_library"]}
    write(output / "job.json", job)
    command([runtime["blender"], "--background", "--factory-startup", "--python-exit-code", "1",
             "--python", str(Path(__file__).with_name("evaluate_blender.py")), "--", str(output / "job.json")],
            output / "blender.log", timeout=None)
    mounts = [(REPO, "ro"), (output, "rw")]
    command(docker(runtime, mounts, Path(__file__).with_name("evaluate_scan.py"),
                   ["--reference", output / "aligned_reference.npz", "--mesh", output / "geometry.ply",
                    "--scale", alignment["scale"], "--output", output / "geometry_metrics.json"]), output / "geometry.log", timeout=None)
    mounts += [(Path(runtime["torch_cache"]), "ro")]
    env = {"TORCH_HOME": runtime["torch_cache"]}
    if runtime.get("evaluation_packages"):
        mounts.append((Path(runtime["evaluation_packages"]), "ro"))
        env["PYTHONPATH"] = runtime["evaluation_packages"]
    command(docker(runtime, mounts, Path(__file__).with_name("image_metrics.py"),
                   ["--references", references, "--renders", output / "renders", "--output", output / "image_metrics.json"],
                   extra_env=env), output / "image.log", timeout=None)
    command(docker(runtime, mounts, Path(__file__).with_name("target_panels.py"),
                   ["--job", output / "job.json", "--references", references]), output / "target_panels.log", timeout=None)
    blender = read(output / "blender_metrics.json")
    edits = blender["edit_tasks"]
    result = {"trial": trial.name, "checkpoint": checkpoint, "spec": spec, "alignment": alignment,
              "image": read(output / "image_metrics.json"), "geometry": read(output / "geometry_metrics.json"),
              "edit_mechanical_success_rate": sum(x["passed"] for x in edits)/len(edits),
              "edit_unintended_change_rate": sum(bool(x.get("unintended_meshes")) for x in edits)/len(edits),
              "edit_semantic_verification": "pending_independent_visual_check",
              "objects": len(read(frozen / "scene.json")["objects"]), "triangles": blender["triangles"],
              "seconds": time.time()-started, "scene_hash": manifest["scene_hash"],
              "reference_sha256": digest(private / "scan_reference/reference.npz")}
    write(output / "complete.json", result)
    return result
