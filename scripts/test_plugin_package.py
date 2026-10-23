"""Exercise distributable package boundaries without touching Codex configuration."""

from __future__ import annotations

import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import configure_plugin
from build_plugin import COMPAT_MANIFEST, FIXED_FILES, MARKETPLACE_CATALOG_PATHS, MCP_SCHEMA, NAME, PLUGIN_SCHEMA, runtime_files, sync_compatibility, validate_package, write_zip
from configure_plugin import authoring_catalog, configure


class PluginPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scene-plugin-package-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in FIXED_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime content\n")
        (self.root / "plugin.json").write_text(json.dumps({
            "$schema": PLUGIN_SCHEMA, "name": NAME, "version": "0.1.0",
            "extensions": {"com.openai": {"interface": {
                "displayName": "Scene Feedback", "shortDescription": "Visual feedback",
                "longDescription": "Annotate reference images and reconstructed scenes.",
                "logo": "./assets/icon.svg", "composerIcon": "./assets/icon.svg",
            }}},
        }))
        (self.root / "mcp.json").write_text(json.dumps({
            "$schema": MCP_SCHEMA,
            "mcpServers": {"scene_feedback": {"type": "stdio", "command": "python3",
                "args": ["${PLUGIN_ROOT}/scripts/plugin_bridge.py"], "cwd": "${PLUGIN_ROOT}"}},
        }))
        (self.root / "skills" / "visual-feedback").mkdir(parents=True)
        (self.root / "skills" / "visual-feedback" / "SKILL.md").write_text("Review visual evidence.\n")
        (self.root / "backend").mkdir()
        (self.root / "backend" / "server.py").write_text("# runtime\n")
        (self.root / "web").mkdir()
        (self.root / "web" / "index.html").write_text("<html></html>\n")
        (self.root / "assets").mkdir()
        (self.root / "assets" / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128"/>\n')
        self.files = runtime_files(self.root)

    def test_runtime_allowlist_and_reproducible_zip(self):
        for relative in (".venv/auth.json", "backend/data/control_token", "backend/tests/test_runtime.py"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("do not distribute\n")
        files = runtime_files(self.root)
        self.assertEqual(files, self.files)
        first, second = self.root / "one.zip", self.root / "two.zip"
        write_zip(files, first)
        write_zip(files, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertTrue(all(name.startswith(NAME + "/") for name in archive.namelist()))
            self.assertNotIn(NAME + "/backend/data/control_token", archive.namelist())
            for name in ("docs/physical-world.md", "scripts/world.py", "scripts/world_blender.py",
                         "examples/physical_world/scene.json", "docs/multiview-reconstruction.md",
                         "scripts/reconstruct_multiview.py", "scripts/multiview_uv.py"):
                self.assertIn(NAME + "/" + name, archive.namelist())

    def test_symlink_and_secret_files_are_rejected(self):
        (self.root / "web" / "linked.js").symlink_to(self.root / "backend" / "server.py")
        with self.assertRaisesRegex(ValueError, "regular file"):
            runtime_files(self.root)
        (self.root / "web" / "linked.js").unlink()
        (self.root / "web" / ".env").write_text("SECRET=test\n")
        with self.assertRaisesRegex(ValueError, "Credentials"):
            runtime_files(self.root)
        with self.assertRaisesRegex(ValueError, "Disallowed"):
            validate_package({**self.files, "../escape.py": b"invalid"})

    def test_repository_marketplace_is_excluded_from_single_plugin_zip(self):
        catalog = self.root / ".agents" / "plugins" / "marketplace.json"
        catalog.parent.mkdir(parents=True)
        content = json.dumps({"name": "local-review", "plugins": [{"name": NAME, "source": "../../"}]}).encode()
        catalog.write_bytes(content)
        files = runtime_files(self.root)
        self.assertEqual(files, self.files)
        self.assertEqual(catalog.read_bytes(), content)
        archive_path = self.root / "single-plugin.zip"
        write_zip(files, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertFalse(any(name.endswith("marketplace.json") for name in archive.namelist()))

    def test_nested_marketplace_catalogs_are_rejected(self):
        for catalog in MARKETPLACE_CATALOG_PATHS:
            for prefix in ((), ("web", "vendor")):
                relative = "/".join((*prefix, *catalog))
                with self.subTest(catalog=relative):
                    files = {**self.files, relative: b'{"plugins": []}'}
                    with self.assertRaisesRegex(ValueError, "single-plugin archive"):
                        validate_package(files)
                    output = self.root / "invalid.zip"
                    with self.assertRaisesRegex(ValueError, "single-plugin archive"):
                        write_zip(files, output)
                    self.assertFalse(output.exists())
            # Recursive runtime collection must catch a catalog hidden in assets,
            # skills or web rather than letting it enter a distributable ZIP.
            for directory in ("web", "skills", "assets"):
                path = self.root / directory / Path(*catalog)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"plugins": []}')
                with self.assertRaisesRegex(ValueError, "single-plugin archive"):
                    runtime_files(self.root)
                path.unlink()

    def test_external_tracking_runtime_cannot_enter_plugin_archive(self):
        for name in ("backend/pose_worker.py", "backend/pose_launcher.py", "scripts/run_pose_worker.py",
                     "backend/requirements-pose.txt", "external-skills/capsule-human-tracking/SKILL.md"):
            with self.subTest(path=name):
                with self.assertRaisesRegex(ValueError, "External capsule tracking"):
                    validate_package({**self.files, name: b"external skill resource"})

    def test_authoring_catalog_copies_valid_source_or_generates_for_extracted_package(self):
        generated = authoring_catalog(self.root)
        self.assertEqual(generated["plugins"][0]["source"], {"source": "local", "path": "./"})
        catalog = self.root / ".agents" / "plugins" / "marketplace.json"
        catalog.parent.mkdir(parents=True)
        generated["interface"]["displayName"] = "Existing local catalog"
        catalog.write_text(json.dumps(generated))
        self.assertEqual(authoring_catalog(self.root), generated)
        generated["plugins"][0]["source"]["path"] = "../../another-plugin"
        catalog.write_text(json.dumps(generated))
        with self.assertRaisesRegex(ValueError, "only the configured plugin"):
            authoring_catalog(self.root)
        catalog.write_text("[]")
        with self.assertRaisesRegex(ValueError, "only the configured plugin"):
            authoring_catalog(self.root)

    def test_configuration_cli_writes_authoring_catalog_separately_from_runtime_zip(self):
        data, project = self.root / "data", self.root / "project"
        data.mkdir()
        project.mkdir()
        modes = {
            "local": ["--project-id", "a" * 32, "--data-dir", str(data),
                      "--project-dir", str(project), "--port", "18769"],
            "hosted": ["--app-id", "plugin_asdk_app_registered_test"],
        }
        for mode, arguments in modes.items():
            with self.subTest(mode=mode):
                output = self.root / mode
                argv = ["configure_plugin.py", *arguments, "--output", str(output)]
                with patch.object(configure_plugin, "ROOT", self.root), \
                        patch.object(configure_plugin, "runtime_files", return_value=self.files), \
                        patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                    self.assertEqual(configure_plugin.main(), 0)
                catalog = output / ".agents" / "plugins" / "marketplace.json"
                self.assertEqual(json.loads(catalog.read_bytes()), authoring_catalog(self.root))
                files = runtime_files(output)
                self.assertNotIn(".agents/plugins/marketplace.json", files)
                validate_package(files)
                archive_path = self.root / f"{mode}.zip"
                write_zip(files, archive_path)
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertFalse(any(name.endswith("marketplace.json") for name in archive.namelist()))

    def test_local_configuration_preserves_project_identity_without_token(self):
        data, project = self.root / "data", self.root / "project"
        data.mkdir()
        project.mkdir()
        (data / "control_token").write_text("sensitive-test-token")
        result = configure(self.files, project_id="a" * 32, data_dir=data, project_dir=project, port=18769)
        env = json.loads(result["mcp.json"])["mcpServers"]["scene_feedback"]["env"]
        self.assertEqual(env["SCENE_FEEDBACK_DATA_DIR"], str(data))
        self.assertEqual(env["SCENE_FEEDBACK_PROJECT_ID"], "a" * 32)
        compat_server = json.loads(result[".mcp.json"])["mcpServers"]["scene_feedback"]
        self.assertEqual(compat_server["env"], env)
        self.assertEqual(compat_server["args"], ["./scripts/plugin_bridge.py"])
        self.assertEqual(compat_server["cwd"], ".")
        self.assertFalse(any(b"sensitive-test-token" in content for content in result.values()))
        for url in ("http://192.168.3.157:18769/p/" + "a" * 32 + "/mcp", "https://example.com/p/" + "b" * 32 + "/mcp"):
            with self.assertRaises(ValueError):
                configure(self.files, project_id="a" * 32, data_dir=data, project_dir=project, port=18769, mcp_url=url)
        local = self.root / "local"
        for relative, content in result.items():
            path = local / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.assertEqual(runtime_files(local), result)

    def test_hosted_mapping_has_no_local_server_or_registration_side_effect(self):
        result = configure(self.files, app_id="plugin_asdk_app_registered_test")
        self.assertNotIn("mcp.json", result)
        self.assertNotIn(".mcp.json", result)
        compat = json.loads(result[COMPAT_MANIFEST])
        self.assertNotIn("mcpServers", compat)
        self.assertEqual(compat["apps"], "./.app.json")
        self.assertEqual(json.loads(result[".app.json"])["apps"]["scene_feedback"]["id"], "plugin_asdk_app_registered_test")
        self.assertEqual(json.loads(result["plugin.json"])["extensions"]["com.openai"]["apps"], "./.app.json")
        with self.assertRaises(ValueError):
            configure(self.files, app_id="https://example.com/mcp")
        with self.assertRaises(ValueError):
            configure(self.files, app_id="plugin_asdk_app_registered_test", project_id="a" * 32)
        # A configured hosted directory must be independently rebuildable.
        hosted = self.root / "hosted"
        for relative, content in result.items():
            path = hosted / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.assertEqual(runtime_files(hosted), result)

    def test_listing_metadata_and_icons_are_required(self):
        for field in ("longDescription", "logo", "composerIcon"):
            with self.subTest(field=field):
                manifest = json.loads(self.files["plugin.json"])
                manifest["extensions"]["com.openai"]["interface"].pop(field)
                files = dict(self.files)
                files["plugin.json"] = json.dumps(manifest).encode()
                with self.assertRaisesRegex(ValueError, field):
                    validate_package(sync_compatibility(files))
        files = {name: content for name, content in self.files.items() if name != "assets/icon.svg"}
        with self.assertRaisesRegex(ValueError, "icon is missing"):
            validate_package(files)
        with self.assertRaisesRegex(ValueError, "square"):
            validate_package({**self.files, "assets/icon.svg": b'<svg width="128" height="64" viewBox="0 0 128 64"/>'})

    def test_compatibility_manifest_and_mcp_configuration_must_stay_synced(self):
        manifest = json.loads(self.files["plugin.json"])
        compat = json.loads(self.files[COMPAT_MANIFEST])
        self.assertEqual(compat["interface"], manifest["extensions"]["com.openai"]["interface"])
        self.assertEqual(compat["version"], manifest["version"])
        self.assertEqual(compat["skills"], "./skills/")
        self.assertEqual(compat["mcpServers"], "./.mcp.json")
        for name in (COMPAT_MANIFEST, ".mcp.json"):
            with self.subTest(file=name):
                files = {**self.files, name: b'{}'}
                with self.assertRaisesRegex(ValueError, "out of sync"):
                    validate_package(files)


if __name__ == "__main__":
    unittest.main()
