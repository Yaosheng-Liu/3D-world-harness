"""Exercise real saved Blender lighting and PBR controls when Blender is present."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
from world_build import build_world


@unittest.skipUnless(shutil.which("blender"), "Blender executable required")
class WorldLightingBlenderTests(unittest.TestCase):
    def test_saved_scene_preserves_lights_parent_optics_and_render_response(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            scene = json.loads((REPO / "examples/physical_world/scene.json").read_text())
            scene["color_management"] = {"view_transform": "Standard", "exposure": 0}
            scene["lighting"] = {"environment": {"color": [1, 1, 1], "strength": 0}, "lights": [
                {"id": "key", "type": "area", "position": [0, 0, 3], "rotation": [0, 0, 0],
                 "color": [1, .9, .8], "energy": 300, "size": 2},
                {"id": "fixture", "type": "spot", "parent_id": "block", "position": [0, 0, .5],
                 "rotation": [0, 0, 0], "color": [1, 1, 1], "energy": 10, "spot_size": .7, "spot_blend": .3}]}
            scene["objects"][0]["material"].update(transmission=.15, ior=1.45, emission_strength=0)
            receipt = build_world(project, scene=scene, render=False, timeout=180)
            probe = project / "probe.py"
            probe.write_text('''import bpy,json,sys
from pathlib import Path
import numpy as np
bpy.ops.wm.open_mainfile(filepath=sys.argv[-2])
s=bpy.context.scene
lights={o.name:o for o in s.objects if o.type=='LIGHT'}
assert set(lights)=={'Light/key','Light/fixture'}
assert lights['Light/fixture'].parent['stable_id']=='block'
assert abs(lights['Light/fixture'].data.spot_size-.7)<1e-6
assert abs(lights['Light/key'].data.energy-300)<1e-6
optics=[m.node_tree.nodes.get('World shader') for m in bpy.data.materials if m.get('world_material_json') and json.loads(m['world_material_json']).get('transmission')==.15]
assert len(optics)==1 and abs(optics[0].inputs['Transmission Weight'].default_value-.15)<1e-6
assert abs(optics[0].inputs['IOR'].default_value-1.45)<1e-6
assert s.view_settings.view_transform=='Standard'
s.render.resolution_percentage=25;s.cycles.samples=8;s.render.threads_mode='FIXED';s.render.threads=4
means=[]
for state in ('on','off'):
 if state=='off':
  for light in lights.values():light.data.energy=0
 s.render.filepath=str(Path(sys.argv[-1]).with_suffix('.png'));bpy.ops.render.render(write_still=True)
 image=bpy.data.images.load(s.render.filepath,check_existing=False);pixels=np.asarray(image.pixels[:]).reshape(-1,4);means.append(float(pixels[:,:3].mean()));bpy.data.images.remove(image)
assert means[0]>means[1]+.01,means
Path(sys.argv[-1]).write_text(json.dumps({'lights':list(lights),'on_off_mean_linear_rgb':means,'saved_optics_verified':True}))
''')
            with (project / "probe.log").open("w") as log:
                subprocess.run([shutil.which("blender"), "-b", "--python-exit-code", "1", "--python", str(probe),
                                "--", receipt["blend_path"], str(project / "check.json")],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
            self.assertTrue(json.loads((project / "check.json").read_text())["saved_optics_verified"])


if __name__ == "__main__":
    unittest.main()
