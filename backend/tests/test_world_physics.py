"""Physical behavior tests run with the optional MuJoCo dependency installed."""

import copy
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from world_model import WorldError, scene_hash
from world_physics import export_mjcf, simulate


def box(size, position=None, rotation=None):
    return {"type": "box", "size": size, "position": position or [0, 0, 0],
            "rotation": rotation or [0, 0, 0]}


def obj(name, position, shape, *, mass=0, parent=None, joint=None):
    result = {"id": name, "name": name, "parent_id": parent, "position": position,
              "rotation": [0, 0, 0], "visual": [shape], "collision": [copy.deepcopy(shape)],
              "material": {"base_color": [0.5, 0.5, 0.5, 1], "roughness": 0.5, "metallic": 0},
              "physics": {"body_type": "dynamic" if mass else "static", "mass": mass, "friction": 0.6},
              "evidence": {"geometry": "synthetic", "physics": "synthetic", "notes": "Unit-test fixture"}}
    if joint:
        result["joint"] = joint
    return result


def scene(*objects):
    return {"schema_version": 1, "revision": 1, "scene_id": "physics_test", "name": "Physics test",
            "units": "m", "up_axis": "Z",
            "provenance": {"kind": "synthetic", "description": "Unit-test fixture", "sources": []},
            "objects": list(objects),
            "views": [{"id": "front", "position": [2, -2, 2], "target": [0, 0, 0], "fov_y_deg": 50}]}


def falling_scene(mass=1):
    return scene(obj("floor", [0, 0, -0.05], box([4, 4, 0.1])),
                 obj("block", [0, 0, 0.5], box([0.1, 0.1, 0.1]), mass=mass))


class PhysicsValidationTests(unittest.TestCase):
    def test_invalid_runs_fail_before_loading_optional_engine(self):
        for options in ({"duration_sec": 11}, {"duration_sec": float("nan")}, {"sample_hz": 61},
                        {"duration_sec": True}, {"action": {"type": "teleport"}},
                        {"action": {"type": "impulse", "object_id": "floor", "vector": [1, 0, 0]}},
                        {"action": {"type": "force", "object_id": "block", "vector": [1, 0, 0], "duration_sec": 3}},
                        {"action": {"type": "impulse", "object_id": "block", "vector": [math.inf, 0, 0]}},
                        {"action": {"type": "joint_force", "object_id": "block", "force": 1, "duration_sec": 1}}):
            with self.subTest(options=options), self.assertRaises(WorldError):
                simulate(falling_scene(), **options)

    def test_missing_engine_is_explicit(self):
        with patch.dict(sys.modules, {"mujoco": None}), self.assertRaisesRegex(WorldError, "requirements-world"):
            simulate(falling_scene())

    def test_visual_is_not_a_collision_hull(self):
        world = falling_scene()
        world["objects"][1]["visual"] = [box([5, 5, 5])]
        xml = export_mjcf(world)
        self.assertNotIn('size="2.5 2.5 2.5"', xml)
        self.assertIn('size="0.05 0.05 0.05"', xml)

    def test_finite_but_unrepresentable_volumes_are_rejected(self):
        world = falling_scene()
        for shape in (box([1e300, 1e300, 1e300]), {"type": "sphere", "radius": 1e300},
                      box([1e-300, 1e-300, 1e-300])):
            world["objects"][1]["collision"] = [shape]
            with self.subTest(shape=shape), self.assertRaises(WorldError):
                export_mjcf(world)


