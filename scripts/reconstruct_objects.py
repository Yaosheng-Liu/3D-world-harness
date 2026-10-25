#!/usr/bin/env python3
"""Fuse instance-owned observations in the existing known-camera DA3 frame.

These open surfaces are modeling evidence, not completed editable objects.
Run in the same NumPy/OpenCV/Open3D environment as reconstruct_multiview.py.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reconstruct_multiview import (camera_arrays, digest, prepared_inputs,
                                   ray_visibility, source_data, source_file, write_json)
from multiview_uv import UVRefinementLimit


def owned_masks(masks, parents):
    """Parts own their pixels before ancestors; unrelated overlaps stay unknown."""
    import numpy as np
    result = {key: {i: value.copy() for i, value in views.items()}
              for key, views in masks.items()}
    removed = {key: {"part_pixels": 0, "conflicting_pixels": 0} for key in masks}
    for child, views in masks.items():
        ancestor, seen = parents.get(child), {child}
        while ancestor is not None:
            if ancestor in seen:
                raise ValueError("Object parent graph contains a cycle")
            seen.add(ancestor)
            for index, mask in views.items():
                if index in result.get(ancestor, {}):
                    target = result[ancestor][index]
                    removed[ancestor]["part_pixels"] += int((target & mask).sum())
                    target &= ~mask
            ancestor = parents.get(ancestor)
    for index in sorted({index for views in result.values() for index in views}):
        entries = [(key, views[index]) for key, views in result.items() if index in views]
        overlap = np.sum([mask for _, mask in entries], axis=0) > 1
        for key, mask in entries:
            removed[key]["conflicting_pixels"] += int((mask & overlap).sum())
            mask &= ~overlap
    return result, removed


def align_mask(mask, full_intrinsics, dense_intrinsics, size):
    """Use camera intrinsics, not an assumed uncropped image resize."""
    import cv2
    import numpy as np
    transform = dense_intrinsics @ np.linalg.inv(full_intrinsics)
    return cv2.warpPerspective(mask.astype(np.uint8), transform, size,
                               flags=cv2.INTER_NEAREST, borderValue=0).astype(bool)


def consistent_object_depths(depth, masks, intrinsics, extrinsics, voxel):
    """Require another same-instance surface observation; ignore occluders.

Unknown or occluded pixels are not negative evidence. A visible point landing
on a different mask, or in observed free space, is conflicting evidence.
"""
    import numpy as np
    filtered = np.zeros_like(depth)
    report = []
    cameras = np.linalg.inv(extrinsics)
    for i, mask in enumerate(masks):
        y, x = np.nonzero(mask & np.isfinite(depth[i]) & (depth[i] > 0))
        pixels = np.column_stack((x, y, np.ones(len(x))))
        points = pixels @ np.linalg.inv(intrinsics[i]).T * depth[i, y, x, None]
        points = points @ cameras[i, :3, :3].T + cameras[i, :3, 3]
        support = np.zeros(len(points), dtype=np.int32)
        conflict = np.zeros(len(points), dtype=np.int32)
        occluded = np.zeros(len(points), dtype=np.int32)
        for j in range(len(depth)):
            if i == j:
                continue
            camera = points @ extrinsics[j, :3, :3].T + extrinsics[j, :3, 3]
            z = camera[:, 2]
            uv = camera @ intrinsics[j].T
            uv = uv[:, :2] / np.maximum(z[:, None], 1e-9)
            h, w = masks[j].shape
            inside = ((z > 0) & (uv[:, 0] >= 0) & (uv[:, 0] < w - 1)
                      & (uv[:, 1] >= 0) & (uv[:, 1] < h - 1))
            u = np.clip(np.rint(uv[:, 0]).astype(int), 0, w - 1)
            v = np.clip(np.rint(uv[:, 1]).astype(int), 0, h - 1)
            observed = depth[j, v, u]
            valid = inside & np.isfinite(observed) & (observed > 0)
            tolerance = np.maximum(3 * voxel, .035 * z)
            same_surface = valid & (abs(observed - z) <= tolerance)
            support += same_surface & masks[j][v, u]
            conflict += ((same_surface & ~masks[j][v, u])
                         | (valid & (observed > z + tolerance)))
            occluded += valid & (observed < z - tolerance)
        keep = (support >= 1) & (support > conflict)
        filtered[i, y[keep], x[keep]] = depth[i, y[keep], x[keep]]
        report.append({"input_pixels": len(x), "retained_pixels": int(keep.sum()),
                       "unsupported_pixels": int((support == 0).sum()),
                       "conflicting_pixels": int((conflict > 0).sum()),
                       "occluded_in_other_views_pixels": int((occluded > 0).sum())})
    return filtered, report


def masked_atlas(images, masks):
    """Only the target instance's pixels can enter its texture image."""
    import cv2
    import numpy as np
    count, h, w = images.shape[:3]
    columns = min(4, count + 1)
    tile_w, tile_h = w + 4, h + 4
    atlas = np.full((math.ceil((count + 1) / columns) * tile_h, columns * tile_w, 3),
                    110, dtype=np.uint8)
    for i, (rgb, mask) in enumerate(zip(images, masks)):
        owned = np.where(mask[..., None], rgb, 110).astype(np.uint8)
        tile = cv2.copyMakeBorder(owned, 2, 2, 2, 2, cv2.BORDER_REPLICATE)
        x, y = i % columns * tile_w, i // columns * tile_h
        atlas[y:y + tile_h, x:x + tile_w] = tile
    return atlas, columns, tile_w, tile_h


