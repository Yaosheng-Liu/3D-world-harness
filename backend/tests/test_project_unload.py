"""Unloading hides a scene while retaining its evidence, identity and task."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import APIError
import test_folder_import as folder_tests


class ProjectUnloadTests(unittest.TestCase):
    def setUp(self):
        self.harness = folder_tests.FolderImportTests("test_underscore_standard_manifest_is_accepted")
        self.harness.setUp()
        self.source, _, _ = self.harness.fixture()
        result = self.harness.import_path(self.source)
        self.assertEqual(len(result["imported"]), 1, result)
        self.metadata = result["projects"][0]
        self.project_id = self.metadata["project_id"]
        self.context = self.registry.get(self.project_id)

    @property
    def registry(self):
        return self.harness.registry

    def tearDown(self):
        self.harness.tearDown()

    def assert_error(self, status, function, *args):
        with self.assertRaises(APIError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, status, caught.exception)
        return caught.exception

    def restart(self):
        self.registry.close()
        self.harness.open_registry()

    def test_unload_hides_context_without_deleting_files_or_touching_root(self):
        data_before = {str(path): path.read_bytes() for path in self.context.store.data_dir.rglob("*")
                       if path.is_file() and path.name != ".scene_feedback.lock"}
        source_before = {str(path): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        root_before = self.registry.root.store.state_path.read_bytes()
        result = self.registry.unload(self.project_id)
        self.assertEqual(set(result), {"unloaded_project_id", "fallback"})
        self.assertEqual(result["fallback"]["project_id"], self.registry.root.project_id)
        catalog = self.registry.list(self.registry.root.project_id)
        self.assertTrue(catalog["project_unload_supported"])
        self.assertEqual([item["project_id"] for item in catalog["projects"]], [self.registry.root.project_id])
        self.assertFalse(catalog["projects"][0]["can_unload"])
        self.assertTrue(self.metadata["can_unload"])
        self.assert_error(404, self.registry.get, self.project_id)
        self.assert_error(403, self.registry.by_control_token, self.context.store.control_token)
        self.assertFalse(self.registry.accepts_browser_token(self.context.store.browser_token))
        self.assertEqual({str(path): path.read_bytes() for path in self.context.store.data_dir.rglob("*")
                          if path.is_file() and path.name != ".scene_feedback.lock"}, data_before)
        self.assertEqual({str(path): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}, source_before)
        self.assertEqual(self.registry.root.store.state_path.read_bytes(), root_before)
        self.harness.assert_no_tasks()

    def test_default_and_unknown_scene_cannot_be_unloaded(self):
        before = self.registry.path.read_bytes()
        self.assert_error(409, self.registry.unload, self.registry.root.project_id)
        self.assert_error(404, self.registry.unload, uuid.uuid4().hex)
        self.assert_error(400, self.registry.unload, "../wrong")
        self.assertEqual(self.registry.path.read_bytes(), before)

    def test_repeat_unload_and_credential_retry_do_not_create_context(self):
        token = self.context.store.browser_token
        first = self.registry.unload(self.project_id)
        before = self.registry.path.read_bytes()
        calls = len(self.harness.factory_calls)
        self.assertEqual(self.registry.unload(self.project_id), first)
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertTrue(self.registry.authorize_unload_retry(self.project_id, token))
        self.assertFalse(self.registry.authorize_unload_retry(self.project_id, "wrong"))
        self.assertFalse(self.registry.authorize_unload_retry(self.registry.root.project_id, self.registry.root.store.browser_token))
        self.assertFalse(self.registry.authorize_unload_retry(uuid.uuid4().hex, token))
        self.restart()
        self.assertTrue(self.registry.authorize_unload_retry(self.project_id, token))
        self.assertEqual(len(self.harness.factory_calls), calls)
        self.assertEqual(self.registry.unload(self.project_id), first)

    def test_retry_credential_never_follows_registry_escape_or_token_symlink(self):
        token = self.context.store.browser_token
        self.registry.unload(self.project_id)
        record = self.registry._records[self.project_id]
        original_data = record["data_dir"]
        record["data_dir"] = str(self.registry.root.store.data_dir)
        self.assertFalse(self.registry.authorize_unload_retry(self.project_id, self.registry.root.store.browser_token))
        record["data_dir"] = original_data
        token_path = Path(original_data) / "browser_token"
        token_path.unlink()
        token_path.symlink_to(self.registry.root.store.browser_token_path)
        self.assertFalse(self.registry.authorize_unload_retry(self.project_id, self.registry.root.store.browser_token))
        token_path.unlink()
        token_path.write_text(token)

    def test_restart_keeps_unloaded_scene_hidden_even_when_source_missing(self):
        self.registry.unload(self.project_id)
        missing = self.source.with_name("temporarily-moved")
        self.source.rename(missing)
        try:
            calls = len(self.harness.factory_calls)
            self.restart()
            self.assertEqual(len(self.harness.factory_calls), calls)
            self.assertEqual(len(self.registry.contexts()), 1)
            self.assertFalse(self.registry._records[self.project_id]["loaded"])
            self.assert_error(404, self.registry.get, self.project_id)
        finally:
            missing.rename(self.source)

    def test_reimport_restores_same_session_edits_assets_credentials_and_task(self):
        store = self.context.store
        workspace = store.state["workspace"]
        thread_id = str(uuid.uuid4())
        workspace["thread_id"] = thread_id
        workspace["created_thread_ids"] = [thread_id]
        workspace["created_thread_specs"] = {thread_id: {"model": "gpt-6-astra", "permission_mode": "full_access"}}
        workspace["queue"] = [{"feedback_id": uuid.uuid4().hex, "status": "completed", "turn_id": "old-turn"}]
        store.state["scene"]["objects"][0]["name"] = "User edited model"
        store.state["scene"]["revision"] += 1
        store._save()
        before_state = copy.deepcopy(store.state)
        before_tokens = (store.browser_token, store.control_token)
        before_assets = {path.name: path.read_bytes() for path in store.assets_dir.iterdir() if path.is_file()}
        self.registry.unload(self.project_id)
        self.restart()
        # Discovery still recognizes this source. Reactivation uses managed
        # evidence, not any newer or invalid model bytes at the original path.
        (self.source / "output" / "scene.glb").write_bytes(b"changed source, not an imported revision")
        result = self.harness.import_path(self.source)
        self.assertEqual(len(result["imported"]), 1, result)
        restored = self.registry.get(self.project_id)
        self.assertEqual(result["projects"][0]["project_id"], self.project_id)
        self.assertEqual(restored.store.state["scene"], before_state["scene"])
        self.assertEqual(restored.store.state["sessions"], before_state["sessions"])
        for key in ("session_id", "thread_id", "created_thread_ids", "created_thread_specs", "queue"):
            self.assertEqual(restored.store.workspace()[key], before_state["workspace"][key])
        self.assertEqual((restored.store.browser_token, restored.store.control_token), before_tokens)
        self.assertEqual({path.name: path.read_bytes() for path in restored.store.assets_dir.iterdir() if path.is_file()}, before_assets)
        self.assertEqual(len(self.registry._records), 2)
        self.assertTrue(self.registry._records[self.project_id]["loaded"])
        self.assertEqual(self.harness.factory_calls[-1][1], False)
        self.harness.target_mock.assert_not_called()
        self.harness.model_mock.assert_not_called()

    def test_unavailable_child_can_be_unloaded(self):
        self.registry._close_import_context(self.context)
        self.registry._contexts.pop(self.project_id)
        self.registry._records[self.project_id].update(creation_status="unavailable", creation_error="source offline")
        self.registry._save()
        metadata = self.registry.list(self.registry.root.project_id)["projects"][1]
        self.assertTrue(metadata["can_unload"])
        self.registry.unload(self.project_id)
        self.assertFalse(self.registry._records[self.project_id]["loaded"])

    def test_busy_feedback_and_approvals_block_without_mutation(self):
        workspace = self.context.store.state["workspace"]
        original = copy.deepcopy(workspace)
        cases = [
            {"active_feedback_id": uuid.uuid4().hex}, {"approvals": [{"approval_id": "pending"}]},
            {"agent": {"status": "running"}}, {"agent": {"status": "awaiting_approval"}},
            *[{"queue": [{"feedback_id": uuid.uuid4().hex, "status": status}]} for status in
              ("queued", "awaiting_mcp", "blocked_stale", "dispatching", "running", "delivery_uncertain", "unknown")],
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                workspace.clear()
                workspace.update(copy.deepcopy(original))
                workspace.update(changes)
                registry_before = self.registry.path.read_bytes()
                self.assert_error(409, self.registry.unload, self.project_id)
                self.assertIs(self.registry.get(self.project_id), self.context)
                self.assertEqual(self.registry.path.read_bytes(), registry_before)
        workspace.clear()
        workspace.update(original)

    def test_historical_feedback_does_not_block_unload(self):
        workspace = self.context.store.state["workspace"]
        workspace["queue"] = [{"feedback_id": uuid.uuid4().hex, "status": status} for status in
                              ("completed", "failed", "interrupted", "discarded", "returned_to_mcp")]
        self.registry.unload(self.project_id)

    def test_event_feedback_checks_live_delivery_receipt(self):
        workspace = self.context.store.state["workspace"]
        item = {"feedback_id": uuid.uuid4().hex, "status": "event_pending", "feedback_transport": "mcp_events"}
        workspace["queue"] = [item]
        events = self.context.gateway.mcp_events = Mock()
        for status in ("event_pending", "event_failed", "event_unsubscribed"):
            with self.subTest(status=status):
                events.feedback_status.return_value = {"status": status}
                self.assert_error(409, self.registry.unload, self.project_id)
        events.feedback_status.return_value = {"status": "event_delivered"}
        self.registry.unload(self.project_id)
        self.assertEqual(item["status"], "event_pending")
        events.close.assert_called_once()

    def test_worker_adapter_and_creation_guards(self):
        gateway = self.context.gateway
        gateway._worker_running = True
        self.assert_error(409, self.registry.unload, self.project_id)
        gateway._worker_running = False
        adapter = gateway.adapter = Mock()
        for status in ({"connected": True, "turn_state": "active"},
                       {"connected": True, "pending_requests": [{"id": "pending"}]}):
            adapter.status.return_value = status
            self.assert_error(409, self.registry.unload, self.project_id)
        adapter.status.return_value = {"connected": True, "turn_state": "idle"}
        adapter.inspect_thread_status.return_value = "active"
        self.assert_error(409, self.registry.unload, self.project_id)
        adapter.inspect_thread_status.side_effect = RuntimeError("offline")
        self.assert_error(503, self.registry.unload, self.project_id)
        gateway.adapter = None
        record = self.registry._records[self.project_id]
        for status in ("creating", "uncertain"):
            record["creation_status"] = status
            self.assert_error(409, self.registry.unload, self.project_id)
        record["creation_status"] = "ready"

    def test_inflight_operation_blocks_then_stale_context_is_rejected(self):
        with self.registry.operation(self.context):
            self.assert_error(409, self.registry.unload, self.project_id)
        self.registry.unload(self.project_id)
        with self.assertRaises(APIError) as caught:
            with self.registry.operation(self.context):
                self.fail("stale context lease was accepted")
        self.assertEqual(caught.exception.status, 404)

    def test_gateway_lock_contention_does_not_wait_or_detach(self):
        entered, release = threading.Event(), threading.Event()

        def hold():
            with self.context.gateway._supervisor_lock:
                entered.set()
                release.wait(timeout=3)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        try:
            self.assert_error(409, self.registry.unload, self.project_id)
            self.assertIs(self.registry.get(self.project_id), self.context)
        finally:
            release.set()
            worker.join(timeout=3)

    def test_persistence_failure_rolls_back_without_closing_context(self):
        before = self.registry.path.read_bytes()
        record_before = copy.deepcopy(self.registry._records[self.project_id])
        with patch.object(self.registry, "_save", side_effect=OSError("write failed")), \
                patch.object(self.context.gateway, "close", wraps=self.context.gateway.close) as closed:
            with self.assertRaises(OSError):
                self.registry.unload(self.project_id)
            closed.assert_not_called()
        self.assertIs(self.registry.get(self.project_id), self.context)
        self.assertTrue(self.context.gateway._started)
        self.assertFalse(self.context.gateway._supervisor_stop.is_set())
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertEqual(self.registry._records[self.project_id], record_before)

    def test_reactivation_save_failure_retains_hidden_data_and_can_retry(self):
        self.registry.unload(self.project_id)
        before = self.registry.path.read_bytes()
        original_save = self.registry._save

        def fail_publication():
            if self.registry._records[self.project_id].get("loaded") is True:
                raise OSError("publication failed")
            original_save()

        with patch.object(self.registry, "_save", side_effect=fail_publication):
            result = self.harness.import_path(self.source)
        self.assertEqual(len(result["errors"]), 1, result)
        self.assertTrue(self.context.store.data_dir.is_dir())
        self.assertFalse(self.registry._records[self.project_id]["loaded"])
        self.assert_error(404, self.registry.get, self.project_id)
        # Only the new request identity was added; the retained hidden record
        # and store continue to exist and the failed factory released its lock.
        self.assertEqual(json.loads(before)["projects"], json.loads(self.registry.path.read_text())["projects"])
        result = self.harness.import_path(self.source)
        self.assertEqual(len(result["imported"]), 1, result)
        self.assertEqual(result["projects"][0]["session_id"], self.metadata["session_id"])

    def test_reimport_waits_for_close_while_catalog_remains_available(self):
        closing, release, import_started = threading.Event(), threading.Event(), threading.Event()
        original_close = self.context.gateway.close
        outcomes = {}

        def close():
            closing.set()
            release.wait(timeout=3)
            original_close()

        def unload():
            try:
                outcomes["unload"] = self.registry.unload(self.project_id)
            except BaseException as exc:
                outcomes["unload_error"] = exc

        def reimport():
            import_started.set()
            try:
                outcomes["import"] = self.harness.import_path(self.source)
            except BaseException as exc:
                outcomes["import_error"] = exc

        with patch.object(self.context.gateway, "close", side_effect=close):
            first = threading.Thread(target=unload)
            first.start()
            self.assertTrue(closing.wait(timeout=1))
            try:
                self.assertEqual(len(self.registry.list(self.registry.root.project_id)["projects"]), 1)
                self.assert_error(404, self.registry.get, self.project_id)
                second = threading.Thread(target=reimport)
                second.start()
                self.assertTrue(import_started.wait(timeout=1))
                self.assertEqual(len(self.harness.factory_calls), 1)
            finally:
                release.set()
                first.join(timeout=3)
                if "second" in locals():
                    second.join(timeout=3)
        self.assertNotIn("unload_error", outcomes)
        self.assertNotIn("import_error", outcomes)
        self.assertEqual(len(outcomes["import"]["imported"]), 1, outcomes)
        self.assertEqual(outcomes["import"]["projects"][0]["project_id"], self.project_id)
        self.assertEqual(outcomes["import"]["projects"][0]["session_id"], self.metadata["session_id"])
        self.harness.assert_no_tasks()


if __name__ == "__main__":
    unittest.main()
