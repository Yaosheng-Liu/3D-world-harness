// Captured RGB already contains the scene lighting; keep it in display color space.
export function configureWorldMaterials(root) {
  root.traverse(node => {
    const materials = Array.isArray(node.material) ? node.material : node.material ? [node.material] : [];
    for (const material of materials) {
      if (material.userData.world_appearance === 'captured') material.toneMapped = false;
    }
  });
}
