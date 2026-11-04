"""Branch isolation, optimistic edits and build-before-save transactions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from core import APIError, SceneStore
from scene_editor import SceneEditor
from server import make_server
from world_build import file_hash
from world_model import WorldStore, scene_hash


ROOT = Path(__file__).resolve().parents[2]


class SceneEditorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.store = SceneStore(self.project / "data")
        self.source = json.loads((ROOT / "examples/physical_world/scene.json").read_text())
        self.world = WorldStore(self.project)
        self.world.initialize(self.source)
        self.displayed = deepcopy(self.store.scene())
        self.candidate = deepcopy(self.source)
        self.candidate["revision"] = 2
        self.candidate["objects"][0]["position"][0] = 0.3
        self.receipt = self.build(self.project, scene=self.candidate)
        self.receipt["draft_scene"] = self.candidate
        self.editor = SceneEditor(self.project, self.store)
        mock = patch("scene_editor.published_build", return_value=(self.displayed, self.receipt))
        mock.start(); self.addCleanup(mock.stop)

    def build(self, project, *, scene, **kwargs):
        directory = project / "world/builds" / scene_hash(scene)
        directory.mkdir(parents=True, exist_ok=True)
        for kind in ("blend", "glb"):
            (directory / f"scene.{kind}").write_text(scene_hash(scene) + kind)
        (directory / "job.json").write_text(json.dumps({"scene": scene}))
        return {"id": "test-build", "revision": scene["revision"], "scene_hash": scene_hash(scene),
                "build_dir": str(directory), "blend_path": str(directory / "scene.blend"),
                "glb_path": str(directory / "scene.glb"), "asset_hashes": {},
                "files": {f"scene.{kind}": file_hash(directory / f"scene.{kind}") for kind in ("blend", "glb")}}

    def fork(self, name="布局方案"):
        return self.editor.create({"name": name, "expected_scene_revision": self.displayed["revision"]})

    def edit(self, branch):
        obj = next(obj for obj in branch["scene"]["objects"] if obj["id"] == "block")
        return {"action": "save", "expected_version": branch["version"], "object_id": "block",
                "position": [1, 2, 3], "rotation": [.1, .2, .3], "scale_factor": 1.5,
                "material": {**obj["material"], "base_color": [1, .1, .2, 1]}}

    def test_fork_uses_displayed_candidate_and_owns_blender_copy(self):
        branch = self.fork()
        self.assertEqual(branch["scene"], self.candidate)
        self.assertEqual(branch["source"]["scene_hash"], scene_hash(self.candidate))
        own = Path(branch["blend_path"])
        self.assertNotEqual(own, Path(self.receipt["blend_path"]))
        self.assertEqual(own.read_bytes(), Path(self.receipt["blend_path"]).read_bytes())
        Path(self.receipt["blend_path"]).write_bytes(b"changed source build")
        self.assertEqual(self.editor.asset(branch["id"], "blend", 1), own)
        self.assertEqual(self.world.read(), self.source)
        self.assertEqual(self.store.scene(), self.displayed)

    def test_edit_undo_redo_and_second_branch_leave_reconstruction_untouched(self):
        branch, other = self.fork(), self.fork("另一方案")
        status = self.project / "reconstruction/status.json"
        status.parent.mkdir(); status.write_text('{"stage":"modeled"}')
        with patch("scene_editor.build_world", side_effect=self.build):
            edited = self.editor.update(branch["id"], self.edit(branch))
        block = next(obj for obj in edited["scene"]["objects"] if obj["id"] == "block")
        self.assertEqual(block["position"], [1, 2, 3])
        self.assertEqual(block["material"]["base_color"], [1, .1, .2, 1])
        for before, after in zip(self.candidate["objects"], edited["scene"]["objects"]):
            if before["id"] != "block": self.assertEqual(before, after)
        undone = self.editor.update(branch["id"], {"action": "undo", "expected_version": 2})
        self.assertEqual(undone["scene"], branch["scene"])
        redone = self.editor.update(branch["id"], {"action": "redo", "expected_version": 3})
        self.assertEqual(redone["scene"], edited["scene"])
        self.assertEqual(redone["version"], 4)
        self.assertEqual(self.editor.describe(other["id"])["scene"], branch["scene"])
        self.assertEqual(self.world.read(), self.source)
        self.assertEqual(self.store.scene(), self.displayed)
        self.assertEqual(status.read_text(), '{"stage":"modeled"}')

    def test_failed_build_and_stale_edits_do_not_commit(self):
        branch = self.fork()
        with patch("scene_editor.build_world", side_effect=ValueError("broken mesh")):
            with self.assertRaisesRegex(ValueError, "broken mesh"):
                self.editor.update(branch["id"], self.edit(branch))
        self.assertEqual(self.editor.describe(branch["id"]), branch)
        with patch("scene_editor.build_world") as build:
            with self.assertRaises(APIError):
                self.editor.update(branch["id"], {**self.edit(branch), "expected_version": 9})
            build.assert_not_called()

    def test_scaling_preserves_child_hierarchy_and_does_not_scale_neighbors(self):
        child = deepcopy(self.source["objects"][0])
        child.update(id="child", parent_id="table", position=[.2, .1, .3])
        self.candidate["objects"].append(child)
        self.receipt.update(self.build(self.project, scene=self.candidate))
        branch = self.fork()
        table = next(obj for obj in branch["scene"]["objects"] if obj["id"] == "table")
        payload = {**self.edit(branch), "object_id": "table", "position": table["position"],
                   "rotation": table["rotation"], "scale_factor": 2, "material": table["material"]}
        with patch("scene_editor.build_world", side_effect=self.build):
            edited = self.editor.update(branch["id"], payload)
        objects = {obj["id"]: obj for obj in edited["scene"]["objects"]}
        self.assertEqual(objects["child"]["position"], [.4, .2, .6])
        self.assertEqual(objects["child"]["visual"][0]["size"], [8, 8, .2])
        self.assertEqual(objects["table"]["visual"][0]["size"], [3.6, 2.2, .12])
        self.assertEqual(objects["floor"], self.candidate["objects"][0])

    def test_changed_assets_invalid_material_and_bad_factor_fail_before_build(self):
        path = self.project / "mesh.obj"; path.write_text("original")
        self.receipt["asset_hashes"] = {"mesh.obj": file_hash(path)}
        branch = self.fork()
        with patch("scene_editor.build_world") as build:
            for field, value in (("scale_factor", 0), ("scale_factor", float("nan")),
                                 ("material", {"base_color": [1, 2, 0, 1]}), ("rotation", [1, 2]),
                                 ("expected_version", None), ("expected_version", True)):
                with self.subTest(field=field), self.assertRaises((APIError, ValueError)):
                    self.editor.update(branch["id"], {**self.edit(branch), field: value})
            path.write_text("changed")
            with self.assertRaisesRegex(APIError, "来源素材已改变"):
                self.editor.update(branch["id"], self.edit(branch))
            build.assert_not_called()
        self.assertEqual(self.editor.describe(branch["id"]), branch)

    def test_render_uses_branch_build_and_detects_branch_change(self):
        branch = self.fork()
        def render(project, receipt, payload, is_current):
            self.assertEqual(receipt["blend_path"], branch["blend_path"])
            self.store.state["scene"]["revision"] += 1
            self.assertTrue(is_current(), "reconstruction changes must not invalidate branch previews")
            state = self.editor.state(branch["id"]); state["version"] += 1
            WorldStore._write(self.editor.path(branch["id"]) / "state.json", state)
            self.assertFalse(is_current())
        with patch("scene_editor.render_build_preview", side_effect=render):
            self.editor.render(branch["id"], {"scene_revision": 1})

    def test_paths_and_download_revision_are_guarded(self):
        branch = self.fork()
        for identifier, kind, version in (("../world", "glb", 1), (branch["id"], "source", 1), (branch["id"], "glb", 2)):
            with self.assertRaises(APIError): self.editor.asset(identifier, kind, version)

    def test_http_mutations_require_capability_and_valid_authorized_requests_work(self):
        server = make_server(port=0, data_dir=self.project / "http", project_dir=self.project)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            op = build_opener(ProxyHandler({})); url = f"http://127.0.0.1:{server.server_port}"
            for path in ("scene-editor", "scene-editor/" + "0"*32, "scene-editor/" + "0"*32 + "/render"):
                with self.assertRaises(HTTPError) as caught:
                    op.open(Request(url + '/api/workspace/' + path, data=b'{}', headers={'Content-Type':'application/json'}))
                self.assertEqual(caught.exception.code, 403)
            capability = json.load(op.open(url + '/api/workspace/state'))['browser_capability']
            body = json.dumps({"name":"HTTP branch", "expected_scene_revision":self.displayed["revision"]}).encode()
            branch = json.load(op.open(Request(url + '/api/workspace/scene-editor', data=body,
                headers={'Content-Type':'application/json', 'X-Workspace-Capability':capability})))
            self.assertEqual(branch["name"], "HTTP branch")
            self.assertEqual(op.open(url + branch["glb_url"]).read(), Path(self.receipt["glb_path"]).read_bytes())
        finally:
            server.shutdown(); server.server_close(); thread.join()
