"""Private ScanNet++ scan rays and a color reprojection check in released coordinates."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import open3d as o3d
from PIL import Image, ImageDraw

from common import digest, read, write


def scan_reference(private, width=640, stride=4):
    destination = private / "scan_reference"
    destination.mkdir(exist_ok=True)
    mesh = o3d.io.read_triangle_mesh(str(private / "scan.ply"))
    vertices, triangles = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    colors = np.asarray(mesh.vertex_colors)
    if not len(triangles) or len(colors) != len(vertices):
        raise ValueError("Scan must contain triangles and vertex colors")
    scene = o3d.t.geometry.RaycastingScene(nthreads=4)
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    arrays = {key: [] for key in ("points", "origins", "directions", "depths", "frame_indices", "reprojection_errors")}
    rows = []
    for number, frame in enumerate(read(private / "transforms.json")["frames"]):
        image = Image.open(private / frame["file_path"]).convert("RGB")
        height = round(width * image.height / image.width)
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        k = frame["intrinsics"]
        sx, sy = width/k["width"], height/k["height"]
        yy, xx = np.mgrid[:height, :width]
        # OpenGL c2w: image rows run downward, camera forward is -Z.
        local = np.stack([(xx+.5-k["cx"]*sx)/(k["fx"]*sx),
                          -(yy+.5-k["cy"]*sy)/(k["fy"]*sy), -np.ones_like(xx)], axis=-1)
        camera = np.asarray(frame["camera_to_world"])
        directions = local @ camera[:3, :3].T
        directions /= np.linalg.norm(directions, axis=-1, keepdims=True)
        origins = np.broadcast_to(camera[:3, 3], directions.shape)
        hit = scene.cast_rays(o3d.core.Tensor(np.concatenate([origins, directions], axis=-1).astype(np.float32)), nthreads=4)
        depth = hit["t_hit"].numpy()
        valid = np.isfinite(depth) & (depth > 0)
        uv = hit["primitive_uvs"].numpy()
        triangle_ids = hit["primitive_ids"].numpy()[valid]
        barycentric = np.stack([1-uv[..., 0]-uv[..., 1], uv[..., 0], uv[..., 1]], axis=-1)[valid]
        rgb = np.full((height, width, 3), 235, dtype=np.uint8)
        rgb[valid] = np.clip(np.sum(colors[triangles[triangle_ids]] * barycentric[..., None], axis=1)*255, 0, 255).astype(np.uint8)
        if number < 2:
            panel = Image.new("RGB", (width*2, height+30), "white")
            panel.paste(image, (0, 30)); panel.paste(Image.fromarray(rgb), (width, 30))
            ImageDraw.Draw(panel).text((8, 8), "RGB | released scan vertex colors at released camera (no fitting)", fill="black")
            panel.save(destination / f"reprojection_{frame['source_index']}.jpg", quality=90)
        selected = valid & (xx % stride == 0) & (yy % stride == 0)
        ray = directions[selected]
        origin = origins[selected]
        distance = depth[selected].astype(np.float64)
        arrays["directions"].append(ray)
        arrays["origins"].append(origin)
        arrays["points"].append(origin + ray*distance[:, None])
        arrays["depths"].append(distance)
        arrays["frame_indices"].append(np.full(len(distance), frame["source_index"], dtype=np.int64))
        arrays["reprojection_errors"].append(np.zeros(len(distance)))
        rows.append({"source_index": frame["source_index"], "scan_hit_fraction": float(valid.mean()), "rays": len(distance)})
    np.savez_compressed(destination / "reference.npz", **{k: np.concatenate(v) for k, v in arrays.items()})
    report = {"reference_kind": "Released ScanNet++ aligned semantic laser mesh; visible first-hit rays, not sparse SfM",
              "scan_sha256": digest(private / "scan.ply"), "camera_sha256": digest(private / "transforms.json"),
              "width": width, "pixel_stride": stride, "triangles": len(triangles), "frames": rows,
              "limitations": "Mirror hash not cross-checked with official release. No anonymization mask available; all valid scan hits are retained. This is observed surface coverage, not a full-scene symmetric mesh score."}
    write(destination / "provenance.json", report)
    print(private.name, "SCAN_REFERENCE_READY", sum(r["rays"] for r in rows), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, required=True)
    args = parser.parse_args()
    scan_reference(args.private)
