import { Color } from './vendor/three/build/three.module.min.js';
import { createHistoryViewer } from './reconstruction-history-viewer.js';

export function createSceneEditor({ api, resourceURL }) {
  const dialog = document.createElement('dialog');
  dialog.id = 'scene-editor';
  dialog.setAttribute('aria-label', '场景编辑工作区');
  dialog.innerHTML = `
    <header class="edit-heading"><div><h2>场景编辑 <span>独立分支</span></h2><p id="edit-source">从当前重建版本开始创作</p></div><button id="edit-close" type="button">返回重建 ×</button></header>
    <div class="edit-branches"><label>编辑分支 <select id="edit-branch" aria-label="编辑分支"></select></label><div class="edit-new"><input id="edit-name" aria-label="新分支名称" placeholder="新分支名称" maxlength="80"/><button id="edit-create" type="button">从当前重建新建</button></div></div>
    <div class="edit-layout"><section class="edit-stage" aria-label="编辑画布"><div id="edit-canvas"></div><div id="edit-empty"><strong>为重建场景创建一个编辑分支</strong><p>在这里移动物品、调整大小与材质，保存自己的布置方案。</p><p>原重建版本与审核记录独立保留。</p></div><img id="edit-rendered" alt="当前编辑分支的 Cycles 渲染" hidden/><div class="edit-view-tools"><button id="edit-camera" type="button">初始视角</button><button id="edit-focus" type="button">定位所选</button><button id="edit-render" type="button">Cycles 精细预览</button><button id="edit-live" type="button" hidden>返回实时 3D</button></div><p class="edit-canvas-help">点击物体选择 · 拖动环视 · 滚轮缩放</p></section>
    <aside class="edit-inspector"><fieldset id="edit-controls" disabled><legend>物体与属性</legend><input id="edit-search" type="search" aria-label="搜索编辑物体" placeholder="搜索物体名称或 ID"/><select id="edit-objects" size="6" aria-label="编辑物体清单"></select><h3 id="edit-object-name">选择一个物体</h3><p id="edit-parent" class="edit-muted"></p><form id="edit-form">
    <fieldset><legend>位置 · 父对象坐标</legend><div class="edit-vector" id="edit-position"></div></fieldset><fieldset><legend>旋转 · 度</legend><div class="edit-vector" id="edit-rotation"></div></fieldset>
    <label class="edit-row">整体缩放 <input id="edit-scale" type="number" value="1" min="0.001" step="any" required/></label><p id="edit-units" class="edit-muted"></p>
    <fieldset><legend>材质</legend><label class="edit-row">颜色 <input id="edit-color" type="color"/></label><label class="edit-row">外观 <select id="edit-appearance"><option value="captured">原图贴色</option><option value="surface">表面材质</option></select></label><label class="edit-row">粗糙度 <input id="edit-roughness" type="number" min="0" max="1" step="0.05" required/></label><label class="edit-row">金属度 <input id="edit-metallic" type="number" min="0" max="1" step="0.05" required/></label><label class="edit-texture"><input id="edit-texture" type="checkbox"/> 保留原贴图（颜色作为叠色）</label><p id="edit-material-hint" class="edit-muted"></p></fieldset>
    </form></fieldset></aside></div>
    <footer class="edit-footer"><p id="edit-status" role="status">正在读取场景…</p><button id="edit-discard" type="button">重置修改</button><button id="edit-undo" type="button">撤销</button><button id="edit-redo" type="button">重做</button><button id="edit-save" type="submit" form="edit-form">保存到编辑分支</button><a id="edit-download" download="scene.blend" hidden>下载 Blender 工程</a></footer>`;
  document.body.append(dialog);
  const el = name => dialog.querySelector('#edit-' + name);
  for (const field of ['position', 'rotation']) for (const axis of ['X', 'Y', 'Z']) {
    const label = document.createElement('label');
    label.textContent = axis;
    const input = document.createElement('input');
    input.type = 'number'; input.step = 'any'; input.required = true;
    input.setAttribute('aria-label', `${field === 'position' ? '位置' : '旋转'} ${axis}`);
    label.append(input); el(field).append(label);
  }
  let branch = null, catalog = null, viewer = null, selectedId = null, dirty = false, busy = false, loaded = false;
  const object = () => branch?.scene.objects.find(obj => obj.id === selectedId);
  const message = text => { el('status').textContent = text; };
  function hideRender() { el('rendered').hidden = true; el('live').hidden = true; }
  function controls() {
    el('controls').disabled = busy || !loaded;
    for (const name of ['create', 'branch', 'name']) el(name).disabled = busy || dirty;
    el('close').disabled = busy || dirty;
    el('save').disabled = busy || !dirty;
    el('discard').disabled = busy || !dirty;
    el('undo').disabled = busy || dirty || !branch?.can_undo;
    el('redo').disabled = busy || dirty || !branch?.can_redo;
    for (const name of ['render', 'camera', 'focus']) el(name).disabled = busy || !loaded;
    el('render').disabled ||= dirty;
    el('download').hidden = !branch || dirty || busy;
  }
  function materialHint() {
    const captured = el('appearance').value === 'captured';
    el('roughness').disabled = el('metallic').disabled = captured;
    el('material-hint').textContent = captured ? '原图贴色已包含光照；切换为表面材质后可调粗糙度和金属度。' : '实时画布使用预览灯光；保存后用 Cycles 查看材质效果。';
  }
  function fill() {
    const obj = object();
    if (!obj) return;
    for (const field of ['position', 'rotation']) [...el(field).querySelectorAll('input')].forEach((input, i) => {
      input.value = Number((obj[field][i] * (field === 'rotation' ? 180/Math.PI : 1)).toFixed(6));
      input.dataset.saved = input.value;
    });
    el('scale').value = '1';
    el('object-name').textContent = obj.name;
    const parent = branch.scene.objects.find(item => item.id === obj.parent_id);
    el('parent').textContent = `${obj.id} · ${parent ? '父对象：' + parent.name : '场景根对象'}`;
    el('color').value = '#' + new Color().setRGB(...obj.material.base_color.slice(0,3)).getHexString();
    el('color').dataset.saved = el('color').value;
    el('appearance').value = obj.material.appearance || 'surface';
    el('roughness').value = obj.material.roughness; el('metallic').value = obj.material.metallic;
    el('texture').checked = Boolean(obj.material.base_color_texture);
    el('texture').disabled = !obj.material.base_color_texture;
    el('units').textContent = `${branch.scene.units === 'm' ? '位置单位：米' : '位置为重建坐标，尚未标定米制尺度'}。缩放为本次倍数，包含子对象。`;
    materialHint(); controls();
  }
  function listObjects() {
    const needle = el('search').value.trim().toLowerCase();
    el('objects').replaceChildren(...branch.scene.objects.filter(obj => (obj.id + ' ' + obj.name).toLowerCase().includes(needle)).map(obj => new Option(obj.name, obj.id)));
    el('objects').value = selectedId;
  }
  function select(id) {
    if (dirty && id !== selectedId) { el('objects').value = selectedId; message('请先保存或重置当前修改，再切换物体。'); return; }
    selectedId = id;
    if (![...el('objects').options].some(option => option.value === id)) { el('search').value = ''; listObjects(); }
    el('objects').value = id;
    viewer.select(id); fill();
  }
  function draft() {
    const obj = structuredClone(object());
    for (const field of ['position', 'rotation']) obj[field] = [...el(field).querySelectorAll('input')].map((input,i) => input.value === input.dataset.saved ? obj[field][i] : Number(input.value) * (field === 'rotation' ? Math.PI/180 : 1));
    if (el('color').value !== el('color').dataset.saved) obj.material.base_color = [...new Color(el('color').value).toArray(), 1];
    obj.material.roughness = Number(el('roughness').value); obj.material.metallic = Number(el('metallic').value);
    if (el('appearance').value !== (obj.material.appearance || 'surface')) obj.material.appearance = el('appearance').value;
    if (!el('texture').checked) delete obj.material.base_color_texture;
    return obj;
  }
  async function load(next, preserveCamera = false) {
    branch = next; dirty = false; loaded = false;
    hideRender(); el('empty').hidden = true;
    el('source').textContent = `来自重建 v${branch.source.scene_revision} → ${branch.name} · 编辑版本 ${branch.version} · ${branch.scene.objects.length} 个物体`;
    el('download').href = resourceURL(branch.blend_url);
    el('download').title = branch.blend_path;
    message('正在加载编辑场景…');
    await viewer.load(resourceURL(branch.glb_url), branch.scene.views[0], { preserveCamera });
    loaded = true;
    if (!branch.scene.objects.some(obj => obj.id === selectedId)) selectedId = branch.scene.objects[0].id;
    listObjects(); select(selectedId);
    message(`已保存 · ${branch.name} · 编辑版本 ${branch.version}`);
  }
  async function listBranches(preferred) {
    catalog = await api('/api/workspace/scene-editor');
    el('branch').replaceChildren(...catalog.branches.map(item => new Option(item.name, item.id)));
    if (!catalog.branches.length) el('branch').append(new Option('尚无编辑分支', ''));
    if (preferred) el('branch').value = preferred;
    el('name').placeholder = `新分支名称（来源 v${catalog.scene_revision}）`;
  }
  async function run(task) {
    if (busy) return;
    busy = true; controls();
    try { await task(); } catch (error) { el('branch').value = branch?.id || ''; message('未完成：' + error.message); }
    finally { busy = false; controls(); }
  }
  async function change(action) {
    await run(async () => {
      message(action === 'save' ? '正在保存并生成 Blender 工程…' : '正在切换编辑版本…');
      const body = { action, expected_version:branch.version };
      if (action === 'save') {
        const obj = draft();
        Object.assign(body, {object_id:obj.id, position:obj.position, rotation:obj.rotation, material:obj.material, scale_factor:Number(el('scale').value)});
      }
      const next = await api('/api/workspace/scene-editor/' + branch.id, {method:'POST', body});
      await load(next, true);
      await listBranches(next.id);
    });
  }
  el('create').onclick = () => run(async () => {
    message('正在从当前重建创建编辑分支…');
    const next = await api('/api/workspace/scene-editor', {method:'POST', body:{name:el('name').value.trim() || `编辑方案 ${catalog.branches.length+1}`, expected_scene_revision:catalog.scene_revision}});
    await listBranches(next.id); await load(next); el('name').value = '';
  });
  el('branch').onchange = () => run(async () => load(await api('/api/workspace/scene-editor/' + el('branch').value)));
  el('search').oninput = listObjects;
  el('objects').onchange = () => select(el('objects').value);
  el('form').oninput = () => {
    dirty = true; hideRender(); materialHint(); controls();
    if (!el('form').checkValidity()) { message('请输入有效数值。'); return; }
    viewer.previewObject(draft(), Number(el('scale').value));
    message('未保存 · 画布已预览修改，保存后生成 Blender 工程。');
  };
  el('form').onsubmit = event => { event.preventDefault(); if (el('form').checkValidity()) change('save'); };
  el('discard').onclick = () => { dirty = false; hideRender(); viewer.restore(); fill(); message('已重置未保存的修改。'); };
  el('undo').onclick = () => change('undo'); el('redo').onclick = () => change('redo');
  el('camera').onclick = () => { hideRender(); viewer.reset(); };
  el('focus').onclick = () => { hideRender(); viewer.select(selectedId, true); };
  el('live').onclick = hideRender;
  el('render').onclick = () => run(async () => {
    const frame = viewer.capture(branch.version), key = JSON.stringify(frame);
    message('Cycles 正在渲染已保存的编辑分支…');
    const result = await api(`/api/workspace/scene-editor/${branch.id}/render`, {method:'POST', body:frame});
    if (key !== JSON.stringify(viewer.capture(branch.version))) { message('视角已改变，请重新预览。'); return; }
    el('rendered').src = result.image; await el('rendered').decode();
    if (key !== JSON.stringify(viewer.capture(branch.version))) return;
    el('rendered').hidden = false; el('live').hidden = false;
    message(`Cycles · ${result.denoiser ? '已降噪' : '未降噪'} · ${result.elapsed_seconds.toFixed(1)} 秒`);
  });
  el('close').onclick = () => dialog.close();
  dialog.addEventListener('cancel', event => { if (busy || dirty) { event.preventDefault(); message('请先保存或重置修改，再返回重建。'); } });
  dialog.addEventListener('close', () => { viewer?.dispose(); viewer = null; });
  window.addEventListener('beforeunload', event => { if (dialog.open && (dirty || busy)) event.preventDefault(); });
  return { async open() {
    dialog.showModal();
    viewer = createHistoryViewer(el('canvas'), { onSelect:select, onChange:hideRender });
    await run(async () => {
      await listBranches(branch?.id);
      if (catalog.branches.length) await load(await api('/api/workspace/scene-editor/' + el('branch').value));
      else { message('新建分支即可开始编辑。'); el('empty').hidden = false; }
    });
  }};
}
