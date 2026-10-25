#!/usr/bin/env python3
"""Actual Blender check: every object gets independently hidden and restored."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from reconstruction_review import make_object_inspection_sheets
from world_build import build_world, file_hash
from world_model import WorldStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    project = options.output or Path(tempfile.mkdtemp(prefix="object-edit-blender-"))
    project.mkdir(parents=True, exist_ok=True)
    scene = json.loads((ROOT / "examples/physical_world/scene.json").read_text())
    prototype = next(obj for obj in scene["objects"] if obj["id"] == "block")
    scene["objects"] = []
    for key, x, color in (("red", -.45, [1, .02, .02, 1]), ("blue", .45, [.02, .02, 1, 1])):
        obj = deepcopy(prototype)
        obj.update(id=key, name=key, position=[x, 0, .2], parent_id=None,
                   visual=[{"type": "box", "size": [.5, .5, .5]}])
        obj["material"].update(base_color=color, appearance="captured")
        scene["objects"].append(obj)
    scene["views"] = [{"id": "far", "position": [0, -12, 4], "target": [0, 0, .2], "fov_y_deg": 50},
                      {"id": "near", "position": [0, -3, 1.2], "target": [0, 0, .2], "fov_y_deg": 50}]
    WorldStore(project).initialize(scene)
    receipt = build_world(project, blender=options.blender, render=False)
    original_hash = file_hash(Path(receipt["blend_path"]))
    output = project / "edit_checks"
    output.mkdir()
    job = {"scene": scene, "blend_path": receipt["blend_path"], "output_dir": str(output),
           "inspection_object_ids": ["red", "blue"]}
    (output / "job.json").write_text(json.dumps(job))
    with (output / "worker.log").open("w") as log:
        subprocess.run([options.blender, "--background", "--factory-startup", "--python-exit-code", "1",
                        "--python", str(ROOT / "scripts/review_editability_blender.py"), "--", str(output / "job.json")],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
    report = json.loads((output / "editability.json").read_text())
    assert report["passed"] and report["visual_checks_complete"]
    assert len(report["object_inspections"]) == 2
    for row in report["object_inspections"]:
        assert row["status"] == "rendered" and row["view_id"] == "near", row
        with Image.open(row["before_path"]) as before, Image.open(row["hidden_path"]) as hidden:
            before, hidden = before.convert("RGBA"), hidden.convert("RGBA")
            assert max(before.size) <= 480 and before.size == hidden.size
            assert ImageChops.difference(before, hidden).getbbox() is not None
            pixels = list(hidden.getdata())
            red = sum(r > b * 2 and r > 80 and a > 0 for r, g, b, a in pixels)
            blue = sum(b > r * 2 and b > 80 and a > 0 for r, g, b, a in pixels)
            assert (red == 0 and blue > 100) if row["object_id"] == "red" else (blue == 0 and red > 100), row
    first, second = report["object_inspections"]
    assert Path(first["before_path"]).read_bytes() == Path(second["before_path"]).read_bytes(), "hidden state leaked to next object"
    assert file_hash(Path(receipt["blend_path"])) == original_hash, "review changed the editable source"
    make_object_inspection_sheets(project, output, report)
    (output / "editability.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"project": str(project), "passed": True, "object_ids": [row["object_id"] for row in report["object_inspections"]],
                      "inspection_paths": report["inspection_paths"]}, indent=2))


if __name__ == "__main__":
    main()
