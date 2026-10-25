"""Object inventory, evidence contracts and read-only workbench presentation.

The planner owns identities; a segmentation miss never removes an object.
Images/depth remain observations, separate from the modeler's editable world.
"""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from world_build import file_hash
from world_model import WorldStore


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_STAGES = {"planning", "segmenting", "geometry", "modeling", "reviewing"}


def record(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]*$"}
FRAME = {"type": "integer", "minimum": 0}
BOX = {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
       "minItems": 4, "maxItems": 4}
OBSERVATION = record({"frame_index": FRAME, "box": BOX, "description": TEXT})
PLAN_SCHEMA = record({
    "schema_version": {"const": 1}, "source_manifest_sha256": TEXT, "summary": TEXT,
    "objects": {"type": "array", "minItems": 1, "items": record({
        "id": ID, "name": TEXT, "category": TEXT,
        "parent_id": {"anyOf": [ID, {"type": "null"}]},
        "kind": {"enum": ["group", "object", "part", "structure"]},
        "priority": {"type": "integer", "minimum": 1},
        "geometry_strategy": {"enum": ["fuse", "model", "group"]},
        "segmentation_prompt": TEXT,
        "observations": {"type": "array", "items": OBSERVATION},
        "uncertainty": TEXT,
        "acceptance_criteria": {"type": "array", "minItems": 1, "items": TEXT},
    })},
    "unexplained_regions": {"type": "array", "items": OBSERVATION},
    "excluded_objects": {"type": "array", "items": record({
        "id": ID, "reason": TEXT,
        "frame_indices": {"type": "array", "minItems": 1, "items": FRAME},
    })},
})


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    WorldStore._write(Path(path), value)


def project_file(project, relative):
    project = Path(project).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("planning evidence must use project-relative paths")
    path = (project / relative).resolve(strict=True)
    if not path.is_relative_to(project) or not path.is_file():
        raise ValueError("planning evidence must be a file in this project")
    return path


def observations(project):
    path = project_file(project, "observations/manifest.json")
    manifest = read_json(path)
    if not manifest["train"]:
        raise ValueError("known-camera prepared training observations are required")
    # Verify heldout bytes without exposing them to planner/model inference.
    for row in manifest["train"] + manifest.get("heldout", []):
        if file_hash(project_file(project, row["path"])) != row["sha256"]:
            raise ValueError("prepared reference image changed")
    return manifest


def validate_plan(project, plan, previous=None):
    Draft202012Validator(PLAN_SCHEMA).validate(plan)
    path = project_file(project, "observations/manifest.json")
    if plan["source_manifest_sha256"] != file_hash(path):
        raise ValueError("plan refers to different prepared observations")
    training = {row["index"] for row in read_json(path)["train"]}
    objects = {obj["id"]: obj for obj in plan["objects"]}
    excluded = {obj["id"] for obj in plan["excluded_objects"]}
    if len(objects) != len(plan["objects"]) or len(excluded) != len(plan["excluded_objects"]) or objects.keys() & excluded:
        raise ValueError("plan object IDs must be unique, including exclusions")
    for obj in objects.values():
        if (obj["kind"] == "group") != (obj["geometry_strategy"] == "group"):
            raise ValueError("only grouping nodes use the group geometry strategy")
        if obj["kind"] != "group" and not obj["observations"]:
            raise ValueError(f"object needs original-image evidence: {obj['id']}")
        if obj["geometry_strategy"] == "fuse" and len(obj["observations"]) < 2:
            raise ValueError("fusion needs at least two distinct reference views")
        ids = [row["frame_index"] for row in obj["observations"]]
        if len(ids) != len(set(ids)):
            raise ValueError("an object must not repeat an observation frame")
        seen, parent = {obj["id"]}, obj["parent_id"]
        while parent is not None:
            if parent not in objects or parent in seen:
                raise ValueError("plan hierarchy has a cycle or missing parent")
            seen.add(parent)
            parent = objects[parent]["parent_id"]
    rows = [row for obj in objects.values() for row in obj["observations"]] + plan["unexplained_regions"]
    for row in rows:
        x0, y0, x1, y1 = row["box"]
        if row["frame_index"] not in training or x0 >= x1 or y0 >= y1:
            raise ValueError("plan boxes must be nonempty and cite prepared training views")
    for obj in plan["excluded_objects"]:
        if len(obj["frame_indices"]) != len(set(obj["frame_indices"])) or not set(obj["frame_indices"]) <= training:
            raise ValueError("exclusions must cite original training evidence")
    if previous:
        known = {obj["id"] for obj in previous["objects"] + previous["excluded_objects"]}
        if not known <= objects.keys() | excluded:
            raise ValueError("previous objects cannot disappear without an evidenced exclusion")
    return plan


