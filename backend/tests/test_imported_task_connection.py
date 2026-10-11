"""Imported event scenes connect only after an explicit task action."""

from __future__ import annotations

import copy
import http.client
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import APIError, SceneStore
from gateway import WorkspaceGateway
from server import make_server
import test_folder_import as _folders
from test_target_switch import TargetAdapter as _TargetAdapter, NEW

ROOT = Path(__file__).resolve().parents[2]
SOCKET = Path("/tmp/imported-task-fixture-desktop")


class DesktopFixture:
    """Fake existing Desktop transport; never reaches the actual daemon."""

    def __init__(self, project):
        self.project = project
        self.records = [{"id": NEW, "name": "Existing user task", "cwd": str(project),
                         "status": {"type": "idle"}, "model": "gpt-6-astra", "reasoningEffort": "ultra"}]
        self.adapters = []
        self.created = []
        self.connections = []

    def catalog(self):
        return [(SOCKET, copy.deepcopy(record)) for record in self.records]

    def bridge(self, thread_id, **_kwargs):
        self.connections.append(thread_id)
        fixture = self

        class Bridge:
            socket_path = SOCKET

            def __init__(self):
                self.thread_id = thread_id

            def list_models(self):
                return [{"model": "gpt-6-astra", "inputModalities": ["text", "image"],
                         "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}],
                         "defaultReasoningEffort": "ultra", "isDefault": True}]

            def create_thread(self, model, cwd, **options):
                self.thread_id = str(uuid.uuid4())
                fixture.created.append({"thread_id": self.thread_id, "model": model, "cwd": str(cwd), **options})
                fixture.records.append({"id": self.thread_id, "name": options.get("title"), "cwd": str(cwd),
                                        "status": {"type": "idle"}, "model": model})
                return self.thread_id

            def read_thread(self):
                return next(copy.deepcopy(record) for record in fixture.records if record["id"] == self.thread_id)

            def read_loaded_thread(self, task_id):
                return next(copy.deepcopy(record) for record in fixture.records if record["id"] == task_id)

            def close(self):
                pass

        return Bridge()

    def adapter(self, thread_id, on_event=None, *, initial_bridge=None, **options):
        adapter = _TargetAdapter(thread_id, on_event, **options)
        adapter.initial_bridge = initial_bridge
        self.adapters.append(adapter)
        return adapter


class ImportedTaskConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.sources = self.project / "instances"
        self.sources.mkdir()
        self.desktop = DesktopFixture(self.project)
        self.patches = [patch("gateway.SharedThreadBridge.discover_loaded_threads", side_effect=self.desktop.catalog),
                        patch("gateway.SharedThreadBridge.connect_to_desktop", side_effect=self.desktop.bridge),
                        patch("gateway.SharedDesktopAdapter", side_effect=self.desktop.adapter),
                        patch("shared_thread_adapter.SharedDesktopAdapter", side_effect=self.desktop.adapter),
                        patch("shared_thread_bridge._private_socket_candidates", return_value=[SOCKET])]
        for patched in self.patches:
            patched.start()
        self.open_server()
        self.source, _, _ = _folders.FolderImportTests.fixture(SimpleNamespace(source=self.sources), "ready")
        result = self.import_source()
        self.project_id = result["projects"][0]["project_id"]
        self.context = self.server.project_registry.get(self.project_id)
        self.gateway = self.context.gateway
        self.gateway._RECONCILE_INTERVAL_SEC = 60
        self.assertIsNone(self.gateway.adapter)
        self.assertFalse(self.desktop.created)
        self.assertFalse(self.desktop.connections)

    def open_server(self):
        self.server = make_server(port=0, data_dir=self.root / "data", project_dir=self.project,
                                  web_dir=ROOT / "web", external_review=True, feedback_transport="mcp_events")
        self.server.workspace_gateway.blank_workbench = True

    def import_source(self):
        return self.server.project_registry.import_folder({"path": str(self.sources), "request_id": uuid.uuid4().hex})

    def tearDown(self):
        self.server.server_close()
        for patched in reversed(self.patches):
            patched.stop()
        self.temporary.cleanup()

    def test_task_capabilities_and_explicit_read_choices_do_not_adopt_any_task(self):
        state = self.gateway.state()
        self.assertTrue(state["task_connection_supported"])
        self.assertTrue(state["task_creation_supported"])
        self.assertTrue(state["desktop_available"])
        self.assertFalse(state["project_creation_supported"])
        self.assertFalse(self.desktop.connections, "state polling contacted Desktop")
        self.assertEqual(self.gateway.list_targets()["thread_id"], None)
        self.assertEqual([entry["thread_id"] for entry in self.gateway.list_targets()["targets"]], [NEW])
        self.assertEqual(self.gateway.list_models()["default_model"], "gpt-6-astra")
        self.assertEqual(self.desktop.connections, ["00000000-0000-0000-0000-000000000000"])
        self.assertFalse(self.desktop.created)
        self.assertIsNone(self.gateway.adapter)
        self.assertEqual(self.gateway.state()["feedback_transport"], "mcp_events")
        self.assertIsNone(self.gateway.state()["thread_id"])

    def test_explicit_connection_routes_only_new_packets_and_keeps_old_event_retry_idempotent(self):
        session = self.gateway.ensure()["session_id"]
        old_payload = {"scene_revision": self.context.store.scene()["revision"], "idempotency_key": "old-event", "note": "old"}
        old = self.gateway.submit(session, old_payload)
        old_queue = copy.deepcopy(self.context.store.workspace()["queue"])
        old_events = copy.deepcopy(self.gateway.mcp_events._document)
        with patch.object(self.gateway, "wake"):
            connected = self.gateway.switch_target(NEW)
            new = self.gateway.submit(session, {**old_payload, "idempotency_key": "new-direct", "note": "new"})
            retry = self.gateway.submit(session, old_payload)
        self.assertEqual(connected["feedback_transport"], "legacy")
        self.assertEqual(connected["service_feedback_transport"], "mcp_events")
        self.assertEqual(self.context.store.workspace()["feedback_transport_origin"], "mcp_events")
        self.assertEqual(self.context.store.workspace()["queue"][0], old_queue[0])
        self.assertEqual(self.gateway.mcp_events._document, old_events)
        self.assertEqual(retry["feedback_id"], old["feedback_id"])
        self.assertEqual(new["delivery"]["target_thread_id"], NEW)
        self.assertNotEqual(new["delivery"].get("feedback_transport"), "mcp_events")
        self.gateway._dispatch()
        self.assertEqual(self.gateway.adapter.sent, [new["feedback_id"]])
        self.assertIsNone(self.gateway.adapter.permission_mode, "existing target permissions were overridden")
        self.assertTrue(self.gateway.adapter.allow_bound_resume)

    def test_new_task_uses_selected_permissions_and_restores_after_unload_and_restart(self):
        with patch.object(self.gateway, "wake"):
            result = self.gateway.create_target("gpt-6-astra", reasoning_effort="ultra", title="Reconstruct scene", permission_mode="full_access")
        target = result["thread_id"]
        self.assertEqual(self.desktop.created[0]["cwd"], str(self.source))
        self.assertEqual(self.desktop.created[0]["permission_mode"], "full_access")
        config = self.desktop.created[0]["config"]["mcp_servers"]["scene_feedback"]["env"]
        self.assertEqual(config["SCENE_FEEDBACK_DATA_DIR"], str(self.context.store.data_dir))
        self.assertEqual(self.gateway.adapter.permission_mode, "full_access")
        before_scene = self.context.store.scene()
        self.server.project_registry.unload(self.project_id)
        restored = self.import_source()
        context = self.server.project_registry.get(self.project_id)
        self.assertEqual(context.gateway.adapter.thread_id, target)
        self.assertTrue(context.gateway.adapter.allow_owned_resume)
        self.assertEqual(context.gateway.adapter.permission_mode, "full_access")
        self.assertEqual(context.store.scene(), before_scene)
        self.assertEqual(context.gateway.state()["feedback_transport"], "legacy")
        self.assertIsNotNone(context.gateway.mcp_events)
        self.server.server_close()
        self.open_server()
        context = self.server.project_registry.get(self.project_id)
        self.assertEqual(context.gateway.adapter.thread_id, target)
        self.assertEqual(context.gateway.adapter.permission_mode, "full_access")
        self.assertEqual(context.gateway.state()["feedback_transport"], "legacy")
        self.assertEqual(self.server.workspace_gateway.state()["feedback_transport"], "mcp_events")
        self.assertEqual(len(self.desktop.created), 1, "restoration created another task")

    def test_active_target_and_active_feedback_are_refused_without_migrating_route(self):
        self.desktop.records[0]["status"] = {"type": "active"}
        with self.assertRaisesRegex(APIError, "active"):
            self.gateway.switch_target(NEW)
        self.assertEqual(self.gateway.feedback_transport, "mcp_events")
        self.assertIsNone(self.gateway.adapter)
        self.desktop.records[0]["status"] = {"type": "idle"}
        workspace = self.context.store.state["workspace"]
        workspace["active_feedback_id"] = "0" * 32
        with self.assertRaisesRegex(APIError, "active feedback"):
            self.gateway.create_target("gpt-6-astra")
        self.assertFalse(self.desktop.created)
        self.assertEqual(self.gateway.feedback_transport, "mcp_events")

    def test_binding_save_failure_preserves_event_route_and_existing_packet(self):
        session = self.gateway.ensure()["session_id"]
        self.gateway.submit(session, {"scene_revision": self.context.store.scene()["revision"], "idempotency_key": "before-save-failure"})
        before = copy.deepcopy(self.context.store.state["workspace"])
        with patch.object(self.context.store, "_save", side_effect=OSError("binding could not save")):
            with self.assertRaises(OSError):
                self.gateway.switch_target(NEW)
        self.assertEqual(self.context.store.state["workspace"], before)
        self.assertEqual(self.gateway.feedback_transport, "mcp_events")
        self.assertIsNone(self.gateway.adapter)
        self.assertTrue(self.desktop.adapters[-1].closed)

    def test_blank_home_and_other_scene_binding_remain_protected(self):
        root = self.server.workspace_gateway
        self.assertFalse(root.state()["task_connection_supported"])
        with self.assertRaisesRegex(APIError, "select an imported scene"):
            root.switch_target(NEW)
        with self.assertRaisesRegex(APIError, "select an imported scene"):
            root.create_target("gpt-6-astra")
        self.gateway.switch_target(NEW)
        _folders.FolderImportTests.fixture(SimpleNamespace(source=self.sources), "other")
        imported = self.import_source()
        other_meta = next(entry for entry in imported["projects"] if entry["project_id"] != self.project_id)
        other = self.server.project_registry.get(other_meta["project_id"])
        with self.assertRaisesRegex(APIError, "another reconstruction"):
            other.gateway.switch_target(NEW)
        self.assertEqual(other.gateway.feedback_transport, "mcp_events")
        self.assertIsNone(other.gateway.adapter)

    def test_http_target_connection_requires_the_child_capability_and_legacy_no_seed_also_works(self):
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            path = f"/p/{self.project_id}/api/workspace/target"
            body = json.dumps({"thread_id": NEW})
            connection.request("POST", path, body=body, headers={"Content-Type": "application/json", "X-Workspace-Capability": self.server.scene_store.browser_token})
            response = connection.getresponse()
            self.assertEqual(response.status, 403, response.read())
            response.read()
            connection.request("POST", path, body=body, headers={"Content-Type": "application/json", "X-Workspace-Capability": self.context.store.browser_token})
            response = connection.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, 200, data)
            self.assertEqual(data["feedback_transport"], "legacy")
            self.assertEqual(data["thread_id"], NEW)
            connection.close()
        finally:
            self.server.shutdown()
            worker.join(5)
        standalone = WorkspaceGateway(SceneStore(self.root / "legacy"), self.source, external_review=True)
        try:
            standalone.ensure()
            self.assertEqual(standalone.list_models()["default_model"], "gpt-6-astra")
            self.assertEqual(standalone.list_targets()["thread_id"], None)
            self.assertIsNone(standalone.adapter)
        finally:
            standalone.close()


if __name__ == "__main__":
    unittest.main()
