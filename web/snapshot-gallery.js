import {actionIcon} from './action-icons.js';

// The live scene leads the collection; saved evidence keeps its creation order.
export function setupSnapshotGallery({getItems, getPreview=()=>[], drawPreviewMark=()=>{}, getLive=()=>({active:true,disabled:false}), getTimeline, onLive=()=>{}, onOpen, onTime=()=>{}, onRemove, resourceURL}) {
  const strip=document.getElementById('scene-snapshot-strip');
  const gallery=document.getElementById('scene-snapshots'),footer=document.querySelector('.scene-pane-footer');
  function placeGallery() {
    if(document.documentElement.dataset.layout==='immersive') {
      if(gallery.parentElement!==document.body) document.body.append(gallery);
    } else if(gallery.parentElement!==footer) {
      footer.append(gallery);
    }
  }
  new MutationObserver(()=>{hide();placeGallery();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-layout']});
  placeGallery();
  const ticks=document.getElementById('timeline-marks');
  const preview=document.createElement('div');preview.className='snapshot-preview';preview.hidden=true;
  preview.id='snapshot-gallery-preview';preview.setAttribute('role','group');preview.setAttribute('aria-label','选择此时刻的截图');document.body.append(preview);
  let signature='',tickSignature='',activeKey=null,anchor=null,restoringFocus=false;
  function el(tag,text,cls) {const node=document.createElement(tag);if(text) node.textContent=text;if(cls) node.className=cls;return node;}
  // The floating preview never contributes to row geometry or opens a snapshot.
  const hover=document.createElement('div');hover.id='snapshot-card-preview';hover.className='snapshot-card-preview';
  hover.hidden=true;hover.setAttribute('role','dialog');hover.setAttribute('aria-label','截图预览');document.body.append(hover);
  const finePointer=matchMedia('(hover: hover) and (pointer: fine)'),reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let hoverTimer=null,leaveTimer=null,pendingAnchor=null,hoverAnchor=null,hoverMotion=null,hoverEpoch=0;
  function hideHover(immediate=false) {
    clearTimeout(hoverTimer);clearTimeout(leaveTimer);hoverTimer=null;pendingAnchor=null;
    hoverAnchor?.setAttribute('aria-expanded','false');hoverAnchor=null;hover.inert=true;
    const ticket=++hoverEpoch;hoverMotion?.cancel();
    if(hover.hidden || immediate || reduced.matches) {hover.hidden=true;return;}
    hoverMotion=hover.animate([{opacity:1},{opacity:0}],{duration:110,fill:'both'});
    hoverMotion.finished.then(()=>{if(ticket===hoverEpoch) hover.hidden=true;}).catch(()=>{});
  }
  function leaveHover() {
    clearTimeout(leaveTimer);
    if(hover.hidden) {hideHover(true);return;}
    // Allow crossing the gap to the preview, including when it avoids the chat.
    leaveTimer=setTimeout(()=>{
      if(!hover.matches(':hover') && !hover.querySelector(':focus-visible') && !hoverAnchor?.matches(':hover,:focus-visible')) hideHover();
    },350);
  }
  function returnHoverFocus() {
    const button=hoverAnchor;hideHover(true);restoringFocus=true;
    button?.focus({preventScroll:true});restoringFocus=false;
  }
  function previewContent(item,ticket) {
    const layers=getPreview(item.id);
    if(!layers.length) layers.push({name:'场景',url:item.cover || item.scene,marks:[]});
    let layerIndex=0,showMarks=true,imageEpoch=0;
    const caption=el('div',null,'snapshot-card-preview-caption');
    caption.append(el('strong',item.name),el('span',item.detail || '固定视角'));
    const media=el('div',null,'snapshot-card-preview-media');
    const bar=el('div',null,'snapshot-card-preview-toolbar'),sources=el('div',null,'snapshot-preview-segments');
    const modes=el('div',null,'snapshot-preview-segments');modes.setAttribute('role','group');modes.setAttribute('aria-label','标记显示');
    const marked=el('button','标记'),original=el('button','原图'),count=el('span',null,'snapshot-preview-count');marked.append(count);
    marked.dataset.previewMode='marked';original.dataset.previewMode='original';
    for(const button of [marked,original]) button.type='button';
    modes.append(marked,original);
    if(layers.length>1) {
      sources.setAttribute('role','group');sources.setAttribute('aria-label','预览画面');
      layers.forEach((layer,index)=>{
        const button=el('button',layer.name);button.type='button';button.dataset.previewLayer=String(index);
        button.addEventListener('click',()=>{if(layerIndex!==index) {layerIndex=index;renderImage();}});sources.append(button);
      });
    } else sources.append(el('span','场景','snapshot-preview-source'));
    bar.append(sources,modes);hover.replaceChildren(caption,media,bar);
    hover.setAttribute('aria-label',item.name+'预览');
    function renderImage() {
      const layer=layers[layerIndex],loadTicket=++imageEpoch;
      const img=el('img'),canvas=el('canvas');img.alt=item.name+' · '+layer.name;canvas.setAttribute('aria-hidden','true');
      const comparison=layer.comparison?.enabled && layer.comparison.opacity>0 ? layer.comparison : null;
      const overlay=comparison ? new Image() : null;
      const current=()=>ticket===hoverEpoch && loadTicket===imageEpoch;
      function paint() {
        if(!current() || !img.naturalWidth) return;
        const scale=Math.min(media.clientWidth/img.naturalWidth,media.clientHeight/img.naturalHeight);
        const width=img.naturalWidth*scale,height=img.naturalHeight*scale,ratio=Math.min(devicePixelRatio || 1,2);
        canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);
        canvas.style.width=width+'px';canvas.style.height=height+'px';
        const context=canvas.getContext('2d');context.scale(ratio,ratio);
        if(overlay?.complete && overlay.naturalWidth) {
          const rect=comparison.rect;context.globalAlpha=comparison.opacity/100;
          context.drawImage(overlay,rect.x*width,rect.y*height,rect.width*width,rect.height*height);context.globalAlpha=1;
        }
        for(const mark of layer.marks) drawPreviewMark(context,mark,width,height);
        canvas.dataset.ready='true';
      }
      img.addEventListener('load',paint,{once:true});
      img.addEventListener('error',()=>{if(current()) media.replaceChildren(el('span','图片暂时无法加载','snapshot-preview-error'));},{once:true});
      if(overlay) {overlay.addEventListener('load',paint,{once:true});overlay.src=resourceURL(comparison.data_url);}
      canvas.hidden=!showMarks;media.replaceChildren(img,canvas);img.src=resourceURL(layer.url);
      count.textContent=String(layer.marks.length);marked.setAttribute('aria-label','显示标记 · '+layer.marks.length+' 个');
      for(const button of sources.querySelectorAll('button')) button.setAttribute('aria-pressed',String(Number(button.dataset.previewLayer)===layerIndex));
      updateMode();
    }
    function updateMode() {
      marked.setAttribute('aria-pressed',String(showMarks));original.setAttribute('aria-pressed',String(!showMarks));
      const canvas=media.querySelector('canvas');if(canvas) canvas.hidden=!showMarks;
    }
    marked.addEventListener('click',()=>{showMarks=true;updateMode();});original.addEventListener('click',()=>{showMarks=false;updateMode();});
    renderImage();
  }
  function positionHover(button) {
    const r=button.getBoundingClientRect(),p=hover.getBoundingClientRect(),gap=12;
    let x=Math.max(8,Math.min(innerWidth-p.width-8,r.left+(r.width-p.width)/2));
    let y=Math.max(8,r.top-p.height-gap);
    // Keep the conversation readable. Prefer beside it; above it on narrow screens.
    const chat=document.getElementById('chat-dock');
    if(chat && getComputedStyle(chat).visibility!=='hidden' && chat.getClientRects().length) {
      const c=chat.getBoundingClientRect();
      if(x<c.right+gap && x+p.width>c.left-gap && y<c.bottom+gap && y+p.height>c.top-gap) {
        const right=c.right+gap,left=c.left-p.width-gap;
        if(right+p.width<=innerWidth-8) x=right;
        else if(left>=8) x=left;
        else y=c.top-p.height-gap;
      }
    }
    if(y<8) return false;
    hover.style.left=x+'px';hover.style.top=y+'px';return true;
  }
  function scheduleHover(button) {
    clearTimeout(hoverTimer);clearTimeout(leaveTimer);hoverTimer=null;pendingAnchor=null;
    if(button===hoverAnchor && !hover.hidden && !hover.inert) return;
    if(!button || button.disabled || button.id==='scene-live-card' || collapsed || !preview.hidden) {leaveHover();return;}
    // Crossing a neighboring card on the way to the popup must not close it.
    // Replace the current preview only after the new card's dwell time.
    const ticket=hoverEpoch;pendingAnchor=button;
    hoverTimer=setTimeout(()=>{
      hoverTimer=null;pendingAnchor=null;
      if(ticket!==hoverEpoch || !button.isConnected || document.querySelector('dialog[open]') ||
          !button.matches(':hover,:focus-visible')) return;
      const item=getItems().find(item=>item.id===button.dataset.snapshotId);
      if(!item || item.disabled) return;
      hideHover(true);hoverAnchor=button;
      hover.hidden=false;hover.inert=false;previewContent(item,hoverEpoch);
      if(!positionHover(button)) {hideHover(true);return;}
      button.setAttribute('aria-expanded','true');
      if(!reduced.matches) hoverMotion=hover.animate([{opacity:0,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:160,easing:'cubic-bezier(.22,1,.36,1)',fill:'both'});
    },400);
  }
  strip.addEventListener('pointerover',event=>{
    const button=event.target.closest('.snapshot-open');
    if(finePointer.matches && event.pointerType!=='touch' && !event.buttons && button && !button.contains(event.relatedTarget)) scheduleHover(button);
  });
  strip.addEventListener('pointerout',event=>{
    const button=event.target.closest('.snapshot-open');
    if(button && !button.contains(event.relatedTarget)) leaveHover();
  });
  strip.addEventListener('focusin',event=>{if(!restoringFocus && event.target.matches('.snapshot-open:focus-visible')) scheduleHover(event.target);});
  strip.addEventListener('focusout',event=>{if(!hover.contains(event.relatedTarget)) hideHover();});
  strip.addEventListener('keydown',event=>{
    if(event.key==='Tab' && !event.shiftKey && event.target===hoverAnchor && !hover.hidden && !hover.inert) {
      event.preventDefault();hover.querySelector('button')?.focus({preventScroll:true});
    }
  });
  hover.addEventListener('pointerenter',()=>clearTimeout(leaveTimer));
  hover.addEventListener('pointerleave',leaveHover);
  hover.addEventListener('focusout',event=>{if(!hover.contains(event.relatedTarget) && event.relatedTarget!==hoverAnchor) hideHover();});
  hover.addEventListener('keydown',event=>{
    const buttons=[...hover.querySelectorAll('button')];
    if(event.key==='Tab' && (event.shiftKey && event.target===buttons[0] || !event.shiftKey && event.target===buttons.at(-1))) {
      returnHoverFocus();if(event.shiftKey) event.preventDefault();
    }
  });
  strip.addEventListener('wheel',()=>hideHover(),{passive:true});
  document.addEventListener('pointerdown',event=>{if(!hover.contains(event.target)) hideHover();},{capture:true});
  document.addEventListener('scroll',event=>{
    // Arrow-key navigation scrolls the focused card into view before its preview.
    if(event.target===strip && (pendingAnchor || hoverAnchor)?.matches(':focus-visible')) {
      if(!hover.hidden && hoverAnchor && !positionHover(hoverAnchor)) hideHover(true);
    } else hideHover();
  },{capture:true,passive:true});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape' && (hoverTimer || !hover.hidden)) {
      if(hover.contains(document.activeElement)) returnHoverFocus();else hideHover(true);
      event.preventDefault();event.stopImmediatePropagation();
    }
  },{capture:true});
  window.addEventListener('blur',()=>hideHover(true));
  document.addEventListener('visibilitychange',()=>{if(document.hidden) hideHover(true);});
  // Collapsing is an explicit preference, independent of selection and hovering.
  const body=document.getElementById('snapshot-gallery-body'),toggle=document.getElementById('snapshot-gallery-toggle'),count=document.getElementById('snapshot-gallery-count');
  const storageKey='scene-gallery-collapsed:'+location.pathname;
  let collapsed=false;
  try {collapsed=localStorage.getItem(storageKey)==='true';} catch { /* Optional preference. */ }
  function updateCollapse(total=getItems().length) {
    gallery.classList.toggle('is-collapsed',collapsed);body.inert=collapsed;
    toggle.setAttribute('aria-expanded',String(!collapsed));
    toggle.setAttribute('aria-label',(collapsed?'展开':'收起')+'截图栏 · '+total+' 张截图');
    toggle.title=collapsed?'展开截图栏':'收起截图栏';
    count.textContent=total+' 张';count.hidden=!collapsed;
  }
  toggle.addEventListener('click',()=>{
    hide();collapsed=!collapsed;updateCollapse();
    try {localStorage.setItem(storageKey,String(collapsed));} catch { /* Optional preference. */ }
  });
  function liveItem() {
    const live=getLive() || {};
    return {...live,id:'live',live:true,name:live.name || '3D',title:live.title || '实时 3D · 拖动旋转、滚轮缩放',
      detail:'',scene:live.cover};
  }
  function cube() {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    for(const [name,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.35','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(name,value);
    const path=document.createElementNS(svg.namespaceURI,'path');
    path.setAttribute('d','m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Zm0 9 8-4.5M12 12 4 7.5M12 12v9');svg.append(path);
    return svg;
  }
  function hide() {
    hideHover();preview.hidden=true;anchor?.removeAttribute('aria-describedby');anchor=null;
  }
  function position() {
    if(!anchor?.isConnected) {hide();return;}
    const r=anchor.getBoundingClientRect(),p=preview.getBoundingClientRect();
    preview.style.left=Math.max(8,Math.min(innerWidth-p.width-8,r.right-p.width))+'px';
    const below=r.bottom+8;
    preview.style.top=Math.max(8,below+p.height<innerHeight-8 ? below : r.top-p.height-8)+'px';
  }
  function showChooser(button,items) {
    if(document.querySelector('dialog[open]')) return;
    hideHover();
    anchor?.removeAttribute('aria-describedby');anchor=button;
    preview.replaceChildren();
    for(const item of items) {
      const section=el('div',null,'snapshot-preview-item');
      const open=el('button',item.title,'compact-button');open.type='button';open.addEventListener('click',()=>{hide();onOpen(item.id);});section.append(open);
      section.append(el('small',item.title));
      const pair=el('div',null,'snapshot-preview-pair');
      for(const [label,url] of [['参考帧',item.reference],['场景截图',item.scene]]) {
        if(!url) continue;
        const figure=el('figure'),img=el('img');img.src=resourceURL(url);img.alt=label;img.addEventListener('load',position,{once:true});figure.append(img,el('figcaption',label));pair.append(figure);
      }
      section.append(pair);preview.append(section);
    }
    preview.hidden=false;button.setAttribute('aria-describedby',preview.id);position();
    preview.querySelector('button')?.focus({preventScroll:true});
  }
  function render() {
    const items=getItems();
    const live=liveItem();
    const cards=[live,...items];
    const nextActive=cards.find(item=>item.active)?.id || null,activeChanged=nextActive!==activeKey;
    activeKey=nextActive;
    gallery.classList.remove('hidden');
    updateCollapse(items.length);
    strip.setAttribute('aria-label','实时 3D 与保留截图');
    const next=JSON.stringify(cards.map(({id,title,name,detail,count,active,disabled,cover,live})=>[id,title,name,detail,count,active,disabled,live ? null : cover?.slice(0,80),live ? null : cover?.length]));
    if(next!==signature) {
      signature=next;hide();
      const focused=document.activeElement?.closest('.snapshot-card');const focusedId=focused?.dataset.galleryKey, wasRemove=document.activeElement?.classList.contains('snapshot-remove'), wasTime=document.activeElement?.classList.contains('snapshot-time-reference');
      const focusedIndex=[...strip.children].indexOf(focused);
      const scroll=strip.scrollLeft;strip.replaceChildren();
      for(const item of cards) {
        const card=el('div',null,'snapshot-card');card.dataset.kind=item.live?'live':item.dynamic?'moment':'static';card.dataset.galleryKey=item.id;
        if(!item.live) card.dataset.snapshotId=item.id;
        const open=el('button',null,'snapshot-open');open.type='button';open.disabled=item.disabled;
        if(item.live) open.id='scene-live-card';else {
          open.dataset.snapshotId=item.id;open.setAttribute('aria-haspopup','dialog');
          open.setAttribute('aria-controls',hover.id);open.setAttribute('aria-expanded','false');
        }
        open.setAttribute('aria-pressed',String(!!item.active));open.setAttribute('aria-label',item.title);
        const thumb=el('span',null,'snapshot-thumb'),img=el('img');img.alt='';thumb.append(img);
        if(item.live) {
          const placeholder=el('span',null,'snapshot-live-placeholder');placeholder.append(cube());thumb.append(placeholder);
          img.addEventListener('error',()=>{img.dataset.failedUrl=img.getAttribute('src') || '';img.hidden=true;placeholder.hidden=false;});
          thumb.append(el('span','实时','snapshot-live-badge'));
        } else img.src=resourceURL(item.cover);
        if(item.dynamic) {const badge=el('span',null,'snapshot-video-icon');badge.append(actionIcon('play'));thumb.append(badge);}
        if(item.count) thumb.append(el('span',String(item.count),'snapshot-mark-count'));
        const meta=el('span',null,'snapshot-card-meta');
        meta.append(el('strong',item.name,'snapshot-name'));
        if(!item.live) meta.append(el('small',item.dynamic ? '\u00a0' : item.detail || '固定视角',item.dynamic ? 'snapshot-time-space' : 'snapshot-source'));
        open.append(thumb,meta);
        open.addEventListener('click',()=>{hide();if(item.live) onLive();else onOpen(item.id);});card.append(open);
        if(item.dynamic) {
          // A separate button survives both clicks of a double click. Quoting
          // its stored time never opens the snapshot or seeks another view.
          const time=el('button',item.detail,'snapshot-source snapshot-time-reference');time.type='button';
          time.dataset.snapshotId=item.id;time.disabled=item.disabled;
          time.title='双击或按 Enter / 空格引用'+item.name+'的时间';time.setAttribute('aria-label',time.title+' · '+item.detail);
          time.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();if(event.detail===0){hide();onTime(item.id);}});
          time.addEventListener('dblclick',event=>{event.preventDefault();event.stopPropagation();hide();onTime(item.id);});
          card.append(time);
        }
        if(!item.live) {
          const remove=el('button','×','snapshot-remove');remove.type='button';remove.disabled=item.disabled;remove.title='删除'+item.name+'及其标记，可撤销';remove.setAttribute('aria-label',remove.title);
          remove.addEventListener('click',()=>{hide();onRemove(item.id);});card.append(remove);
        }
        strip.append(card);
      }
      strip.scrollLeft=scroll;
      if(focusedId) {
        restoringFocus=true;
        const card=[...strip.children].find(card=>card.dataset.galleryKey===focusedId) || strip.children[Math.max(0,Math.min(focusedIndex,strip.children.length-1))];
        (wasRemove && card?.querySelector('.snapshot-remove') || wasTime && card?.querySelector('.snapshot-time-reference') || card?.querySelector('.snapshot-open'))?.focus({preventScroll:true});
        restoringFocus=false;
      }
    }
    if(activeChanged && nextActive) requestAnimationFrame(()=>{
      if(activeKey!==nextActive) return;
      const card=[...strip.children].find(card=>card.dataset.galleryKey===nextActive);
      if(!collapsed) card?.querySelector('.snapshot-open')?.scrollIntoView({block:'nearest',inline:'nearest'});
    });
    // Updating the live cover must not rebuild saved cards or reset their focus.
    const liveThumb=strip.querySelector('[data-kind="live"] .snapshot-thumb');
    const liveImage=liveThumb?.querySelector('img'),placeholder=liveThumb?.querySelector('.snapshot-live-placeholder');
    if(liveImage) {
      const url=live.cover ? resourceURL(live.cover) : null;
      if(url && liveImage.getAttribute('src')!==url) {delete liveImage.dataset.failedUrl;liveImage.src=url;}
      if(!url) liveImage.removeAttribute('src');
      const available=!!url && liveImage.dataset.failedUrl!==url;
      liveImage.hidden=!available;placeholder.hidden=available;
    }
    const {duration,viewId}=getTimeline();
    const moments=items.filter(item=>item.dynamic && item.time>=0 && item.time<=duration && (!viewId || item.viewId===viewId));
    const nextTicks=JSON.stringify([duration,viewId,moments.map(({id,title,active,disabled})=>[id,title,active,disabled])]);
    if(tickSignature===nextTicks) return;
    tickSignature=nextTicks;ticks.replaceChildren();
    const groups=new Map();
    for(const item of moments) {const key=Math.round(item.time*1e6);if(!groups.has(key)) groups.set(key,[]);groups.get(key).push(item);}
    for(const entries of groups.values()) {
      const item=entries[0],button=el('button',null,'timeline-mark');button.type='button';button.disabled=item.disabled;
      button.style.left=(duration ? item.time/duration*100 : 0)+'%';button.setAttribute('aria-label',item.name+' · '+entries.length+' 张保存截图');button.setAttribute('aria-pressed',String(entries.some(entry=>entry.active)));
      button.dataset.snapshotId=item.id;
      button.addEventListener('click',()=> {if(entries.length===1) {hide();onOpen(item.id);} else showChooser(button,entries);});ticks.append(button);
    }
    ticks.hidden=!groups.size;
  }
  strip.addEventListener('keydown',event=>{
    if(!event.target.closest('.snapshot-open') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    const buttons=[...strip.querySelectorAll('.snapshot-open:not(:disabled)')],index=buttons.indexOf(event.target.closest('.snapshot-open'));
    const next=event.key==='Home' ? 0 : event.key==='End' ? buttons.length-1 : Math.max(0,Math.min(buttons.length-1,index+(event.key==='ArrowRight'?1:-1)));
    if(!buttons[next]) return;
    event.preventDefault();buttons[next].focus({preventScroll:true});buttons[next].scrollIntoView({block:'nearest',inline:'nearest'});
  });
  const layoutSizes=new ResizeObserver(()=>{
    const root=document.documentElement;
    root.style.setProperty('--scene-gallery-height',gallery.getBoundingClientRect().height+'px');
    root.style.setProperty('--workspace-footer-height',footer.getBoundingClientRect().height+'px');
    const timeline=document.getElementById('timeline-panel');
    root.style.setProperty('--workspace-timeline-height',(!timeline.classList.contains('hidden') && !timeline.classList.contains('reference-timeline') ? timeline.getBoundingClientRect().height : 0)+'px');
  });
  for(const node of [gallery,footer,document.getElementById('timeline-panel')]) layoutSizes.observe(node);
  preview.addEventListener('focusout',event=>{if(!preview.contains(event.relatedTarget)) hide();});
  document.addEventListener('pointerdown',event=>{if(!preview.hidden && !preview.contains(event.target) && event.target!==anchor) hide();},{capture:true});
  document.addEventListener('keydown',event=> {if(event.key==='Escape' && !preview.hidden) {const target=anchor;hide();target?.focus({preventScroll:true});event.stopImmediatePropagation();event.preventDefault();}},{capture:true});
  document.addEventListener('scroll',event=>{if(!preview.hidden && !preview.contains(event.target)) hide();},{capture:true,passive:true});
  window.addEventListener('resize',hide);
  return {render,hide};
}
