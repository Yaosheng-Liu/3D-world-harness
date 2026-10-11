"""Read-only discovery of explicit or established ready scene directories."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from core import APIError


MAX_SCAN_DEPTH = 5
MAX_SCAN_ENTRIES = 20_000
MAX_FOLDER_ENTRIES = 2_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
STANDARD_MARKERS = ("workbench-ready.json", "workbench_ready.json")
# These directories contain evidence, generated revisions or installed software,
# rather than independent scene instances. Inspect their known marker paths only.
IGNORED_DIRECTORIES = {
    "workbench", "references", "output", "review", "revisions", "history",
    "archive", "archives", "backup", "backups", "frames", "images", "rgb",
    "undistorted", "renders", "media", "assets", "node_modules", "venv",
    "env", "core_env", "vision_env", "__pycache__", "site-packages",
}


@dataclass(frozen=True)
class ReadyInstance:
    root: Path
    name: str
    glb: Path
    manifest: Path | None
    format: str
    marker: Path
    reference_images: tuple[Path, ...] = ()
    blend: Path | None = None
    source_revision: Any = None
    camera_manifest: Path | None = None
    world_up: str | None = None

    def provenance(self) -> dict[str, Any]:
        result = {"root": str(self.root), "glb": str(self.glb),
                  "manifest": str(self.manifest) if self.manifest is not None else None, "format": self.format,
                  "marker": str(self.marker), "read_only": True}
        if self.blend is not None:
            result["blend"] = str(self.blend)
        if self.source_revision is not None:
            result["source_revision"] = self.source_revision
        if self.reference_images:
            result["reference_images"] = [str(path) for path in self.reference_images]
        if self.camera_manifest is not None:
            result["camera_manifest"] = str(self.camera_manifest)
        if self.world_up is not None:
            result["world_up"] = self.world_up
        return result


@dataclass
class Discovery:
    path: Path
    instances: list[ReadyInstance] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    directories_scanned: int = 0
    entries_scanned: int = 0
    candidates: int = 0
    truncated: bool = False


def host_directory(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise APIError(400, "folder path must be an absolute directory path")
    try:
        original = Path(value).expanduser()
        if not original.is_absolute():
            raise APIError(400, "folder path must be an absolute directory path")
        path = original.resolve(strict=True)
        if not path.is_dir():
            raise APIError(400, "folder path must point to a directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise APIError(400, "folder cannot be read or does not exist") from exc
    return path


def browse_folders(path: Any = None, *, default: str | Path | None = None) -> dict[str, Any]:
    directory = host_directory(path if path is not None else (default or Path.home()))
    directories = []
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append({"name": entry.name, "path": str(directory / entry.name)})
                    if len(directories) >= MAX_FOLDER_ENTRIES:
                        truncated = True
                        break
    except OSError as exc:
        raise APIError(403, "folder cannot be listed") from exc
    directories.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    return {"path": str(directory), "parent": str(directory.parent) if directory.parent != directory else None,
            "directories": directories, "truncated": truncated}


def _scoped_file(root: Path, value: Any, *, base: Path | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise APIError(400, "ready scene needs a local file path")
    try:
        original = Path(value).expanduser()
        path = (original if original.is_absolute() else (base or root) / original).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise APIError(400, "ready scene file does not exist") from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise APIError(400, "ready scene files must remain inside their instance directory")
    return path


def _document(root: Path, path: Path, *, limit: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    path = _scoped_file(root, str(path))
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise APIError(400, "ready scene manifest exceeds the size limit")
        document = json.loads(data)
    except (OSError, UnicodeError, ValueError) as exc:
        raise APIError(400, "ready scene manifest cannot be read as JSON") from exc
    if not isinstance(document, dict):
        raise APIError(400, "ready scene manifest must be an object")
    return document


def _validate_reference_manifest(root: Path, path: Path, *, depth: int = 0, group: bool = True) -> None:
    """Check all paths before allocating managed data; importer validates bytes/cameras."""
    if depth > 4:
        raise APIError(400, "reference manifest nesting exceeds the limit")
    document = _document(root, path, limit=MAX_MANIFEST_BYTES if group else 2 * 1024 * 1024)

    def view(spec: Any, base: Path) -> None:
        if not isinstance(spec, dict):
            raise APIError(400, "reference view must be an object")
        sources = [key for key in ("manifest_path", "video_path", "frames") if key in spec]
        if len(sources) != 1:
            raise APIError(400, "reference view needs manifest_path, video_path or frames")
        if spec.get("camera_manifest_path") is not None:
            _document(root, _scoped_file(root, spec["camera_manifest_path"], base=base), limit=2 * 1024 * 1024)
        source = sources[0]
        if source == "manifest_path":
            _validate_reference_manifest(root, _scoped_file(root, spec[source], base=base), depth=depth + 1, group=False)
        elif source == "video_path":
            _scoped_file(root, spec[source], base=base)
        else:
            frames = spec["frames"]
            if not isinstance(frames, list) or not 1 <= len(frames) <= 600:
                raise APIError(400, "reference view must contain 1 to 600 frames")
            for frame in frames:
                if not isinstance(frame, dict):
                    raise APIError(400, "reference frame must be an object")
                _scoped_file(root, frame.get("path"), base=base)

    if "views" in document:
        if not group:
            raise APIError(400, "nested multi-view reference manifests are not supported")
        views = document["views"]
        if not isinstance(views, list) or not 1 <= len(views) <= 8:
            raise APIError(400, "reference manifest must contain 1 to 8 views")
        for spec in views:
            view(spec, path.parent)
    else:
        view(document, path.parent)


def _instance(root: Path, marker: Path, document: dict[str, Any], format: str) -> ReadyInstance:
    if format == "standard" and (type(document.get("schema_version")) is not int or document["schema_version"] != 1):
        raise APIError(400, "workbench ready manifest requires schema_version 1")
    if format == "ready_package":
        document = {**document, "scene_glb_path": document.get("scene_glb"),
                    "reference_manifest": document.get("reference_sequence"),
                    "blend": document.get("editable_blend")}
    name = document.get("name", root.name)
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(ch) < 32 for ch in name):
        raise APIError(400, "ready scene name must contain 1 to 120 printable characters")
    glb = _scoped_file(root, document.get("scene_glb_path", document.get("glb")))
    if glb.suffix.lower() != ".glb":
        raise APIError(400, "ready scene model must be a GLB file")
    reference = document.get("reference_manifest")
    if reference is None and isinstance(document.get("reference_clip"), dict):
        reference = document["reference_clip"].get("manifest_path")
    images = document.get("reference_images", [])
    if not isinstance(images, list) or len(images) > 8:
        raise APIError(400, "reference_images must contain at most 8 local image paths")
    references = tuple(_scoped_file(root, image) for image in images)
    manifest = _scoped_file(root, reference) if reference is not None else None
    if manifest is not None:
        _validate_reference_manifest(root, manifest)
    elif not references and format != "scene_catalog":
        raise APIError(400, "ready scene needs a reference manifest or reference_images")
    blend = _scoped_file(root, document["blend"]) if document.get("blend") is not None else None
    camera_manifest = None
    if document.get("camera_manifest") is not None:
        camera_manifest = _scoped_file(root, document["camera_manifest"])
        _document(root, camera_manifest, limit=2 * 1024 * 1024)
        if manifest is None:
            raise APIError(400, "camera_manifest requires a reference sequence")
        sequence = _document(root, manifest)
        if "views" in sequence:
            raise APIError(400, "declare cameras within each view of a multi-view reference manifest")
        if sequence.get("camera_manifest_path") is not None:
            declared = _scoped_file(root, sequence["camera_manifest_path"], base=manifest.parent)
            if declared != camera_manifest:
                raise APIError(400, "ready package and reference sequence declare different camera manifests")
    world_up = document.get("world_up")
    if world_up is not None:
        if not isinstance(world_up, str) or world_up.lower() not in {"y", "z"}:
            raise APIError(400, "world_up must be Y or Z")
        world_up = world_up.lower()
    return ReadyInstance(root, name.strip(), glb, manifest, format, marker, references, blend,
                         document.get("scene_revision"), camera_manifest, world_up)


def _scene_catalog(root: Path) -> tuple[Path, dict[str, Any]] | None:
    """Recognize explicit scene directories, rather than arbitrary manifest files."""
    marker = root / "manifest.json"
    if not marker.exists():
        return None
    try:
        document = _document(root, marker)
    except APIError:
        # Many unrelated exports use manifest.json. A malformed or differently
        # shaped document cannot claim its directory or hide ready descendants.
        return None
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not any(
        isinstance(scene, dict) and ("folder" in scene or "glb" in scene) for scene in scenes
    ):
        return None
    return marker, document


def _catalog_instance(root: Path, marker: Path, scene: Any) -> ReadyInstance:
    if not isinstance(scene, dict):
        raise APIError(400, "scene catalog entries must be objects")
    value = scene.get("folder")
    if not isinstance(value, str) or not value:
        raise APIError(400, "scene catalog entry needs a relative instance folder")
    folder = Path(value)
    if folder.is_absolute() or not folder.parts or ".." in folder.parts:
        raise APIError(400, "scene catalog folders must be relative subdirectories")
    instance_root = host_directory(root / folder)
    if instance_root == root or not instance_root.is_relative_to(root):
        raise APIError(400, "scene catalog folders must remain inside the catalog directory")
    # All catalog paths are relative to the catalog, and every visual/editable
    # asset must resolve within its declared instance. source_* and evidence
    # metadata are provenance only, never fallbacks or commands to execute.
    document = {**scene, "scene_glb_path": str(_scoped_file(instance_root, scene.get("glb"), base=root))}
    if scene.get("blend") is not None:
        blend = _scoped_file(instance_root, scene["blend"], base=root)
        if blend.suffix.lower() != ".blend":
            raise APIError(400, "scene catalog editable source must be a Blender file")
        document["blend"] = str(blend)
    reference = scene.get("reference_manifest")
    if reference is None and isinstance(scene.get("reference_clip"), dict):
        reference = scene["reference_clip"].get("manifest_path")
    if reference is not None:
        document["reference_manifest"] = str(_scoped_file(instance_root, reference, base=root))
    images = scene.get("reference_images", [])
    if not isinstance(images, list) or len(images) > 8:
        raise APIError(400, "reference_images must contain at most 8 local image paths")
    document["reference_images"] = [str(_scoped_file(instance_root, image, base=root)) for image in images]
    if scene.get("camera_manifest") is not None:
        document["camera_manifest"] = str(_scoped_file(instance_root, scene["camera_manifest"], base=root))
    return _instance(instance_root, marker, document, "scene_catalog")


def _discover_catalog(root: Path, depth: int, catalog: tuple[Path, dict[str, Any]],
                      result: Discovery, seen: set[Path], *, max_depth: int, max_entries: int) -> None:
    marker, document = catalog
    if document.get("ready") is False:
        result.candidates += 1
        result.skipped.append({"path": str(root), "reason": "catalog explicitly declares ready:false"})
        return
    for index, scene in enumerate(document["scenes"]):
        result.entries_scanned += 1
        if result.entries_scanned > max_entries:
            result.truncated = True
            result.errors.append({"path": str(root), "error": "folder scan reached its entry limit"})
            break
        result.candidates += 1
        label = str(marker) + f"#scenes[{index}]"
        try:
            if isinstance(scene, dict) and scene.get("ready") is False:
                result.skipped.append({"path": label, "reason": "instance explicitly declares ready:false"})
                continue
            instance = _catalog_instance(root, marker, scene)
            label = str(instance.root)
            if depth + len(instance.root.relative_to(root).parts) > max_depth:
                result.truncated = True
                result.skipped.append({"path": label, "reason": "folder scan reached its depth limit"})
            elif instance.root in seen:
                result.skipped.append({"path": label, "reason": "source directory is already declared by this folder scan"})
            else:
                seen.add(instance.root)
                result.directories_scanned += 1
                result.instances.append(instance)
        except (APIError, OSError, RuntimeError, ValueError) as exc:
            result.errors.append({"path": label, "error": exc.message if isinstance(exc, APIError) else str(exc)[:500]})


def _inspect(root: Path) -> tuple[bool, ReadyInstance | None, str | None]:
    standards = [root / name for name in STANDARD_MARKERS if (root / name).exists()]
    active = root / "workbench" / "active_scene.json"
    multiview = root / "references" / "multiview.json"
    if standards:
        if len(standards) != 1:
            raise APIError(400, "ambiguous ready manifests; keep one explicit ready manifest")
        marker, format = standards[0], "standard"
    elif active.exists():
        marker, format = active, "active_scene"
    elif multiview.exists():
        marker, format = multiview, "multiview_output"
    else:
        return False, None, None
    document = _document(root, marker)
    if document.get("ready") is False:
        return True, None, "instance explicitly declares ready:false"
    if format == "standard" and "schema_version" not in document and any(
        field in document for field in ("scene_glb", "reference_sequence")
    ):
        # Existing video-ready packages predate the versioned marker. Their
        # explicit status and filenames select assets; never run start_command.
        if document.get("status") != "ready":
            return True, None, "ready package status is not ready"
        format = "ready_package"
    if format == "multiview_output":
        # The group itself is evidence; only a unique exported output GLB selects
        # the model. Do not guess from scripts, filenames or modification times.
        if "views" not in document:
            raise APIError(400, "references/multiview.json must contain a multi-view group")
        output = root / "output"
        if not output.is_dir():
            return True, None, "instance has no ready output GLB"
        output = host_directory(str(output))
        if not output.is_relative_to(root):
            raise APIError(400, "ready output directory must remain inside its instance directory")
        glbs = []
        with os.scandir(output) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_FOLDER_ENTRIES:
                    raise APIError(400, "output directory exceeds the discovery limit; provide an explicit ready manifest")
                if entry.name.lower().endswith(".glb"):
                    glbs.append(_scoped_file(root, str(output / entry.name)))
        if not glbs:
            return True, None, "instance has no ready output GLB"
        if len(glbs) > 1:
            raise APIError(400, "ambiguous output GLBs; provide an explicit workbench-ready.json")
        document = {"name": document.get("name", root.name), "glb": str(glbs[0]), "reference_manifest": str(marker)}
    return True, _instance(root, marker, document, format), None


def discover_ready_instances(path: Any, *, max_depth: int = MAX_SCAN_DEPTH,
                             max_entries: int = MAX_SCAN_ENTRIES) -> Discovery:
    directory = host_directory(path)
    result = Discovery(directory)
    seen: set[Path] = set()
    pending = [(directory, 0)]
    while pending:
        level, pending = pending, []
        unrecognized = []
        # Inspect every nearby instance before descending into any large raw
        # data directory at the same depth. A file limit still reports truncation.
        for root, depth in level:
            result.directories_scanned += 1
            try:
                if root.is_symlink():
                    continue
                candidate, instance, reason = _inspect(root)
                if not candidate:
                    catalog = _scene_catalog(root)
                    if catalog is not None:
                        _discover_catalog(root, depth, catalog, result, seen,
                                          max_depth=max_depth, max_entries=max_entries)
                        continue
                    unrecognized.append((root, depth))
                    continue
                result.candidates += 1
                if instance is not None:
                    if instance.root not in seen:
                        seen.add(instance.root)
                        result.instances.append(instance)
                else:
                    result.skipped.append({"path": str(root), "reason": reason})
                # A recognized instance owns its history and evidence tree.
            except (APIError, OSError, RuntimeError, ValueError) as exc:
                result.candidates += 1
                result.errors.append({"path": str(root), "error": exc.message if isinstance(exc, APIError) else str(exc)[:500]})
        for root, depth in unrecognized:
            try:
                children = []
                with os.scandir(root) as entries:
                    for entry in entries:
                        result.entries_scanned += 1
                        if result.entries_scanned > max_entries:
                            result.truncated = True
                            result.errors.append({"path": str(root), "error": "folder scan reached its entry limit"})
                            pending.clear()
                            break
                        if entry.name.startswith(".") or entry.name.lower() in IGNORED_DIRECTORIES or entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth >= max_depth:
                                result.truncated = True
                                result.skipped.append({"path": str(root / entry.name), "reason": "folder scan reached its depth limit"})
                            else:
                                children.append((root / entry.name, depth + 1))
                if result.entries_scanned > max_entries:
                    break
                pending.extend(sorted(children, key=lambda item: str(item[0])))
            except (OSError, RuntimeError) as exc:
                result.errors.append({"path": str(root), "error": str(exc)[:500]})
    if not result.instances and not result.skipped and not result.errors:
        result.skipped.append({"path": str(directory), "reason": "no recognized ready scene instances found"})
    return result
