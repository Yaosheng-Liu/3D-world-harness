#!/usr/bin/env python3
"""Known-camera RGB reconstruction; all scene distances stay in dataset units.

Run prepare/infer/fuse in the documented DA3/Open3D environment. Only infer
loads the learned model. Heldout pixels never enter calibration, depth or UVs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def split_frames(count: int, train_count: int = 48, heldout_count: int = 6):
    if min(train_count, heldout_count) < 2 or count < train_count + heldout_count:
        raise ValueError('Not enough distinct images for the requested split')
    heldout = [round(i * (count - 1) / (heldout_count - 1)) for i in range(heldout_count)]
    remaining = [i for i in range(count) if i not in heldout]
    train = [remaining[round(i * (len(remaining) - 1) / (train_count - 1))] for i in range(train_count)]
    return train, heldout


def validate_split(manifest: dict) -> None:
    train, heldout = manifest['train_indices'], manifest['heldout_indices']
    if set(train) & set(heldout) or len(set(train)) != len(train) or len(set(heldout)) != len(heldout):
        raise ValueError('Training and heldout image identities must be distinct')
    if min(train + heldout) < 0 or max(train + heldout) >= manifest['frame_count']:
        raise ValueError('Split index outside the source sequence')
    entries = manifest['frames']
    if len(entries) != len(train) + len(heldout) or {e['index'] for e in entries} != set(train + heldout):
        raise ValueError('Split image manifest is incomplete')
    for row in entries:
        expected = 'train' if row['index'] in train else 'heldout'
        if row['role'] != expected:
            raise ValueError('Image role differs from frozen split')


def source_file(source: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Source image path must stay within the source scene')
    resolved = (source / path).resolve()
    if not resolved.is_relative_to(source.resolve()):
        raise ValueError('Source image symlink escapes the source scene')
    return resolved


def prepared_inputs(output: Path, manifest: dict) -> str:
    path = output / 'observations/manifest.json'
    prepared = json.loads(path.read_text())
    if prepared['split_manifest_sha256'] != digest(output / 'split_manifest.json'):
        raise ValueError('Prepared images refer to a different frozen split')
    if prepared['calibration_audit_sha256'] != digest(output / 'calibration_audit.json'):
        raise ValueError('Camera preparation changed after input freezing')
    for role, indices in (('train', manifest['train_indices']),
                          ('heldout', manifest['heldout_indices'])):
        if [row['index'] for row in prepared[role]] != indices:
            raise ValueError('Prepared image identities differ from the frozen split')
        for row in prepared[role]:
            expected = f"observations/{role}/{row['index']:06d}.png"
            if row['path'] != expected or digest(output / expected) != row['sha256']:
                raise ValueError('Prepared image bytes changed after input freezing')
    return digest(path)


def source_data(source: Path, output: Path):
    manifest = json.loads((output / 'split_manifest.json').read_text())
    validate_split(manifest)
    if digest(source / 'transforms.json') != manifest['source_transforms_sha256']:
        raise ValueError('Source cameras changed after the split was frozen')
    data = json.loads((source / 'transforms.json').read_text())
    for row in manifest['frames']:
        if row['path'] != data['frames'][row['index']]['file_path']:
            raise ValueError('Frozen image path differs from its source frame')
        if digest(source_file(source, row['path'])) != row['sha256']:
            raise ValueError(f"Source image changed: {row['path']}")
    return data, manifest


def camera_arrays(data: dict, indices):
    import numpy as np
    if data.get('camera_model', 'PINHOLE') not in ('OPENCV', 'PINHOLE'):
        raise ValueError('Only OPENCV/pinhole cameras with shared intrinsics are supported')
    if any(data.get(key, 0) != 0 for key in ('k4', 'k5', 'k6')):
        raise ValueError('Only the five standard OPENCV distortion coefficients are supported')
    for frame in data['frames']:
        for key in ('fl_x', 'fl_y', 'cx', 'cy', 'w', 'h', 'k1', 'k2', 'p1', 'p2', 'k3', 'k4', 'k5', 'k6'):
            if key in frame and frame[key] != data.get(key, 0):
                raise ValueError('Per-frame camera calibration is not supported')
        if frame.get('camera_model', data.get('camera_model', 'PINHOLE')) != data.get('camera_model', 'PINHOLE'):
            raise ValueError('Per-frame camera models are not supported')
    cameras = np.asarray([data['frames'][i]['transform_matrix'] for i in indices], dtype=np.float32)
    extrinsics = np.linalg.inv(cameras @ np.diag([1, -1, -1, 1]).astype(np.float32))
    K = [[data['fl_x'], 0, data['cx']], [0, data['fl_y'], data['cy']], [0, 0, 1]]
    intrinsics = np.repeat(np.asarray(K, dtype=np.float32)[None], len(indices), axis=0)
    return cameras, extrinsics, intrinsics


def prepare(source: Path, output: Path) -> None:
    import cv2
    import numpy as np
    data = json.loads((source / 'transforms.json').read_text())
    manifest_path = output / 'split_manifest.json'
    if not manifest_path.exists():
        train, heldout = split_frames(len(data['frames']))
        write_json(manifest_path, {
            'scene_id': source.name, 'source_directory': str(source),
            'source_transforms_sha256': digest(source / 'transforms.json'),
            'frame_count': len(data['frames']), 'train_indices': train, 'heldout_indices': heldout,
            'split_policy': (
                'Evenly spaced heldout images fixed independently of image content; '
                'training images exclude all heldout identities. Supplied cameras use '
                'full-dataset SfM, not independent camera ground truth.'),
            'frames': [{'index': i, 'role': 'train' if i in train else 'heldout',
                        'path': data['frames'][i]['file_path'],
                        'sha256': digest(source_file(source, data['frames'][i]['file_path']))}
                       for i in sorted(train + heldout)]})
    data, manifest = source_data(source, output)
    ids = manifest['train_indices']
    _, extrinsics, matrices = camera_arrays(data, ids)
    K = matrices[0].astype(float)
    distortion = np.asarray([data.get(k, 0) for k in ('k1', 'k2', 'p1', 'p2', 'k3')], dtype=float)
    detector, matcher = cv2.SIFT_create(nfeatures=5000), cv2.BFMatcher()
    errors, pairs = [[], []], []
    for order in np.linspace(0, len(ids) - 4, 12, dtype=int):
        j = int(order + 3)
        images = [
            cv2.imread(str(source_file(source, data['frames'][ids[k]]['file_path'])),
                       cv2.IMREAD_GRAYSCALE) for k in (order, j)]
        features = [detector.detectAndCompute(image, None) for image in images]
        if any(descriptor is None for _, descriptor in features):
            continue
        matches = [p[0] for p in matcher.knnMatch(features[0][1], features[1][1], k=2)
                   if len(p) == 2 and p[0].distance < .72 * p[1].distance]
        if len(matches) < 20:
            continue
        a = np.float32([features[0][0][m.queryIdx].pt for m in matches])
        b = np.float32([features[1][0][m.trainIdx].pt for m in matches])
        _, mask = cv2.findFundamentalMat(a, b, cv2.FM_RANSAC, 1.5, .999)
        if mask is None:
            continue
        a, b = a[mask.ravel() > 0], b[mask.ravel() > 0]
        relative = extrinsics[j] @ np.linalg.inv(extrinsics[order])
        x, y, z = relative[:3, 3]
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        fundamental = np.linalg.inv(K).T @ skew @ relative[:3, :3] @ np.linalg.inv(K)
        rectified = tuple(cv2.undistortPoints(points[:, None].astype(float),
                                            K, distortion, P=K)[:, 0]
                          for points in (a, b))
        variants = [(a, b), rectified]
        medians = []
        for k, (first, second) in enumerate(variants):
            first, second = np.c_[first, np.ones(len(first))], np.c_[second, np.ones(len(second))]
            lines_b, lines_a = first @ fundamental.T, second @ fundamental
            inverse_line_length = (
                1 / np.maximum(np.linalg.norm(lines_b[:, :2], axis=1), 1e-9)
                + 1 / np.maximum(np.linalg.norm(lines_a[:, :2], axis=1), 1e-9))
            residual = abs(np.sum(second * lines_b, axis=1)) * .5 * inverse_line_length
            errors[k].extend(residual.tolist())
            medians.append(float(np.median(residual)))
        pairs.append({'indices': [ids[int(order)], ids[j]], 'matches': len(a), 'median_px_stored_rectified': medians})
    if not pairs or sum(len(e) for e in errors) < 80:
        raise ValueError('Insufficient training feature support for distortion audit')
    medians = [float(np.median(e)) for e in errors]
    rectify = medians[1] < .8 * medians[0]
    audit = {
        'training_only': True, 'pair_count': len(pairs), 'matches': len(errors[0]),
        'median_px_stored_rectified': medians, 'rectify': rectify, 'pairs': pairs,
        'note': 'Relative epipolar check with supplied SfM cameras; not ground-truth accuracy.',
    }
    write_json(output / 'calibration_audit.json', audit)
    for role, indices in (('train', ids), ('heldout', manifest['heldout_indices'])):
        directory = output / 'observations' / role
        directory.mkdir(parents=True, exist_ok=True)
        for i in indices:
            image = cv2.imread(str(source_file(source, data['frames'][i]['file_path'])))
            if image.shape[:2] != (data['h'], data['w']):
                raise ValueError('Image dimensions differ from camera calibration')
            if rectify:
                image = cv2.undistort(image, K, distortion, None, K)
            if not cv2.imwrite(str(directory / f'{i:06d}.png'), image):
                raise OSError('Failed to write prepared image')
    prepared = {
        'split_manifest_sha256': digest(manifest_path),
        'calibration_audit_sha256': digest(output / 'calibration_audit.json'),
    }
    for role, indices in (('train', ids), ('heldout', manifest['heldout_indices'])):
        prepared[role] = [
            {'index': i, 'path': f'observations/{role}/{i:06d}.png',
             'sha256': digest(output / f'observations/{role}/{i:06d}.png')}
            for i in indices
        ]
    write_json(output / 'observations/manifest.json', prepared)
    # Review includes training images only, even when heldout references exist.
    tiles = [cv2.resize(cv2.imread(str(output / 'observations/train' / f'{i:06d}.png')), (320, 180)) for i in ids]
    sheet_rows = [np.concatenate(tiles[i:i+8], axis=1) for i in range(0, len(tiles), 8)]
    cv2.imwrite(str(output / 'training_contact_sheet.jpg'), np.concatenate(sheet_rows))
    print('PREPARED', json.dumps(audit), flush=True)


def infer(source: Path, output: Path, model_path: Path, resolution: int) -> None:
    import numpy as np
    import torch
    import depth_anything_3.api as da3_api
    data, manifest = source_data(source, output)
    prepared_hash = prepared_inputs(output, manifest)
    ids = manifest['train_indices']
    _, extrinsics, intrinsics = camera_arrays(data, ids)
    paths = [str(output / 'observations/train' / f'{i:06d}.png') for i in ids]
    started = time.monotonic()
    torch.set_num_threads(8)
    model = da3_api.DepthAnything3.from_pretrained(str(model_path)).to('cuda')
    prediction = model.inference(
        image=paths, extrinsics=extrinsics, intrinsics=intrinsics,
        align_to_input_ext_scale=True, infer_gs=False, use_ray_pose=False,
        process_res=resolution, process_res_method='upper_bound_resize')
    depth = np.asarray(prediction.depth, dtype=np.float32)
    predicted_ext = np.asarray(prediction.extrinsics, dtype=np.float32)
    if predicted_ext.shape[-2:] == (3, 4):
        expanded = np.repeat(np.eye(4, dtype=np.float32)[None], len(ids), axis=0)
        expanded[:, :3] = predicted_ext
        predicted_ext = expanded
    if not np.isfinite(depth).all() or not (depth > 0).all() or not np.allclose(predicted_ext, extrinsics, atol=1e-4):
        raise ValueError('Depth must be finite, positive and aligned to supplied cameras')
    if prediction.conf is None:
        raise ValueError('DA3 confidence is required for multi-view filtering')
    dense = output / 'dense'
    dense.mkdir(exist_ok=True)
    np.savez_compressed(
        dense / 'depths.npz', depth=depth,
        confidence=np.asarray(prediction.conf, dtype=np.float32),
        intrinsics=np.asarray(prediction.intrinsics, dtype=np.float32),
        extrinsics=predicted_ext, images=np.asarray(prediction.processed_images, dtype=np.uint8),
        frame_indices=np.asarray(ids))
    code_root = Path(da3_api.__file__).resolve().parent
    code_hashes = {str(p.relative_to(code_root)): digest(p)
                   for p in sorted(code_root.rglob('*.py'))}
    if not code_hashes:
        raise ValueError('No imported DA3 Python sources found for provenance')
    if prepared_inputs(output, manifest) != prepared_hash:
        raise ValueError('Prepared input manifest changed during inference')
    write_json(dense / 'inference.json', {
        'prepared_manifest_sha256': prepared_hash,
        'pipeline_sha256': digest(Path(__file__)),
        'depths_sha256': digest(dense / 'depths.npz'),
        'model': model_path.name, 'model_path': str(model_path),
        'weights_sha256': digest(model_path / 'model.safetensors'),
        'config_sha256': digest(model_path / 'config.json'),
        'model_python_source_directory': str(code_root),
        'model_python_sources_sha256': code_hashes,
        'training_indices': ids, 'depth_shape': list(depth.shape),
        'elapsed_seconds': time.monotonic() - started,
        'units': 'arbitrary dataset translation units',
        'depth_convention': 'OpenCV camera Z, not Euclidean ray distance',
        'resolution': resolution,
        'camera_alignment_max_abs': float(np.max(abs(predicted_ext - extrinsics))),
        'torch': torch.__version__,
    })
    del model, prediction
    torch.cuda.empty_cache()
    print('INFERRED', depth.shape, 'seconds', time.monotonic() - started, flush=True)


def consistent_depths(depth, confidence, K, E, voxel):
    import numpy as np
    n, h, w = depth.shape
    yy, xx = np.mgrid[:h, :w]
    pixels = np.stack([xx, yy, np.ones_like(xx)], axis=-1).reshape(-1, 3)
    C = np.linalg.inv(E)
    centers, forward = C[:, :3, 3], C[:, :3, 2]
    filtered, stats = np.zeros_like(depth), []
    for i in range(n):
        points = pixels @ np.linalg.inv(K[i]).T * depth[i].reshape(-1, 1)
        points = points @ C[i, :3, :3].T + centers[i]
        distance = np.linalg.norm(centers - centers[i], axis=1)
        rank = distance / max(float(np.median(distance)), voxel) + 2 * (1 - forward @ forward[i])
        rank[i] = math.inf
        neighbors = np.argsort(rank)[:min(8, n-1)]
        support, conflict = np.zeros(h*w, dtype=np.uint8), np.zeros(h*w, dtype=np.uint8)
        for j in neighbors:
            camera = points @ E[j, :3, :3].T + E[j, :3, 3]
            z = camera[:, 2]
            uv = camera @ K[j].T
            uv = uv[:, :2] / np.maximum(z[:, None], 1e-9)
            visible = (z > 0) & (uv[:, 0] >= 1) & (uv[:, 0] < w-2) & (uv[:, 1] >= 1) & (uv[:, 1] < h-2)
            u = np.clip(np.rint(uv[:, 0]).astype(int), 0, w-1)
            v = np.clip(np.rint(uv[:, 1]).astype(int), 0, h-1)
            other = depth[j, v, u]
            tolerance = np.maximum(voxel*3, .035*z)
            support += visible & (abs(other-z) < tolerance)
            conflict += visible & (other-z > 2*tolerance)
        conf = confidence[i].ravel()
        confident = conf >= max(1.05, float(np.percentile(conf, 8)))
        keep = (((support >= 1) & (conflict <= 3) & confident)
                | ((support >= 3) & (conflict <= 2) & (conf > 1.1)))
        keep &= np.isfinite(depth[i]).ravel() & (depth[i].ravel() > voxel)
        filtered[i] = np.where(keep.reshape(h, w), depth[i], 0)
        stats.append({'training_index': i, 'retained_fraction': float(keep.mean()), 'neighbor_orders': neighbors.tolist()})
        if i % 8 == 0:
            print('FILTERED', i, n, float(keep.mean()), flush=True)
    return filtered, stats


def ray_visibility(scene, centers, vertices, faces, camera_center, candidate, tolerance):
    """First-hit checks at face centroid and edge midpoints reject occluded UVs."""
    import numpy as np
    import open3d as o3d
    indices = np.flatnonzero(candidate)
    if not len(indices):
        return indices
    tri = vertices[faces[indices]]
    samples = np.concatenate([centers[indices, None], .5 * (tri + np.roll(tri, 1, axis=1))], axis=1)
    delta = samples - camera_center
    distance = np.linalg.norm(delta, axis=2)
    directions = delta / np.maximum(distance[..., None], 1e-9)
    origins = np.broadcast_to(camera_center, directions.shape)
    rays = np.concatenate([origins, directions], axis=2).reshape(-1, 6).astype(np.float32)
    hit = scene.cast_rays(o3d.core.Tensor(rays))['t_hit'].numpy().reshape(-1, 4)
    visible = np.all(np.isfinite(hit) & (abs(hit - distance) < tolerance[indices, None]), axis=1)
    return indices[visible]


def textured_obj(mesh, dense, source_info, manifest, output: Path, voxel):
    import cv2
    import numpy as np
    import open3d as o3d
    vertices, faces = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    triangle = vertices[faces]
    centers = triangle.mean(axis=1)
    normals = np.cross(triangle[:, 1]-triangle[:, 0], triangle[:, 2]-triangle[:, 0])
    twice_area = np.linalg.norm(normals, axis=1)
    normals /= np.maximum(twice_area[:, None], 1e-12)
    scores = np.zeros(len(faces))
    best = np.full(len(faces), -1, dtype=np.int32)
    ids = manifest['train_indices']
    _, extrinsics, fullK = camera_arrays(source_info, ids)
    C = np.linalg.inv(extrinsics)
    ray_scene = o3d.t.geometry.RaycastingScene()
    ray_scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    h, w = source_info['h'], source_info['w']
    for i in range(len(ids)):
        E, K = extrinsics[i], fullK[i]
        projected = vertices @ E[:3, :3].T + E[:3, 3]
        z = projected[:, 2]
        uv = projected @ K.T
        uv = uv[:, :2] / np.maximum(z[:, None], 1e-9)
        inside = (z > 0) & (uv[:, 0] >= 2) & (uv[:, 0] < w-3) & (uv[:, 1] >= 2) & (uv[:, 1] < h-3)
        candidate = inside[faces].all(axis=1)
        center_camera = centers @ E[:3, :3].T + E[:3, 3]
        center_z = center_camera[:, 2]
        direction = C[i, :3, 3] - centers
        distance = np.linalg.norm(direction, axis=1)
        incidence = abs(np.sum(normals * direction, axis=1)) / np.maximum(distance, 1e-9)
        score = incidence / np.maximum(distance**2, voxel**2)
        candidate &= (incidence > .15) & (score > scores)
        depth_uv = center_camera @ dense['intrinsics'][i].T
        depth_uv = depth_uv[:, :2] / np.maximum(center_z[:, None], 1e-9)
        dh, dw = dense['depth'][i].shape
        candidate &= ((depth_uv[:, 0] >= 0) & (depth_uv[:, 0] < dw)
                      & (depth_uv[:, 1] >= 0) & (depth_uv[:, 1] < dh))
        du = np.clip(np.rint(depth_uv[:, 0]).astype(int), 0, dw-1)
        dv = np.clip(np.rint(depth_uv[:, 1]).astype(int), 0, dh-1)
        tolerance = np.maximum(4*voxel, .035*np.maximum(center_z, 0))
        candidate &= abs(dense['depth'][i, dv, du] - center_z) < tolerance
        selected = ray_visibility(ray_scene, centers, vertices, faces, C[i, :3, 3], candidate, tolerance)
        best[selected], scores[selected] = i, score[selected]
        if i % 8 == 0:
            print('TEXTURE_VIEW', i, 'assigned', int((best >= 0).sum()), '/', len(faces), flush=True)
    from multiview_uv import refine_projective_uv
    vertices, faces, best, refinement = refine_projective_uv(
        vertices, faces, best, extrinsics, fullK)
    mesh.vertices = o3d.utility.Vector3dVector(vertices)
    mesh.triangles = o3d.utility.Vector3iVector(faces)
    mesh.vertex_colors = o3d.utility.Vector3dVector()
    mesh.compute_vertex_normals()
    triangle = vertices[faces]
    twice_area = np.linalg.norm(np.cross(triangle[:, 1]-triangle[:, 0],
                                        triangle[:, 2]-triangle[:, 0]), axis=1)
    print('REFINED_UV', json.dumps(refinement), flush=True)
    columns = 8
    tile_w, tile_h = w+4, h+4
    rows = math.ceil((len(ids)+1)/columns)
    atlas = np.full((rows*tile_h, columns*tile_w, 3), 110, dtype=np.uint8)
    for order, index in enumerate(ids):
        rgb = cv2.imread(str(output / 'observations/train' / f'{index:06d}.png'))
        tile = cv2.copyMakeBorder(rgb, 2, 2, 2, 2, cv2.BORDER_REPLICATE)
        x, y = order % columns * tile_w, order // columns * tile_h
        atlas[y:y+tile_h, x:x+tile_w] = tile
    assets = output / 'assets'
    assets.mkdir(exist_ok=True)
    cv2.imwrite(str(assets / 'captured_atlas.png'), atlas)
    uv = np.empty((len(faces), 3, 2))
    for order in range(-1, len(ids)):
        selected = np.flatnonzero(best == order)
        if not len(selected):
            continue
        if order == -1:
            uv[selected] = (
                (len(ids) % columns * tile_w + tile_w/2) / atlas.shape[1],
                1 - (len(ids)//columns * tile_h + tile_h/2) / atlas.shape[0])
            continue
        camera = triangle[selected] @ extrinsics[order, :3, :3].T + extrinsics[order, :3, 3]
        pixels = camera @ fullK[order].T
        pixels = pixels[..., :2] / pixels[..., 2:]
        pixels[..., 0] += order % columns * tile_w + 2.5
        pixels[..., 1] += order // columns * tile_h + 2.5
        uv[selected, :, 0] = pixels[..., 0] / atlas.shape[1]
        uv[selected, :, 1] = 1 - pixels[..., 1] / atlas.shape[0]
    with (assets / 'observed_environment.obj').open('w') as stream:
        stream.write('# Observed multi-view TSDF surface; no invented completion. Units arbitrary.\no observed_environment\n')
        for vertex in vertices:
            stream.write('v %.8g %.8g %.8g\n' % tuple(vertex))
        for coordinate in uv.reshape(-1, 2):
            stream.write('vt %.9g %.9g\n' % tuple(coordinate))
        for i, face in enumerate(faces):
            stream.write('f ' + ' '.join(f'{int(v)+1}/{i*3+j+1}' for j, v in enumerate(face)) + '\n')
    np.savez_compressed(
        assets / 'surface_observations.npz', face_source_training_order=best,
        source_indices=np.asarray(ids), untextured_faces=np.flatnonzero(best < 0))
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    unique_edges, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = unique_edges[counts == 1]
    coverage = []
    for order, index in enumerate(ids):
        K = fullK[order]
        yy, xx = np.mgrid[0:h:6, 0:w:6]
        pixels = np.stack([xx, yy, np.ones_like(xx)], axis=-1).reshape(-1, 3)
        directions = pixels @ np.linalg.inv(K).T @ C[order, :3, :3].T
        directions /= np.linalg.norm(directions, axis=1, keepdims=True)
        rays = np.c_[np.broadcast_to(C[order, :3, 3], directions.shape), directions].astype(np.float32)
        hit = ray_scene.cast_rays(o3d.core.Tensor(rays))['t_hit'].numpy()
        coverage.append({
            'source_index': index, 'sample_count': len(hit),
            'observed_surface_hit_fraction': float(np.isfinite(hit).mean())})
    return {
        'triangles': len(faces), 'vertices': len(vertices),
        'textured_face_fraction': float((best >= 0).mean()),
        'textured_area_fraction': float(twice_area[best >= 0].sum()/twice_area.sum()),
        'untextured_faces': int((best < 0).sum()),
        'boundary_edges': len(boundary),
        'boundary_length_dataset_units': float(np.linalg.norm(
            vertices[boundary[:, 0]]-vertices[boundary[:, 1]], axis=1).sum()),
        'atlas_size': [atlas.shape[1], atlas.shape[0]],
        'training_view_coverage': coverage,
        'uv_refinement': refinement,
        'uv_refinement_code_sha256': digest(Path(refine_projective_uv.__code__.co_filename)),
        'texture_selection': (
            'Training RGB only; best incidence/distance score subject to projected '
            'triangle bounds, DA3 center depth and mesh first-hit checks at centroid '
            'plus three edge midpoints. Per-triangle seams and baked illumination remain.'),
        'hole_policy': (
            'No surface in unobserved space. Open mesh boundaries retained; '
            'visible surfaces without a valid source view are neutral grey.'),
    }



def fuse(source: Path, output: Path) -> None:
    import numpy as np
    import open3d as o3d
    data, manifest = source_data(source, output)
    prepared_hash = prepared_inputs(output, manifest)
    inference = json.loads((output / 'dense/inference.json').read_text())
    if inference['prepared_manifest_sha256'] != prepared_hash:
        raise ValueError('Fusion inputs differ from the inference inputs')
    if inference['depths_sha256'] != digest(output / 'dense/depths.npz'):
        raise ValueError('Depth artifact changed after inference')
    with np.load(output / 'dense/depths.npz') as archive:
        dense = {name: archive[name] for name in archive.files}
    if dense['frame_indices'].tolist() != manifest['train_indices']:
        raise ValueError('Inference frames differ from the frozen training set')
    started = time.monotonic()
    depth, K, E = dense['depth'], dense['intrinsics'], dense['extrinsics']
    voxel = float(np.median(depth))/320
    filtered, stats = consistent_depths(depth, dense['confidence'], K, E, voxel)
    np.savez_compressed(output / 'dense/filtered_depths.npz', depth=filtered)
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel, sdf_trunc=voxel*4,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    n, h, w = depth.shape
    depth_truncation = float(np.percentile(depth, 98))
    for i in range(n):
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(np.ascontiguousarray(dense['images'][i])),
            o3d.geometry.Image(np.ascontiguousarray(filtered[i])),
            depth_scale=1., depth_trunc=depth_truncation, convert_rgb_to_intensity=False)
        camera = o3d.camera.PinholeCameraIntrinsic(
            w, h, float(K[i, 0, 0]), float(K[i, 1, 1]), float(K[i, 0, 2]), float(K[i, 1, 2]))
        volume.integrate(rgbd, camera, E[i].astype(float))
        if i % 8 == 0:
            print('INTEGRATED', i, n, flush=True)
    print('EXTRACTING_SURFACE', flush=True)
    mesh = volume.extract_triangle_mesh()
    mesh.remove_duplicated_vertices()
    mesh.remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()
    labels, counts, _ = mesh.cluster_connected_triangles()
    mesh.remove_triangles_by_mask(np.asarray(counts)[np.asarray(labels)] < 60)
    mesh.remove_unreferenced_vertices()
    triangles_before = len(mesh.triangles)
    before = training_surface_hits(mesh, data, manifest['train_indices'])
    (output / 'intermediate').mkdir(exist_ok=True)
    pre_simplification = output / 'intermediate/tsdf_pre_simplification.ply'
    o3d.io.write_triangle_mesh(str(pre_simplification), mesh)
    cluster_size = 6 * voxel
    mesh = mesh.simplify_vertex_clustering(cluster_size)
    faces = np.asarray(mesh.triangles)
    mesh.triangles = o3d.utility.Vector3iVector(faces[unique_triangle_indices(faces)])
    mesh.remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()
    if not len(mesh.triangles):
        raise ValueError('No observed geometry survived simplification')
    after = training_surface_hits(mesh, data, manifest['train_indices'])
    audit = simplification_audit(before, after, manifest['train_indices'])
    write_json(output / 'simplification_audit.json', audit)
    if not audit['passed']:
        raise ValueError('Simplification exceeds the training coverage loss limit; see audit')
    lineage = {
        'kind': 'full_reconstruction',
        'pre_simplification_sha256': digest(pre_simplification),
        'simplification_audit_sha256': digest(output / 'simplification_audit.json'),
    }
    export_reconstruction(mesh, dense, data, manifest, output, voxel, stats,
                          triangles_before, started, cluster_size, lineage)


def training_surface_hits(mesh, data, indices):
    """Sample only training cameras; ray distances do not use RGB pixels."""
    import numpy as np
    import open3d as o3d
    _, extrinsics, intrinsics = camera_arrays(data, indices)
    camera_to_world = np.linalg.inv(extrinsics)
    pixel_stride = 16
    yy, xx = np.mgrid[pixel_stride//2:data['h']:pixel_stride,
                     pixel_stride//2:data['w']:pixel_stride]
    pixels = np.column_stack((xx.ravel(), yy.ravel(), np.ones(xx.size)))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    distances = []
    for camera, K in zip(camera_to_world, intrinsics):
        directions = pixels @ np.linalg.inv(K).T @ camera[:3, :3].T
        directions /= np.linalg.norm(directions, axis=1, keepdims=True)
        origins = np.broadcast_to(camera[:3, 3], directions.shape)
        rays = np.column_stack((origins, directions)).astype(np.float32)
        distances.append(scene.cast_rays(o3d.core.Tensor(rays))['t_hit'].numpy())
    return np.asarray(distances)


def simplification_audit(before, after, indices, max_coverage_drop=.03):
    """Reject excessive loss of observed training-camera coverage; never fill holes."""
    import numpy as np
    rows = []
    for index, original, simplified in zip(indices, before, after):
        original_hit, simplified_hit = np.isfinite(original), np.isfinite(simplified)
        shared = original_hit & simplified_hit
        original_fraction = float(original_hit.mean())
        simplified_fraction = float(simplified_hit.mean())
        relative = abs(simplified[shared] - original[shared]) / original[shared]
        rows.append({
            'source_index': index, 'samples': len(original),
            'before_hit_fraction': original_fraction,
            'after_hit_fraction': simplified_fraction,
            'coverage_drop': original_fraction - simplified_fraction,
            'lost_original_hit_fraction': float((original_hit & ~simplified_hit).mean()),
            'shared_hit_relative_depth_error_p95': (
                float(np.percentile(relative, 95)) if len(relative) else None),
        })
    maximum = max(row['coverage_drop'] for row in rows)
    return {
        'pixel_stride': 16, 'maximum_allowed_coverage_drop': max_coverage_drop,
        'maximum_observed_coverage_drop': maximum,
        'passed': maximum <= max_coverage_drop, 'training_views': rows,
        'scope': 'Training-camera sparse ray coverage only; not a completeness guarantee.',
    }


def unique_triangle_indices(triangles):
    """Keep the first triangle for each vertex set, including reversed winding."""
    import numpy as np
    _, first = np.unique(np.sort(triangles, axis=1), axis=0, return_index=True)
    return np.sort(first)


def export_reconstruction(mesh, dense, data, manifest, output, voxel, stats,
                          triangles_before, started, cluster_size, lineage):
    import numpy as np
    import open3d as o3d
    prepared_hash = prepared_inputs(output, manifest)
    source_id = manifest['scene_id']
    triangles = np.asarray(mesh.triangles)
    keep = unique_triangle_indices(triangles)
    duplicate_count = len(triangles) - len(keep)
    mesh.triangles = o3d.utility.Vector3iVector(triangles[keep])
    mesh.remove_degenerate_triangles()
    degenerate_count = len(keep) - len(mesh.triangles)
    mesh.remove_unreferenced_vertices()
    mesh.compute_vertex_normals()
    print('CLEANED_TOPOLOGY', duplicate_count, 'duplicates,',
          degenerate_count, 'degenerate faces removed', flush=True)
    (output / 'assets').mkdir(exist_ok=True)
    texture = textured_obj(mesh, dense, data, manifest, output, voxel)
    o3d.io.write_triangle_mesh(str(output / 'assets/observed_surface.ply'), mesh)
    if prepared_inputs(output, manifest) != prepared_hash:
        raise ValueError('Prepared input manifest changed during texture generation')
    # Keep the supplied world frame: Z is not inferred to be gravity. Identity is explicit.
    report = {
        'scene_id': source_id,
        'method': 'Known-camera DA3, multi-view consistency, TSDF, visibility-tested RGB atlas',
        'units': 'arbitrary', 'metric_scale': None,
        'source_to_world': np.eye(4).tolist(),
        'up_axis_note': 'Source coordinates retained; scene Z is a display convention, not gravity.',
        'camera_truth': 'Full-dataset SfM including heldout cameras, not independent camera truth.',
        'training_indices': manifest['train_indices'],
        'heldout_indices': manifest['heldout_indices'],
        'split_manifest_sha256': digest(output / 'split_manifest.json'),
        'prepared_manifest_sha256': prepared_hash,
        'inference_manifest_sha256': digest(output / 'dense/inference.json'),
        'source_transforms_sha256': manifest['source_transforms_sha256'],
        'pipeline_sha256': digest(Path(__file__)),
        'calibration': json.loads((output / 'calibration_audit.json').read_text()),
        'voxel_size_dataset_units': voxel,
        'triangles_before_simplification': triangles_before,
        'simplification_method': 'Vertex clustering at six TSDF voxels; no QEM or hole filling',
        'vertex_cluster_size_dataset_units': cluster_size,
        'geometry_lineage': lineage,
        'topology_cleanup': {'duplicate_faces_removed': duplicate_count,
                             'degenerate_faces_removed': degenerate_count},
        'consistency': stats,
        **texture,
        'timing': ({'fresh_fusion_seconds': time.monotonic()-started}
                   if lineage['kind'] == 'full_reconstruction' else
                   {'postprocess_seconds': time.monotonic()-started,
                    'source_candidate_fusion_seconds': lineage['source_candidate_fusion_seconds']}),
        'object_segmentation': 'not_performed; one editable observed_environment mesh',
        'physics': 'not_calibrated; disabled for arbitrary units',
        'appearance': 'captured illumination, not intrinsic albedo',
        'heldout_evaluation_at_reconstruction': (
            'Not run yet; evaluation is recorded separately after freezing. '
            'No heldout RGB used for reconstruction.'),
        'heldout_rgb_used_for_reconstruction': False,
        'prior_candidate_inspection_indices': manifest.get('prior_candidate_inspection_indices', []),
        'evaluation_blinding': 'Not strictly blind when prior candidate inspection included heldout images.',
        'artifacts': {str(p.relative_to(output)): digest(p)
                      for p in (output / 'assets').iterdir() if p.is_file()},
    }
    write_json(output / 'reconstruction_report.json', report)
    views = []
    for index in manifest['heldout_indices']:
        views.append({
            'id': f'heldout_{index:06d}',
            'camera_to_world': data['frames'][index]['transform_matrix'],
            'intrinsics': {'width': data['w'], 'height': data['h'],
                           'fx': data['fl_x'], 'fy': data['fl_y'],
                           'cx': data['cx'], 'cy': data['cy']},
            'reference_image': f'observations/heldout/{index:06d}.png',
        })
    sources = [
        ('split_manifest.json', 'Frozen image split, source hashes and location'),
        ('observations/manifest.json', 'Prepared images and their hashes'),
        ('calibration_audit.json', 'Training-only distortion audit'),
        ('dense/inference.json', 'Model code and weights, input hashes, runtime'),
        ('reconstruction_report.json', 'Coordinates, coverage, limits and asset hashes'),
    ]
    scene = {
        'schema_version': 1, 'revision': 1, 'scene_id': 'dl3dv_' + source_id,
        'name': f'DL3DV {source_id[:12]}: observed multi-view reconstruction',
        'units': 'arbitrary', 'up_axis': 'Z',
        'provenance': {
            'kind': 'reconstruction',
            'description': ('Real RGB and supplied SfM cameras. Uncalibrated dataset scale. '
                            'Captured appearance includes lighting. One observed mesh; '
                            'object segmentation and physical calibration are pending.'),
            'sources': [{'path': path, 'description': description}
                        for path, description in sources],
        },
        'objects': [{
            'id': 'observed_environment', 'name': 'Observed environment geometry',
            'parent_id': None, 'position': [0, 0, 0], 'rotation': [0, 0, 0],
            'visual': [{'type': 'mesh', 'path': 'assets/observed_environment.obj',
                        'scale': [1, 1, 1]}],
            'collision': [{'type': 'box', 'size': [voxel, voxel, voxel]}],
            'material': {'base_color': [1, 1, 1, 1], 'roughness': 1, 'metallic': 0,
                         'base_color_texture': 'assets/captured_atlas.png',
                         'appearance': 'captured'},
            'physics': {'body_type': 'static', 'mass': 0, 'friction': .5},
            'evidence': {
                'geometry': 'estimated', 'physics': 'estimated',
                'notes': ('Observed mesh, not complete geometry or separated objects. '
                          'Collision box is a schema placeholder, not a physical proxy; '
                          'simulation is disabled by arbitrary units.'),
            },
        }],
        'views': views,
    }
    write_json(output / 'world/scene.json', scene)
    summary_keys = ('triangles', 'vertices', 'textured_face_fraction',
                    'textured_area_fraction', 'boundary_edges', 'timing')
    print('COMPLETE', json.dumps({key: report[key] for key in summary_keys}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', choices=('prepare', 'infer', 'fuse', 'all'), default='all')
    parser.add_argument('--model', type=Path, default=Path('/opt/da3/checkpoints/DA3NESTED-GIANT-LARGE'))
    parser.add_argument('--resolution', type=int, default=756)
    args = parser.parse_args()
    if (args.output / 'world/scene.json').exists():
        raise FileExistsError('An initialized world already exists; reconstruct into a new project')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage in ('prepare', 'all'):
        prepare(args.scene, args.output)
    if args.stage in ('infer', 'all'):
        infer(args.scene, args.output, args.model, args.resolution)
    if args.stage in ('fuse', 'all'):
        fuse(args.scene, args.output)


if __name__ == '__main__':
    main()
