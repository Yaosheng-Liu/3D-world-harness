"""Bound perspective texture interpolation error without changing a 3D surface."""
import json
import numpy as np


def projection_errors(vertices, faces, sources, extrinsics, intrinsics):
    errors = np.zeros(len(faces))
    for source in np.unique(sources[sources >= 0]):
        selected = np.flatnonzero(sources == source)
        camera = vertices[faces[selected]] @ extrinsics[source, :3, :3].T
        camera += extrinsics[source, :3, 3]
        projected = camera @ intrinsics[source].T
        uv = projected[..., :2] / projected[..., 2:]
        centroid = projected.mean(axis=1)
        centroid = centroid[:, :2] / centroid[:, 2:]
        center_error = np.linalg.norm(centroid - uv.mean(axis=1), axis=1)
        midpoint = .5 * (projected + np.roll(projected, 1, axis=1))
        midpoint = midpoint[..., :2] / midpoint[..., 2:]
        edge_error = np.linalg.norm(
            midpoint - .5 * (uv + np.roll(uv, 1, axis=1)), axis=2).max(axis=1)
        errors[selected] = np.maximum(center_error, edge_error)
    return errors


def subdivide_marked_faces(vertices, faces, sources, marked_faces):
    """Split requested edges on both adjacent faces, preserving winding/source."""
    count = len(vertices)
    edges = np.stack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]), axis=1)
    ordered = np.sort(edges.astype(np.int64), axis=2)
    keys = ordered[..., 0] * count + ordered[..., 1]
    requested = np.unique(keys[marked_faces])
    pairs = np.column_stack((requested // count, requested % count))
    new_vertices = np.concatenate((vertices, vertices[pairs].mean(axis=1)))
    positions = np.searchsorted(requested, keys)
    marked = ((positions < len(requested)) &
              (requested[np.minimum(positions, len(requested)-1)] == keys))
    midpoint = count + positions
    flags = marked @ np.array([1, 2, 4])
    parts, labels = [faces[flags == 0]], [sources[flags == 0]]
    for rotation in range(3):
        a, b, c = rotation, (rotation+1) % 3, (rotation+2) % 3
        selected = np.flatnonzero(flags == 1 << rotation)
        A, B, C = faces[selected, a], faces[selected, b], faces[selected, c]
        M = midpoint[selected, a]
        children = np.stack((np.column_stack((A, M, C)),
                             np.column_stack((M, B, C))), axis=1)
        parts.append(children.reshape(-1, 3))
        labels.append(np.repeat(sources[selected], 2))

        selected = np.flatnonzero(flags == ((1 << a) | (1 << b)))
        A, B, C = faces[selected, a], faces[selected, b], faces[selected, c]
        M, N = midpoint[selected, a], midpoint[selected, b]
        children = np.stack((np.column_stack((B, N, M)),
                             np.column_stack((A, M, C)),
                             np.column_stack((M, N, C))), axis=1)
        parts.append(children.reshape(-1, 3))
        labels.append(np.repeat(sources[selected], 3))

    selected = np.flatnonzero(flags == 7)
    A, B, C = faces[selected].T
    M, N, P = midpoint[selected].T
    children = np.stack((np.column_stack((A, M, P)), np.column_stack((M, B, N)),
                         np.column_stack((P, N, C)), np.column_stack((M, N, P))), axis=1)
    parts.append(children.reshape(-1, 3))
    labels.append(np.repeat(sources[selected], 4))
    return new_vertices, np.concatenate(parts), np.concatenate(labels)


def refine_projective_uv(vertices, faces, sources, extrinsics, intrinsics,
                         tolerance=.5, max_iterations=8, max_faces=1000000):
    original_count = len(faces)
    if original_count > max_faces:
        raise ValueError('Input surface exceeds the explicit UV refinement face limit')
    initial_errors = projection_errors(vertices, faces, sources, extrinsics, intrinsics)
    errors = initial_errors
    rounds = []
    for iteration in range(max_iterations + 1):
        bad = errors > tolerance
        if not bad.any():
            report = {
                'tolerance_pixels': tolerance, 'max_iterations': max_iterations,
                'face_limit': max_faces, 'triangles_before': original_count,
                'triangles_after': len(faces),
                'initial_faces_over_tolerance': int((initial_errors > tolerance).sum()),
                'max_error_px_before': float(initial_errors.max()),
                'max_error_px_after': float(errors.max()), 'rounds': rounds,
                'method': ('Centroid/edge-midpoint source projection error; shared edges '
                           'split consistently. Surface positions and face source images preserved.'),
            }
            return vertices, faces, sources, report
        if iteration == max_iterations:
            raise ValueError('Projective UV refinement did not reach its pixel tolerance')
        vertices, faces, sources = subdivide_marked_faces(vertices, faces, sources, bad)
        errors = projection_errors(vertices, faces, sources, extrinsics, intrinsics)
        round_info = {'iteration': iteration+1, 'triangles': len(faces),
                      'max_error_px': float(errors.max()),
                      'faces_over_tolerance': int((errors > tolerance).sum())}
        rounds.append(round_info)
        print('UV_REFINEMENT', json.dumps(round_info), flush=True)
        if len(faces) > max_faces:
            raise ValueError('Projective UV refinement exceeded its explicit face limit')
