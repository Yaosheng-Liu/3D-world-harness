"""Object-level Z-up world documents and revision-checked transactions."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from jsonschema import Draft202012Validator


class WorldError(ValueError):
    """The requested world or edit is invalid."""


class WorldConflict(WorldError):
    """The caller is editing a revision that is no longer current."""


def _record(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _vector(length, item=None):
    return {"type": "array", "items": item or {"type": "number"},
            "minItems": length, "maxItems": length}


_NONNEGATIVE = {"type": "number", "minimum": 0}
_POSITIVE = {"type": "number", "exclusiveMinimum": 0}
_UNIT = {"type": "number", "minimum": 0, "maximum": 1}
_TEXT = {"type": "string", "minLength": 1}
_ID = {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_-]*$"}
_VEC3 = _vector(3)
_TRANSFORM = {"position": _VEC3, "rotation": _VEC3}
_SHAPES = {
    "box": {"size": _vector(3, _POSITIVE)},
    "sphere": {"radius": _POSITIVE},
    "cylinder": {"radius": _POSITIVE, "depth": _POSITIVE},
    "mesh": {"path": _TEXT, "scale": _vector(3, _POSITIVE)},
}


def _shapes(types):
    variants = [_record({"type": {"const": kind}, **dimensions, **_TRANSFORM},
                        ["type", *dimensions]) for kind, dimensions in _SHAPES.items() if kind in types]
    return {"type": "array", "minItems": 1, "items": {"oneOf": variants}}


_OBJECT = _record({
    "id": _ID, "name": _TEXT,
    "parent_id": {"anyOf": [_ID, {"type": "null"}]}, **_TRANSFORM,
    "visual": _shapes(_SHAPES), "collision": _shapes(("box", "sphere", "cylinder")),
    "material": _record({"base_color": _vector(4, _UNIT), "roughness": _UNIT, "metallic": _UNIT,
                         "base_color_texture": _TEXT, "appearance": {"enum": ["surface", "captured"]}},
                        ["base_color", "roughness", "metallic"]),
    "physics": _record({"body_type": {"enum": ["static", "dynamic"]},
                        "mass": _NONNEGATIVE, "friction": _NONNEGATIVE}),
    "evidence": _record({"geometry": {"enum": ["measured", "observed", "estimated", "synthetic"]},
                         "physics": {"enum": ["measured", "observed", "estimated", "synthetic"]},
                         "notes": _TEXT}),
    "joint": {"anyOf": [{"type": "null"}, _record({
        "type": {"enum": ["hinge", "slide"]}, "axis": _VEC3, "position": _VEC3,
        "range": _vector(2), "damping": _NONNEGATIVE,
    })]},
}, ["id", "name", "parent_id", "position", "rotation", "visual", "collision",
    "material", "physics", "evidence"])

WORLD_SCHEMA = _record({
    "schema_version": {"const": 1}, "revision": {"type": "integer", "minimum": 1},
    "scene_id": _ID, "name": _TEXT, "units": {"enum": ["m", "arbitrary"]}, "up_axis": {"const": "Z"},
    "provenance": _record({"kind": {"enum": ["synthetic", "reconstruction"]},
                           "description": _TEXT,
                           "sources": {"type": "array", "items": _record({"path": _TEXT, "description": _TEXT})}}),
    "objects": {"type": "array", "minItems": 1, "items": _OBJECT},
    "views": {"type": "array", "minItems": 1, "items": {"oneOf": [_record({
        "id": _ID, "position": _VEC3, "target": _VEC3,
        "fov_y_deg": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 180},
    }), _record({
        "id": _ID, "camera_to_world": _vector(4, _vector(4)),
        "intrinsics": _record({"width": {"type": "integer", "minimum": 1},
                               "height": {"type": "integer", "minimum": 1},
                               "fx": _POSITIVE, "fy": _POSITIVE, "cx": {"type": "number"}, "cy": {"type": "number"}}),
        "reference_image": _TEXT,
    })]}},
})
_VALIDATOR = Draft202012Validator(WORLD_SCHEMA)
_EDITABLE_FIELDS = set(_OBJECT["properties"]) - {"id"}


def scene_hash(scene):
    """Hash the full revision, including its evidence and fixed validation views."""
    data = json.dumps(scene, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def resolve_source_path(path, project_dir=None):
    """Resolve an existing file below the project; reject absolute/traversal paths."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path or not relative.parts:
        raise WorldError(f"Source must be a project-relative path without traversal: {path}")
    if project_dir is None:
        return relative
    root = Path(project_dir).resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise WorldError(f"Source escapes project directory: {path}")
    if not result.is_file():
        raise WorldError(f"Source file does not exist: {path}")
    return result


