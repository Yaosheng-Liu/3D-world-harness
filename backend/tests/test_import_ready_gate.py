"""Every folder import checks new sources or retained evidence before loading."""

from __future__ import annotations

import copy
import base64
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import SceneStore
import test_folder_import as _folder_import
import test_ready_check as _ready_check


class ImportReadyGateTests(unittest.TestCase):
    def setUp(self):
        self.case = _folder_import.FolderImportTests("test_static_images_without_clip_manifest_are_ready")
        self.case.setUp()
        self.source = self.case.source

    def tearDown(self):
        self.case.tearDown()

    def fixture(self, name="ready"):
        root, marker, document = self.case.fixture(name)
        (root / "output" / "scene.glb").write_bytes(_ready_check.animated_glb())
        return root, marker, document

    def context(self, result):
        return self.case.registry.get(result["projects"][0]["project_id"])

    def test_partial_batch_checks_bad_glb_and_camera_before_allocating_contexts(self):
        self.fixture("good")
        bad_glb, _, _ = self.fixture("bad_glb")
        (bad_glb / "output" / "scene.glb").write_bytes(b"broken GLB")
        bad_camera, _, _ = self.fixture("bad_camera")
        manifest = bad_camera / "references" / "multiview.json"
        document = json.loads(manifest.read_text())
        document["views"][0]["frames"][0]["camera"]["intrinsics"]["fx"] = 0
        manifest.write_text(json.dumps(document))
        result = self.case.import_path()
        self.assertEqual((len(result["imported"]), len(result["errors"])), (1, 2), result)
        self.assertEqual(result["ready_check"]["status"], "partial")
        self.assertEqual(result["ready_check"]["counters"]["blocked"], 2)
        self.assertEqual(len(self.case.factory_calls), 1)
        for source in (bad_glb, bad_camera):
            project_id = uuid.uuid5(uuid.NAMESPACE_URL, "scene-feedback-import:" + str(source)).hex
            self.assertFalse((self.case.registry.managed_dir / project_id).exists())
        self.case.assert_no_tasks()

    def test_missing_viewer_decoder_and_truncated_jpeg_block_all_imports_with_file_details(self):
        root, marker, document = self.fixture("draco")
        (root / "output" / "scene.glb").write_bytes(_ready_check.animated_glb(extensionsUsed=["KHR_draco_mesh_compression"]))
        image_root, image_marker, image_document = self.fixture("image")
        image_document.pop("reference_clip")
        image_document["reference_images"] = ["references/broken.jpg"]
        image_marker.write_text(json.dumps(image_document))
        image = io.BytesIO()
        Image.new("RGB", (24, 24), "red").save(image, format="JPEG")
        (image_root / "references" / "broken.jpg").write_bytes(image.getvalue()[:-5])
        result = self.case.import_path()
        self.assertFalse(result["ready_check"]["can_import"])
        self.assertEqual(result["ready_check"]["status"], "blocked")
        self.assertEqual(result["imported"], [])
        self.assertFalse(self.case.factory_calls)
        image_check = next(item for item in result["ready_check"]["instances"] if item["path"] == str(image_root))
        error = next(item for item in image_check["checks"] if item["status"] == "error")
        self.assertEqual(error["file"], str(image_root / "references" / "broken.jpg"))
        self.assertIn("broken.jpg", error["message"])

    def test_warning_only_model_catalog_is_allowed(self):
        root = self.source / "model_only"
        root.mkdir()
        (root / "scene.glb").write_bytes(_ready_check.animated_glb())
        (self.source / "manifest.json").write_text(json.dumps({"scenes": [
            {"name": "Model only", "folder": "model_only", "glb": "model_only/scene.glb"}]}))
        result = self.case.import_path()
        self.assertEqual(len(result["imported"]), 1, result)
        self.assertEqual(result["ready_check"]["status"], "warning")
        self.assertTrue(result["ready_check"]["can_import"])

    def test_reimport_and_reactivation_check_saved_evidence_and_keep_edits_when_source_bytes_break(self):
        root, _, _ = self.fixture()
        request_id = uuid.uuid4().hex
        first = self.case.import_path(root, request_id)
        context = self.context(first)
        context.store.state["scene"]["objects"][0]["name"] = "Edited saved model"
        context.store.state["scene"]["revision"] += 1
        context.store._save()
        before_scene = copy.deepcopy(context.store.state["scene"])
        before_sessions = copy.deepcopy(context.store.state["sessions"])
        saved_assets = {path.name: path.read_bytes() for path in context.store.assets_dir.iterdir() if path.is_file()}
        saved_token = context.store.browser_token
        (root / "output" / "scene.glb").write_bytes(b"broken original")
        (root / "references" / "frame.png").write_bytes(b"broken original image")
        repeat = self.case.import_path(root, request_id)
        self.assertFalse(repeat["errors"], repeat)
        self.assertEqual(repeat["ready_check"]["instances"][0]["validation_basis"], "managed")
        self.assertEqual(len(self.case.factory_calls), 1)
        self.case.registry.unload(context.project_id)
        restored = self.case.import_path(root, request_id)
        self.assertEqual(len(restored["imported"]), 1, restored)
        context = self.context(restored)
        self.assertEqual(context.store.state["scene"], before_scene)
        self.assertEqual(context.store.state["sessions"], before_sessions)
        self.assertEqual(context.store.browser_token, saved_token)
        self.assertEqual({path.name: path.read_bytes() for path in context.store.assets_dir.iterdir() if path.is_file()}, saved_assets)
        self.assertEqual(restored["ready_check"]["instances"][0]["validation_basis"], "managed")
        self.case.assert_no_tasks()

    def test_corrupt_saved_model_blocks_reuse_and_reactivation_without_opening_context(self):
        root, _, _ = self.fixture()
        first = self.case.import_path(root)
        context = self.context(first)
        asset = context.store.assets_dir / context.store.scene()["objects"][0]["url"].rsplit("/", 1)[-1]
        saved = asset.read_bytes()
        asset.write_bytes(b"damaged managed model")
        repeated = self.case.import_path(root)
        self.assertEqual(repeated["ready_check"]["status"], "blocked", repeated)
        self.assertFalse(repeated["projects"])
        self.assertEqual(len(self.case.factory_calls), 1)
        self.case.registry.unload(context.project_id)
        rejected = self.case.import_path(root)
        self.assertEqual(rejected["ready_check"]["status"], "blocked", rejected)
        self.assertEqual(len(self.case.factory_calls), 1, "damaged preserved data opened a context")
        self.assertFalse(self.case.registry._records[context.project_id]["loaded"])
        asset.write_bytes(saved)
        restored = self.case.import_path(root)
        self.assertFalse(restored["errors"], restored)
        self.assertEqual(restored["projects"][0]["project_id"], context.project_id)

    def test_saved_reference_damage_bad_camera_and_bad_time_each_block_restoration(self):
        for name in ("image", "camera", "time"):
            with self.subTest(name=name):
                root, _, _ = self.fixture(name)
                first = self.case.import_path(root)
                context = self.context(first)
                session = context.store.state["sessions"][first["projects"][0]["session_id"]]
                if name == "image":
                    image = context.store.media_dir / session["reference_clip"]["frames"][0]["url"].rsplit("/", 1)[-1]
                    image.write_bytes(b"damaged managed image")
                elif name == "camera":
                    session["reference_clip"]["frames"][0]["camera"]["intrinsics"]["fy"] = 0
                    context.store._save()
                else:
                    session["reference_clip"]["frames"][1]["time_sec"] = 0
                    context.store._save()
                self.case.registry.unload(context.project_id)
                calls_before = len(self.case.factory_calls)
                result = self.case.import_path(root)
                self.assertEqual(result["ready_check"]["status"], "blocked", result)
                self.assertFalse(result["projects"])
                self.assertEqual(len(self.case.factory_calls), calls_before)

    def test_actual_import_failure_is_reported_as_blocked_and_same_request_can_retry(self):
        root, _, _ = self.fixture()
        request_id = uuid.uuid4().hex
        with patch.object(SceneStore, "import_model", side_effect=OSError("test copy failed")):
            rejected = self.case.import_path(root, request_id)
        self.assertEqual(rejected["ready_check"]["status"], "blocked", rejected)
        self.assertFalse(rejected["ready_check"]["can_import"])
        self.assertIn("import_failed", [item["code"] for item in rejected["ready_check"]["instances"][0]["checks"]])
        retry = self.case.import_path(root, request_id)
        self.assertEqual(len(retry["imported"]), 1, retry)
        self.assertTrue(retry["ready_check"]["can_import"])

    def test_preflight_exception_is_an_explicit_blocked_report(self):
        root, _, _ = self.fixture()
        first = self.case.import_path(root)
        with patch.object(self.case.registry, "_validate_managed_paths", side_effect=RuntimeError("saved path changed")):
            result = self.case.import_path(root)
        self.assertEqual(result["ready_check"]["status"], "blocked", result)
        self.assertEqual(result["ready_check"]["counters"]["blocked"], 1)
        self.assertIn("saved path changed", result["errors"][0]["error"])

    def test_saved_alignment_image_damage_and_wrong_size_each_block_reimport(self):
        for name in ("static_damage", "dynamic_size"):
            with self.subTest(name=name):
                root, _, _ = self.fixture(name)
                first = self.case.import_path(root)
                context = self.context(first)
                session_id = first["projects"][0]["session_id"]
                session = context.store.state["sessions"][session_id]
                reference = session["reference_images"][0] if name == "static_damage" else session["reference_clip"]["frames"][0]
                image = io.BytesIO()
                Image.new("RGB", (24, 24), "blue").save(image, format="PNG")
                context.store.set_reference_cameras(session_id, [{"reference_id": reference["id"],
                    "camera": _folder_import.camera(),
                    "alignment_image_data_url": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()}])
                reference = context.store.state["sessions"][session_id]["reference_images"][0] if name == "static_damage" else context.store.state["sessions"][session_id]["reference_clip"]["frames"][0]
                alignment = context.store.media_dir / reference["alignment_image_url"].rsplit("/", 1)[-1]
                if name == "static_damage":
                    alignment.write_bytes(b"damaged saved overlay")
                else:
                    Image.new("RGB", (12, 24), "blue").save(alignment, format="PNG")
                result = self.case.import_path(root)
                self.assertEqual(result["ready_check"]["status"], "blocked", result)
                self.assertFalse(result["projects"])
                self.assertIn(alignment.name, result["errors"][0]["error"])

    def test_bad_parent_manifest_errors_are_in_the_import_result_and_counters(self):
        (self.source / "manifest.json").write_text('{"scenes": [')
        result = self.case.import_path()
        self.assertEqual(result["ready_check"]["status"], "blocked", result)
        self.assertEqual(result["counters"]["errors"], 1)
        self.assertEqual(result["errors"], result["ready_check"]["errors"])
        self.assertIn("manifest.json", result["errors"][0]["path"])
        self.assertFalse(self.case.factory_calls)


if __name__ == "__main__":
    unittest.main()
