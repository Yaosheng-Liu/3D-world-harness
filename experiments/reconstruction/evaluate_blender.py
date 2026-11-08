"""Private evaluator: fixed-camera rendering, mesh export and persisted edits."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys

import bpy
import numpy as np
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from world_blender import configure_camera, check_projection
from oidn_blender import prepare_passes, denoise


def state():
    return {obj.name: {"matrix": [list(row) for row in obj.matrix_world],
                       "hide": obj.hide_render,
                       "materials": [list(m.diffuse_color) for m in obj.data.materials if m]
                       if obj.type == "MESH" else []}
            for obj in bpy.context.scene.objects if obj.type == "MESH" and obj.get("world_role") == "visual"}


def close(a, b):
    if type(a) is list:
        return len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
    if type(a) is dict:
        return a.keys() == b.keys() and all(close(a[k], b[k]) for k in a)
    if isinstance(a, (float, int)) and not isinstance(a, bool):
        return abs(a-b) <= 1e-5 * max(1, abs(a), abs(b))
    return a == b


def main():
    job = json.loads(Path(sys.argv[sys.argv.index("--")+1]).read_text())
    output = Path(job["output"])
    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.open_mainfile(filepath=job["blend"])
    scene = bpy.context.scene
    deps = bpy.context.evaluated_depsgraph_get()
    vertices, faces, owners = [], [], []
    identifiers=sorted(o['stable_id'] for o in scene.objects if o.get('stable_id'))
    for obj in scene.objects:
        if obj.type != "MESH" or obj.get("world_role") != "visual":
            continue
        evaluated = obj.evaluated_get(deps)
        mesh = evaluated.to_mesh()
        mesh.calc_loop_triangles()
        offset = len(vertices)
        vertices.extend(tuple(evaluated.matrix_world @ v.co) for v in mesh.vertices)
        faces.extend(tuple(offset+i for i in face.vertices) for face in mesh.loop_triangles)
        parent=obj.parent
        while parent and not parent.get('stable_id'):
            parent=parent.parent
        if parent is None:
            raise ValueError('Visual mesh has no stable object owner')
        owners.extend([identifiers.index(parent['stable_id'])]*len(mesh.loop_triangles))
        evaluated.to_mesh_clear()
    with (output / "geometry.ply").open("w") as f:
        f.write(f"ply\nformat ascii 1.0\nelement vertex {len(vertices)}\nproperty float x\nproperty float y\nproperty float z\nelement face {len(faces)}\nproperty list uchar int vertex_indices\nend_header\n")
        for v in vertices:
            f.write(" ".join(map(str, v))+"\n")
        for face in faces:
            f.write("3 "+" ".join(map(str, face))+"\n")
    np.savez_compressed(output/'geometry_owners.npz',owners=np.asarray(owners,dtype=np.int32),
                        identifiers=np.asarray(identifiers))
    scene.cycles.samples = job["samples"]
    scene.cycles.use_denoising = False
    scene.render.threads_mode, scene.render.threads = "FIXED", 8
    scene.render.image_settings.file_format = "PNG"
    camera = scene.camera
    projection_audit=[]
    for view in job["views"]:
        if 'camera_to_world' in view:
            camera.matrix_world=Matrix(view['camera_to_world'])
        else:
            camera.location=view['position']
            camera.rotation_euler=(Vector(view['target'])-camera.location).to_track_quat('-Z','Y').to_euler()
        configure_camera(scene, camera, view)
        if 'intrinsics' in view:
            projection_audit.append({'view_id':view['id'],**check_projection(scene,camera,view,
                                     probe_distance=max(1,camera.location.length))})
        passes=output/"denoise_passes"
        passes.mkdir(exist_ok=True)
        scene.use_nodes=True
        scene.node_tree.nodes.clear()
        prepare_passes(scene,passes)
        scene.render.filepath = str(passes / "raw.png")
        bpy.ops.render.render(write_still=True)
        denoise(scene,passes,job['oidn_library'])
        (output/'renders').mkdir(exist_ok=True)
        shutil.copyfile(passes/'preview.png',output/'renders'/(view['id']+'.png'))
        shutil.rmtree(passes)
    edits=[]
    targets=job.get("edit_targets",{})
    for label, identifier in targets.items():
        for operation in ("translate","rotate","uniform_scale","hide","material"):
            bpy.ops.wm.open_mainfile(filepath=job["blend"])
            roots={o.get("stable_id"):o for o in bpy.data.objects if o.get("stable_id")}
            target=roots.get(identifier)
            row={"target":label,"object_id":identifier,"operation":operation}
            if target is None:
                edits.append({**row,"status":"missing_target","passed":False});continue
            before=state()
            members={target,*target.children_recursive}
            if operation == "material":
                members={target,*target.children}
            owned={o.name for o in members if o.type=="MESH" and o.get("world_role")=="visual"}
            if not owned:
                edits.append({**row,"status":"no_geometry","passed":False});continue
            base=target.matrix_world.copy()
            points=[o.matrix_world @ Vector(corner) for o in members if o.type=="MESH" for corner in o.bound_box]
            lo=Vector([min(v[i] for v in points) for i in range(3)])
            hi=Vector([max(v[i] for v in points) for i in range(3)])
            extent=max((hi-lo).length, .001)
            if operation=="translate":
                target.matrix_world=Matrix.Translation((extent*.2,0,0)) @ base
            elif operation=="rotate":
                pivot=Matrix.Translation(base.translation)
                target.matrix_world=pivot @ Matrix.Rotation(math.radians(20),4,'Z') @ pivot.inverted() @ base
            elif operation=="uniform_scale":
                target.scale*=1.1
            elif operation=="hide":
                for obj in members:
                    obj.hide_render=True
            else:
                for obj in members:
                    if obj.type!='MESH' or obj.name not in owned:
                        continue
                    for i, mat in enumerate(obj.data.materials):
                        material=mat.copy()
                        material.diffuse_color=(.8,.15,.2,1)
                        if material.use_nodes:
                            for node in material.node_tree.nodes:
                                if node.type=='BSDF_PRINCIPLED':
                                    socket=node.inputs['Base Color']
                                    for link in list(socket.links):
                                        material.node_tree.links.remove(link)
                                    socket.default_value=(.8,.15,.2,1)
                        obj.data.materials[i]=material
            bpy.context.view_layer.update()
            changed=state()
            affected=[name for name in before if not close(before[name],changed[name])]
            unintended=[name for name in affected if name not in owned]
            temp=output/"edit_roundtrip.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(temp))
            bpy.ops.wm.open_mainfile(filepath=str(temp))
            persisted=close(changed,state())
            edits.append({**row,"status":"tested","passed":bool(affected) and not unintended and persisted,
                          "affected_meshes":len(affected),"unintended_meshes":unintended,"persisted":persisted})
            temp.unlink(missing_ok=True)
            temp.with_suffix('.blend1').unlink(missing_ok=True)
    result={"vertices":len(vertices),"triangles":len(faces),"edit_tasks":edits,
            "projection_audit":projection_audit,
            "edit_semantics":"Declared target mapping; semantic ownership requires independent visual verification.",
            "reopened":True}
    (output/"blender_metrics.json").write_text(json.dumps(result,indent=2)+"\n")


if __name__=="__main__":
    main()
