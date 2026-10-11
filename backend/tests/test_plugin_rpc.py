"""Isolated plugin RPC -> feedback -> webhook -> multimodal tool integration."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from core import SceneStore, APIError
from gateway import WorkspaceGateway
from mcp_events import MCPEvents
from plugin_bridge import Bridge
from server import make_server

SECRET_BYTES = b"p" * 32
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()
ROOT = Path(__file__).resolve().parents[2]


class PluginRPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.calls = []
        self.challenge_valid = True

        def receiver(url, body, headers):
            self.calls.append((body, headers.copy()))
            decoded = json.loads(body)
            if decoded.get("type") == "verification":
                return 200, json.dumps({"challenge": decoded["challenge"] if self.challenge_valid else "wrong"}).encode()
            return 202, b"{}"

        def factory(store, project_id, **kwargs):
            return MCPEvents(store, project_id, receiver, **kwargs)

        with patch("mcp_events.MCPEvents", factory):
            self.server = make_server(port=0, data_dir=self.root / "data", project_dir=self.project,
                                      web_dir=ROOT / "web", feedback_transport="mcp_events")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.gateway = self.server.workspace_gateway
        self.store = self.server.scene_store
        self.workspace = self.gateway.state()
        self.session_id = self.workspace["session_id"]
        self.project_id = self.workspace["project_id"]
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.url = f"{self.base}/p/{self.project_id}/mcp"
        self.sub = {"name": "visual_feedback.submitted", "arguments": {"session_id": self.session_id},
                    "delivery": {"mode": "webhook", "url": "https://receiver.example/events", "secret": SECRET}, "cursor": None}
        self.env = {"SCENE_FEEDBACK_PROJECT_ID": self.project_id, "SCENE_FEEDBACK_DATA_DIR": str(self.store.data_dir),
                    "SCENE_FEEDBACK_PROJECT_DIR": str(self.project), "SCENE_FEEDBACK_PORT": str(self.server.server_port)}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def rpc(self, method, params=None, *, token=None, url=None):
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + (token or self.store.control_token)}
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(url or self.url, data=json.dumps(request).encode(), headers=headers)) as response:
            return json.load(response)

    def submit(self, *, key="plugin-first-packet"):
        output = io.BytesIO()
        Image.new("RGB", (12, 8), "white").save(output, "PNG")
        image_url = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
        source = self.project / "reference.png"
        source.write_bytes(output.getvalue())
        reference = self.gateway.add_reference_paths([str(source)])["reference_images"][0]
        return self.gateway.submit(self.session_id, {"idempotency_key": key, "scene_revision": self.store.scene()["revision"],
                "note": "请把 [[annotation:line-1]] 附近的柜子改高。",
                "annotations": [{"id": "line-1", "type": "line", "pane": "reference", "reference_image_id": reference["id"],
                                 "coordinates": {"x": .1, "y": .2, "x2": .8, "y2": .2}}],
                "reference_annotated_data_urls": [{"reference_id": reference["id"], "data_url": image_url}],
                "scene_original_data_url": image_url, "scene_annotated_data_url": image_url})

    def wait_for_delivery(self, feedback_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.gateway.mcp_events.feedback_status(feedback_id)["status"] == "event_delivered":
                return
            time.sleep(.01)
        self.fail("event callback not delivered")

    def test_discovery_initialize_catalog_and_immediate_review(self):
        discovery = self.rpc("server/discover")["result"]
        self.assertEqual(discovery["supportedVersions"], ["2026-07-28"])
        self.assertIn("events", discovery["capabilities"])
        initialized = self.rpc("initialize", {"protocolVersion": "2025-11-25"})["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-11-25")
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertIn("workspace_get_feedback", [item["name"] for item in tools])
        self.assertNotIn("wait_visual_feedback", [item["name"] for item in tools])
        result = self.rpc("tools/call", {"name": "request_visual_feedback", "arguments": {}})["result"]["structuredContent"]
        self.assertEqual(result["event"]["arguments"]["session_id"], self.session_id)
        self.assertEqual(result["event_delivery"]["subscriber_count"], 0)
        self.assertIsNone(self.gateway.adapter)
        self.assertIsNone(self.gateway._worker_thread)

    def test_saved_feedback_then_subscription_signature_and_real_image_blocks(self):
        packet = self.submit()
        self.assertEqual(packet["delivery"]["status"], "event_unsubscribed")
        subscribed = self.rpc("events/subscribe", self.sub)["result"]
        self.wait_for_delivery(packet["feedback_id"])
        events = [(body, headers) for body, headers in self.calls if "type" not in json.loads(body)]
        self.assertEqual(len(events), 1)
        body, headers = events[0]
        event = json.loads(body)
        self.assertEqual(event["data"]["feedback_id"], packet["feedback_id"])
        self.assertEqual(headers["X-MCP-Subscription-Id"], subscribed["id"])
        signed = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
        signature = "v1," + base64.b64encode(hmac.new(SECRET_BYTES, signed, hashlib.sha256).digest()).decode()
        self.assertEqual(headers["webhook-signature"], signature)
        self.assertNotIn("scene_original_url", event["data"])
        result = self.rpc("tools/call", {"name": "workspace_get_feedback", "arguments": {"feedback_id": packet["feedback_id"], "include_details": True}})["result"]
        images = [block for block in result["content"] if block["type"] == "image"]
        self.assertEqual(len(images), 1)  # Identical fixture bytes are sent once with source aliases.
        for block in images:
            with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as opened:
                self.assertEqual(opened.size, (12, 8))
        feedback = result["structuredContent"]["items"][0]
        self.assertTrue(Path(feedback["scene_original_path"]).is_relative_to(self.store.data_dir))
        self.assertEqual(self.gateway.state()["queue"][0]["status"], "event_delivered")
        self.assertIsNone(self.gateway.state()["queue"][0]["turn_id"])
        duplicate = self.submit()
        self.assertEqual(duplicate["feedback_id"], packet["feedback_id"])
        self.assertEqual(duplicate["delivery"]["status"], "event_delivered")
        self.assertEqual(len(events), 1)

    def test_project_authentication_and_failed_verification_are_protocol_errors(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.rpc("events/list", token=self.store.browser_token)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        invalid = {**self.sub, "arguments": {"session_id": "0" * 32}}
        self.assertIn("error", self.rpc("events/subscribe", invalid))
        self.challenge_valid = False
        failed = self.rpc("events/subscribe", self.sub)
        self.assertEqual(failed["error"]["code"], -32015)
        self.assertEqual(failed["error"]["data"]["reason"], "challenge_failed")
        self.assertEqual(self.gateway.mcp_events.status(self.session_id)["subscriber_count"], 0)
        response = self.rpc("tools/call", {"name": "workspace_get_feedback", "arguments": {"feedback_id": "missing"}})
        self.assertEqual(response["error"]["code"], -32602)
        response = self.rpc("tools/call", {"name": "workspace_get_feedback", "arguments": {"feedback_id": "0" * 32}})
        self.assertTrue(response["result"]["isError"])
        public = json.dumps(self.gateway.state())
        self.assertNotIn(SECRET, public)
        self.assertNotIn("receiver.example", public)

    def test_bridge_stdio_roundtrip_and_wrong_workspace_refused(self):
        bridge = Bridge(self.env)
        bridge.check_workspace()
        request = {"jsonrpc": "2.0", "id": "stdio", "method": "tools/list"}
        self.assertIn("tools", bridge.forward(request)["result"])
        result = subprocess.run([sys.executable, str(ROOT / "scripts/plugin_bridge.py")],
                                input=json.dumps(request) + "\n", text=True, capture_output=True,
                                env={**os.environ, **self.env}, timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout)["id"], "stdio")
        with self.assertRaises(ValueError):
            Bridge({})
        wrong = self.root / "different_project"
        wrong.mkdir()
        mismatch = Bridge({**self.env, "SCENE_FEEDBACK_PROJECT_DIR": str(wrong)})
        with self.assertRaises(ValueError):
            mismatch.check_workspace()
        with self.assertRaises(ValueError):
            Bridge({**self.env, "SCENE_FEEDBACK_MCP_URL": f"http://192.168.1.2/p/{self.project_id}/mcp"})

    def test_event_restart_recovers_only_tagged_feedback_and_refuses_legacy_mode(self):
        packet = self.submit()
        self.gateway.mcp_events.close()
        replacement = MCPEvents(self.store, self.project_id, self.gateway.mcp_events._send_webhook)
        self.gateway.mcp_events = replacement
        self.gateway.start()
        self.assertEqual(replacement.feedback_status(packet["feedback_id"])["status"], "event_unsubscribed")
        with self.assertRaises(APIError):
            WorkspaceGateway(self.store, self.project, external_review=True).ensure()
        with self.assertRaises(ValueError):
            WorkspaceGateway(self.store, self.project, external_review=True, adapter=object(), feedback_transport="mcp_events")
        with self.assertRaises(APIError):
            self.gateway.take_external_feedback(self.session_id, 0)

    def test_crash_between_feedback_save_and_event_save_recovers_on_service_restart(self):
        with patch.object(self.gateway.mcp_events, "emit_feedback", side_effect=OSError("simulated storage failure")):
            with self.assertRaises(OSError):
                self.submit(key="crash-before-event-save")
        saved = self.store.workspace()["queue"][0]
        self.assertEqual(saved["feedback_transport"], "mcp_events")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        sender = self.gateway.mcp_events._send_webhook
        def factory(store, project_id, **kwargs):
            return MCPEvents(store, project_id, sender, **kwargs)
        with patch("mcp_events.MCPEvents", factory):
            self.server = make_server(port=0, data_dir=self.root / "data", project_dir=self.project,
                                      web_dir=ROOT / "web", feedback_transport="mcp_events")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.gateway, self.store = self.server.workspace_gateway, self.server.scene_store
        self.url = f"http://127.0.0.1:{self.server.server_port}/p/{self.project_id}/mcp"
        self.assertEqual(self.gateway.state()["session_id"], self.session_id)
        self.assertEqual(self.gateway.state()["event_delivery"]["waiting_count"], 1)
        self.rpc("events/subscribe", self.sub)
        self.wait_for_delivery(saved["feedback_id"])
        self.assertEqual(self.gateway.state()["queue"][0]["status"], "event_delivered")

    def test_explicit_event_task_choices_keep_default_route_and_preserve_legacy_workspace(self):
        for function, args in [(self.gateway.create_target, ("",)), (self.gateway.switch_target, ("task",))]:
            with self.assertRaises(APIError) as caught:
                function(*args)
            self.assertEqual(caught.exception.status, 400)
        bridge = Mock()
        bridge.list_models.return_value = []
        with patch("gateway.SharedThreadBridge.discover_loaded_threads", return_value=[]), \
             patch("gateway.SharedThreadBridge.connect_to_desktop", return_value=bridge):
            self.assertEqual(self.gateway.list_targets()["targets"], [])
            self.assertEqual(self.gateway.list_models()["models"], [])
        self.assertIsNone(self.gateway.adapter)
        self.assertIsNone(self.gateway.state()["thread_id"])
        self.assertEqual(self.gateway.feedback_transport, "mcp_events")
        legacy_store = SceneStore(self.root / "legacy-data")
        legacy = WorkspaceGateway(legacy_store, self.project, external_review=True)
        legacy.ensure()
        with legacy_store.lock:
            legacy_store.state["workspace"]["queue"].append({"feedback_id": "0" * 32, "status": "delivery_uncertain"})
            legacy_store._save()
        before = (legacy_store.data_dir / "state.json").read_bytes()
        with self.assertRaises(APIError):
            WorkspaceGateway(legacy_store, self.project, external_review=True, feedback_transport="mcp_events").ensure()
        self.assertEqual(before, (legacy_store.data_dir / "state.json").read_bytes())
        self.assertNotIn("feedback_transport", legacy_store.workspace())

    def test_notifications_no_execution_and_source_paths_stay_project_scoped(self):
        bridge = Bridge(self.env)
        self.assertIsNone(bridge.forward({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        source = self.root / "outside.png"
        source.write_bytes(b"not an image")
        response = self.rpc("tools/call", {"name": "request_visual_feedback", "arguments": {"reference_images": [str(source)]}})
        self.assertTrue(response["result"]["isError"])
        with self.assertRaises(APIError):
            self.store.feedback_by_id("0" * 32)


if __name__ == "__main__":
    unittest.main()
