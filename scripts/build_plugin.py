#!/usr/bin/env python3
"""Build a reproducible plugin ZIP from an explicit runtime file allowlist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
NAME = "scene-feedback-harness"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
COMPAT_MANIFEST = ".codex-plugin/plugin.json"
MARKETPLACE_CATALOG_PATHS = (
    (".agents", "plugins", "marketplace.json"),
    (".claude-plugin", "marketplace.json"),
    (".codex-plugin", "marketplace.json"),
)
FIXED_FILES = (
    "plugin.json", "mcp.json", "LICENSE", "README.md", "README.en.md",
    "README.ja.md", "README.ko.md", "README.ru.md", "CHANGELOG.md", "CONTRIBUTING.md", "preview.png",
    "docs/mcp-events-plugin.md", "docs/folder-import.md", "scripts/plugin_bridge.py",
    "scripts/build_plugin.py",
    "scripts/configure_plugin.py",
    "scripts/start_blank_workbench.py",
    "scripts/check_workbench_ready.py",
)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def compatibility_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """Derive legacy Codex files from the portable manifest, never hand edit them."""
    manifest = json.loads(files["plugin.json"])
    extension = manifest.get("extensions", {}).get("com.openai", {})
    compat = {key: value for key, value in manifest.items() if key not in {"$schema", "extensions"}}
    compat.update(extension)
    compat["skills"] = "./skills/"
    result = {}
    if extension.get("apps"):
        compat.pop("mcpServers", None)
    else:
        compat["mcpServers"] = "./.mcp.json"
        mcp = json.loads(files["mcp.json"])
        servers = {}
        for name, portable in mcp.get("mcpServers", {}).items():
            server = {key: value for key, value in portable.items() if key != "type"}
            # Legacy relative cwd is resolved against the plugin root. Keep
            # arguments relative too, so no template expansion is required.
            if server.get("cwd") == "${PLUGIN_ROOT}":
                server["cwd"] = "."
                server["args"] = [
                    "./" + arg.removeprefix("${PLUGIN_ROOT}/")
                    if isinstance(arg, str) and arg.startswith("${PLUGIN_ROOT}/") else arg
                    for arg in server.get("args", [])
                ]
            servers[name] = server
        result[".mcp.json"] = json_bytes({"mcpServers": servers})
    result[COMPAT_MANIFEST] = json_bytes(compat)
    return result


def sync_compatibility(files: dict[str, bytes]) -> dict[str, bytes]:
    result = {name: content for name, content in files.items() if name not in {COMPAT_MANIFEST, ".mcp.json"}}
    result.update(compatibility_files(result))
    return result


def runtime_files(root: Path = ROOT) -> dict[str, bytes]:
    root = root.resolve()
    selected = set(FIXED_FILES)
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("extensions", {}).get("com.openai", {}).get("apps"):
        selected.discard("mcp.json")
        selected.add(".app.json")
    selected.update(str(path.relative_to(root)) for path in (root / "backend").glob("*.py"))
    selected.update(str(path.relative_to(root)) for path in (root / "backend").glob("requirements*.txt"))
    for directory in ("web", "skills", "assets"):
        selected.update(str(path.relative_to(root)) for path in (root / directory).rglob("*") if path.is_file())
    result = {}
    for relative in sorted(selected):
        path = root / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Plugin file is missing or is not a contained regular file: {relative}")
        # Reject directory symlinks too: a runtime package must contain its own bytes.
        if any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
            raise ValueError(f"Plugin file traverses a symlink: {relative}")
        result[relative] = path.read_bytes()
    result = sync_compatibility(result)
    validate_package(result)
    return result


def validate_package(files: dict[str, bytes]) -> None:
    manifest = json.loads(files["plugin.json"])
    if manifest.get("$schema") != PLUGIN_SCHEMA or manifest.get("name") != NAME:
        raise ValueError("Plugin identity or portable schema is invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")):
        raise ValueError("Plugin version must be a semantic release version")
    if "skills/visual-feedback/SKILL.md" not in files or "scripts/plugin_bridge.py" not in files:
        raise ValueError("Plugin is missing its review skill or bridge")
    extension = manifest.get("extensions", {}).get("com.openai", {})
    interface = extension.get("interface", {})
    for field in ("displayName", "shortDescription", "longDescription"):
        if not isinstance(interface.get(field), str) or not interface[field].strip():
            raise ValueError(f"Plugin listing requires interface.{field}")
    for field in ("logo", "composerIcon"):
        icon = interface.get(field)
        if not isinstance(icon, str) or not icon.startswith("./") or ".." in Path(icon).parts:
            raise ValueError(f"Plugin listing requires a contained interface.{field} path")
        relative = icon.removeprefix("./")
        if relative not in files:
            raise ValueError(f"Plugin listing icon is missing: {relative}")
        if Path(relative).suffix.lower() != ".svg":
            raise ValueError("This package uses SVG listing icons")
        try:
            svg = ET.fromstring(files[relative])
            view_box = [float(value) for value in svg.get("viewBox", "").replace(",", " ").split()]
            width = float(svg.get("width", "").removesuffix("px"))
            height = float(svg.get("height", "").removesuffix("px"))
        except (ValueError, ET.ParseError) as exc:
            raise ValueError(f"Listing icon needs numeric SVG dimensions: {relative}") from exc
        if (svg.tag.rsplit("}", 1)[-1] != "svg" or width != height or width < 48
                or len(view_box) != 4 or view_box[2] != view_box[3] or view_box[2] <= 0):
            raise ValueError(f"Listing icon must be square and at least 48 pixels: {relative}")
    if extension.get("apps"):
        if extension["apps"] != "./.app.json" or ".app.json" not in files:
            raise ValueError("Hosted mapping must use the contained .app.json")
        apps = json.loads(files[".app.json"]).get("apps", {})
        app_id = apps.get("scene_feedback", {}).get("id", "")
        if not re.fullmatch(r"plugin_asdk_app_[A-Za-z0-9_-]+", app_id):
            raise ValueError("Hosted mapping requires a registered plugin_asdk_app ID")
        if "mcp.json" in files:
            raise ValueError("Hosted package must not also connect the local bridge")
    else:
        mcp = json.loads(files["mcp.json"])
        server = mcp.get("mcpServers", {}).get("scene_feedback", {})
        if (mcp.get("$schema") != MCP_SCHEMA or server.get("type") != "stdio"
                or server.get("command") != "python3"
                or server.get("args") != ["${PLUGIN_ROOT}/scripts/plugin_bridge.py"]
                or server.get("cwd") != "${PLUGIN_ROOT}"):
            raise ValueError("Local plugin must use its contained stdio bridge")
    expected_compatibility = compatibility_files(files)
    for name, content in expected_compatibility.items():
        if name not in files or json.loads(files[name]) != json.loads(content):
            raise ValueError(f"Derived compatibility configuration is missing or out of sync: {name}")
    if extension.get("apps") and ".mcp.json" in files:
        raise ValueError("Hosted package must not include a legacy local bridge")
    for name in files:
        path = Path(name)
        if path.name in {"pose_worker.py", "pose_launcher.py", "run_pose_worker.py", "requirements-pose.txt"} or "external-skills" in path.parts:
            raise ValueError(f"External capsule tracking resources must not be bundled in the workbench plugin: {name}")
        # Catalogs describe a collection of plugins. They belong in the source
        # checkout, never inside an archive submitted as one plugin. Check
        # suffixes too, since runtime directories can contain nested catalogs.
        if any(path.parts[-len(catalog):] == catalog for catalog in MARKETPLACE_CATALOG_PATHS):
            raise ValueError(f"A single-plugin archive cannot contain a marketplace catalog: {name}")
        if path.is_absolute() or ".." in path.parts or any(part in {".venv", ".git", "__pycache__", "data", "tests"} for part in path.parts):
            raise ValueError(f"Disallowed package path: {name}")
        if path.name in {"control_token", "auth.json", "connection.json"}:
            raise ValueError(f"Secrets and workspace connection state cannot be bundled: {name}")
        if path.name == ".env" or path.name.startswith(".env.") or path.suffix in {".pem", ".key", ".pth", ".pt", ".safetensors"}:
            raise ValueError(f"Credentials and model weights cannot be bundled: {name}")


def write_zip(files: dict[str, bytes], output: Path) -> None:
    validate_package(files)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in sorted(files.items()):
            info = zipfile.ZipInfo(f"{NAME}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT, help="Repository or configured plugin directory")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"{NAME}.zip")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.suffix != ".zip" or any(output.is_relative_to(source / directory) for directory in ("backend", "web", "skills")):
        parser.error("Choose a .zip output outside runtime source directories")
    files = runtime_files(args.source)
    write_zip(files, output)
    print(f"Built {output} ({len(files)} runtime files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
