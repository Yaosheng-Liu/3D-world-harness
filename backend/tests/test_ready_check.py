"""Ready checks validate importer gates without publishing or modifying sources."""

from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import SceneStore
from gateway import WorkspaceGateway
from ready_check import check_ready_folder
from ready_import import discover_ready_instances
import test_folder_import as _folder_import


def animated_glb(**fields) -> bytes:
    binary = struct.pack("<17f", 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    document = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                "buffers": [{"byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                                {"buffer": 0, "byteOffset": 36, "byteLength": 8},
                                {"buffer": 0, "byteOffset": 44, "byteLength": 24}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 3},
                              {"bufferView": 1, "componentType": 5126, "type": "SCALAR", "count": 2, "min": [0], "max": [1]},
                              {"bufferView": 2, "componentType": 5126, "type": "VEC3", "count": 2}],
                "animations": [{"samplers": [{"input": 1, "output": 2}],
                                "channels": [{"sampler": 0, "target": {"node": 0, "path": "translation"}}]}]}
    document.update(fields)
    data = json.dumps(document).encode()
    data += b" " * (-len(data) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 28 + len(data) + len(binary))
            + struct.pack("<I4s", len(data), b"JSON") + data
            + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


class ReadyCheckTests(unittest.TestCase):
    def setUp(self):
        # Keep pure source fixtures: do not initialize a service or registry.
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "instances"
        self.source.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self, name="ready", *, nested=False):
        root, marker, document = _folder_import.FolderImportTests.fixture(self, name, nested=nested)
        (root / "output" / "scene.glb").write_bytes(animated_glb())
        document["blend"] = "output/scene.blend"
        document.pop("reference_images")
        marker.write_text(json.dumps(document))
        return root, marker, document

    def snapshot(self):
        return {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_model_only_catalog_is_importable_warning_and_never_initializes_or_writes(self):
        scenes = []
        for index in range(5):
            folder = self.source / str(index)
            folder.mkdir()
            (folder / "scene.glb").write_bytes(animated_glb())
            (folder / "scene.blend").write_bytes(b"only readability is checked")
            scenes.append({"name": f"模型 {index}", "folder": str(index), "glb": f"{index}/scene.glb",
                           "blend": f"{index}/scene.blend", "start_command": "must not execute"})
        (self.source / "manifest.json").write_text(json.dumps({"scenes": scenes}))
        before = self.snapshot()
        with patch.object(SceneStore, "__init__", side_effect=AssertionError("created store")), \
             patch.object(WorkspaceGateway, "__init__", side_effect=AssertionError("created gateway")), \
             patch.object(SceneStore, "_save", side_effect=AssertionError("saved state")), \
             patch.object(SceneStore, "_write_media", side_effect=AssertionError("wrote media")), \
             patch.object(SceneStore, "import_model", side_effect=AssertionError("imported model")), \
             patch("subprocess.run", side_effect=AssertionError("executed a manifest command")):
            report = check_ready_folder(self.source)
        self.assertEqual(report["status"], "warning", report)
        self.assertTrue(report["can_import"])
        self.assertEqual(report["counters"]["warning"], 5)
        self.assertEqual(report["counters"]["blocked"], 0)
        for instance in report["instances"]:
            metrics = instance["metrics"]
            self.assertEqual((metrics["mesh_count"], metrics["node_count"], metrics["animation_count"]), (1, 1, 1))
            self.assertEqual(metrics["animation_time_span_sec"], {"start": 0, "end": 1, "source": "accessor_metadata"})
            self.assertTrue(metrics["editable_blend"])
            self.assertIn("reference_missing", [check["code"] for check in instance["checks"]])
        self.assertEqual(before, self.snapshot())

    def test_multiview_nested_reference_frames_camera_times_and_metrics_are_validated_readonly(self):
        root, _, _ = self.fixture(nested=True)
        before = self.snapshot()
        with patch.object(SceneStore, "__init__", side_effect=AssertionError("created store")), \
             patch.object(SceneStore, "_write_media", side_effect=AssertionError("wrote media")), \
             patch.object(SceneStore, "_save", side_effect=AssertionError("saved workspace")):
            report = check_ready_folder(root)
        self.assertEqual(report["status"], "ready", report)
        metrics = report["instances"][0]["metrics"]
        self.assertEqual((metrics["view_count"], metrics["frame_count"], metrics["camera_count"]), (2, 4, 4))
        self.assertEqual([view["fps"] for view in metrics["views"]], [2, 4])
        self.assertEqual([view["duration_sec"] for view in metrics["views"]], [1, 1.25])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report, check_ready_folder(root), "random prepared clip IDs leaked into the report")

    def test_missing_camera_is_warning_without_blocking_import(self):
        root, _, _ = self.fixture()
        manifest = root / "references" / "multiview.json"
        document = json.loads(manifest.read_text())
        document["views"][0]["frames"][0].pop("camera")
        manifest.write_text(json.dumps(document))
        report = check_ready_folder(root)
        self.assertEqual(report["status"], "warning")
        self.assertTrue(report["can_import"])
        self.assertEqual(report["instances"][0]["metrics"]["camera_count"], 3)

    def test_invalid_image_camera_timestamp_fps_duration_and_glb_block_import(self):
        mutations = {
            "glb": lambda root, document: (root / "output" / "scene.glb").write_bytes(b"invalid model"),
            "image": lambda root, document: (root / "references" / "frame.png").write_bytes(b"invalid image"),
            "camera": lambda root, document: document["views"][0]["frames"][0]["camera"].update(camera_to_world=[[0]]),
            "camera_dimensions": lambda root, document: document["views"][0]["frames"][0]["camera"]["intrinsics"].update(width=25),
            "time": lambda root, document: document["views"][0]["frames"][1].update(time_sec=0),
            "fps": lambda root, document: document["views"][0].update(fps=0),
            "duration": lambda root, document: document["views"][0].update(duration_sec=0.1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                root, _, _ = self.fixture(name)
                manifest = root / "references" / "multiview.json"
                document = json.loads(manifest.read_text())
                mutate(root, document)
                manifest.write_text(json.dumps(document))
                report = check_ready_folder(root)
                self.assertEqual(report["status"], "blocked", report)
                self.assertFalse(report["can_import"])
                self.assertEqual(report["counters"]["blocked"], 1)
                self.assertTrue(report["errors"])

    def test_declared_camera_manifest_uses_the_importer_coverage_and_aspect_checks(self):
        for name, camera_document in (
            ("bad_shape", {"camera": {"camera_to_world": [[0]]}}),
            ("bad_coverage", {"frames": [{"time_sec": 99, "camera": _folder_import.camera()}]}),
            ("bad_aspect", {"camera": {**_folder_import.camera(), "intrinsics": {**_folder_import.camera()["intrinsics"], "width": 48}}}),
        ):
            with self.subTest(name=name):
                root, marker, document = self.fixture(name)
                manifest = root / "references" / "multiview.json"
                single = json.loads(manifest.read_text())["views"][0]
                for frame in single["frames"]:
                    frame.pop("camera")
                manifest.write_text(json.dumps(single))
                (root / "references" / "cameras.json").write_text(json.dumps(camera_document))
                document["camera_manifest"] = "references/cameras.json"
                marker.write_text(json.dumps(document))
                report = check_ready_folder(root)
                self.assertEqual(report["status"], "blocked", report)
                self.assertFalse(report["can_import"])

    def test_static_images_are_checked_and_invalid_bytes_are_blocking(self):
        root, marker, document = self.fixture()
        document.pop("reference_clip")
        document["reference_images"] = ["references/frame.png"]
        marker.write_text(json.dumps(document))
        report = check_ready_folder(root)
        self.assertEqual(report["status"], "warning")
        self.assertTrue(report["can_import"])
        self.assertEqual(report["instances"][0]["metrics"]["static_image_count"], 1)
        (root / "references" / "frame.png").write_bytes(b"broken PNG")
        report = check_ready_folder(root)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["can_import"])

    def test_truncated_jpeg_pixels_are_rejected_even_when_pil_verify_accepts_them(self):
        root, marker, document = self.fixture()
        document.pop("reference_clip")
        document["reference_images"] = ["references/truncated.jpg"]
        marker.write_text(json.dumps(document))
        image = io.BytesIO()
        Image.new("RGB", (24, 24), "red").save(image, format="JPEG")
        data = image.getvalue()[:-5]
        SceneStore._image_kind(data)
        (root / "references" / "truncated.jpg").write_bytes(data)
        report = check_ready_folder(root)
        self.assertFalse(report["can_import"], report)
        self.assertIn("fully decoded", report["errors"][0]["error"])

    def test_valid_and_invalid_scenes_report_partial_and_preserve_all_inputs(self):
        good, _, _ = self.fixture("good")
        bad, _, _ = self.fixture("bad")
        (bad / "output" / "scene.glb").write_bytes(b"bad GLB")
        before = self.snapshot()
        report = check_ready_folder(self.source)
        self.assertEqual(report["status"], "partial", report)
        self.assertTrue(report["can_import"])
        self.assertEqual((report["counters"]["ready"], report["counters"]["blocked"]), (1, 1))
        self.assertEqual(self.snapshot(), before)

    def test_malformed_markers_and_unknown_folders_have_actionable_reports(self):
        root, marker, _ = self.fixture()
        marker.write_text("invalid JSON")
        report = check_ready_folder(root)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["instances"][0]["checks"][0]["code"], "discovery_error")
        unknown = self.root / "unknown"
        unknown.mkdir()
        empty = check_ready_folder(unknown)
        self.assertEqual(empty["status"], "empty")
        self.assertTrue(empty["skipped"])
        (unknown / "manifest.json").write_text("invalid JSON")
        broken = check_ready_folder(unknown)
        self.assertEqual(broken["status"], "blocked")
        self.assertIn("清单格式", broken["errors"][0]["error"])

    def test_scan_truncation_cannot_be_reported_as_fully_ready(self):
        self.fixture("one")
        self.fixture("two")
        limited = discover_ready_instances(self.source, max_depth=0)
        with patch("ready_check.discover_ready_instances", return_value=limited):
            report = check_ready_folder(self.source)
        self.assertTrue(report["truncated"])
        self.assertFalse(report["can_import"])
        self.assertEqual(report["status"], "blocked")

    def test_video_uses_only_the_importers_bounded_sampler_and_checked_frames(self):
        root, _, _ = self.fixture()
        manifest = root / "references" / "multiview.json"
        manifest.write_text(json.dumps({"video_path": "clip.mp4", "fps": 2, "start_command": "must not execute"}))
        video = root / "references" / "clip.mp4"
        video.write_bytes(b"the test sampler supplies decoded bytes")
        data = (root / "references" / "frame.png").read_bytes()
        frames = [{"name": "sample.png", "time_sec": 0, "data": data}, {"name": "sample2.png", "time_sec": 0.5, "data": data}]
        before = self.snapshot()
        with patch("dynamic.sample_video", return_value=(frames, 1)) as sample:
            report = check_ready_folder(root)
        sample.assert_called_once_with(video, 2)
        self.assertTrue(report["can_import"], report)
        self.assertEqual(report["instances"][0]["metrics"]["views"][0]["source_type"], "video")
        self.assertEqual(report["instances"][0]["metrics"]["frame_count"], 2)
        self.assertEqual(self.snapshot(), before)

    def test_current_viewer_missing_decoders_block_required_compression(self):
        for extension in ("KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_texture_basisu"):
            with self.subTest(extension=extension):
                root, _, _ = self.fixture(extension)
                (root / "output" / "scene.glb").write_bytes(animated_glb(extensionsUsed=[extension], extensionsRequired=[extension]))
                report = check_ready_folder(root)
                self.assertFalse(report["can_import"], report)
                checks = report["instances"][0]["checks"]
                self.assertIn("viewer_decoder_missing", [check["code"] for check in checks])
        root, _, _ = self.fixture("optional_draco")
        (root / "output" / "scene.glb").write_bytes(animated_glb(extensionsUsed=["KHR_draco_mesh_compression"]))
        self.assertFalse(check_ready_folder(root)["can_import"], "GLTFLoader initializes Draco from extensionsUsed")
        root, _, _ = self.fixture("optional_meshopt")
        (root / "output" / "scene.glb").write_bytes(animated_glb(extensionsUsed=["EXT_meshopt_compression"]))
        self.assertTrue(check_ready_folder(root)["can_import"], "optional Meshopt may use ordinary fallback buffers")


if __name__ == "__main__":
    unittest.main()
