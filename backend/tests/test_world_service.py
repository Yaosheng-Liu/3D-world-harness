"""Authenticated world tools, project isolation and publication evidence gates."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import APIError, SceneStore
from server import make_server
from test_glb_validation import make_glb
from world_build import file_hash
from world_model import WorldStore, scene_hash
from world_service import WORLD_TOOLS, WorldService


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/physical_world/scene.json"


class WorldServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "source.json").write_bytes(EXAMPLE.read_bytes())
        self.store = SceneStore(self.root / "data")
        self.service = WorldService(self.project, self.store)
        self.call("initialize", local_path="source.json")

    def call(self, operation, **arguments):
        return self.service.call("workspace_" + operation + "_world", arguments)

    def build_fixture(self, simulation_id=None, source=None):
        scene = self.call("inspect")
        directory = self.project / "world" / "builds" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        nodes = [{"extras": {"stable_id": obj["id"]}} for obj in scene["objects"]]
        glb = directory / "scene.glb"
        glb.write_bytes(make_glb({"asset": {"version": "2.0"}, "scene": 0,
                                  "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes}))
        receipt = {"revision": scene["revision"], "scene_hash": scene_hash(scene),
                   "build_dir": str(directory), "glb_path": str(glb),
                   "files": {glb.name: file_hash(glb)}, "asset_hashes": {},
                   "object_ids": [obj["id"] for obj in scene["objects"]]}
        if source:
            receipt["asset_hashes"][source.name] = file_hash(source)
        with patch("world_build.build_world", return_value=receipt):
            return self.call("build", expected_revision=scene["revision"], **({"simulation_id": simulation_id} if simulation_id else {}))

    def assert_api_error(self, status, function, *args, **kwargs):
        with self.assertRaises(APIError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def test_publication_guards_world_and_display_revisions_independently(self):
        build = self.build_fixture()
        evaluated = self.call("evaluate", expected_revision=1, build_id=build["id"])
        self.assertEqual(evaluated["structure"]["status"], "passed")
        self.assertEqual(evaluated["visual_accuracy"]["status"], "not_measured")
        display_revision = self.store.scene()["revision"]
        self.assert_api_error(409, self.call, "publish", expected_revision=1,
                              expected_scene_revision=display_revision + 1, build_id=build["id"])
        published = self.call("publish", expected_revision=1, expected_scene_revision=display_revision, build_id=build["id"])
        self.assertEqual(published["scene"]["revision"], display_revision + 1)
        self.assertEqual(self.call("inspect")["revision"], 1)
        self.call("edit", expected_revision=1, changes=[{"op": "update", "object_id": "block", "fields": {"name": "Edited"}}])
        self.assert_api_error(409, self.call, "evaluate", expected_revision=2, build_id=build["id"])
        self.assert_api_error(409, self.call, "publish", expected_revision=1,
                              expected_scene_revision=display_revision + 1, build_id=build["id"])

    def test_changed_build_or_source_asset_cannot_publish(self):
        source = self.project / "source.obj"
        source.write_text("v 0 0 0\n")
        build = self.build_fixture(source=source)
        source.write_text("v 1 0 0\n")
        self.assert_api_error(400, self.call, "evaluate", expected_revision=1, build_id=build["id"])
        source.write_text("v 0 0 0\n")
        Path(build["glb_path"]).write_bytes(b"replaced build")
        self.assert_api_error(400, self.call, "publish", expected_revision=1,
                              expected_scene_revision=self.store.scene()["revision"], build_id=build["id"])

    def test_real_simulation_is_bound_to_world_and_failed_physics_blocks_publish(self):
        run = self.call("simulate", expected_revision=1, duration_sec=.15)
        self.assertFalse(run["report"]["real_world_calibrated"])
        self.assertTrue(run["report"]["passed"], run["report"])
        trajectory = json.loads(Path(run["artifacts"]["trajectory"]).read_text())
        self.assertGreater(len(trajectory["timestamps"]), 2)
        build = self.build_fixture(simulation_id=run["id"])
        self.assertTrue(self.call("evaluate", expected_revision=1, build_id=build["id"])["physics"]["passed"])
        # Real intersecting bodies produce a failed contact report, not a fabricated flag.
        self.call("edit", expected_revision=1, changes=[{"op": "update", "object_id": "block", "fields": {"position": [0, 0, -.05]}}])
        bad_run = self.call("simulate", expected_revision=2, duration_sec=.05)
        self.assertFalse(bad_run["report"]["passed"])
        bad_build = self.build_fixture(simulation_id=bad_run["id"])
        self.assert_api_error(400, self.call, "publish", expected_revision=2,
                              expected_scene_revision=self.store.scene()["revision"], build_id=bad_build["id"])
        self.assert_api_error(409, self.call, "build", expected_revision=2, simulation_id=run["id"])

    def test_modified_simulation_trajectory_is_rejected_before_build(self):
        run = self.call("simulate", expected_revision=1, duration_sec=.05)
        path = Path(run["artifacts"]["trajectory"])
        trajectory = json.loads(path.read_text())
        trajectory["poses"]["block"][-1]["position"][0] += 10
        path.write_text(json.dumps(trajectory))
        with patch("world_build.build_world", side_effect=AssertionError("unverified trajectory reached builder")) as builder:
            self.assert_api_error(400, self.call, "build", expected_revision=1, simulation_id=run["id"])
            builder.assert_not_called()

    def test_generated_storage_symlinks_cannot_write_outside_project(self):
        outside = self.root / "other-project"
        outside.mkdir()
        world = self.project / "world"
        (world / "runs").symlink_to(outside, target_is_directory=True)
        with patch("world_physics.simulate") as simulator:
            self.assert_api_error(400, self.call, "simulate", expected_revision=1, duration_sec=.01)
            simulator.assert_not_called()
        (world / "runs").unlink()
        (world / "simulations").symlink_to(outside, target_is_directory=True)
        self.assert_api_error(400, self.call, "simulate", expected_revision=1, duration_sec=.01)
        (world / "receipts").symlink_to(outside, target_is_directory=True)
        self.assert_api_error(400, self.build_fixture)
        self.assertEqual(list(outside.iterdir()), [])

    def test_http_and_stdio_plugin_tools_keep_project_credentials_and_worlds_isolated(self):
        server = make_server(port=0, project_dir=self.project, data_dir=self.root / "http-data", feedback_transport="mcp_events")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(3))
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        context = server.project_registry.root
        opener = build_opener(ProxyHandler({}))

        def request(path, data, key):
            req = Request(base + path, data=json.dumps(data).encode(), headers={
                "Content-Type": "application/json", "X-Scene-Harness-Key": key})
            try:
                response = opener.open(req, timeout=10)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)

        prefix = f"/p/{context.project_id}/api/workspace/world/"
        self.assertEqual(request(prefix + "inspect", {}, context.store.browser_token)[0], 403)
        self.assertEqual(request(prefix + "inspect", {}, context.store.control_token)[1]["revision"], 1)
        changes = [{"op": "update", "object_id": "block", "fields": {"name": "HTTP edited"}}]
        self.assertEqual(request(prefix + "edit", {"expected_revision": 1, "changes": changes}, context.store.control_token)[0], 200)
        self.assertEqual(request(prefix + "edit", {"expected_revision": 1, "changes": changes}, context.store.control_token)[0], 409)
        self.assertEqual(request(prefix + "edit", {"expected_revision": 2, "changes": []}, context.store.control_token)[0], 400)
        self.assertEqual(request(prefix + "inspect", {"object_id": "missing"}, context.store.control_token)[0], 404)
        self.assertEqual(request(prefix + "restore", {"expected_revision": 2, "revision": 1}, context.store.control_token)[1]["revision"], 3)
        # Import an independent scene to exercise actual /p/<id> routing.
        catalog = self.root / "catalog"
        other = catalog / "other"
        other.mkdir(parents=True)
        (other / "scene.glb").write_bytes(make_glb({"asset": {"version": "2.0"}, "scene": 0, "scenes": [{}]}))
        (catalog / "manifest.json").write_text(json.dumps({"scenes": [{"name": "Other", "folder": "other", "glb": "other/scene.glb"}]}))
        imported = server.project_registry.import_folder({"path": str(catalog), "request_id": uuid.uuid4().hex})
        self.assertEqual(len(imported["projects"]), 1, imported)
        other_context = server.project_registry.get(imported["projects"][0]["project_id"])
        (other_context.project_dir / "source.json").write_bytes(EXAMPLE.read_bytes())
        other_prefix = f"/p/{other_context.project_id}/api/workspace/world/"
        self.assertEqual(request(other_prefix + "initialize", {"local_path": "source.json"}, context.store.control_token)[0], 403)
        self.assertEqual(request(other_prefix + "initialize", {"local_path": "source.json"}, other_context.store.control_token)[0], 200)
        self.assertEqual(WorldStore(other_context.project_dir).read()["revision"], 1)
        self.assertEqual(WorldStore(self.project).read()["revision"], 3)
        env = {**os.environ, "SCENE_FEEDBACK_PROJECT_ID": context.project_id,
               "SCENE_FEEDBACK_PROJECT_DIR": str(self.project), "SCENE_FEEDBACK_DATA_DIR": str(context.store.data_dir),
               "SCENE_FEEDBACK_PORT": str(server.server_port)}
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "workspace_inspect_world", "arguments": {"object_id": "cup"}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                        "name": "workspace_edit_world", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                        "name": "workspace_inspect_world", "arguments": {"object_id": "missing"}}}]
        result = subprocess.run([sys.executable, str(ROOT / "scripts/plugin_bridge.py")],
                                input="\n".join(json.dumps(item) for item in requests) + "\n",
                                text=True, capture_output=True, env=env, timeout=15, check=True)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        listing, inspection = (response["result"] for response in responses[:2])
        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertTrue({tool["name"] for tool in WORLD_TOOLS} <= {tool["name"] for tool in listing["tools"]})
        self.assertEqual(inspection["structuredContent"]["object"]["id"], "cup")
        self.assertEqual(inspection["structuredContent"]["revision"], 3)


if __name__ == "__main__":
    unittest.main()
