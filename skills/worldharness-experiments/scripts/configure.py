"""Create one participant/model's experiment records; never launch paid work."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main():
    p = argparse.ArgumentParser()
    for name in ['repo','dataset','root','tools','codex','auth-home','blender','oidn']:
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--model', choices=['gpt-6-luna','gpt-6.1-sol','gpt-6-astra'], required=True)
    p.add_argument('--scenes', required=True, help='Comma-separated scene keys assigned to this participant')
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--geometry-image', default='worldharness-tools:20261110')
    p.add_argument('--segmentation-image', default='worldharness-tools:20261110')
    p.add_argument('--docker-gpus', default='all', help='Docker --gpus value, e.g. device=0')
    p.add_argument('--proxy', default='')
    a = p.parse_args()
    if a.workers < 1:
        p.error('--workers must be positive')
    root, repo, dataset, tools = [getattr(a,k).resolve() for k in ['root','repo','dataset','tools']]
    if root.exists():
        p.error('Experiment root must be new; existing trials are not overwritten')
    assets = Path(__file__).parents[1]/'assets'
    protocol = json.loads((assets/'protocol.json').read_text())
    effort = protocol['model_efforts'][a.model]
    available = {r['key']:r for r in json.loads((dataset/'manifest.json').read_text())['scenes']}
    keys = a.scenes.split(',')
    if len(set(keys)) != len(keys) or any(k not in protocol['scenes'] for k in keys):
        p.error('Unknown or repeated scene key')
    for k in keys:
        assert available[k]['scene_id'] == protocol['scenes'][k]
        assert (dataset/'scenes'/protocol['scenes'][k]/'transforms.json').is_file()
    for path in [a.codex,a.blender,a.oidn,a.auth_home/'auth.json',repo/'experiments/reconstruction/run.py']:
        if not path.is_file():
            p.error('Required file does not exist: '+str(path))
    protocol.update(id=root.name,models=[a.model],main_effort=effort,scenes={k:protocol['scenes'][k] for k in keys},
                    edit_targets={k:protocol['edit_targets'][k] for k in keys},
                    planner={'model':a.model,'effort':effort},reviewer={'model':a.model,'effort':effort},
                    code_revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip())
    protocol['suite_protocol_sha256'] = hashlib.sha256((assets/'protocol.json').read_bytes()).hexdigest()
    runtime = dict(codex=str(a.codex.resolve()),auth_home=str(a.auth_home.resolve()),
                   data_root=str(dataset/'scenes'), da3_code=str(tools/'Depth-Anything-3'),
                   sam_code=str(tools/'sam3'),sam_weights=str(tools/'weights/sam3'),
                   blender=str(a.blender.resolve()),oidn_library=str(a.oidn.resolve()),
                   reference_builder=str(repo/'experiments/reconstruction/build_reference.py'),
                   sparse_evaluator=str(repo/'experiments/reconstruction/evaluate_sparse.py'),
                   geometry_image=a.geometry_image,segmentation_image=a.segmentation_image,
                   evaluation_packages=str(tools/'evaluation-packages'),torch_cache=str(tools/'torch-cache'),
                   docker_gpus=a.docker_gpus,gpu_lock=str(tools/'locks/gpu.lock'),
                   proxy=a.proxy,tool_timeout_seconds=None)
    root.mkdir(parents=True)
    for name in ['evaluation-packages','torch-cache','locks']:
        (tools/name).mkdir(parents=True,exist_ok=True)
    records = {'protocol.json':protocol,'runtime.json':runtime,
               'scheduler.json':{'workers_per_model':a.workers,'before_gate_trials':[]}}
    for name,data in records.items():
        (root/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    shutil.copyfile(assets/'scenes.json',root/'scene_selection.json')
    print(json.dumps({'root':str(root),'model':a.model,'effort':effort,'trials':len(keys)*3,'modeling_checkpoints':len(keys)*9,'launched':False}))


if __name__ == '__main__':
    main()
