"""Independent editing branches forked from the build displayed in the workbench."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import uuid

from core import APIError
from cycles_preview import published_build, render_build_preview
from world_build import build_world, file_hash
from world_model import WorldStore, scene_hash, validate_scene
from world_service import WorldService


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def scale_subtree(scene, object_id, factor):
    """Bake uniform scaling into shapes and child offsets, preserving the world schema."""
    children = {}
    for obj in scene["objects"]:
        children.setdefault(obj["parent_id"], []).append(obj)

    def resize(obj, root=False):
        if not root:
            obj["position"] = [value * factor for value in obj["position"]]
        for shape in obj["visual"] + obj["collision"]:
            for key in ("size", "scale", "position"):
                if key in shape:
                    shape[key] = [value * factor for value in shape[key]]
            for key in ("radius", "depth"):
                if key in shape:
                    shape[key] *= factor
        if obj.get("joint"):
            obj["joint"]["position"] = [value * factor for value in obj["joint"]["position"]]
            if obj["joint"]["type"] == "slide":
                obj["joint"]["range"] = [value * factor for value in obj["joint"]["range"]]
        for child in children.get(obj["id"], []):
            resize(child)

    resize(next(obj for obj in scene["objects"] if obj["id"] == object_id), root=True)


class SceneEditor:
    def __init__(self, project, store):
        self.project = Path(project).resolve()
        self.store = store
        self.directory = self.project / "editing"

    def path(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise APIError(400, "无效的编辑分支")
        path = (self.directory / identifier).resolve()
        if not path.is_relative_to(self.project):
            raise APIError(400, "编辑分支路径超出项目")
        if not (path / "state.json").is_file():
            raise APIError(404, "编辑分支不存在")
        return path

    @contextmanager
    def lock(self, identifier):
        with (self.path(identifier) / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def state(self, identifier, expected_version=None):
        state = read_json(self.path(identifier) / "state.json")
        if expected_version is not None and (type(expected_version) is not int or expected_version != state["version"]):
            raise APIError(409, "编辑分支已改变，请重新打开分支后再操作")
        return state

    def record(self, state):
        return read_json(self.path(state["id"]) / "revisions" / (state["history"][state["cursor"]] + ".json"))

    def list(self):
        return {"scene_revision": self.store.scene()["revision"], "branches": [
            {key: value[key] for key in ("id", "name", "version", "source", "updated_at")}
            for path in sorted(self.directory.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for value in [read_json(path)]]}

    def describe(self, identifier):
        state = self.state(identifier)
        record = self.record(state)
        base = f"/api/workspace/scene-editor/{identifier}"
        return {**{key: state[key] for key in ("id", "name", "version", "source", "updated_at")},
                "scene": record["scene"], "can_undo": state["cursor"] > 0,
                "can_redo": state["cursor"] + 1 < len(state["history"]),
                "glb_url": f"{base}/asset?kind=glb&version={state['version']}",
                "blend_url": f"{base}/asset?kind=blend&version={state['version']}",
                "blend_path": record["build"]["blend_path"]}

    def create(self, payload):
        if set(payload) != {"name", "expected_scene_revision"}:
            raise APIError(400, "需要分支名称和当前显示版本")
        name = payload["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise APIError(400, "分支名称应为 1–80 个字符")
        displayed, build = published_build(self.project, self.store)
        if type(payload["expected_scene_revision"]) is not int or displayed["revision"] != payload["expected_scene_revision"]:
            raise APIError(409, "重建显示版本已改变，请重新打开编辑工作区")
        service = WorldService(self.project)
        scene = read_json(service._file(str(Path(build["build_dir"]) / "job.json")))["scene"]
        validate_scene(scene, self.project)
        service._match(scene, build)
        identifier = uuid.uuid4().hex
        directory = self.directory / identifier
        baseline = directory / "baseline"
        baseline.mkdir(parents=True)
        # Own Blender/GLB copies; OBJ/texture inputs are shared and hash-checked on save.
        copied = {}
        for kind in ("blend", "glb"):
            source = service._file(build[f"{kind}_path"])
            target = baseline / f"scene.{kind}"
            shutil.copyfile(source, target)
            if file_hash(target) != build["files"][target.name]:
                raise APIError(409, "来源构建已改变，无法创建分支")
            copied[f"{kind}_path"] = str(target)
        build = {**build, **copied}
        version_id = uuid.uuid4().hex
        WorldStore._write(directory / "revisions" / (version_id + ".json"), {"scene": scene, "build": build})
        WorldStore._write(directory / "state.json", {
            "id": identifier, "name": name.strip(), "version": 1, "cursor": 0, "history": [version_id],
            "source": {"scene_revision": displayed["revision"], "scene_hash": scene_hash(scene)},
            "updated_at": datetime.now(timezone.utc).isoformat()})
        return self.describe(identifier)

    def update(self, identifier, payload):
        action = payload.get("action")
        required = {"action", "expected_version"}
        if action == "save":
            required |= {"object_id", "position", "rotation", "scale_factor", "material"}
        if action not in {"save", "undo", "redo"} or set(payload) != required:
            raise APIError(400, "无效的编辑操作")
        if type(payload["expected_version"]) is not int or payload["expected_version"] < 1:
            raise APIError(400, "需要有效的编辑版本")
        with self.lock(identifier):
            state = self.state(identifier, payload["expected_version"])
            record = self.record(state)
            if action == "save":
                candidate = deepcopy(record["scene"])
                obj = next((obj for obj in candidate["objects"] if obj["id"] == payload["object_id"]), None)
                if obj is None:
                    raise APIError(404, "物体不存在")
                factor = payload["scale_factor"]
                if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
                    raise APIError(400, "缩放倍数必须为正数")
                obj.update(position=payload["position"], rotation=payload["rotation"], material=payload["material"])
                if factor != 1:
                    scale_subtree(candidate, obj["id"], factor)
                candidate["revision"] += 1
                validate_scene(candidate, self.project)
                for path, digest in record["build"]["asset_hashes"].items():
                    if file_hash(WorldService(self.project)._file(path)) != digest:
                        raise APIError(409, "分支来源素材已改变，请从新的重建版本创建分支")
                receipt = build_world(self.project, scene=candidate, render=False,
                                      blender=os.environ.get("SCENE_WORLD_BLENDER", "blender"))
                receipt["id"] = uuid.uuid4().hex
                version_id = uuid.uuid4().hex
                WorldStore._write(self.path(identifier) / "revisions" / (version_id + ".json"),
                                  {"scene": candidate, "build": receipt})
                state["history"] = state["history"][:state["cursor"] + 1] + [version_id]
                state["cursor"] += 1
            else:
                cursor = state["cursor"] + (-1 if action == "undo" else 1)
                if not 0 <= cursor < len(state["history"]):
                    raise APIError(409, "没有可撤销／重做的版本")
                state["cursor"] = cursor
            state["version"] += 1
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            WorldStore._write(self.path(identifier) / "state.json", state)
            return self.describe(identifier)

    def asset(self, identifier, kind, version):
        if kind not in {"glb", "blend"}:
            raise APIError(400, "未知编辑成果类型")
        record = self.record(self.state(identifier, version))
        path = WorldService(self.project)._file(record["build"][kind + "_path"])
        if file_hash(path) != record["build"]["files"][f"scene.{kind}"]:
            raise APIError(409, "编辑成果文件已改变")
        return path

    def render(self, identifier, payload):
        state = self.state(identifier, payload.get("scene_revision"))
        record = self.record(state)
        return render_build_preview(self.project, record["build"], payload,
                                    lambda: self.state(identifier)["version"] == state["version"])
