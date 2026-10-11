// Model parsing, precise bounds, GPU setup and JPEG encoding run off the page
// thread. Only a small scene description crosses to the worker; GLB bytes are
// fetched and decoded there, never cloned through the composer.
const ASSET=/^(?:\/p\/[0-9a-f]{32})?\/assets\/[0-9a-f]{32}\.glb$/;
let worker=null,sequence=0;
const pending=new Map();
function stopWorker(message) {
  const old=worker;worker=null;old?.terminate();
  for(const task of pending.values()){clearTimeout(task.timer);task.reject(Error(message));}
  pending.clear();
}
function previewWorker() {
  if(worker) return worker;
  const active=new Worker(new URL('./scene-reference-preview-worker.js',import.meta.url),{type:'module',name:'scene-reference-preview'});
  worker=active;
  active.onmessage=({data})=>{
    if(worker!==active) return;
    const task=pending.get(data?.id);if(!task) return;
    clearTimeout(task.timer);pending.delete(data.id);
    if(data.error) task.reject(Error(data.error));else task.resolve(data.preview);
  };
  active.onerror=event=>{event.preventDefault();if(worker===active)stopWorker('后台场景预览暂不可用，请重试引用。');};
  active.onmessageerror=()=>{if(worker===active)stopWorker('后台场景预览返回无效，请重试引用。');};
  return active;
}
export async function renderSceneReferencePreview(record,{resourceURL=(url)=>url}={}) {
  const objects=record?.scene?.objects;
  if(!Array.isArray(objects) || objects.length>500) throw Error('参考场景的模型列表无效。');
  const sourceTime=record.source_reference_image?.time_sec;
  const time=Number.isFinite(sourceTime) && sourceTime>=0?sourceTime:0;
  if(!objects.length) return {data_url:null,camera:null,time_sec:time,width:0,height:0};
  if(typeof Worker!=='function' || typeof OffscreenCanvas!=='function') throw Error('此浏览器未提供后台场景预览。');
  const resources={};
  for(const item of objects) if(item.type==='model') {
    if(typeof item.url!=='string' || !ASSET.test(item.url)) throw Error('参考场景的模型地址无效。');
    const address=new URL(resourceURL(item.url),location.href);
    if(address.origin!==location.origin || !ASSET.test(address.pathname) || address.search || address.hash) throw Error('参考场景的模型地址无效。');
    resources[item.url]=address.href;
  }
  const active=previewWorker(),id=++sequence;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{if(worker===active)stopWorker('后台场景预览超时，请重试引用。');},35000);
    pending.set(id,{resolve,reject,timer});
    try {active.postMessage({id,record:{scene:record.scene,source_reference_image:{time_sec:time}},resources});}
    catch {clearTimeout(timer);pending.delete(id);reject(Error('无法准备后台场景预览，请重试引用。'));}
  });
}
globalThis.addEventListener?.('pagehide',()=>stopWorker('工作台已离开，场景预览已取消。'));
