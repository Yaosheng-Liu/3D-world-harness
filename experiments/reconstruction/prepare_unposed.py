"""Freeze RGB keyframes, then estimate camera/depth evidence without released poses."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from common import REPO, digest, read, write


def proper_rotations(cameras):
    cameras = np.asarray(cameras, dtype=float).copy()
    u, _, vt = np.linalg.svd(cameras[:, :3, :3])
    rotation = u @ vt
    if np.any(np.linalg.det(rotation) <= 0):
        raise ValueError("Camera rotations must preserve handedness")
    cameras[:, :3, :3] = rotation
    cameras[:, 3] = [0, 0, 0, 1]
    return cameras


def normalize(cameras):
    cameras = proper_rotations(cameras)
    up = cameras[:, :3, 1].mean(0)
    up /= np.linalg.norm(up)
    right = cameras[0, :3, 0] - up * np.dot(cameras[0, :3, 0], up)
    right /= np.linalg.norm(right)
    rotation = np.stack([right, np.cross(up, right), up])
    origin = np.median(cameras[:, :3, 3], axis=0)
    scale = float(3 / np.median(np.linalg.norm(cameras[:, :3, 3] - origin, axis=1)))
    translation = -scale * rotation @ origin
    cameras[:, :3, :3] = rotation @ cameras[:, :3, :3]
    cameras[:, :3, 3] = scale * (cameras[:, :3, 3] @ rotation.T) + translation
    return cameras, {"scale": scale, "rotation": rotation.tolist(), "translation": translation.tolist()}


def prepare_rgb(root, name, scene_id, source_root):
    shared = root / "shared" / name
    marker = shared / "rgb_prepared.json"
    if marker.exists():
        return read(marker)
    source = source_root / "source_rgb" / scene_id / "iphone"
    files = sorted(source.glob("*.jpg"))
    if len(files) < 100:
        raise ValueError("Insufficient RGB images for this registered split")
    centers = np.linspace(8, len(files)-9, 12).round().astype(int).tolist()
    reserved = {j for i in centers for j in range(i-2, i+3)}
    allowed = [i for i in range(len(files)) if i not in reserved]
    selected = [allowed[i] for i in np.linspace(0, len(allowed)-1, 54).round().astype(int)]
    if len(set(selected)) != 54:
        raise ValueError("Cannot select 54 distinct visible frames")
    validation_slots = set(np.linspace(4, 49, 6).round().astype(int))
    selected = ([n for i, n in enumerate(selected) if i not in validation_slots]
                + [n for i, n in enumerate(selected) if i in validation_slots])
    project = shared / "rgb_project"
    rows = {"train": [], "heldout": []}
    for index, source_index in enumerate(selected):
        role = "train" if index < 48 else "heldout"
        path = project / "observations" / role / f"{index:06d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.open(files[source_index]).convert("RGB").resize((960, 720), Image.Resampling.LANCZOS)
        image.save(path)  # Decoded pixels only; EXIF and supplied calibration are omitted.
        rows[role].append({"index": index, "source_frame": files[source_index].stem,
                           "path": str(path.relative_to(project)), "sha256": digest(path)})
    write(project / "observations/manifest.json", rows)
    sheet = Image.new("RGB", (2560, 6*266), "#f3f4f6")
    draw = ImageDraw.Draw(sheet)
    for n, row in enumerate(rows["train"]):
        image = ImageOps.contain(Image.open(project / row["path"]), (312, 234))
        x, y = n % 8 * 320, n // 8 * 266
        sheet.paste(image, (x+4, y+25))
        draw.text((x+6, y+5), f"input_{row['index']}  {row['source_frame']}", fill="black")
    sheet.save(project / "training_contact_sheet.jpg", quality=90)

    # Released cameras and scans are consumed only by this private evaluation preparation.
    reference = source_root / "evaluation_private/scannetpp_scan_mirror/data" / scene_id
    released = read(reference / "iphone/pose_intrinsic_imu.json")
    private = root / "evaluation_private" / name
    (private / "images").mkdir(parents=True, exist_ok=True)
    def camera(index, original):
        frame = released[original.stem]
        c2w = proper_rotations(np.asarray(frame["aligned_pose"])[None])[0] @ np.diag([1, -1, -1, 1])
        k = np.asarray(frame["intrinsic"], dtype=float)
        return {"index": index, "source_index": int(original.stem.split("_")[-1]),
                "source_frame": original.stem, "camera_to_world": c2w.tolist(),
                "intrinsics": {"width": 1920, "height": 1440, "fx": float(k[0, 0]),
                               "fy": float(k[1, 1]), "cx": float(k[0, 2]), "cy": float(k[1, 2])}}
    visible = [camera(i, files[n]) for i, n in enumerate(selected)]
    write(private / "input_cameras.json", {"frames": visible, "training_indices": list(range(48))})
    tests = []
    for n in centers:
        frame = camera(n, files[n])
        destination = private / "images" / files[n].name
        shutil.copyfile(files[n], destination)
        tests.append({**frame, "file_path": "images/"+destination.name,
                      "transform_matrix": frame["camera_to_world"]})
    write(private / "transforms.json", {"frames": tests})
    write(private / "split.json", {"test_centers": [f["source_index"] for f in tests],
                                    "training_indices": list(range(48)), "validation_indices": list(range(48, 54))})
    (private / "scan.ply").symlink_to(reference / "scans/mesh_aligned_0.05_semantic.ply")
    record = {"scene_id": scene_id, "source_frames": len(files), "visible_frames": 54,
              "input_frames": [{"index": i, "source_frame": files[n].stem} for i, n in enumerate(selected)],
              "test_source_frames": [files[n].stem for n in centers], "test_exclusion_radius_in_available_frames": 2,
              "image_size": [960, 720], "released_camera_input": False,
              "rgb_manifest_sha256": digest(project / "observations/manifest.json")}
    write(marker, record)
    return record


def estimate(root, name, weights, resolution):
    import torch
    from depth_anything_3.api import DepthAnything3
    shared = root / "shared" / name
    rgb = shared / "rgb_project"
    observations = read(rgb / "observations/manifest.json")
    frames = observations["train"] + observations["heldout"]
    started = time.monotonic()
    torch.set_num_threads(8)
    model = DepthAnything3.from_pretrained(str(weights)).to("cuda")
    prediction = model.inference(image=[str(rgb / r["path"]) for r in frames], process_res=resolution,
                                 process_res_method="upper_bound_resize", use_ray_pose=True,
                                 ref_view_strategy="middle", infer_gs=False)
    ext = np.repeat(np.eye(4)[None], len(frames), axis=0)
    ext[:, :3] = prediction.extrinsics[:, :3]
    raw = np.linalg.inv(ext) @ np.diag([1, -1, -1, 1])
    cameras, normalization = normalize(raw)
    normalized_ext = np.linalg.inv(cameras @ np.diag([1, -1, -1, 1]))
    processed = np.asarray(prediction.processed_images, dtype=np.uint8)
    k = np.asarray(prediction.intrinsics, dtype=float)
    full_k = k.copy()
    full_k[:, 0] *= 960 / processed.shape[2]
    full_k[:, 1] *= 720 / processed.shape[1]
    median_k = np.median(full_k, axis=0)
    source = shared / "source"
    (source / "images").mkdir(parents=True, exist_ok=True)
    source_frames = []
    for row, c2w in zip(frames, cameras):
        path = source / "images" / f"{row['index']:06d}.png"
        shutil.copyfile(rgb / row["path"], path)
        source_frames.append({"file_path": "images/"+path.name,
                              "source_index": int(row["source_frame"].split("_")[-1]),
                              "transform_matrix": c2w.tolist()})
    data = {"w": 960, "h": 720, "fl_x": float(median_k[0, 0]), "fl_y": float(median_k[1, 1]),
            "cx": float(median_k[0, 2]), "cy": float(median_k[1, 2]), "camera_model": "PINHOLE", "frames": source_frames}
    write(source / "transforms.json", data)
    project = shared / "project"
    shutil.copytree(rgb / "observations", project / "observations", dirs_exist_ok=True)
    shutil.copyfile(rgb / "training_contact_sheet.jpg", project / "training_contact_sheet.jpg")
    split = {"scene_id": name, "source_directory": str(source), "frame_count": 54,
             "source_transforms_sha256": digest(source / "transforms.json"),
             "train_indices": list(range(48)), "heldout_indices": list(range(48, 54)),
             "frames": [{"index": r["index"], "role": "train" if r["index"]<48 else "heldout",
                         "path": source_frames[r["index"]]["file_path"], "sha256": r["sha256"]} for r in frames]}
    write(project / "split_manifest.json", split)
    write(project / "calibration_audit.json", {"rectify": False, "input_calibration": False,
          "note": "RGB pixels only; joint DA3 ray-pose and depth estimation on 54 visible keyframes."})
    prepared = {**observations, "split_manifest_sha256": digest(project / "split_manifest.json"),
                "calibration_audit_sha256": digest(project / "calibration_audit.json")}
    write(project / "observations/manifest.json", prepared)
    dense = project / "dense"
    dense.mkdir(exist_ok=True)
    np.savez_compressed(dense / "depths.npz", depth=np.asarray(prediction.depth[:48], dtype=np.float32)*normalization["scale"],
                        confidence=np.asarray(prediction.conf[:48], dtype=np.float32), intrinsics=k[:48],
                        extrinsics=normalized_ext[:48], images=processed[:48], frame_indices=np.arange(48))
    elapsed = time.monotonic()-started
    write(dense / "inference.json", {"prepared_manifest_sha256": digest(project / "observations/manifest.json"),
          "depths_sha256": digest(dense / "depths.npz"), "training_indices": list(range(48)),
          "input_frames": 54, "input_camera_parameters": False, "model": weights.name,
          "weights_sha256": digest(weights / "model.safetensors"), "config_sha256": digest(weights / "config.json"),
          "depth_shape": list(prediction.depth[:48].shape), "depth_convention": "OpenCV camera Z, not Euclidean ray distance",
          "resolution": resolution, "elapsed_seconds": elapsed, "normalization": normalization,
          "torch": torch.__version__, "peak_cuda_allocated_GiB": torch.cuda.max_memory_allocated()/1024**3})
    intr = {"width": 960, "height": 720, "fx": data["fl_x"], "fy": data["fl_y"], "cx": data["cx"], "cy": data["cy"]}
    write(shared / "views.json", [{"id": f"validation_{i}", "camera_to_world": cameras[i].tolist(),
          "intrinsics": intr, "reference_image": f"observations/heldout/{i:06d}.png"} for i in range(48, 54)])
    write(shared / "inference_time.json", {"seconds": elapsed})
    write(shared / "pose_estimation.json", {"method": "DA3 RGB-only ray-pose", "input_frames": 54,
          "input_parameters": None, "normalization": normalization, "raw_camera_to_world": raw.tolist(),
          "raw_intrinsics": full_k.tolist(), "source_cameras_sha256": digest(source / "transforms.json"), "seconds": elapsed})
    del model, prediction
    torch.cuda.empty_cache()
    print("POSE_READY", name, elapsed, flush=True)


def fuse(root, name):
    sys.path.insert(0, str(REPO / "scripts"))
    import reconstruct_multiview
    shared = root / "shared" / name
    project = shared / "project"
    reconstruct_multiview.fuse(shared / "source", project, observations_only=True)
    scene = read(project / "world/scene.json")
    scene["views"] = read(shared / "views.json")
    scene["provenance"]["description"] = "RGB-only DA3 camera/depth estimation and neutral TSDF observation evidence; no released calibration or scan inputs."
    write(project / "world/scene.json", scene)
    report = read(project / "reconstruction_report.json")
    report.update(camera_truth="RGB-only DA3 prediction; not released camera ground truth",
                  up_axis_note="Predicted camera-up normalized to +Z; arbitrary global scale.")
    write(project / "reconstruction_report.json", report)
    write(shared / "initialization.json", {"fresh": True, "pose": read(shared / "pose_estimation.json"),
          "scene_sha256": digest(project / "world/scene.json"), "depth_sha256": digest(project / "dense/depths.npz"),
          "fusion_seconds": report["timing"]["fresh_fusion_seconds"]})
    print("INITIALIZED", name, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scene")
    parser.add_argument("--stage", choices=("rgb", "infer", "fuse"), required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--weights", type=Path, default=Path("/opt/da3/checkpoints/DA3NESTED-GIANT-LARGE"))
    args = parser.parse_args()
    spec = read(args.root / "batch_spec.json")
    for name, scene_id in spec["scenes"].items():
        if args.scene and name != args.scene:
            continue
        if args.stage == "rgb":
            prepare_rgb(args.root, name, scene_id, args.source_root)
        elif args.stage == "infer":
            estimate(args.root, name, args.weights, spec["da3_resolution"])
        else:
            fuse(args.root, name)


if __name__ == "__main__":
    main()
