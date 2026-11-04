"""Real Blender check: tilted visual plane, off-center cameras, ignored collider."""
import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from test_world_depth import reference_fixture
from reconstruction_review import freeze_candidate, verify_files, write_json
from world_build import build_world, file_hash
from world_depth import write_depth_inspection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blender", default="blender")
    args = parser.parse_args()
    project = args.output.resolve()
    project.mkdir(parents=True, exist_ok=False)
    reference_fixture(project)
    cache = project / "dense/depths.npz"
    with np.load(cache) as archive:
        data = {key: archive[key] for key in archive.files}
    c, s = math.cos(.35), math.sin(.35)
    camera = np.array([[c, -s, 0, .25], [s, c, 0, .1], [0, 0, 1, 3], [0, 0, 0, 1]])
    data["extrinsics"][1] = np.linalg.inv(camera @ np.diag([1, -1, -1, 1]))
    np.savez_compressed(cache, **data)
    inference_path = project / "dense/inference.json"
    inference = json.loads(inference_path.read_text()); inference["depths_sha256"] = file_hash(cache)
    write_json(inference_path, inference)
    (project / "plane.obj").write_text("v -20 -20 0\nv 20 -20 0\nv 20 20 0\nv -20 20 0\nf 1 2 3 4\n")
    scene = json.loads((ROOT / "examples/physical_world/scene.json").read_text())
    scene["units"] = "arbitrary"
    obj = scene["objects"][0]
    obj.update(id="plane", name="Tilted plane", parent_id=None, position=[0, 0, .4], rotation=[0, .25, 0],
               visual=[{"type": "mesh", "path": "plane.obj", "scale": [1, 1, 1]}],
               collision=[{"type": "box", "size": [40, 40, 2]}])
    scene["objects"] = [obj]
    scene["provenance"]["sources"] = [{"path": name, "description": "Synthetic fixed depth evidence"} for name in
                                       ("dense/inference.json", "observations/manifest.json", "reconstruction_report.json")]
    scene["views"] = [{"id": "source_1", "camera_to_world": np.diag([1., 1., 1., 1.]).tolist(),
                       "intrinsics": {"width": 8, "height": 6, "fx": 5, "fy": 6, "cx": 3, "cy": 2},
                       "reference_image": "observations/train/000001.png"}]
    scene["views"][0]["camera_to_world"][2][3] = 3
    scene["views"].append({"id": "diagnostic", "position": [2, -3, 4], "target": [0, 0, 0], "fov_y_deg": 50})
    receipt = build_world(project, blender=args.blender, scene=scene, render=True)
    before = file_hash(Path(receipt["blend_path"]))
    result = write_depth_inspection(project, scene, receipt, project / "depth_check", blender=args.blender)
    assert file_hash(Path(receipt["blend_path"])) == before
    normal = np.array([math.sin(.25), 0, math.cos(.25)])
    y, x = np.mgrid[:6, :8]
    pixels = np.stack((x, y, np.ones_like(x)), axis=-1).reshape(-1, 3)
    expected = []
    for k, e in zip(data["intrinsics"], data["extrinsics"]):
        cv_camera = np.linalg.inv(e)
        directions = pixels @ np.linalg.inv(k).T @ cv_camera[:3, :3].T
        expected.append(((np.array([0, 0, .4]) - cv_camera[:3, 3]) @ normal) / (directions @ normal))
    with np.load(project / "depth_check/model_depth.npz") as archive:
        error = float(np.max(abs(archive["depth"].reshape(2, -1) - expected)))
    assert error < 1e-4, error
    assert all(row["compared_pixels"] == 48 for row in result["views"])
    write_json(project / "candidate.json", scene)
    write_json(project / "receipt.json", receipt)
    write_json(project / "geometry_rebuild_notes.json", {"issues": [{"issue_id": "plane", "reason": "synthetic fixture"}]})
    manifest = freeze_candidate(project, "candidate.json", "receipt.json", project / "frozen", blender=args.blender)
    assert manifest["repair_evidence_paths"] == ["frozen/geometry_rebuild_notes.json"]
    assert file_hash(project / "geometry_rebuild_notes.json") == manifest["files"]["frozen/geometry_rebuild_notes.json"]
    assert "frozen/depth/model_depth.npz" in manifest["files"]
    assert manifest["source_view_ids"] == ["source_1", "source_7"]
    assert manifest["diagnostic_view_ids"] == ["diagnostic"]
    assert manifest["diagnostic_render_paths"] == [receipt["render_paths"][1]]
    verify_files(project, manifest["files"])
    print(json.dumps({"max_camera_z_error": error, "views": 2, "blend_unchanged": True,
                      "collider_excluded": True, "frozen_depth_bound": True}))


if __name__ == "__main__":
    main()
