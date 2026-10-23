"""Bounded, read-only Cycles previews of the build currently on screen."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from jsonschema import Draft202012Validator

from core import APIError
from world_build import file_hash


WORKER = Path(__file__).resolve().parents[1] / "scripts" / "cycles_preview_blender.py"
RENDER_SLOT = threading.Lock()
NUMBER = {"type": "number"}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["scene_revision", "camera_to_world", "intrinsics", "near", "far", "time_sec"],
    "properties": {
        "scene_revision": {"type": "integer", "minimum": 1},
        "camera_to_world": {"type": "array", "minItems": 4, "maxItems": 4,
                            "items": {"type": "array", "minItems": 4, "maxItems": 4, "items": NUMBER}},
        "intrinsics": {"type": "object", "additionalProperties": False,
                       "required": ["width", "height", "fx", "fy", "cx", "cy"],
                       "properties": {**{key: {"type": "integer", "minimum": 16, "maximum": 1280}
                                         for key in ("width", "height")},
                                      **{key: {"type": "number", "exclusiveMinimum": 0} for key in ("fx", "fy")},
                                      "cx": NUMBER, "cy": NUMBER}},
        "near": {"type": "number", "exclusiveMinimum": 0},
        "far": {"type": "number", "exclusiveMinimum": 0},
        "time_sec": {"type": "number", "minimum": 0, "maximum": 600},
    },
}


def validate_request(payload):
    errors = list(Draft202012Validator(SCHEMA).iter_errors(payload))
    if errors:
        raise APIError(400, errors[0].message)
    try:
        json.dumps(payload, allow_nan=False)
    except ValueError as exc:
        raise APIError(400, "camera values must be finite") from exc
    m = payload["camera_to_world"]
    columns = [[m[row][col] for row in range(3)] for col in range(3)]
    determinant = sum(m[0][i] * (m[1][(i+1) % 3]*m[2][(i+2) % 3] - m[1][(i+2) % 3]*m[2][(i+1) % 3]) for i in range(3))
    if (any(abs(a-b) > 1e-5 for a, b in zip(m[3], [0, 0, 0, 1]))
            or abs(determinant-1) > 1e-4
            or any(abs(sum(a*b for a, b in zip(columns[i], columns[j])) - (i == j)) > 1e-4
                   for i in range(3) for j in range(3))
            or payload["far"] <= payload["near"]):
        raise APIError(400, "camera must be a rigid OpenGL pose with valid clipping planes")


def published_build(project, store):
    """Match the displayed GLB, not an unpublished world revision or newest build."""
    scene = store.scene()
    objects = scene["objects"]
    if len(objects) != 1:
        raise APIError(409, "Cycles requires a published world build")
    obj = objects[0]
    if (obj.get("type") != "model" or obj.get("position") != [0, 0, 0]
            or obj.get("rotation") != [0, 0, 0] or obj.get("size") != [1, 1, 1]):
        raise APIError(409, "Publish the world build before using Cycles")
    digest = obj.get("metadata", {}).get("sha256")
    for path in sorted((Path(project) / "world/receipts").glob("*/receipt.json")):
        record = json.loads(path.read_text())
        if digest and record.get("files", {}).get("scene.glb") == digest and "blend_path" in record:
            return scene, record
    raise APIError(409, "No matching Blender build for the displayed scene")


def render_preview(project, store, payload):
    validate_request(payload)
    project = Path(project).resolve()
    scene, record = published_build(project, store)
    if scene["revision"] != payload["scene_revision"]:
        raise APIError(409, "Scene changed; render the current view again")
    blend = Path(record["blend_path"]).resolve()
    if not blend.is_relative_to(project) or not blend.is_file():
        raise APIError(409, "Published Blender source is unavailable")
    if not RENDER_SLOT.acquire(blocking=False):
        raise APIError(429, "Cycles is busy; try again after the current preview")
    try:
        if file_hash(blend) != record["files"]["scene.blend"]:
            raise APIError(409, "Blender source changed after publication; rebuild and publish")
        with tempfile.TemporaryDirectory(prefix="cycles-preview-") as directory:
            root = Path(directory)
            job = {**payload, "blend_path": str(blend), "output_dir": directory}
            (root / "job.json").write_text(json.dumps(job, allow_nan=False))
            with (root / "render.log").open("w") as log:
                try:
                    result = subprocess.run([os.environ.get("SCENE_WORLD_BLENDER", "blender"),
                                             "--background", "--factory-startup", "--disable-autoexec",
                                             "--threads", "8", "--python-exit-code", "1", "--python", str(WORKER),
                                             "--", str(root / "job.json")], stdout=log, stderr=subprocess.STDOUT, timeout=90)
                except subprocess.TimeoutExpired as exc:
                    raise APIError(504, "Cycles preview exceeded 90 seconds") from exc
                except FileNotFoundError as exc:
                    raise APIError(503, "Blender is not installed on this workbench host") from exc
            if result.returncode:
                raise APIError(500, "Cycles failed: " + (root / "render.log").read_text()[-1500:])
            if store.scene()["revision"] != scene["revision"]:
                raise APIError(409, "Scene changed during rendering; preview discarded")
            report = json.loads((root / "report.json").read_text())
            return {**report, "scene_revision": scene["revision"], "build_id": record["id"],
                    "image": "data:image/png;base64," + base64.b64encode((root / "preview.png").read_bytes()).decode()}
    finally:
        RENDER_SLOT.release()
