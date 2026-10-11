"""Compact model-facing feedback; immutable packets remain in the store."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def _fields(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: copy.deepcopy(value[name]) for name in names if name in value}


SOURCE_FIELDS = (
    "id", "name", "label", "pane", "reference_id", "reference_name", "reference_frame_id",
    "reference_image_id", "frame_id", "clip_id",
    "view_id", "view_name", "frame_index", "time_sec", "reference_time_sec",
    "scene_revision", "from_stale_snapshot", "width", "height", "image_width",
    "image_height", "source_image_width", "source_image_height", "image_sha256",
    "image_orientation", "source_snapshot_id", "tracking_status",
    "selected_object_ids", "selected_scene_nodes", "animation_clips",
)
POSE_FIELDS = (
    "job_id", "parent_job_id", "track_id", "source", "evidence_kind",
    "parent_evidence_kind", "coordinate_frame", "keypoint_profile",
    "confidence_threshold", "multi_view", "view_ids", "cross_view_identity_source",
    "corrections_path", "parent_result_path", "source_manifest_path",
)


def _archive_manifest(feedback: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Describe each source once, without embedding whole pose archives.

    Short source keys bind image captions to this manifest. Times stay exact
    numeric values and frame_index retains the stored zero-based convention.
    Camera references share one description; full camera matrices, topology,
    predictions and correction documents can be read from the immutable packet.
    """
    result = _fields(feedback, (
        "feedback_id", "session_id", "scene_revision", "delivery_scene_revision",
        "submitted_from_stale_snapshot", "note", "object_prompts",
        "selected_object_ids", "selected_scene_nodes", "active_reference_id",
        "aligned_reference_id", "timeline",
    ))
    result["details"] = {"state_path": str((data_dir / "state.json").resolve()),
                         "feedback_id": feedback["feedback_id"]}
    cameras: dict[str, dict[str, Any]] = {}
    camera_ids: dict[str, str] = {}

    def camera(value: Any) -> str | None:
        if not isinstance(value, dict) or not value:
            return None
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if signature not in camera_ids:
            key = "cam" + str(len(cameras) + 1)
            camera_ids[signature] = key
            summary = _fields(value, (
                "position", "target", "up", "fov", "aspect", "near", "far",
                "reference_image_id", "alignment_exact", "calibration_status",
                "image_undistorted", "coordinate_frame", "intrinsics", "projection",
            ))
            if "camera_to_world" in value:
                summary["calibrated"] = True
            cameras[key] = summary
        return camera_ids[signature]

    def comparison(value: dict[str, Any]) -> dict[str, Any]:
        summary = _fields(value, ("reference_id", "reference_name", "source", "enabled", "opacity",
                                  "rect", "alignment_exact", "coordinate_space"))
        camera_key = camera(value.get("reference_camera"))
        if camera_key:
            summary["reference_camera"] = camera_key
        return summary

    def source(value: dict[str, Any], key: str) -> dict[str, Any]:
        summary = {"key": key, **_fields(value, SOURCE_FIELDS)}
        camera_key = camera(value.get("camera"))
        if camera_key:
            summary["camera"] = camera_key
        reference_camera = camera(value.get("reference_camera"))
        if reference_camera:
            summary["reference_camera"] = reference_camera
        if value.get("comparison"):
            summary["comparison"] = comparison(value["comparison"])
        return summary

    camera_key = camera(feedback.get("camera"))
    if camera_key:
        result["camera"] = camera_key
    if feedback.get("comparison"):
        result["comparison"] = comparison(feedback["comparison"])
    for field, prefix in (("reference_images", "R"), ("image_refs", "I"),
                          ("scene_snapshots", "S"), ("dynamic_frames", "F")):
        if feedback.get(field):
            result[field] = [source(item, f"{prefix}{index}")
                             for index, item in enumerate(feedback[field], 1)]
            if field == "image_refs":
                for item in result[field]:
                    item["token"] = f"[[image:{item['id']}]]"
    if feedback.get("reference_annotated_images"):
        reference_keys = {item["id"]: item["key"] for item in result.get("reference_images", [])}
        result["reference_annotated_images"] = [
            {"key": reference_keys.get(item["reference_id"], f"RA{index}"), "reference_id": item["reference_id"]}
            for index, item in enumerate(feedback["reference_annotated_images"], 1)
        ]
    if feedback.get("crops"):
        result["crops"] = [{"key": f"crop{index}", **_fields(item, ("source", "reference_id"))}
                           for index, item in enumerate(feedback["crops"], 1)]
    if feedback.get("inline_references"):
        references = []
        for item in feedback["inline_references"]:
            reference = _fields(item, ("token", "kind", "id", "from_stale_snapshot"))
            if item.get("kind") == "object":
                reference["object"] = _fields(item.get("object", {}), ("id", "name", "type"))
            elif item.get("kind") == "node":
                reference["scene_node"] = copy.deepcopy(item.get("scene_node"))
            elif item.get("kind") == "annotation":
                reference["annotation"] = _fields(item.get("annotation", {}), ("id", "name", "type", "pane"))
            elif item.get("kind") == "image":
                reference["image"] = {"id": item["id"]}
            # Annotation/image details live in their own lists, once per source.
            references.append(reference)
        result["inline_references"] = references
    if feedback.get("annotations"):
        result["annotations"] = []
        for item in feedback["annotations"]:
            annotation = {name: copy.deepcopy(value) for name, value in item.items()
                          if name != "camera" and not name.endswith("_url")}
            camera_key = camera(item.get("camera"))
            if camera_key:
                annotation["camera"] = camera_key
            result["annotations"].append(annotation)
    if feedback.get("human_pose"):
        result["human_pose"] = []
        for index, pose in enumerate(feedback["human_pose"], 1):
            summary = {"key": f"P{index}", **_fields(pose, POSE_FIELDS)}
            summary["frame"] = source(pose["frame"], f"P{index}")
            job_id = pose.get("job_id")
            if job_id:
                directory = data_dir / "human_pose" / job_id
                manifest_path = directory / "input.json"
                if "source_manifest_path" not in summary and manifest_path.is_file():
                    summary["source_manifest_path"] = str(manifest_path)
                if "parent_result_path" not in summary:
                    result_path = next((directory / name for name in ("output.json", "job.json")
                                        if (directory / name).is_file()), None)
                    if result_path:
                        summary["parent_result_path"] = str(result_path)
            result["human_pose"].append(summary)
    if feedback.get("human_pose_edits"):
        result["human_pose_edits"] = []
        for index, pose in enumerate(feedback["human_pose_edits"], 1):
            summary = {"key": f"E{index}", "id": pose["id"],
                       "token": f"[[pose_edit:{pose['id']}]]", **_fields(pose, POSE_FIELDS)}
            summary["frame"] = source(pose["frame"], f"E{index}")
            document = pose.get("document", {})
            if "source_snapshot_id" in document:
                summary["source_snapshot_id"] = document["source_snapshot_id"]
            frames = document.get("frames", [])
            originals = frames[0].get("original_keypoints", []) if frames else pose["frame"].get("keypoints", [])
            names = document.get("keypoint_names", pose.get("keypoint_names", []))
            original_by_name = {point.get("name", names[index] if index < len(names) else ""): point
                                for index, point in enumerate(originals)}
            summary["edits"] = []
            for edit in pose.get("edits", []):
                summary["edits"].append({
                    "name": edit["name"],
                    "before": _fields(original_by_name.get(edit["name"], {}),
                                      ("x", "y", "score", "in_frame", "manual_visibility")),
                    "after": _fields(edit, ("x", "y", "visibility")),
                })
            result["human_pose_edits"].append(summary)
    if cameras:
        result["cameras"] = cameras
    return result


