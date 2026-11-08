"""Show declared target ownership using first-hit geometry masks for visual audit."""
import argparse
import json
from pathlib import Path

import numpy as np
import open3d as o3d
from PIL import Image,ImageDraw


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--job',type=Path,required=True)
    parser.add_argument('--references',type=Path,required=True)
    args=parser.parse_args()
    job=json.loads(args.job.read_text());output=Path(job['output'])
    data=np.load(output/'geometry_owners.npz',allow_pickle=False)
    owners,identifiers=data['owners'],data['identifiers'].tolist()
    mesh=o3d.io.read_triangle_mesh(str(output/'geometry.ply'))
    if len(mesh.triangles)!=len(owners):raise ValueError('Geometry owner indices do not match')
    scene=o3d.t.geometry.RaycastingScene();scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    visible=[]
    for view in job['views']:
        k=view['intrinsics'];w,h=k['width'],k['height']
        yy,xx=np.mgrid[:h,:w]
        directions=np.stack(((xx+.5-k['cx'])/k['fx'],(yy+.5-k['cy'])/k['fy'],np.ones((h,w))),axis=-1)
        pose=np.asarray(view['camera_to_world'])@np.diag([1,-1,-1,1])
        directions=directions@pose[:3,:3].T
        origins=np.broadcast_to(pose[:3,3],directions.shape)
        rays=np.concatenate((origins,directions),axis=-1).astype(np.float32)
        hits=scene.cast_rays(o3d.core.Tensor(rays))['primitive_ids'].numpy()
        labels=np.full((h,w),-1,dtype=np.int32)
        valid=hits<len(owners);labels[valid]=owners[hits[valid]]
        visible.append((view,labels))
    width,height=job['views'][0]['intrinsics']['width'],job['views'][0]['intrinsics']['height']
    panel=Image.new('RGB',(width*3,(height+28)*len(job['edit_targets'])),'white')
    draw=ImageDraw.Draw(panel);records=[]
    for row,(target,identifier) in enumerate(job['edit_targets'].items()):
        index=identifiers.index(identifier) if identifier in identifiers else -2
        view,labels=max(visible,key=lambda x:int((x[1]==index).sum()))
        mask=labels==index;pixels=int(mask.sum())
        source=Image.open(args.references/(view['id']+'.png')).convert('RGB')
        render=Image.open(output/'renders'/(view['id']+'.png')).convert('RGBA')
        render=Image.alpha_composite(Image.new('RGBA',render.size,'white'),render).convert('RGB')
        rgb=np.asarray(render).copy();rgb[mask]=(rgb[mask]*.4+np.asarray([255,40,20])*.6).astype(np.uint8)
        for col,(im,label) in enumerate(((source,'source'),(render,'render'),(Image.fromarray(rgb),'selected geometry'))):
            y=row*(height+28);draw.text((col*width+4,y+6),f'{target} / {identifier} / {view["id"]} / {label}',fill='black')
            panel.paste(im,(col*width,y+28))
        records.append({'target':target,'object_id':identifier,'view_id':view['id'],'visible_pixels':pixels,
                        'semantic_verdict':'pending','mask_scope':'Direct visual owner; excludes descendants'})
    panel.save(output/'target_ownership.jpg',quality=94)
    (output/'target_ownership.json').write_text(json.dumps(records,indent=2)+'\n')


if __name__=='__main__':main()
