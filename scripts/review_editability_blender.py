"""Independently reopen a candidate and move/rotate every object without saving it."""
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    bpy.ops.wm.open_mainfile(filepath=job["blend_path"])
    output = Path(job["output_dir"])
    roots = {obj["stable_id"]: obj for obj in bpy.data.objects if obj.get("stable_id")}
    expected = {obj["id"] for obj in job["scene"]["objects"]}
    if set(roots) != expected:
        raise ValueError("Blender object IDs differ from the candidate")
    base = {key: obj.matrix_world.copy() for key, obj in roots.items()}
    meshes = [obj for obj in bpy.data.objects if obj.type == "MESH" and obj.get("world_role") == "visual"]
    original = {obj.name: obj.matrix_world.copy() for obj in meshes}
    from world_blender import configure_camera
    scene = bpy.context.scene
    view = job["scene"]["views"][0]
    scene.camera = next(obj for obj in scene.objects if obj.get("world_view_id") == view["id"])
    configure_camera(scene, scene.camera, view)
    scene.render.threads_mode, scene.render.threads = "FIXED", 8
    scene.cycles.samples = 16
    scene.render.filepath = str(output / "edit_before.png")
    bpy.ops.render.render(write_still=True)
    demonstration = "main_bed" if "main_bed" in roots else next(iter(roots))
    results = []
    for key, root in roots.items():
        descendants = {root, *root.children_recursive}
        own_meshes = [obj for obj in meshes if obj in descendants]
        points = [obj.matrix_world @ Vector(corner) for obj in own_meshes for corner in obj.bound_box]
        if not points:
            raise ValueError(f"object has no visual geometry: {key}")
        lo = Vector([min(point[i] for point in points) for i in range(3)])
        hi = Vector([max(point[i] for point in points) for i in range(3)])
        scale = max((hi - lo).length, .001)
        pivot = base[key].translation
        pivot_ok = all(lo[i] - scale * .1 <= pivot[i] <= hi[i] + scale * .1 for i in range(3))
        delta = Matrix.Translation((scale * .2, 0, scale * .05)) @ Matrix.Translation(pivot) @ Matrix.Rotation(math.radians(20), 4, 'Z') @ Matrix.Translation(-pivot)
        root.matrix_world = delta @ base[key]
        bpy.context.view_layer.update()
        error = 0.0
        for obj in meshes:
            wanted = delta @ original[obj.name] if obj in descendants else original[obj.name]
            error = max(error, max(abs(obj.matrix_world[i][j] - wanted[i][j]) for i in range(4) for j in range(4)))
        moved = (root.matrix_world.translation - base[key].translation).length > scale * .1
        if key == demonstration:
            scene.render.filepath = str(output / "edit_after.png")
            bpy.ops.render.render(write_still=True)
        results.append({"object_id": key, "affected_objects": sorted(k for k, v in roots.items() if v in descendants),
                        "pivot_in_bounds": pivot_ok, "max_transform_error": error,
                        "passed": pivot_ok and moved and error < max(1e-5, scale * 1e-6),
                        "bounds": [list(lo), list(hi)], "pivot": list(pivot)})
        root.matrix_world = base[key]
        bpy.context.view_layer.update()
    result = {"passed": all(item["passed"] for item in results), "objects": results,
              "demonstration_object": demonstration, "demonstration_view": view["id"],
              "method": "Reopened Blender source; translate and rotate each root, check every visual mesh, restore without saving.",
              "limits": "Transform isolation does not prove semantic segmentation or absence of duplicated surfaces; critic must check images."}
    (output / "editability.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
