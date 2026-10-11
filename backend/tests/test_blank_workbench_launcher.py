"""An independent empty root restores imports without creating Codex tasks."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO_ROOT / "scripts"), str(REPO_ROOT / "backend")]

from core import SceneStore
from gateway import WorkspaceGateway
from start_blank_workbench import create_blank_server, workbench_url, WORKBENCH_NAME


def glb_bytes() -> bytes:
    document = json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "scene": 0}).encode()
    document += b" " * (-len(document) % 4)
    return struct.pack("<4sII", b"glTF", 2, len(document) + 20) + struct.pack("<I4s", len(document), b"JSON") + document


class BlankWorkbenchLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "workspace"
        self.data = self.root / "data"
        self.server = None
        self.no_tasks = [
            patch("appserver_adapter.CodexAppServerAdapter", side_effect=AssertionError("implicit Codex adapter")),
            patch("shared_thread_adapter.SharedDesktopAdapter", side_effect=AssertionError("implicit shared task")),
            patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("implicit model task")),
            patch.object(WorkspaceGateway, "list_models", side_effect=AssertionError("implicit model selection")),
        ]
        self.forbidden = [item.start() for item in self.no_tasks]

    def tearDown(self):
        self.close()
        for item in self.no_tasks:
            item.stop()
        self.temporary.cleanup()

    def open(self):
        self.server = create_blank_server(port=0, project_dir=self.project, data_dir=self.data)
        return self.server

    def close(self):
        if self.server is not None:
            self.server.server_close()
            self.server = None

    def fixture(self):
        source = self.root / "ready-source"
        source.mkdir()
        (source / "scene.glb").write_bytes(glb_bytes())
        Image.new("RGB", (12, 12), "navy").save(source / "reference.png")
        (source / "scene.blend").write_bytes(b"editable original scene")
        (source / "workbench-ready.json").write_text(json.dumps({
            "schema_version": 1, "name": "Imported scene", "scene_glb_path": "scene.glb",
            "reference_images": ["reference.png"], "blend": "scene.blend",
        }), encoding="utf-8")
        return source

    def test_empty_root_and_stable_direct_access_url_without_task(self):
        server = self.open()
        root = server.project_registry.root
        state = root.gateway.state()
        self.assertEqual(root.store.scene()["objects"], [])
        self.assertEqual(root.name, WORKBENCH_NAME)
        self.assertEqual(root.store.scene()["name"], WORKBENCH_NAME)
        self.assertEqual(state["project_name"], WORKBENCH_NAME)
        self.assertEqual(state["feedback_transport"], "mcp_events")
        self.assertIsNone(state["thread_id"])
        self.assertIsNone(root.gateway.adapter)
        self.assertEqual(state["created_thread_ids"], [])
        session = root.store.get_session(state["session_id"])
        self.assertEqual(session["reference_images"], [])
        self.assertIsNone(session["reference_clip"])
        self.assertEqual(workbench_url(server), f"http://127.0.0.1:{server.server_port}/")
        project_id, session_id = root.project_id, state["session_id"]
        self.close()
        server = self.open()
        self.assertEqual(server.project_registry.root.project_id, project_id)
        self.assertEqual(server.workspace_gateway.state()["session_id"], session_id)
        self.assertEqual(server.project_registry.root.name, WORKBENCH_NAME)
        for forbidden in self.forbidden:
            forbidden.assert_not_called()

    def test_import_restart_unload_and_reimport_preserve_source_and_child_edits(self):
        source = self.fixture()
        original = {path.name: path.read_bytes() for path in source.iterdir()}
        server = self.open()
        root = server.project_registry.root
        root_id = root.project_id
        root_session = root.gateway.state()["session_id"]
        result = server.project_registry.import_folder({"path": str(source), "request_id": uuid.uuid4().hex})
        self.assertEqual(result["counters"]["imported"], 1, result)
        child_id = result["projects"][0]["project_id"]
        child = server.project_registry.get(child_id)
        child_session = child.gateway.state()["session_id"]
        scene = child.store.scene()
        child.store.update_scene(scene["revision"], [{"object_id": scene["objects"][0]["id"], "fields": {"position": [2, 0, 1]}}])
        changed_scene = child.store.scene()
        self.close()
        server = self.open()
        root = server.project_registry.root
        self.assertEqual(root.project_id, root_id)
        self.assertEqual(root.gateway.state()["session_id"], root_session)
        self.assertEqual(root.store.scene()["objects"], [])
        child = server.project_registry.get(child_id)
        self.assertEqual(child.gateway.state()["session_id"], child_session)
        self.assertEqual(child.store.scene(), changed_scene)
        server.project_registry.unload(child_id)
        self.assertEqual(len(server.project_registry.contexts()), 1)
        self.close()
        server = self.open()
        self.assertEqual(len(server.project_registry.contexts()), 1)
        result = server.project_registry.import_folder({"path": str(source), "request_id": uuid.uuid4().hex})
        child = server.project_registry.get(child_id)
        self.assertEqual(result["projects"][0]["project_id"], child_id)
        self.assertEqual(child.gateway.state()["session_id"], child_session)
        self.assertEqual(child.store.scene(), changed_scene)
        self.assertEqual({path.name: path.read_bytes() for path in source.iterdir()}, original)
        self.assertEqual(server.project_registry.root.store.scene()["objects"], [])
        for forbidden in self.forbidden:
            forbidden.assert_not_called()

    def test_existing_scene_reference_or_task_is_rejected_before_state_changes(self):
        self.project.mkdir()
        store = SceneStore(self.data)
        pristine = json.loads(store.state_path.read_text(encoding="utf-8"))
        cases = [
            {"scene": {"revision": 1, "objects": [{"id": "existing-model"}]}},
            {"sessions": {"a": {"reference_images": [{"url": "/media/old.png"}]}}},
            {"sessions": {"a": {"reference_clip": {"frames": []}}}},
            {"workspace": {"thread_id": "existing-task"}},
            {"workspace": {"created_thread_ids": ["owned-task"]}},
            {"workspace": {"created_thread_specs": {"owned-task": {}}}},
            {"workspace": {"queue": [{"status": "queued"}]}},
            {"workspace": {"project_dir": str(self.root / "other-project")}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                state = {**pristine, **changes}
                store.state_path.write_text(json.dumps(state), encoding="utf-8")
                before = store.state_path.read_bytes()
                with patch("start_blank_workbench.make_server") as make:
                    with self.assertRaisesRegex(ValueError, "新的 --data-dir"):
                        self.open()
                    make.assert_not_called()
                self.assertEqual(store.state_path.read_bytes(), before)
        store.state_path.write_text(json.dumps(pristine), encoding="utf-8")
        (self.data / "codex_app_server_thread.json").write_text('{"thread_id":"existing-task"}')
        before = store.state_path.read_bytes()
        with patch("start_blank_workbench.make_server") as make:
            with self.assertRaisesRegex(ValueError, "已有 Codex 任务"):
                self.open()
            make.assert_not_called()
        self.assertEqual(store.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
