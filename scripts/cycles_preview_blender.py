"""Render a browser camera from an immutable world build; never save the blend."""
import json
from pathlib import Path
import sys
import time

import bpy
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parent))
from world_blender import configure_camera, check_projection


def main():
    job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    output = Path(job["output_dir"])
    started = time.monotonic()
    bpy.ops.wm.open_mainfile(filepath=job["blend_path"])
    scene = bpy.context.scene
    frame = scene.frame_start + job["time_sec"] * scene.render.fps / scene.render.fps_base
    scene.frame_set(int(frame), subframe=frame-int(frame))
    camera = bpy.data.objects.new("Cycles browser preview", bpy.data.cameras.new("Cycles browser preview"))
    scene.collection.objects.link(camera)
    camera.matrix_world = Matrix(job["camera_to_world"])
    camera.data.clip_start, camera.data.clip_end = job["near"], job["far"]
    scene.camera = camera
    view = {"id": "browser", **job}
    configure_camera(scene, camera, view)
    # Probe at scene-scale depth to avoid float32 cancellation far from the origin.
    projection = check_projection(scene, camera, view, probe_distance=max(1, camera.location.length))
    scene.render.engine = "CYCLES"
    scene.cycles.device, scene.cycles.samples = "CPU", 16
    scene.cycles.use_denoising = False
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.filepath = str(output / "preview.png")
    bpy.ops.render.render(write_still=True)
    report = {"engine": "Cycles", "samples": 16, "elapsed_seconds": time.monotonic()-started,
              "time_sec": job["time_sec"], **projection}
    (output / "report.json").write_text(json.dumps(report))


if __name__ == "__main__":
    main()
