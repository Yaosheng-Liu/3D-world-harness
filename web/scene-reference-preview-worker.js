import {renderSceneReferencePreview} from './scene-reference-preview-renderer.js';

// Requests stay serial so large scenes cannot multiply GPU memory or interrupt
// another snapshot's camera. Fetch, parsing, rendering and encoding stay here.
let queue=Promise.resolve();
self.onmessage=({data})=>{
  queue=queue.catch(()=>{}).then(async()=>{
    try {
      if(typeof OffscreenCanvas!=='function') throw Error('此浏览器未提供后台场景预览。');
      const preview=await renderSceneReferencePreview(data.record,{
        canvas:new OffscreenCanvas(640,400),
        resourceURL:url=>{
          const address=data.resources[url];
          if(typeof address!=='string' || new URL(address).origin!==self.location.origin) throw Error('参考场景的模型地址无效。');
          return address;
        }
      });
      self.postMessage({id:data.id,preview});
    } catch(error) {self.postMessage({id:data.id,error:error?.message || '后台场景预览生成失败。'});}
  });
};