def validate_segments(project, plan_path, segments):
    plan = validate_plan(project, read_json(project_file(project, plan_path)))
    if segments["plan_sha256"] != file_hash(project_file(project, plan_path)):
        raise ValueError("segmentation belongs to another plan")
    if segments["observations_manifest_sha256"] != plan["source_manifest_sha256"]:
        raise ValueError("segmentation belongs to another set of images")
    expected = {obj["id"] for obj in plan["objects"]}
    rows = segments["objects"]
    planned = {obj["id"]: obj for obj in plan["objects"]}
    if {obj["id"] for obj in rows} != expected or len(rows) != len(expected):
        raise ValueError("segmentation must retain every planned object, including misses")
    training = {row["index"] for row in read_json(project_file(project, "observations/manifest.json"))["train"]}
    for obj in rows:
        if obj["status"] not in {"segmented", "missing", "uncertain", "not_required"}:
            raise ValueError("unknown segmentation status")
        ids = [row["frame_index"] for row in obj["observations"]]
        cited = {row["frame_index"] for row in planned[obj["id"]]["observations"]}
        if len(ids) != len(set(ids)) or not set(ids) <= training & cited:
            raise ValueError("segmentation observations must use distinct training frames")
        if (obj["status"] == "not_required") != (planned[obj["id"]]["kind"] == "group"):
            raise ValueError("only grouping nodes may skip segmentation")
        if obj["status"] == "missing" and ids:
            raise ValueError("missing segmentation cannot claim successful masks")
        if obj["status"] == "segmented" and not ids:
            raise ValueError("successful segmentation needs actual masks")
        for row in obj["observations"]:
            if file_hash(project_file(project, row["mask_path"])) != row["mask_sha256"]:
                raise ValueError("instance mask bytes changed")
    return segments


def validate_geometry(project, plan_path, segments_path, geometry):
    plan = validate_plan(project, read_json(project_file(project, plan_path)))
    validate_segments(project, plan_path, read_json(project_file(project, segments_path)))
    if (geometry["plan_sha256"] != file_hash(project_file(project, plan_path))
            or geometry["segments_sha256"] != file_hash(project_file(project, segments_path))):
        raise ValueError("geometry belongs to different object evidence")
    planned = {obj["id"]: obj for obj in plan["objects"]}
    rows = geometry["objects"]
    if {obj["id"] for obj in rows} != planned.keys() or len(rows) != len(planned):
        raise ValueError("geometry must retain every planned object, including misses")
    for obj in rows:
        if obj["status"] not in {"ready", "insufficient_evidence", "needs_revision", "not_required"}:
            raise ValueError("unknown geometry status")
        if obj["status"] == "not_required" and planned[obj["id"]]["geometry_strategy"] == "fuse":
            raise ValueError("fusion objects cannot silently skip geometry")
        if obj["status"] == "ready":
            for key in ("mesh_path", "texture_path"):
                if not obj.get(key) or obj[key] not in obj["files"] or obj["files"][obj[key]] != geometry["files"].get(obj[key]):
                    raise ValueError("ready geometry needs hash-bound independent mesh and texture")
    for name, digest in {**geometry.get("input_files", {}), **geometry["files"]}.items():
        if file_hash(project_file(project, name)) != digest:
            raise ValueError("geometry evidence bytes changed")
    return geometry


def unresolved_objects(plan, segments=None, geometry=None):
    masks = {obj["id"]: obj for obj in (segments or {}).get("objects", [])}
    meshes = {obj["id"]: obj for obj in (geometry or {}).get("objects", [])}
    result = []
    for obj in plan["objects"]:
        if obj["geometry_strategy"] == "group":
            continue
        mask, mesh = masks.get(obj["id"]), meshes.get(obj["id"])
        if mask and mask["status"] in {"missing", "uncertain"}:
            result.append({"object_id": obj["id"], "reason": mask.get("reason", "分割缺少可靠证据"),
                           "blocking": obj["geometry_strategy"] == "fuse",
                           "next_action": "补充视角/框提示，或由计划 Agent 明确改为参考建模" if obj["geometry_strategy"] == "fuse"
                           else "按原图独立建模；审核仍须检查此物体"})
        if mesh and obj["geometry_strategy"] == "fuse" and mesh["status"] != "ready":
            result.append({"object_id": obj["id"], "reason": mesh.get("reason", "缺少跨视角几何支持"),
                           "blocking": True, "next_action": "修正实例关联/掩码，或明确改为参考建模"})
    for row in plan["unexplained_regions"]:
        result.append({"object_id": "scene", "reason": row["description"], "blocking": True,
                       "next_action": f"检查原始帧 {row['frame_index']}，补充物体或记录排除证据"})
    return result


