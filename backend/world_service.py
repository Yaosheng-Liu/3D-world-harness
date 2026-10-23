"""Project-scoped world tools shared by HTTP, MCP and the command line."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from core import APIError
from world_model import WorldConflict, WorldStore, scene_hash


REVISION = {"type": "integer", "minimum": 1}
RUN_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}


def _tool(operation, description, properties=None, required=(), *, readonly=False):
    return {
        "name": "workspace_" + operation + "_world",
        "description": description,
        "inputSchema": {"type": "object", "properties": properties or {},
                        "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": readonly, "destructiveHint": False,
                        "idempotentHint": readonly, "openWorldHint": False},
    }


WORLD_TOOLS = [
    _tool("inspect", "Read the editable world, persistent object IDs, source evidence and world revision. World revision is independent of the displayed scene revision.",
          {"object_id": {"type": "string", "minLength": 1}}, readonly=True),
    _tool("initialize", "Create world/scene.json from a project-local world manifest. Refuses to replace an existing world.",
          {"local_path": {"type": "string", "minLength": 1}}, ["local_path"]),
    _tool("edit", "Atomically edit objects using update {object_id,fields}, add {object}, or delete {object_id} operations. Update fields replace complete top-level fields; edit visual and collision together when resizing. Does not rebuild or publish.",
          {"expected_revision": REVISION, "changes": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object"}}},
          ["expected_revision", "changes"]),
    _tool("restore", "Restore an earlier world as a new revision; preserves history. Rebuild before publishing.",
          {"expected_revision": REVISION, "revision": REVISION}, ["expected_revision", "revision"]),
    _tool("build", "Build a Blender source and GLB with persistent object IDs. Optionally render fixed review views or bake a same-revision physics run. Rendering does not establish similarity to real photographs.",
          {"expected_revision": REVISION, "render": {"type": "boolean", "default": False}, "simulation_id": RUN_ID},
          ["expected_revision"]),
    _tool("simulate", "Run MuJoCo rigid-body dynamics on a frozen world. Action: impulse/force {object_id,vector,duration_sec?}, or joint_force {object_id,force,duration_sec}. Returns artifact paths and physical checks, not a fabricated animation.",
          {"expected_revision": REVISION, "action": {"type": "object"},
           "duration_sec": {"type": "number", "exclusiveMinimum": 0, "maximum": 10, "default": 2},
           "sample_hz": {"type": "number", "minimum": 1, "maximum": 60, "default": 30}}, ["expected_revision"]),
    _tool("evaluate", "Check build and optional simulation provenance against the current world. Separately reports structural checks and unmeasured visual/real-world accuracy.",
          {"expected_revision": REVISION, "build_id": RUN_ID, "simulation_id": RUN_ID}, ["expected_revision", "build_id"], readonly=True),
    _tool("publish", "Publish a verified, current-world GLB through the existing scene revision guard. A physics animation with failed checks cannot be published by this tool.",
          {"expected_revision": REVISION, "expected_scene_revision": REVISION, "build_id": RUN_ID},
          ["expected_revision", "expected_scene_revision", "build_id"]),
]
WORLD_TOOL_MAP = {tool["name"]: tool for tool in WORLD_TOOLS}


class WorldService:
    def __init__(self, project_dir, scene_store=None):
        self.project_dir = Path(project_dir).resolve()
        self.world = WorldStore(self.project_dir)
        self.scene_store = scene_store

    def _file(self, value):
        path = (self.project_dir / value).resolve(strict=True)
        if not path.is_relative_to(self.project_dir) or not path.is_file():
            raise ValueError("world input must be a file inside the current project")
        return path

    def _snapshot(self, revision):
        scene = self.world.read()
        if scene["revision"] != revision:
            raise APIError(409, f"world revision changed to {scene['revision']}; inspect and retry")
        return scene

    def _record(self, kind, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid world artifact ID")
        path = self._file(f"world/{kind}/{identifier}/receipt.json")
        return json.loads(path.read_text())

    def _save_record(self, kind, record):
        identifier = uuid.uuid4().hex
        directory = self.world.storage_path(kind, identifier)
        directory.mkdir(parents=True)
        record["id"] = identifier
        (directory / "receipt.json").write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
        return record

    def _simulation(self, scene, identifier):
        from world_build import file_hash

        record = self._record("simulations", identifier)
        self._match(scene, record)
        for key, path in record["artifacts"].items():
            if file_hash(self._file(path)) != record["files"][key]:
                raise ValueError("a simulation artifact changed; run the simulation again")
        if json.loads(self._file(record["artifacts"]["report"]).read_text()) != record["report"]:
            raise ValueError("simulation report differs from its receipt")
        return record

    @staticmethod
    def _match(scene, record):
        if record.get("scene_hash") != scene_hash(scene) or record.get("revision") != scene["revision"]:
            raise APIError(409, "world changed since this artifact was produced; rebuild or simulate again")

    def call(self, name, arguments):
        definition = WORLD_TOOL_MAP[name]
        error = next(Draft202012Validator(definition["inputSchema"]).iter_errors(arguments), None)
        if error:
            raise APIError(400, error.message)
        operation = name.removeprefix("workspace_").removesuffix("_world")
        try:
            return getattr(self, operation)(**arguments)
        except WorldConflict as exc:
            raise APIError(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise APIError(404, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise APIError(400, str(exc)) from exc

    def inspect(self, object_id=None):
        scene = self.world.read()
        if object_id is None:
            return scene
        obj = next((obj for obj in scene["objects"] if obj["id"] == object_id), None)
        if obj is None:
            raise APIError(404, f"world object {object_id!r} not found")
        return {"revision": scene["revision"], "scene_hash": scene_hash(scene), "object": obj}

    def initialize(self, local_path):
        scene = json.loads(self._file(local_path).read_text())
        return self.world.initialize(scene)

    def edit(self, expected_revision, changes):
        return self.world.edit(expected_revision, changes)

    def restore(self, expected_revision, revision):
        return self.world.restore(expected_revision, revision)

    def build(self, expected_revision, render=False, simulation_id=None):
        from world_build import build_world

        scene = self._snapshot(expected_revision)
        trajectory = None
        if simulation_id:
            simulation = self._simulation(scene, simulation_id)
            trajectory = json.loads(self._file(simulation["artifacts"]["trajectory"]).read_text())
        receipt = build_world(self.project_dir, scene=scene, render=render, trajectory=trajectory,
                              blender=os.environ.get("SCENE_WORLD_BLENDER", "blender"))
        receipt["simulation_id"] = simulation_id
        return self._save_record("receipts", receipt)

    def simulate(self, expected_revision, action=None, duration_sec=2, sample_hz=30):
        from world_physics import simulate
        from world_build import file_hash

        scene = self._snapshot(expected_revision)
        directory = self.world.storage_path("runs", uuid.uuid4().hex)
        result = simulate(scene, action=action, duration_sec=duration_sec, sample_hz=sample_hz,
                          output_dir=directory, project_dir=self.project_dir)
        return self._save_record("simulations", {"revision": scene["revision"], "scene_hash": scene_hash(scene),
                                                "report": result["report"], "artifacts": result["artifacts"],
                                                "files": {key: file_hash(Path(path)) for key, path in result["artifacts"].items()}})

    def evaluate(self, expected_revision, build_id, simulation_id=None):
        scene = self._snapshot(expected_revision)
        build = self._record("receipts", build_id)
        self._match(scene, build)
        from world_build import file_hash, inspect_glb

        for name, digest in build["files"].items():
            if file_hash(self._file(str(Path(build["build_dir"]) / name))) != digest:
                raise ValueError("a build artifact changed after validation; rebuild before publishing")
        for name, digest in build["asset_hashes"].items():
            if file_hash(self._file(name)) != digest:
                raise ValueError("a source asset changed after the build; rebuild before publishing")
        inspect_glb(self._file(build["glb_path"]), {obj["id"] for obj in scene["objects"]})
        physics = {"status": "not_run"}
        if simulation_id and build.get("simulation_id") and simulation_id != build["simulation_id"]:
            raise ValueError("animation is bound to a different simulation")
        simulation_id = simulation_id or build.get("simulation_id")
        if simulation_id:
            simulation = self._simulation(scene, simulation_id)
            physics = simulation["report"]
        return {"revision": scene["revision"], "scene_hash": scene_hash(scene), "build_id": build_id,
                "structure": {"status": "passed", "object_ids": build["object_ids"]}, "physics": physics,
                "visual_accuracy": {"status": "not_measured", "reason": "No calibrated real reference comparison was supplied."},
                "physical_calibration": {"status": "not_measured", "reason": "Simulation checks do not establish agreement with real experiments."}}

    def publish(self, expected_revision, expected_scene_revision, build_id):
        if self.scene_store is None:
            raise ValueError("publishing requires a running workbench")
        with self.world._lock():
            evaluation = self.evaluate(expected_revision, build_id)
            build = self._record("receipts", build_id)
            if build.get("simulation_id") and evaluation["physics"].get("passed") is not True:
                raise ValueError("physics checks failed; inspect the simulation report before publishing")
            scene = self.scene_store.set_scene_preview(str(self._file(build["glb_path"])), expected_revision=expected_scene_revision)
        return {"scene": scene, "world_revision": expected_revision, "build_id": build_id, "evaluation": evaluation}
