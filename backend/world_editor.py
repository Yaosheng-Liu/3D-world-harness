"""Object inventory of the currently displayed reconstruction for history navigation."""
from cycles_preview import published_build
from world_model import scene_hash
from world_service import WorldService


def editor_state(project, store):
    service = WorldService(project, store)
    scene = service.world.read()
    displayed, build = published_build(project, store)
    draft = build.get("draft_scene")
    if draft:
        scene = draft
    return {"revision": scene["revision"], "scene_revision": displayed["revision"], "units": scene["units"],
            "editable": not bool(draft), "draft": bool(draft),
            "matches_display": build["scene_hash"] == scene_hash(scene),
            "objects": [{key: obj[key] for key in ("id", "name", "parent_id", "position", "rotation", "evidence")}
                        for obj in scene["objects"]]}
