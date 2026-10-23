"""Real Blender acceptance: hierarchy, stable IDs, editing, reopen and rendering."""

import argparse
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from world_build import build_world
from world_model import WorldStore


def check_glb_world_matrices(receipt):
    data = Path(receipt["glb_path"]).read_bytes()
    document = json.loads(data[20:20 + struct.unpack_from("<I", data, 12)[0]])
    report = json.loads(Path(receipt["report_path"]).read_text())
    identity = [[int(i == j) for j in range(4)] for i in range(4)]
    conversion = [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]]

    def multiply(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]

    def visit(index, parent):
        node = document["nodes"][index]
        if "matrix" in node:
            local = [[node["matrix"][j * 4 + i] for j in range(4)] for i in range(4)]
        else:
            x, y, z, w = node.get("rotation", [0, 0, 0, 1])
            local = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w), 0],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w), 0],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y), 0], [0, 0, 0, 1]]
            for i in range(3):
                for j, scale in enumerate(node.get("scale", [1, 1, 1])):
                    local[i][j] *= scale
                local[i][3] = node.get("translation", [0, 0, 0])[i]
        world = multiply(parent, local)
        object_id = node.get("extras", {}).get("stable_id")
        if object_id:
            expected = multiply(multiply(conversion, report["objects"][object_id]["matrix_world"]), list(zip(*conversion)))
            assert max(abs(world[i][j] - expected[i][j]) for i in range(4) for j in range(4)) < 1e-5, object_id
        for child in node.get("children", []):
            visit(child, world)

    for index in document["scenes"][document.get("scene", 0)]["nodes"]:
        visit(index, identity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trajectory", type=Path)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()
    project = args.output or Path(tempfile.mkdtemp(prefix="world-blender-acceptance-"))
    scene = json.loads((Path(__file__).resolve().parents[1] / "examples" / "physical_world" / "scene.json").read_text())
    store = WorldStore(project)
    store.initialize(scene)
    trajectory = json.loads(args.trajectory.read_text()) if args.trajectory else None
    first = build_world(project, blender=args.blender, render=not args.no_render, trajectory=trajectory, timeout=600)
    check_glb_world_matrices(first)
    report = json.loads(Path(first["report_path"]).read_text())
    assert report["objects"]["door"]["parent_id"] == "cabinet"
    assert len(report["objects"]["cup"]["meshes"]) >= 10
    before = Path(first["blend_path"]).read_bytes()
    cup = next(obj for obj in scene["objects"] if obj["id"] == "cup")
    cup_position = [cup["position"][0] + 0.2, *cup["position"][1:]]
    material = {**cup["material"], "base_color": [0.02, 0.12, 0.8, 1]}
    updated = store.edit(1, [{"op": "update", "object_id": "cup", "fields": {
        "position": cup_position, "rotation": [0.2, 0.3, 0.4], "material": material}}])
    second = build_world(project, blender=args.blender, render=False, scene=updated, timeout=600)
    check_glb_world_matrices(second)
    after_report = json.loads(Path(second["report_path"]).read_text())
    assert first["build_dir"] != second["build_dir"]
    assert Path(first["blend_path"]).read_bytes() == before
    assert second["revision"] == 2 and first["object_ids"] == second["object_ids"]
    measured_material = after_report["objects"]["cup"]["material"]
    assert max(abs(a-b) for a, b in zip(measured_material["base_color"], material["base_color"])) < 1e-6
    assert abs(measured_material["roughness"] - material["roughness"]) < 1e-6
    if trajectory is None:
        for object_id, value in report["objects"].items():
            if object_id != "cup":
                assert after_report["objects"][object_id] == value, object_id
        actual = after_report["objects"]["cup"]["matrix_world"][0][3] - report["objects"]["cup"]["matrix_world"][0][3]
        assert abs(actual - 0.2) < 1e-5, actual
    # OBJ coordinates are explicitly metre/Z-up; no implicit Y-up import rotation.
    mesh_project = project / "obj_acceptance"
    mesh_project.mkdir()
    (mesh_project / "tetra.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 2 0\nv 0 0 3\nf 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n")
    mesh_scene = copy.deepcopy(scene)
    mesh_object = copy.deepcopy(scene["objects"][0])
    mesh_object.update(id="asset", parent_id=None, position=[0.2, 0.4, 0.6], rotation=[0, 0, 0],
                       visual=[{"type": "mesh", "path": "tetra.obj", "scale": [0.1, 0.2, 0.3], "position": [0.1, 0.2, 0.3]}])
    mesh_scene["objects"] = [mesh_object]
    WorldStore(mesh_project).initialize(mesh_scene)
    asset = build_world(mesh_project, blender=args.blender, render=False, timeout=600)
    check_glb_world_matrices(asset)
    bounds = json.loads(Path(asset["report_path"]).read_text())["objects"]["asset"]["bounds_world"]
    for measured, expected in zip(bounds, [[0.3, 0.6, 0.9], [0.4, 1.0, 1.8]]):
        assert max(abs(a-b) for a, b in zip(measured, expected)) < 1e-5, bounds
    assert set(asset["asset_hashes"]) == {"tetra.obj"}
    print(json.dumps({"project": str(project), "initial_build": first, "edited_build": second, "obj_build": asset}, indent=2))


if __name__ == "__main__":
    main()
