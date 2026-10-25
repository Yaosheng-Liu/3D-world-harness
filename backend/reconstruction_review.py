"""Frozen candidate evidence and the independent visual-review acceptance gate."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import shutil

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


HISTORICAL_PLANNED_REVIEW_SCHEMA = deepcopy(REVIEW_SCHEMA)
_planned_issue = HISTORICAL_PLANNED_REVIEW_SCHEMA["properties"]["issues"]["items"]
_planned_issue["properties"]["target"] = {"enum": ["planning", "segmentation", "modeling"]}
_planned_issue["required"].append("target")

PLANNED_REVIEW_SCHEMA = deepcopy(REVIEW_SCHEMA)
_planned_issue = PLANNED_REVIEW_SCHEMA["properties"]["issues"]["items"]
_planned_issue["properties"]["category"] = {"enum": [
    "inventory", "segmentation", "silhouette", "texture_alignment", "lighting",
    "geometry", "appearance", "editability",
]}
_planned_issue["required"].append("category")


def review_schema(manifest):
    if not manifest.get("plan_path"):
        return REVIEW_SCHEMA
    version = manifest.get("workflow_version", 1)
    if version == 1:
        return HISTORICAL_PLANNED_REVIEW_SCHEMA
    if version == 2:
        return PLANNED_REVIEW_SCHEMA
    raise ValueError("unsupported reconstruction review workflow version")


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


def validate_fixed_views(project, scene):
    """Keep the published reference protocol; additional inspection views are allowed."""
    base = WorldStore(project).read()
    candidate = {view["id"]: view for view in scene["views"]}
    for view in base["views"]:
        if candidate.get(view["id"]) != view:
            raise ValueError(f"candidate changed or omitted a fixed reference view: {view['id']}")


def make_object_inspection_sheets(project, output, edit_check):
    """Expose every hidden-object render pair; images remain individually available."""
    from PIL import Image, ImageDraw, ImageOps
    rows = edit_check["object_inspections"]
    sheets = []
    for start in range(0, len(rows), 8):
        batch = rows[start:start + 8]
        sheet = Image.new("RGB", (1024, ((len(batch) + 1) // 2) * 230), "#f1eee8")
        draw = ImageDraw.Draw(sheet)
        for index, row in enumerate(batch):
            x, y = index % 2 * 512, index // 2 * 230
            title = f"{row['object_id']} / {row.get('view_id', 'NO VISIBLE FIXED VIEW')}"
            draw.text((x + 5, y + 5), title, fill="#202423")
            if row["status"] != "rendered":
                draw.text((x + 5, y + 35), row["status"] + " - coverage failed", fill="#a02626")
                continue
            for column, (field, label) in enumerate((("before_path", "BEFORE"), ("hidden_path", "HIDDEN + DESCENDANTS"))):
                path = project_file(project, row[field])
                row[field] = str(path.relative_to(project))
                with Image.open(path) as image:
                    crop = [round(value * (image.width if i % 2 == 0 else image.height))
                            for i, value in enumerate(row["region"])]
                    cropped = image.crop(crop).convert("RGBA")
                    background = Image.new("RGBA", cropped.size, "#dedede")
                    background.alpha_composite(cropped)
                    thumb = ImageOps.contain(background.convert("RGB"), (250, 182))
                    sheet.paste(thumb, (x + column * 256 + (256 - thumb.width) // 2, y + 42))
                draw.text((x + column * 256 + 5, y + 23), label, fill="#202423")
        path = output / f"edit_objects_{start // 8 + 1:02d}.png"
        sheet.save(path)
        sheets.append(str(path.relative_to(project)))
    edit_check["inspection_paths"] = sheets
    return sheets


def freeze_plan_evidence(project, scene, output, files, plan_path, segments_path, geometry_path, *, modeling_report_path):
    """Bind the independent inventory, all original training views and object evidence."""
    from reconstruction_plan import observations, validate_geometry, validate_modeling_report
    inputs = {"plan": project_file(project, plan_path), "segments": project_file(project, segments_path),
              "geometry": project_file(project, geometry_path),
              "modeling_report": project_file(project, modeling_report_path)}
    plan, segments = read_json(inputs["plan"]), read_json(inputs["segments"])
    geometry = validate_geometry(project, str(inputs["plan"].relative_to(project)),
                                 str(inputs["segments"].relative_to(project)), read_json(inputs["geometry"]))
    if geometry["observations_manifest_sha256"] != plan["source_manifest_sha256"]:
        raise ValueError("object geometry belongs to different observations")
    for mapping in (geometry["input_files"], geometry["files"]):
        if any(name in files and files[name] != digest for name, digest in mapping.items()):
            raise ValueError("candidate and geometry evidence hashes disagree")
        files.update(mapping)
    prepared = observations(project)
    inputs["observations"] = project_file(project, "observations/manifest.json")
    source_views = []
    for row in prepared["train"]:
        path = project_file(project, row["path"])
        files[str(path.relative_to(project))] = row["sha256"]
        source_views.append({"view_id": f"source_{row['index']}", "frame_index": row["index"],
                             "path": str(path.relative_to(project))})
    for obj in segments["objects"]:
        for row in obj["observations"]:
            files[str(project_file(project, row["mask_path"]).relative_to(project))] = row["mask_sha256"]
    paths = {}
    for name, path in inputs.items():
        files[str(path.relative_to(project))] = file_hash(path)
        frozen = output / (name + ".json")
        shutil.copyfile(path, frozen)
        paths[name + "_path"] = str(frozen.relative_to(project))
        files[paths[name + "_path"]] = file_hash(frozen)
    inventory = validate_modeling_report(project, str(inputs["plan"].relative_to(project)), scene,
                                        read_json(inputs["modeling_report"]))
    inventory_path = output / "inventory.json"
    write_json(inventory_path, inventory)
    paths["inventory_path"] = str(inventory_path.relative_to(project))
    files[paths["inventory_path"]] = file_hash(inventory_path)
    # Contact sheets expose every source view to the critic, including regions not
    # in the planner's object list. Full-resolution originals remain bound above.
    from PIL import Image, ImageDraw, ImageOps
    source_sheets = []
    for start in range(0, len(source_views), 12):
        batch = source_views[start:start + 12]
        sheet = Image.new("RGB", (1280, ((len(batch) + 3) // 4) * 224), "#f1eee8")
        draw = ImageDraw.Draw(sheet)
        for index, view in enumerate(batch):
            x, y = index % 4 * 320, index // 4 * 224
            with Image.open(project_file(project, view["path"])) as image:
                thumb = ImageOps.contain(image.convert("RGB"), (320, 200))
                sheet.paste(thumb, (x + (320 - thumb.width) // 2, y))
            draw.text((x + 6, y + 204), view["view_id"], fill="#202423")
        path = output / f"source_views_{start // 12 + 1:02d}.png"
        sheet.save(path)
        relative = str(path.relative_to(project))
        files[relative] = file_hash(path)
        source_sheets.append(relative)
    verify_files(project, files)
    return {**paths, "workflow_version": 2, "planned_object_ids": [obj["id"] for obj in plan["objects"]],
            "required_object_ids": [obj["id"] for obj in scene["objects"]],
            "source_observations": source_views, "source_view_ids": [view["view_id"] for view in source_views],
            "source_inspection_paths": source_sheets}, inventory["passed"]


def freeze_candidate(project, candidate_path, receipt_path, output, *, blender="blender", previous_review=None,
                     plan_path=None, segments_path=None, geometry_path=None, modeling_report_path=None):
    """Verify builder output, then independently exercise the reopened Blender file."""
    project, output = Path(project).resolve(), Path(output).resolve()
    if not output.is_relative_to(project):
        raise ValueError("review output must be inside this project")
    planned_inputs = (plan_path, segments_path, geometry_path)
    if any(planned_inputs) and not all(planned_inputs):
        raise ValueError("plan, segments and geometry must be supplied together")
    output.mkdir(parents=True, exist_ok=False)
    scene = validate_scene(read_json(project_file(project, candidate_path)), project)
    if plan_path:
        validate_fixed_views(project, scene)
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
    planned, inventory_passed = {}, None
    if plan_path:
        modeling_report_path = modeling_report_path or str(Path(candidate_path).parent / "modeling_report.json")
        planned, inventory_passed = freeze_plan_evidence(project, scene, output, files, *planned_inputs,
                                                        modeling_report_path=modeling_report_path)
    write_json(output / "scene.json", scene)
    write_json(output / "build.json", receipt)
    inspection_ids = sorted(obj["id"] for obj in scene["objects"]) if planned else []
    job = {"blend_path": receipt["blend_path"], "scene": scene, "output_dir": str(output),
           "inspection_object_ids": inspection_ids}
    write_json(output / "edit_job.json", job)
    with (output / "edit_check.log").open("w") as log:
        subprocess.run([blender, "--background", "--factory-startup", "--python-exit-code", "1",
                        "--python", str(ROOT / "scripts/review_editability_blender.py"),
                        "--", str(output / "edit_job.json")], check=True, timeout=min(1800, max(300, 20 * len(inspection_ids))),
                       stdout=log, stderr=subprocess.STDOUT)
    edit_check = read_json(output / "editability.json")
    if planned:
        rows = edit_check["object_inspections"]
        if len(rows) != len(inspection_ids) or {row["object_id"] for row in rows} != set(inspection_ids):
            raise ValueError("object edit images do not cover the planned and candidate objects")
        make_object_inspection_sheets(project, output, edit_check)
        write_json(output / "editability.json", edit_check)
    from world_evaluation import evaluate_views, write_view_inspection
    metrics = evaluate_views(project, scene, receipt)
    write_json(output / "metrics.json", metrics)
    inspection = write_view_inspection(project, scene, receipt)
    write_json(output / "inspection.json", inspection)
    # Hash every generated check, including the exact comparisons the critic sees.
    for path in [output / name for name in ("scene.json", "build.json", "editability.json", "metrics.json", "inspection.json")]:
        files[str(path.relative_to(project))] = file_hash(path)
    for path in output.rglob("*.png"):
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
                "previous_review_path": previous_path, **planned}
    if planned:
        manifest["checks"]["inventory"] = inventory_passed
        manifest["checks"]["edit_visual_coverage"] = all(row["status"] == "rendered" for row in edit_check["object_inspections"])
    write_json(output / "candidate.json", manifest)
    return manifest


def validate_review(manifest, review, previous=None):
    """Reject unbound, incomplete, or self-contradictory approvals."""
    Draft202012Validator(review_schema(manifest)).validate(review)
    for field in ("reviewed_views", "checked_objects", "resolved_issue_ids"):
        if len(review[field]) != len(set(review[field])):
            raise ValueError(f"{field} must not repeat IDs")
    if review["candidate_id"] != manifest["candidate_id"]:
        raise ValueError("review belongs to a different frozen candidate")
    objects = set(manifest["object_ids"]) | set(manifest.get("planned_object_ids", []))
    required_objects = set(manifest["object_ids"]) | set(manifest.get("required_object_ids", []))
    views = set(manifest["view_ids"]) | set(manifest.get("source_view_ids", []))
    issue_objects = objects | ({"scene"} if manifest.get("plan_path") else set())
    if not set(review["reviewed_views"]) <= views or not set(review["checked_objects"]) <= objects:
        raise ValueError("review refers to unknown objects or views")
    ids = [issue["id"] for issue in review["issues"]]
    if len(ids) != len(set(ids)):
        raise ValueError("issue IDs must be unique")
    for issue in review["issues"]:
        if issue["object_id"] not in issue_objects or issue["view_id"] not in views:
            raise ValueError("issue must identify a bound object and review view")
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
                     or not required_objects <= set(review["checked_objects"])
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
    status = read_json(path) if path.is_file() else {"stage": "not_started"}
    active = {"modeling", "checking", "reviewing", "reviewed", "repairing", "publishing"}
    if status["stage"] in active and status.get("pid"):
        from reconstruction_plan import controller_running
        if not controller_running(project, status["pid"], "review_reconstruction.py", "run_reconstruction.py"):
            status.update(stage="interrupted", reason="流程控制进程已停止；最近候选和审核证据仍保留，请检查运行记录。")
    return status


def review_image(project, view_id, kind):
    project = Path(project).resolve()
    status = review_status(project)
    directory = status.get("review_directory")
    if not directory or kind not in {"reference", "render"}:
        raise ValueError("review images are not available")
    manifest = read_json(project_file(project, Path(directory) / "candidate.json"))
    source = next((item for item in manifest.get("source_observations", []) if item["view_id"] == view_id), None)
    if source:
        if kind != "reference":
            raise ValueError("source evidence has no corresponding candidate render")
        path = project_file(project, source["path"])
        relative = str(path.relative_to(project))
        if relative not in manifest["files"] or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("source image is not part of the frozen candidate")
        verify_files(project, {relative: manifest["files"][relative]})
        return path
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
