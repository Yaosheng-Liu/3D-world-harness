// The floating tool dock reuses the real controls; scene/evidence state stays in app.js.
export function setupWorkspaceChrome({getState, setMode, activateToolPane, revealReference}) {
  const byId = id => document.getElementById(id);
  const root = document.documentElement;
  const dock = byId('annotation-tool-panel');
  const reference = byId('reference-pane');
  const referenceMedia = byId('reference-media');
  const referenceStage = byId('reference-stage');
  const sceneStage = byId('scene-stage');
  const referenceCanvas = byId('reference-annotations');
  const sceneViewport = byId('viewport');
  const sceneMedia = byId('scene-snapshot-media');
  const textEditor = byId('text-editor');
  const scene = document.querySelector('.scene-pane');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let target = 'scene';
  let opened = false;
  let previousMode = 'select';
  let previousView = 'live';
  let animation = null;
  let frame = 0;
  let closeTimer = null;
  let settleTimer = null;
  let geometrySettling = false;
  let placed = null;
  const automatic = {
    reference:{stage:referenceStage,media:[referenceMedia],upperHover:false,pointer:null,touch:false,dismissed:false,suppressFocus:false},
    scene:{stage:sceneStage,media:[sceneViewport,sceneMedia],upperHover:false,pointer:null,touch:false,dismissed:false,suppressFocus:false}
  };
  const activePointers = new Set();

  function paneReady(pane) {
    const state = getState();
    if (pane === 'reference') return state.workspaceReady && state.activeReferenceId && !reference.inert && !referenceMedia.classList.contains('hidden');
    return state.workspaceReady && Number.isInteger(state.sceneRevision) && !state.sceneLoading;
  }
  function gestureActive() {
    const state = getState();
    return activePointers.size || state.drag || state.referencePanning || state.snapshotPanning || state.poseEditDrag || state.textPending;
  }
  function placementLocked() {
    return gestureActive() || geometrySettling || dock.matches(':hover') ||
      (dock.contains(document.activeElement) && document.activeElement.matches(':focus-visible')) || dock.querySelector('details[open]');
  }
  function inPaneArea(pane,element) {
    return !!element && (automatic[pane].stage.contains(element) || dock.contains(element) || textEditor.contains(element));
  }
  function paneAreaActive(pane) {
    const focused = document.activeElement;
    return automatic[pane].upperHover || dock.matches(':hover') || textEditor.matches(':hover') ||
      (focused?.matches(':focus-visible') && inPaneArea(pane,focused));
  }
  function cancelAutomaticClose() { clearTimeout(closeTimer); closeTimer = null; }
  function scheduleAutomaticClose() {
    cancelAutomaticClose();
    if (!opened || automatic[target].touch) return;
    closeTimer = setTimeout(() => {
      closeTimer = null;
      if (!opened || automatic[target].touch || paneAreaActive(target)) return;
      if (gestureActive()) { scheduleAutomaticClose(); return; }
      close({resetTool:false});
    },220);
  }
  function openAutomatically(pane) {
    if (!paneReady(pane) || gestureActive() || automatic[pane].dismissed) return;
    if (opened && target !== pane && placementLocked()) return;
    cancelAutomaticClose();
    if (!opened || target !== pane) open(pane);
  }

  function upperArea(pane) {
    const anchor = automatic[pane].stage.getBoundingClientRect();
    const immersiveScene = root.dataset.layout === 'immersive' && pane === 'scene';
    const headerBottom = immersiveScene
      ? Math.max(scene.querySelector('.pane-head').getBoundingClientRect().bottom,
                 document.querySelector('.topbar').getBoundingClientRect().bottom) : 0;
    return {anchor, top:Math.max(10, anchor.top + 10, headerBottom + (headerBottom ? 10 : 0))};
  }
  function visibleViewport() {
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    return {left,top,right:left + (viewport?.width || innerWidth),bottom:top + (viewport?.height || innerHeight)};
  }
  function spareAreas(pane) {
    const {anchor,top} = upperArea(pane), viewport = visibleViewport();
    const safe = {left:Math.max(anchor.left + 10,viewport.left + 10), right:Math.min(anchor.right - 10,viewport.right - 10),
      top:Math.max(top,viewport.top + 10),bottom:Math.min(anchor.bottom - 10,viewport.bottom - 12)};
    const media = pane === 'reference' ? referenceMedia : getState().sceneView === 'snapshot' ? sceneMedia : sceneViewport;
    const bitmap = media.getBoundingClientRect();
    // Measure only the visible bitmap/renderer, including its zoom and pan.
    // The hidden live renderer must not consume a screenshot's letterbox.
    const content = {left:Math.max(anchor.left,bitmap.left),right:Math.min(anchor.right,bitmap.right),
      top:Math.max(anchor.top,bitmap.top),bottom:Math.min(anchor.bottom,bitmap.bottom)};
    const empty = content.right <= content.left || content.bottom <= content.top;
    return {safe,viewport,
      top:{...safe,bottom:empty ? safe.bottom : Math.min(safe.bottom,content.top - 8)},
      right:{...safe,left:empty ? safe.left : Math.max(safe.left,content.right + 8)}};
  }
  function inUpperArea(pane, pointer) {
    if (!pointer) return false;
    const {anchor,top} = upperArea(pane);
    const upper = pointer.clientX >= anchor.left && pointer.clientX <= anchor.right &&
      pointer.clientY >= Math.max(anchor.top,top - 10) && pointer.clientY <= Math.min(anchor.bottom,top + 70);
    if (upper) return true;
    const areas = spareAreas(pane);
    // Keep the familiar top entry; a larger letterbox also stays an entry area.
    return ['top','right'].some(side => {
      const area = areas[side];
      return area.right - area.left >= (side === 'right' ? 64 : 120) && area.bottom - area.top >= (side === 'right' ? 280 : 32) &&
        pointer.clientX >= area.left && pointer.clientX <= area.right && pointer.clientY >= area.top && pointer.clientY <= area.bottom;
    });
  }
  function refreshUpperHover() {
    for (const [pane,settings] of Object.entries(automatic)) settings.upperHover = inUpperArea(pane,settings.pointer);
  }
  function position() {
    if (!opened) return;
    refreshUpperHover();
    if (!paneAreaActive(target) && !closeTimer) scheduleAutomaticClose();
    const areas = spareAreas(target), viewport = areas.viewport;
    let rect = {width:dock.offsetWidth,height:dock.offsetHeight};
    const locked = placed?.pane === target && placementLocked() &&
      rect.width <= viewport.right - viewport.left - 20 && rect.height <= viewport.bottom - viewport.top - 24;
    if (!locked) {
      const availableWidth = Math.max(60, Math.min(areas.safe.right - areas.safe.left,viewport.right - viewport.left - 20));
      const measure = placement => {
        dock.dataset.placement = placement;
        dock.style.setProperty('--annotation-dock-max-width', `${Math.round(availableWidth)}px`);
        dock.classList.toggle('is-compact', placement === 'right' || availableWidth < 780);
        // Entry animation scales the painted box; use its final layout size.
        const bounds = {width:dock.offsetWidth,height:dock.offsetHeight}, area = areas[placement];
        const width = area.right - area.left, height = area.bottom - area.top;
        return {placement,area,width:bounds.width,height:bounds.height,
          fits:width >= bounds.width && height >= bounds.height,
          score:Math.min(width / bounds.width,height / bounds.height)};
      };
      const row = measure('top'), column = measure('right');
      let chosen = row.fits && column.fits ? (column.score > row.score ? column : row) : column.fits ? column : row;
      const previous = placed?.pane === target && (placed.placement === 'right' ? column : row);
      // Keep a fitting side through small changes near the decision boundary.
      if (previous?.fits && chosen.fits && chosen.score < previous.score + .15) chosen = previous;
      dock.dataset.placement = chosen.placement;
      dock.classList.toggle('is-compact', chosen.placement === 'right' || availableWidth < 780);
      rect = {width:dock.offsetWidth,height:dock.offsetHeight};
      const area = chosen.fits ? chosen.area : areas.safe;
      const left = area.left + (area.right - area.left - rect.width) / 2;
      const y = chosen.fits ? area.top + (area.bottom - area.top - rect.height) / 2 : area.top;
      placed = {pane:target,placement:chosen.placement,left,top:y};
    }
    const left = Math.max(viewport.left + 10,Math.min(viewport.right - rect.width - 10,placed.left));
    const y = Math.max(viewport.top + 10,Math.min(viewport.bottom - rect.height - 12,placed.top));
    dock.style.left = `${Math.round(left)}px`;
    dock.style.top = `${Math.round(y)}px`;
    for (const details of dock.querySelectorAll('details[open]')) {
      const popup = details.querySelector('.popover-content');
      popup.style.setProperty('--annotation-popover-x', '0px');
      popup.style.setProperty('--annotation-popover-y', '0px');
      const bounds = popup.getBoundingClientRect();
      const dx = Math.min(0, viewport.right - 12 - bounds.right) + Math.max(0, viewport.left + 12 - bounds.left);
      const dy = Math.min(0, viewport.bottom - 12 - bounds.bottom) + Math.max(0, viewport.top + 12 - bounds.top);
      popup.style.setProperty('--annotation-popover-x', `${Math.round(dx)}px`);
      popup.style.setProperty('--annotation-popover-y', `${Math.round(dy)}px`);
    }
  }

  function schedulePosition() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; position(); });
  }
  function render() {
    dock.dataset.context = target;
    root.dataset.annotationTarget = opened ? target : '';
    root.dataset.toolsVisible = String(opened);
    dock.inert = !opened;
    dock.setAttribute('aria-hidden', String(!opened));
    const liveScene = target === 'scene' && getState().sceneView === 'live';
    dock.setAttribute('aria-label', target === 'reference' ? '参考图标注工具' : liveScene ? '场景标注工具' : '场景截图标注工具');
    byId('annotation-context-label').textContent = target === 'reference' ? '参考' : liveScene ? '场景' : '截图';
    byId('scene-labels-toggle').hidden = target !== 'scene';
    byId('pose-edit-tool').hidden = target !== 'reference';
    byId('livewire-tool').hidden = target !== 'reference';
    reference.classList.toggle('is-annotation-target', opened && target === 'reference');
    scene.classList.toggle('is-annotation-target', opened && target === 'scene');
    dock.hidden = !opened;
    position();
  }
  function open(pane, {toggle=false}={}) {
    cancelAutomaticClose();
    if (toggle && opened && target === pane) { close({focus:true}); return; }
    if (pane === 'reference' && reference.inert) revealReference?.();
    if (pane !== target) { automatic[target].touch = false; placed = null; }
    const wasOpen = opened;
    target = pane; opened = true;
    activateToolPane?.(pane);
    animation?.cancel(); render();
    if (!wasOpen && !reduced.matches) animation = dock.animate([
      {opacity:0, translate:'0 -6px'}, {opacity:1, translate:'0 0'}
    ], {duration:240,easing:'cubic-bezier(.22,1,.36,1)'});
  }
  function close({focus=false, resetTool=true}={}) {
    cancelAutomaticClose();
    if (!opened) return;
    const trigger = target === 'reference' ? referenceCanvas : getState().sceneView === 'live' ? sceneViewport.querySelector('canvas') : byId('scene-annotations');
    automatic[target].dismissed = automatic[target].upperHover || dock.matches(':hover');
    automatic[target].touch = false;
    animation?.cancel();
    opened = false;
    placed = null;
    if (resetTool && getState().mode !== 'select') setMode('select',target);
    render();
    if (focus) {
      automatic[target].suppressFocus = true;
      (trigger || byId('scene-live-card')).focus({preventScroll:true});
      automatic[target].suppressFocus = false;
    }
  }
  function syncState() {
    const state = getState();
    byId('selection-level-switch').hidden = state.sceneView !== 'live';
    if (opened && !paneReady(target)) close({resetTool:false});
    const viewChanged = previousView !== state.sceneView;
    const newTool = state.mode !== 'select' && previousMode !== state.mode;
    previousView = state.sceneView; previousMode = state.mode;
    if (newTool && !opened) open(state.toolPane === 'reference' && !reference.inert ? 'reference' : 'scene');
    else if (viewChanged) render();
    if (viewChanged) scheduleAutomaticClose();
    schedulePosition();
  }
  function layoutChanged() {
    if (opened && target === 'reference' && reference.inert) close({resetTool:false});
    schedulePosition();
  }
  for (const [pane,settings] of Object.entries(automatic)) {
    const hoverUpper = event => {
      if (event.pointerType !== 'mouse') return;
      const wasUpper = settings.upperHover;
      settings.pointer = {clientX:event.clientX, clientY:event.clientY};
      settings.upperHover = inUpperArea(pane,settings.pointer);
      if (settings.upperHover) {
        settings.touch = false; openAutomatically(pane);
      } else {
        settings.dismissed = false;
        if (wasUpper || opened && target === pane && !closeTimer) scheduleAutomaticClose();
      }
    };
    settings.stage.addEventListener('pointerenter', hoverUpper);
    settings.stage.addEventListener('pointermove', hoverUpper);
    settings.stage.addEventListener('wheel', () => {
      geometrySettling = true; clearTimeout(settleTimer);
      settleTimer = setTimeout(() => { geometrySettling = false; schedulePosition(); },140);
    },{passive:true,capture:true});
    settings.stage.addEventListener('pointerleave', event => {
      if (event.pointerType !== 'mouse') return;
      settings.pointer = null; settings.upperHover = false;
      settings.dismissed = false; scheduleAutomaticClose();
    });
    for (const media of settings.media) {
      media.addEventListener('focusin', event => {
        if (settings.suppressFocus || !event.target.matches(':focus-visible')) return;
        settings.dismissed = false; openAutomatically(pane);
      });
      media.addEventListener('focusout', scheduleAutomaticClose);
    }
  }
  for (const element of [dock,textEditor]) {
    element.addEventListener('pointerenter', cancelAutomaticClose);
    element.addEventListener('pointerleave', event => { if (event.pointerType === 'mouse') scheduleAutomaticClose(); });
    element.addEventListener('focusin', cancelAutomaticClose);
    element.addEventListener('focusout', scheduleAutomaticClose);
  }
  byId('annotation-tools-close').addEventListener('click', () => close({focus:true}));
  document.addEventListener('click', event => { if (event.target.closest('#scene-live-card')) close(); });
  // Top/right entry areas stay separate from image gestures, so drawing or
  // rotating in the rest of the image never reopens the toolbar.
  document.addEventListener('pointerdown', event => {
    const gesturePane = event.target.closest('#reference-media') ? 'reference'
      : event.target.closest('#scene-snapshot-media, #viewport canvas') ? 'scene' : null;
    const triggerPane = event.target.closest('#reference-stage') ? 'reference'
      : event.target.closest('#scene-stage') ? 'scene' : null;
    if (opened && !inPaneArea(target,event.target) && !gestureActive()) close({resetTool:false});
    if (triggerPane && inUpperArea(triggerPane,event)) {
      automatic[triggerPane].dismissed = false;
      automatic[triggerPane].touch = event.pointerType !== 'mouse';
      openAutomatically(triggerPane);
    }
    if (gesturePane || dock.contains(event.target)) activePointers.add(event.pointerId);
  }, {capture:true});
  for (const type of ['pointerup','pointercancel']) document.addEventListener(type, event => {
    activePointers.delete(event.pointerId); scheduleAutomaticClose(); schedulePosition();
  }, {capture:true});
  window.addEventListener('blur', () => { activePointers.clear(); scheduleAutomaticClose(); });
  for (const details of document.querySelectorAll('details.popover')) details.addEventListener('toggle', schedulePosition);
  dock.addEventListener('animationend',schedulePosition);
  const pill = byId('session-pill');
  const syncStatus = () => { pill.title = pill.textContent; pill.setAttribute('aria-label', pill.textContent); };
  new MutationObserver(syncStatus).observe(pill,{childList:true,characterData:true,subtree:true});
  syncStatus();
  const observer = new ResizeObserver(schedulePosition);
  for (const element of [reference,referenceStage,referenceMedia,sceneStage,sceneViewport,sceneMedia,dock]) observer.observe(element);
  for (const image of [byId('reference-image'),byId('scene-snapshot-image')]) image.addEventListener('load',schedulePosition);
  window.addEventListener('resize', schedulePosition);
  window.addEventListener('scroll', schedulePosition, {passive:true});
  window.visualViewport?.addEventListener('resize',schedulePosition);
  window.visualViewport?.addEventListener('scroll',schedulePosition);
  render(); syncState();
  return {open, close, syncState, layoutChanged};
}
