const stages = {
  not_started:'尚未启动重建审核', modeling:'建模中', checking:'检查候选', reviewing:'独立审核中',
  reviewed:'审核结束', repairing:'准备返工', publishing:'发布中', accepted:'已通过并发布',
  needs_revision:'待改进', failed:'流程中断', edited:'已编辑，待复核'
};
const busyStages = new Set(['modeling','checking','reviewing','reviewed','repairing','publishing']);

export function createReconstructionWorkflow({api, resourceURL, loadScene, focusObject, alignView}) {
  const bar = document.createElement('div');
  bar.id = 'reconstruction-bar';
  bar.hidden = true;
  bar.innerHTML = '<span class="flow-dot"></span><strong id="flow-stage"></strong><span id="flow-version"></span><button id="flow-open" type="button">重建流程 / 物体编辑</button>';
  document.querySelector('.scene-pane > .pane-head').append(bar);
  const dialog = document.createElement('dialog');
  dialog.id = 'reconstruction-dialog';
  dialog.setAttribute('aria-label','重建流程与物体编辑');
  dialog.innerHTML = `<div class="flow-heading"><div><small>3D-world harness</small><h2>真实场景重建</h2></div><button type="button" id="flow-close" aria-label="关闭重建流程">×</button></div>
    <ol class="flow-steps"><li>参考资料</li><li>建模</li><li>程序检查</li><li>独立审核</li><li>通过发布</li></ol>
    <p id="flow-summary" role="status"></p><div id="flow-facts" class="flow-facts"></div>
    <div class="flow-columns"><section><h3>候选与审核</h3><p id="flow-candidate"></p>
    <label for="flow-view">审核视角</label><select id="flow-view"></select>
    <div id="flow-comparison" class="flow-comparison" hidden><figure><figcaption>原始参考</figcaption><img id="flow-reference" alt="候选视角的原始参考图"/></figure><figure><figcaption>待审候选 · 独立渲染</figcaption><img id="flow-render" alt="候选的独立渲染，不含参考叠图"/></figure></div>
    <button id="flow-align" type="button" disabled>在工具台对齐此机位</button><div id="flow-issues"></div></section>
    <section><h3>当前版本的物体</h3><p id="flow-editor-status"></p><label for="flow-object">选择物体</label><select id="flow-object"></select>
    <button id="flow-focus" type="button">定位物体</button><p id="flow-object-evidence"></p>
    <form id="flow-edit-form"><fieldset><legend>位置 · 父对象坐标</legend><div class="flow-vector" id="flow-position"></div></fieldset>
    <fieldset><legend>旋转 · 度 / XYZ</legend><div class="flow-vector" id="flow-rotation"></div></fieldset>
    <p id="flow-units"></p><button id="flow-apply" type="submit">保存并更新场景</button><button id="flow-reset" type="button">重置输入</button></form>
    <p id="flow-edit-result" role="status"></p></section></div>
    <details><summary>运行记录</summary><pre id="flow-record"></pre></details>`;
  document.body.append(dialog);
  const el = name => document.getElementById('flow-' + name);
  for (const field of ['position','rotation']) for (const axis of ['X','Y','Z']) {
    const label = document.createElement('label'); label.textContent = axis;
    const input = document.createElement('input'); input.type = 'number'; input.step = 'any'; input.required = true;
    input.setAttribute('aria-label', (field === 'position' ? '位置 ' : '旋转 ') + axis);
    label.append(input); el(field).append(label);
  }
  let status = {stage:'not_started'}, workspace = {}, editor = null, lastPoll = 0, polling = false, saving = false;
  const modelLabel = role => role ? `${role.model} · ${role.effort}` : '未配置';
  const selectedObject = () => editor?.objects.find(obj => obj.id === el('object').value);
  const selectedView = () => status.views?.find(view => view.id === el('view').value);

  function fillObject() {
    const obj = selectedObject();
    for (const field of ['position','rotation']) [...el(field).querySelectorAll('input')].forEach((input,i) => {
      input.value = obj ? Number((obj[field][i] * (field === 'rotation' ? 180 / Math.PI : 1)).toFixed(6)) : '';
    });
    el('object-evidence').textContent = obj ? `父对象：${obj.parent_id || '场景'}。${obj.evidence.notes}` : '';
    el('apply').disabled = saving || !obj || !editor?.matches_display || busyStages.has(status.stage);
    el('focus').disabled = !obj;
  }

  async function refreshEditor() {
    try {
      const next = await api('/api/workspace/world-editor');
      const selected = el('object').value;
      editor = next;
      el('object').replaceChildren(...editor.objects.map(obj => new Option(obj.name + ' · ' + obj.id, obj.id)));
      if (editor.objects.some(obj => obj.id === selected)) el('object').value = selected;
      el('editor-status').textContent = `显示 v${editor.scene_revision} · 世界 v${editor.revision} · ${editor.objects.length} 个对象`;
      el('units').textContent = editor.units === 'm' ? '位置单位：米。修改父对象会带动其子对象。' : '位置使用重建坐标，尚未标定米制尺度。修改父对象会带动其子对象。';
      fillObject();
    } catch (error) {
      editor = null; el('object').replaceChildren(); fillObject();
      el('editor-status').textContent = error.message;
    }
  }

  function showView() {
    const view = selectedView();
    el('comparison').hidden = !view;
    el('align').disabled = !view;
    if (view) for (const kind of ['reference','render']) {
      el(kind).src = resourceURL('/api/workspace/reconstruction/image?' + new URLSearchParams({view_id:view.id, kind, candidate:status.candidate_id}));
    }
  }

  function render() {
    const active = busyStages.has(status.stage);
    bar.hidden = status.stage === 'not_started' && !workspace.reference_clip;
    bar.dataset.active = String(active);
    el('stage').textContent = stages[status.stage] || status.stage;
    el('version').textContent = `显示 v${workspace.scene_revision ?? '—'}${status.round ? ` · 第 ${status.round}/${status.max_rounds} 轮` : ''}`;
    el('summary').textContent = status.error || status.reason || (status.stage === 'modeling'
      ? '建模 Agent 正在制作候选。当前工具台保留上一版；候选完成后进入独立检查与审核。'
      : status.stage === 'reviewing' ? '审核 Agent 正在检查冻结候选；问题单将回传建模 Agent。' : status.summary || '参考资料就绪后，可启动建模与独立审核。');
    const currentStep = ({modeling:1,repairing:1,checking:2,reviewing:3,reviewed:3,needs_revision:3,edited:3,publishing:4,accepted:4})[status.stage] ?? 0;
    [...dialog.querySelectorAll('.flow-steps li')].forEach((node,i) => {
      node.classList.toggle('current', i === currentStep); node.classList.toggle('done', i < currentStep || status.accepted === true);
    });
    const facts = [
      ['参考资料', workspace.reference_clip ? `${workspace.reference_clip.frames.length} 帧 · 已接入` : '未接入片段'],
      ['建模 Agent', modelLabel(status.builder)], ['审核 Agent', modelLabel(status.reviewer)],
      ['程序检查', status.checks ? (Object.values(status.checks).every(Boolean) ? '通过' : '发现问题') : '等待候选'],
      ['外观 / 结构 / 编辑', status.dimensions ? Object.values(status.dimensions).map(v => ({passed:'通过',failed:'未通过',unknown:'待核实'})[v]).join(' / ') : '尚未验收'],
      ['物理', '本阶段暂缓']
    ];
    el('facts').replaceChildren(...facts.map(([label,value]) => { const node = document.createElement('div'); const key = document.createElement('small'); key.textContent = label; const text = document.createElement('span'); text.textContent = value; node.append(key,text); return node; }));
    el('candidate').textContent = status.candidate_id ? `${status.stage === 'edited' ? '上次审核候选' : '候选'} ${status.candidate_id.slice(0,10)} · ${status.object_count} 个对象${status.stage === 'edited' ? ' · 当前编辑版本待复核' : status.accepted ? ' · 已通过' : ' · 未通过验收'}` : '候选尚未提交。';
    const viewKey = JSON.stringify([status.candidate_id, status.views?.map(view => view.id)]);
    if (el('view').dataset.key !== viewKey) {
      el('view').dataset.key = viewKey;
      el('view').replaceChildren(...(status.views || []).map(view => new Option(view.id, view.id)));
      showView();
    }
    el('issues').replaceChildren(...(status.issues || []).map(issue => {
      const card = document.createElement('article'); card.className = 'flow-issue';
      const heading = document.createElement('strong'); heading.textContent = `${({blocking:'阻塞',major:'主要',minor:'次要'})[issue.severity]} · ${issue.object_id}`;
      const evidence = document.createElement('p'); evidence.textContent = issue.evidence;
      const criterion = document.createElement('p'); criterion.textContent = '复验标准：' + issue.acceptance_criterion;
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '查看 ' + issue.view_id;
      button.onclick = () => { el('view').value = issue.view_id; showView(); el('comparison').scrollIntoView({block:'nearest'}); };
      card.append(heading,evidence,criterion,button); return card;
    }));
    el('record').textContent = JSON.stringify({run_id:status.run_id, candidate_id:status.candidate_id,
      requested_reviewer:status.reviewer?.requested_model, actual_reviewer:status.reviewer?.model,
      builder_thread:status.builder?.thread_id, reviewer_thread:status.reviewer?.thread_id, updated_at:status.updated_at},null,2);
    el('apply').disabled = saving || !selectedObject() || !editor?.matches_display || active;
  }

  async function refresh(nextWorkspace, force=false) {
    workspace = nextWorkspace;
    if (polling || (!force && Date.now()-lastPoll < 5000)) return;
    polling = true; lastPoll = Date.now();
    try { status = await api('/api/workspace/reconstruction'); render(); }
    catch (error) { el('stage').textContent = '流程状态暂不可用'; el('summary').textContent = error.message; }
    finally { polling = false; }
  }

  el('open').onclick = async () => { dialog.showModal(); await refresh(workspace,true); await refreshEditor(); };
  el('close').onclick = () => dialog.close();
  el('object').onchange = fillObject;
  el('reset').onclick = fillObject;
  el('view').onchange = showView;
  el('focus').onclick = () => { if (selectedObject()) { dialog.close(); focusObject(selectedObject().id); } };
  el('align').onclick = () => { const view = selectedView(); if (view) { dialog.close(); alignView(view,workspace.reference_clip); } };
  el('edit-form').onsubmit = async event => {
    event.preventDefault(); const obj = selectedObject(); if (!obj || saving) return;
    saving = true; el('apply').disabled = true; el('edit-result').textContent = '正在构建并保存物体变换…';
    const vector = field => [...el(field).querySelectorAll('input')].map(input => Number(input.value) * (field === 'rotation' ? Math.PI / 180 : 1));
    try {
      const result = await api('/api/workspace/world-editor', {method:'POST',body:{
        expected_revision:editor.revision,expected_scene_revision:editor.scene_revision,object_id:obj.id,
        position:vector('position'),rotation:vector('rotation')
      }});
      await loadScene(result.scene); await refreshEditor(); await refresh({...workspace,scene_revision:result.scene.revision},true);
      el('edit-result').textContent = '已保存。新位置已同步到场景与 Cycles；本次编辑尚未重新审核。';
    } catch (error) { el('edit-result').textContent = '未完成：' + error.message; }
    finally { saving = false; el('apply').disabled = !selectedObject() || busyStages.has(status.stage); }
  };
  return {refresh};
}