@unittest.skipUnless(importlib.util.find_spec("mujoco"), "Install backend/requirements-world.txt for physical tests")
class MuJoCoTests(unittest.TestCase):
    def test_gravity_settles_on_floor_and_saves_reproducible_evidence(self):
        world = falling_scene()
        original = copy.deepcopy(world)
        with tempfile.TemporaryDirectory() as directory:
            result = simulate(world, output_dir=Path(directory))
            final = result["trajectory"]["poses"]["block"][-1]["position"]
            self.assertAlmostEqual(final[2], 0.05, delta=0.002)
            self.assertTrue(result["report"]["passed"], result["report"])
            self.assertFalse(result["report"]["real_world_calibrated"])
            self.assertEqual(result["report"]["scene_hash"], scene_hash(world))
            times = result["trajectory"]["timestamps"]
            self.assertEqual((times[0], times[-1]), (0, 2))
            self.assertTrue(all(a < b for a, b in zip(times, times[1:])))
            for name, path in result["artifacts"].items():
                self.assertTrue(Path(path).is_file())
                if name != "mjcf":
                    self.assertEqual(json.loads(Path(path).read_text()), result[name])
        self.assertEqual(world, original)

    def test_impulse_and_force_obey_mass_not_a_recorded_motion(self):
        positions = []
        for mass in (1, 2):
            world = scene(obj("block", [0, 0, 10], box([0.1, 0.1, 0.1]), mass=mass))
            result = simulate(world, duration_sec=0.2,
                              action={"type": "impulse", "object_id": "block", "vector": [1, 0, 0]})
            positions.append(result["trajectory"]["poses"]["block"][-1]["position"][0])
        self.assertAlmostEqual(positions[0], 0.2, places=6)
        self.assertAlmostEqual(positions[0] / positions[1], 2, places=6)
        result = simulate(world, duration_sec=0.2, action={"type": "force", "object_id": "block",
                                                         "vector": [10, 0, 0], "duration_sec": 0.2})
        self.assertAlmostEqual(result["trajectory"]["poses"]["block"][-1]["position"][0], 0.1, delta=0.002)

    def test_hinge_and_slide_are_force_driven_and_respect_limits(self):
        for kind, axis, high in (("hinge", [0, 0, 1], 0.5), ("slide", [1, 0, 0], 0.2)):
            with self.subTest(joint=kind):
                joint = {"type": kind, "position": [0, 0, 0], "axis": axis,
                         "range": [0, high], "damping": 0.1}
                world = scene(obj("base", [-1, 0, 0], box([0.1, 0.1, 0.1])),
                              obj("part", [1, 0, 0.5], box([0.4, 0.04, 0.2], [0.2, 0, 0]),
                                  mass=0.5, parent="base", joint=joint))
                result = simulate(world, duration_sec=2, action={"type": "joint_force", "object_id": "part",
                                                                "force": 0.2, "duration_sec": 2})
                self.assertTrue(result["report"]["passed"], result["report"])
                self.assertAlmostEqual(result["trajectory"]["joints"]["part"][-1], high, delta=0.003)
                self.assertGreater(len(set(result["trajectory"]["joints"]["part"])), 10)

    def test_child_collides_with_parent_before_reaching_joint_limit(self):
        world = scene(obj("base", [0, 0, 0], box([0.2, 0.2, 0.2])),
                      obj("slider", [0.3, 0, 0], box([0.1, 0.1, 0.1]), mass=0.5, parent="base",
                          joint={"type": "slide", "axis": [-1, 0, 0], "position": [0, 0, 0],
                                 "range": [0, 0.5], "damping": 0.1}))
        result = simulate(world, duration_sec=1, action={"type": "joint_force", "object_id": "slider",
                                                        "force": 1, "duration_sec": 1})
        self.assertAlmostEqual(result["trajectory"]["joints"]["slider"][-1], 0.15, delta=0.002)
        self.assertTrue(result["report"]["passed"], result["report"])

    def test_sliding_friction_changes_impulse_response(self):
        distances = []
        for friction in (0, 0.6):
            world = falling_scene()
            world["objects"][1]["position"][2] = 0.05
            for body in world["objects"]:
                body["physics"]["friction"] = friction
            result = simulate(world, duration_sec=1,
                              action={"type": "impulse", "object_id": "block", "vector": [0.3, 0, 0]})
            distances.append(result["trajectory"]["poses"]["block"][-1]["position"][0])
        self.assertAlmostEqual(distances[0], 0.3, delta=0.003)
        self.assertLess(distances[1], 0.03)

    def test_composite_cup_has_a_real_open_cavity(self):
        cup = obj("cup", [0, 0, 0.1], {"type": "cylinder", "radius": 0.065, "depth": 0.2})
        cup["collision"] = [box([0.13, 0.13, 0.02], [0, 0, -0.09])]
        for index in range(16):
            angle = 2 * math.pi * index / 16
            cup["collision"].append(box([0.008, 0.026, 0.18],
                                         [0.06 * math.cos(angle), 0.06 * math.sin(angle), 0], [0, 0, angle]))
        world = scene(cup, obj("ball", [0, 0, 0.35], {"type": "sphere", "radius": 0.015}, mass=0.05))
        result = simulate(world, duration_sec=1)
        final_height = result["trajectory"]["poses"]["ball"][-1]["position"][2]
        self.assertAlmostEqual(final_height, 0.035, delta=0.002)
        self.assertTrue(result["report"]["passed"], result["report"])

    def test_overlap_is_a_failed_acceptance_report(self):
        world = falling_scene()
        world["objects"][1]["position"][2] = 0
        result = simulate(world, duration_sec=0.1)
        self.assertFalse(result["report"]["passed"])
        self.assertFalse(result["report"]["checks"]["penetration"]["passed"])
        self.assertEqual(set(result["report"]["checks"]["penetration"]["worst_contact"]["object_ids"]),
                         {"floor", "block"})

    def test_parent_local_composite_euler_matches_rz_ry_rx(self):
        import numpy as np
        parent = obj("base", [1, 2, 3], box([0.1, 0.1, 0.1]))
        parent["rotation"] = [0.3, 0.4, 0.5]
        child = obj("child", [1, 0, 0], box([0.1, 0.1, 0.1]), parent="base")
        result = simulate(scene(parent, child), duration_sec=0.002)
        expected = np.array([1, 2, 3]) + [math.cos(0.5) * math.cos(0.4),
                                        math.sin(0.5) * math.cos(0.4), -math.sin(0.4)]
        np.testing.assert_allclose(result["trajectory"]["poses"]["child"][0]["position"], expected, atol=1e-10)


if __name__ == "__main__":
    unittest.main()
