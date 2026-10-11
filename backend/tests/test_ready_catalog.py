"""Explicit scene catalogs import local models without guessing evidence paths."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ready_import import discover_ready_instances
from dynamic import reference_views
import test_folder_import as _folder_import

glb_bytes = _folder_import.glb_bytes


class ReadyCatalogTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing isolated registry harness, never a running service.
        self.case = _folder_import.FolderImportTests("test_static_images_without_clip_manifest_are_ready")
        self.case.setUp()
        self.root = self.case.source

    def tearDown(self):
        self.case.tearDown()

    def scene(self, folder, name=None):
        path = self.root / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / "scene.glb").write_bytes(glb_bytes())
        (path / "scene.blend").write_bytes(b"editable source")
        return {"name": name or folder, "folder": folder,
                "glb": f"{folder}/scene.glb", "blend": f"{folder}/scene.blend"}

    def catalog(self, scenes, root=None, **extra):
        marker = (root or self.root) / "manifest.json"
        marker.write_text(json.dumps({"scenes": scenes, **extra}), encoding="utf-8")
        return marker

    def test_five_model_only_entries_keep_exact_names_models_and_sources(self):
        scenes = [self.scene(f"0{i}_vehicle", f"Assembly101 {i} 动态人体") for i in range(1, 6)]
        for scene in scenes:
            scene.update(source_glb="/unavailable/original.glb", source_blend="/unavailable/original.blend",
                         readiness_evidence="do not run this text", start_command="touch should-never-exist")
        marker = self.catalog(scenes, directory="/stale/export/location", scope="workbench-ready scenes")
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        active_before = self.case.registry.root.store.state_path.read_bytes()
        result = self.case.import_path()
        self.assertEqual((len(result["imported"]), len(result["errors"]), len(result["skipped"])), (5, 0, 0), result)
        self.assertEqual(result["counters"]["candidates"], 5)
        self.assertEqual([project["name"] for project in result["projects"]], [scene["name"] for scene in scenes])
        for project, scene in zip(result["projects"], scenes):
            context = self.case.registry.get(project["project_id"])
            provenance = project["import_source"]
            self.assertEqual(context.project_dir, self.root / scene["folder"])
            self.assertEqual(provenance["format"], "scene_catalog")
            self.assertEqual(provenance["marker"], str(marker))
            self.assertIsNone(provenance["manifest"])
            self.assertNotIn("reference_images", provenance)
            self.assertEqual(provenance["glb"], str(self.root / scene["glb"]))
            self.assertEqual(provenance["blend"], str(self.root / scene["blend"]))
            session = context.store.get_session(project["session_id"])
            self.assertFalse(session["reference_images"])
            self.assertIsNone(session.get("reference_clip"))
            self.assertEqual(context.store.scene()["objects"][0]["metadata"]["source_name"], "scene.glb")
        self.assertEqual(self.case.registry.root.store.state_path.read_bytes(), active_before)
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.case.assert_no_tasks()

    def test_catalog_uses_declared_glb_even_when_other_outputs_exist(self):
        scene = self.scene("selected")
        (self.root / "selected" / "newer.glb").write_bytes(b"not a model and never selected")
        self.catalog([scene])
        discovery = discover_ready_instances(self.root)
        self.assertEqual(discovery.instances[0].glb, self.root / "selected" / "scene.glb")
        result = self.case.import_path()
        self.assertEqual(result["counters"]["imported"], 1, result)

    def test_partial_failures_retry_and_source_duplicates_reuse_existing_state(self):
        good = self.scene("good")
        late = self.scene("late")
        (self.root / late["glb"]).unlink()
        self.catalog([good, late, {**good, "name": "duplicate name"}, {"folder": "bad"}])
        request_id = uuid.uuid4().hex
        first = self.case.import_path(request_id=request_id)
        self.assertEqual((len(first["imported"]), len(first["errors"]), len(first["skipped"])), (1, 2, 1), first)
        context = self.case.registry.get(first["projects"][0]["project_id"])
        context.store.state["feedback"].append({"feedback_id": "preserved"})
        context.store._save()
        before = context.store.state_path.read_bytes()
        (self.root / late["glb"]).write_bytes(glb_bytes())
        self.catalog([good, late, {**good, "name": "duplicate name"}])
        retry = self.case.import_path(request_id=request_id)
        self.assertEqual((len(retry["imported"]), len(retry["errors"]), len(retry["skipped"])), (1, 0, 2), retry)
        repeat = self.case.import_path()
        self.assertEqual((len(repeat["imported"]), len(repeat["errors"])), (0, 0), repeat)
        self.assertEqual(len(repeat["projects"]), 2)
        self.assertEqual(len(self.case.factory_calls), 2)
        self.assertEqual(context.store.state_path.read_bytes(), before)
        self.case.assert_no_tasks()

    def test_catalog_paths_and_symlinks_cannot_escape_the_declared_instance(self):
        outside = self.case.root / "outside"
        outside.mkdir()
        (outside / "scene.glb").write_bytes(glb_bytes())
        (outside / "scene.blend").write_bytes(b"source")
        good = self.scene("good")
        other = self.scene("other")
        (self.root / "linked_outside").symlink_to(outside, target_is_directory=True)
        linked_file = self.root / "good" / "linked.glb"
        linked_file.symlink_to(outside / "scene.glb")
        mutations = [
            {**good, "folder": str(outside)},
            {**good, "folder": "../outside"},
            {**good, "folder": "."},
            {**good, "folder": "linked_outside", "glb": "linked_outside/scene.glb"},
            {**good, "glb": str(outside / "scene.glb")},
            {**good, "glb": other["glb"]},
            {**good, "glb": "good/linked.glb"},
            {**good, "blend": str(outside / "scene.blend")},
            {**good, "reference_images": [str(outside / "scene.glb")]},
            {**good, "reference_manifest": str(outside / "scene.glb")},
        ]
        self.catalog([good, *mutations])
        result = self.case.import_path()
        self.assertEqual((len(result["imported"]), len(result["errors"])), (1, len(mutations)), result)
        self.assertEqual(len(self.case.factory_calls), 1)
        self.assertEqual((outside / "scene.glb").read_bytes(), glb_bytes())

    def test_internal_folder_aliases_are_deduplicated_by_canonical_root(self):
        scene = self.scene("original")
        (self.root / "alias").symlink_to(self.root / "original", target_is_directory=True)
        alias = {**scene, "folder": "alias", "glb": "alias/scene.glb", "blend": "alias/scene.blend"}
        self.catalog([scene, alias])
        discovery = discover_ready_instances(self.root)
        self.assertEqual(len(discovery.instances), 1)
        self.assertEqual(discovery.instances[0].root, self.root / "original")
        self.assertEqual(len(discovery.skipped), 1)

    def test_optional_reference_paths_use_catalog_parent_and_remain_scoped(self):
        source, marker, document = self.case.fixture("with_refs")
        marker.unlink()
        scene = {"folder": source.name, "glb": f"{source.name}/output/scene.glb",
                 "reference_manifest": f"{source.name}/references/multiview.json",
                 "reference_images": [f"{source.name}/references/frame.png"]}
        self.catalog([scene])
        result = self.case.import_path()
        self.assertEqual(result["counters"]["imported"], 1, result)
        context = self.case.registry.get(result["projects"][0]["project_id"])
        session = context.store.get_session(result["projects"][0]["session_id"])
        self.assertEqual(len(reference_views(session["reference_clip"])), 2)
        self.assertEqual(len(session["reference_images"]), 1)

    def test_ready_false_and_invalid_entries_do_not_hide_valid_siblings(self):
        scene = self.scene("good")
        self.catalog([{**scene, "ready": False}, 3, scene, {"name": "missing folder", "glb": "missing.glb"}])
        result = self.case.import_path()
        self.assertEqual((len(result["imported"]), len(result["errors"]), len(result["skipped"])), (1, 2, 1), result)

    def test_unrelated_or_malformed_manifests_do_not_hide_ready_descendants(self):
        ready, _, _ = self.case.fixture("ready")
        for document in ({"scenes": [{"name": "a glTF scene", "nodes": [0]}]},
                         {"scenes": "unrelated metadata"}, {"frames": []}, None):
            with self.subTest(document=document):
                marker = self.root / "manifest.json"
                marker.write_text(json.dumps(document) if document is not None else "invalid JSON")
                discovery = discover_ready_instances(self.root)
                self.assertEqual([instance.root for instance in discovery.instances], [ready])
                self.assertFalse(discovery.errors)

    def test_nested_catalogs_obey_depth_and_entry_scan_limits(self):
        nested = self.root / "collection"
        nested.mkdir()
        scene = self.scene("collection/deeper/instance")
        relative = {"folder": "deeper/instance", "glb": "deeper/instance/scene.glb",
                    "blend": "deeper/instance/scene.blend"}
        self.catalog([relative], root=nested)
        discovered = discover_ready_instances(self.root)
        self.assertEqual([instance.root for instance in discovered.instances], [self.root / scene["folder"]])
        limited = discover_ready_instances(self.root, max_depth=2)
        self.assertTrue(limited.truncated)
        self.assertFalse(limited.instances)
        self.assertIn("depth limit", limited.skipped[0]["reason"])
        self.catalog([self.scene("one"), self.scene("two"), self.scene("three")])
        limited = discover_ready_instances(self.root, max_entries=2)
        self.assertTrue(limited.truncated)
        self.assertEqual(len(limited.instances), 2)
        self.assertIn("entry limit", limited.errors[0]["error"])


if __name__ == "__main__":
    unittest.main()
