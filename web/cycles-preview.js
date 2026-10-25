// One request at a time; never show an image belonging to an earlier camera.
export function createCyclesPreview({api, viewport, capture}) {
  const panel = document.getElementById('cycles-controls');
  const button = document.getElementById('cycles-render');
  const automatic = document.getElementById('cycles-auto');
  const status = document.getElementById('cycles-status');
  const image = document.createElement('img');
  image.className = 'cycles-preview-image';
  image.alt = '当前视角的 Cycles 渲染';
  image.hidden = true;
  viewport.append(image);
  let available = false, busy = false, signature = '', shown = '', attempted = '';
  let changedAt = 0, revision = null, requestVersion = 0, interacting = false, generation = 0;
  const key = frame => frame ? JSON.stringify(frame) : '';
  const hide = () => { image.hidden = true; shown = ''; };

  async function refresh(sceneRevision) {
    const version = ++requestVersion;
    available = false;
    panel.hidden = true;
    hide();
    try {
      const result = await api('/api/workspace/cycles-preview');
      if (version !== requestVersion) return;
      available = result.available && result.scene_revision === sceneRevision;
      panel.hidden = !available;
      status.textContent = available ? '实时 3D' : '';
      attempted = '';
    } catch (error) {
      if (version === requestVersion) status.textContent = error.message;
    }
  }

  async function render() {
    const frame = capture();
    if (!available || busy || !frame || interacting) return;
    const requested = key(frame);
    const startedGeneration = generation;
    attempted = requested;
    busy = true;
    button.disabled = true;
    status.textContent = 'Cycles 渲染中…';
    try {
      const result = await api('/api/workspace/cycles-preview', {method:'POST', body:frame});
      if (key(capture()) !== requested || interacting || generation !== startedGeneration) return;
      image.src = result.image;
      await image.decode();
      if (key(capture()) !== requested || interacting || generation !== startedGeneration) return;
      image.hidden = false;
      shown = requested;
      status.textContent = `Cycles · ${result.denoiser ? '已降噪' : '未降噪'} · ${result.elapsed_seconds.toFixed(1)} 秒`;
    } catch (error) {
      if (key(capture()) === requested) status.textContent = '预览失败：' + error.message;
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  function tick(now, sceneRevision) {
    if (sceneRevision !== null && sceneRevision !== revision) {
      revision = sceneRevision;
      refresh(revision);
    }
    const frame = available ? capture() : null;
    const current = key(frame);
    if (current !== signature) {
      signature = current;
      changedAt = now;
      hide();
      status.textContent = busy ? 'Cycles 渲染中，当前显示实时 3D' : '实时 3D';
    }
    if (available && automatic.checked && current && current !== attempted && !busy && !interacting && now-changedAt > 900) render();
  }
  viewport.addEventListener('pointerdown', () => { interacting = true; hide(); });
  const end = () => { if (interacting) { interacting = false; attempted = ''; changedAt = performance.now(); } };
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
  viewport.addEventListener('wheel', () => { hide(); changedAt = performance.now(); }, {passive:true});
  button.addEventListener('click', render);
  automatic.addEventListener('change', () => { generation++; attempted = ''; hide(); });
  return {tick, currentImage:() => !image.hidden && shown === key(capture()) ? image : null};
}
