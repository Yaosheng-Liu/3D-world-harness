"""Frozen candidate evidence and the independent visual-review acceptance gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from jsonschema import Draft202012Validator

from world_build import file_hash
from world_model import WorldStore, scene_hash, source_paths, validate_scene


ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS = ("appearance", "structure", "editability")


def record(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": TEXT}
REVIEW_SCHEMA = record({
    "candidate_id": TEXT,
    "verdict": {"enum": ["accepted", "revise", "blocked"]},
    "summary": TEXT,
    "dimensions": record({key: {"enum": ["passed", "failed", "unknown"]} for key in DIMENSIONS}),
    "reviewed_views": STRINGS,
    "checked_objects": STRINGS,
    "resolved_issue_ids": STRINGS,
    "issues": {"type": "array", "items": record({
        "id": TEXT, "object_id": TEXT, "view_id": TEXT,
        "region": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                   "minItems": 4, "maxItems": 4},
        "severity": {"enum": ["blocking", "major", "minor"]},
        "evidence": TEXT, "acceptance_criterion": TEXT,
    })},
})


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    WorldStore._write(Path(path), value)


def project_file(project, value):
    path = (project / value).resolve(strict=True)
    if not path.is_relative_to(project) or not path.is_file():
        raise ValueError("review evidence must be a file in this project")
    return path


def verify_files(project, files):
    for name, digest in files.items():
        if file_hash(project_file(project, name)) != digest:
            raise ValueError(f"candidate evidence changed after freezing: {name}")


def freeze_candidate(project, candidate_path, receipt_path, output, *, blender="blender", previous_review=None):
    """Verify builder output, then independently exercise the reopened Blender file."""
    project, output = Path(project).resolve(), Path(output).resolve()
    if not output.is_relative_to(project):
        raise ValueError("review output must be inside this project")
    output.mkdir(parents=True, exist_ok=False)
    scene = validate_scene(read_json(project_file(project, candidate_path)), project)
    if any("reference_image" not in view for view in scene["views"]):
        raise ValueError("real-scene review requires calibrated reference images for every view")
    receipt = read_json(project_file(project, receipt_path))
    if receipt["scene_hash"] != scene_hash(scene) or receipt["revision"] != scene["revision"]:
        raise ValueError("build receipt belongs to another candidate")
    if set(receipt["asset_hashes"]) != set(source_paths(scene)):
        raise ValueError("build receipt does not bind all scene sources")
    files = dict(receipt["asset_hashes"])
    build_dir = Path(receipt["build_dir"])
    for name, digest in receipt["files"].items():
        path = project_file(project, build_dir / name)
        files[str(path.relative_to(project))] = digest
    required = [receipt["blend_path"], receipt["glb_path"], receipt["report_path"], *receipt["render_paths"]]
    if any(str(project_file(project, name).relative_to(project)) not in files for name in required):
        raise ValueError("receipt is missing required artifact hashes")
    if len(receipt["render_paths"]) != len(scene["views"]):
        raise ValueError("all fixed review views must be rendered")
    verify_files(project, files)
    write_json(output / "scene.json", scene)
    write_json(output / "build.json", receipt)
    job = {"blend_path": receipt["blend_path"], "scene": scene, "output_dir": str(output)}
    write_json(output / "edit_job.json", job)
    with (output / "edit_check.log").open("w") as log:
        subprocess.run([blender, "--background", "--factory-startup", "--python-exit-code", "1",
                        "--python", str(ROOT / "scripts/review_editability_blender.py"),
                        "--", str(output / "edit_job.json")], check=True, timeout=300,
                       stdout=log, stderr=subprocess.STDOUT)
    edit_check = read_json(output / "editability.json")
    from world_evaluation import evaluate_views, write_view_inspection
    metrics = evaluate_views(project, scene, receipt)
    write_json(output / "metrics.json", metrics)
    inspection = write_view_inspection(project, scene, receipt)
    write_json(output / "inspection.json", inspection)
    # Hash every generated check, including the exact comparisons the critic sees.
    for path in [output / name for name in ("scene.json", "build.json", "editability.json", "metrics.json", "inspection.json")]:
        files[str(path.relative_to(project))] = file_hash(path)
    for path in output.glob("*.png"):
        files[str(path.relative_to(project))] = file_hash(path)
    for path in (build_dir / "evaluation").glob("*.png"):
        files[str(path.relative_to(project))] = file_hash(path)
    previous_path = None
    if previous_review:
        previous_path = str((output / "previous_review.json").relative_to(project))
        write_json(output / "previous_review.json", read_json(project_file(project, previous_review)))
        files[previous_path] = file_hash(output / "previous_review.json")
    verify_files(project, files)
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = {"candidate_id": identity, "scene_hash": scene_hash(scene),
                "scene_path": str((output / "scene.json").relative_to(project)),
                "receipt_path": str((output / "build.json").relative_to(project)),
                "files": files, "view_ids": [view["id"] for view in scene["views"]],
                "object_ids": [obj["id"] for obj in scene["objects"]],
                "checks": {"editability": edit_check["passed"]}, "physics": "deferred",
                "previous_review_path": previous_path}
    write_json(output / "candidate.json", manifest)
    return manifest


def validate_review(manifest, review, previous=None):
    """Reject unbound, incomplete, or self-contradictory approvals."""
    Draft202012Validator(REVIEW_SCHEMA).validate(review)
    for field in ("reviewed_views", "checked_objects", "resolved_issue_ids"):
        if len(review[field]) != len(set(review[field])):
            raise ValueError(f"{field} must not repeat IDs")
    if review["candidate_id"] != manifest["candidate_id"]:
        raise ValueError("review belongs to a different frozen candidate")
    objects, views = set(manifest["object_ids"]), set(manifest["view_ids"])
    if not set(review["reviewed_views"]) <= views or not set(review["checked_objects"]) <= objects:
        raise ValueError("review refers to unknown objects or views")
    ids = [issue["id"] for issue in review["issues"]]
    if len(ids) != len(set(ids)):
        raise ValueError("issue IDs must be unique")
    for issue in review["issues"]:
        if issue["object_id"] not in objects or issue["view_id"] not in views:
            raise ValueError("issue must identify a candidate object and review view")
        x0, y0, x1, y1 = issue["region"]
        if x0 >= x1 or y0 >= y1:
            raise ValueError("issue region must have positive width and height")
    old = {issue["id"] for issue in (previous or {}).get("issues", [])}
    resolved = set(review["resolved_issue_ids"])
    if not resolved <= old or resolved & set(ids) or not old <= resolved | set(ids):
        raise ValueError("every previous issue must be rechecked, resolved or retained")
    accepted = review["verdict"] == "accepted"
    if accepted and (not all(manifest["checks"].values())
                     or any(review["dimensions"][key] != "passed" for key in DIMENSIONS)
                     or views != set(review["reviewed_views"])
                     or objects != set(review["checked_objects"])
                     or any(issue["severity"] != "minor" for issue in review["issues"])):
        raise ValueError("acceptance requires all views, objects, dimensions and deterministic checks")
    return accepted


def accept_candidate(project, store, *, candidate_path, review_path, expected_revision, expected_scene_revision):
    from world_service import WorldService
    project = Path(project).resolve()
    manifest = read_json(project_file(project, candidate_path))
    review = read_json(project_file(project, review_path))
    previous = read_json(project_file(project, manifest["previous_review_path"])) if manifest.get("previous_review_path") else None
    if not validate_review(manifest, review, previous):
        raise ValueError("the independent review has not accepted this candidate")
    verify_files(project, manifest["files"])
    scene = read_json(project_file(project, manifest["scene_path"]))
    if scene_hash(scene) != manifest["scene_hash"] or scene["revision"] != expected_revision + 1:
        raise ValueError("candidate does not match the expected next world revision")
    if store.scene()["revision"] != expected_scene_revision:
        raise ValueError("displayed scene changed during review")
    service = WorldService(project, store)
    with service.world._lock():
        current = service.world.read()
        if scene_hash(current) != manifest["scene_hash"]:
            service.world._current(expected_revision)
            service.world._commit(current, scene)
    receipt = read_json(project_file(project, manifest["receipt_path"]))
    receipt["id"] = manifest["candidate_id"][:32]
    receipt["review_candidate_id"] = manifest["candidate_id"]
    write_json(service.world.storage_path("receipts", receipt["id"], "receipt.json"), receipt)
    return service.publish(scene["revision"], expected_scene_revision, receipt["id"])


def review_status(project):
    path = Path(project) / "reconstruction" / "status.json"
    return read_json(path) if path.is_file() else {"stage": "not_started"}


def review_image(project, view_id, kind):
    project = Path(project).resolve()
    status = review_status(project)
    directory = status.get("review_directory")
    if not directory or kind not in {"reference", "render"}:
        raise ValueError("review images are not available")
    manifest = read_json(project_file(project, Path(directory) / "candidate.json"))
    scene = read_json(project_file(project, manifest["scene_path"]))
    view = next((item for item in scene["views"] if item["id"] == view_id), None)
    if view is None:
        raise ValueError("unknown review view")
    build = read_json(project_file(project, manifest["receipt_path"]))
    path = project_file(project, view["reference_image"] if kind == "reference"
                        else Path(build["build_dir"]) / "renders" / f"{view_id}.png")
    relative = str(path.relative_to(project))
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"} or relative not in manifest["files"]:
        raise ValueError("image is not part of the frozen candidate")
    verify_files(project, {relative: manifest["files"][relative]})
    return path
