"""Revision, identity and failure boundaries for Blender builds."""

import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world_build import build_world, inspect_glb, validate_trajectory
from world_model import WorldError, WorldStore, scene_hash


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "physical_world" / "scene.json"


class WorldBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.scene = json.loads(EXAMPLE.read_text())
        WorldStore(self.project).initialize(self.scene)

    def tearDown(self):
        self.temporary.cleanup()

    def trajectory(self):
        return {"scene_id": self.scene["scene_id"], "revision": self.scene["revision"],
                "engine": {"name": "mujoco", "version": "test"},
                "scene_hash": scene_hash(self.scene), "timestamps": [0, 0.1],
                "poses": {obj["id"]: [{"position": [0, 0, 0], "quaternion": [1, 0, 0, 0]}] * 2
                          for obj in self.scene["objects"]}}

    def test_trajectory_rejects_stale_revision_incomplete_poses_and_invalid_rotations(self):
        valid = self.trajectory()
        validate_trajectory(valid, self.scene)
        invalid = []
        stale = copy.deepcopy(valid)
        stale["revision"] += 1
        invalid.append(stale)
        missing = copy.deepcopy(valid)
        missing["poses"].pop(next(iter(missing["poses"])))
        invalid.append(missing)
        rotation = copy.deepcopy(valid)
        next(iter(rotation["poses"].values()))[0]["quaternion"] = [2, 0, 0, 0]
        invalid.append(rotation)
        for trajectory in invalid:
            with self.subTest(trajectory=trajectory), self.assertRaises(WorldError):
                validate_trajectory(trajectory, self.scene)

    def test_worker_failure_preserves_existing_source_and_never_produces_receipt(self):
        original = self.project / "scene.blend"
        original.write_bytes(b"user-owned Blender source")
        with patch("world_build.shutil.which", return_value="/usr/bin/blender"), patch(
                "world_build.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["blender"])):
            with self.assertRaisesRegex(WorldError, "World build failed; see"):
                build_world(self.project, render=False)
        self.assertEqual(original.read_bytes(), b"user-owned Blender source")
        builds = list((self.project / "world" / "builds").iterdir())
        self.assertEqual(len(builds), 1)
        self.assertTrue((builds[0] / "blender.log").exists())
        self.assertFalse((builds[0] / "receipt.json").exists())

    def test_build_directory_symlink_cannot_write_outside_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            (self.project / "world" / "builds").symlink_to(outside, target_is_directory=True)
            with patch("world_build.shutil.which", return_value="/usr/bin/blender"), patch(
                    "world_build.subprocess.run") as worker:
                with self.assertRaisesRegex(WorldError, "storage escapes"):
                    build_world(self.project, render=False)
                worker.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])

    def test_missing_runtime_creates_no_failed_revision(self):
        with patch("world_build.shutil.which", return_value=None):
            with self.assertRaisesRegex(WorldError, "Blender executable not found"):
                build_world(self.project)
        self.assertFalse((self.project / "world" / "builds").exists())

    def test_glb_identity_gate_rejects_missing_duplicate_and_collision_nodes(self):
        path = self.project / "test.glb"

        def write(nodes):
            document = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes}
            data = json.dumps(document).encode()
            data += b" " * (-len(data) % 4)
            path.write_bytes(struct.pack("<4sIII4s", b"glTF", 2, 20 + len(data), len(data), b"JSON") + data)

        node = {"extras": {"stable_id": "cup", "world_role": "object"}}
        write([node])
        self.assertEqual(inspect_glb(path, {"cup"})["object_ids"], ["cup"])
        for nodes in ([], [node, node], [node, {"extras": {"world_role": "collision"}}]):
            write(nodes)
            with self.subTest(nodes=nodes), self.assertRaises(WorldError):
                inspect_glb(path, {"cup"})

    def test_animation_timing_comes_from_exported_binary(self):
        document = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
                    "nodes": [{"extras": {"stable_id": "cup"}}], "buffers": [{"byteLength": 48}],
                    "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 12},
                                    {"buffer": 0, "byteOffset": 12, "byteLength": 36}],
                    "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "SCALAR", "min": [0], "max": [0.2]},
                                  {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC3"}],
                    "animations": [{"samplers": [{"input": 0, "output": 1}],
                                    "channels": [{"sampler": 0, "target": {"node": 0, "path": "translation"}}]}]}
        encoded = json.dumps(document).encode()
        encoded += b" " * (-len(encoded) % 4)
        binary = struct.pack("<12f", 0, 0.1, 0.2, 0, 0, 0, 0.1, 0, 0, 0.2, 0, 0)
        data = struct.pack("<4sIII4s", b"glTF", 2, 28 + len(encoded) + len(binary), len(encoded), b"JSON")
        path = self.project / "animation.glb"
        path.write_bytes(data + encoded + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)
        preview = inspect_glb(path, {"cup"})
        self.assertAlmostEqual(preview["animation_duration_sec"], 0.2)
        self.assertEqual(preview["animation_start_sec"], 0)
        self.assertEqual(preview["animated_object_ids"], ["cup"])


if __name__ == "__main__":
    unittest.main()
