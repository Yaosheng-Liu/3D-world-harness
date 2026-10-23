"""World transactions preserve identity, history and unrelated objects."""
from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from world_model import WorldConflict, WorldError, WorldStore, scene_hash, source_paths, validate_scene


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "physical_world" / "scene.json"


def _edit_in_process(project, queue, x):
    try:
        WorldStore(project).edit(1, [{"op": "update", "object_id": "block", "fields": {"position": [x, 0, 1]}}])
        queue.put("committed")
    except WorldConflict:
        queue.put("conflict")


class WorldModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scene = json.loads(EXAMPLE.read_text())
        self.store = WorldStore(self.root)

    def initialize(self):
        return self.store.initialize(self.scene)

    def update(self, object_id="block", **fields):
        return {"op": "update", "object_id": object_id, "fields": fields}

    def calibrated_view(self):
        return {"id": "heldout", "camera_to_world": [[0, -1, 0, 2], [1, 0, 0, 3], [0, 0, 1, 4], [0, 0, 0, 1]],
                "intrinsics": {"width": 64, "height": 48, "fx": 50, "fy": 60, "cx": 25, "cy": 28},
                "reference_image": "reference.png"}

    def test_calibrated_cameras_texture_paths_and_arbitrary_units(self):
        self.scene["views"] = [self.calibrated_view()]
        self.scene["units"] = "arbitrary"
        self.scene["objects"][0]["material"].update(base_color_texture="texture.png", appearance="captured")
        (self.root / "reference.png").write_bytes(b"reference")
        (self.root / "texture.png").write_bytes(b"texture")
        self.assertEqual(self.initialize()["units"], "arbitrary")
        self.assertEqual(source_paths(self.scene), ["reference.png", "texture.png"])
        for mutate in (lambda s: s["views"][0]["camera_to_world"][0].__setitem__(0, 2),
                       lambda s: s["views"][0]["camera_to_world"][3].__setitem__(0, 1),
                       lambda s: s["views"][0]["intrinsics"].update(fx=0),
                       lambda s: s["views"][0].update(reference_image="../escape.png"),
                       lambda s: s["objects"][0]["material"].update(base_color_texture="texture.exr")):
            candidate = deepcopy(self.scene)
            mutate(candidate)
            with self.assertRaises(WorldError):
                validate_scene(candidate, self.root)

    def test_constructor_and_missing_read_do_not_create_world(self):
        self.assertFalse(self.store.directory.exists())
        with self.assertRaisesRegex(WorldError, "not initialized"):
            self.store.read()
        self.assertFalse(self.store.directory.exists())

    def test_roundtrip_edit_and_restore_keep_identity_and_unrelated_objects(self):
        initial = self.initialize()
        material = dict(initial["objects"][2]["material"], base_color=[0, 0, 1, 1])
        edited = self.store.edit(1, [self.update(position=[-.25, .15, .835], material=material)])
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(self.scene, initial)
        self.assertEqual(edited["objects"][:2], initial["objects"][:2])
        self.assertEqual(edited["objects"][3:], initial["objects"][3:])
        self.assertEqual([o["id"] for o in edited["objects"]], [o["id"] for o in initial["objects"]])
        restored = self.store.restore(2, 1)
        self.assertEqual(restored, dict(initial, revision=3))
        reopened = WorldStore(self.root)
        self.assertEqual(reopened.read(), restored)
        self.assertEqual(reopened.read(2), edited)
        self.assertEqual(reopened.read(1), initial)

    def test_failed_transaction_never_changes_current_or_creates_history(self):
        self.initialize()
        before = self.store.path.read_bytes()
        operations = [self.update(position=[2, 0, 1]), self.update("ball", parent_id="missing")]
        with self.assertRaises(WorldError):
            self.store.edit(1, operations)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.store.history_dir.exists())
        with self.assertRaises(WorldError):
            self.store.edit(1, [self.update(id="renamed")])
        with self.assertRaises(WorldConflict):
            self.store.edit(2, [self.update(position=[0, 0, 1])])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_add_delete_and_parent_references_are_transactional(self):
        self.initialize()
        added = deepcopy(self.scene["objects"][2])
        added["id"] = "extra_block"
        self.store.edit(1, [{"op": "add", "object": added}])
        with self.assertRaisesRegex(WorldError, "unknown parent"):
            self.store.edit(2, [{"op": "delete", "object_id": "cabinet"}])
        result = self.store.edit(2, [{"op": "delete", "object_id": obj_id} for obj_id in ("cabinet", "door", "extra_block")])
        self.assertEqual(result["revision"], 3)
        self.assertEqual([o["id"] for o in result["objects"]], ["floor", "table", "block", "ball", "cup"])

    def test_rejects_cycles_bad_dynamics_and_nonfinite_values(self):
        mutations = [
            lambda s: s["objects"][0].update(parent_id="table") or s["objects"][1].update(parent_id="floor"),
            lambda s: s["objects"][1].update(parent_id="block"),
            lambda s: s["objects"][2].update(parent_id="table"),
            lambda s: s["objects"][2]["physics"].update(mass=0),
            lambda s: s["objects"][0]["physics"].update(mass=1),
            lambda s: s["objects"][-1]["joint"].update(axis=[0, 0, 2]),
            lambda s: s["objects"][-1]["joint"].update(range=[1, 2]),
            lambda s: s["objects"][2].update(position=[float("nan"), 0, 1]),
            lambda s: s["objects"][2]["material"].update(roughness=float("inf")),
            lambda s: s["objects"][2]["material"].update(base_color=[1, 1, 1, 0.5]),
            lambda s: s["objects"][2]["physics"].update(restitution=.5),
            lambda s: s["objects"][2].update(id="floor"),
            lambda s: s.update(revision=1.0),
            lambda s: s["views"][0].update(target=s["views"][0]["position"]),
        ]
        for mutate in mutations:
            candidate = deepcopy(self.scene)
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(WorldError):
                validate_scene(candidate)

    def test_mesh_and_evidence_paths_stay_inside_project(self):
        mesh = self.root / "cup.obj"
        mesh.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        self.scene["objects"][4]["visual"] = [{"type": "mesh", "path": "cup.obj", "scale": [1, 1, 1]}]
        self.scene["provenance"]["sources"] = [{"path": "cup.obj", "description": "Synthetic mesh fixture"}]
        validate_scene(self.scene, self.root)
        self.assertEqual(self.scene["objects"][4]["visual"][0]["path"], "cup.obj")
        for bad in ("/tmp/cup.obj", "../cup.obj", "a/../cup.obj", "a\\cup.obj", "missing.obj", "cup.glb"):
            with self.subTest(path=bad):
                candidate = deepcopy(self.scene)
                candidate["objects"][4]["visual"][0]["path"] = bad
                with self.assertRaises(WorldError):
                    validate_scene(candidate, self.root)
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "external.obj"
            external.write_text(mesh.read_text())
            (self.root / "escape.obj").symlink_to(external)
            candidate = deepcopy(self.scene)
            candidate["objects"][4]["visual"][0]["path"] = "escape.obj"
            with self.assertRaisesRegex(WorldError, "escapes"):
                validate_scene(candidate, self.root)

    def test_revision_types_duplicates_and_unknown_operations_fail_cleanly(self):
        self.initialize()
        for revision in (True, 1.0, -1, "1"):
            with self.subTest(revision=revision), self.assertRaises(WorldError):
                self.store.read(revision)
        for changes in ([], [None], [{"op": []}], [{"op": "unknown"}],
                        [self.update(position=[0, 0, 1]), self.update(name="Again")],
                        [{"op": "add", "object": self.scene["objects"][0]}]):
            with self.subTest(changes=changes), self.assertRaises(WorldError):
                self.store.edit(1, changes)
        with self.assertRaises(WorldConflict):
            self.initialize()
        with self.assertRaises(WorldError):
            self.store.restore(1, 1)
        self.assertEqual(self.store.read(), self.scene)

    def test_interrupted_replace_preserves_current_and_cleans_temporary_file(self):
        self.initialize()
        original_replace = __import__("os").replace
        def fail_current(source, destination):
            if Path(destination) == self.store.path:
                raise OSError("simulated storage failure")
            original_replace(source, destination)
        with patch("world_model.os.replace", side_effect=fail_current):
            with self.assertRaisesRegex(OSError, "storage failure"):
                self.store.edit(1, [self.update(position=[0, 0, 1])])
        self.assertEqual(self.store.read(), self.scene)
        self.assertEqual(json.loads((self.store.history_dir / "1.json").read_text()), self.scene)
        self.assertEqual(list(self.store.directory.glob(".scene.json.*")), [])
        self.assertEqual(self.store.edit(1, [self.update(position=[0, 0, 1])])["revision"], 2)

    def test_cross_process_writers_cannot_commit_the_same_revision(self):
        self.initialize()
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        processes = [context.Process(target=_edit_in_process, args=(str(self.root), queue, x)) for x in (1, 2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(queue.get(timeout=2) for _ in processes), ["committed", "conflict"])
        queue.close()
        self.assertEqual(self.store.read()["revision"], 2)
        self.assertEqual(self.store.read(1), self.scene)

    def test_world_storage_symlinks_cannot_read_or_write_another_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            foreign_scene = outside / "scene.json"
            foreign_scene.write_text(json.dumps(self.scene))
            before = foreign_scene.read_bytes()
            self.store.directory.symlink_to(outside, target_is_directory=True)
            for operation in (self.store.read, self.initialize):
                with self.assertRaisesRegex(WorldError, "storage escapes"):
                    operation()
            self.assertEqual(sorted(path.name for path in outside.iterdir()), ["scene.json"])
            self.store.directory.unlink()
            self.initialize()
            self.store.path.unlink()
            self.store.path.symlink_to(foreign_scene)
            with self.assertRaisesRegex(WorldError, "storage escapes"):
                self.store.read()
            self.store.path.unlink()
            self.store.path.write_bytes(before)
            self.store.history_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(WorldError, "storage escapes"):
                self.store.edit(1, [self.update(name="Changed")])
            self.assertEqual(self.store.path.read_bytes(), before)
            self.assertEqual(foreign_scene.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in outside.iterdir()), ["scene.json"])

    def test_hash_is_canonical_and_covers_revision(self):
        reordered = dict(reversed(list(self.scene.items())))
        self.assertEqual(scene_hash(self.scene), scene_hash(reordered))
        self.assertNotEqual(scene_hash(self.scene), scene_hash(dict(self.scene, revision=2)))
        self.assertEqual(len(scene_hash(self.scene)), 64)


if __name__ == "__main__":
    unittest.main()
