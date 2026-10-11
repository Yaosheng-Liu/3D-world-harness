// Immutable other-scene snapshots are distinct from the current reconstruction.
const ID=/^[0-9a-f]{32}$/;
const IMAGE=/^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/]+=*$/;
export const MAX_SCENE_REFERENCES=4;
export function sceneToken(id) {return ID.test(id || '')?`[[scene:${id}]]`:null;}
export function sceneReferenceLabel(record) {return `${record?.name || '参考场景'} · 版本 ${record?.source_scene_revision ?? '—'} · 只读参考`;}
export function validSceneReference(entry) {
  const record=entry?.reference;
  if(!entry || !sceneToken(entry.id) || !record || record.id!==entry.id || record.read_only!==true ||
    !ID.test(record.source_project_id || '') || !Number.isInteger(record.source_scene_revision) ||
    typeof record.name!=='string' || !record.scene || !Array.isArray(record.scene.objects)) return false;
  if(entry.preview_data_url) {
    const camera=entry.preview_camera;
    return IMAGE.test(entry.preview_data_url) && camera && ['position','target','up'].every(key=>
      Array.isArray(camera[key]) && camera[key].length===3 && camera[key].every(Number.isFinite)) &&
      Number.isFinite(camera.fov) && camera.fov>0 && camera.fov<180 && Number.isFinite(camera.aspect) && camera.aspect>0 &&
      Number.isInteger(entry.width) && entry.width>0 && Number.isInteger(entry.height) && entry.height>0 &&
      Math.abs(camera.aspect-entry.width/entry.height)<.02 && Number.isFinite(entry.time_sec) && entry.time_sec>=0;
  }
  return typeof record.source_reference_image?.url==='string' && !entry.preview_camera;
}
export function collectSceneReferences(note,references) {
  const matches=[...String(note).matchAll(/\[\[scene:([0-9a-f]{32})\]\]/g)],starts=new Set(matches.map(match=>match.index));
  for(const match of String(note).matchAll(/\[\[scene:/g)) if(!starts.has(match.index)) throw Error('场景引用不完整，请从左侧场景列表重新引用。');
  const ids=[...new Set(matches.map(match=>match[1]))];
  if(ids.length>MAX_SCENE_REFERENCES) throw Error(`一条提示最多引用 ${MAX_SCENE_REFERENCES} 个参考场景。`);
  return ids.map(id=> {
    const found=references.filter(entry=>entry.id===id);
    if(found.length!==1 || !validSceneReference(found[0])) throw Error('参考场景的固定快照无法恢复，请移除这处引用后重新引用。');
    const entry=found[0];
    return {id,...(entry.preview_data_url?{preview_data_url:entry.preview_data_url,preview_camera:structuredClone(entry.preview_camera),time_sec:entry.time_sec}:{})};
  });
}
export function createSceneReferenceStore(openDatabase) {
  let pending=Promise.resolve();
  async function read(key) {
    const db=await openDatabase();
    try {return await new Promise((resolve,reject)=>{
      const request=db.transaction('drafts').objectStore('drafts').get(key);
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });} finally {db.close();}
  }
  return {
    get pending(){return pending;},
    save(sessionId,references) {
      const captured=structuredClone(references);
      pending=pending.catch(()=>{}).then(async()=>{
        const db=await openDatabase();
        try {await new Promise((resolve,reject)=>{
          const tx=db.transaction('drafts','readwrite');tx.objectStore('drafts').put(captured,'prompt-scenes:'+sessionId);
          tx.oncomplete=resolve;tx.onerror=tx.onabort=()=>reject(tx.error || Error('场景引用草稿保存失败'));
        });} finally {db.close();}
      });return pending;
    },
    async load(sessionId) {
      const entries=await read('prompt-scenes:'+sessionId);
      return Array.isArray(entries)?entries.filter(validSceneReference).slice(0,16):[];
    },
    async loadPreview(sessionId,id) {
      await pending.catch(()=>{});
      const entries=await read('scene-previews:'+sessionId);
      return Array.isArray(entries)?entries.find(entry=>entry?.id===id) || null:null;
    },
    savePreview(sessionId,entry) {
      if(!entry.preview_data_url) return Promise.resolve();
      // Cache only the small rendered evidence. Source metadata always comes
      // from the server's canonical immutable snapshot on the next citation.
      const captured=structuredClone(Object.fromEntries(['id','preview_data_url','preview_camera','width','height','time_sec'].map(key=>[key,entry[key]])));
      pending=pending.catch(()=>{}).then(async()=>{
        const db=await openDatabase();
        try {await new Promise((resolve,reject)=>{
          const tx=db.transaction('drafts','readwrite'),store=tx.objectStore('drafts');
          const key='scene-previews:'+sessionId,request=store.get(key);
          request.onsuccess=()=>{
            const entries=Array.isArray(request.result)?request.result.filter(item=>item?.id!==captured.id):[];
            store.put([...entries.slice(-15),captured],key);
          };
          tx.oncomplete=resolve;tx.onerror=tx.onabort=()=>reject(tx.error || Error('场景预览缓存保存失败'));
        });} finally {db.close();}
      });return pending;
    }
  };
}
