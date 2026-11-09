"""Evaluate frozen checkpoints outside model packets and export flat results."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
import time

from PIL import Image

from common import REPO, checkpoint_calls, command, digest, docker, read, usage_summary, write


def references(root, scene, protocol):
    private=root/"evaluation_private"/scene
    destination=private/"render_references"
    destination.mkdir(exist_ok=True)
    cameras=read(private/"transforms.json")
    frames={x['source_index']:x for x in cameras['frames']}
    views=[]
    for index in read(private/'split.json')['test_centers']:
        frame=frames[index]
        image=Image.open(private/frame['file_path']).convert('RGB')
        width=protocol['test_render_width']
        height=round(width*image.height/image.width)
        sx,sy=width/image.width,height/image.height
        image.resize((width,height),Image.Resampling.LANCZOS).save(destination/f'test_{index}.png')
        views.append({'id':f'test_{index}','camera_to_world':frame['transform_matrix'],
                      'intrinsics':{'width':width,'height':height,'fx':cameras['fl_x']*sx,
                      'fy':cameras['fl_y']*sy,'cx':cameras['cx']*sx,'cy':cameras['cy']*sy},
                      'reference_image':str(destination/f'test_{index}.png')})
    return destination,views


def evaluate(root, trial, checkpoint, runtime, protocol):
    frozen=trial/'checkpoints'/checkpoint
    output=root/'evaluation_results'/trial.name/checkpoint
    if (output/'complete.json').exists():
        return read(output/'complete.json')
    output.mkdir(parents=True,exist_ok=True)
    started=time.time()
    manifest=read(frozen/'frozen.json')
    for name,sha in manifest['files'].items():
        if digest(frozen/name)!=sha:
            raise ValueError('Frozen checkpoint changed: '+str(frozen/name))
    spec=read(trial/'spec.json')
    reference_dir,views=references(root,spec['scene'],protocol)
    targets=read(frozen/'edit_targets.json')
    expected=protocol['edit_targets'][spec['scene']]
    if set(targets)!=set(expected):
        raise ValueError('Editing target declaration does not cover the registered tasks')
    job={'blend':str(frozen/'scene.blend'),'output':str(output),'views':views,
         'samples':protocol['test_render_samples'],'edit_targets':targets,'oidn_library':runtime['oidn_library']}
    write(output/'job.json',job)
    command([runtime['blender'],'--background','--factory-startup','--python-exit-code','1',
             '--python',str(Path(__file__).with_name('evaluate_blender.py')),'--',str(output/'job.json')],
             output/'blender.log',timeout=5400)
    private=root/'evaluation_private'/spec['scene']
    mounts=[(REPO,'ro'),(output,'rw'),(private,'ro'),
            (Path(runtime['sparse_evaluator']).parent,'ro')]
    command(docker(runtime,mounts,runtime['sparse_evaluator'],
                    ['--reference',private/'reference.npz','--mesh',output/'geometry.ply',
                     '--output',output/'geometry_metrics.json']),output/'geometry.log')
    argv=docker(runtime,mounts,Path(__file__).with_name('image_metrics.py'),
                ['--references',reference_dir,'--renders',output/'renders','--output',output/'image_metrics.json'])
    # lpips itself is a lightweight package; the cached AlexNet weights are read-only.
    position=argv.index('--entrypoint')
    argv[position:position]=['-v',f"{runtime['evaluation_packages']}:/evaluation-packages:ro",
                             '-v',f"{runtime['torch_cache']}:/torch-cache:ro",'-e',
                             'PYTHONPATH=/evaluation-packages','-e','TORCH_HOME=/torch-cache']
    command(argv,output/'image.log')
    command(docker(runtime,mounts,Path(__file__).with_name('target_panels.py'),
                   ['--job',output/'job.json','--references',reference_dir]),output/'target_panels.log')
    blender=read(output/'blender_metrics.json')
    edits=blender['edit_tasks']
    result={'trial':trial.name,'checkpoint':checkpoint,'spec':spec,
            'image':read(output/'image_metrics.json'),'geometry':read(output/'geometry_metrics.json'),
            'edit_mechanical_success_rate':sum(x['passed'] for x in edits)/len(edits),
            'edit_unintended_change_rate':sum(bool(x.get('unintended_meshes')) for x in edits)/len(edits),
            'edit_semantic_verification':'pending_independent_visual_check',
            'objects':len(read(frozen/'scene.json')['objects']),'triangles':blender['triangles'],
            'seconds':time.time()-started,'scene_hash':manifest['scene_hash'],
            'reference_sha256':digest(private/'reference.npz')}
    write(output/'complete.json',result)
    return result


def collect(root):
    rows=[]
    for trial in sorted((root/'runs').glob('*')):
        if not (trial/'spec.json').exists():
            continue
        spec=read(trial/'spec.json')
        recovery=trial/'validation_recovery.json'
        submission_policy=read(recovery)['policy'] if recovery.exists() else 'original'
        for number in range(3):
            metric=root/'evaluation_results'/trial.name/f'M{number}/complete.json'
            if not metric.exists():
                continue
            result=read(metric)
            records=checkpoint_calls(trial,number)
            totals=usage_summary(records)
            row={**spec,'trial':trial.name,'checkpoint':f'M{number}',**result['image']['mean'],
                 'edit_mechanical_success_rate':result['edit_mechanical_success_rate'],
                 'edit_unintended_change_rate':result['edit_unintended_change_rate'],
                 'objects':result['objects'],'triangles':result['triangles'],**totals,
                 'agent_seconds':sum(r['seconds'] for r in records),
                 'initial_origin':spec.get('initial_origin','fresh'),
                 'submission_policy':submission_policy,
                 'edit_semantic_verification':result['edit_semantic_verification']}
            geometry=result['geometry']
            row.update(geometry['macro_average_metrics'])
            row['geometry_metrics_path']=str(metric.parent/'geometry_metrics.json')
            rows.append(row)
    write(root/'results.json',rows)
    if rows:
        with (root/'results.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return rows


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    protocol,runtime=read(args.root/'protocol.json'),read(args.runtime)
    while True:
        errors=[]
        for trial in sorted((args.root/'runs').glob('*')):
            for frozen in sorted((trial/'checkpoints').glob('M*')):
                if not (frozen/'frozen.json').exists():continue
                destination=args.root/'evaluation_results'/trial.name/frozen.name
                if (destination/'error.json').exists():continue
                try:evaluate(args.root,trial,frozen.name,runtime,protocol)
                except Exception as error:
                    record={'trial':trial.name,'checkpoint':frozen.name,'error':str(error)}
                    errors.append(record)
                    write(destination/'error.json',record)
                    write(args.root/'evaluation_errors.json',errors)
        collect(args.root)
        if not args.watch:break
        time.sleep(20)


if __name__=='__main__':
    main()
