import * as THREE from './vendor/three/build/three.module.min.js';
import {GLTFLoader} from './vendor/three/examples/jsm/loaders/GLTFLoader.js';
import {clone as cloneSkeleton} from './vendor/three/examples/jsm/utils/SkeletonUtils.js';

const ASSET=/^(?:\/p\/[0-9a-f]{32})?\/assets\/[0-9a-f]{32}\.glb$/;
const vector=(value,fallback)=>Array.isArray(value) && value.length===3 && value.every(Number.isFinite)?value:fallback;

// This renderer owns its resources. Capturing another scene must never reparent
// an object or change the camera, animation or selection in the live viewport.
export async function renderSceneReferencePreview(record,{resourceURL=(url)=>url,canvas=null}={}) {
  const objects=record?.scene?.objects;
  if(!Array.isArray(objects) || objects.length>500) throw Error('参考场景的模型列表无效。');
  const sourceTime=record.source_reference_image?.time_sec;
  const time=Number.isFinite(sourceTime) && sourceTime>=0?sourceTime:0;
  if(!objects.length) return {data_url:null,camera:null,time_sec:time,width:0,height:0};
  const width=640,height=400;
  const scene=new THREE.Scene();
  scene.background=new THREE.Color('#eae9e3');
  const layer=new THREE.Group(); scene.add(layer);
  const manager=new THREE.LoadingManager();
  const loader=new GLTFLoader(manager);
  const loaded=new Map(),mixers=[],upAxes=new Set();
  let renderer,expired=false;
  const timeout=setTimeout(()=>{expired=true;manager.abort?.();},30000);
  try {
    const urls=[...new Set(objects.filter(item=>item.type==='model').map(item=>item.url))];
    if(urls.some(url=>typeof url!=='string' || !ASSET.test(url))) throw Error('参考场景的模型地址无效。');
    const results=await Promise.allSettled(urls.map(async url=>{
      const gltf=await loader.loadAsync(resourceURL(url));
      loaded.set(url,gltf);return gltf;
    }));
    const failure=results.find(result=>result.status==='rejected');
    if(expired || failure) throw Error(expired?'参考场景预览超时，请重试。':'参考场景模型加载失败。');
    for(const item of objects) {
      const root=new THREE.Group();
      root.position.fromArray(vector(item.position,[0,0,0]));
      root.rotation.set(...vector(item.rotation,[0,0,0]));
      const size=vector(item.size,[1,1,1]);
      if(size.some(value=>value<=0)) throw Error('参考场景的模型尺寸无效。');
      layer.add(root);
      if(item.type==='model') {
        const gltf=loaded.get(item.url),model=cloneSkeleton(gltf.scene);
        root.scale.fromArray(size);root.add(model);
        const asset=gltf.parser.json.asset || {};
        const declared=String(item.metadata?.up_axis || gltf.scene.userData.up_axis || asset.extras?.up_axis || '').toLowerCase();
        upAxes.add(['y','z'].includes(declared)?declared:asset.generator==='scene-feedback-harness room demo'?'z':'y');
        if(gltf.animations?.length) {
          const clip=gltf.animations[0],mixer=new THREE.AnimationMixer(model),action=mixer.clipAction(clip);
          action.setLoop(THREE.LoopOnce,1);action.clampWhenFinished=true;action.play();
          mixer.setTime(Math.min(time,clip.duration));mixers.push({mixer,root:model});
        }
      } else {
        let geometry;
        if(item.type==='sphere') geometry=new THREE.SphereGeometry(.5,32,20);
        else if(item.type==='cylinder') {geometry=new THREE.CylinderGeometry(.5,.5,1,32);geometry.rotateX(Math.PI/2);}
        else if(item.type==='box') geometry=new THREE.BoxGeometry(1,1,1);
        else throw Error('参考场景包含不支持的模型类型。');
        const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:item.color || '#9aaeb8',roughness:.66,metalness:.08}));
        mesh.scale.fromArray(size);root.add(mesh);
      }
    }
    layer.updateMatrixWorld(true);
    layer.traverse(node=>{if(node.isSkinnedMesh){node.skeleton.update();node.computeBoundingBox();node.computeBoundingSphere();}});
    const bounds=new THREE.Box3().setFromObject(layer,true);
    if(bounds.isEmpty() || ![...bounds.min.toArray(),...bounds.max.toArray()].every(Number.isFinite)) throw Error('参考场景没有可预览的模型。');
    const center=bounds.getCenter(new THREE.Vector3()),radius=Math.max(bounds.getSize(new THREE.Vector3()).length()/2,.05);
    const upAxis=upAxes.size===1?[...upAxes][0]:'z';
    const remap=(x,y,z)=>upAxis==='y'?new THREE.Vector3(x,z,-y):new THREE.Vector3(x,y,z);
    const hemisphere=new THREE.HemisphereLight(0xdcefff,0x7e8a93,2.3);
    hemisphere.position.copy(remap(0,0,1));scene.add(hemisphere);
    const key=new THREE.DirectionalLight(0xffedda,3.5);key.position.copy(center).add(remap(5,-4,10).multiplyScalar(radius));key.target.position.copy(center);scene.add(key,key.target);
    const fill=new THREE.DirectionalLight(0x9bc6ea,1.8);fill.position.copy(center).add(remap(-5,6,5).multiplyScalar(radius));fill.target.position.copy(center);scene.add(fill,fill.target);
    const camera=new THREE.PerspectiveCamera(44,width/height,Math.max(radius/1000,.0001),radius*20);
    camera.up.copy(remap(0,0,1));
    const distance=radius/Math.sin(THREE.MathUtils.degToRad(camera.fov/2))*1.15;
    camera.position.copy(center).add(remap(1,-1,.75).normalize().multiplyScalar(distance));camera.lookAt(center);camera.updateMatrixWorld(true);
    renderer=new THREE.WebGLRenderer({canvas,antialias:true,preserveDrawingBuffer:true});
    renderer.setPixelRatio(1);renderer.setSize(width,height,false);
    renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.65;
    renderer.render(scene,camera);
    const blob=await renderer.domElement.convertToBlob({type:'image/jpeg',quality:.86});
    const bytes=new Uint8Array(await blob.arrayBuffer());
    let binary='';for(let offset=0;offset<bytes.length;offset+=32768) binary+=String.fromCharCode(...bytes.subarray(offset,offset+32768));
    return {data_url:'data:'+blob.type+';base64,'+btoa(binary),camera:{position:camera.position.toArray(),target:center.toArray(),up:camera.up.toArray(),fov:camera.fov,aspect:camera.aspect,near:camera.near,far:camera.far},time_sec:time,width,height};
  } finally {
    clearTimeout(timeout);
    for(const {mixer,root} of mixers){mixer.stopAllAction();mixer.uncacheRoot(root);}
    const geometries=new Set(),materials=new Set(),textures=new Set(),images=new Set(),skeletons=new Set();
    const collect=root=>root.traverse(node=>{
      if(node.geometry) geometries.add(node.geometry);
      if(node.skeleton) skeletons.add(node.skeleton);
      for(const material of Array.isArray(node.material)?node.material:node.material?[node.material]:[]) materials.add(material);
    });
    collect(scene);for(const gltf of loaded.values()) for(const root of gltf.scenes || [gltf.scene]) collect(root);
    for(const material of materials) for(const value of Object.values(material)) if(value?.isTexture) textures.add(value);
    for(const skeleton of skeletons){if(skeleton.boneTexture)textures.add(skeleton.boneTexture);skeleton.dispose();}
    for(const texture of textures){const data=texture.source?.data;for(const image of Array.isArray(data)?data:[data]) if(image?.close) images.add(image);texture.dispose();}
    for(const geometry of geometries) geometry.dispose();for(const material of materials) material.dispose();for(const image of images) image.close();
    if(renderer){renderer.dispose();renderer.forceContextLoss();}
  }
}