def export_surface(mesh, dense, masks, output, voxel):
    """Export projective OBJ UVs with per-instance, depth-checked color sources."""
    import cv2
    import numpy as np
    import open3d as o3d
    from multiview_uv import refine_projective_uv
    vertices, faces = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    triangles = vertices[faces]
    centers = triangles.mean(axis=1)
    samples = np.concatenate((triangles, centers[:, None],
                              .5 * (triangles + np.roll(triangles, 1, axis=1))), axis=1)
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    E, K = dense["extrinsics"], dense["intrinsics"]
    cameras = np.linalg.inv(E)
    best = np.full(len(faces), -1, dtype=np.int32)
    best_score = np.zeros(len(faces))
    ray_scene = o3d.t.geometry.RaycastingScene()
    ray_scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    for i, mask in enumerate(masks):
        camera = samples @ E[i, :3, :3].T + E[i, :3, 3]
        z = camera[..., 2]
        uv = camera @ K[i].T
        uv = uv[..., :2] / np.maximum(z[..., None], 1e-9)
        h, w = mask.shape
        inside = ((z > 0) & (uv[..., 0] >= 1) & (uv[..., 0] < w - 2)
                  & (uv[..., 1] >= 1) & (uv[..., 1] < h - 2))
        u = np.clip(np.rint(uv[..., 0]).astype(int), 0, w - 1)
        v = np.clip(np.rint(uv[..., 1]).astype(int), 0, h - 1)
        tolerance = np.maximum(4 * voxel, .035 * z)
        matches = (inside & mask[v, u]
                   & (abs(dense["depth"][i, v, u] - z) < tolerance))
        direction = cameras[i, :3, 3] - centers
        distance = np.linalg.norm(direction, axis=1)
        incidence = abs(np.sum(normals * direction, axis=1)) / np.maximum(distance, 1e-9)
        score = incidence / np.maximum(distance ** 2, voxel ** 2)
        candidate = matches.all(axis=1) & (incidence > .15) & (score > best_score)
        selected = ray_visibility(ray_scene, centers, vertices, faces, cameras[i, :3, 3],
                                  candidate, tolerance[:, 3])
        best[selected], best_score[selected] = i, score[selected]
    vertices, faces, best, uv_report = refine_projective_uv(vertices, faces, best, E, K)
    atlas, columns, tile_w, tile_h = masked_atlas(dense["images"], masks)
    output.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output / "captured.png"), cv2.cvtColor(atlas, cv2.COLOR_RGB2BGR)):
        raise OSError("Failed to write instance texture")
    uv = np.empty((len(faces), 3, 2))
    for order in range(-1, len(masks)):
        selected = np.flatnonzero(best == order)
        if not len(selected):
            continue
        if order == -1:
            tile = len(masks)
            uv[selected] = ((tile % columns * tile_w + tile_w / 2) / atlas.shape[1],
                            1 - (tile // columns * tile_h + tile_h / 2) / atlas.shape[0])
        else:
            camera = vertices[faces[selected]] @ E[order, :3, :3].T + E[order, :3, 3]
            projected = camera @ K[order].T
            pixels = projected[..., :2] / projected[..., 2:]
            uv[selected, :, 0] = (pixels[..., 0] + order % columns * tile_w + 2.5) / atlas.shape[1]
            uv[selected, :, 1] = 1 - (pixels[..., 1] + order // columns * tile_h + 2.5) / atlas.shape[0]
    (output / "observed.mtl").write_text("newmtl captured\nKd 1 1 1\nmap_Kd captured.png\n")
    with (output / "observed.obj").open("w") as stream:
        stream.write("# Observed open surface in shared dataset coordinates; no completion.\n"
                     "mtllib observed.mtl\no observed_surface\nusemtl captured\n")
        for vertex in vertices:
            stream.write("v %.9g %.9g %.9g\n" % tuple(vertex))
        for coordinate in uv.reshape(-1, 2):
            stream.write("vt %.9g %.9g\n" % tuple(coordinate))
        for i, face in enumerate(faces):
            stream.write("f " + " ".join(f"{int(v) + 1}/{i * 3 + j + 1}" for j, v in enumerate(face)) + "\n")
    return {"vertices": len(vertices), "triangles": len(faces),
            "textured_face_fraction": float((best >= 0).mean()),
            "bounds": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
            "uv_refinement": uv_report}


def fuse_object(dense, masks, output, voxel):
    import numpy as np
    import open3d as o3d
    filtered, consistency = consistent_object_depths(
        dense["depth"], masks, dense["intrinsics"], dense["extrinsics"], voxel)
    if sum(bool(row["retained_pixels"]) for row in consistency) < 2:
        return None, consistency
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel, sdf_trunc=4 * voxel,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    _, h, w = filtered.shape
    for i, depth in enumerate(filtered):
        if not np.any(depth):
            continue
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(np.ascontiguousarray(dense["images"][i])),
            o3d.geometry.Image(np.ascontiguousarray(depth.astype(np.float32))),
            depth_scale=1., depth_trunc=float(depth.max() + voxel), convert_rgb_to_intensity=False)
        K = dense["intrinsics"][i]
        camera = o3d.camera.PinholeCameraIntrinsic(w, h, K[0, 0], K[1, 1], K[0, 2], K[1, 2])
        volume.integrate(rgbd, camera, dense["extrinsics"][i])
    mesh = volume.extract_triangle_mesh()
    mesh.remove_duplicated_vertices().remove_duplicated_triangles().remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()
    if not len(mesh.triangles):
        return None, consistency
    return export_surface(mesh, dense, masks, output, voxel), consistency


def reconstruct(project, plan_path, segments_path, output):
    import cv2
    import numpy as np
    project = Path(project).resolve(strict=True)
    plan_path, segments_path = (source_file(project, str(path)) for path in (plan_path, segments_path))
    output = source_file(project, str(output))
    if output.exists() and any(output.iterdir()):
        raise ValueError("Geometry output must be a new or empty directory")
    plan = json.loads(plan_path.read_text())
    segments = json.loads(segments_path.read_text())
    manifest = json.loads((project / "split_manifest.json").read_text())
    source = Path(manifest["source_directory"])
    data, manifest = source_data(source, project)
    prepared_hash = prepared_inputs(project, manifest)
    if (plan["source_manifest_sha256"] != prepared_hash
            or segments["observations_manifest_sha256"] != prepared_hash
            or segments["plan_sha256"] != digest(plan_path)):
        raise ValueError("Plan or segmentation does not match the frozen observations")
    inference = json.loads((project / "dense/inference.json").read_text())
    if (inference["prepared_manifest_sha256"] != prepared_hash
            or inference["depths_sha256"] != digest(project / "dense/depths.npz")):
        raise ValueError("Depth bytes or prepared inputs changed after inference")
    with np.load(project / "dense/depths.npz") as archive:
        dense = {key: archive[key] for key in archive.files}
    indices = dense["frame_indices"].tolist()
    if indices != manifest["train_indices"]:
        raise ValueError("Depth indices differ from training image identities")
    objects = {obj["id"]: obj for obj in plan["objects"]}
    if len(objects) != len(plan["objects"]) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) for key in objects):
        raise ValueError("Object IDs must be unique and safe file names")
    if set(objects) != {obj["id"] for obj in segments["objects"]} or len(objects) != len(segments["objects"]):
        raise ValueError("Segmentation must retain every planned object exactly once")
    parents = {key: obj["parent_id"] for key, obj in objects.items()}
    if any(parent is not None and parent not in objects for parent in parents.values()):
        raise ValueError("Unknown object parent")
    _, E, fullK = camera_arrays(data, indices)
    if not np.allclose(dense["extrinsics"], E, atol=1e-4):
        raise ValueError("Depth cameras differ from source cameras")
    n, h, w = dense["depth"].shape
    masks, input_files = {}, {str(plan_path.relative_to(project)): digest(plan_path),
                              str(segments_path.relative_to(project)): digest(segments_path)}
    for name in ("observations/manifest.json", "split_manifest.json", "calibration_audit.json",
                 "dense/inference.json", "dense/depths.npz"):
        input_files[name] = digest(project / name)
    for item in segments["objects"]:
        key = item["id"]
        masks[key] = {}
        if objects[key]["kind"] == "group":
            continue
        for observation in item["observations"]:
            index = observation["frame_index"]
            if index not in indices or index in masks[key]:
                raise ValueError("Masks must uniquely identify frozen training images")
            path = source_file(project, observation["mask_path"])
            actual_hash = digest(path)
            if actual_hash != observation["mask_sha256"]:
                raise ValueError("Mask bytes changed after segmentation")
            raw = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if raw is None or raw.shape != (data["h"], data["w"]) or not np.isin(raw, [0, 255]).all():
                raise ValueError("Mask must be binary PNG aligned with its prepared training image")
            order = indices.index(index)
            masks[key][index] = align_mask(raw > 0, fullK[order], dense["intrinsics"][order], (w, h))
            input_files[str(path.relative_to(project))] = actual_hash
    masks, ownership = owned_masks(masks, parents)
    positive = dense["depth"][np.isfinite(dense["depth"]) & (dense["depth"] > 0)]
    if not len(positive):
        raise ValueError("Depth evidence contains no positive finite values")
    voxel = float(np.median(positive)) / 320
    rows, files = [], {}
    for item in segments["objects"]:
        key, obj = item["id"], objects[item["id"]]
        selected = sorted(masks[key], key=indices.index)
        row = {"id": key, "status": "insufficient_evidence", "observations": [],
               "ownership": ownership[key], "reason": "No multi-view instance evidence", "files": {}}
        if obj["geometry_strategy"] in {"group", "model"}:
            row.update(status="not_required", reason="Explicit group/reference-modeling strategy; no fused mesh requested")
        elif len(selected) >= 2:
            order = [indices.index(index) for index in selected]
            subset = {key: dense[key][order] for key in ("depth", "images", "intrinsics", "extrinsics")}
            confidence = dense["confidence"][order]
            subset["depth"] = np.where(np.isfinite(confidence) & (confidence > 1.), subset["depth"], 0.)
            owned = np.stack([cv2.erode(masks[key][index].astype(np.uint8), np.ones((3, 3), np.uint8),
                                      borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool) for index in selected])
            try:
                info, consistency = fuse_object(subset, owned, output / key, voxel)
            except UVRefinementLimit as exc:
                row.update(status="needs_revision", reason=str(exc), observations=[
                    {"frame_index": index, "input_pixels": int(mask.sum())} for index, mask in zip(selected, owned)])
            else:
                row["observations"] = [{"frame_index": index, **report} for index, report in zip(selected, consistency)]
                if info is not None:
                    paths = [output / key / name for name in ("observed.obj", "observed.mtl", "captured.png")]
                    row_files = {str(path.relative_to(project)): digest(path) for path in paths}
                    row.update(status="ready", reason="Observed open surface only; requires modeling and review",
                               mesh_path=str(paths[0].relative_to(project)), texture_path=str(paths[2].relative_to(project)),
                               files=row_files, **info)
                    files.update(row_files)
                else:
                    row["reason"] = "Cross-view instance support is insufficient for a fused surface"
        else:
            row["observations"] = [{"frame_index": index, "input_pixels": int(masks[key][index].sum())}
                                   for index in selected]
        rows.append(row)
        print(json.dumps({"object_id": key, "geometry_status": row["status"]}), flush=True)
    report = {"schema_version": 1, "plan_sha256": digest(plan_path), "segments_sha256": digest(segments_path),
              "observations_manifest_sha256": prepared_hash, "depths_sha256": inference["depths_sha256"],
              "pipeline_sha256": digest(Path(__file__)), "objects": rows, "files": files,
              "input_files": input_files, "voxel_size": voxel, "coordinate_frame": "shared source world frame",
              "units": inference.get("units", "arbitrary dataset translation units"),
              "limitations": ["Multi-view consistency checks estimated depth and masks, not ground truth identity.",
                              "No unobserved back, underside or background completion is performed.",
                              "Masked photographic appearance still contains captured lighting and shadows."]}
    write_json(output / "geometry.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--plan", required=True, help="Project-relative plan JSON")
    parser.add_argument("--segments", required=True, help="Project-relative segmentation JSON")
    parser.add_argument("--output", required=True, help="New project-relative geometry directory")
    options = parser.parse_args()
    reconstruct(options.project, options.plan, options.segments, options.output)


if __name__ == "__main__":
    main()
