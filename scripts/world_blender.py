"""Blender worker for validated world jobs. Run with --python ... -- job.json."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector
from bpy_extras.object_utils import world_to_camera_view


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def material(name, spec, project=None):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    captured = spec.get("appearance", "surface") == "captured"
    shader = nodes.new("ShaderNodeEmission" if captured else "ShaderNodeBsdfPrincipled")
    shader.name = "World shader"
    color = shader.inputs["Color" if captured else "Base Color"]
    color.default_value = spec["base_color"]
    if not captured:
        shader.inputs["Roughness"].default_value = spec["roughness"]
        shader.inputs["Metallic"].default_value = spec["metallic"]
    if "base_color_texture" in spec:
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = bpy.data.images.load(str(project / spec["base_color_texture"]), check_existing=True)
        texture.image.colorspace_settings.name = "sRGB"
        multiply = nodes.new("ShaderNodeMix")
        multiply.name, multiply.data_type, multiply.blend_type = "World texture tint", "RGBA", "MULTIPLY"
        multiply.inputs[0].default_value = 1
        multiply.inputs[7].default_value = spec["base_color"]
        links.new(texture.outputs["Color"], multiply.inputs[6])
        links.new(multiply.outputs[2], color)
    if captured:
        # Blender's glTF exporter recognizes this camera-only emission as unlit.
        lightpath = nodes.new("ShaderNodeLightPath")
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        visibility = nodes.new("ShaderNodeMixShader")
        links.new(lightpath.outputs["Is Camera Ray"], visibility.inputs[0])
        links.new(transparent.outputs[0], visibility.inputs[1])
        links.new(shader.outputs[0], visibility.inputs[2])
        links.new(visibility.outputs[0], output.inputs["Surface"])
    else:
        links.new(shader.outputs[0], output.inputs["Surface"])
    result.diffuse_color = spec["base_color"]
    result["world_material_json"] = json.dumps(spec, sort_keys=True)
    result["world_appearance"] = spec.get("appearance", "surface")
    return result


def appearance_inventory(appearance):
    shader = appearance.node_tree.nodes["World shader"]
    images = []
    for node in appearance.node_tree.nodes:
        if node.type == "TEX_IMAGE":
            image = node.image
            data = bytes(image.packed_file.data) if image.packed_file else Path(bpy.path.abspath(image.filepath)).read_bytes()
            images.append({"name": image.name, "sha256": hashlib.sha256(data).hexdigest(),
                           "size": list(image.size), "color_space": image.colorspace_settings.name})
    spec = json.loads(appearance["world_material_json"])
    captured = spec.get("appearance", "surface") == "captured"
    color = shader.inputs["Color" if captured else "Base Color"].default_value
    if "base_color_texture" in spec:
        color = appearance.node_tree.nodes["World texture tint"].inputs[7].default_value
    return {"base_color": list(color), "roughness": spec["roughness"] if captured else shader.inputs["Roughness"].default_value,
            "metallic": spec["metallic"] if captured else shader.inputs["Metallic"].default_value,
            "appearance": spec.get("appearance", "surface"), "shader": shader.bl_idname, "images": images}


def move_to_collection(obj, collection):
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def create_shape(spec, name, collection, project):
    kind = spec["type"]
    if kind == "box":
        bpy.ops.mesh.primitive_cube_add(size=1)
        obj = bpy.context.object
        for vertex in obj.data.vertices:
            vertex.co = Vector([value * size for value, size in zip(vertex.co, spec["size"])])
    elif kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=spec["radius"])
        obj = bpy.context.object
    elif kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=spec["radius"], depth=spec["depth"])
        obj = bpy.context.object
    elif kind == "mesh":
        bpy.ops.wm.obj_import(filepath=str(project / spec["path"]), forward_axis="Y", up_axis="Z")
        imported = list(bpy.context.selected_objects)
        if not imported or any(obj.type != "MESH" for obj in imported):
            raise ValueError(f"OBJ must contain mesh geometry: {spec['path']}")
        bpy.context.view_layer.objects.active = imported[0]
        if len(imported) > 1:
            bpy.ops.object.join()
        obj = bpy.context.object
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        if not obj.data.vertices or not obj.data.polygons:
            raise ValueError(f"OBJ must contain polygon geometry: {spec['path']}")
        if obj.data.validate():
            raise ValueError(f"OBJ contains invalid topology; repair the source mesh before building: {spec['path']}")
        for vertex in obj.data.vertices:
            vertex.co = Vector([value * scale for value, scale in zip(vertex.co, spec["scale"])])
    else:
        raise ValueError(f"Unknown geometry type: {kind}")
    obj.name = name
    obj.data.name = name + "/mesh"
    obj.location = spec.get("position", [0, 0, 0])
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = spec.get("rotation", [0, 0, 0])
    move_to_collection(obj, collection)
    if kind == "sphere":
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
    elif kind == "cylinder":
        for polygon in obj.data.polygons:
            polygon.use_smooth = len(polygon.vertices) == 4
    return obj


def geometry_hash(mesh):
    # Blender may reorder primitive vertex indices between processes.
    vertices = [tuple(vertex.co) for vertex in mesh.vertices]
    faces = []
    for polygon in mesh.polygons:
        points = [vertices[index] for index in polygon.vertices]
        first = min(range(len(points)), key=points.__getitem__)
        faces.append(points[first:] + points[:first])
    geometry = {"vertices": sorted(vertices), "faces": sorted(faces)}
    return hashlib.sha256(json.dumps(geometry, separators=(",", ":")).encode()).hexdigest()


def uv_hash(mesh):
    # Like geometry_hash, describe face corners rather than unstable loop indices.
    layers = []
    for layer in mesh.uv_layers:
        faces = []
        for polygon in mesh.polygons:
            points = [(tuple(mesh.vertices[mesh.loops[index].vertex_index].co), tuple(layer.data[index].uv))
                      for index in polygon.loop_indices]
            first = min(range(len(points)), key=points.__getitem__)
            faces.append(points[first:] + points[:first])
        layers.append({"name": layer.name, "faces": sorted(faces)})
    return hashlib.sha256(json.dumps(layers, separators=(",", ":")).encode()).hexdigest()


def inventory(roots):
    result = {}
    for object_id, root in roots.items():
        meshes = [obj for obj in root.children if obj.type == "MESH"]
        points = [obj.matrix_world @ Vector(corner) for obj in meshes
                  if obj["world_role"] == "visual" for corner in obj.bound_box]
        appearance = next(obj.data.materials[0] for obj in meshes if obj["world_role"] == "visual")
        result[object_id] = {
            "parent_id": root.parent.get("stable_id") if root.parent else None,
            "matrix_world": [list(row) for row in root.matrix_world],
            "bounds_world": [[min(p[axis] for p in points) for axis in range(3)],
                             [max(p[axis] for p in points) for axis in range(3)]],
            "material": appearance_inventory(appearance),
            "meshes": {obj.name: {"role": obj["world_role"], "vertices": len(obj.data.vertices),
                                  "polygons": len(obj.data.polygons), "geometry_sha256": geometry_hash(obj.data),
                                  "uv_sha256": uv_hash(obj.data)}
                       for obj in meshes},
        }
    return result


def same_inventory(before, after):
    if before.keys() != after.keys():
        return False
    for object_id, expected in before.items():
        for field, value in expected.items():
            actual = after[object_id][field]
            if field in ("matrix_world", "bounds_world"):
                if any(abs(a-b) > 1e-6 for row_a, row_b in zip(value, actual) for a, b in zip(row_a, row_b)):
                    return False
            elif value != actual:
                return False
    return True


def animate(roots, trajectory, scene):
    scene.render.fps = 60
    scene.frame_end = math.ceil(1 + trajectory["timestamps"][-1] * 60)
    previous = {}
    for index, time in enumerate(trajectory["timestamps"]):
        matrices = {object_id: Matrix.LocRotScale(Vector(frames[index]["position"]),
                                                 Quaternion(frames[index]["quaternion"]), Vector((1, 1, 1)))
                    for object_id, frames in trajectory["poses"].items()}
        for object_id, root in roots.items():
            transform = matrices[object_id]
            if root.parent:
                transform = matrices[root.parent["stable_id"]].inverted() @ transform
            position, rotation, _ = transform.decompose()
            if object_id in previous and rotation.dot(previous[object_id]) < 0:
                rotation.negate()
            previous[object_id] = rotation.copy()
            root.location, root.rotation_mode, root.rotation_quaternion = position, "QUATERNION", rotation
            root.keyframe_insert("location", frame=1 + time * 60, group="Physics playback")
            root.keyframe_insert("rotation_quaternion", frame=1 + time * 60, group="Physics playback")
    # Matching NLA track names merge the per-object actions into one glTF clip.
    for root in roots.values():
        action = root.animation_data.action
        for curve in action.fcurves:
            for keyframe in curve.keyframe_points:
                keyframe.interpolation = "LINEAR"
        track = root.animation_data.nla_tracks.new()
        track.name = "Physics playback"
        track.strips.new(action.name, 1, action)
        root.animation_data.action = None
    scene.frame_set(1)


def verify_animation(roots, trajectory, scene):
    sampled = sorted({0, len(trajectory["timestamps"]) // 2, len(trajectory["timestamps"]) - 1})
    error = 0.0
    for index in sampled:
        frame = 1 + trajectory["timestamps"][index] * 60
        scene.frame_set(math.floor(frame), subframe=frame % 1)
        bpy.context.view_layer.update()
        for object_id, root in roots.items():
            pose = trajectory["poses"][object_id][index]
            expected = Matrix.LocRotScale(Vector(pose["position"]), Quaternion(pose["quaternion"]), Vector((1, 1, 1)))
            error = max(error, max(abs(root.matrix_world[i][j] - expected[i][j]) for i in range(4) for j in range(4)))
    scene.frame_set(1)
    if error > 1e-4:
        raise ValueError(f"Reopened animation differs from simulation world poses: {error}")
    return {"sampled_frames": sampled, "max_world_matrix_error": error}


def configure_camera(scene, camera, view):
    if "intrinsics" not in view:
        scene.render.resolution_x, scene.render.resolution_y = 640, 480
        scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
        camera.data.sensor_fit = "VERTICAL"
        camera.data.angle = math.radians(view["fov_y_deg"])
        camera.data.shift_x = camera.data.shift_y = 0
        return
    k = view["intrinsics"]
    scene.render.resolution_x, scene.render.resolution_y = k["width"], k["height"]
    aspect = k["fx"] / k["fy"]
    # Blender clamps each pixel-aspect component to >=1; preserve their ratio.
    scene.render.pixel_aspect_x, scene.render.pixel_aspect_y = max(1, 1/aspect), max(1, aspect)
    camera.data.sensor_fit, camera.data.sensor_width = "HORIZONTAL", 36
    camera.data.lens = k["fx"] * 36 / k["width"]
    camera.data.shift_x = (k["width"] / 2 - k["cx"]) / k["width"]
    camera.data.shift_y = (k["cy"] - k["height"] / 2) * aspect / k["width"]


def check_projection(scene, camera, view):
    k, error = view["intrinsics"], 0
    bpy.context.view_layer.update()
    for u, v in ((0, 0), (k["width"], 0), (0, k["height"]), (k["width"], k["height"]), (k["cx"], k["cy"])):
        point = Matrix(view["camera_to_world"]) @ Vector(((u-k["cx"])/k["fx"], -(v-k["cy"])/k["fy"], -1))
        projected = world_to_camera_view(scene, camera, point)
        error = max(error, abs(projected.x*k["width"]-u), abs((1-projected.y)*k["height"]-v))
    if error > 0.002:
        raise ValueError(f"Calibrated camera projection differs by {error} pixels: {view['id']}")
    return {"max_projection_error_px": error, "width": k["width"], "height": k["height"]}


def build(job):
    source, project, output = job["scene"], Path(job["project_dir"]), Path(job["output_dir"])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC" if source["units"] == "m" else "NONE"
    scene.unit_settings.scale_length = 1
    visual = bpy.data.collections.new("World visual")
    collision = bpy.data.collections.new("World collision (inspection only)")
    scene.collection.children.link(visual)
    scene.collection.children.link(collision)
    collision.hide_render = True
    roots, export_objects = {}, []
    collider_material = material("Collision inspection", {"base_color": [1, 0.25, 0.03, 1], "roughness": 0.8, "metallic": 0})
    for spec in source["objects"]:
        root = bpy.data.objects.new(spec["id"], None)
        visual.objects.link(root)
        root["stable_id"], root["world_role"] = spec["id"], "object"
        root["world_object_json"] = json.dumps(spec, ensure_ascii=False)
        root.location, root.rotation_euler = spec["position"], spec["rotation"]
        root.rotation_mode = "XYZ"
        roots[spec["id"]] = root
        export_objects.append(root)
        appearance = material(spec["id"] + "/material", spec["material"], project)
        for role, collection in (("visual", visual), ("collision", collision)):
            for index, shape in enumerate(spec[role]):
                obj = create_shape(shape, f"{spec['id']}/{role}/{index}", collection, project)
                obj.parent = root
                obj["object_id"], obj["world_role"] = spec["id"], role
                if role == "visual" and "base_color_texture" in spec["material"] and not obj.data.uv_layers:
                    raise ValueError(f"Textured geometry requires UV coordinates: {obj.name}")
                obj.data.materials.clear()
                obj.data.materials.append(appearance if role == "visual" else collider_material)
                if role == "visual":
                    export_objects.append(obj)
                else:
                    obj.display_type = "WIRE"
                    obj.hide_set(True)
    for spec in source["objects"]:
        if spec["parent_id"]:
            roots[spec["id"]].parent = roots[spec["parent_id"]]
    if job["trajectory"] is not None:
        animate(roots, job["trajectory"], scene)
    scene.frame_set(1)
    bpy.context.view_layer.update()
    before = inventory(roots)
    scene["world_scene_json"] = json.dumps(source, ensure_ascii=False)
    scene["world_scene_hash"] = job["scene_hash"]
    scene.world = bpy.data.worlds.new("World lighting")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.18, 0.20, 0.24, 1)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.4
    for name, location, energy, size in (("Key", (0, -3, 5), 800, 4), ("Fill", (-3, 1, 3), 450, 3)):
        light = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
        scene.collection.objects.link(light)
        light.location, light.data.energy, light.data.shape, light.data.size = location, energy, "DISK", size
        light.rotation_euler = (-Vector(location)).to_track_quat("-Z", "Y").to_euler()
    cameras = {}
    for view in source["views"]:
        camera = bpy.data.objects.new("Camera/" + view["id"], bpy.data.cameras.new(view["id"]))
        scene.collection.objects.link(camera)
        if "camera_to_world" in view:
            camera.matrix_world = Matrix(view["camera_to_world"])
        else:
            camera.location = view["position"]
            camera.rotation_euler = (Vector(view["target"]) - camera.location).to_track_quat("-Z", "Y").to_euler()
        camera.data.clip_start = 0.01
        camera["world_view_id"] = view["id"]
        cameras[view["id"]] = camera
    scene.camera = next(iter(cameras.values()))
    scene.render.engine = "CYCLES"
    captured = [obj["material"].get("appearance") == "captured" for obj in source["objects"]]
    scene.cycles.device, scene.cycles.samples = "CPU", 1 if all(captured) else 24
    scene.cycles.use_denoising = False
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 640, 480, 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = True
    scene.view_settings.view_transform = "Standard" if any(captured) else "AgX"
    scene.view_settings.look = "None"
    scene.view_settings.exposure, scene.view_settings.gamma = 0, 1
    for view in source["views"]:
        configure_camera(scene, cameras[view["id"]], view)
    configure_camera(scene, scene.camera, source["views"][0])
    bpy.ops.object.select_all(action="DESELECT")
    for obj in export_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = export_objects[0]
    bpy.ops.export_scene.gltf(filepath=str(output / "scene.glb"), export_format="GLB", use_selection=True,
                              export_extras=True, export_yup=True, export_normals=not all(captured),
                              export_cameras=False, export_lights=False,
                              export_animations=job["trajectory"] is not None, export_force_sampling=False,
                              export_animation_mode="NLA_TRACKS", export_anim_slide_to_zero=True)
    # Discard replaced OBJ/MTL materials so only explicit world assets are packed.
    bpy.ops.outliner.orphans_purge(do_recursive=True)
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(output / "scene.blend"), compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(output / "scene.blend"))
    scene = bpy.context.scene
    scene.frame_set(1)
    bpy.context.view_layer.update()
    reopened = {obj["stable_id"]: obj for obj in scene.objects if "stable_id" in obj}
    after = inventory(reopened)
    if not same_inventory(before, after) or scene["world_scene_hash"] != job["scene_hash"]:
        write_json(output / "reopen_mismatch.json", {"before": before, "after": after})
        raise ValueError("Saved Blender scene changed after reopening; see reopen_mismatch.json")
    animation_check = verify_animation(reopened, job["trajectory"], scene) if job["trajectory"] is not None else None
    cameras = {obj["world_view_id"]: obj for obj in scene.objects if "world_view_id" in obj}
    camera_checks = {}
    for view in source["views"]:
        configure_camera(scene, cameras[view["id"]], view)
        if "camera_to_world" in view:
            camera_checks[view["id"]] = check_projection(scene, cameras[view["id"]], view)
    render_paths = []
    if job["render"]:
        (output / "renders").mkdir()
        for view in source["views"]:
            scene.camera = cameras[view["id"]]
            configure_camera(scene, scene.camera, view)
            scene.render.filepath = str(output / "renders" / (view["id"] + ".png"))
            bpy.ops.render.render(write_still=True)
            render_paths.append(scene.render.filepath)
    write_json(output / "geometry.json", {"scene_hash": job["scene_hash"], "revision": source["revision"],
                                          "blender_version": bpy.app.version_string, "reopened_blend": True,
                                          "units": source["units"], "up_axis": "Z", "glb_up_axis": "Y",
                                          "camera_checks": camera_checks,
                                          "appearance": "captured" if all(captured) else "mixed" if any(captured) else "surface",
                                          "objects": before, "render_paths": render_paths,
                                          "animation_check": animation_check,
                                          "source": source["provenance"]})


if __name__ == "__main__":
    job_path = Path(sys.argv[sys.argv.index("--") + 1])
    build(json.loads(job_path.read_text()))
