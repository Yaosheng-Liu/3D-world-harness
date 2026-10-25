import { TextureLoader } from './vendor/three/build/three.module.min.js';

export function configureWorldLoader(loader) {
  loader.register(parser => {
    const definitions = parser.json.materials || [];
    if (definitions.some(material => material.extras?.world_appearance === 'captured')) {
      // Use HTML images for captured atlases instead of the ImageBitmap GPU path.
      parser.textureLoader = new TextureLoader(parser.options.manager)
        .setCrossOrigin(parser.options.crossOrigin).setRequestHeader(parser.options.requestHeader);
    }
    return {name:'workbench_captured_textures', async afterRoot() {
      for (const [index, definition] of definitions.entries()) {
        if (definition.extras?.world_appearance !== 'captured' || !definition.pbrMetallicRoughness?.baseColorTexture) continue;
        const material = await parser.getDependency('material', index);
        if (!material.map?.image) throw new Error('实拍纹理加载失败：' + (definition.name || index));
      }
    }};
  });
}

// WebGL uses the workbench lighting rig; scene lighting is evaluated by Cycles.
export function configureWorldPreview(root, maxTextureSize = 4096) {
  const limit = Math.min(4096, maxTextureSize);
  root.traverse(node => {
    // Imported lights lack the scene's Cycles occlusion and can wash out PBR surfaces.
    if (node.isLight) node.visible = false;
    const materials = Array.isArray(node.material) ? node.material : node.material ? [node.material] : [];
    for (const material of materials) {
      // Imported scene units vary; decorative viewport fog must not hide geometry.
      material.fog = false;
      if (material.userData.world_appearance !== 'captured') continue;
      // Captured RGB already includes lighting; preserve its display colors.
      material.toneMapped = false;
      const texture = material.map, image = texture?.image;
      if (!image || Math.max(image.width, image.height) <= limit) continue;
      // Resize the shared source once, before its first GPU upload. Keep source assets intact.
      const scale = limit / Math.max(image.width, image.height);
      const canvas = new OffscreenCanvas(Math.max(1, Math.round(image.width * scale)),
        Math.max(1, Math.round(image.height * scale)));
      const context = canvas.getContext('2d');
      context.imageSmoothingQuality = 'high';
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      texture.source.data = canvas;
      texture.needsUpdate = true;
      image.close?.();
    }
  });
}
