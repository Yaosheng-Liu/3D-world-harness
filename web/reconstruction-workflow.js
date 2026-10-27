const stages = {
  not_started: '等待开始',
  modeling: '正在建模',
  checking: '正在检查',
  reviewing: '正在审核',
  reviewed: '审核结束',
  repairing: '准备返修',
  publishing: '正在发布',
  accepted: '已验收',
  needs_revision: '待改进',
  failed: '流程中断',
  interrupted: '已停止',
  edited: '编辑后待复核',
};
const planningStages = {
  planning: '生成物体计划',
  planned: '计划已完成',
  segmenting: '正在分割',
  segmented: '分割已完成',
  geometry: '融合初始几何',
  ready_for_modeling: '等待精细建模',
  modeling: '正在精细建模',
  reviewing: '正在审核',
  modeled: '返修完成 · 待复核',
  repairing: '准备返修',
  needs_revision: '待返修',
  accepted: '已验收',
  failed: '流程中断',
  interrupted: '已停止',
};
const busyStages = new Set(['modeling', 'checking', 'reviewing', 'reviewed', 'repairing', 'publishing']);
const planningBusyStages = new Set([
  'planning',
  'segmenting',
  'geometry',
  'modeling',
  'reviewing',
  'repairing',
]);
const evidenceStates = {
  pending: '待处理',
  segmented: '已分割',
  missing: '未检出',
  missed: '未检出',
  ambiguous: '待核实',
  uncertain: '待核实',
  partial: '部分完成',
  failed: '失败',
  ready: '就绪',
  completed: '已完成',
  unavailable: '暂无',
  not_started: '尚未处理',
  manual: '按参考建模',
  needs_revision: '待改进',
  observed: '观测几何',
  insufficient: '证据不足',
  insufficient_evidence: '证据不足',
  not_required: '无需执行',
  skipped: '未执行',
  fused: '已融合',
  modeled: '已建模',
  excluded: '已排除',
  unresolved: '待解决',
};
const pipelineSteps = [
  ['reference', '参考资料', '原视频与相机'],
  ['initialization', '场景初始化', '计划 · 分割 · 初始几何'],
  ['modeling', '精细建模', '首次建模与反馈返修'],
  ['checking', '程序检查', '覆盖与编辑检查'],
  ['reviewing', '审核反馈', '结论 · 问题 · 证据'],
  ['accepted', '验收交付', '正式成果'],
];
const issueCategories = {
  spatial_structure: '空间结构错误',
  silhouette: '轮廓错误',
  texture_alignment: '纹理错位',
  lighting: '光照偏差',
  inventory: '物体清单',
  segmentation: '分割与归属',
  geometry: '几何',
  appearance: '外观与材质',
  editability: '可编辑性',
  planning: '物体清单',
  modeling: '建模',
};
const checkLabels = {
  editability: '变换与编辑',
  inventory: '物体覆盖',
  edit_visual_coverage: '可视检查覆盖',
  appearance: '外观',
  structure: '结构',
};
const verdictLabels = { accepted: '审核通过', revise: '需要返修', blocked: '证据不足' };
const timeLabel = (value) =>
  value
    ? new Date(value).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    : '';

