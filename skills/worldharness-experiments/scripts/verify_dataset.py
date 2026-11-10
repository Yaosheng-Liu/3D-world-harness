"""Verify the shared raw dataset without importing model or GPU libraries."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', type=Path)
    args = parser.parse_args()
    root = args.dataset.resolve()
    manifest = json.loads((root/'manifest.json').read_text())
    checks = json.loads((root/'checksums.json').read_text())
    expected = json.loads((Path(__file__).parents[1]/'assets/scenes.json').read_text())
    assert [(x['key'], x['scene_id']) for x in manifest['scenes']] == [(x['key'], x['scene_id']) for x in expected['scenes']]
    for name, sha in checks.items():
        path = (root/name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Path outside dataset: '+name)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest() != sha:
                raise ValueError('Checksum mismatch: '+name)
    frames = 0
    for row in manifest['scenes']:
        folder = root/row['path']
        camera = json.loads((folder/'transforms.json').read_text())
        assert len(camera['frames']) == row['frame_count']
        for frame in camera['frames']:
            name = str((folder/frame['file_path']).relative_to(root))
            assert name in checks, name
        frames += len(camera['frames'])
    assert frames == 3622
    print(f'PASS: {len(manifest["scenes"])} scenes, {frames} frames, {len(checks)} file hashes')


if __name__ == '__main__':
    main()
