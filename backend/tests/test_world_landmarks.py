"""Known-camera recovery and refusal of geometrically uninformative observations."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from world_landmarks import fit_landmarks


class LandmarkTests(unittest.TestCase):
    def setUp(self):
        self.views = []
        self.point = np.array([.3, -.2, -4.])
        observations = []
        for index, x in enumerate((-1., .2, 1.)):
            pose = np.eye(4)
            # Rotated camera and off-centre intrinsics catch coordinate-convention errors.
            theta = .1 * index
            pose[:3, :3] = [[np.cos(theta), 0, np.sin(theta)], [0, 1, 0],
                           [-np.sin(theta), 0, np.cos(theta)]]
            pose[:3, 3] = [x, .1 * index, 0.]
            camera = np.linalg.inv(pose) @ [*self.point, 1.]
            pixel = [700 * camera[0] / -camera[2] + 410, 650 * camera[1] / camera[2] + 290]
            view = {"id": str(index), "reference_image": f"{index}.png", "camera_to_world": pose.tolist(),
                    "intrinsics": {"width": 960, "height": 540, "fx": 700, "fy": 650, "cx": 410, "cy": 290}}
            self.views.append(view)
            observations.append({"view_id": str(index), "pixel": pixel})
        self.landmarks = [{"id": "corner", "observations": observations}]

    def test_recovers_world_point_without_depth_or_candidate_geometry(self):
        result = fit_landmarks(self.views, self.landmarks)[0]
        np.testing.assert_allclose(result["position_world"], self.point, atol=1e-10)
        self.assertLess(result["max_error_px"], 1e-9)
        self.assertGreater(result["parallax_deg"], 20)

    def test_inconsistent_correspondence_exposes_reprojection_error(self):
        self.landmarks[0]["observations"][1]["pixel"][1] += 25
        result = fit_landmarks(self.views, self.landmarks)[0]
        self.assertGreater(result["max_error_px"], 10)

    def test_same_camera_cannot_supply_triangulation_baseline(self):
        views = [deepcopy(self.views[0]), deepcopy(self.views[0])]
        views[1]["id"] = "other"
        a = self.landmarks[0]["observations"][0]
        points = [{"id": "corner", "observations": [a, {**a, "view_id": "other"}]}]
        with self.assertRaisesRegex(ValueError, "baseline"):
            fit_landmarks(views, points)

    def test_model_only_views_and_wrong_pixel_coordinates_are_rejected(self):
        del self.views[0]["reference_image"]
        with self.assertRaisesRegex(ValueError, "reference image"):
            fit_landmarks(self.views, self.landmarks)
        self.views[0]["reference_image"] = "0.png"
        self.landmarks[0]["observations"][0]["pixel"] = [1200, 50]
        with self.assertRaisesRegex(ValueError, "inside"):
            fit_landmarks(self.views, self.landmarks)


if __name__ == "__main__":
    unittest.main()
