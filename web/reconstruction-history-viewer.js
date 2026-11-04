import * as THREE from './vendor/three/build/three.module.min.js';
import { GLTFLoader } from './vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from './vendor/three/examples/jsm/controls/OrbitControls.js';
import { configureWorldLoader, configureWorldPreview } from './world-materials.js';

// Separate renderer: history and editing branches never replace the live reconstruction.
export function createHistoryViewer(host, { onSelect, onChange = () => {} } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  host.append(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#e5e8e3');
  scene.add(new THREE.HemisphereLight(0xe4f0ff, 0x73796a, 2.4));
  const light = new THREE.DirectionalLight(0xfff1df, 2.5);
  light.position.set(3, 8, 5);
  scene.add(light);
  const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 1000);
  const controls = new OrbitControls(camera, renderer.domElement);
  const loader = new GLTFLoader();
  configureWorldLoader(loader);
  const objects = new Map();
  const selection = new THREE.Box3Helper(new THREE.Box3(), 0x328b73);
  selection.visible = false;
  scene.add(selection);
  let selected = null, pointerDown = null;
  let model = null,
    version = 0,
    disposed = false,
    initial = null;
  function release(root) {
    const geometries = new Set(),
      materials = new Set(),
      textures = new Set();
    root.traverse((node) => {
      if (node.geometry) geometries.add(node.geometry);
      for (const mat of Array.isArray(node.material) ? node.material : node.material ? [node.material] : [])
        materials.add(mat);
    });
    for (const mat of materials)
      for (const value of Object.values(mat)) if (value?.isTexture) textures.add(value);
    for (const texture of textures) {
      texture.dispose();
      texture.source?.data?.close?.();
    }
    for (const material of materials) material.dispose();
    for (const geometry of geometries) geometry.dispose();
  }
  function draw() {
    if (selected) selection.box.setFromObject(selected);
    if (!disposed) renderer.render(scene, camera);
  }
  function resize() {
    const { width, height } = host.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    draw();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(host);
  controls.addEventListener('change', () => { draw(); onChange(); });
  function restore() {
    for (const original of objects.values()) {
      original.node.position.copy(original.position);
      original.node.quaternion.copy(original.rotation);
      original.node.scale.copy(original.scale);
      for (const [mesh, material] of original.materials) {
        if (mesh.material !== material) mesh.material.dispose();
        mesh.material = material;
      }
    }
    draw();
  }
  function select(id, focus = false) {
    selected = objects.get(id)?.node || null;
    selection.visible = Boolean(selected);
    if (selected && focus) {
      const box = new THREE.Box3().setFromObject(selected), center = box.getCenter(new THREE.Vector3());
      const direction = camera.position.clone().sub(controls.target).normalize();
      camera.position.copy(center).add(direction.multiplyScalar(Math.max(box.getSize(new THREE.Vector3()).length(), 0.1) * 1.5));
      controls.target.copy(center);
      controls.update();
    }
    draw();
  }
  function pick(event) {
    if (!onSelect || !model || !pointerDown || Math.hypot(event.clientX-pointerDown.x, event.clientY-pointerDown.y) > 5) return;
    const rect = renderer.domElement.getBoundingClientRect();
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1, 1-(event.clientY-rect.top)/rect.height*2), camera);
    let hit = ray.intersectObject(model, true)[0]?.object;
    while (hit && hit.userData.world_role !== 'object') hit = hit.parent;
    if (hit?.userData.stable_id) onSelect(hit.userData.stable_id);
  }
  renderer.domElement.addEventListener('pointerdown', event => { pointerDown = { x:event.clientX, y:event.clientY }; });
  renderer.domElement.addEventListener('pointerup', pick);
  function reset() {
    if (initial) {
      camera.position.copy(initial.position);
      camera.quaternion.copy(initial.rotation);
      controls.target.copy(initial.target);
      controls.update();
      draw();
    }
  }
  return {
    async load(url, view, { preserveCamera = false } = {}) {
      const token = ++version;
      const previous = preserveCamera && initial ? { position:camera.position.clone(), rotation:camera.quaternion.clone(), target:controls.target.clone() } : null;
      restore();
      selected = null;
      selection.visible = false;
      objects.clear();
      if (model) {
        scene.remove(model);
        release(model);
        model = null;
      }
      const gltf = await loader.loadAsync(url);
      if (disposed || token !== version) {
        release(gltf.scene);
        return;
      }
      model = gltf.scene;
      configureWorldPreview(model, renderer.capabilities.maxTextureSize);
      scene.add(model);
      model.traverse(node => {
        if (node.userData.world_role !== 'object') return;
        objects.set(node.userData.stable_id, { node, position:node.position.clone(), rotation:node.quaternion.clone(), scale:node.scale.clone(),
          materials:node.children.filter(child => child.isMesh).map(mesh => [mesh, mesh.material]) });
      });
      const box = new THREE.Box3().setFromObject(model),
        center = box.getCenter(new THREE.Vector3()),
        radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.1);
      camera.near = radius / 10000;
      camera.far = radius * 50;
      if (view?.camera_to_world) {
        const pose = new THREE.Matrix4().set(...view.camera_to_world.flat());
        pose.premultiply(new THREE.Matrix4().makeRotationX(-Math.PI / 2));
        camera.position.setFromMatrixPosition(pose);
        camera.quaternion.setFromRotationMatrix(pose);
        // Saved world cameras already use Blender/OpenGL axes; only convert world Z-up to glTF Y-up.
        camera.up.setFromMatrixColumn(pose, 1).normalize();
        camera.fov = THREE.MathUtils.radToDeg(
          2 * Math.atan(view.intrinsics.height / (2 * view.intrinsics.fy)),
        );
        controls.target
          .copy(camera.position)
          .add(camera.getWorldDirection(new THREE.Vector3()).multiplyScalar(radius * 0.45));
      } else {
        camera.position
          .copy(center)
          .add(new THREE.Vector3(1, 0.7, 1).normalize().multiplyScalar(radius * 2.5));
        controls.target.copy(center);
      }
      controls.update();
      initial = {
        position: camera.position.clone(),
        rotation: camera.quaternion.clone(),
        target: controls.target.clone(),
      };
      if (previous) {
        camera.position.copy(previous.position);
        camera.quaternion.copy(previous.rotation);
        controls.target.copy(previous.target);
        controls.update();
      }
      resize();
    },
    select,
    restore,
    previewObject(spec, factor = 1) {
      restore();
      const original = objects.get(spec.id);
      if (!original) throw new Error('模型中找不到此物体');
      const basis = new THREE.Matrix4().makeRotationX(-Math.PI / 2);
      const matrix = new THREE.Matrix4().compose(new THREE.Vector3(...spec.position),
        new THREE.Quaternion().setFromEuler(new THREE.Euler(...spec.rotation, 'ZYX')), new THREE.Vector3(1,1,1));
      matrix.premultiply(basis).multiply(basis.clone().invert());
      matrix.decompose(original.node.position, original.node.quaternion, original.node.scale);
      original.node.scale.multiplyScalar(factor);
      for (const [mesh, source] of original.materials) {
        const captured = spec.material.appearance === 'captured';
        const material = captured ? new THREE.MeshBasicMaterial() : new THREE.MeshStandardMaterial({roughness:spec.material.roughness, metalness:spec.material.metallic});
        material.color.setRGB(...spec.material.base_color.slice(0,3));
        material.map = spec.material.base_color_texture ? source.map : null;
        material.side = source.side;
        material.toneMapped = !captured;
        mesh.material = material;
      }
      draw();
    },
    capture(revision) {
      camera.updateMatrixWorld();
      const m = new THREE.Matrix4().makeRotationX(Math.PI / 2).multiply(camera.matrixWorld).elements;
      const { width, height } = host.getBoundingClientRect(), scale = Math.min(1, 960 / Math.max(width, height));
      const w = Math.max(16, Math.round(width*scale)), h = Math.max(16, Math.round(height*scale));
      const p = camera.projectionMatrix.elements;
      return {scene_revision:revision, time_sec:0, camera_to_world:Array.from({length:4}, (_,row) => Array.from({length:4}, (_,col) => m[col*4+row])),
        intrinsics:{width:w, height:h, fx:p[0]*w/2, fy:p[5]*h/2, cx:(1-p[8])*w/2, cy:(1+p[9])*h/2}, near:camera.near, far:camera.far};
    },
    reset,
    dispose() {
      disposed = true;
      ++version;
      observer.disconnect();
      controls.dispose();
      restore();
      if (model) release(model);
      selection.geometry.dispose();
      selection.material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    },
  };
}
