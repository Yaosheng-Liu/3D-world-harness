import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


DIRECTORY = Path(__file__).resolve().parents[2]/"experiments/reconstruction"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("experiment_continuation", DIRECTORY/"continue_v1.py")
continuation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(continuation)


class ExperimentContinuationTests(unittest.TestCase):
    def test_clean_packet_keeps_geometry_and_textures_but_excludes_audit_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source, project = Path(directory)/"source", Path(directory)/"clean"
            scene = {"objects":[{"visual":[{"type":"mesh", "path":"models/M0/meshes/chair.obj"}],
                                 "material":{"base_color_texture":"models/M0/textures/color.jpg"}}],
                     "provenance":{"sources":[{"path":"models/M0/model.py"},
                                                 {"path":"models/M0/diagnostics/panel.jpg"}]}}
            continuation.write(source/"models/M0/candidate.json", scene)
            continuation.write(source/"models/M0/self_inspection.json", {"overlay_paths":["secret"]})
            files = {"models/M0/diagnostics/panel.jpg":b"audit", "models/M0/overlay.png":b"audit",
                     "models/M0/meshes/chair.obj":b"mesh", "models/M0/textures/color.jpg":b"texture",
                     "models/M1/old_feedback.txt":b"audit"}
            for name, data in files.items():
                p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            (source/"models/M0/model.py").write_text(str(source))
            with patch.object(continuation,"validate_scene"):
                continuation.clean_single_project(source, project)
            current = continuation.read(project/"models/M0/candidate.json")
            self.assertEqual(current["objects"], scene["objects"])
            self.assertEqual((project/"models/M0/textures/color.jpg").read_bytes(), b"texture")
            self.assertEqual((project/"models/M0/meshes/chair.obj").read_bytes(), b"mesh")
            self.assertEqual((project/"models/M0/model.py").read_text(), str(project))
            self.assertFalse((project/"models/M0/self_inspection.json").exists())
            self.assertFalse((project/"models/M0/diagnostics").exists())
            self.assertFalse((project/"models/M0/overlay.png").exists())
            self.assertFalse((project/"models/M1").exists())
            self.assertEqual(len(current["provenance"]["sources"]),1)


if __name__ == "__main__":
    unittest.main()
