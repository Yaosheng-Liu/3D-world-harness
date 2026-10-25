"""Independently reopen a candidate and move/rotate every object without saving it."""
import json
import math
from pathlib import Path
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))


def best_inspection_view(scene, points, views, cameras):
    """Choose by clipped projected bounds; this does not establish visibility."""
    from world_blender import configure_camera
    choices = []
    for view in views:
        camera = cameras[view["id"]]
        configure_camera(scene, camera, view)
        projected = [world_to_camera_view(scene, camera, point) for point in points]
        front = [point for point in projected if point.z > camera.data.clip_start]
        if not front:
            continue
        x0, x1 = max(0., min(point.x for point in front)), min(1., max(point.x for point in front))
        y0, y1 = max(0., min(point.y for point in front)), min(1., max(point.y for point in front))
        area = max(0., x1 - x0) * max(0., y1 - y0)
        if area <= 1e-6:
            continue
        choices.append((area * len(front) / len(points), view,
                        [x0, 1 - y1, x1, 1 - y0], area))
    return max(choices, key=lambda item: item[0]) if choices else None


def object_inspections(scene, roots, meshes, views, object_ids, output):
    from world_blender import configure_camera
    cameras = {obj["world_view_id"]: obj for obj in scene.objects if obj.get("world_view_id")}
    directory = output / "object_edits"
    directory.mkdir(exist_ok=True)
    result = []
    scene.cycles.samples = 4
    for key in object_ids:
        row = {"object_id": key, "status": "missing"}
        root = roots.get(key)
        if root is None:
            result.append(row)
            continue
        descendants = {root, *root.children_recursive}
        points = [obj.matrix_world @ Vector(corner) for obj in meshes if obj in descendants
                  for corner in obj.bound_box]
        chosen = best_inspection_view(scene, points, views, cameras) if points else None
        if chosen is None:
            result.append({**row, "status": "not_visible"})
            continue
        _, view, region, area = chosen
        scene.camera = cameras[view["id"]]
        configure_camera(scene, scene.camera, view)
        scene.render.resolution_percentage = max(1, min(100, int(48000 / max(
            scene.render.resolution_x, scene.render.resolution_y))))
        # Keep surrounding surfaces in frame to expose baked replicas and holes.
        x0, y0, x1, y1 = region
        padx, pady = max(.04, (x1 - x0) * .2), max(.04, (y1 - y0) * .2)
        region = [max(0., x0 - padx), max(0., y0 - pady), min(1., x1 + padx), min(1., y1 + pady)]
        before, hidden = directory / f"{key}-before.png", directory / f"{key}-hidden.png"
        scene.render.filepath = str(before)
        bpy.ops.render.render(write_still=True)
        original = {obj: obj.hide_render for obj in descendants}
        try:
            for obj in descendants:
                obj.hide_render = True
            bpy.context.view_layer.update()
            scene.render.filepath = str(hidden)
            bpy.ops.render.render(write_still=True)
        finally:
            for obj, value in original.items():
                obj.hide_render = value
            bpy.context.view_layer.update()
        result.append({**row, "status": "rendered", "view_id": view["id"], "region": region,
                       "projected_fraction": area, "before_path": str(before), "hidden_path": str(hidden)})
    return result


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
    inspections = object_inspections(scene, roots, meshes, job["scene"]["views"],
                                     job.get("inspection_object_ids", []), output)
    result = {"passed": all(item["passed"] for item in results), "objects": results,
              "demonstration_object": demonstration, "demonstration_view": view["id"],
              "object_inspections": inspections,
              "visual_checks_complete": all(item["status"] == "rendered" for item in inspections),
              "visibility_method": "Fixed view with largest clipped projected object bounds, weighted by front-facing corner count.",
              "visibility_limits": "Projection does not prove visibility through occluders. Critic must inspect each before/hidden pair; no visible fixed view blocks coverage.",
              "method": "Reopened Blender source; translate and rotate each root, check every visual mesh, restore without saving.",
              "limits": "Transform isolation does not prove semantic segmentation or absence of duplicated surfaces; critic must check images."}
    (output / "editability.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
