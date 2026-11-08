"""PSNR, SSIM and AlexNet LPIPS at one fixed test resolution."""
import argparse
import json
from pathlib import Path

import lpips
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity
import torch


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--references',type=Path,required=True)
    parser.add_argument('--renders',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    torch.set_num_threads(4)
    model=lpips.LPIPS(net='alex').eval()
    rows=[]
    for path in sorted(args.references.glob('*.png')):
        reference=Image.open(path)
        if reference.mode=='RGBA' and reference.getchannel('A').getextrema()!=(255,255):
            raise ValueError('Evaluation reference must be opaque')
        a=np.asarray(reference.convert('RGB'),dtype=np.float32)/255
        rendered=Image.open(args.renders/path.name).convert('RGBA')
        white=Image.new('RGBA',rendered.size,'white')
        b=np.asarray(Image.alpha_composite(white,rendered).convert('RGB'),dtype=np.float32)/255
        if a.shape!=b.shape:
            raise ValueError('Fixed test image dimensions differ')
        mse=float(np.mean((a-b)**2))
        ta=torch.from_numpy(a).permute(2,0,1)[None]*2-1
        tb=torch.from_numpy(b).permute(2,0,1)[None]*2-1
        with torch.inference_mode():
            perceptual=float(model(ta,tb).item())
        rows.append({'view':path.stem,'psnr_db':-10*np.log10(mse) if mse else None,
                     'ssim':float(structural_similarity(a,b,data_range=1,channel_axis=2)),
                     'lpips_alex':perceptual})
    result={'views':rows,'mean':{key:float(np.mean([r[key] for r in rows]))
            if all(r[key] is not None for r in rows) else None for key in ('psnr_db','ssim','lpips_alex')},
            'perfect_psnr_views':sum(r['psnr_db'] is None for r in rows),
            'domain':'RGB [0,1], no exposure/color fitting; alpha composited on white; fixed resized references',
            'lpips_version':getattr(lpips,'__version__',None)}
    args.output.write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    main()
