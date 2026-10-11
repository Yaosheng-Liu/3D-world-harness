"""Similar-scene references preserve evidence and isolate owners and tasks."""

from __future__ import annotations

import base64
import copy
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import APIError, MAX_MODEL_BYTES, SceneStore
from feedback_summary import model_input_plan
from gateway import WorkspaceGateway
from mcp_server import _visual_tool_result
from scene_references import SceneReferences
from server import make_server
import scene_references
import test_folder_import as _folders

ROOT = Path(__file__).resolve().parents[2]


def picture(color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def camera():
    return {"position": [0, -3, 2], "target": [0, 0, 0], "up": [0, 0, 1], "fov": 44, "aspect": 1}


class SceneReferenceTests(unittest.TestCase):
    def setUp(self):
        self.harness = _folders.FolderImportTests("test_underscore_standard_manifest_is_accepted")
        self.harness.setUp()
        self.source_root, marker, document = self.harness.fixture("source")
        document["blend"] = "output/scene.blend"
        marker.write_text(json.dumps(document))
        result = self.harness.import_path(self.source_root)
        self.source = self.harness.registry.get(result["projects"][0]["project_id"])
        self.receiver = self.harness.registry.root
        self.session = self.receiver.store.workspace()["session_id"]
        self.archive = SceneReferences(self.receiver.store)

    def tearDown(self):
        self.harness.tearDown()

    def capture(self, source=None, receiver=None):
        return self.harness.registry.capture_scene_reference(receiver or self.receiver,
            {"project_id": (source or self.source).project_id})

    def payload(self, reference, *, preview=True):
        item = {"id": reference["id"]}
        if preview:
            item.update(preview_data_url=picture(), preview_camera=camera(), time_sec=0)
        return {"scene_revision": self.receiver.store.scene()["revision"],
                "note": f"参考 [[scene:{reference['id']}]] 的结构。", "scene_refs": [item]}

    def files(self, store):
        return {str(path.relative_to(store.data_dir)): path.read_bytes() for path in store.data_dir.rglob("*") if path.is_file()}

    def test_capture_copies_only_visual_evidence_and_preserves_source_receiver_and_tasks(self):
        source_store = self.source.store
        original = source_store.scene()["objects"][0]
        source_store.state["scene"]["objects"].append({**copy.deepcopy(original), "id": "second_instance", "position": [2, 0, 0]})
        source_store.state["scene"]["objects"][0]["metadata"].update(up_axis="z", control_token=source_store.control_token,
            browser_capability=source_store.browser_token, thread_id="private-thread-marker", conversation="private-conversation-marker")
        source_store.state["scene"]["private_prompt"] = "private-scene-prompt"
        source_store._save()
        before_source = self.files(source_store)
        before_state = self.receiver.store.state_path.read_bytes()
        with patch.object(self.source.gateway, "ensure", side_effect=AssertionError("read source gateway")), \
             patch.object(self.receiver.store, "import_model", side_effect=AssertionError("edited receiver scene")):
            reference = self.capture()
        self.assertEqual(self.files(source_store), before_source)
        self.assertEqual(self.receiver.store.state_path.read_bytes(), before_state)
        self.assertEqual(len(reference["assets"]), 1, "same source asset was copied for each instance")
        self.assertEqual(len(reference["scene"]["objects"]), 2)
        self.assertEqual(reference["scene"]["objects"][0]["metadata"], {"up_axis": "z"})
        self.assertEqual(reference["scene"]["objects"][0]["url"], reference["scene"]["objects"][1]["url"])
        self.assertTrue(reference["scene"]["objects"][0]["url"].startswith("/p/" + self.receiver.project_id + "/assets/"))
        self.assertTrue(reference["source_reference_image"]["url"].startswith("/p/" + self.receiver.project_id + "/media/"))
        self.assertEqual(Path(reference["assets"][0]["path"]).read_bytes(), _folders.glb_bytes())
        self.assertTrue(Path(reference["snapshot_json_path"]).is_file())
        self.assertEqual(reference["source_blend_path"], str(self.source_root / "output" / "scene.blend"))
        encoded = json.dumps(reference)
        for private in (source_store.control_token, source_store.browser_token, "private-thread-marker", "private-conversation-marker", "private-scene-prompt"):
            self.assertNotIn(private, encoded)
        self.harness.assert_no_tasks()

    def test_repeated_capture_reuses_id_and_source_gt_changes_create_a_new_snapshot(self):
        first = self.capture()
        before = self.files(self.receiver.store)
        self.assertEqual(self.capture(), first)
        self.assertEqual(self.files(self.receiver.store), before)
        source_session = self.source.store.workspace()["session_id"]
        source_image = self.source.store.get_session(source_session)["reference_images"][0]
        original_revision = self.source.store.scene()["revision"]
        self.source.store.set_reference_cameras(source_session, [{"reference_id": source_image["id"], "camera": _folders.camera(1)}])
        newer = self.capture()
        self.assertEqual(self.source.store.scene()["revision"], original_revision)
        self.assertNotEqual(newer["id"], first["id"])
        self.assertNotIn("camera", self.archive.get(first["id"])["source_reference_image"])
        self.assertEqual(newer["source_reference_image"]["camera"]["camera_to_world"][0][3], 1)

    def test_preview_cache_reuses_stable_hashes_but_feedback_rereads_every_file(self):
        reference = self.capture()
        with patch("scene_references._digest", wraps=scene_references._digest) as digest:
            self.assertEqual(self.archive.get(reference["id"]), reference)
            self.assertEqual(digest.call_count, 2)
            for _ in range(3):
                self.assertEqual(self.capture(), reference)
                self.assertEqual(SceneReferences(self.receiver.store).get(reference["id"]), reference)
            self.assertEqual(digest.call_count, 2, "preview reread unchanged GLB and GT bytes")
            self.receiver.store.submit_feedback(self.session, self.payload(reference, preview=False))
            self.assertEqual(digest.call_count, 4, "feedback reused preview hashes instead of reading evidence")
            restored = SceneStore(self.receiver.store.data_dir)
            SceneReferences(restored).get(reference["id"])
            self.assertEqual(digest.call_count, 6, "process restart trusted a persisted cache")

    def test_snapshot_and_submission_survive_source_update_and_unload(self):
        reference = self.capture()
        archived = self.archive.get(reference["id"])
        old_revision = reference["source_scene_revision"]
        self.source.store.update_scene(old_revision, [{"op": "update", "object_id": self.source.store.scene()["objects"][0]["id"], "fields": {"name": "Changed later"}}])
        self.harness.registry.unload(self.source.project_id)
        self.assertEqual(self.archive.get(reference["id"]), archived)
        packet = self.receiver.store.submit_feedback(self.session, self.payload(reference))
        self.assertEqual(packet["scene_refs"][0]["source_scene_revision"], old_revision)
        self.assertEqual(self.receiver.store.scene()["revision"], 1)
        restored = SceneStore(self.receiver.store.data_dir).feedback_by_id(packet["feedback_id"])
        self.assertEqual(restored, packet)
        self.assertEqual(restored["inline_references"][0]["kind"], "scene")

    def test_self_unloaded_unavailable_and_client_paths_are_rejected(self):
        for payload in ({"project_id": self.receiver.project_id}, {"project_id": "../scene"},
                        {"project_id": self.source.project_id, "scene": {}}, {"project_id": uuid.uuid4().hex}):
            with self.subTest(payload=payload), self.assertRaises(APIError):
                self.harness.registry.capture_scene_reference(self.receiver, payload)
        self.harness.registry.unload(self.source.project_id)
        with self.assertRaises(APIError) as caught:
            self.capture()
        self.assertEqual(caught.exception.status, 404)

    def test_owner_session_deleted_and_forged_reference_ids_cannot_submit(self):
        reference = self.capture()
        _, _, _ = self.harness.fixture("other")
        imported = self.harness.import_path()
        other = next(self.harness.registry.get(item["project_id"]) for item in imported["projects"] if item["project_id"] != self.source.project_id)
        with self.assertRaises(APIError) as caught:
            SceneReferences(other.store).get(reference["id"])
        self.assertEqual(caught.exception.status, 404)
        base = self.payload(reference, preview=False)
        bad = [{"id": uuid.uuid4().hex}, {"id": "../reference"}, {"id": {}}, {"id": []},
               {"id": reference["id"], "snapshot_json_path": "/etc/passwd"},
               {"id": reference["id"], "source_scene_revision": 999}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(APIError) as caught:
                self.receiver.store.submit_feedback(self.session, {**base, "scene_refs": [item]})
            if isinstance(item["id"], (dict, list)):
                self.assertEqual(caught.exception.status, 400)
        before = self.receiver.store.state_path.read_bytes()
        with self.assertRaises(APIError):
            self.receiver.store.submit_feedback(self.session, {**base, "note": "not cited"})
        with self.assertRaises(APIError):
            self.receiver.store.submit_feedback(self.session, {**base, "scene_refs": [base["scene_refs"][0]] * 2})
        self.assertEqual(before, self.receiver.store.state_path.read_bytes())
        old_session = self.session
        self.receiver.store.state["workspace"]["session_id"] = self.receiver.store.create_session()["session_id"]
        with self.assertRaises(APIError):
            self.archive.get(reference["id"])
        self.receiver.store.state["workspace"]["session_id"] = old_session
        (self.receiver.store.data_dir / "scene_references" / reference["id"] / "reference.json").unlink()
        with self.assertRaises(APIError):
            self.receiver.store.submit_feedback(self.session, base)

    def test_preview_image_camera_and_uncited_limits_fail_before_writing_feedback(self):
        reference = self.capture()
        base = self.payload(reference)
        failures = [
            {"preview_camera": {**camera(), "aspect": 2}},
            {"preview_camera": {**camera(), "position": [0, 0, 0]}},
            {"preview_camera": {**camera(), "fov": float("nan")}},
            {"preview_data_url": "data:image/png;base64,YmFk"},
            {"time_sec": -1},
        ]
        before = self.files(self.receiver.store)
        for mutation in failures:
            with self.subTest(mutation=mutation), self.assertRaises(APIError):
                self.receiver.store.submit_feedback(self.session, {**base, "scene_refs": [{**base["scene_refs"][0], **mutation}]})
        with self.assertRaises(APIError):
            self.receiver.store.submit_feedback(self.session, {**base, "scene_refs": base["scene_refs"] * 5})
        with patch("scene_references.MAX_FEEDBACK_BYTES", 1), self.assertRaises(APIError):
            self.receiver.store.submit_feedback(self.session, base)
        self.assertEqual(self.files(self.receiver.store), before)

    def test_failed_capture_rolls_back_only_its_files_and_byte_budget_is_preflight(self):
        before_source = self.files(self.source.store)
        before_receiver = self.files(self.receiver.store)
        original = scene_references._json
        def failed(path, value):
            if path.name == "reference.json":
                raise OSError("snapshot publication failed")
            return original(path, value)
        with patch("scene_references._json", side_effect=failed), self.assertRaises(OSError):
            self.capture()
        self.assertEqual(self.files(self.source.store), before_source)
        self.assertEqual(self.files(self.receiver.store), before_receiver)
        with patch("scene_references.MAX_CAPTURE_BYTES", 1), self.assertRaises(APIError) as caught:
            self.capture()
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.files(self.receiver.store), before_receiver)
        source_url = self.source.store.scene()["objects"][0]["url"]
        source_path = self.source.store.assets_dir / source_url.rsplit("/", 1)[-1]
        source_path.write_bytes(b"bad GLB")
        before_invalid = self.files(self.receiver.store)
        with self.assertRaises(APIError) as caught:
            self.capture()
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.files(self.receiver.store), before_invalid, "invalid copied GLB left receiver files")
        with source_path.open("wb") as handle:
            handle.truncate(MAX_MODEL_BYTES + 1)
        with patch.object(self.receiver.store, "_copy_glb", side_effect=AssertionError("copied an oversized source")), \
             self.assertRaises(APIError) as caught:
            self.capture()
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse(self.archive.root.exists() and list(self.archive.root.iterdir()))

    def test_same_size_archived_model_and_gt_damage_are_detected_and_never_silently_replaced(self):
        for field in ("assets", "source_reference_image"):
            with self.subTest(field=field):
                reference = self.capture()
                item = reference[field][0] if field == "assets" else reference[field]
                path = Path(item["path"])
                self.archive.get(reference["id"])
                stamp = path.stat()
                data = path.read_bytes()
                os.chmod(path, 0o600)
                path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                os.chmod(path, stamp.st_mode & 0o777)
                with self.assertRaises(APIError):
                    self.archive.get(reference["id"])
                with self.assertRaises(APIError):
                    self.receiver.store.submit_feedback(self.session, self.payload(reference))
                fresh = self.capture()
                self.assertNotEqual(fresh["id"], reference["id"])
                self.assertEqual(path.read_bytes()[0], data[0] ^ 1, "old archive was silently restored from the source")

    def test_mcp_and_direct_plans_send_only_cited_snapshot_paths_preview_and_gt(self):
        reference = self.capture()
        self.receiver.store.add_reference(self.session, "Unrelated current GT", picture("green"))
        payload = self.payload(reference)
        payload["note"] += " 再看一次 [[scene:" + reference["id"] + "]]。"
        packet = self.receiver.store.submit_feedback(self.session, payload)
        plan = model_input_plan(packet, self.receiver.store.data_dir)
        self.assertEqual(len(packet["scene_refs"]), 1)
        self.assertNotIn("scene", packet["scene_refs"][0])
        self.assertEqual(len(plan["images"]), 2)
        self.assertEqual({image["source"] for image in plan["images"]}, {"C1"})
        self.assertEqual(plan["manifest"]["scene_refs"][0]["glb_paths"], [reference["assets"][0]["path"]])
        self.assertNotIn("reference_images", plan["manifest"])
        self.assertNotIn("objects", json.dumps(plan["manifest"]["scene_refs"]))
        message, paths = self.receiver.gateway._turn_input(packet)
        self.assertEqual(paths, [image["path"] for image in plan["images"]])
        self.assertIn("只修改当前项目", message)
        result = _visual_tool_result({"items": [packet]}, self.receiver.store.data_dir)
        self.assertEqual(sum(part.type == "image" for part in result.content), 2)
        self.assertEqual(result.structured_content["items"][0]["scene_refs"][0]["source_scene_revision"], reference["source_scene_revision"])
        self.assertTrue(any("never its source scene" in part.text for part in result.content if part.type == "text"))
        detailed = _visual_tool_result({"items": [packet]}, self.receiver.store.data_dir, include_details=True)
        self.assertEqual(detailed.structured_content["items"][0]["scene_refs"][0]["preview_path"], packet["scene_refs"][0]["preview_path"])

    def test_source_lease_prevents_unload_during_copy_and_gt_only_fallback_is_usable(self):
        original = self.receiver.store._copy_glb
        calls = []
        def copy_with_unload_probe(path):
            with self.assertRaises(APIError) as caught:
                self.harness.registry.unload(self.source.project_id)
            self.assertEqual(caught.exception.status, 409)
            calls.append(True)
            return original(path)
        with patch.object(self.receiver.store, "_copy_glb", side_effect=copy_with_unload_probe):
            self.capture()
        self.assertTrue(calls)
        self.source.store.replace_scene(self.source.store.scene()["revision"], [])
        reference = self.capture()
        self.assertFalse(reference["assets"])
        packet = self.receiver.store.submit_feedback(self.session, self.payload(reference, preview=False))
        self.assertNotIn("preview_url", packet["scene_refs"][0])
        self.assertEqual(len(model_input_plan(packet, self.receiver.store.data_dir)["images"]), 1)


