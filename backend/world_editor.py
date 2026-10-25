"""Browser edits of a published world's object transforms, using the normal build path."""
from copy import deepcopy
import math
import os

from core import APIError
from cycles_preview import published_build
from reconstruction_review import review_status
from reconstruction_plan import planning_status, ACTIVE_STAGES
from world_build import build_world
from world_model import scene_hash
from world_service import WorldService


BUSY_STAGES = {"modeling", "checking", "reviewing", "reviewed", "repairing", "publishing"}


def editor_state(project, store):
    service = WorldService(project, store)
    scene = service.world.read()
    displayed, build = published_build(project, store)
    return {"revision": scene["revision"], "scene_revision": displayed["revision"], "units": scene["units"],
            "matches_display": build["scene_hash"] == scene_hash(scene),
            "objects": [{key: obj[key] for key in ("id", "name", "parent_id", "position", "rotation", "evidence")}
                        for obj in scene["objects"]]}


def edit_transform(project, store, payload):
    if set(payload) != {"expected_revision", "expected_scene_revision", "object_id", "position", "rotation"}:
        raise APIError(400, "object, both revisions, position and rotation are required")
    for field in ("position", "rotation"):
        values = payload[field]
        if (not isinstance(values, list) or len(values) != 3
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
            raise APIError(400, "transforms must contain three finite numbers")
    if (review_status(project)["stage"] in BUSY_STAGES
            or (planning_status(project) or {}).get("stage") in ACTIVE_STAGES):
        raise APIError(409, "重建或审核正在运行，完成本轮后再编辑当前版本")
    displayed, build = published_build(project, store)
    if displayed["revision"] != payload["expected_scene_revision"]:
        raise APIError(409, "显示版本已改变，请刷新物体清单")
    service = WorldService(project, store)
    with service.world._lock():
        current = service.world._current(payload["expected_revision"])
        if build["scene_hash"] != scene_hash(current):
            raise APIError(409, "当前世界尚未发布，请先完成构建和发布")
        candidate = deepcopy(current)
        obj = next((obj for obj in candidate["objects"] if obj["id"] == payload["object_id"]), None)
        if obj is None:
            raise APIError(404, "物体不存在")
        obj.update(position=payload["position"], rotation=payload["rotation"])
        candidate["revision"] += 1
        receipt = build_world(project, scene=candidate, render=False,
                              blender=os.environ.get("SCENE_WORLD_BLENDER", "blender"))
        if store.scene()["revision"] != payload["expected_scene_revision"]:
            raise APIError(409, "构建期间显示版本改变，编辑未保存")
        service.world._commit(current, candidate)
    receipt = service._save_record("receipts", receipt)
    result = service.publish(candidate["revision"], payload["expected_scene_revision"], receipt["id"])
    from reconstruction_review import write_json
    from pathlib import Path
    status = review_status(project)
    status.update(stage="edited", accepted=False, world_revision=candidate["revision"],
                  scene_revision=result["scene"]["revision"], checks=None,
                  dimensions={key: "unknown" for key in ("appearance", "structure", "editability")},
                  reason=None, error=None, summary="物体变换已保存；编辑后的版本尚未重新审核。")
    write_json(Path(project) / "reconstruction/status.json", status)
    return result
