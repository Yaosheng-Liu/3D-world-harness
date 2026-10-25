"""Run in Blender with SCENE_WORLD_OIDN_LIBRARY to check alpha and color preservation."""
import argparse
import json
import os
from pathlib import Path
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oidn_blender import prepare_passes, denoise

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
out = args.output
out.mkdir(parents=True, exist_ok=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 16
scene.render.resolution_x = scene.render.resolution_y = 128
scene.render.resolution_percentage = 100
scene.render.film_transparent = True
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.filepath = str(out / 'raw.png')
prepare_passes(scene, out)
bpy.ops.render.render(write_still=True)
denoise(scene, out, os.environ['SCENE_WORLD_OIDN_LIBRARY'])


def pixels(path):
    image = bpy.data.images.load(str(path), check_existing=False)
    data = np.empty(128 * 128 * 4, dtype=np.float32)
    image.pixels.foreach_get(data)
    bpy.data.images.remove(image)
    return data.reshape(128, 128, 4)


raw, filtered = pixels(out / 'raw.png'), pixels(out / 'preview.png')
alpha_error = float(np.abs(raw[:, :, 3] - filtered[:, :, 3]).max())
assert alpha_error == 0, alpha_error
assert np.any((raw[:, :, 3] > 0) & (raw[:, :, 3] < 1))
assert np.all(filtered[filtered[:, :, 3] == 0, :3] == 0)
opaque = raw[:, :, 3] > .99
color_error = float(np.abs(raw[opaque, :3].mean(0) - filtered[opaque, :3].mean(0)).max())
assert color_error < .025, color_error
result = {'alpha_max_error': alpha_error, 'color_mean_error': color_error,
          'transparent_pixels': int((raw[:, :, 3] == 0).sum())}
(out / 'result.json').write_text(json.dumps(result))
print('PASS', result)