class SceneReferenceHTTPTests(unittest.TestCase):
    def test_receiver_capability_owner_urls_and_unloaded_source_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            sources = project / "sources"
            sources.mkdir()
            for name in ("receiver", "source"):
                _folders.FolderImportTests.fixture(SimpleNamespace(source=sources), name)
            server = make_server(port=0, project_dir=project, data_dir=root / "data", web_dir=ROOT / "web", external_review=True, feedback_transport="mcp_events")
            imported = server.project_registry.import_folder({"path": str(sources), "request_id": uuid.uuid4().hex})
            contexts = {item["name"]: server.project_registry.get(item["project_id"]) for item in imported["projects"]}
            receiver, source = contexts["receiver"], contexts["source"]
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                endpoint = f"/p/{receiver.project_id}/api/workspace/scene-references"
                body = json.dumps({"project_id": source.project_id})
                connection.request("POST", endpoint, body=body, headers={"Content-Type": "application/json", "X-Workspace-Capability": source.store.browser_token})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                connection.request("POST", endpoint, body=body, headers={"Content-Type": "application/json", "X-Workspace-Capability": receiver.store.browser_token})
                response = connection.getresponse()
                reference = json.loads(response.read())
                self.assertEqual(response.status, 201, reference)
                self.assertTrue(reference["scene"]["objects"][0]["url"].startswith("/p/" + receiver.project_id + "/assets/"))
                server.project_registry.unload(source.project_id)
                connection.request("GET", endpoint + "/" + reference["id"])
                response = connection.getresponse()
                self.assertEqual(response.status, 200, response.read())
                response.read()
                connection.request("GET", reference["scene"]["objects"][0]["url"])
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), _folders.glb_bytes())
                self.assertFalse(receiver.store.state["feedback"])
                self.assertIsNone(receiver.gateway.adapter)
            finally:
                connection.close()
                server.shutdown()
                worker.join(5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