def source_paths(scene):
    """All explicit dependencies whose bytes must remain bound to a build."""
    paths = {source["path"] for source in scene["provenance"]["sources"]}
    paths.update(shape["path"] for obj in scene["objects"] for shape in obj["visual"] if shape["type"] == "mesh")
    paths.update(obj["material"]["base_color_texture"] for obj in scene["objects"] if "base_color_texture" in obj["material"])
    paths.update(view["reference_image"] for view in scene["views"] if "reference_image" in view)
    return sorted(paths)


def validate_scene(scene, project_dir=None):
    """Validate without mutation. Shape transforms default to zero in consumers.

    A project directory additionally checks source files and symlink containment.
    Euler rotations use radians and XYZ order; every transform is parent-local.
    """
    try:
        json.dumps(scene, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorldError(f"World must contain finite JSON values: {exc}") from exc
    error = next(_VALIDATOR.iter_errors(scene), None)
    if error:
        location = ".".join(map(str, error.absolute_path)) or "scene"
        raise WorldError(f"{location}: {error.message}")
    # JSON Schema's integer type also accepts 1.0; revisions are disk identifiers.
    if type(scene["revision"]) is not int or type(scene["schema_version"]) is not int:
        raise WorldError("schema_version and revision must be integers")
    objects = {obj["id"]: obj for obj in scene["objects"]}
    if len(objects) != len(scene["objects"]):
        raise WorldError("Object IDs must be unique")
    for obj in objects.values():
        if obj["material"]["base_color"][3] != 1:
            raise WorldError(f"{obj['id']}: version 1 supports opaque materials only (alpha=1)")
        parent = obj["parent_id"]
        ancestors = {obj["id"]}
        while parent is not None:
            if parent not in objects:
                raise WorldError(f"{obj['id']}: unknown parent {parent}")
            if parent in ancestors:
                raise WorldError(f"{obj['id']}: parent hierarchy contains a cycle")
            ancestors.add(parent)
            if obj["physics"]["body_type"] == "static" and objects[parent]["physics"]["body_type"] == "dynamic":
                raise WorldError(f"{obj['id']}: a static object cannot have a dynamic ancestor")
            parent = objects[parent]["parent_id"]
        dynamic = obj["physics"]["body_type"] == "dynamic"
        if dynamic != (obj["physics"]["mass"] > 0):
            raise WorldError(f"{obj['id']}: dynamic mass must be positive; static mass must be zero")
        joint = obj.get("joint")
        if joint:
            if not dynamic or obj["parent_id"] is None:
                raise WorldError(f"{obj['id']}: a joint requires a dynamic object and a parent")
            if not math.isclose(sum(x*x for x in joint["axis"]), 1.0, abs_tol=1e-6):
                raise WorldError(f"{obj['id']}: joint axis must be a unit vector")
            low, high = joint["range"]
            if not low < high or not low <= 0 <= high:
                raise WorldError(f"{obj['id']}: joint range must be increasing and contain the initial value zero")
        elif dynamic and obj["parent_id"] is not None:
            raise WorldError(f"{obj['id']}: a free dynamic object must be at the root")
        for shape in obj["visual"]:
            if shape["type"] == "mesh":
                if Path(shape["path"]).suffix.lower() != ".obj":
                    raise WorldError("Visual mesh sources must be OBJ files")
        texture = obj["material"].get("base_color_texture")
        if texture and Path(texture).suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise WorldError("Base color textures must be PNG or JPEG files")
    for path in source_paths(scene):
        resolve_source_path(path, project_dir)
    views = scene["views"]
    if len({view["id"] for view in views}) != len(views):
        raise WorldError("View IDs must be unique")
    for view in views:
        if "camera_to_world" not in view:
            if view["position"] == view["target"]:
                raise WorldError(f"{view['id']}: camera position and target must differ")
            continue
        matrix = view["camera_to_world"]
        rotation = [row[:3] for row in matrix[:3]]
        orthogonal = all(abs(sum(rotation[k][i] * rotation[k][j] for k in range(3)) - (i == j)) < 1e-5
                         for i in range(3) for j in range(3))
        a, b, c = rotation
        determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
        if not orthogonal or abs(determinant - 1) > 1e-5 or matrix[3] != [0, 0, 0, 1]:
            raise WorldError(f"{view['id']}: camera_to_world must be a rigid OpenGL camera transform")
        if any(type(view["intrinsics"][key]) is not int for key in ("width", "height")):
            raise WorldError("Camera width and height must be integers")
    return scene


class WorldStore:
    """One current world plus immutable previous revisions under a project.

    Writers serialize with flock. Each successful edit archives the old current
    document before atomically replacing scene.json; a failed replacement leaves
    the current world intact and at most archives that already valid revision.
    """

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).resolve()
        self.directory = self.project_dir / "world"
        self.path = self.directory / "scene.json"
        self.history_dir = self.directory / "history"

    def storage_path(self, *parts):
        """Keep current, historical and generated world files inside this project."""
        path = self.directory.joinpath(*parts).resolve()
        if not path.is_relative_to(self.project_dir):
            raise WorldError("World storage escapes the project directory")
        return path

    @contextmanager
    def _lock(self):
        self.storage_path().mkdir(parents=True, exist_ok=True)
        with self.storage_path(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self, revision=None):
        path = self.storage_path("scene.json")
        if not path.is_file():
            raise WorldError("World is not initialized; initialize world/scene.json first")
        if revision is not None:
            self._check_revision(revision)
        current = self._read_path(path)
        if revision is None or revision == current["revision"]:
            return current
        if revision >= current["revision"]:
            raise WorldError(f"World revision does not exist: {revision}")
        return self._read_path(self.storage_path("history", f"{revision}.json"))

    def _read_path(self, path):
        try:
            scene = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise WorldError(f"Cannot read world revision: {path.name}") from exc
        return validate_scene(scene, self.project_dir)

    @staticmethod
    def _check_revision(revision):
        if type(revision) is not int or revision < 1:
            raise WorldError("Revision must be a positive integer")

    def _current(self, expected_revision):
        self._check_revision(expected_revision)
        current = self.read()
        if expected_revision != current["revision"]:
            raise WorldConflict(f"Expected revision {expected_revision}, current revision is {current['revision']}")
        return current

    @staticmethod
    def _write(path, scene):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(scene, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def initialize(self, scene):
        candidate = deepcopy(scene)
        validate_scene(candidate, self.project_dir)
        if candidate["revision"] != 1:
            raise WorldError("A new world must start at revision 1")
        with self._lock():
            path = self.storage_path("scene.json")
            if path.exists():
                raise WorldConflict("World is already initialized")
            self._write(path, candidate)
        return candidate

    def _commit(self, current, candidate):
        candidate["revision"] = current["revision"] + 1
        validate_scene(candidate, self.project_dir)
        history = self.storage_path("history", f"{current['revision']}.json")
        path = self.storage_path("scene.json")
        self._write(history, current)
        self._write(path, candidate)
        return candidate

    def edit(self, expected_revision, changes):
        """Apply add/update/delete operations as one transaction.

        Updates replace named top-level object fields. IDs cannot be renamed.
        References are checked after the full transaction, allowing coordinated
        parent/child edits and deletions. Unknown fields and operations fail.
        """
        if not isinstance(changes, list) or not changes:
            raise WorldError("changes must be a non-empty list")
        with self._lock():
            current = self._current(expected_revision)
            candidate = deepcopy(current)
            objects = {obj["id"]: obj for obj in candidate["objects"]}
            touched = set()
            for change in changes:
                if not isinstance(change, dict):
                    raise WorldError("Each change must be an object")
                op = change.get("op")
                keys = {"add": {"op", "object"}, "update": {"op", "object_id", "fields"},
                        "delete": {"op", "object_id"}}
                if not isinstance(op, str) or op not in keys or set(change) != keys[op]:
                    raise WorldError("Use add(object), update(object_id, fields), or delete(object_id)")
                obj_id = change["object"].get("id") if op == "add" and isinstance(change["object"], dict) else change.get("object_id")
                if not isinstance(obj_id, str) or obj_id in touched:
                    raise WorldError("Each change needs a distinct object ID")
                touched.add(obj_id)
                if op == "add":
                    if obj_id in objects:
                        raise WorldError(f"Object already exists: {obj_id}")
                    objects[obj_id] = deepcopy(change["object"])
                else:
                    if obj_id not in objects:
                        raise WorldError(f"Object does not exist: {obj_id}")
                    if op == "delete":
                        del objects[obj_id]
                    else:
                        fields = change["fields"]
                        if not isinstance(fields, dict) or not fields or not set(fields) <= _EDITABLE_FIELDS:
                            raise WorldError("Update fields must be editable object fields; IDs are immutable")
                        objects[obj_id].update(deepcopy(fields))
            candidate["objects"] = list(objects.values())
            return self._commit(current, candidate)

    def restore(self, expected_revision, revision):
        self._check_revision(revision)
        with self._lock():
            current = self._current(expected_revision)
            if revision >= current["revision"]:
                raise WorldError("Restore requires an earlier revision")
            candidate = self.read(revision)
            return self._commit(current, candidate)