def candidate_coverage(plan, scene):
    """Grouping-only plan nodes do not require artificial geometry in the world."""
    planned = {obj["id"]: obj for obj in plan["objects"]}
    actual = {obj["id"]: obj for obj in scene["objects"]}
    missing, hierarchy = [], []
    for obj in planned.values():
        if obj["kind"] == "group" and obj["id"] not in actual:
            continue
        if obj["id"] not in actual:
            missing.append(obj["id"])
            continue
        parent = obj["parent_id"]
        while parent and planned[parent]["kind"] == "group":
            parent = planned[parent]["parent_id"]
        # A modeler may materialize grouping nodes; otherwise skip task-only groups.
        accepted_parents = {parent, obj["parent_id"]} & ({None} | actual.keys())
        if actual[obj["id"]]["parent_id"] not in accepted_parents:
            hierarchy.append(obj["id"])
    return {"passed": not missing and not hierarchy and not plan["unexplained_regions"],
            "missing_objects": missing, "hierarchy_mismatches": hierarchy,
            "unexplained_regions": len(plan["unexplained_regions"])}


def controller_running(project, pid, *script_names):
    process = Path(f"/proc/{pid}")
    try:
        command = (process / "cmdline").read_bytes().decode().split("\0")
        cwd = (process / "cwd").resolve(strict=True)
    except FileNotFoundError:
        return False
    scripts = {ROOT / "scripts" / name for name in script_names}
    return (str(Path(project).resolve()) in command
            and any((cwd / argument).resolve() in scripts for argument in command if argument.endswith(".py")))


def planning_status(project):
    path = Path(project) / "object_pipeline/status.json"
    if not path.is_file():
        return None
    status = read_json(path)
    if status["stage"] in ACTIVE_STAGES and status.get("pid"):
        if not controller_running(project, status["pid"], "run_reconstruction.py"):
            status.update(stage="interrupted", reason="物体重建控制进程已停止；证据和未解决任务已保留。")
    plan = read_json(project_file(project, status["plan_path"])) if status.get("plan_path") else None
    segments = read_json(project_file(project, status["segments_path"])) if status.get("segments_path") else None
    geometry = read_json(project_file(project, status["geometry_path"])) if status.get("geometry_path") else None
    masks = {obj["id"]: obj for obj in (segments or {}).get("objects", [])}
    meshes = {obj["id"]: obj for obj in (geometry or {}).get("objects", [])}
    status["objects"] = []
    for obj in (plan or {}).get("objects", []):
        mask = masks.get(obj["id"], {})
        frames = {row["frame_index"] for row in mask.get("observations", [])}
        status["objects"].append({"id": obj["id"], "name": obj["name"], "parent_id": obj["parent_id"],
                                  "kind": obj["kind"], "segmentation_status": mask.get("status", "pending"),
                                  "geometry_status": meshes.get(obj["id"], {}).get("status", "pending"),
                                  "notes": obj["uncertainty"],
                                  "evidence": [{"view_id": str(row["frame_index"]),
                                                "label": f"原始帧 {row['frame_index']}",
                                                "has_mask": row["frame_index"] in frames}
                                               for row in obj["observations"]]})
    status["unresolved"] = unresolved_objects(plan, segments, geometry) if plan else []
    return status


def planning_image(project, object_id, view_id, kind):
    from PIL import Image
    import io
    status = planning_status(project)
    if not status or not status.get("plan_path") or kind not in {"reference", "mask"}:
        raise ValueError("planning images are not available")
    plan = read_json(project_file(project, status["plan_path"]))
    obj = next((obj for obj in plan["objects"] if obj["id"] == object_id), None)
    if obj is None or view_id not in {str(row["frame_index"]) for row in obj["observations"]}:
        raise ValueError("unknown planned object or reference view")
    manifest_path = project_file(project, "observations/manifest.json")
    if file_hash(manifest_path) != plan["source_manifest_sha256"]:
        raise ValueError("prepared observations changed after planning")
    frame = next(row for row in read_json(manifest_path)["train"] if str(row["index"]) == view_id)
    path = project_file(project, frame["path"])
    if file_hash(path) != frame["sha256"]:
        raise ValueError("reference image bytes changed")
    if kind == "reference":
        return path
    segments = read_json(project_file(project, status["segments_path"]))
    if segments["plan_sha256"] != file_hash(project_file(project, status["plan_path"])):
        raise ValueError("mask belongs to a different plan")
    item = next(obj for obj in segments["objects"] if obj["id"] == object_id)
    row = next((row for row in item["observations"] if str(row["frame_index"]) == view_id), None)
    if row is None:
        raise ValueError("no instance mask for this view")
    mask_path = project_file(project, row["mask_path"])
    if file_hash(mask_path) != row["mask_sha256"]:
        raise ValueError("mask bytes changed")
    with Image.open(path) as source, Image.open(mask_path) as mask:
        image = source.convert("RGB")
        if mask.size != image.size:
            raise ValueError("mask and reference dimensions differ")
        alpha = mask.convert("L").point(lambda value: 100 if value else 0)
        image.paste(Image.new("RGB", image.size, (30, 210, 130)), mask=alpha)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
    return stream.getvalue()
