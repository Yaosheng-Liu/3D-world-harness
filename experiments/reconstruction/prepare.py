"""Create a new RGB packet and private evaluation split from raw dataset images."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path
import sys
import time

import cv2
import numpy as np

from common import REPO, command, digest, docker, read, write

sys.path.insert(0, str(REPO / "scripts"))
import reconstruct_multiview as multiview


def prepare_scene(root, name, scene_id, protocol, runtime):
    shared = root / "shared" / name
    marker = shared / "prepared.json"
    if marker.exists():
        return read(marker)
    started = time.time()
    source = Path(runtime["data_root"]) / scene_id
    data = read(source / "transforms.json")
    count = len(data["frames"])
    radius = protocol["test_support_radius"]
    centers = np.linspace(radius + 4, count - radius - 5, protocol["test_centers"]).round().astype(int).tolist()
    reserved = sorted({j for i in centers for j in range(i-radius, i+radius+1)})
    allowed = [i for i in range(count) if i not in reserved]
    selected = [allowed[i] for i in np.linspace(0, len(allowed)-1, 54).round().astype(int)]
    if len(set(selected)) != 54:
        raise ValueError("Insufficient disjoint training and validation observations")
    validation_slots = set(np.linspace(4, 49, 6).round().astype(int))
    training = [value for index, value in enumerate(selected) if index not in validation_slots]
    validation = [value for index, value in enumerate(selected) if index in validation_slots]
    selected = training + validation
    packet = shared / "source"
    (packet / "images").mkdir(parents=True, exist_ok=True)
    frames = []
    for i in selected:
        frame = dict(data["frames"][i])
        destination = packet / "images" / f"{i:06d}.png"
        image = cv2.imread(str(source / frame["file_path"]))
        if image is None:
            raise ValueError(f"Missing source image {i}")
        cv2.imwrite(str(destination), image)
        frame.update(file_path=f"images/{i:06d}.png", source_index=i)
        frames.append(frame)
    write(packet / "transforms.json", {**data, "frames": frames})
    project = shared / "project"
    project.mkdir(exist_ok=True)
    split = {"scene_id": scene_id, "source_directory": str(packet),
             "source_transforms_sha256": digest(packet / "transforms.json"), "frame_count": 54,
             "train_indices": list(range(48)), "heldout_indices": list(range(48, 54)),
             "split_policy": "48 construction and 6 visible validation images; independent test windows omitted from packet.",
             "frames": [{"index": i, "role": "train" if i < 48 else "heldout", "path": f["file_path"],
                         "sha256": digest(packet / f["file_path"])} for i, f in enumerate(frames)]}
    write(project / "split_manifest.json", split)
    multiview.prepare(packet, project)
    # These calibrated validation views are the same across every condition.
    intr = {k: data[v] for k, v in {"width":"w", "height":"h", "fx":"fl_x", "fy":"fl_y", "cx":"cx", "cy":"cy"}.items()}
    views = [{"id": f"validation_{i}", "camera_to_world": frames[i]["transform_matrix"],
              "intrinsics": intr, "reference_image": f"observations/heldout/{i:06d}.png"} for i in range(48, 54)]
    write(shared / "views.json", views)
    private = root / "evaluation_private" / name
    (private / "images").mkdir(parents=True, exist_ok=True)
    rectify = read(project / "calibration_audit.json")["rectify"]
    k = np.array([[data["fl_x"], 0, data["cx"]], [0, data["fl_y"], data["cy"]], [0,0,1]])
    distortion = np.array([data.get(k, 0) for k in ("k1","k2","p1","p2","k3")])
    evaluation_frames = []
    for i in reserved:
        frame = dict(data["frames"][i])
        image = cv2.imread(str(source / frame["file_path"]))
        if rectify:
            image = cv2.undistort(image, k, distortion, None, k)
        cv2.imwrite(str(private / "images" / f"{i:06d}.png"), image)
        evaluation_frames.append({**frame, "file_path": f"images/{i:06d}.png", "source_index": i})
    camera = {**data, "frames": evaluation_frames, "camera_model": "PINHOLE"}
    for key in ("k1","k2","k3","k4","p1","p2"):
        camera[key] = 0.
    write(private / "transforms.json", camera)
    write(private / "split.json", {"test_centers": centers, "scene_id": scene_id,
                                   "train_indices": training, "validation_indices": validation})
    command([sys.executable, runtime["reference_builder"], "--scene-dir", str(private)],
            private / "reference.log")
    record = {"name": name, "scene_id": scene_id, "source_frames": count,
              "training_source_indices": training, "validation_source_indices": validation,
              "test_source_indices": centers, "reserved_source_indices": reserved,
              "camera_sha256": digest(packet / "transforms.json"),
              "observations_sha256": digest(project / "observations/manifest.json"),
              "reference_sha256": digest(private / "reference.npz"),
              "rectify": rectify, "seconds": time.time()-started}
    write(marker, record)
    return record


def initialize(root, name, protocol, runtime):
    shared = root / "shared" / name
    project, source = shared / "project", shared / "source"
    marker = shared / "initialization.json"
    if marker.exists():
        return read(marker)
    started = time.time()
    mounts = [(REPO, "ro"), (shared, "rw")]
    if not (project / "dense/inference.json").exists():
        elapsed = command(docker(runtime, mounts, REPO / "scripts/reconstruct_multiview.py",
            ["--scene", source, "--output", project, "--stage", "infer", "--model",
             "/opt/da3/checkpoints/DA3NESTED-GIANT-LARGE", "--resolution", protocol["da3_resolution"]], gpu=True),
            shared / "infer.log", timeout=3600)
        write(shared / "inference_time.json", {"seconds": elapsed})
    if not (project / "world/scene.json").exists():
        command(docker(runtime, mounts, REPO / "scripts/reconstruct_multiview.py",
                       ["--scene", source, "--output", project, "--stage", "fuse", "--observations-only"]), shared / "fuse.log")
    scene = read(project / "world/scene.json")
    scene["views"] = read(shared / "views.json")
    write(project / "world/scene.json", scene)
    record = {"seconds": time.time()-started, "inference": read(shared / "inference_time.json"),
              "scene_sha256": digest(project / "world/scene.json"),
              "depth_sha256": digest(project / "dense/depths.npz"), "fresh": True}
    write(marker, record)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--stage", choices=("inputs", "initialize"), default="inputs")
    args = parser.parse_args()
    runtime = read(args.runtime)
    args.root.mkdir(parents=True, exist_ok=True)
    protocol_file = args.root / "protocol.json"
    if not protocol_file.exists():
        shutil.copyfile(Path(__file__).with_name("protocol.json"), protocol_file)
    protocol = read(protocol_file)
    rows = []
    for name, sid in protocol["scenes"].items():
        row = (prepare_scene(args.root, name, sid, protocol, runtime) if args.stage == "inputs"
               else initialize(args.root, name, protocol, runtime))
        rows.append({"scene": name, **row})
        write(args.root / (args.stage + ".json"), rows)


if __name__ == "__main__":
    main()
