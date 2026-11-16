#!/usr/bin/env python3
"""Real Blender acceptance for calibrated views and captured UV appearance."""
import argparse
import copy
import json
import math
from pathlib import Path
import struct
import sys
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import world_build
from world_build import build_world
from world_model import WorldError, WorldStore
from world_service import WorldService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blender", default="blender")
    args = parser.parse_args()
    project = args.output.resolve()
    project.mkdir(parents=True, exist_ok=False)
    texture = Image.new("RGB", (16, 16))
    texture.putdata([(220, 60, 30) if (x//4+y//4)%2 else (30, 170, 230) for y in range(16) for x in range(16)])
    texture.save(project / "texture.png")
    (project / "quad.obj").write_text("v -1 -1 0\nv 1 -1 0\nv 1 1 0\nv -1 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3 4/4\n")
    (project / "source.json").write_text('{"source":"synthetic calibrated acceptance fixture"}\n')
    source = json.loads((Path(__file__).resolve().parents[1] / "examples/physical_world/scene.json").read_text())
    world = copy.deepcopy(source)
    world["units"] = "arbitrary"
    obj = copy.deepcopy(source["objects"][0])
    obj.update(id="panel", name="Captured panel", position=[0, 0, 0],
               visual=[{"type":"mesh", "path":"quad.obj", "scale":[1,1,1]}],
               collision=[{"type":"box", "size":[2,2,0.01]}],
               material={"base_color":[0.5,1,0.75,1], "roughness":0.5, "metallic":0,
                         "base_color_texture":"texture.png", "appearance":"captured"})
    world["objects"] = [obj]
    world["provenance"]["sources"] = [{"path":"source.json", "description":"fixture provenance"}]
    world["views"] = []
    for index, (width, height, fx, fy, cx, cy, roll) in enumerate(((64,48,55,45,24,19,.3), (80,60,70,85,48,22,-.4))):
        c, s = math.cos(roll), math.sin(roll)
        view = {"id":f"view_{index}", "camera_to_world":[[c,-s,0,index*.2],[s,c,0,0],[0,0,1,3],[0,0,0,1]],
                "intrinsics":{"width":width,"height":height,"fx":fx,"fy":fy,"cx":cx,"cy":cy},
                "reference_image":f"reference_{index}.png"}
        world["views"].append(view)
        Image.new("RGB", (width,height), "white").save(project / view["reference_image"])
    WorldStore(project).initialize(world)
    result = build_world(project, blender=args.blender, render=True)
    expected = {"quad.obj", "texture.png", "source.json", "reference_0.png", "reference_1.png"}
    assert set(result["asset_hashes"]) == expected, result["asset_hashes"]
    report = json.loads(Path(result["report_path"]).read_text())
    assert report["reopened_blend"] and report["units"] == "arbitrary"
    assert report["appearance"] == "captured"
    for view, path in zip(world["views"], result["render_paths"]):
        with Image.open(path) as image:
            assert image.size == (view["intrinsics"]["width"],view["intrinsics"]["height"])
            assert image.mode == "RGBA"
            assert image.getchannel("A").getextrema() == (0,255)
    data = Path(result["glb_path"]).read_bytes()
    document = json.loads(data[20:20+struct.unpack_from("<I",data,12)[0]])
    assert all("NORMAL" not in primitive["attributes"] for mesh in document["meshes"] for primitive in mesh["primitives"])
    material = document["materials"][0]
    assert "KHR_materials_unlit" in material["extensions"], material
    assert material["pbrMetallicRoughness"]["baseColorFactor"] == [0.5,1,0.75,1], material
    assert result["preview"]["embedded_image_count"] == 1
    surface = copy.deepcopy(world)
    surface["objects"][0]["material"].pop("appearance")
    surface_result = build_world(project, scene=surface, blender=args.blender, render=False)
    assert surface_result["preview"]["embedded_image_count"] == 1
    surface_data = Path(surface_result["glb_path"]).read_bytes()
    surface_document = json.loads(surface_data[20:20+struct.unpack_from("<I", surface_data, 12)[0]])
    assert all("NORMAL" in primitive["attributes"] for mesh in surface_document["meshes"] for primitive in mesh["primitives"])
    service = WorldService(project)
    record = service._save_record("receipts", copy.deepcopy(result))
    for relative in sorted(expected):
        path = project / relative
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"changed")
            try:
                service.evaluate(1, record["id"])
            except ValueError as error:
                assert "source asset changed" in str(error), str(error)
            else:
                raise AssertionError(f"Old build accepted changed source: {relative}")
        finally:
            path.write_bytes(original)
    # Blender cleans duplicate faces; usable textured geometry still needs UVs.
    (project / "duplicate.obj").write_text((project/"quad.obj").read_text()+"f 4/4 3/3 2/2 1/1\n")
    duplicate = copy.deepcopy(world)
    duplicate["objects"][0]["visual"][0]["path"] = "duplicate.obj"
    cleaned = build_world(project, scene=duplicate, blender=args.blender, render=False)
    cleaned_report = json.loads(Path(cleaned["report_path"]).read_text())
    assert cleaned_report["reopened_blend"]
    assert cleaned_report["objects"] == report["objects"]
    # A pre-save inventory hash discrepancy is diagnostic; scene identity is required.
    worker_source = world_build.WORKER.read_text()
    anchor = '    after = inventory(reopened)\n'
    assert worker_source.count(anchor) == 1
    diagnostic_worker = project / 'reopen_diagnostic_worker.py'
    diagnostic_worker.write_text(worker_source.replace(anchor, anchor +
        '    before["panel"]["meshes"]["panel/visual/0"]["geometry_sha256"] = "pre-save-diagnostic"\n'))
    with patch.object(world_build, 'WORKER', diagnostic_worker):
        diagnostic = build_world(project, scene=world, blender=args.blender, render=False)
    diagnostic_report = json.loads(Path(diagnostic['report_path']).read_text())
    assert diagnostic_report['reopened_blend'] and not diagnostic_report['reopen_inventory_matches']
    assert diagnostic_report['objects'] == report['objects']
    assert (Path(diagnostic['build_dir']) / 'reopen_mismatch.json').exists()
    wrong_scene_worker = project / 'wrong_scene_worker.py'
    wrong_scene_worker.write_text(worker_source.replace(anchor, anchor +
        '    scene["world_scene_hash"] = "another-submission"\n'))
    with patch.object(world_build, 'WORKER', wrong_scene_worker):
        try:
            build_world(project, scene=world, blender=args.blender, render=False)
        except WorldError:
            pass
        else:
            raise AssertionError('Reopened scene with a different submission identity was accepted')
    for name, contents, expected_error in (
        ("no_uv.obj", "v -1 -1 0\nv 1 -1 0\nv 1 1 0\nf 1 2 3\n", "requires UV coordinates"),
        ("empty.obj", "v 0 0 0\n", "polygon geometry"),
    ):
        (project / name).write_text(contents)
        invalid = copy.deepcopy(world)
        invalid["objects"][0]["visual"][0]["path"] = name
        try:
            build_world(project, scene=invalid, blender=args.blender, render=False)
        except WorldError as error:
            failed = sorted((project/"world/builds").glob("*/blender.log"),key=lambda path:path.stat().st_mtime)[-1]
            assert expected_error in failed.read_text(), str(error)
        else:
            raise AssertionError(f"Invalid textured OBJ was accepted: {name}")
    (project / "acceptance.json").write_text(json.dumps({"build":result, "camera_checks":report["camera_checks"],
          "passed":True, "checks":["OpenGL roll", "off-center principal point", "fx differs from fy",
          "per-view dimensions", "transparent missing surface", "UV and images survive Blender reopening",
          "embedded unlit GLB texture and tint", "captured omits normals; surface preserves normals", "default surface texture export", "duplicate faces cleaned without changing geometry", "reopen inventory differences recorded without blocking", "reopened submission identity required", "missing UV and empty meshes rejected", "all explicit source hashes", "changed mesh, texture, references and provenance invalidate build"]},indent=2)+"\n")
    print(json.dumps({"passed":True,"acceptance":str(project/"acceptance.json"),"camera_checks":report["camera_checks"]}))


if __name__ == "__main__":
    main()
