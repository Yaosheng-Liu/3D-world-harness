"""Read a frozen Blender candidate and sample camera-Z of its visual geometry."""
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
import numpy as np


def inspect(job):
    bpy.ops.wm.open_mainfile(filepath=job["blend_path"])
    scene = bpy.context.scene
    if scene.get("world_scene_hash") != job["scene_hash"]:
        raise ValueError("Blender scene does not match the candidate")
    scene.frame_set(1)
    graph = bpy.context.evaluated_depsgraph_get()
    vertices, triangles = [], []
    count = 0
    for obj in scene.objects:
        if obj.type != "MESH" or obj.get("world_role") != "visual" or obj.hide_render:
            continue
        evaluated = obj.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        mesh.calc_loop_triangles()
        xyz = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", xyz)
        transform = np.asarray(evaluated.matrix_world)
        xyz = xyz.reshape(-1, 3) @ transform[:3, :3].T + transform[:3, 3]
        faces = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", faces)
        vertices.append(xyz)
        triangles.append(faces.reshape(-1, 3) + count)
        count += len(xyz)
        evaluated.to_mesh_clear()
    if not triangles:
        raise ValueError("candidate has no visual meshes")
    tree = BVHTree.FromPolygons(np.concatenate(vertices).tolist(), np.concatenate(triangles).tolist(), all_triangles=True)
    h, w = job["height"], job["width"]
    y, x = np.mgrid[:h, :w]
    pixels = np.stack((x, y, np.ones_like(x)), axis=-1).reshape(-1, 3)
    depths = []
    for frame, k, ext in zip(job["frame_indices"], job["intrinsics"], job["extrinsics"]):
        camera = np.linalg.inv(ext)
        rays = pixels @ np.linalg.inv(k).T @ camera[:3, :3].T
        rays /= np.linalg.norm(rays, axis=1, keepdims=True)
        origin, world_to_camera = Vector(camera[:3, 3]), Matrix(ext)
        depth = np.zeros(h * w, dtype=np.float32)
        for index, ray in enumerate(rays):
            hit, _, _, _ = tree.ray_cast(origin, Vector(ray))
            if hit is not None:
                depth[index] = (world_to_camera @ hit).z
        depths.append(depth.reshape(h, w))
        print(f"DEPTH source_{frame}: {int((depth > 0).sum())}/{h*w} hits", flush=True)
    np.savez_compressed(Path(job["output_dir"]) / "model_depth.npz",
                        depth=np.asarray(depths), frame_indices=job["frame_indices"])


if __name__ == "__main__":
    inspect(json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text()))
