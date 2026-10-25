const stages = {
  not_started:'尚未启动重建审核', modeling:'逐物体精细建模中', checking:'检查候选', reviewing:'独立审核中',
  reviewed:'审核结束', repairing:'建模 Agent 准备返工', publishing:'发布中', accepted:'已通过并发布',
  needs_revision:'待改进', failed:'流程中断', interrupted:'流程已停止', edited:'已编辑，待复核'
};
const busyStages = new Set(['modeling','checking','reviewing','reviewed','repairing','publishing']);
const planningStages = {
  planned:'初始化计划已生成，等待分割', segmented:'初始分割结束，等待几何融合',
  planning:'初始化：计划 Agent 拆解场景', segmenting:'初始化：分割与核对物体', geometry:'初始化：融合观测几何',
  ready_for_modeling:'初始证据可用，等待精细建模', modeling:'逐物体精细建模中', reviewing:'审核物体还原',
  modeled:'反馈建模已完成，待下次复核',
  repairing:'建模 Agent 准备返工',
  needs_revision:'待建模 Agent 返工', accepted:'建模审核已通过并发布', failed:'物体流程中断', interrupted:'物体流程已停止'
};
const planningBusyStages = new Set(['planning','segmenting','geometry','modeling','reviewing','repairing']);
const evidenceStates = {
  pending:'待处理', segmented:'已分割', missing:'未检出', missed:'未检出', ambiguous:'待核实',
  uncertain:'待核实', partial:'部分完成', failed:'失败', ready:'就绪', completed:'已完成',
  unavailable:'暂无', not_started:'尚未处理', manual:'按参考建模', needs_revision:'待改进',
  observed:'观测几何', insufficient:'证据不足', insufficient_evidence:'证据不足',
  not_required:'无需执行', skipped:'未执行', fused:'已融合'
};
const pipelineSteps = [
  ['reference','参考资料',''], ['initialization','场景初始化','计划 / SAM / DA3 / TSDF'],
  ['modeling','逐物体精细建模','补全 / 拆分 / 材质'], ['checking','程序检查','完整性 / 可编辑性'],
  ['reviewing','独立审核','对照原图与编辑结果'], ['accepted','通过发布','']
];
const issueCategories = {
  silhouette:'轮廓错误', texture_alignment:'纹理错位', lighting:'光照偏差',
  inventory:'物体清单', segmentation:'分割与归属', geometry:'几何', appearance:'外观与材质',
  editability:'可编辑性', planning:'物体清单', modeling:'建模'
};

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
    <ol class="flow-steps">${pipelineSteps.map(([key,label,detail]) => `<li data-stage="${key}"><span>${label}</span>${detail ? `<small>${detail}</small>` : ''}</li>`).join('')}</ol>
    <p class="flow-review-loop">审核反馈 → 建模 Agent 返工 → 再次审核</p>
    <p id="flow-summary" role="status"></p><div id="flow-facts" class="flow-facts"></div>
    <section id="flow-plan" class="flow-plan" hidden aria-label="场景初始化与观测证据">
    <div class="flow-plan-heading"><h3>场景初始化与观测证据</h3><span id="flow-plan-state" class="flow-badge"></span></div>
    <p id="flow-plan-summary"></p><p id="flow-plan-counts" class="flow-muted"></p>
    <div class="flow-plan-columns"><div><div id="flow-plan-list" class="flow-plan-list" aria-label="初始物体清单"></div>
    <div id="flow-unresolved"></div></div><div class="flow-plan-detail">
    <h4 id="flow-plan-object-name"></h4><p id="flow-plan-object-notes"></p>
    <p id="flow-plan-publication" class="flow-muted"></p><button id="flow-plan-focus" type="button" disabled>定位当前显示物体</button>
    <label for="flow-plan-view">原始证据视角</label><select id="flow-plan-view"></select>
    <div id="flow-plan-images" class="flow-comparison" hidden><figure><figcaption>原始参考</figcaption><img id="flow-plan-reference" alt="计划物体所在的原始参考图"/></figure>
    <figure id="flow-plan-mask-figure"><figcaption>实例掩码叠图 · 分割证据</figcaption><img id="flow-plan-mask" alt="计划物体的实例分割掩码叠图"/></figure></div>
    <p id="flow-plan-image-status" role="status"></p></div></div></section>
    <div class="flow-columns"><section><h3>候选与审核</h3><p id="flow-candidate"></p>
    <label for="flow-view">审核视角</label><select id="flow-view"></select>
    <div id="flow-comparison" class="flow-comparison" hidden><figure><figcaption>原始参考</figcaption><div class="flow-picture"><img id="flow-reference" alt="候选视角的原始参考图"/><div class="flow-region" hidden></div></div></figure><figure><figcaption>待审候选 · 独立渲染</figcaption><div class="flow-picture"><img id="flow-render" alt="候选的独立渲染，不含参考叠图"/><div class="flow-region" hidden></div></div></figure></div>
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
  let plannedObjectId = null;
  const selectedPlannedObject = () => status.planning?.objects.find(obj => obj.id === plannedObjectId);
  const isBusy = () => busyStages.has(status.stage) || planningBusyStages.has(status.planning?.stage);
  const stateLabel = value => evidenceStates[value] || value || '待处理';

  function fillObject() {
    const obj = selectedObject();
    el('edit-form').hidden = Boolean(editor?.draft);
    for (const field of ['position','rotation']) [...el(field).querySelectorAll('input')].forEach((input,i) => {
      input.value = obj ? Number((obj[field][i] * (field === 'rotation' ? 180 / Math.PI : 1)).toFixed(6)) : '';
    });
    el('object-evidence').textContent = obj ? `父对象：${obj.parent_id || '场景'}。${obj.evidence.notes}` : '';
    el('apply').disabled = saving || !obj || !editor?.matches_display || editor?.editable === false || isBusy();
    el('focus').disabled = !obj;
  }

  async function refreshEditor() {
    try {
      const next = await api('/api/workspace/world-editor');
      const selected = el('object').value;
      editor = next;
      el('object').replaceChildren(...editor.objects.map(obj => new Option(obj.name + ' · ' + obj.id, obj.id)));
      if (editor.objects.some(obj => obj.id === selected)) el('object').value = selected;
      el('editor-status').textContent = editor.draft
        ? `建模中预览 v${editor.scene_revision} · ${editor.objects.length} 个对象 · 可定位、标注并提交修改意见，尚未验收`
        : `显示 v${editor.scene_revision} · 世界 v${editor.revision} · ${editor.objects.length} 个对象`;
      el('units').textContent = editor.units === 'm' ? '位置单位：米。修改父对象会带动其子对象。' : '位置使用重建坐标，尚未标定米制尺度。修改父对象会带动其子对象。';
      fillObject(); showPlannedPublication();
    } catch (error) {
      editor = null; el('object').replaceChildren(); fillObject(); showPlannedPublication();
      el('editor-status').textContent = error.message;
    }
  }

  function showView(region=null) {
    const view = selectedView();
    el('comparison').hidden = !view;
    el('align').disabled = !view || view.reference_only === true;
    el('render').closest('figure').hidden = view?.reference_only === true;
    for (const box of dialog.querySelectorAll('.flow-region')) {
      box.hidden = !region;
      if (region) Object.assign(box.style, {left:region[0]*100+'%',top:region[1]*100+'%',width:(region[2]-region[0])*100+'%',height:(region[3]-region[1])*100+'%'});
    }
    if (view) for (const kind of ['reference','render']) {
      if (kind === 'render' && view.reference_only) { el(kind).removeAttribute('src'); continue; }
      el(kind).src = resourceURL('/api/workspace/reconstruction/image?' + new URLSearchParams({view_id:view.id, kind, candidate:status.candidate_id}));
    }
  }

  function showPlannedPublication() {
    const obj = selectedPlannedObject();
    const published = obj && editor?.matches_display && editor.objects.some(item => item.id === obj.id);
    el('plan-focus').disabled = !published;
    el('plan-publication').textContent = !obj ? '' : published
      ? '此 ID 已存在于当前显示版本；可定位和标注，质量仍以独立审核为准。'
      : '此物体 ID 尚未出现在当前显示版本中；这里展示计划与证据。';
  }

  function showPlanView() {
    const obj = selectedPlannedObject();
    const view = obj?.evidence.find(item => String(item.view_id) === el('plan-view').value);
    el('plan-images').hidden = !view;
    el('plan-mask-figure').hidden = !view?.has_mask;
    el('plan-view').disabled = !obj?.evidence.length;
    el('plan-image-status').textContent = !view ? '尚无可查看的图像证据。'
      : view.has_mask ? '掩码用于核对像素归属；几何和可编辑性另行验收。'
      : '此视角尚无有效掩码，物体仍保留，由建模 Agent 对照原图处理。';
    for (const kind of ['reference','mask']) {
      const image = el('plan-' + kind);
      image.removeAttribute('src');
      image.hidden = !view || (kind === 'mask' && !view.has_mask);
      if (image.hidden) continue;
      image.onerror = () => { image.hidden = true; el('plan-image-status').textContent = '图像证据暂不可用，请检查该物体的分割记录。'; };
      image.src = resourceURL('/api/workspace/object-plan/image?' + new URLSearchParams({
        object_id:obj.id, view_id:view.view_id, kind, run:status.planning.run_id, updated:status.planning.updated_at
      }));
    }
  }

  function selectPlannedObject(id) {
    plannedObjectId = id;
    const obj = selectedPlannedObject();
    for (const button of el('plan-list').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.objectId === id));
    el('plan-object-name').textContent = obj ? `${obj.name} · ${obj.id}` : '选择一个计划物体';
    el('plan-object-notes').textContent = obj ? `父对象：${obj.parent_id || '场景'}。${obj.notes || ''}` : '';
    const previous = el('plan-view').value;
    el('plan-view').replaceChildren(...(obj?.evidence || []).map(view => new Option(view.label || `视角 ${view.view_id}`, String(view.view_id))));
    if (obj?.evidence.some(view => String(view.view_id) === previous)) el('plan-view').value = previous;
    showPlannedPublication(); showPlanView();
  }

  function renderPlan() {
    const plan = status.planning;
    el('plan').hidden = !plan;
    if (!plan) return;
    el('plan-state').textContent = planningStages[plan.stage] || plan.stage;
    el('plan-summary').textContent = plan.error || plan.reason || plan.summary || '计划物体将按原始图像证据逐项核对。';
    el('plan-counts').textContent = `${plan.objects.length} 个初始对象 · ${plan.unresolved.length} 项初始化缺口 · 处理结果见候选审核`;
    const byId = new Map(plan.objects.map(obj => [obj.id,obj]));
    const ordered = [], visited = new Set();
    function addBranch(obj, depth) {
      if (visited.has(obj.id)) return;
      visited.add(obj.id); ordered.push({obj,depth});
      for (const child of plan.objects) if (child.parent_id === obj.id) addBranch(child,depth+1);
    }
    for (const obj of plan.objects) if (!obj.parent_id || !byId.has(obj.parent_id)) addBranch(obj,0);
    for (const obj of plan.objects) if (!visited.has(obj.id)) addBranch(obj,0);
    el('plan-list').replaceChildren(...ordered.map(({obj,depth}) => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'flow-plan-object';
      button.dataset.objectId = obj.id; button.style.setProperty('--object-depth', Math.min(depth,5));
      const label = document.createElement('strong'); label.textContent = obj.name;
      const identity = document.createElement('small'); identity.textContent = `${obj.id}${obj.parent_id ? ` · 属于 ${obj.parent_id}` : ''}`;
      const progress = document.createElement('span'); progress.textContent = `分割：${stateLabel(obj.segmentation_status)} · 几何：${stateLabel(obj.geometry_status)}`;
      button.append(label,identity,progress); button.onclick = () => selectPlannedObject(obj.id); return button;
    }));
    if (!plan.objects.length) el('plan-list').textContent = '计划 Agent 尚未提交物体清单。';
    el('unresolved').replaceChildren(...plan.unresolved.map(item => {
      const article = document.createElement('article'); article.className = 'flow-issue';
      const heading = document.createElement('strong'); heading.textContent = '初始化缺口 · ' + (item.object_id || '场景');
      const reason = document.createElement('p'); reason.textContent = item.reason;
      const action = document.createElement('p'); action.textContent = '建模任务：' + item.next_action;
      article.append(heading,reason,action);
      if (byId.has(item.object_id)) { const button = document.createElement('button'); button.type = 'button'; button.textContent = '查看物体证据'; button.onclick = () => selectPlannedObject(item.object_id); article.append(button); }
      return article;
    }));
    selectPlannedObject(byId.has(plannedObjectId) ? plannedObjectId : plan.objects[0]?.id);
  }

  function render() {
    const active = isBusy();
    const plan = status.planning;
    const planStage = plan?.stage;
    bar.hidden = status.stage === 'not_started' && !workspace.reference_clip && !plan;
    bar.dataset.active = String(active);
    el('stage').textContent = (status.stage === 'modeling' || planStage === 'modeling') && workspace.approvals?.length
      ? '建模等待确认' : plan ? planningStages[planStage] || planStage : stages[status.stage] || status.stage;
    el('version').textContent = `显示 v${workspace.scene_revision ?? '—'}${plan ? ' · 物体重建' : status.round ? ` · 第 ${status.round}/${status.max_rounds} 轮` : ''}`;
    el('summary').textContent = plan ? plan.error || plan.reason || plan.summary || planningStages[planStage] : status.error || status.reason || (status.stage === 'modeling'
      ? '建模 Agent 正在基于初始场景与原始证据逐物体细化候选；候选完成后进入检查与审核。'
      : status.stage === 'reviewing' ? '审核 Agent 正在检查冻结候选；问题单将回传建模 Agent。' : status.summary || '参考资料就绪后，可启动建模与独立审核。');
    const displayStage = planStage || status.stage;
    const currentStep = ['failed','interrupted'].includes(displayStage) ? null : ({
      planning:'initialization',planned:'initialization',segmenting:'initialization',segmented:'initialization',geometry:'initialization',
      repairing:'modeling',needs_revision:'modeling',ready_for_modeling:'modeling',modeled:'modeling',reviewed:'reviewing',edited:'reviewing',publishing:'accepted'
    })[displayStage] || displayStage;
    const completed = new Set(plan?.completed_stages || []);
    if (['planning','segmenting','geometry'].every(stage => completed.has(stage))) completed.add('initialization');
    for (const node of dialog.querySelectorAll('.flow-steps li')) {
      node.classList.toggle('current', node.dataset.stage === currentStep);
      node.classList.toggle('done', completed.has(node.dataset.stage) || (!plan && node.dataset.stage === 'accepted' && status.accepted === true));
    }
    const facts = [
      ['当前预览', status.preview ? `${status.preview.object_count} 个对象 · 未验收 · ${status.preview.summary}` : '等待建模阶段更新'],
      ['参考资料', workspace.reference_clip ? `${workspace.reference_clip.frames.length} 帧 · 已接入` : '未接入片段'],
      ['初始化 · 计划 Agent', modelLabel(plan?.planner)], ['建模 Agent', modelLabel(status.builder)], ['审核 Agent', modelLabel(status.reviewer)],
      [plan ? '已有候选 · 程序检查' : '程序检查', status.checks ? (Object.values(status.checks).every(Boolean) ? '通过' : '发现问题') : '等待候选'],
      [plan ? '已有审核 · 外观 / 结构 / 编辑' : '外观 / 结构 / 编辑', status.dimensions ? Object.values(status.dimensions).map(v => ({passed:'通过',failed:'未通过',unknown:'待核实'})[v]).join(' / ') : '尚未验收'],
      ['物理', '本阶段暂缓']
    ];
    el('facts').replaceChildren(...facts.map(([label,value]) => { const node = document.createElement('div'); const key = document.createElement('small'); key.textContent = label; const text = document.createElement('span'); text.textContent = value; node.append(key,text); return node; }));
    renderPlan();
    el('candidate').textContent = (plan && status.candidate_id ? '已有审核记录 · ' : '') + (status.candidate_id ? `${status.stage === 'edited' ? '上次审核候选' : '候选'} ${status.candidate_id.slice(0,10)} · ${status.object_count} 个对象${status.stage === 'edited' ? ' · 当前编辑版本待复核' : status.accepted ? ' · 已通过' : ' · 未通过验收'}` : '候选尚未提交。');
    const viewKey = JSON.stringify([status.candidate_id, status.views?.map(view => view.id)]);
    if (el('view').dataset.key !== viewKey) {
      el('view').dataset.key = viewKey;
      el('view').replaceChildren(...(status.views || []).map(view => new Option(view.reference_only ? '原始证据 · ' + view.id : view.id, view.id)));
      showView();
    }
    el('issues').replaceChildren(...(status.issues || []).map(issue => {
      const card = document.createElement('article'); card.className = 'flow-issue';
      const category = issue.category || issue.target;
      const heading = document.createElement('strong'); heading.textContent = `${({blocking:'阻塞',major:'主要',minor:'次要'})[issue.severity]} · ${issue.object_id}${category ? ' · ' + (issueCategories[category] || category) : ''}`;
      const owner = document.createElement('p'); owner.className = 'flow-issue-owner'; owner.textContent = '交给建模 Agent';
      const evidence = document.createElement('p'); evidence.textContent = issue.evidence;
      const criterion = document.createElement('p'); criterion.textContent = '复验标准：' + issue.acceptance_criterion;
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '查看 ' + issue.view_id;
      button.onclick = () => { el('view').value = issue.view_id; showView(issue.region); el('comparison').scrollIntoView({block:'nearest'}); };
      card.append(heading,owner,evidence,criterion,button); return card;
    }));
    el('record').textContent = JSON.stringify({run_id:status.run_id, candidate_id:status.candidate_id,
      requested_reviewer:status.reviewer?.requested_model, actual_reviewer:status.reviewer?.model,
      builder_thread:status.builder?.thread_id, reviewer_thread:status.reviewer?.thread_id, updated_at:status.updated_at,
      planning:plan ? {run_id:plan.run_id,stage:plan.stage,planner:plan.planner,updated_at:plan.updated_at} : null},null,2);
    el('apply').disabled = saving || !selectedObject() || !editor?.matches_display || editor?.editable === false || active;
  }

  async function refresh(nextWorkspace, force=false) {
    workspace = nextWorkspace;
    if (polling || (!force && Date.now()-lastPoll < 5000)) return;
    polling = true; lastPoll = Date.now();
    try {
      status = await api('/api/workspace/reconstruction'); render();
      if (dialog.open && editor?.scene_revision !== workspace.scene_revision) await refreshEditor();
    }
    catch (error) { el('stage').textContent = '流程状态暂不可用'; el('summary').textContent = error.message; }
    finally { polling = false; }
  }

  el('open').onclick = async () => { dialog.showModal(); await refresh(workspace,true); await refreshEditor(); };
  el('close').onclick = () => dialog.close();
  el('object').onchange = fillObject;
  el('plan-view').onchange = showPlanView;
  el('plan-focus').onclick = () => { const obj = selectedPlannedObject(); if (obj && !el('plan-focus').disabled) { dialog.close(); focusObject(obj.id); } };
  el('reset').onclick = fillObject;
  el('view').onchange = () => showView();
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
    finally { saving = false; el('apply').disabled = !selectedObject() || !editor?.matches_display || editor?.editable === false || isBusy(); }
  };
  return {refresh};
}