def feedback_result_manifest(result: dict[str, Any], data_dir: Path, *, plans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    summary = {key: copy.deepcopy(value) for key, value in result.items() if key != "items"}
    summary["format"] = "feedback_summary"
    summary["read_details"] = "Full immutable packets: state_path → feedback by feedback_id, then each source's archive path; include_details=True returns full MCP structuredContent. Default structuredContent is this compact summary. Prompt #frame is one-based; JSON frame_index is zero-based; times are clip-relative seconds."
    summary["items"] = ([plan["manifest"] for plan in plans] if plans is not None else
                        [feedback_manifest(item, data_dir) for item in result.get("items", [])])
    return summary


def caption(source_key: str, role: str) -> str:
    return f"{source_key} · {role}"


def image_caption(image: dict[str, Any]) -> str:
    """Keep every source binding visible when one image serves several roles."""
    return "; ".join(caption(item["source"], item["role"])
                     for item in [image, *image.get("aliases", [])])


def _scene_reference_summary(value: dict[str, Any], key: str, index: int) -> dict[str, Any]:
    result = {"key": key, "archive": f"scene_refs[{index}]",
              **_fields(value, ("id", "name", "source_project_id", "source_scene_revision", "read_only",
                               "snapshot_json_path", "archive_path", "source_project_dir", "source_blend_path", "time_sec"))}
    result["token"] = f"[[scene:{value['id']}]]"
    result["usage"] = "只读类比；仅编辑当前项目，勿修改来源场景。"
    assets = value.get("assets", [])
    result["glb_paths"] = [asset["path"] for asset in assets[:8]]
    result["asset_count"] = len(assets)
    if len(assets) > 8:
        result["additional_assets"] = "snapshot_json_path → assets"
    if value.get("preview_camera"):
        result["preview_camera"] = {"archive": f"scene_refs[{index}].preview_camera"}
    if value.get("source_reference_image"):
        result["source_reference_image"] = _fields(value["source_reference_image"], ("name", "view_id", "view_name", "time_sec", "frame_index"))
    return result


_BOUND_FIELDS = (
    "id", "name", "label", "pane", "reference_id", "reference_frame_id",
    "static_reference_id", "reference_image_id", "frame_id", "snapshot_id",
    "clip_id", "view_id", "view_name", "frame_index", "time_sec",
    "reference_time_sec", "scene_revision", "from_stale_snapshot",
    "selected_object_ids", "selected_scene_nodes",
)
_MARK_FIELDS = (
    "id", "name", "label", "type", "pane", "object_id", "previous_object_id",
    "scene_node", "previous_scene_node", "text", "note", "number", "coordinates",
    "coordinate_frame", "center", "size", "start", "end", "target_position", "target_size", "anchor",
)
_MARK_BINDING_FIELDS = ("reference_image_id", "frame_id", "snapshot_id", "clip_id", "view_id",
                        "frame_index", "time_sec", "reference_time_sec", "scene_revision")


def _reference_ids(value: dict[str, Any]) -> set[str]:
    result = {value[name] for name in ("reference_id", "reference_frame_id", "static_reference_id", "reference_image_id")
              if isinstance(value.get(name), str)}
    for nested in (value.get("camera"), value.get("comparison")):
        if isinstance(nested, dict):
            result.update(nested[name] for name in ("reference_id", "reference_image_id")
                          if isinstance(nested.get(name), str))
    return result


def _mark_matches(mark: dict[str, Any], source: dict[str, Any], group: str) -> bool:
    bound = mark.get("frame_id") or mark.get("snapshot_id")
    if group in {"S", "F"}:
        if bound:
            return bound == source.get("id")
        if mark.get("pane") != "reference" or mark.get("reference_image_id") not in _reference_ids(source):
            return False
        return (mark.get("time_sec") is None or source.get("time_sec") is None
                or abs(mark["time_sec"] - source["time_sec"]) <= 1e-6)
    if group == "R":
        return (mark.get("pane") == "reference" and not bound
                and mark.get("reference_image_id") == source.get("id"))
    return mark.get("pane") == "scene" and not bound


def _short_source(source: dict[str, Any], key: str, archive: str) -> dict[str, Any]:
    result = {"key": key, "archive": archive, **_fields(source, _BOUND_FIELDS)}
    for name in ("camera", "reference_camera"):
        value = source.get(name)
        if isinstance(value, dict) and value:
            result[name] = {"archive": f"{archive}.{name}" if archive else name,
                            **_fields(value, ("reference_image_id", "alignment_exact"))}
    comparison = source.get("comparison")
    if isinstance(comparison, dict):
        result["reference_id"] = comparison.get("reference_id")
        if comparison.get("reference_camera"):
            result["reference_camera"] = {"archive": f"{archive}.comparison.reference_camera" if archive else "comparison.reference_camera"}
    return result


def _mentions_frame(note: str, source: dict[str, Any], *, multiple_views: bool) -> bool:
    """Recognize an exact displayed frame citation, never infer a time range."""
    if source.get("id") and source["id"] in note:
        return True
    name = source.get("name")
    if isinstance(name, str) and name:
        compact_name = re.sub(r"\s+", "", name)
        compact_note = re.sub(r"\s+", "", note)
        boundary = r"(?!\d)" if compact_name[-1:].isdigit() else ""
        if re.search(re.escape(compact_name) + boundary, compact_note):
            return True
    for match in re.finditer(r"(片段参考|片段|场景)\s*(\d+(?:\.\d+)?)\s*s(?![A-Za-z0-9])", note):
        value = source.get("reference_time_sec") if match[1] == "片段参考" else source.get("time_sec")
        if isinstance(value, (int, float)) and abs(float(match[2]) - value) <= 1e-6:
            return True
    index = source.get("frame_index")
    if not isinstance(index, int) or not re.search(rf"#{index + 1}(?!\d)", note):
        return False
    name = source.get("view_name")
    if name:
        literal = str(name).translate(str.maketrans({"[": "［", "]": "］", "【": "〔", "】": "〕"}))
        return bool(re.search(re.escape(literal) + rf"\s+#{index + 1}(?!\d)", note))
    return not multiple_views


def model_input_plan(feedback: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Select this round's evidence without changing its complete stored packet.

    Source keys retain their original array positions. Image deduplication uses
    file bytes, and every alternate source/role remains in the image index.
    """
    data_dir = Path(data_dir)
    marks = list(feedback.get("annotations", []))
    references = list(feedback.get("inline_references", []))
    note = feedback.get("note", "")
    cited_images = set(re.findall(r"\[\[image:([A-Za-z0-9_-]{1,64})\]\]", note))
    cited_images.update(item.get("id") for item in references if item.get("kind") == "image")
    cited_scenes = set(re.findall(r"\[\[scene:([0-9a-f]{32})\]\]", note))
    cited_scenes.update(item.get("id") for item in references if item.get("kind") == "scene")
    cited_poses = set(re.findall(r"\[\[pose:([0-9a-f]{32}):([0-9a-f]{32})\]\]", note))
    cited_edits = set(re.findall(r"\[\[pose_edit:([0-9a-f]{32})\]\]", note))
    cited_objects = {item.get("id") for item in references if item.get("kind") == "object"}
    cited_nodes = {(item.get("scene_node", {}).get("parent_object_id"),
                    tuple(item.get("scene_node", {}).get("node_path", [])))
                   for item in references if item.get("kind") == "node"}
    selected: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
    for field, prefix in (("reference_images", "R"), ("scene_snapshots", "S"),
                          ("dynamic_frames", "F"), ("image_refs", "I"),
                          ("human_pose", "P"), ("human_pose_edits", "E"), ("scene_refs", "C")):
        selected[field] = []
        for index, value in enumerate(feedback.get(field, [])):
            key = f"{prefix}{index + 1}"
            if prefix == "I" and value.get("id") in cited_images:
                selected[field].append((key, index, value))
            elif prefix == "P" and (value.get("job_id"), value.get("frame", {}).get("reference_id")) in cited_poses:
                selected[field].append((key, index, value))
            elif prefix == "E" and value.get("id") in cited_edits:
                selected[field].append((key, index, value))
            elif prefix == "C" and value.get("id") in cited_scenes:
                selected[field].append((key, index, value))
    independent_only = (any(selected[field] for field in ("image_refs", "human_pose", "human_pose_edits", "scene_refs"))
                        and not marks and not cited_objects and not cited_nodes
                        and not feedback.get("crops")
                        and not feedback.get("selected_object_ids") and not feedback.get("selected_scene_nodes"))
    timeline = feedback.get("timeline") or {}
    current_frames = []
    if not independent_only:
        for index, value in enumerate(feedback.get("dynamic_frames", [])):
            if (isinstance(timeline.get("time_sec"), (int, float)) and isinstance(value.get("time_sec"), (int, float))
                    and abs(timeline["time_sec"] - value["time_sec"]) <= 1e-6
                    and (not timeline.get("view_id") or value.get("view_id") == timeline["view_id"])):
                current_frames.append(index)
    involved_refs: set[str] = set()
    for mark in marks:
        if isinstance(mark.get("reference_image_id"), str):
            involved_refs.add(mark["reference_image_id"])
    if not independent_only and feedback.get("active_reference_id"):
        involved_refs.add(feedback["active_reference_id"])
    if not independent_only:
        involved_refs.update(crop["reference_id"] for crop in feedback.get("crops", [])
                             if crop.get("source") == "reference" and isinstance(crop.get("reference_id"), str))
    for field, prefix in (("scene_snapshots", "S"), ("dynamic_frames", "F")):
        multiple_views = len({value.get("view_id") for value in feedback.get(field, []) if value.get("view_id")}) > 1
        for index, value in enumerate(feedback.get(field, [])):
            source_nodes = {(item.get("parent_object_id"), tuple(item.get("node_path", [])))
                            for item in value.get("selected_scene_nodes", [])}
            used = (any(_mark_matches(mark, value, prefix) for mark in marks)
                    or bool(cited_objects.intersection(value.get("selected_object_ids", [])))
                    or bool(cited_nodes.intersection(source_nodes))
                    or _mentions_frame(note, value, multiple_views=multiple_views)
                    or prefix == "F" and index in current_frames)
            if used:
                selected[field].append((f"{prefix}{index + 1}", index, value))
    for _, _, value in selected["image_refs"]:
        involved_refs.update(_reference_ids(value))
    unbound_marks = [mark for mark in marks if not _mark_matches(mark, feedback, "scene")
                     and not any(_mark_matches(mark, value, group)
                                 for field, group in (("reference_images", "R"), ("scene_snapshots", "S"), ("dynamic_frames", "F"))
                                 for value in feedback.get(field, []))]
    if unbound_marks:
        # Legacy marks may lack a source ID. Keep candidate evidence rather
        # than silently dropping the user's marked image.
        for field, group in (("scene_snapshots", "S"), ("dynamic_frames", "F")):
            chosen_indices = {index for _, index, _ in selected[field]}
            for index, value in enumerate(feedback.get(field, [])):
                if index not in chosen_indices:
                    selected[field].append((f"{group}{index + 1}", index, value))
            selected[field].sort(key=lambda item: item[1])
        for mark in unbound_marks:
            if mark.get("pane") == "reference" and not mark.get("reference_image_id"):
                involved_refs.update(value["id"] for value in feedback.get("reference_images", []))
    for index, value in enumerate(feedback.get("reference_images", [])):
        if value.get("id") in involved_refs:
            selected["reference_images"].append((f"R{index + 1}", index, value))
    if (not independent_only and not selected["reference_images"] and not selected["dynamic_frames"]
            and not (feedback.get("comparison") or {}).get("reference_id")
            and feedback.get("reference_images")):
        # Old packets may predate active-reference bindings. Supply one
        # reference for context; the remaining images stay in the archive.
        selected["reference_images"].append(("R1", 0, feedback["reference_images"][0]))

    manifest = _fields(feedback, ("feedback_id", "session_id", "scene_revision", "delivery_scene_revision",
                                  "submitted_from_stale_snapshot", "note", "object_prompts",
                                  "selected_object_ids", "selected_scene_nodes", "active_reference_id", "aligned_reference_id"))
    manifest["details"] = {"state_path": str((data_dir / "state.json").resolve()), "feedback_id": feedback["feedback_id"]}
    if timeline:
        manifest["timeline"] = _fields(timeline, ("clip_id", "view_id", "time_sec", "scope"))
    if references:
        manifest["inline_references"] = []
        for item in references:
            summary = _fields(item, ("token", "kind", "id", "from_stale_snapshot"))
            if item.get("kind") == "object":
                summary["object"] = _fields(item.get("object", {}), ("id", "name", "type"))
            elif item.get("kind") == "node":
                summary["scene_node"] = copy.deepcopy(item.get("scene_node"))
            manifest["inline_references"].append(summary)
    if marks:
        manifest["annotations"] = []
        for index, mark in enumerate(marks):
            annotation = {"archive": f"annotations[{index}]", **_fields(mark, _MARK_FIELDS)}
            points = [point for point in mark.get("points", []) if isinstance(point, dict)
                      and isinstance(point.get("x"), (int, float)) and isinstance(point.get("y"), (int, float))]
            if points:
                if mark.get("type") == "freehand":
                    annotation.pop("coordinates", None)
                annotation["bounds"] = {"x": min(point["x"] for point in points), "y": min(point["y"] for point in points),
                                         "x2": max(point["x"] for point in points), "y2": max(point["y"] for point in points)}
            if mark.get("camera"):
                annotation["camera"] = {"archive": f"annotations[{index}].camera"}
            bindings = [key for field, group in (("reference_images", "R"), ("scene_snapshots", "S"), ("dynamic_frames", "F"))
                        for key, _, value in selected[field] if _mark_matches(mark, value, group)]
            if _mark_matches(mark, feedback, "scene") and not independent_only:
                bindings.append("scene")
            if bindings:
                annotation["sources"] = bindings
                # Source records already carry the exact capture binding.
                # Preserve a mark's differing time/view/version explicitly.
                values = [value for field in ("reference_images", "scene_snapshots", "dynamic_frames")
                          for key, _, value in selected[field] if key in bindings]
                for name in ("view_id", "frame_index", "time_sec", "reference_time_sec", "scene_revision"):
                    if name in mark and values and any(value.get(name) != mark[name] for value in values):
                        annotation[name] = copy.deepcopy(mark[name])
            else:
                annotation.update(_fields(mark, _MARK_BINDING_FIELDS))
            manifest["annotations"].append(annotation)
    archive = _archive_manifest(feedback, data_dir) if selected["human_pose"] or selected["human_pose_edits"] else {}
    for field, entries in selected.items():
        if not entries:
            continue
        manifest[field] = []
        for key, index, value in entries:
            path = f"{field}[{index}]"
            if field in {"human_pose", "human_pose_edits"}:
                summary = copy.deepcopy(archive[field][index])
                summary["archive"] = path
                summary["frame"] = _short_source(value.get("frame", {}), key, f"{path}.frame")
            elif field == "scene_refs":
                summary = _scene_reference_summary(value, key, index)
            else:
                summary = _short_source(value, key, path)
                if field == "image_refs":
                    summary["token"] = f"[[image:{value['id']}]]"
            manifest[field].append(summary)

    images: list[dict[str, Any]] = []
    digests: dict[str, dict[str, Any]] = {}
    media_root = (data_dir / "media").resolve()

    def add(key: str, role: str, url: Any) -> None:
        if not url:
            return
        if not isinstance(url, str) or not re.fullmatch(r"/media/[0-9a-f]{32}\.(?:jpg|png)", url):
            raise ValueError("stored image URL is invalid")
        path = (media_root / url.rsplit("/", 1)[-1]).resolve()
        if path.parent != media_root or not path.is_file():
            raise ValueError("stored image is missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        alias = {"source": key, "role": role}
        if digest in digests:
            image = digests[digest]
            if alias != {"source": image["source"], "role": image["role"]} and alias not in image["aliases"]:
                image["aliases"].append(alias)
            return
        image = {**alias, "url": url, "path": str(path), "aliases": []}
        images.append(image)
        digests[digest] = image

    def scene_images(value: dict[str, Any], key: str, group: str) -> None:
        add(key, "场景原图", value.get("scene_original_url"))
        if not value.get("scene_original_url"):
            # Older clients supplied only screenshot_data_url. It is their
            # sole captured scene evidence, even without explicit mark data.
            add(key, "场景截图", value.get("scene_annotated_url") or value.get("screenshot_url"))
        scene_marks = any(mark.get("pane") == "scene" and _mark_matches(mark, value, group) for mark in marks)
        if scene_marks or any(mark.get("pane") == "scene" for mark in unbound_marks) or value.get("selected_object_ids") or value.get("selected_scene_nodes"):
            add(key, "场景标记或高亮", value.get("scene_annotated_url"))
        comparison = value.get("comparison") or {}
        add(key, "参考原图", value.get("comparison_reference_original_url"))
        if comparison.get("enabled") is True and comparison.get("opacity", 0) > 0:
            add(key, "叠图对比（辅助）", value.get("scene_comparison_url"))

    annotated = {value["reference_id"]: value["url"] for value in feedback.get("reference_annotated_images", [])}
    for key, _, value in selected["reference_images"]:
        add(key, "参考原图", value.get("url"))
        if (any(_mark_matches(mark, value, "R") for mark in marks)
                or any(mark.get("pane") == "reference" and (not mark.get("reference_image_id") or mark["reference_image_id"] == value.get("id")) for mark in unbound_marks)):
            add(key, "参考标记图", annotated.get(value.get("id")))
    if not independent_only:
        manifest["scene"] = _short_source(feedback, "scene", "")
        scene_images(feedback, "scene", "scene")
    for index, crop in enumerate(feedback.get("crops", [])):
        if (crop.get("source") == "reference" and crop.get("reference_id") in involved_refs
                or not independent_only and crop.get("source") in {"scene", None}):
            key = f"crop{index + 1}"
            add(key, "局部标记图", crop.get("url"))
            manifest.setdefault("crops", []).append({"key": key, "archive": f"crops[{index}]", **_fields(crop, ("source", "reference_id"))})
    for key, _, value in selected["image_refs"]:
        add(key, "引用原图", value.get("original_url"))
        if not value.get("annotated_url"):
            add(key, "引用时的显示原图", value.get("display_original_url"))
        add(key, "引用标记图", value.get("annotated_url"))
    for field in ("human_pose", "human_pose_edits"):
        for key, _, value in selected[field]:
            add(key, "人体来源原帧", value.get("reference_original_url"))
            role = "人工修正（橙色）" if field == "human_pose_edits" else "三维投影（青色）" if value.get("evidence_kind") == "projected_3d" else "二维关节（青色）"
            add(key, role, value.get("pose_overlay_url"))
    for key, _, value in selected["scene_refs"]:
        add(key, "相似场景预览（只读）", value.get("preview_url"))
        add(key, "相似场景参考原图（只读）", value.get("source_reference_image", {}).get("url"))
    for field, group in (("scene_snapshots", "S"), ("dynamic_frames", "F")):
        for key, _, value in selected[field]:
            if group == "F":
                add(key, "参考原帧", value.get("reference_original_url"))
                if (any(mark.get("pane") == "reference" and _mark_matches(mark, value, group) for mark in marks)
                        or any(mark.get("pane") == "reference" for mark in unbound_marks)):
                    add(key, "参考标记图", value.get("reference_annotated_url"))
            scene_images(value, key, group)
    manifest["images"] = [{"index": index, "source": image["source"], "role": image["role"],
                            **({"aliases": copy.deepcopy(image["aliases"])} if image["aliases"] else {})}
                           for index, image in enumerate(images, 1)]
    return {"manifest": manifest, "images": images}


def feedback_manifest(feedback: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    return model_input_plan(feedback, data_dir)["manifest"]
