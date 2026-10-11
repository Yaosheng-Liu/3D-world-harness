"""Ready directory imports isolate scene data and never create model tasks."""

from __future__ import annotations

import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import APIError, SceneStore
from dynamic import reference_views
from gateway import WorkspaceGateway
from projects import ProjectContext, ProjectRegistry
from ready_import import browse_folders, discover_ready_instances
from server import _DataDirLock


def glb_bytes() -> bytes:
    document = json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "scene": 0}).encode()
    document += b" " * (-len(document) % 4)
    return struct.pack("<4sII", b"glTF", 2, len(document) + 20) + struct.pack("<I4s", len(document), b"JSON") + document


def camera(x: float = 0) -> dict:
    return {"camera_to_world": [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]],
            "intrinsics": {"width": 24, "height": 24, "fx": 20, "fy": 20, "cx": 12, "cy": 12},
            "image_undistorted": True}


class FolderImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "instances"
        self.source.mkdir()
        self.default_project = self.root / "current"
        self.default_project.mkdir()
        self.data = self.root / "service-data"
        self.factory_calls = []
        self.factory_contexts = []
        self.transport = "legacy"
        self.external_review = True
        self.target_patch = patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("import started a model task"))
        self.model_patch = patch.object(WorkspaceGateway, "list_models", side_effect=AssertionError("import requested model settings"))
        self.target_mock = self.target_patch.start()
        self.model_mock = self.model_patch.start()
        self.open_registry()

    def configure(self, context):
        context.gateway.registry_project_id = context.project_id
        context.gateway.project_name = context.name
        context.gateway.ensure()
        if self.transport == "mcp_events" and context.gateway.mcp_events is None:
            from mcp_events import MCPEvents
            context.gateway.mcp_events = MCPEvents(context.store, context.project_id)

    def factory(self, record, newly_created):
        self.factory_calls.append((copy.deepcopy(record), newly_created))
        project = Path(record["project_dir"])
        data = Path(record["data_dir"])
        if not project.is_dir() or (not newly_created and not (data / "state.json").is_file()):
            raise RuntimeError("saved source or imported state missing")
        lock = _DataDirLock(data)
        gateway = None
        try:
            store = SceneStore(data)
            if newly_created:
                store.state["scene"]["name"] = record["name"]
                store._save()
            gateway = WorkspaceGateway(store, project, external_review=self.external_review, feedback_transport=self.transport)
            context = ProjectContext(record["project_id"], record["name"], store, gateway, lock)
            self.configure(context)
            self.factory_contexts.append(context)
            return context
        except BaseException:
            if gateway is not None:
                gateway.close()
            lock.close()
            raise

    def open_registry(self):
        store = SceneStore(self.data)
        gateway = WorkspaceGateway(store, self.default_project, external_review=self.external_review, feedback_transport=self.transport)
        root_id = uuid.uuid5(uuid.NAMESPACE_URL, str(self.default_project)).hex
        root = ProjectContext(root_id, "Current scene", store, gateway)
        self.registry = ProjectRegistry(root, context_factory=self.factory, configure_context=self.configure)

    def tearDown(self):
        self.registry.close()
        self.target_patch.stop()
        self.model_patch.stop()
        self.temporary.cleanup()

    def fixture(self, name="ready", *, style="standard", root=None, nested=False):
        root = root or self.source / name
        root.mkdir(exist_ok=True)
        references = root / "references"
        references.mkdir(exist_ok=True)
        output = root / "output"
        output.mkdir(exist_ok=True)
        image = io.BytesIO()
        Image.new("RGB", (24, 24), "red").save(image, format="PNG")
        (references / "frame.png").write_bytes(image.getvalue())
        views = [{"name": "Camera A", "fps": 2, "duration_sec": 1,
                  "frames": [{"path": "frame.png", "time_sec": 0, "camera": camera()},
                             {"path": "frame.png", "time_sec": 0.5, "camera": camera()}]},
                 {"name": "Camera B", "fps": 4, "duration_sec": 1.25,
                  "frames": [{"path": "frame.png", "time_sec": 0, "camera": camera(2)},
                             {"path": "frame.png", "time_sec": 0.75, "camera": camera(2)}]}]
        if nested:
            (references / "camera_b.json").write_text(json.dumps(views[1]), encoding="utf-8")
            views[1] = {"name": "Camera B", "manifest_path": "camera_b.json"}
        manifest = references / "multiview.json"
        manifest.write_text(json.dumps({"name": name + " references", "views": views}), encoding="utf-8")
        glb = output / "scene.glb"
        glb.write_bytes(glb_bytes())
        blend = output / "scene.blend"
        blend.write_bytes(b"editable Blender source")
        if style == "active":
            workbench = root / "workbench"
            workbench.mkdir(exist_ok=True)
            marker = workbench / "active_scene.json"
            document = {"glb": str(glb), "blend": str(blend), "reference_manifest": str(manifest), "scene_revision": 17}
        else:
            marker = root / "workbench-ready.json"
            document = {"schema_version": 1, "name": name, "scene_glb_path": "output/scene.glb",
                        "reference_clip": {"manifest_path": "references/multiview.json"},
                        "reference_images": ["references/frame.png"]}
        if style != "multiview":
            marker.write_text(json.dumps(document), encoding="utf-8")
        return root, marker, document

    def import_path(self, path=None, request_id=None):
        return self.registry.import_folder({"path": str(path or self.source), "request_id": request_id or uuid.uuid4().hex})

    def assert_no_tasks(self):
        self.target_mock.assert_not_called()
        self.model_mock.assert_not_called()
        for context in self.registry.contexts()[1:]:
            self.assertIsNone(context.gateway.adapter)
            self.assertIsNone(context.store.workspace()["thread_id"])
            self.assertEqual(context.store.workspace()["created_thread_ids"], [])

    def test_standard_manifest_imports_all_view_times_cameras_and_preserves_source(self):
        root, _, _ = self.fixture(nested=True)
        before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        root_before = self.registry.root.store.state_path.read_bytes()
        result = self.import_path()
        self.assertEqual(result["counters"]["imported"], 1, result)
        metadata = result["projects"][0]
        context = self.registry.get(metadata["project_id"])
        self.assertEqual(context.project_dir, root)
        self.assertEqual(context.store.data_dir, self.data / "projects" / metadata["project_id"] / "data")
        self.assertEqual(metadata["creation_status"], "ready")
        self.assertEqual(metadata["kind"], "imported")
        self.assertTrue(metadata["import_source"]["read_only"])
        self.assertNotEqual(context.store.control_token, self.registry.root.store.control_token)
        self.assertNotEqual(context.store.browser_token, self.registry.root.store.browser_token)
        session = context.store.get_session(metadata["session_id"])
        views = reference_views(session["reference_clip"])
        self.assertEqual([view["name"] for view in views], ["Camera A", "Camera B"])
        self.assertEqual([view["fps"] for view in views], [2, 4])
        self.assertEqual([[frame["time_sec"] for frame in view["frames"]] for view in views], [[0, 0.5], [0, 0.75]])
        self.assertEqual(views[1]["frames"][1]["camera"]["camera_to_world"], camera(2)["camera_to_world"])
        self.assertEqual(views[1]["frames"][1]["camera"]["intrinsics"], camera(2)["intrinsics"])
        self.assertEqual(len(session["reference_images"]), 1)
        asset = context.store.assets_dir / context.store.scene()["objects"][0]["url"].rsplit("/", 1)[-1]
        self.assertEqual(asset.read_bytes(), glb_bytes())
        self.assertEqual(self.registry.root.store.state_path.read_bytes(), root_before)
        self.assertEqual({str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)
        self.assert_no_tasks()

    def test_active_scene_and_unique_output_formats_are_discovered(self):
        active, _, _ = self.fixture("active", style="active")
        history = active / "workbench" / "revisions" / "old"
        history.mkdir(parents=True)
        self.fixture("historic", root=history)
        self.fixture("unique", style="multiview")
        result = self.import_path()
        self.assertEqual(result["counters"]["imported"], 2, result)
        self.assertEqual(result["counters"]["candidates"], 2)
        sources = {item["import_source"]["format"]: item["import_source"] for item in result["projects"]}
        self.assertEqual(set(sources), {"active_scene", "multiview_output"})
        self.assertEqual(sources["active_scene"]["source_revision"], 17)
        self.assertEqual(Path(sources["active_scene"]["blend"]).read_bytes(), b"editable Blender source")
        self.assert_no_tasks()

    def test_underscore_standard_manifest_is_accepted(self):
        root, marker, _ = self.fixture()
        marker.rename(root / "workbench_ready.json")
        self.assertEqual(self.import_path(root)["counters"]["imported"], 1)

    def test_static_images_without_clip_manifest_are_ready(self):
        root, marker, document = self.fixture()
        document.pop("reference_clip")
        marker.write_text(json.dumps(document))
        result = self.import_path(root)
        self.assertEqual(result["counters"]["imported"], 1, result)
        context = self.registry.get(result["projects"][0]["project_id"])
        session = context.store.get_session(result["projects"][0]["session_id"])
        self.assertEqual(len(session["reference_images"]), 1)
        self.assertIsNone(session.get("reference_clip"))
        self.assertIsNone(result["projects"][0]["import_source"]["manifest"])
        self.assert_no_tasks()

    def test_mixed_batch_reports_ambiguity_false_and_malformed_without_ghosts(self):
        self.fixture("good")
        ambiguous, _, _ = self.fixture("ambiguous", style="multiview")
        (ambiguous / "output" / "older.glb").write_bytes(glb_bytes())
        false, marker, document = self.fixture("not_ready")
        marker.write_text(json.dumps(dict(document, ready=False)))
        bad, marker, _ = self.fixture("malformed")
        marker.write_text("not valid JSON")
        receipts = self.source / "receipts"
        receipts.mkdir()
        (receipts / "workbench_open.json").write_text('{"ready": true, "glb": "fake.glb"}')
        (receipts / "state.json").write_text('{"ready": true}')
        (receipts / "publication.json").write_text('{"ready": true}')
        review = receipts / "review"
        review.mkdir()
        self.fixture("fake_review", root=review)
        (receipts / "load.sh").write_text("touch should-never-exist")
        result = self.import_path()
        self.assertEqual((len(result["imported"]), len(result["skipped"]), len(result["errors"])), (1, 1, 2), result)
        self.assertIn("ambiguous", next(item["error"] for item in result["errors"] if item["path"] == str(ambiguous)))
        self.assertEqual(result["skipped"][0]["path"], str(false))
        self.assertEqual(len(self.registry.contexts()), 2)
        self.assertFalse((receipts / "should-never-exist").exists())
        self.assert_no_tasks()

    def test_paths_and_symlinks_cannot_escape_instance_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "scene.glb").write_bytes(glb_bytes())
        mutations = [
            lambda root, doc: doc.update(scene_glb_path=str(outside / "scene.glb")),
            lambda root, doc: (root / "output" / "scene.glb").unlink() or (root / "output" / "scene.glb").symlink_to(outside / "scene.glb"),
            lambda root, doc: doc.update(reference_clip={"manifest_path": str(outside / "refs.json")}),
            lambda root, doc: doc.update(reference_images=[str(outside / "frame.png")]),
        ]
        source, _, _ = self.fixture("external")
        (outside / "refs.json").write_bytes((source / "references" / "multiview.json").read_bytes())
        (outside / "frame.png").write_bytes((source / "references" / "frame.png").read_bytes())
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                root, marker, document = self.fixture(f"escape{index}")
                mutate(root, document)
                marker.write_text(json.dumps(document))
                result = self.import_path(root)
                self.assertEqual(len(result["errors"]), 1, result)
                self.assertEqual(result["projects"], [])
        root, _, _ = self.fixture("nested_escape", nested=True)
        nested = root / "references" / "camera_b.json"
        document = json.loads(nested.read_text())
        document["frames"][0]["path"] = str(outside / "frame.png")
        nested.write_text(json.dumps(document))
        self.assertEqual(len(self.import_path(root)["errors"]), 1)
        self.assertEqual(len(self.factory_calls), 0)

    def test_invalid_glb_or_image_rolls_back_data_and_releases_locks(self):
        for broken in ("glb", "image"):
            with self.subTest(broken=broken):
                root, _, _ = self.fixture(broken)
                path = root / ("output/scene.glb" if broken == "glb" else "references/frame.png")
                path.write_bytes(b"not the required binary format")
                result = self.import_path(root)
                self.assertEqual(len(result["errors"]), 1, result)
                self.assertEqual(len(self.registry.contexts()), 1)
                self.assertEqual(len(self.registry.list(self.registry.root.project_id)["projects"]), 1)
                project_id = uuid.uuid5(uuid.NAMESPACE_URL, "scene-feedback-import:" + str(root)).hex
                managed = self.data / "projects" / project_id
                self.assertFalse(managed.exists())
                lock = _DataDirLock(managed / "data")
                lock.close()
                # Remove only the directory this test's lock probe created.
                import shutil
                shutil.rmtree(managed)
        self.assert_no_tasks()

    def test_factory_and_registry_write_failures_leave_no_failed_project(self):
        root, _, _ = self.fixture()
        project_id = uuid.uuid5(uuid.NAMESPACE_URL, "scene-feedback-import:" + str(root)).hex
        managed = self.data / "projects" / project_id

        def fail_factory(record, newly_created):
            lock = _DataDirLock(Path(record["data_dir"]))
            try:
                SceneStore(record["data_dir"])
                raise RuntimeError("factory failed after allocating data")
            finally:
                lock.close()

        with patch.object(self.registry, "_factory", fail_factory):
            result = self.import_path(root)
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(managed.exists())
        self.assertEqual(len(self.registry.contexts()), 1)
        save = self.registry._save
        calls = []

        def fail_publication():
            calls.append(True)
            if len(calls) == 2:
                raise OSError("registry publication could not be saved")
            return save()

        request_id = uuid.uuid4().hex
        with patch.object(self.registry, "_save", fail_publication):
            result = self.import_path(root, request_id)
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(managed.exists())
        self.assertEqual(len(json.loads(self.registry.path.read_text())["projects"]), 1)
        retry = self.import_path(root, request_id)
        self.assertEqual(len(retry["imported"]), 1, retry)

    def test_glb_changed_to_external_symlink_during_copy_is_rejected(self):
        root, _, _ = self.fixture()
        external = self.root / "external.glb"
        external.write_bytes(glb_bytes())
        source = root / "output" / "scene.glb"
        original = WorkspaceGateway.set_reference_clip_paths

        def changed(gateway, payload):
            result = original(gateway, payload)
            source.unlink()
            source.symlink_to(external)
            return result

        with patch.object(WorkspaceGateway, "set_reference_clip_paths", changed):
            result = self.import_path(root)
        self.assertEqual(len(result["errors"]), 1, result)
        self.assertIn("inside", result["errors"][0]["error"])
        self.assertEqual(len(self.registry.contexts()), 1)

    def test_duplicate_source_request_and_root_reuse_preserve_existing_state(self):
        root, _, _ = self.fixture()
        request_id = uuid.uuid4().hex
        first = self.import_path(root, request_id)
        context = self.registry.get(first["projects"][0]["project_id"])
        context.store.state["feedback"].append({"feedback_id": "existing-feedback"})
        context.store._save()
        before = context.store.state_path.read_bytes()
        repeat = self.import_path(root, request_id)
        again = self.import_path(root)
        self.assertEqual(repeat["projects"], first["projects"])
        self.assertEqual(again["projects"], first["projects"])
        self.assertEqual(repeat["imported"], [])
        self.assertEqual(context.store.state_path.read_bytes(), before)
        self.assertEqual(len(self.factory_calls), 1)
        self.fixture(root=self.default_project)
        root_before = self.registry.root.store.state_path.read_bytes()
        reused = self.import_path(self.default_project)
        self.assertEqual(reused["projects"][0]["project_id"], self.registry.root.project_id)
        self.assertEqual(reused["imported"], [])
        self.assertEqual(self.registry.root.store.state_path.read_bytes(), root_before)

    def test_request_binding_survives_restart_and_failed_sources_can_be_retried(self):
        root, _, _ = self.fixture()
        glb = root / "output" / "scene.glb"
        glb.write_bytes(b"broken GLB")
        request_id = uuid.uuid4().hex
        self.assertEqual(len(self.import_path(root, request_id)["errors"]), 1)
        glb.write_bytes(glb_bytes())
        first = self.import_path(root, request_id)
        self.assertEqual(len(first["imported"]), 1, first)
        calls = len(self.factory_calls)
        self.registry.close()
        self.open_registry()
        self.assertEqual(len(self.factory_calls), calls + 1)
        restored = self.import_path(root, request_id)
        self.assertEqual(restored["projects"], first["projects"])
        self.assertEqual(restored["imported"], [])
        with self.assertRaisesRegex(APIError, "different folder"):
            self.import_path(self.source, request_id)
        self.assert_no_tasks()

    def test_missing_import_source_keeps_default_available_and_recovers_ready(self):
        source, _, _ = self.fixture()
        first = self.import_path(source)
        project_id = first["projects"][0]["project_id"]
        context = self.registry.get(project_id)
        child_state = context.store.state_path.read_bytes()
        moved = self.root / "temporarily_moved_source"
        self.registry.close()
        source.rename(moved)
        with self.assertLogs(level="ERROR"):
            self.open_registry()
        self.assertIs(self.registry.get(self.registry.root.project_id), self.registry.root)
        metadata = next(item for item in self.registry.list(self.registry.root.project_id)["projects"]
                        if item["project_id"] == project_id)
        self.assertEqual(metadata["creation_status"], "unavailable")
        with self.assertRaises(APIError):
            self.registry.get(project_id)
        self.registry.close()
        moved.rename(source)
        self.open_registry()
        restored = self.registry.get(project_id)
        metadata = self.registry.metadata(restored)
        self.assertEqual(metadata["creation_status"], "ready")
        self.assertNotIn("creation_error", metadata)
        self.assertNotIn("creation_http_status", self.registry._records[project_id])
        self.assertEqual(restored.store.state_path.read_bytes(), child_state)
        self.assertEqual(metadata["session_id"], first["projects"][0]["session_id"])
        self.assert_no_tasks()

    def test_changed_import_source_symlink_is_isolated_from_default_restore(self):
        source, _, _ = self.fixture("source")
        first = self.import_path(source)
        project_id = first["projects"][0]["project_id"]
        context = self.registry.get(project_id)
        child_state = context.store.state_path.read_bytes()
        other, _, _ = self.fixture("other")
        moved = self.root / "original_source"
        self.registry.close()
        source.rename(moved)
        source.symlink_to(other, target_is_directory=True)
        calls = len(self.factory_calls)
        with self.assertLogs(level="ERROR"):
            self.open_registry()
        self.assertIs(self.registry.get(self.registry.root.project_id), self.registry.root)
        self.assertEqual(len(self.factory_calls), calls, "unsafe changed source reached the factory")
        metadata = next(item for item in self.registry.list(self.registry.root.project_id)["projects"]
                        if item["project_id"] == project_id)
        self.assertEqual(metadata["creation_status"], "unavailable")
        self.registry.close()
        source.unlink()
        moved.rename(source)
        self.open_registry()
        self.assertEqual(self.registry.metadata(self.registry.get(project_id))["creation_status"], "ready")
        self.assertEqual(self.registry.get(project_id).store.state_path.read_bytes(), child_state)
        self.assert_no_tasks()

    def test_changed_source_does_not_claim_another_canonical_ready_instance(self):
        source, _, _ = self.fixture("source_a")
        first = self.import_path(source)
        old_id = first["projects"][0]["project_id"]
        other, _, _ = self.fixture("source_b")
        self.registry.close()
        source.rename(self.root / "moved_source_a")
        source.symlink_to(other, target_is_directory=True)
        with self.assertLogs(level="ERROR"):
            self.open_registry()
        second = self.import_path(other)
        self.assertEqual(second["counters"]["imported"], 1, second)
        self.assertNotEqual(second["projects"][0]["project_id"], old_id)
        self.assertEqual(second["projects"][0]["project_dir"], str(other))
        self.assertEqual(second["projects"][0]["name"], "source_b")
        alias = self.import_path(source)
        self.assertEqual(alias["projects"], second["projects"])
        self.assertEqual(alias["imported"], [])
        self.assertEqual(self.registry._records[old_id]["creation_status"], "unavailable")
        self.assert_no_tasks()

    def test_world_up_is_saved_on_the_model_without_losing_source_metadata(self):
        source, _, _ = self.fixture()
        discovery = discover_ready_instances(source)
        discovery.instances[0] = replace(discovery.instances[0], world_up="Z")
        with patch("projects.discover_ready_instances", return_value=discovery):
            result = self.import_path(source)
        self.assertEqual(result["counters"]["imported"], 1, result)
        context = self.registry.get(result["projects"][0]["project_id"])
        model = context.store.scene()["objects"][0]
        self.assertEqual(model["metadata"]["up_axis"], "z")
        self.assertEqual(model["metadata"]["source_name"], "scene.glb")
        self.assertEqual(len(model["metadata"]["sha256"]), 64)
        self.assertEqual(context.store.scene()["revision"], 3)
        self.registry.close()
        self.open_registry()
        self.assertEqual(self.registry.get(context.project_id).store.scene()["objects"][0]["metadata"], model["metadata"])
        self.assert_no_tasks()

    def test_single_view_camera_manifest_is_passed_to_reference_importer(self):
        source, _, _ = self.fixture()
        manifest = source / "references" / "multiview.json"
        single = json.loads(manifest.read_text())["views"][0]
        for frame in single["frames"]:
            frame.pop("camera")
        manifest.write_text(json.dumps(single))
        calibration = source / "references" / "camera.json"
        calibration.write_text(json.dumps({"camera": camera(7)}))
        discovery = discover_ready_instances(source)
        discovery.instances[0] = replace(discovery.instances[0], camera_manifest=calibration)
        with patch("projects.discover_ready_instances", return_value=discovery):
            result = self.import_path(source)
        self.assertEqual(result["counters"]["imported"], 1, result)
        context = self.registry.get(result["projects"][0]["project_id"])
        clip = context.store.get_session(result["projects"][0]["session_id"])["reference_clip"]
        self.assertEqual([frame["camera"]["camera_to_world"] for frame in clip["frames"]],
                         [camera(7)["camera_to_world"], camera(7)["camera_to_world"]])
        self.assertEqual(clip["frames"][0]["camera"]["intrinsics"], camera(7)["intrinsics"])
        self.assert_no_tasks()

    def test_ordinary_managed_path_validation_remains_a_strict_startup_gate(self):
        self.registry.close()
        document = json.loads(self.registry.path.read_text())
        child_id = uuid.uuid4().hex
        document["projects"].append({"project_id": child_id, "name": "tampered managed project",
                                     "project_dir": str(self.root / "external_workspace"),
                                     "data_dir": str(self.root / "external_data"), "creation_status": "ready"})
        self.registry.path.write_text(json.dumps(document))
        with self.assertRaisesRegex(RuntimeError, "managed project directories"):
            self.open_registry()

    def test_input_validation_and_creation_request_namespace(self):
        for payload in ({"path": str(self.source)}, {"path": "relative", "request_id": uuid.uuid4().hex},
                        {"path": str(self.source), "request_id": uuid.uuid4().hex, "model": "gpt-6-astra"}):
            with self.assertRaises(APIError):
                self.registry.import_folder(payload)
        request_id = uuid.uuid4().hex
        self.import_path(request_id=request_id)
        with self.assertRaisesRegex(APIError, "folder import"):
            self.registry.create({"request_id": request_id, "name": "new", "model": "gpt-6-astra"})
        self.registry._records[self.registry.root.project_id]["request_id"] = uuid.uuid4().hex
        with self.assertRaisesRegex(APIError, "project creation"):
            self.import_path(request_id=self.registry._records[self.registry.root.project_id]["request_id"])

    def test_invalid_path_text_is_contained_and_bad_marker_does_not_stop_batch(self):
        for path in ("/invalid\0folder", "~scene_feedback_missing_user_991"):
            with self.subTest(path=path), self.assertRaises(APIError) as error:
                self.registry.import_folder({"path": path, "request_id": uuid.uuid4().hex})
            self.assertEqual(error.exception.status, 400)
        self.fixture("good")
        _, marker, document = self.fixture("invalid_path")
        document["scene_glb_path"] = "output/scene\0.glb"
        marker.write_text(json.dumps(document))
        result = self.import_path()
        self.assertEqual((len(result["imported"]), len(result["errors"])), (1, 1), result)

    def test_scan_limits_hidden_software_image_trees_and_symlinks(self):
        self.fixture("real")
        for name in ("venv", "images", ".hidden", "references", "workbench", "output"):
            directory = self.source / name
            directory.mkdir()
            self.fixture(root=directory)
        (self.source / "linked").symlink_to(self.source / "real", target_is_directory=True)
        discovery = discover_ready_instances(self.source)
        self.assertEqual([instance.name for instance in discovery.instances], ["real"])
        limited = discover_ready_instances(self.source, max_entries=1)
        self.assertTrue(limited.truncated)
        self.assertTrue(limited.errors)
        deep = self.root / "deep"
        (deep / "child" / "deeper").mkdir(parents=True)
        limited = discover_ready_instances(deep, max_depth=0)
        self.assertTrue(limited.truncated)
        self.assertIn("depth limit", limited.skipped[0]["reason"])
        browser = browse_folders(self.source)
        self.assertEqual(browser["path"], str(self.source))
        self.assertEqual(browser["parent"], str(self.source.parent))
        self.assertNotIn(".hidden", [item["name"] for item in browser["directories"]])
        self.assertNotIn("linked", [item["name"] for item in browser["directories"]])

    def test_import_copy_does_not_hold_catalog_lock(self):
        root, _, _ = self.fixture()
        entered, release, fetched = threading.Event(), threading.Event(), threading.Event()
        original = WorkspaceGateway.set_reference_clip_paths
        outcome = []

        def blocked(gateway, payload):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test import was not released")
            return original(gateway, payload)

        with patch.object(WorkspaceGateway, "set_reference_clip_paths", blocked):
            importer = threading.Thread(target=lambda: outcome.append(self.import_path(root)))
            importer.start()
            self.assertTrue(entered.wait(3))
            reader = threading.Thread(target=lambda: (self.registry.get(self.registry.root.project_id), fetched.set()))
            reader.start()
            try:
                self.assertTrue(fetched.wait(1), "active project retrieval was blocked by image copying")
            finally:
                release.set()
                importer.join(4)
                reader.join(4)
        self.assertEqual(outcome[0]["counters"]["imported"], 1, outcome)

    def test_nearby_ready_instances_are_checked_before_large_raw_trees(self):
        raw = self.source / "aaa_raw_capture"
        raw.mkdir()
        for index in range(10):
            (raw / f"raw{index}.jpg").write_bytes(b"image")
        ready, _, _ = self.fixture("zzz_ready")
        discovery = discover_ready_instances(self.source, max_entries=5)
        self.assertTrue(discovery.truncated)
        self.assertEqual([instance.root for instance in discovery.instances], [ready])

    def test_existing_managed_data_is_not_reused_or_deleted(self):
        root, _, _ = self.fixture()
        project_id = uuid.uuid5(uuid.NAMESPACE_URL, "scene-feedback-import:" + str(root)).hex
        existing = self.data / "projects" / project_id / "data"
        existing.mkdir(parents=True)
        sentinel = existing / "control_token"
        sentinel.write_text("external old key")
        result = self.import_path(root)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(sentinel.read_text(), "external old key")
        self.assertEqual(len(self.factory_calls), 0)
        self.assertEqual(len(self.registry.contexts()), 1)

    def test_imported_context_inherits_mcp_events_and_restarts_without_tasks(self):
        self.registry.close()
        # A separate service data directory is required when changing transport.
        self.data = self.root / "event-service-data"
        self.transport = "mcp_events"
        self.open_registry()
        root, _, _ = self.fixture()
        result = self.import_path(root)
        self.assertEqual(result["counters"]["imported"], 1, result)
        context = self.registry.get(result["projects"][0]["project_id"])
        self.assertEqual(context.store.workspace()["feedback_transport"], "mcp_events")
        self.assertIsNotNone(context.gateway.mcp_events)
        self.assertTrue(context.gateway._started)
        self.assertEqual(context.store.workspace()["agent"]["status"], "external_idle")
        self.registry.close()
        self.open_registry()
        context = self.registry.get(result["projects"][0]["project_id"])
        self.assertTrue(context.gateway._started)
        self.assertEqual(context.gateway.feedback_transport, "mcp_events")
        self.assert_no_tasks()

    def test_cli_service_import_has_no_model_adapter_or_automatic_start(self):
        self.registry.close()
        self.data = self.root / "cli-service-data"
        self.external_review = False
        self.open_registry()
        root, _, _ = self.fixture()
        with patch.object(WorkspaceGateway, "start", side_effect=AssertionError("CLI import started an agent")):
            result = self.import_path(root)
            self.assertEqual(len(result["imported"]), 1, result)
            self.registry.close()
            self.open_registry()
        self.assert_no_tasks()


if __name__ == "__main__":
    unittest.main()
