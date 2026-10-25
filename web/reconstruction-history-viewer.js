import * as THREE from './vendor/three/build/three.module.min.js';
import { GLTFLoader } from './vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from './vendor/three/examples/jsm/controls/OrbitControls.js';
import { configureWorldLoader, configureWorldPreview } from './world-materials.js';

// Separate renderer: inspecting an old stage never changes the editable workbench.
export function createHistoryViewer(host) {
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
  controls.addEventListener('change', draw);
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
    async load(url, view) {
      const token = ++version;
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
      resize();
    },
    reset,
    dispose() {
      disposed = true;
      ++version;
      observer.disconnect();
      controls.dispose();
      if (model) release(model);
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    },
  };
}
