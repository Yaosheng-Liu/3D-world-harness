"""Triangulate explicitly matched image landmarks using unchanged build cameras."""
from pathlib import Path

import numpy as np

from reconstruction_review import project_file, read_json, verify_files
from world_model import scene_hash


def fit_landmarks(views, landmarks):
    cameras = {view["id"]: view for view in views if "reference_image" in view}
    results = []
    ids = [point["id"] for point in landmarks]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("landmarks must have distinct IDs")
    for point in landmarks:
        observations = point["observations"]
        names = [row["view_id"] for row in observations]
        if len(names) < 2 or len(names) != len(set(names)):
            raise ValueError("each landmark needs at least two distinct reference views")
        equations, projections, centers = [], [], []
        for row in observations:
            if row["view_id"] not in cameras:
                raise ValueError("landmark view must have a calibrated reference image")
            view = cameras[row["view_id"]]
            intr = view["intrinsics"]
            pixel = np.asarray(row["pixel"], dtype=float)
            if (pixel.shape != (2,) or not np.isfinite(pixel).all()
                    or not (0 <= pixel[0] < intr["width"] and 0 <= pixel[1] < intr["height"])):
                raise ValueError("landmark pixels must be inside the actual reference image")
            pose = np.asarray(view["camera_to_world"], dtype=float)
            # Scene cameras use Blender/OpenGL: right, up, back. Pixels use right, down.
            extrinsic = np.diag([1., -1., -1.]) @ np.linalg.inv(pose)[:3]
            k = np.array([[intr["fx"], 0, intr["cx"]], [0, intr["fy"], intr["cy"]], [0, 0, 1.]])
            x, y = np.linalg.solve(k, [*pixel, 1.])[:2]
            equations.extend([x * extrinsic[2] - extrinsic[0], y * extrinsic[2] - extrinsic[1]])
            projections.append(k @ extrinsic)
            centers.append(pose[:3, 3])
        _, singular, vectors = np.linalg.svd(equations)
        if singular[-2] < singular[0] * 1e-8 or abs(vectors[-1, 3]) < 1e-10:
            raise ValueError("landmark views have insufficient triangulation baseline")
        position = vectors[-1] / vectors[-1, 3]
        rays = position[:3] - np.asarray(centers)
        rays /= np.linalg.norm(rays, axis=1)[:, None]
        parallax = float(np.degrees(np.arccos(np.clip(rays @ rays.T, -1, 1))).max())
        rows = []
        for row, projection in zip(observations, projections):
            projected = projection @ position
            if projected[2] <= 0:
                raise ValueError("triangulated landmark is behind a reference camera")
            pixel = projected[:2] / projected[2]
            rows.append({**row, "projected_pixel": pixel.tolist(),
                         "error_px": float(np.linalg.norm(pixel - row["pixel"]))})
        results.append({"id": point["id"], "position_world": position[:3].tolist(),
                        "parallax_deg": parallax, "observations": rows,
                        "max_error_px": max(row["error_px"] for row in rows)})
    return results


def inspect_landmarks(project, receipt_path, landmarks_path):
    project = project.resolve()
    receipt_path = project_file(project, receipt_path)
    landmarks_path = project_file(project, landmarks_path)
    build = read_json(receipt_path)
    scene = read_json(project_file(project, Path(build["build_dir"]) / "job.json"))["scene"]
    if scene_hash(scene) != build["scene_hash"]:
        raise ValueError("landmark cameras differ from the built scene")
    landmarks = read_json(landmarks_path)
    results = fit_landmarks(scene["views"], landmarks)
    used = {row["view_id"] for point in landmarks for row in point["observations"]}
    inputs = {view["reference_image"]: build["asset_hashes"][view["reference_image"]]
              for view in scene["views"] if view["id"] in used}
    from world_build import file_hash
    inputs.update({str(path.relative_to(project)): file_hash(path) for path in (receipt_path, landmarks_path)})
    verify_files(project, inputs)
    return {"scene_hash": build["scene_hash"], "units": scene["units"], "input_files": inputs,
            "method": "Calibrated linear triangulation; fixed cameras, no DA3 depth or candidate surface fitting.",
            "limitations": "Correspondences are supplied, not automatically verified. Inspect reprojection error and parallax; low error does not prove correct identity. Coordinates retain the input scene scale, not necessarily metres.",
            "landmarks": results}
