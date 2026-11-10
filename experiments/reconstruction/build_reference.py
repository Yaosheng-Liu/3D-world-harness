"""Independent, held-out-only three-view sparse references; never scan ground truth."""
from pathlib import Path
import json, time, argparse
import cv2
import numpy as np
p=argparse.ArgumentParser();p.add_argument('--scene-dir',type=Path,required=True);args=p.parse_args()
root=args.scene_dir;t=json.loads((root/'transforms.json').read_text());split=json.loads((root/'split.json').read_text())
frames={f['source_index']:f for f in t['frames']}
K=np.array([[t['fl_x'],0,t['cx']],[0,t['fl_y'],t['cy']],[0,0,1]],float);Ki=np.linalg.inv(K)
cv2.setNumThreads(4);cv2.setRNGSeed(20261010);np.random.seed(20261010)
sift=cv2.SIFT_create(nfeatures=10000,contrastThreshold=.025);bf=cv2.BFMatcher();cache={};start=time.time()
def features(i):
 if i not in cache:
  f=frames[i];im=cv2.imread(str(root/f['file_path']),0);kp,desc=sift.detectAndCompute(im,None)
  C=np.asarray(f['transform_matrix'])@np.diag([1,-1,-1,1]);E=np.linalg.inv(C)
  cache[i]=(np.array([k.pt for k in kp]),desc,C,E,K@E[:3])
 return cache[i]
def match(i,j):
 a,da,*_=features(i);b,db,*_=features(j)
 if da is None or db is None:return {}
 forward={m.queryIdx:m.trainIdx for m,n in bf.knnMatch(da,db,k=2) if m.distance<.72*n.distance}
 reverse={m.queryIdx:m.trainIdx for m,n in bf.knnMatch(db,da,k=2) if m.distance<.72*n.distance}
 return {k:v for k,v in forward.items() if reverse.get(v)==k}
allrows=[];counts=[]
for center in split['test_centers']:
 used=set();accepted=[];candidates=0
 for gap in (6,3):
  ids=[center-gap,center,center+gap];fa,fb,fc=[features(i) for i in ids]
  ml=match(center,ids[0]);mr=match(center,ids[2]);keys=sorted((set(ml)&set(mr))-used)
  if not keys:continue
  uv=[fa[0][[ml[k] for k in keys]],fb[0][keys],fc[0][[mr[k] for k in keys]]]
  aa=[]
  for coords,ft in zip(uv,(fa,fb,fc)):
   P=ft[-1];aa.extend([coords[:,0,None]*P[2]-P[0],coords[:,1,None]*P[2]-P[1]])
  A=np.stack(aa,axis=1);_,sv,V=np.linalg.svd(A);hom=V[:,-1];X=hom[:,:3]/hom[:,3,None]
  reproj=[];Z=[]
  for coords,ft in zip(uv,(fa,fb,fc)):
   q=X@ft[3][:3,:3].T+ft[3][:3,3];Z.append(q[:,2]);pix=q@K.T;pix=pix[:,:2]/pix[:,2,None];reproj.append(np.linalg.norm(pix-coords,axis=1))
  rays1=X-fa[2][:3,3];rays3=X-fc[2][:3,3]
  cosine=np.sum(rays1*rays3,axis=1)/(np.linalg.norm(rays1,axis=1)*np.linalg.norm(rays3,axis=1));angle=np.degrees(np.arccos(np.clip(cosine,-1,1)))
  error=np.max(reproj,axis=0)
  valid=np.isfinite(X).all(axis=1)&(np.min(Z,axis=0)>0)&(error<=1.25)&(angle>=.75)&(angle<70)
  candidates+=len(keys)
  for j in np.flatnonzero(valid):
   used.add(keys[j]);origin=fb[2][:3,3];direction=X[j]-origin;depth=float(np.linalg.norm(direction));direction/=depth
   accepted.append({'point':X[j],'origin':origin,'direction':direction,'depth':depth,'pixel':uv[1][j],'error':error[j],'angle':angle[j],'gap':gap,'frame':center})
 # Pre-fixed image-grid cap avoids a small densely textured sign dominating a view.
 cells={};retained=[]
 for row in sorted(accepted,key=lambda r:r['error']):
  key=tuple((row['pixel']//48).astype(int));cells[key]=cells.get(key,0)+1
  if cells[key]<=12:retained.append(row)
 allrows.extend(retained);counts.append({'frame':center,'three_view_candidates':candidates,'accepted_before_grid_cap':len(accepted),'retained':len(retained),'occupied_48px_cells':len(cells),'median_reprojection_px':float(np.median([r['error'] for r in retained])) if retained else None})
 print(json.dumps(counts[-1]),flush=True)
eligible={x['frame'] for x in counts if x['retained']>=20}
allrows=[x for x in allrows if x['frame'] in eligible]
if not allrows:raise RuntimeError('No valid independent reference points')
fields={'points':'point','origins':'origin','directions':'direction','depths':'depth','frame_indices':'frame','pixels':'pixel','reprojection_errors':'error','parallax_degrees':'angle','support_gap':'gap'}
np.savez_compressed(root/'reference.npz',**{key:np.asarray([r[val] for r in allrows]) for key,val in fields.items()})
report={'scene_id':split['scene_id'],'reference_type':'heldout-only calibrated 3-view SIFT triangulation, NOT scan ground truth','points':len(allrows),'frames':counts,'scored_frames':sorted(eligible),'excluded_low_support_frames':[x['frame'] for x in counts if x['frame'] not in eligible],'thresholds':{'mutual_lowe_ratio':.72,'max_reprojection_px':1.25,'min_parallax_degrees':.75,'grid_size_px':48,'max_points_per_cell':12,'min_reference_points_per_scored_frame':20},'elapsed_seconds':time.time()-start,'limitations':['Texture-biased and sparse; weak texture/glass/hidden surfaces not covered.','Correspondence errors and dataset camera errors remain possible.','No per-method registration or scale fitting.','All support frames excluded from both training sets and DA3.']}
(root/'reference_report.json').write_text(json.dumps(report,indent=2)+'\n');print('COMPLETE',len(allrows),flush=True)