export function createReconstructionWorkflow({ api, resourceURL, loadScene, focusObject, alignView }) {
  const bar = document.createElement('div');
  bar.id = 'reconstruction-bar';
  bar.hidden = true;
  bar.innerHTML = `<div class="flow-bar-heading"><span class="flow-dot"></span><strong id="flow-stage"></strong><span id="flow-version"></span><button id="flow-editor-open" type="button">物体编辑 ↗</button></div><nav class="flow-track" aria-label="重建阶段">${pipelineSteps.map(([key, label], i) => `<button type="button" data-stage="${key}"><span class="flow-step-number">${String(i + 1).padStart(2, '0')}</span>${label}<span class="flow-step-state"></span></button>`).join('')}</nav>`;
  document.querySelector('.scene-pane > .pane-head').append(bar);
  const dialog = document.createElement('dialog');
  dialog.id = 'reconstruction-dialog';
  dialog.setAttribute('aria-label', '重建过程回溯');
  dialog.innerHTML = `<div class="flow-heading"><div><small>3D-WORLD HARNESS / PROCESS</small><h2>重建过程</h2></div><span class="flow-readonly">历史记录 · 只读查看</span><button id="flow-close" type="button" aria-label="关闭流程回溯">×</button></div>
  <div class="flow-layout"><aside class="flow-sidebar"><nav class="flow-steps" aria-label="历史阶段">${pipelineSteps.map(([key, label, detail], i) => `<button type="button" data-stage="${key}"><span class="flow-step-number">${i + 1}</span><span>${label}<small>${detail}</small></span><span class="flow-step-state"></span></button>`).join('')}</nav><p class="flow-review-loop">审核意见回到建模阶段<br/>各轮结果单独保留</p></aside>
  <main class="flow-content"><div class="flow-section-heading"><div><small id="flow-entry-meta"></small><h3 id="flow-title"></h3></div><label class="flow-version-picker">查看记录<select id="flow-history-version" aria-label="选择阶段记录"></select></label></div>
  <p id="flow-summary" role="status"></p><div id="flow-facts" class="flow-facts"></div><p id="flow-history-error" role="alert" hidden></p>
  <section id="flow-reference-panel" hidden><div class="flow-reference-frame"><img id="flow-source-frame" alt="原视频抽帧"/></div><label>原视频帧 <span id="flow-source-time"></span><input id="flow-source-scrub" aria-label="原视频帧" type="range" min="0" value="0"/></label><p class="flow-muted">主工作台保留原视频播放与相机对齐；这里可回看输入帧。</p></section>
  <section id="flow-result" hidden><div class="flow-result-toolbar"><div class="flow-mode"><button type="button" id="flow-show-images" aria-pressed="true">渲染对比</button><button type="button" id="flow-show-model" aria-pressed="false">三维查看</button></div><select id="flow-view" aria-label="选择历史视角"></select></div>
  <div id="flow-comparison" class="flow-comparison" hidden><figure><figcaption>原始参考</figcaption><div class="flow-picture"><img id="flow-reference" alt="原始参考图"/><div class="flow-region" hidden></div></div></figure><figure><figcaption>该阶段结果</figcaption><div class="flow-picture"><img id="flow-render" alt="所选阶段的独立渲染"/><div class="flow-region" hidden></div></div></figure></div>
  <div id="flow-model-panel" hidden><div id="flow-model-canvas"></div><div class="flow-model-caption"><span id="flow-model-status" role="status"></span><button id="flow-model-reset" type="button">恢复视角</button></div></div>
  <p class="flow-muted">历史查看不会替换当前工作场景。三维窗口使用预览照明，外观判断请看渲染对比。</p></section>
  <section id="flow-plan" class="flow-plan" hidden aria-label="初始化物体与证据"><div class="flow-plan-heading"><h4>物体与原图证据</h4><span id="flow-plan-state" class="flow-badge"></span></div><p id="flow-plan-counts" class="flow-muted"></p>
  <div class="flow-plan-columns"><div><input id="flow-plan-search" type="search" placeholder="搜索物体" aria-label="搜索初始物体"/><div id="flow-plan-list" class="flow-plan-list"></div><details><summary>初始化缺口</summary><div id="flow-unresolved"></div></details></div><div class="flow-plan-detail"><h4 id="flow-plan-object-name"></h4><p id="flow-plan-object-notes"></p><p id="flow-plan-publication" class="flow-muted"></p><button id="flow-plan-focus" type="button" disabled>定位当前显示物体 ↗</button><label for="flow-plan-view">原始证据视角</label><select id="flow-plan-view"></select><div id="flow-plan-images" class="flow-comparison" hidden><figure><figcaption>原始参考</figcaption><img id="flow-plan-reference" alt="物体原图"/></figure><figure id="flow-plan-mask-figure"><figcaption>分割掩码</figcaption><img id="flow-plan-mask" alt="实例掩码叠图"/></figure></div><p id="flow-plan-image-status" role="status"></p></div></div></section>
  <section id="flow-review-panel" hidden><div class="flow-section-heading"><h4>审核问题 <span id="flow-issue-count"></span></h4><select id="flow-issue-filter" aria-label="筛选审核问题"></select></div><div class="flow-review-columns"><div id="flow-issues"></div><article id="flow-issue-detail"></article></div></section>
  <details id="flow-items-panel" hidden><summary id="flow-items-label">处理明细</summary><input id="flow-items-search" type="search" placeholder="搜索处理记录" aria-label="搜索处理记录"/><div id="flow-items"></div></details>
  <details id="flow-documents-panel" hidden><summary>原始记录</summary><p class="flow-muted">用于核对这一阶段的输入、输出和完整报告。</p><div id="flow-documents"></div></details>
  <div id="flow-empty" class="flow-empty" hidden></div>
  </main></div>`;
  document.body.append(dialog);
  const editorDialog = document.createElement('dialog');
  editorDialog.id = 'reconstruction-editor';
  editorDialog.setAttribute('aria-label', '当前场景物体编辑');
  editorDialog.innerHTML = `<div class="flow-heading"><div><small>CURRENT SCENE</small><h2>物体编辑</h2></div><button id="flow-editor-close" type="button" aria-label="关闭物体编辑">×</button></div><p id="flow-editor-status"></p><label for="flow-object">选择物体</label><select id="flow-object"></select><button id="flow-focus" type="button">定位物体 ↗</button><p id="flow-object-evidence"></p><form id="flow-edit-form"><fieldset><legend>位置 · 父对象坐标</legend><div class="flow-vector" id="flow-position"></div></fieldset><fieldset><legend>旋转 · 度 / XYZ</legend><div class="flow-vector" id="flow-rotation"></div></fieldset><p id="flow-units"></p><button id="flow-apply" type="submit">保存并更新场景</button><button id="flow-reset" type="button">重置输入</button></form><p id="flow-edit-result" role="status"></p>`;
  document.body.append(editorDialog);
  const el = (name) => document.getElementById('flow-' + name);
  for (const field of ['position', 'rotation'])
    for (const axis of ['X', 'Y', 'Z']) {
      const label = document.createElement('label');
      label.textContent = axis;
      const input = document.createElement('input');
      input.type = 'number';
      input.step = 'any';
      input.required = true;
      input.setAttribute('aria-label', (field === 'position' ? '位置 ' : '旋转 ') + axis);
      label.append(input);
      el(field).append(label);
    }
  let status = { stage: 'not_started' },
    workspace = {},
    editor = null,
    lastPoll = 0,
    polling = false,
    saving = false;
  let history = { entries: [], errors: [] },
    activeStage = 'modeling',
    entry = null,
    plan = null,
    plannedObjectId = null,
    selectionToken = 0,
    historyKey = '',
    viewer = null,
    viewerToken = 0,
    selectedIssueId = null;
  const entrySelection = new Map();
  const modelLabel = (role) => (role ? `${role.model} · ${role.effort}` : '自动处理');
  const selectedObject = () => editor?.objects.find((obj) => obj.id === el('object').value);
  const selectedView = () => entry?.views?.find((view) => view.id === el('view').value);
  const selectedPlannedObject = () => plan?.objects.find((obj) => obj.id === plannedObjectId);
  const isBusy = () => busyStages.has(status.stage) || planningBusyStages.has(status.planning?.stage);
  const stateLabel = (value) => evidenceStates[value] || value || '待处理';
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  function fillObject() {
    const obj = selectedObject();
    el('edit-form').hidden = Boolean(editor?.draft);
    for (const field of ['position', 'rotation'])
      [...el(field).querySelectorAll('input')].forEach((input, i) => {
        input.value = obj
          ? Number((obj[field][i] * (field === 'rotation' ? 180 / Math.PI : 1)).toFixed(6))
          : '';
      });
    el('object-evidence').textContent = obj
      ? `父对象：${obj.parent_id || '场景'}。${obj.evidence.notes}`
      : '';
    el('apply').disabled =
      saving || !obj || !editor?.matches_display || editor?.editable === false || isBusy();
    el('focus').disabled = !obj;
  }

  async function refreshEditor() {
    try {
      const next = await api('/api/workspace/world-editor');
      const selected = el('object').value;
      editor = next;
      el('object').replaceChildren(
        ...editor.objects.map((obj) => new Option(obj.name + ' · ' + obj.id, obj.id)),
      );
      if (editor.objects.some((obj) => obj.id === selected)) el('object').value = selected;
      el('editor-status').textContent = editor.draft
        ? `建模中预览 v${editor.scene_revision} · ${editor.objects.length} 个对象 · 可定位、标注并提交修改意见，尚未验收`
        : `显示 v${editor.scene_revision} · 世界 v${editor.revision} · ${editor.objects.length} 个对象`;
      el('units').textContent =
        editor.units === 'm'
          ? '位置单位：米。修改父对象会带动其子对象。'
          : '位置使用重建坐标，尚未标定米制尺度。修改父对象会带动其子对象。';
      fillObject();
      showPlannedPublication();
    } catch (error) {
      editor = null;
      el('object').replaceChildren();
      fillObject();
      showPlannedPublication();
      el('editor-status').textContent = error.message;
    }
  }

  function showPlannedPublication() {
    const obj = selectedPlannedObject();
    const published = obj && editor?.matches_display && editor.objects.some((item) => item.id === obj.id);
    el('plan-focus').disabled = !published;
    el('plan-publication').textContent = !obj
      ? ''
      : published
        ? '此 ID 已存在于当前显示版本；可定位和标注，质量仍以独立审核为准。'
        : '此物体 ID 尚未出现在当前显示版本中；这里展示计划与证据。';
  }

  function showPlanView() {
    const obj = selectedPlannedObject();
    const view = obj?.evidence.find((item) => String(item.view_id) === el('plan-view').value);
    el('plan-images').hidden = !view;
    el('plan-mask-figure').hidden = !view?.has_mask;
    el('plan-view').disabled = !obj?.evidence.length;
    el('plan-image-status').textContent = !view
      ? '尚无可查看的图像证据。'
      : view.has_mask
        ? '掩码用于核对像素归属；几何和可编辑性另行验收。'
        : '此视角尚无有效掩码，物体仍保留，由建模 Agent 对照原图处理。';
    for (const kind of ['reference', 'mask']) {
      const image = el('plan-' + kind);
      image.removeAttribute('src');
      image.hidden = !view || (kind === 'mask' && !view.has_mask);
      if (image.hidden) continue;
      image.onerror = () => {
        image.hidden = true;
        el('plan-image-status').textContent = '图像证据暂不可用，请检查该物体的分割记录。';
      };
      image.src = resourceURL(
        '/api/workspace/object-plan/image?' +
          new URLSearchParams({
            object_id: obj.id,
            view_id: view.view_id,
            kind,
            run: plan.run_id,
            updated: plan.updated_at,
          }),
      );
    }
  }

  function selectPlannedObject(id) {
    plannedObjectId = id;
    const obj = selectedPlannedObject();
    for (const button of el('plan-list').querySelectorAll('button'))
      button.setAttribute('aria-pressed', String(button.dataset.objectId === id));
    el('plan-object-name').textContent = obj ? `${obj.name} · ${obj.id}` : '选择一个计划物体';
    el('plan-object-notes').textContent = obj ? `父对象：${obj.parent_id || '场景'}。${obj.notes || ''}` : '';
    const previous = el('plan-view').value;
    el('plan-view').replaceChildren(
      ...(obj?.evidence || []).map(
        (view) => new Option(view.label || `视角 ${view.view_id}`, String(view.view_id)),
      ),
    );
    if (obj?.evidence.some((view) => String(view.view_id) === previous)) el('plan-view').value = previous;
    showPlannedPublication();
    showPlanView();
  }

  function renderPlan() {
    el('plan').hidden = !plan;
    if (!plan) return;
    el('plan-state').textContent = entry.kind === 'plan' ? '初始清单' : '分割证据';
    el('plan-counts').textContent =
      `${plan.objects.length} 个初始对象 · ${plan.unresolved.length} 项初始化缺口 · 处理结果见候选审核`;
    const byId = new Map(plan.objects.map((obj) => [obj.id, obj]));
    const ordered = [],
      visited = new Set();
    function addBranch(obj, depth) {
      if (visited.has(obj.id)) return;
      visited.add(obj.id);
      ordered.push({ obj, depth });
      for (const child of plan.objects) if (child.parent_id === obj.id) addBranch(child, depth + 1);
    }
    for (const obj of plan.objects) if (!obj.parent_id || !byId.has(obj.parent_id)) addBranch(obj, 0);
    for (const obj of plan.objects) if (!visited.has(obj.id)) addBranch(obj, 0);
    el('plan-list').replaceChildren(
      ...ordered.map(({ obj, depth }) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'flow-plan-object';
        button.dataset.objectId = obj.id;
        button.style.setProperty('--object-depth', Math.min(depth, 5));
        const label = document.createElement('strong');
        label.textContent = obj.name;
        const identity = document.createElement('small');
        identity.textContent = `${obj.id}${obj.parent_id ? ` · 属于 ${obj.parent_id}` : ''}`;
        const progress = document.createElement('span');
        progress.textContent = `分割：${stateLabel(obj.segmentation_status)} · 几何：${stateLabel(obj.geometry_status)}`;
        button.append(label, identity, progress);
        button.onclick = () => selectPlannedObject(obj.id);
        return button;
      }),
    );
    if (!plan.objects.length) el('plan-list').textContent = '计划 Agent 尚未提交物体清单。';
    el('unresolved').replaceChildren(
      ...plan.unresolved.map((item) => {
        const article = document.createElement('article');
        article.className = 'flow-issue';
        const heading = document.createElement('strong');
        heading.textContent = '初始化缺口 · ' + (item.object_id || '场景');
        const reason = document.createElement('p');
        reason.textContent = item.reason;
        const action = document.createElement('p');
        action.textContent = '建模任务：' + item.next_action;
        article.append(heading, reason, action);
        if (byId.has(item.object_id)) {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = '查看物体证据';
          button.onclick = () => selectPlannedObject(item.object_id);
          article.append(button);
        }
        return article;
      }),
    );
    el('plan-search').value = '';
    selectPlannedObject(byId.has(plannedObjectId) ? plannedObjectId : plan.objects[0]?.id);
  }

  function currentStep() {
    const stage = status.planning?.stage || status.stage;
    return (
      {
        planning: 'initialization',
        planned: 'initialization',
        segmenting: 'initialization',
        segmented: 'initialization',
        geometry: 'initialization',
        ready_for_modeling: 'modeling',
        modeled: 'modeling',
        repairing: 'modeling',
        needs_revision: 'modeling',
        reviewed: 'reviewing',
        edited: 'reviewing',
        publishing: 'accepted',
      }[stage] || stage
    );
  }
  function renderBar() {
    bar.hidden = status.stage === 'not_started' && !workspace.reference_clip && !status.planning;
    bar.dataset.active = String(isBusy());
    const stage = status.planning?.stage || status.stage;
    el('stage').textContent =
      stage === 'modeling' && workspace.approvals?.length
        ? '建模等待确认'
        : planningStages[stage] || stages[stage] || stage;
    el('version').textContent = `当前场景 v${workspace.scene_revision ?? '—'}`;
    for (const button of [
      ...bar.querySelectorAll('[data-stage]'),
      ...dialog.querySelectorAll('[data-stage]'),
    ]) {
      const key = button.dataset.stage,
        count = history.entries.filter((item) => item.stage === key).length;
      button.classList.toggle('current', key === currentStep());
      button.classList.toggle('done', key === 'accepted' ? status.accepted === true : count > 0);
      button.setAttribute('aria-pressed', String(dialog.open && key === activeStage));
      button.querySelector('.flow-step-state').textContent = count
        ? String(count)
        : key === currentStep()
          ? '·'
          : '';
    }
  }
  function facts(rows) {
    el('facts').replaceChildren(
      ...rows.map(([key, value]) => {
        const item = node('div', '');
        item.append(node('small', checkLabels[key] || key), node('strong', value));
        return item;
      }),
    );
    el('facts').hidden = !rows.length;
  }
  function showView(region = null) {
    const view = selectedView();
    el('comparison').hidden = !view;
    for (const kind of ['reference', 'render']) {
      const image = el(kind),
        source = view?.[kind];
      image.closest('figure').hidden = !source;
      image.removeAttribute('src');
      image.onerror = () => {
        el('history-error').hidden = false;
        el('history-error').textContent = '该记录的图像无法读取，可能已被修改或移除。';
      };
      if (source) image.src = resourceURL(source.url);
      image.closest('figure').querySelector('figcaption').textContent = source?.label || '';
    }
    for (const box of el('comparison').querySelectorAll('.flow-region')) {
      box.hidden = !region;
      if (region)
        Object.assign(box.style, {
          left: region[0] * 100 + '%',
          top: region[1] * 100 + '%',
          width: (region[2] - region[0]) * 100 + '%',
          height: (region[3] - region[1]) * 100 + '%',
        });
    }
  }
  function stopViewer() {
    ++viewerToken;
    viewer?.dispose();
    viewer = null;
    el('model-canvas').replaceChildren();
  }
  async function showModel() {
    if (!entry?.mesh) return;
    stopViewer();
    const token = viewerToken;
    el('model-panel').hidden = false;
    el('comparison').hidden = true;
    el('view').hidden = true;
    el('show-model').setAttribute('aria-pressed', 'true');
    el('show-images').setAttribute('aria-pressed', 'false');
    el('model-status').textContent = '加载此阶段模型…';
    try {
      const { createHistoryViewer } = await import('./reconstruction-history-viewer.js');
      if (token !== viewerToken) return;
      viewer = createHistoryViewer(el('model-canvas'));
      await viewer.load(resourceURL(entry.mesh.url), entry.camera);
      if (token !== viewerToken) return;
      el('model-status').textContent = '拖动旋转 · 滚轮缩放 · 只读';
    } catch (error) {
      if (token === viewerToken) el('model-status').textContent = '模型未加载：' + error.message;
    }
  }
  function showImages() {
    stopViewer();
    el('model-panel').hidden = true;
    el('view').hidden = false;
    el('show-model').setAttribute('aria-pressed', 'false');
    el('show-images').setAttribute('aria-pressed', 'true');
    showView();
  }
  function renderItems() {
    const needle = el('items-search').value.toLowerCase();
    el('items').replaceChildren(
      ...(entry?.items || [])
        .filter((item) => `${item.id} ${item.name} ${item.detail}`.toLowerCase().includes(needle))
        .map((item) => {
          const row = node('article', '');
          row.append(
            node('strong', item.name),
            node('span', stateLabel(item.state), 'flow-badge'),
            node('p', item.detail),
          );
          return row;
        }),
    );
  }
  function selectIssue(issue) {
    selectedIssueId = issue.id;
    const detail = el('issue-detail');
    detail.replaceChildren(
      node('small', `${issue.id} · ${issue.object_id}`),
      node('h4', issueCategories[issue.category || issue.target] || '审核问题'),
      node('p', issue.evidence),
      node('h5', '建模 Agent 的复验目标'),
      node('p', issue.acceptance_criterion),
    );
    const button = node('button', '查看对应图像 ↑');
    button.type = 'button';
    button.onclick = () => {
      el('view').value = issue.view_id;
      showImages();
      showView(issue.region);
      el('result').scrollIntoView({ block: 'start', behavior: 'smooth' });
    };
    detail.append(button);
    for (const item of el('issues').querySelectorAll('button'))
      item.setAttribute('aria-pressed', String(item.dataset.issueId === issue.id));
  }
  function renderIssues() {
    const filter = el('issue-filter').value,
      issues = (entry?.issues || []).filter(
        (issue) => !filter || (issue.category || issue.target) === filter,
      );
    el('issues').replaceChildren(
      ...issues.map((issue) => {
        const button = node('button', '');
        button.type = 'button';
        button.dataset.issueId = issue.id;
        button.append(
          node(
            'small',
            `${{ blocking: '阻塞', major: '主要', minor: '次要' }[issue.severity]} · ${issueCategories[issue.category || issue.target] || '审核问题'}`,
          ),
          node('strong', entry.object_names?.[issue.object_id] || issue.object_id),
          node('span', issue.evidence),
        );
        button.onclick = () => selectIssue(issue);
        return button;
      }),
    );
    const chosen = issues.find((issue) => issue.id === selectedIssueId) || issues[0];
    if (chosen) selectIssue(chosen);
    else el('issue-detail').replaceChildren(node('p', '此类别没有问题。', 'flow-muted'));
  }
  function sourceFrame() {
    const frames = workspace.reference_clip?.frames || [],
      index = Number(el('source-scrub').value),
      frame = frames[index];
    el('source-time').textContent = `${index + 1} / ${frames.length}`;
    el('source-frame').removeAttribute('src');
    if (frame?.url) el('source-frame').src = resourceURL(frame.url);
  }
  async function selectEntry(id) {
    const token = ++selectionToken;
    stopViewer();
    entry = history.entries.find((item) => item.id === id) || null;
    plan = null;
    if (entry) entrySelection.set(activeStage, entry.id);
    for (const name of [
      'reference-panel',
      'result',
      'plan',
      'review-panel',
      'items-panel',
      'documents-panel',
      'empty',
      'model-panel',
    ])
      el(name).hidden = true;
    el('history-error').hidden = true;
    el('items-search').value = '';
    el('items-panel').open = false;
    el('documents-panel').open = false;
    const label = pipelineSteps.find(([key]) => key === activeStage)?.[1] || '流程记录';
    el('title').textContent = entry?.title || label;
    el('entry-meta').textContent = entry
      ? `${timeLabel(entry.recorded_at)} · ${entry.actor ? modelLabel(entry.actor) : '阶段记录'}`
      : '过程与产物';
    el('summary').textContent = entry
      ? entry.verdict
        ? `${verdictLabels[entry.verdict]} · ${entry.issues.length} 项问题。${entry.summary}`
        : entry.summary
      : '';
    facts(entry?.facts || []);
    if (activeStage === 'reference') {
      const frames = workspace.reference_clip?.frames || [];
      el('reference-panel').hidden = !frames.length;
      el('source-scrub').max = Math.max(0, frames.length - 1);
      sourceFrame();
      el('summary').textContent = frames.length
        ? '重建使用的原始输入。相机与图像保持对应，后续各阶段均可回到原图核对。'
        : '尚未导入参考资料。';
      facts([
        ['原始帧', String(frames.length)],
        ['相机', frames.some((frame) => frame.camera) ? '已关联' : '以输入记录为准'],
      ]);
      return;
    }
    if (!entry) {
      el('empty').hidden = false;
      el('empty').textContent =
        activeStage === 'accepted'
          ? status.accepted
            ? '当前结果已通过审核，正式成果见主工作台。'
            : '尚未验收交付。建模候选和历史审核分别保留在对应阶段。'
          : '这个阶段尚无已保存记录；完成后会显示在这里。';
      return;
    }
    if (entry.views?.length || entry.mesh) {
      el('result').hidden = false;
      el('view').replaceChildren(...entry.views.map((view) => new Option(view.id, view.id)));
      el('show-model').disabled = !entry.mesh;
      el('show-images').disabled = !entry.views.length;
      if (entry.views.length) showImages();
      else await showModel();
    }
    if (token !== selectionToken) return;
    if (entry.stage === 'reviewing') {
      el('review-panel').hidden = false;
      el('issue-count').textContent = String(entry.issues.length);
      const cats = [...new Set(entry.issues.map((issue) => issue.category || issue.target))];
      el('issue-filter').replaceChildren(
        new Option('全部问题', ''),
        ...cats.map((key) => new Option(issueCategories[key] || key, key)),
      );
      renderIssues();
      facts([
        ...(entry.facts || []),
        ...Object.entries(entry.dimensions || {}).map(([key, value]) => [
          checkLabels[key] || key,
          { passed: '通过', failed: '未通过', unknown: '待核实' }[value],
        ]),
      ]);
    }
    el('items-panel').hidden = !entry.items?.length;
    el('items-label').textContent =
      `${entry.stage === 'modeling' ? '建模处理' : '对象'}明细 · ${entry.items?.length || 0}`;
    renderItems();
    el('documents-panel').hidden = !entry.documents?.length;
    el('documents').replaceChildren(
      ...entry.documents.map((doc) => {
        const link = node('a', doc.label + ' ↗');
        link.href = resourceURL(doc.url);
        link.target = '_blank';
        link.rel = 'noopener';
        return link;
      }),
    );
    if (entry.stage === 'initialization' && ['plan', 'segments'].includes(entry.kind) && entry.run_id) {
      try {
        const next = await api('/api/workspace/object-plan?' + new URLSearchParams({ run: entry.run_id }));
        if (token !== selectionToken) return;
        plan = next;
        renderPlan();
      } catch (error) {
        if (token === selectionToken) {
          el('history-error').hidden = false;
          el('history-error').textContent = error.message;
        }
      }
    }
  }
  async function selectStage(stage) {
    activeStage = stage;
    dialog.querySelector('.flow-content').scrollTop = 0;
    const entries = history.entries.filter((item) => item.stage === stage);
    el('history-version').replaceChildren(
      ...entries.map((item) => new Option(`${item.title} · ${timeLabel(item.recorded_at)}`, item.id)),
    );
    const selected =
      entries.find((item) => item.id === entrySelection.get(stage)) ||
      (stage === 'initialization' ? entries.find((item) => item.mesh) : null) ||
      entries.at(-1);
    el('history-version').value = selected?.id || '';
    el('history-version').closest('label').hidden = !entries.length;
    renderBar();
    await selectEntry(selected?.id);
  }
  async function refreshHistory() {
    history = await api('/api/workspace/reconstruction/history');
    renderBar();
    if (!dialog.open) return;
    await selectStage(activeStage);
    if (history.errors.length) {
      el('history-error').hidden = false;
      el('history-error').textContent = `${history.errors.length} 条历史记录不完整，其他阶段仍可查看。`;
    }
  }
  async function openStage(stage) {
    if (!dialog.open) dialog.showModal();
    activeStage = stage;
    el('summary').textContent = '读取阶段记录…';
    await refreshEditor();
    try {
      await refreshHistory();
    } catch (error) {
      el('history-error').hidden = false;
      el('history-error').textContent = '历史记录读取失败：' + error.message;
    }
  }
  async function refresh(nextWorkspace, force = false) {
    workspace = nextWorkspace;
    if (polling || (!force && Date.now() - lastPoll < 5000)) return;
    polling = true;
    lastPoll = Date.now();
    try {
      status = await api('/api/workspace/reconstruction');
      renderBar();
      if ((dialog.open || editorDialog.open) && editor?.scene_revision !== workspace.scene_revision)
        await refreshEditor();
      const key = JSON.stringify([status.updated_at, status.planning?.updated_at, workspace.scene_revision]);
      if (key !== historyKey) {
        historyKey = key;
        await refreshHistory();
      }
    } catch (error) {
      el('stage').textContent = '流程状态暂不可用';
      el('history-error').textContent = error.message;
      el('history-error').hidden = false;
    } finally {
      polling = false;
    }
  }
  for (const button of [...bar.querySelectorAll('[data-stage]'), ...dialog.querySelectorAll('[data-stage]')])
    button.onclick = () =>
      dialog.open ? selectStage(button.dataset.stage) : openStage(button.dataset.stage);
  el('close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    ++selectionToken;
    stopViewer();
    renderBar();
  });
  el('history-version').onchange = () => selectEntry(el('history-version').value);
  el('view').onchange = () => showView();
  el('show-images').onclick = showImages;
  el('show-model').onclick = showModel;
  el('model-reset').onclick = () => viewer?.reset();
  el('source-scrub').oninput = sourceFrame;
  el('issue-filter').onchange = renderIssues;
  el('items-search').oninput = renderItems;
  el('plan-search').oninput = () => {
    const needle = el('plan-search').value.toLowerCase();
    for (const row of el('plan-list').children) row.hidden = !row.textContent.toLowerCase().includes(needle);
  };
  el('plan-view').onchange = showPlanView;
  el('plan-focus').onclick = () => {
    const obj = selectedPlannedObject();
    if (obj && !el('plan-focus').disabled) {
      dialog.close();
      focusObject(obj.id);
    }
  };
  el('editor-open').onclick = async () => {
    editorDialog.showModal();
    await refreshEditor();
  };
  el('editor-close').onclick = () => editorDialog.close();
  el('object').onchange = fillObject;
  el('reset').onclick = fillObject;
  el('focus').onclick = () => {
    if (selectedObject()) {
      editorDialog.close();
      focusObject(selectedObject().id);
    }
  };
  el('edit-form').onsubmit = async (event) => {
    event.preventDefault();
    const obj = selectedObject();
    if (!obj || saving) return;
    saving = true;
    el('apply').disabled = true;
    el('edit-result').textContent = '正在构建并保存…';
    const vector = (field) =>
      [...el(field).querySelectorAll('input')].map(
        (input) => Number(input.value) * (field === 'rotation' ? Math.PI / 180 : 1),
      );
    try {
      const result = await api('/api/workspace/world-editor', {
        method: 'POST',
        body: {
          expected_revision: editor.revision,
          expected_scene_revision: editor.scene_revision,
          object_id: obj.id,
          position: vector('position'),
          rotation: vector('rotation'),
        },
      });
      await loadScene(result.scene);
      await refreshEditor();
      await refresh({ ...workspace, scene_revision: result.scene.revision }, true);
      el('edit-result').textContent = '已保存。当前编辑版本仍需复核。';
    } catch (error) {
      el('edit-result').textContent = '未完成：' + error.message;
    } finally {
      saving = false;
      fillObject();
    }
  };
  return { refresh };
}
