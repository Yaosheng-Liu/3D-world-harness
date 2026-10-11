import * as THREE from 'three';

// Render the selected silhouette with the actual geometry, including skinning,
// morph targets, cutouts and occlusion. No geometry or source material is edited.
export class SelectionOutline {
  constructor(renderer, {color='#e4443b', width=2}={}) {
    this.renderer=renderer;
    this.root=null;
    this.width=width;
    this.size=new THREE.Vector2();
    this.materials=new Map();
    this.mask=new THREE.WebGLRenderTarget(1,1,{
      minFilter:THREE.LinearFilter, magFilter:THREE.LinearFilter,
      depthBuffer:true, stencilBuffer:false, samples:4
    });
    this.overlay=new THREE.Scene();
    this.overlayCamera=new THREE.Camera();
    this.stroke=new THREE.ShaderMaterial({
      uniforms:{mask:{value:this.mask.texture}, step:{value:new THREE.Vector2()},
        color:{value:new THREE.Color(color)}},
      vertexShader:`varying vec2 uvMask;
        void main() { uvMask=position.xy*0.5+0.5; gl_Position=vec4(position.xy,0.0,1.0); }`,
      fragmentShader:`uniform sampler2D mask;
        uniform vec2 step;
        uniform vec3 color;
        varying vec2 uvMask;
        void main() {
          float center=texture2D(mask,uvMask).r;
          float edge=0.0;
          for(int x=-1;x<=1;x++) for(int y=-1;y<=1;y++) {
            vec2 direction=vec2(float(x),float(y));
            if(x!=0 || y!=0) direction=normalize(direction);
            edge=max(edge,texture2D(mask,uvMask+direction*step).r-center);
          }
          float alpha=smoothstep(0.04,0.6,edge)*0.95;
          if(alpha<=0.0) discard;
          gl_FragColor=vec4(color,alpha);
          #include <colorspace_fragment>
        }`,
      transparent:true, depthTest:false, depthWrite:false, toneMapped:false
    });
    const geometry=new THREE.BufferGeometry();
    geometry.setAttribute('position',new THREE.Float32BufferAttribute([-1,-1,0,3,-1,0,-1,3,0],3));
    const quad=new THREE.Mesh(geometry,this.stroke);
    quad.frustumCulled=false;
    this.overlay.add(quad);
  }

  setSelection(root) {
    if(root===this.root) return;
    this.root=root || null;
    if(!this.root) this.clearMaterials();
  }

  clearMaterials() {
    for(const pair of this.materials.values()) for(const entry of pair.values()) entry.material.dispose();
    this.materials.clear();
  }

  maskMaterial(source, selected) {
    let pair=this.materials.get(source);
    if(!pair) {pair=new Map();this.materials.set(source,pair);}
    let entry=pair.get(selected);
    // Three increments version while drawing both sides of transparent surfaces
    // (including our ground plane). Invalidate on shader inputs, not that counter.
    const shaderKey=JSON.stringify([
      source.customProgramCacheKey(),source.defines,source.clippingPlanes?.length || 0,
      ...['side','transparent','alphaHash','alphaToCoverage','vertexColors','flatShading',
        'premultipliedAlpha','wireframe','precision'].map(key=>source[key]),source.alphaTest>0,
      Object.entries(source).filter(([,value])=>value?.isTexture).map(([key,texture])=>[key,texture.channel,texture.mapping])
    ]);
    if(!entry || entry.shaderKey!==shaderKey || entry.onBeforeCompile!==source.onBeforeCompile) {
      entry?.material.dispose();
      const material=source.clone();
      material.toneMapped=false;
      material.fog=false;
      material.onBeforeCompile=(shader,renderer)=>{
        source.onBeforeCompile(shader,renderer);
        shader.fragmentShader=shader.fragmentShader.replace('#include <opaque_fragment>',
          '#include <opaque_fragment>\ngl_FragColor.rgb=vec3('+ (selected?'1.0':'0.0') +');');
      };
      material.customProgramCacheKey=()=>source.customProgramCacheKey()+'|selection-mask-'+Number(selected);
      entry={material,shaderKey,onBeforeCompile:source.onBeforeCompile};pair.set(selected,entry);
    }
    // Values can animate without changing the material's shader version.
    for(const key of ['opacity','alphaTest','visible','side','depthTest','depthWrite','colorWrite',
      'blending','blendSrc','blendDst','blendEquation','blendSrcAlpha','blendDstAlpha','blendEquationAlpha',
      'clippingPlanes','clipIntersection','clipShadows','map','alphaMap']) {
      if(key in source) entry.material[key]=source[key];
    }
    return entry.material;
  }

  render(scene,camera,{enabled=true}={}) {
    const renderer=this.renderer;
    renderer.render(scene,camera);
    if(!enabled || !this.root) return;
    const selected=new Set();
    this.root.traverse(node=>{if(node.material) selected.add(node);});
    if(!selected.size) return;

    renderer.getDrawingBufferSize(this.size);
    if(this.mask.width!==this.size.x || this.mask.height!==this.size.y) this.mask.setSize(this.size.x,this.size.y);
    const target=renderer.getRenderTarget(),background=scene.background,override=scene.overrideMaterial;
    const autoClear=renderer.autoClear,clearColor=renderer.getClearColor(new THREE.Color()),clearAlpha=renderer.getClearAlpha();
    const changed=[];
    try {
      scene.background=null;scene.overrideMaterial=null;
      scene.traverse(node=>{
        if(!node.material) return;
        const original=node.material;
        changed.push([node,original]);
        node.material=Array.isArray(original)
          ? original.map(material=>this.maskMaterial(material,selected.has(node)))
          : this.maskMaterial(original,selected.has(node));
      });
      renderer.setRenderTarget(this.mask);
      renderer.setClearColor(0x000000,0);renderer.autoClear=true;
      renderer.render(scene,camera);
    } finally {
      for(const [node,material] of changed) node.material=material;
      scene.background=background;scene.overrideMaterial=override;
      renderer.setRenderTarget(target);renderer.setClearColor(clearColor,clearAlpha);renderer.autoClear=autoClear;
    }
    this.stroke.uniforms.step.value.set(this.width*renderer.getPixelRatio()/this.size.x,
      this.width*renderer.getPixelRatio()/this.size.y);
    try {
      renderer.autoClear=false;
      renderer.render(this.overlay,this.overlayCamera);
    } finally {renderer.autoClear=autoClear;}
  }

  dispose() {
    this.clearMaterials();this.mask.dispose();this.stroke.dispose();
    this.overlay.children[0].geometry.dispose();this.root=null;
  }
}
