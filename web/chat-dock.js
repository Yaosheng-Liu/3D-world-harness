import {createChatSectionMotion,setupChatPanelSizing} from './chat-sections.js';
export function setupChatDock({getState}) {
  const byId = (id) => document.getElementById(id);
  const dock = byId('chat-dock');
  const moveHandle = byId('chat-dock-move');
  const launcher = byId('chat-launcher');
  const history = byId('chat-history');
  const conversation = byId('conversation');
  const historyToggle = byId('chat-history-toggle');
  const latest = byId('chat-latest');
  const resizeHandle = byId('chat-resize-handle');
  let sessionId = null;
  let collapsed = false;
  let historyCollapsed = true;
  let followingLatest = true;
  let savedScrollTop = 0;
  let unread = 0;
  let dockHeight = null;
  let resizeDrag = null;
  let moveDrag = null;
  let dockPositions = {};
  let resizeFrame = 0;
  let lastHeightBounds = {min:1, max:window.innerHeight};
  let launcherStatus = '';
  let sectionMotion=null,panelSizing=null;
  let compact=true,previewAnimation=null,previewCloseTimer=null,dropActive=false,dropDepth=0,previewRevealTarget=null;
  let mousePoint=null,inputMode='mouse',pointerPress=false,composing=false,editUntil=0,editTimer=null;
  const visibilityTransitions = new Map();
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const historyVisible = () => !!dock && !collapsed && !compact && !historyCollapsed;
  const atLatest = () => !conversation ||
    conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 32;
  const storageKey = () => 'astra-visual-layout:' + sessionId;

  function persist() {
    if (!sessionId) return;
    try {
      localStorage.setItem(storageKey(), JSON.stringify({collapsed, historyCollapsed, dockHeight, dockPositions}));
    } catch { /* Layout controls remain usable when browser storage is unavailable. */ }
  }

  const positionLayout = () => document.documentElement.dataset.layout === 'immersive' ? 'immersive' : 'compare';
  const customPosition = () => dockPositions[positionLayout()];
  const popupNodes = () => [byId('prompt-attach-menu'),byId('prompt-mentions')].filter(Boolean);
  const editable = element => !!element?.matches('input,textarea,select,[contenteditable=true]');
  const popupVisible = element => !element.hidden && !element.inert && !element.classList.contains('hidden');
  const mouseWithin = element => !!mousePoint && element.contains(document.elementFromPoint(mousePoint.x,mousePoint.y));
  function previewHeld() {
    const active=document.activeElement;
    const state=getState() || {};
    const pending=(state.approvals || []).length || (state.queue || []).some(item =>
      ['blocked_stale','delivery_uncertain'].includes(item.status) || item.status==='failed' && !item.turn_id);
    const surfaces=[dock,...popupNodes().filter(popupVisible)];
    const focusWithin=surfaces.some(element=>element.contains(active));
    const focusHeld=document.hasFocus() && focusWithin && (inputMode==='keyboard' || inputMode==='touch' && editable(active));
    const editing=focusWithin && editable(active) && (composing || performance.now()<editUntil);
    return surfaces.some(mouseWithin) || focusHeld || editing || pointerPress ||
      popupNodes().some(element=>popupVisible(element) && element.getAttribute('aria-busy')==='true') ||
      !!document.querySelector('dialog[open], .prompt-reference-popover:not([hidden])') || pending || state.agent?.status==='awaiting_approval' ||
      moveDrag || resizeDrag || dock.classList.contains('panel-resizing') || dropActive;
  }
  function renderPreview() {
    dock.classList.toggle('is-compact',compact);dock.classList.toggle('is-expanded',!compact);
    const historyHidden=historyCollapsed || compact;
    history.inert=historyHidden;history.setAttribute('aria-hidden',String(historyHidden));
    historyToggle.setAttribute('aria-expanded',String(!historyHidden));
    const evidence=byId('feedback-evidence'),toggle=byId('feedback-evidence-summary');
    const evidenceHidden=compact || !(panelSizing?.opened ?? !evidence.classList.contains('hidden'));
    evidence.inert=evidenceHidden;evidence.setAttribute('aria-hidden',String(evidenceHidden));
    const wasOpen=toggle.getAttribute('aria-expanded')==='true';
    toggle.setAttribute('aria-expanded',String(!evidenceHidden));
    toggle.title=evidenceHidden?'展开本次反馈':'收起本次反馈';
    if(!evidenceHidden && !wasOpen)evidence.dispatchEvent(new CustomEvent('evidence-visibility',{detail:{open:true}}));
    const canResize=!compact && !collapsed && !historyCollapsed;
    resizeHandle.classList.toggle('hidden',!canResize);resizeHandle.tabIndex=canResize?0:-1;
    resizeHandle.setAttribute('aria-disabled',String(!canResize));
    updateCounts();
  }
  function finishPreview() {
    previewAnimation?.cancel();previewAnimation=null;
    dock.classList.remove('preview-animating','preview-measuring');
  }
  function foldHistory() {
    // Each new preview shows the composer. History is opened explicitly.
    historyCollapsed=true;
    history.classList.add('hidden');dock.classList.add('history-collapsed');
  }
  function setCompact(value,{animate=true}={}) {
    clearTimeout(previewCloseTimer);previewCloseTimer=null;
    if(value===compact) {if(!animate) {finishPreview();applyHeight();}return;}
    rememberScroll();
    const before=dock.getBoundingClientRect(),beforeMax=getComputedStyle(dock).maxHeight;
    finishPreview();sectionMotion?.finish();
    dock.classList.add('preview-measuring');
    if(value)foldHistory();
    compact=value;renderPreview();
    if(compact)document.dispatchEvent(new CustomEvent('chat-preview-compact'));
    if(!compact)panelSizing?.fit({notify:false});
    applyHeight({restoreScroll:false});applyPosition();
    const after=dock.getBoundingClientRect(),afterMax=getComputedStyle(dock).maxHeight;
    dock.classList.remove('preview-measuring');
    if(!animate || collapsed || reducedMotion.matches || !before.width || !dock.animate) {
      if(!compact)restoreHistoryScroll();return;
    }
    const from={width:before.width+'px',height:before.height+'px',maxHeight:beforeMax};
    const to={width:after.width+'px',height:after.height+'px',maxHeight:afterMax};
    if(customPosition()) {Object.assign(from,{left:before.left+'px',top:before.top+'px'});Object.assign(to,{left:after.left+'px',top:after.top+'px'});}
    dock.classList.add('preview-animating');
    const animation=dock.animate([from,to],{duration:320,easing:'cubic-bezier(.22,.68,.2,1)',fill:'both'});
    previewAnimation=animation;
    animation.finished.then(()=>{
      if(previewAnimation!==animation)return;
      finishPreview();applyPosition();if(!compact){panelSizing?.fit({notify:false});applyHeight();restoreHistoryScroll();}
    }).catch(()=>{});
  }
  function scheduleCompact() {
    if(compact || collapsed || previewCloseTimer!==null)return;
    previewCloseTimer=setTimeout(()=>{
      previewCloseTimer=null;
      if(!previewHeld())setCompact(true);
    },260);
  }
  function expandPreview({animate=true}={}) {setCompact(false,{animate});}
  function editingActivity() {
    editUntil=performance.now()+900;
    clearTimeout(editTimer);editTimer=setTimeout(()=>{editTimer=null;scheduleCompact();},900);
    expandPreview();scheduleCompact();
  }
  function viewportBounds() {
    const viewport=window.visualViewport;
    const left=viewport?.offsetLeft || 0,top=viewport?.offsetTop || 0;
    return {left:left+10,top:top+24,right:left+(viewport?.width || innerWidth)-10,
      bottom:top+(viewport?.height || innerHeight)-12};
  }
  function positionedBounds() {
    // Keep the drag header reachable while allowing a little of each edge offscreen.
    const viewport=viewportBounds(),width=dock.offsetWidth,height=dock.offsetHeight;
    const headerHeight=Math.min(height,moveHandle?.offsetHeight || 32);
    const headerVisible=Math.min(24,headerHeight);
    const horizontal=Math.min(160,width*.25);
    const bottom=Math.min(96,height*.25);
    const top=Math.min(16,Math.max(0,headerHeight-headerVisible));
    return {
      left:viewport.left-10-horizontal,right:viewport.right+10-width+horizontal,
      top:viewport.top-24-top,
      bottom:Math.min(viewport.bottom+12+bottom-height,viewport.bottom+12-headerVisible),
      // Moving lower must not shrink the input or the saved history height.
      maxHeight:Math.max(1,viewport.bottom-viewport.top)
    };
  }
  function applyPosition() {
    if (!dock) return;
    const preferred=customPosition();
    if (!preferred) {
      dock.classList.remove('is-positioned');
      for(const property of ['--chat-left','--chat-top','--chat-position-max-height']) dock.style.removeProperty(property);
      return;
    }
    if (collapsed && dock.classList.contains('hidden')) return;
    const bounds=positionedBounds();
    const left=Math.max(bounds.left,Math.min(bounds.right,preferred.left));
    const top=Math.max(bounds.top,Math.min(bounds.bottom,preferred.top));
    for(const [property,value] of Object.entries({'--chat-left':left,'--chat-top':top,
      '--chat-position-max-height':bounds.maxHeight})) {
      const pixels=Math.round(value)+'px';
      if(dock.style.getPropertyValue(property)!==pixels) dock.style.setProperty(property,pixels);
    }
    dock.classList.add('is-positioned');
  }
  function finishMove({cancel=false}={}) {
    if (!moveDrag) return;
    const {pointerId,layout,previous,moved}=moveDrag;
    moveDrag=null;dock.classList.remove('moving');
    if(cancel) {
      if(previous) dockPositions[layout]=previous;
      else delete dockPositions[layout];
    } else if(moved) {
      const rect=dock.getBoundingClientRect();
      dockPositions[layout]={left:Math.round(rect.left),top:Math.round(rect.top)};
    }
    if(moveHandle.hasPointerCapture(pointerId)) moveHandle.releasePointerCapture(pointerId);
    applyPosition();scheduleHeightUpdate();persist();scheduleCompact();
  }
  function settleForMove() {
    expandPreview({animate:false});
    sectionMotion?.finish();panelSizing?.finishResize();finishResize();
    if ([dock,launcher].some(element=>visibilityTransitions.get(element)?.animation)) applyLayout();
  }
  function resetPosition() {
    finishMove({cancel:true});settleForMove();
    delete dockPositions[positionLayout()];
    applyPosition();panelSizing?.fit({notify:false});applyHeight();persist();
  }

  function updateCounts() {
    const count = conversation?.querySelectorAll('.conversation-item').length || 0;
    const messageCount = byId('chat-message-count');
    if (messageCount) {
      messageCount.textContent = (count ? count + ' 条' : '') + (unread ? ' · ' + unread + ' 新' : '');
      messageCount.classList.toggle('has-unread', !!unread);
    }
    const unreadCount = byId('chat-unread-count');
    if (unreadCount) {
      unreadCount.textContent = String(unread);
      unreadCount.classList.toggle('hidden', !unread);
    }
    if (launcher) {
      launcher.setAttribute('aria-label', '打开会话' + (launcherStatus ? '，' + launcherStatus : '') +
        (unread ? '，' + unread + ' 条新消息' : ''));
      launcher.classList.toggle('has-unread', !!unread);
    }
    if (historyToggle) {
      const label = compact ? '记录' : historyCollapsed ? '展开记录' : '收起记录';
      const text = historyToggle.querySelector('[data-chat-history-label]');
      if (text) text.textContent = label;
      historyToggle.setAttribute('aria-label', label + (unread ? '，' + unread + ' 条新消息' : ''));
      historyToggle.title = unread ? unread + ' 条新消息' : label;
      historyToggle.classList.toggle('has-unread', !!unread);
    }
    if (latest) {
      latest.classList.toggle('hidden', !historyVisible() || (followingLatest && !unread));
      latest.textContent = unread ? '查看新消息（' + unread + '）' : '回到最新';
    }
  }

  function scrollToLatest() {
    followingLatest = true;
    if (historyVisible() && conversation) {
      conversation.scrollTop = conversation.scrollHeight;
      savedScrollTop = conversation.scrollTop;
      unread = 0;
    }
    updateCounts();
  }

  function rememberScroll() {
    if (!historyVisible() || !conversation) return;
    savedScrollTop = conversation.scrollTop;
    followingLatest = atLatest();
  }

  function heightBounds({bottomAnchor=null}={}) {
    if (!dock || collapsed) return lastHeightBounds;
    const rect = dock.getBoundingClientRect();
    const viewport = window.visualViewport;
    const top = viewport?.offsetTop || 0;
    const bottom = Math.min(rect.bottom, top + (viewport?.height || window.innerHeight));
    const cssMaximum = parseFloat(getComputedStyle(dock).maxHeight);
    const custom=customPosition(),bounds=custom ? positionedBounds() : null;
    const available=custom ? (bottomAnchor === null ? bounds.maxHeight
      : Math.min(bounds.maxHeight,bottomAnchor-bounds.top)) : bottom-top-12;
    const maximum = Math.max(1, Math.floor(Math.min(available,
      !custom && Number.isFinite(cssMaximum) ? cssMaximum : Infinity)));
    // Everything outside history (header, input, actions and spacing) keeps its
    // natural size; leave enough history to read a message even at the minimum.
    const chrome = rect.height - (history?.getBoundingClientRect().height || 0);
    const minimumHistory = Math.min(64, Math.max(24, conversation?.scrollHeight || 0));
    const minimum = historyCollapsed ? lastHeightBounds.min : Math.ceil(chrome + minimumHistory);
    return lastHeightBounds = {min:Math.min(maximum, minimum), max:maximum};
  }

  function updateHeightAria(bounds) {
    if (!resizeHandle || !dock || collapsed) return;
    const height = historyCollapsed && dockHeight !== null
      ? Math.min(bounds.max, Math.max(bounds.min, dockHeight)) : dock.getBoundingClientRect().height;
    resizeHandle.setAttribute('aria-valuemin', String(Math.min(bounds.min, Math.round(height))));
    resizeHandle.setAttribute('aria-valuemax', String(bounds.max));
    resizeHandle.setAttribute('aria-valuenow', String(Math.round(height)));
    resizeHandle.setAttribute('aria-valuetext', Math.round(height) + ' 像素');
  }

  function restoreHistoryScroll() {
    if (!historyVisible() || !conversation) return;
    if (followingLatest) scrollToLatest();
    else {
      conversation.scrollTop = savedScrollTop;
      savedScrollTop = conversation.scrollTop;
    }
  }

  function applyHeight({restoreScroll=true}={}) {
    if (!dock || sectionMotion?.active || previewAnimation) return;
    applyPosition();
    dock.classList.toggle('is-resized', dockHeight !== null);
    if (dockHeight === null) dock.style.removeProperty('--chat-height');
    if (collapsed || compact) return;
    const bounds = heightBounds();
    if (dockHeight !== null) {
      // Viewport limits are temporary. Keep the user's preferred height so it
      // returns when a small window or the mobile keyboard opens up again.
      const value = Math.min(bounds.max, Math.max(bounds.min, dockHeight)) + 'px';
      if (dock.style.getPropertyValue('--chat-height') !== value) dock.style.setProperty('--chat-height', value);
    }
    applyPosition();
    if (restoreScroll) restoreHistoryScroll();
    updateHeightAria(bounds);
  }

  function scheduleHeightUpdate() {
    if (resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      if(sectionMotion?.active || previewAnimation) return;
      applyPosition();
      if(!compact)panelSizing?.fit({notify:false});
      applyHeight();
    });
  }

  function setHeight(height, {bottomAnchor=null}={}) {
    rememberScroll();
    const custom=customPosition();
    if(custom && bottomAnchor === null) bottomAnchor=dock.getBoundingClientRect().bottom;
    const bounds = heightBounds({bottomAnchor});
    dockHeight = Math.round(Math.min(bounds.max, Math.max(bounds.min, height)));
    if(custom) dockPositions[positionLayout()]={left:custom.left,top:bottomAnchor-dockHeight};
    applyHeight();
  }

  function finishResize(event) {
    if (!resizeDrag || (event?.pointerId !== undefined && event.pointerId !== resizeDrag.pointerId)) return;
    const pointerId = resizeDrag.pointerId;
    resizeDrag = null;
    dock?.classList.remove('resizing');
    if (resizeHandle?.hasPointerCapture(pointerId)) resizeHandle.releasePointerCapture(pointerId);
    persist();scheduleCompact();
  }

  function launcherBounds() {
    // Measure the real button, including unread counts and mobile safe areas.
    // Restoring display in this same task avoids painting a second launcher.
    const hidden = launcher.classList.contains('hidden');
    launcher.classList.remove('hidden');
    const rect = launcher.getBoundingClientRect();
    launcher.classList.toggle('hidden', hidden);
    return rect;
  }

  function setVisible(element, visible, animate) {
    if (!element) return;
    const previous = visibilityTransitions.get(element);
    if (animate && previous?.visible === visible) return;
    const hidden = element.classList.contains('hidden');
    // Capture every animated property before cancelling. Reversals then continue
    // from this exact visible shape, without an old completion hiding the panel.
    const style = getComputedStyle(element);
    const start = {opacity:hidden ? '0' : style.opacity};
    if (element === dock) {
      start.clipPath = style.clipPath;
      start.translate = style.translate === 'none' ? '0px 0px' : style.translate;
    }
    const contents = element === dock ? [dock.querySelector('.chat-dock-header'), byId('chat-dock-body')] : [];
    const contentOpacity = contents.map(child => hidden ? '0' : getComputedStyle(child).opacity);
    previous?.animation?.cancel();
    previous?.contents?.forEach(animation => animation.cancel());
    element.inert = !visible;
    element.setAttribute('aria-hidden', String(!visible));
    element.classList.toggle('is-closing', !visible);
    if (!animate || reducedMotion.matches || typeof element.animate !== 'function') {
      element.classList.toggle('hidden', !visible);
      element.classList.remove('is-transitioning');
      visibilityTransitions.set(element, {visible});
      return;
    }
    element.classList.remove('hidden');
    element.classList.add('is-transitioning');
    let end = {opacity:visible ? '1' : '0'};
    if (element === dock) {
      const rect = dock.getBoundingClientRect();
      const button = launcherBounds();
      const small = {
        clipPath:`inset(${Math.max(0, rect.height - button.height)}px 0px 0px ${Math.max(0, rect.width - button.width)}px round 24px)`,
        translate:`${button.right - rect.right}px ${button.bottom - rect.bottom}px`,
        opacity:'0.35',
      };
      // Leave space around the open shape for its shadow and resize handle.
      const large = {clipPath:'inset(-48px -48px -48px -48px round 24px)', translate:'0px 0px', opacity:'1'};
      if (hidden) Object.assign(start, small);
      else if (start.clipPath === 'none') start.clipPath = large.clipPath;
      end = visible ? large : small;
    }
    const opening = !collapsed;
    const timing = {duration:opening ? 560 : 460, easing:'cubic-bezier(.22, .68, .2, 1)', fill:'both'};
    // Hand the surface from the button to the panel early on opening, and
    // reveal the button near the end of closing instead of showing two panels.
    const frames = element === launcher && !previous?.animation
      ? (visible ? [start, {...start, offset:.65}, end] : [start, {...end, offset:.35}, end])
      : [start, end];
    const animation = element.animate(frames, timing);
    // The panel reveals content at its natural size; only the glass boundary
    // grows. Text never scales, wraps or squeezes during the transition.
    const contentAnimations = contents.map((child, index) => child.animate([
      {opacity:contentOpacity[index]},
      {opacity:visible ? '1' : '0'},
    ], timing));
    visibilityTransitions.set(element, {visible, animation, contents:contentAnimations});
    animation.finished.then(() => {
      if (visibilityTransitions.get(element)?.animation !== animation) return;
      element.classList.toggle('hidden', !visible);
      element.classList.remove('is-transitioning');
      visibilityTransitions.set(element, {visible});
      animation.cancel();
      contentAnimations.forEach(content => content.cancel());
    }, () => { /* A reversed transition continues from its current frame. */ });
  }

  function applyLayout({animate=false}={}) {
    finishPreview();
    launcher?.setAttribute('aria-expanded', String(!collapsed));
    byId('chat-collapse')?.setAttribute('aria-expanded', String(!collapsed));
    history?.classList.toggle('hidden', historyCollapsed);
    if(history){history.inert=historyCollapsed || compact;history.setAttribute('aria-hidden',String(historyCollapsed || compact));}
    historyToggle?.setAttribute('aria-expanded', String(!historyCollapsed && !compact));
    dock?.classList.toggle('history-collapsed', historyCollapsed);
    if (resizeHandle) {
      const enabled = historyVisible();
      resizeHandle.classList.toggle('hidden', !enabled);
      resizeHandle.tabIndex = enabled ? 0 : -1;
      resizeHandle.setAttribute('aria-disabled', String(!enabled));
    }
    const hidden = dock?.classList.contains('hidden');
    if (!collapsed) dock?.classList.remove('hidden');
    applyPosition();
    if(!compact)panelSizing?.fit();
    renderPreview();
    applyHeight({restoreScroll:false});
    if (hidden) dock?.classList.add('hidden');
    updateCounts();
    setVisible(dock, !collapsed, animate);
    setVisible(launcher, collapsed, animate);
    if (historyVisible()) {
      // Let the restored history acquire its height before restoring its position.
      requestAnimationFrame(() => {
        if (!historyVisible()) return;
        if (followingLatest) scrollToLatest();
        else if (conversation) conversation.scrollTop = savedScrollTop;
      });
    }
  }

  function open({focus=false, approval=false}={}) {
    if(collapsed)foldHistory();
    expandPreview({animate:false});
    if (collapsed) {
      collapsed = false;
      applyLayout({animate:true});
      persist();
    }
    if (approval) {
      requestAnimationFrame(async () => {
        await Promise.allSettled((dock?.getAnimations() || []).map(animation => animation.finished));
        if (collapsed) return;
        const card = byId('chat-approvals')?.querySelector('.approval-card, .queue-card');
        if (card) {
          card.scrollIntoView({block:'nearest'});
          card.focus({preventScroll:true});
        } else if (focus) byId('feedback-note')?.focus({preventScroll:true});
      });
    } else if (focus) byId('feedback-note')?.focus({preventScroll:true});
  }

  function refresh() {
    const state = getState() || {};
    if (state.sessionId && state.sessionId !== sessionId) {
      clearTimeout(previewCloseTimer);previewCloseTimer=null;finishPreview();
      clearTimeout(editTimer);editTimer=null;editUntil=0;composing=false;pointerPress=false;
      finishMove({cancel:true});sectionMotion?.finish();panelSizing?.finishResize();finishResize();
      foldHistory();compact=true;renderPreview();
      sessionId = state.sessionId;
      panelSizing?.refresh();
      collapsed = false;
      historyCollapsed = true;
      followingLatest = true;
      savedScrollTop = 0;
      unread = 0;
      dockHeight = null;
      dockPositions = {};
      applyPosition();
      try {
        const stored = JSON.parse(localStorage.getItem(storageKey()) || 'null');
        collapsed = stored?.collapsed === true;
        if (Number.isFinite(stored?.dockHeight) && stored.dockHeight > 0) dockHeight = stored.dockHeight;
        for(const layout of ['compare','immersive']) {
          const position=stored?.dockPositions?.[layout];
          if(Number.isFinite(position?.left) && Number.isFinite(position?.top)) dockPositions[layout]={left:position.left,top:position.top};
        }
      } catch { /* Ignore an unavailable or invalid saved preference. */ }
      applyLayout();
    }
    const pending = state.feedbackTransport === 'mcp_events' ? 0 : (state.approvals || []).length +
      (state.queue || []).filter(item => item?.feedback_id && (['blocked_stale','delivery_uncertain'].includes(item.status) || (item.status === 'failed' && !item.turn_id))).length;
    const waitingForApproval = pending > 0 || state.agent?.status === 'awaiting_approval';
    const approvalPanel = byId('chat-approvals');
    approvalPanel?.classList.toggle('hidden', !pending);
    const count = byId('chat-approval-count');
    if (count) count.textContent = pending > 1 ? pending + ' 项待处理' : '';
    const review = byId('review-pending');
    review?.classList.toggle('hidden', !pending);
    if (review) review.textContent = '到会话中处理待确认操作' + (pending > 1 ? '（' + pending + '）' : '');
    const status = state.agent?.status || 'disconnected';
    const failed = !!state.networkError || state.sessionStatus === 'error' ||
      (state.sessionStatus !== 'connecting' && ['error', 'disconnected', 'delivery_uncertain'].includes(status));
    const label = state.sessionStatus === 'error' ? '连接失败'
      : state.networkError ? '连接中断，正在重试'
      : state.sessionStatus === 'connecting' ? '正在连接'
      : state.sessionStatus !== 'open' ? '会话已结束'
      : waitingForApproval ? '等待你确认'
      : state.submitting ? '正在发送'
      : ({idle:'等待反馈', running:'Codex 正在处理', awaiting_approval:'等待审批',
        waiting:'反馈已排队', disconnected:'Codex 连接中断', delivery_uncertain:'送达待核实',
        error:'Codex 执行出错', waiting_for_mcp:'等待 MCP 读取', external_idle:'等待 MCP 读取'})[status] || '正在连接';
    const compactStatus = byId('chat-status');
    const running = !waitingForApproval && !failed && state.sessionStatus === 'open' && (state.submitting || status === 'running');
    if (compactStatus) {
      compactStatus.textContent = label;
      compactStatus.title = state.networkError || state.agent?.error || byId('agent-status')?.textContent || label;
      compactStatus.classList.toggle('error', failed);
      compactStatus.classList.toggle('running', running);
      compactStatus.classList.toggle('queued', !failed && ['awaiting_approval', 'waiting'].includes(status));
    }
    launcher?.classList.toggle('has-attention', failed || waitingForApproval);
    launcherStatus = label;
    if (launcher) {
      launcher.classList.toggle('is-running', running);
      launcher.classList.toggle('needs-approval', waitingForApproval);
      const launcherLabel = launcher.querySelector('[data-chat-launcher-label]');
      if (launcherLabel) launcherLabel.textContent = waitingForApproval ? '待确认' : '展开会话';
      launcher.setAttribute('aria-busy', String(running));
      launcher.title = label + (unread ? ' · ' + unread + ' 条新消息' : '');
    }
    if(waitingForApproval)expandPreview();
    else if(!previewHeld())scheduleCompact();
    renderPreview();
  }

  sectionMotion=createChatSectionMotion({dock,panels:[history,byId('feedback-evidence')],onFinish:()=>{renderPreview();applyHeight();restoreHistoryScroll();scheduleHeightUpdate();}});
  panelSizing=setupChatPanelSizing({getState,animateChange:change=>{
    expandPreview({animate:false});finishMove();finishResize();rememberScroll();sectionMotion.run(()=>{change();applyHeight({restoreScroll:false});});
  },beforeResize:()=>{expandPreview({animate:false});finishMove();sectionMotion.finish();finishResize();rememberScroll();},
  beforeViewportResize:()=>{finishPreview();finishMove({cancel:true});sectionMotion.finish();finishResize();rememberScroll();},
  onResize:scheduleHeightUpdate});

  moveHandle?.addEventListener('pointerdown',event=>{
    if(event.target!==moveHandle || event.button!==0 || event.isPrimary===false || collapsed || dock.inert || moveDrag ||
      window.getSelection()?.type==='Range') return;
    event.preventDefault();event.stopPropagation();settleForMove();
    moveHandle.focus({preventScroll:true});
    const rect=dock.getBoundingClientRect(),layout=positionLayout();
    moveDrag={pointerId:event.pointerId,x:event.clientX,y:event.clientY,left:rect.left,top:rect.top,layout,
      previous:dockPositions[layout] ? {...dockPositions[layout]} : null,moved:false};
    dock.classList.add('moving');
    try {moveHandle.setPointerCapture(event.pointerId);} catch {finishMove({cancel:true});}
  });
  moveHandle?.addEventListener('pointermove',event=>{
    if(moveDrag?.pointerId!==event.pointerId) return;
    if(event.pointerType==='mouse' && !event.buttons) {finishMove();return;}
    event.preventDefault();event.stopPropagation();
    const dx=event.clientX-moveDrag.x,dy=event.clientY-moveDrag.y;
    if(!moveDrag.moved && Math.hypot(dx,dy)<3) return;
    moveDrag.moved=true;
    dockPositions[moveDrag.layout]={left:moveDrag.left+dx,top:moveDrag.top+dy};
    applyPosition();
  });
  for(const name of ['pointerup','pointercancel','lostpointercapture']) moveHandle?.addEventListener(name,event=>{
    if(moveDrag?.pointerId!==event.pointerId) return;
    event.preventDefault();event.stopPropagation();finishMove({cancel:name==='pointercancel'});
  });
  moveHandle?.addEventListener('dblclick',event=>{
    if(event.target!==moveHandle || collapsed || window.getSelection()?.type==='Range') return;
    event.preventDefault();event.stopPropagation();resetPosition();
  });
  moveHandle?.addEventListener('keydown',event=>{
    if(event.target!==moveHandle || collapsed || event.isComposing || event.altKey || event.ctrlKey || event.metaKey ||
      !['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','Escape'].includes(event.key)) return;
    event.preventDefault();event.stopPropagation();
    if(event.key==='Escape') {finishMove({cancel:true});return;}
    if(event.key==='Home') {resetPosition();return;}
    finishMove();settleForMove();
    const rect=dock.getBoundingClientRect(),step=event.shiftKey?32:8;
    dockPositions[positionLayout()]={left:rect.left+(event.key==='ArrowLeft'?-step:event.key==='ArrowRight'?step:0),
      top:rect.top+(event.key==='ArrowUp'?-step:event.key==='ArrowDown'?step:0)};
    applyPosition();
    const result=dock.getBoundingClientRect();
    dockPositions[positionLayout()]={left:Math.round(result.left),top:Math.round(result.top)};
    scheduleHeightUpdate();persist();
  });

  resizeHandle?.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || event.isPrimary === false || resizeDrag || !historyVisible()) return;
    event.preventDefault(); event.stopPropagation();
    expandPreview({animate:false});finishMove();sectionMotion?.finish();panelSizing?.finishResize();rememberScroll();
    resizeHandle.focus({preventScroll:true});
    const rect=dock.getBoundingClientRect();
    resizeDrag = {pointerId:event.pointerId, y:event.clientY, height:rect.height,bottom:rect.bottom};
    dock.classList.add('resizing');
    try { resizeHandle.setPointerCapture(event.pointerId); }
    catch { finishResize(); }
  });
  resizeHandle?.addEventListener('pointermove', (event) => {
    if (!resizeDrag || event.pointerId !== resizeDrag.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    if (event.pointerType === 'mouse' && !event.buttons) { finishResize(event); return; }
    setHeight(resizeDrag.height + resizeDrag.y - event.clientY,{bottomAnchor:resizeDrag.bottom});
  });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    resizeHandle?.addEventListener(name, (event) => {
      if (!resizeDrag || event.pointerId !== resizeDrag.pointerId) return;
      event.preventDefault(); event.stopPropagation();
      finishResize(event);
    });
  }
  resizeHandle?.addEventListener('click', (event) => {
    event.preventDefault(); event.stopPropagation();
  });
  resizeHandle?.addEventListener('dblclick', (event) => {
    if (!historyVisible()) return;
    event.preventDefault(); event.stopPropagation();
    sectionMotion?.finish();panelSizing?.finishResize();finishResize(); rememberScroll();
    dockHeight = null;
    applyHeight();
    persist();
  });
  resizeHandle?.addEventListener('keydown', (event) => {
    if (!historyVisible() || event.isComposing || !['ArrowUp', 'ArrowDown', 'Home', 'End', 'Escape'].includes(event.key)) return;
    if (event.key === 'Escape') { finishResize(); return; }
    event.preventDefault(); event.stopPropagation();
    expandPreview({animate:false});sectionMotion?.finish();panelSizing?.finishResize();
    const bounds = heightBounds();
    const step = event.shiftKey ? 64 : 24;
    const next = event.key === 'Home' ? bounds.min : event.key === 'End' ? bounds.max
      : dock.getBoundingClientRect().height + (event.key === 'ArrowUp' ? step : -step);
    setHeight(next);
    persist();
  });
  window.addEventListener('blur', () => {finishMove();finishResize();});
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) applyLayout();
  });
  function resizeViewport() {
    clearTimeout(previewCloseTimer);previewCloseTimer=null;finishPreview();
    finishMove({cancel:true});
    sectionMotion?.finish();
    // A viewport change invalidates the launcher-to-panel path. Settle at the
    // requested state before fitting the new screen instead of drifting outside it.
    if ([dock, launcher].some(element => visibilityTransitions.get(element)?.animation)) applyLayout();
    scheduleHeightUpdate();scheduleCompact();
  }
  window.addEventListener('resize', resizeViewport);
  window.visualViewport?.addEventListener('resize', resizeViewport);
  window.visualViewport?.addEventListener('scroll', scheduleHeightUpdate);
  new MutationObserver(()=>{
    clearTimeout(previewCloseTimer);previewCloseTimer=null;finishPreview();
    finishMove({cancel:true});sectionMotion?.finish();panelSizing?.finishResize();finishResize();applyLayout();scheduleCompact();
  }).observe(document.documentElement,{attributes:true,attributeFilter:['data-layout']});
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(scheduleHeightUpdate);
    for (const element of [dock, dock?.querySelector('.chat-dock-header'),
      dock?.querySelector('.composer'), byId('chat-approvals'), byId('timeline-panel')]) {
      if (element) observer.observe(element);
    }
  }

  byId('chat-collapse')?.addEventListener('click', () => {
    finishMove();sectionMotion?.finish();panelSizing?.finishResize();finishResize();
    rememberScroll();
    collapsed = true;
    applyLayout({animate:true});
    persist();
    launcher?.focus({preventScroll:true});
  });
  launcher?.addEventListener('click', () => open({focus:true, approval:launcher.classList.contains('needs-approval')}));
  byId('review-pending')?.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('workspace-sidebar-close',{detail:{afterClose:()=>open({approval:true})}}));
  });
  historyToggle?.addEventListener('click', () => {
    expandPreview({animate:false});panelSizing?.finishResize();finishResize();
    rememberScroll();
    sectionMotion.run(()=>{historyCollapsed = !historyCollapsed;applyLayout();});
    persist();
  });
  latest?.addEventListener('click', scrollToLatest);
  conversation?.addEventListener('scroll', () => {
    if (!historyVisible() || sectionMotion?.active) return;
    savedScrollTop = conversation.scrollTop;
    followingLatest = atLatest();
    if (followingLatest) unread = 0;
    updateCounts();
  });
  dock.addEventListener('pointerenter',event=>{if(event.pointerType==='mouse'){mousePoint={x:event.clientX,y:event.clientY};expandPreview();}});
  dock.addEventListener('pointerleave',event=>{if(event.pointerType==='mouse'){mousePoint={x:event.clientX,y:event.clientY};scheduleCompact();}});
  dock.addEventListener('focusin',()=>{expandPreview();scheduleCompact();});
  dock.addEventListener('focusout',scheduleCompact);
  for(const name of ['beforeinput','input'])dock.addEventListener(name,event=>{if(editable(event.target))editingActivity();});
  dock.addEventListener('compositionstart',()=>{composing=true;expandPreview();});
  dock.addEventListener('compositionend',()=>{composing=false;editingActivity();});
  dock.addEventListener('pointerdown',event=>{
    previewRevealTarget=null;
    if(event.pointerType==='mouse')return;
    if(compact) {
      if(event.target.closest('#feedback-evidence-summary') && panelSizing.opened)previewRevealTarget='feedback-evidence-summary';
    }
    expandPreview({animate:false});
  }, {capture:true});
  dock.addEventListener('click',event=>{
    const reveal=previewRevealTarget;previewRevealTarget=null;
    if(reveal && event.target.closest('#'+reveal)){event.preventDefault();event.stopPropagation();}
  }, {capture:true});
  dock.addEventListener('dragenter',()=>{dropDepth+=1;dropActive=true;expandPreview();});
  dock.addEventListener('dragleave',()=>{dropDepth=Math.max(0,dropDepth-1);if(!dropDepth){dropActive=false;scheduleCompact();}});
  for(const name of ['drop','dragend'])document.addEventListener(name,()=>{dropDepth=0;dropActive=false;scheduleCompact();});
  window.addEventListener('blur',()=>{mousePoint=null;pointerPress=false;composing=false;editUntil=0;dropDepth=0;dropActive=false;scheduleCompact();});
  for(const popup of popupNodes()) {
    popup.addEventListener('pointerenter',event=>{if(event.pointerType==='mouse')mousePoint={x:event.clientX,y:event.clientY};expandPreview();});
    popup.addEventListener('pointerleave',scheduleCompact);popup.addEventListener('focusout',scheduleCompact);
    new MutationObserver(()=>{if(!previewHeld())scheduleCompact();}).observe(popup,{attributes:true,attributeFilter:['hidden','inert','class','aria-busy']});
  }
  document.addEventListener('focusin',()=>{if(!previewHeld())scheduleCompact();});
  document.addEventListener('pointermove',event=>{
    if(event.pointerType!=='mouse')return;
    if(!event.buttons)pointerPress=false;
    mousePoint={x:event.clientX,y:event.clientY};inputMode='mouse';
    if(mouseWithin(dock) || popupNodes().some(element=>popupVisible(element) && mouseWithin(element)))expandPreview();
    else scheduleCompact();
  }, {capture:true});
  document.addEventListener('pointerdown',event=>{
    inputMode=event.pointerType==='mouse'?'mouse':'touch';
    mousePoint=inputMode==='mouse'?{x:event.clientX,y:event.clientY}:null;
    pointerPress=dock.contains(event.target) || popupNodes().some(element=>element.contains(event.target));
    if(!pointerPress)scheduleCompact();
  }, {capture:true});
  for(const name of ['pointerup','pointercancel'])document.addEventListener(name,()=>{pointerPress=false;scheduleCompact();}, {capture:true});
  document.documentElement.addEventListener('pointerleave',event=>{if(event.pointerType==='mouse'){mousePoint=null;scheduleCompact();}});
  document.addEventListener('keydown',event=>{
    if(event.key==='Tab')inputMode='keyboard';
    if(['ArrowUp','ArrowDown','Home','End','Enter',' '].includes(event.key) && popupNodes().some(element=>popupVisible(element) && element.contains(event.target))) {
      inputMode='keyboard';expandPreview();
    }
    if(dock.contains(event.target) && editable(event.target) && !['Tab','Escape','Shift','Control','Alt','Meta'].includes(event.key))editingActivity();
  }, {capture:true});
  document.addEventListener('selectionchange',()=>{if(!previewHeld())scheduleCompact();});
  for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('close',scheduleCompact);
  renderPreview();
  applyLayout();

  return {
    refresh,
    open,
    beforeConversationAppend() {
      rememberScroll();
      const anchor = historyVisible() && conversation
        ? [...conversation.children].find((item) => item.getBoundingClientRect().bottom > conversation.getBoundingClientRect().top)
        : null;
      return {followLatest:historyVisible() && followingLatest, anchor,
        anchorOffset:anchor ? anchor.getBoundingClientRect().top - conversation.getBoundingClientRect().top : 0};
    },
    conversationAppended(previous, {historical=false}={}) {
      if (previous?.followLatest) scrollToLatest();
      else {
        if (!historical) unread += 1;
        if (historyVisible() && previous?.anchor?.isConnected) {
          conversation.scrollTop += previous.anchor.getBoundingClientRect().top -
            conversation.getBoundingClientRect().top - previous.anchorOffset;
          savedScrollTop = conversation.scrollTop;
        }
        updateCounts();
      }
    }
  };
}
