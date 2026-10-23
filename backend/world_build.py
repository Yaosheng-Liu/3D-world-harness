"""Build immutable Blender/GLB revisions from the editable world document."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import uuid
from pathlib import Path

from core import APIError, SceneStore
from world_model import WorldError, WorldStore, scene_hash, validate_scene


WORKER = Path(__file__).resolve().parents[1] / "scripts" / "world_blender.py"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_trajectory(trajectory: dict, scene: dict) -> None:
    """A playback must describe this exact revision and provide complete body poses."""
    if not isinstance(trajectory, dict) or any(trajectory.get(key) != value for key, value in {
        "scene_id": scene["scene_id"], "revision": scene["revision"], "scene_hash": scene_hash(scene)
    }.items()):
        raise WorldError("trajectory must match the world scene_id, revision and scene_hash")
    engine = trajectory.get("engine")
    if not isinstance(engine, dict) or any(not isinstance(engine.get(key), str) or not engine[key] for key in ("name", "version")):
        raise WorldError("trajectory must identify its physics engine name and version")
    times = trajectory.get("timestamps")
    if (not isinstance(times, list) or not 2 <= len(times) <= 36001
            or any(type(t) not in (int, float) or not math.isfinite(t) for t in times)
            or times[0] != 0 or times[-1] > 600
            or any(a >= b for a, b in zip(times, times[1:]))):
        raise WorldError("trajectory timestamps must start at zero and increase, up to 600 seconds")
    poses = trajectory.get("poses")
    if not isinstance(poses, dict) or set(poses) != {obj["id"] for obj in scene["objects"]}:
        raise WorldError("trajectory must provide world poses for every object")
    for object_id, frames in poses.items():
        if not isinstance(frames, list) or len(frames) != len(times):
            raise WorldError(f"trajectory frame count differs for {object_id}")
        for pose in frames:
            if not isinstance(pose, dict):
                raise WorldError(f"invalid trajectory pose for {object_id}")
            for field, length in (("position", 3), ("quaternion", 4)):
                values = pose.get(field)
                if (not isinstance(values, list) or len(values) != length
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
                    raise WorldError(f"invalid trajectory {field} for {object_id}")
            if abs(sum(v * v for v in pose["quaternion"]) - 1) > 1e-4:
                raise WorldError(f"trajectory quaternion must be normalized for {object_id}")


def inspect_glb(path: Path, expected_ids: set[str], parents: dict[str, str | None] | None = None) -> dict:
    """Reuse the publish gate, then check the stable object identity contract."""
    SceneStore._validate_glb_source(str(path))
    with path.open("rb") as handle:
        handle.seek(12)
        length, _ = struct.unpack("<I4s", handle.read(8))
        document = json.loads(handle.read(length))
        binary = handle.read()[8:] if document.get("animations") else b""
    nodes = document.get("nodes", [])
    ids = [node.get("extras", {}).get("stable_id") for node in nodes if "stable_id" in node.get("extras", {})]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise WorldError("GLB object identities differ from the world document")
    if any(node.get("extras", {}).get("world_role") == "collision" for node in nodes):
        raise WorldError("visual GLB contains collision geometry")
    if parents is not None:
        node_ids = {index: node.get("extras", {}).get("stable_id") for index, node in enumerate(nodes)}
        actual_parents = {node_ids[child]: node_ids[index]
                          for index, node in enumerate(nodes) for child in node.get("children", [])
                          if node_ids[child] in expected_ids}
        if any(actual_parents.get(object_id) != parent for object_id, parent in parents.items()):
            raise WorldError("GLB object hierarchy differs from the world document")
    preview = {"object_ids": sorted(ids), "animation_count": len(document.get("animations", [])),
               "node_count": len(nodes)}
    if document.get("animations"):
        times, animated_ids = [], set()
        for animation in document["animations"]:
            for sampler in animation["samplers"]:
                accessor = document["accessors"][sampler["input"]]
                view = document["bufferViews"][accessor["bufferView"]]
                start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
                times.extend(struct.unpack_from("<f", binary, start + index * view.get("byteStride", 4))[0]
                             for index in range(accessor["count"]))
            animated_ids.update(nodes[channel["target"]["node"]].get("extras", {}).get("stable_id")
                                for channel in animation["channels"])
        preview.update(animation_start_sec=min(times), animation_duration_sec=max(times) - min(times),
                       animated_object_ids=sorted(animated_ids - {None}))
    return preview


def build_world(project_dir: str | Path, *, blender: str = "blender", render: bool = True,
                trajectory: dict | None = None, scene: dict | None = None, timeout: float = 300) -> dict:
    """Run Blender as a bounded job; only a validated build receives a receipt.

    Passing a scene snapshot lets callers publish the exact revision they locked.
    Failed jobs retain their log in a separate build directory for diagnosis.
    """
    project = Path(project_dir).resolve()
    scene = validate_scene(scene if scene is not None else WorldStore(project).read(), project)
    if type(render) is not bool:
        raise WorldError("render must be boolean")
    executable = shutil.which(blender)
    if executable is None:
        raise WorldError(f"Blender executable not found: {blender}")
    if trajectory is not None:
        validate_trajectory(trajectory, scene)
    digest = scene_hash(scene)
    output = WorldStore(project).storage_path("builds", f"r{scene['revision']:04d}-{digest[:12]}-{uuid.uuid4().hex[:8]}")
    output.mkdir(parents=True)
    sources = {shape["path"]: file_hash(project / shape["path"])
               for obj in scene["objects"] for shape in obj["visual"] if shape["type"] == "mesh"}
    job = {"scene": scene, "scene_hash": digest, "project_dir": str(project), "output_dir": str(output),
           "render": render, "trajectory": trajectory, "asset_hashes": sources}
    job_path = output / "job.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    log_path = output / "blender.log"
    try:
        with log_path.open("w") as log:
            subprocess.run([executable, "--background", "--factory-startup", "--python-exit-code", "1",
                            "--python", str(WORKER), "--", str(job_path)],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)
        report_path = output / "geometry.json"
        report = json.loads(report_path.read_text())
        expected_ids = {obj["id"] for obj in scene["objects"]}
        if (report["scene_hash"] != digest or set(report["objects"]) != expected_ids
                or report["reopened_blend"] is not True):
            raise WorldError("Blender geometry report does not match its input revision")
        blend_path, glb_path = output / "scene.blend", output / "scene.glb"
        if not blend_path.is_file() or blend_path.stat().st_size == 0:
            raise WorldError("Blender did not produce an editable source file")
        preview = inspect_glb(glb_path, expected_ids, {obj["id"]: obj["parent_id"] for obj in scene["objects"]})
        if trajectory is not None and preview["animation_count"] != 1:
            raise WorldError("simulation GLB must contain one synchronized animation")
        if trajectory is not None and (abs(preview["animation_start_sec"]) > 1e-6
                or abs(preview["animation_duration_sec"] - trajectory["timestamps"][-1]) > 1 / 60 + 1e-6):
            raise WorldError("simulation GLB playback time differs from its source trajectory")
        if trajectory is not None:
            moving = {object_id for object_id, frames in trajectory["poses"].items() if any(
                max(abs(a-b) for a, b in zip(pose["position"], frames[0]["position"])) > 1e-6
                or abs(sum(a*b for a, b in zip(pose["quaternion"], frames[0]["quaternion"]))) < 1 - 1e-6
                for pose in frames[1:])}
            if not moving <= set(preview["animated_object_ids"]):
                raise WorldError("simulation GLB is missing an object motion channel")
        render_paths = [output / "renders" / f"{view['id']}.png" for view in scene["views"]] if render else []
        from PIL import Image
        for path in render_paths:
            with Image.open(path) as image:
                image.verify()
        if any(file_hash(project / name) != value for name, value in sources.items()):
            raise WorldError("a source asset changed during the build")
        artifacts = [blend_path, glb_path, report_path, *render_paths]
        receipt = {"revision": scene["revision"], "scene_hash": digest, "build_dir": str(output),
                   "blend_path": str(blend_path), "glb_path": str(glb_path), "report_path": str(report_path),
                   "render_paths": [str(path) for path in render_paths], "object_ids": sorted(expected_ids),
                   "source": scene["provenance"], "asset_hashes": sources, "preview": preview,
                   "files": {str(path.relative_to(output)): file_hash(path) for path in artifacts}}
        if trajectory is not None:
            receipt["animation"] = {"engine": trajectory["engine"], "frames": len(trajectory["timestamps"]),
                                    "duration_sec": preview["animation_duration_sec"],
                                    "source_duration_sec": trajectory["timestamps"][-1], "kind": "physics_playback"}
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, APIError) as exc:
        raise WorldError(f"World build failed; see {log_path}: {exc}") from exc
