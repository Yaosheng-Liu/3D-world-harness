"""Ready checker CLI reports actual source fixtures and writes only by request."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO_ROOT / "scripts"), str(REPO_ROOT / "backend"), str(Path(__file__).resolve().parent)]

from check_workbench_ready import main
from core import SceneStore
from gateway import WorkspaceGateway
import test_ready_check as _ready_check_test


class ReadyCheckCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.forbidden = [
            patch.object(SceneStore, "__init__", side_effect=AssertionError("created workspace store")),
            patch.object(WorkspaceGateway, "__init__", side_effect=AssertionError("created gateway")),
            patch.object(SceneStore, "import_model", side_effect=AssertionError("imported a scene")),
            patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("created a model task")),
        ]
        self.mocks = [item.start() for item in self.forbidden]

    def tearDown(self):
        for mock in self.mocks:
            mock.assert_not_called()
        for item in self.forbidden:
            item.stop()
        self.temporary.cleanup()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def fixture(self, name="ready"):
        return _ready_check_test.ReadyCheckTests.fixture(self, name)

    def catalog(self):
        instance = self.source / "model-only"
        instance.mkdir()
        (instance / "scene.glb").write_bytes(_ready_check_test.animated_glb())
        (instance / "scene.blend").write_bytes(b"editable Blender source")
        (self.source / "manifest.json").write_text(json.dumps({"scenes": [{
            "name": "仅模型", "folder": instance.name, "glb": f"{instance.name}/scene.glb",
            "blend": f"{instance.name}/scene.blend", "start_command": "must not execute",
        }]}), encoding="utf-8")
        return instance

    def test_complete_ready_json_exit_zero_is_read_only_and_keeps_exact_metrics(self):
        self.fixture()
        before = self.snapshot()
        code, stdout, stderr = self.run_cli(str(self.source), "--json")
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["counters"]["ready"], 1)
        metrics = report["instances"][0]["metrics"]
        self.assertEqual((metrics["view_count"], metrics["frame_count"], metrics["camera_count"]), (2, 4, 4))
        self.assertEqual(metrics["animation_time_span_sec"]["end"], 1)
        self.assertEqual(stderr, "")
        self.assertEqual(before, self.snapshot())

    def test_model_only_warning_is_importable_and_human_output_names_the_limit(self):
        self.catalog()
        before = self.snapshot()
        with patch("subprocess.run", side_effect=AssertionError("executed source command")):
            code, stdout, stderr = self.run_cli(str(self.source))
        self.assertEqual(code, 0, stderr)
        self.assertIn("可导入，有提示 · 仅模型", stdout)
        self.assertIn("未附参考图或相机", stdout)
        self.assertEqual(before, self.snapshot())

    def test_partial_blocked_empty_and_invalid_paths_have_distinct_exit_codes(self):
        self.fixture("valid")
        broken, _, _ = self.fixture("broken")
        (broken / "output" / "scene.glb").write_bytes(b"broken model")
        before = self.snapshot()
        code, stdout, _ = self.run_cli(str(self.source), "--json")
        report = json.loads(stdout)
        self.assertEqual((code, report["status"], report["can_import"]), (1, "partial", True))
        self.assertEqual(report["counters"]["blocked"], 1)
        code, stdout, _ = self.run_cli(str(broken), "--json")
        self.assertEqual((code, json.loads(stdout)["status"]), (1, "blocked"))
        empty = self.root / "empty"
        empty.mkdir()
        code, stdout, _ = self.run_cli(str(empty), "--json")
        self.assertEqual((code, json.loads(stdout)["status"]), (1, "empty"))
        for path in ("relative/folder", str(self.root / "missing")):
            code, stdout, _ = self.run_cli(path, "--json")
            self.assertEqual((code, json.loads(stdout)["status"]), (2, "invalid"))
        self.assertEqual(before, self.snapshot())

    def test_explicit_output_is_exact_json_and_existing_sources_are_not_overwritten(self):
        instance = self.catalog()
        before = self.snapshot()
        destination = self.root / "report.json"
        code, stdout, stderr = self.run_cli(str(self.source), "--json", "--output", str(destination))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(destination.read_text(encoding="utf-8"), stdout)
        self.assertIn(str(destination), stderr)
        after = self.snapshot()
        self.assertEqual(set(after) - set(before), {"report.json"})
        self.assertTrue(all(after[path] == data for path, data in before.items()))
        for target in (instance / "scene.glb", self.source / "manifest.json", destination):
            saved = target.read_bytes()
            code, stdout, _ = self.run_cli(str(self.source), "--json", "--output", str(target))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["status"], "invalid")
            self.assertEqual(target.read_bytes(), saved)

    def test_truncated_success_report_is_not_reported_as_complete(self):
        self.catalog()
        from ready_check import check_ready_folder
        report = check_ready_folder(self.source)
        report["truncated"] = True
        with patch("check_workbench_ready.check_ready_folder", return_value=report):
            code, stdout, _ = self.run_cli(str(self.source))
        self.assertEqual(code, 1)
        self.assertIn("尚未检查全部目录", stdout)


if __name__ == "__main__":
    unittest.main()
