"""Paired appearance/gray renders of frozen geometry under diagnostic lighting."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

import bpy
import numpy as np
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from world_blender import check_projection, configure_camera
from oidn_blender import denoise, prepare_passes


def geometry_hash(scene):
    result = hashlib.sha256()
    for obj in sorted((o for o in scene.objects if o.type == "MESH"), key=lambda o: o.name):
        result.update(obj.name.encode())
        result.update(np.asarray(obj.matrix_world, dtype=np.float64).tobytes())
        for collection, field, size, dtype in ((obj.data.vertices, "co", 3, np.float32),
                                               (obj.data.loops, "vertex_index", 1, np.int32),
                                               (obj.data.polygons, "loop_total", 1, np.int32)):
            values = np.empty(len(collection) * size, dtype=dtype)
            collection.foreach_get(field, values)
            result.update(values.tobytes())
    return result.hexdigest()


def main():
    job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    output = Path(job["output"])
    bpy.ops.wm.open_mainfile(filepath=job["blend"])
    scene = bpy.context.scene
    before = geometry_hash(scene)
    materials = []
    for material in bpy.data.materials:
        if "world_material_json" not in material:
            continue
        nodes, links = material.node_tree.nodes, material.node_tree.links
        old = nodes["World shader"]
        color = old.inputs["Color" if old.type == "EMISSION" else "Base Color"]
        source = color.links[0].from_socket if color.is_linked else None
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.inputs["Roughness"].default_value = .8
        shader.inputs["Metallic"].default_value = 0
        target = next(n for n in nodes if n.type == "OUTPUT_MATERIAL")
        links.new(shader.outputs[0], target.inputs["Surface"])
        materials.append((material, shader.inputs["Base Color"], source, tuple(color.default_value)))
    if not materials:
        raise ValueError("No explicit world materials found")
    for obj in list(scene.objects):
        if obj.type == "LIGHT":
            bpy.data.objects.remove(obj, do_unlink=True)
    background = scene.world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (.5, .5, .5, 1)
    background.inputs["Strength"].default_value = .1
    light = bpy.data.objects.new("Diagnostic camera light", bpy.data.lights.new("Diagnostic", "AREA"))
    scene.collection.objects.link(light)
    light.data.energy, light.data.size = 70, 2
    scene.render.engine = "CYCLES"
    scene.cycles.device, scene.cycles.samples, scene.cycles.seed = "CPU", job["samples"], 0
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.max_bounces = 8
    scene.cycles.diffuse_bounces = scene.cycles.glossy_bounces = 4
    scene.cycles.transmission_bounces, scene.cycles.transparent_max_bounces = 4, 8
    scene.render.threads_mode, scene.render.threads = "FIXED", 8
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform, scene.view_settings.look = "Standard", "None"
    scene.view_settings.exposure, scene.view_settings.gamma = 0, 1
    audit = []
    for variant in ("textured", "clay"):
        for material, color, source, default in materials:
            for link in list(color.links):
                material.node_tree.links.remove(link)
            color.default_value = default if variant == "textured" else (.5, .5, .5, 1)
            if variant == "textured" and source is not None:
                material.node_tree.links.new(source, color)
        destination = output / variant / "renders"
        destination.mkdir(parents=True, exist_ok=True)
        for view in job["views"]:
            scene.camera.matrix_world = Matrix(view["camera_to_world"])
            configure_camera(scene, scene.camera, view)
            light.matrix_world = scene.camera.matrix_world.copy()
            audit.append({"variant": variant, "view": view["id"],
                          **check_projection(scene, scene.camera, view)})
            passes = output / "passes"
            passes.mkdir(exist_ok=True)
            scene.use_nodes = True
            scene.node_tree.nodes.clear()
            prepare_passes(scene, passes)
            scene.render.filepath = str(passes / "raw.png")
            bpy.ops.render.render(write_still=True)
            denoise(scene, passes, job["oidn_library"])
            shutil.copyfile(passes / "preview.png", destination / (view["id"] + ".png"))
            shutil.rmtree(passes)
        if geometry_hash(scene) != before:
            raise ValueError("Appearance ablation changed geometry")
    (output / "render_audit.json").write_text(json.dumps({
        "geometry_sha256": before, "geometry_unchanged": True,
        "projection_checks": audit, "materials": len(materials),
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
