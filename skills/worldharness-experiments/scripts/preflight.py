"""Fail early on missing runtime dependencies; does not call a paid model."""
import argparse
import ctypes
import json
import platform
from pathlib import Path
import subprocess
import sys


def run(argv):
    return subprocess.check_output(list(map(str,argv)),stderr=subprocess.STDOUT,text=True,timeout=180).strip()


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r=json.loads(a.runtime.read_text())
    if sys.platform!='linux':
        raise SystemExit('Full experiment runner requires Linux. Use WSL2 with NVIDIA GPU or a remote Linux GPU host.')
    result={'platform':platform.platform(),'machine':platform.machine(),'python':sys.version}
    result['codex']=run([r['codex'],'--version'])
    if result['codex']!='codex-cli 0.164.0':
        raise SystemExit('Headless experiments are pinned to codex-cli 0.164.0; workbench App Server uses its separate CLI version.')
    result['blender']=run([r['blender'],'--version']).splitlines()[0]
    result['systemd_scope']=run(['systemd-run','--user','--scope','--quiet','/bin/true'])
    lib=ctypes.CDLL(r['oidn_library']);lib.oidnNewDevice
    result['oidn_library']=r['oidn_library']
    import cv2,numpy,PIL,jsonschema
    result['host_packages']={'opencv':cv2.__version__,'numpy':numpy.__version__,'pillow':PIL.__version__}
    for path in [Path(r['auth_home'])/'auth.json',Path(r['da3_code'])/'checkpoints/DA3NESTED-GIANT-LARGE/model.safetensors',
                 Path(r['da3_code'])/'checkpoints/DA3NESTED-GIANT-LARGE/config.json',Path(r['sam_weights'])/'sam3.pt',
                 Path(r['torch_cache'])/'hub/checkpoints/alexnet-owt-7be5be79.pth',Path(r['reference_builder']),Path(r['sparse_evaluator'])]:
        if not path.is_file():raise FileNotFoundError(path)
    for image in set([r['geometry_image'],r['segmentation_image']]):
        info=json.loads(run(['docker','image','inspect',image]))[0]
        result[image]={'id':info['Id'],'architecture':info['Architecture']}
    mounts=['-v',r['da3_code']+':/opt/da3:ro','-v',r['sam_code']+':/workspace/projects/sam3:ro',
            '-v',r['evaluation_packages']+':/evaluation-packages:ro',
            '-e','PYTHONPATH=/evaluation-packages:/opt/da3/src:/workspace/projects/sam3']
    # Run both checks even when a shared image is used.
    for name,script in [('geometry','import torch,open3d,cv2,numpy,trimesh,lpips; from depth_anything_3.api import DepthAnything3; print(torch.__version__,open3d.__version__,torch.cuda.get_device_name())'),
                        ('segmentation','import torch; from sam3.model_builder import build_sam3_image_model; from sam3.model.sam3_image_processor import Sam3Processor; print(torch.__version__,torch.cuda.get_device_name())')]:
        result[name+'_imports']=run(['docker','run','--rm','--network','none','--gpus',r.get('docker_gpus','all'),*mounts,
                                     '--entrypoint','python',r[name+'_image'],'-c',script])
    result['status']='PASS';a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2)+'\n');print('PASS: preflight (imports only; run tool inference smoke before formal trials)')


if __name__=='__main__':main()
