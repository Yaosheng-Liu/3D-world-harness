const paths = {
  select:'M5 3v15l4-4 4 6 3-2-4-6h6L5 3Z', point:'M12 3v4m0 10v4M3 12h4m10 0h4',
  rectangle:'M5 5h14v14H5z', line:'M5 19 19 5', arrow:'m5 19 14-14M8 5h11v11',
  text:'M5 5h14M12 5v15m-4 0h8', freehand:'M4 16c3-9 6-11 7-8s-5 10-1 10 4-9 7-9 3 4 2 6',
  livewire:'M4 6h3l3 3 4-3h6M4 6v13h6l4-4 6 4V6M7 4v4m11-4v4M8 17v4',
  erase:'m4 13 9-9 7 7-9 9H8l-4-4v-3Zm4-4 7 7M11 20h9',
  undo:'m8 4-5 5 5 5M3 9h10a7 7 0 0 1 0 14', redo:'m16 4 5 5-5 5m5-5H11a7 7 0 0 0 0 14',
  chevron:'m7 10 5 5 5-5', up:'m7 14 5-5 5 5', close:'m6 6 12 12M6 18 18 6',
  attach:'m9 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9',
  list:'M8 5h12M8 12h12M8 19h12M3 5h1m-1 7h1m-1 7h1',
  camera:'M3 7h5l2-3h4l2 3h5v14H3V7Z', play:'m8 4 12 8-12 8V4Z', pause:'M8 5v14M16 5v14',
  prev:'M6 5v14m12-14L8 12l10 7V5Z', next:'M18 5v14M6 5l10 7-10 7V5Z', reset:'M4 10a8 8 0 1 1 1 8M4 4v6h6',
  trash:'M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7',
  more:'M4 12h1m6 0h2m6 0h1', history:'M4 7v5h5M4 12a8 8 0 1 1 3 6M12 7v6l3 2'
};
export function actionIcon(name) {
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.65','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',class:'action-icon'})) svg.setAttribute(key,value);
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name] || paths.more);svg.append(path);
  if(name==='point'||name==='camera') {const circle=document.createElementNS(svg.namespaceURI,'circle');circle.setAttribute('cx','12');circle.setAttribute('cy',name==='camera'?'13':'12');circle.setAttribute('r',name==='camera'?'4':'2.5');svg.append(circle);}
  return svg;
}
export function setActionIcon(button,name,label) {
  if(button.dataset.actionIcon===name) return;
  button.dataset.actionIcon=name;button.replaceChildren(actionIcon(name));
  if(label) {const span=document.createElement('span');span.textContent=label;button.append(span);}
}
export function setupActionIcons() {
  for(const button of document.querySelectorAll('[data-tool]')) {
    const symbol=button.querySelector('.tool-icon,svg');
    if(symbol) symbol.replaceWith(actionIcon(button.dataset.tool));
  }
  for(const [selector,name] of [['#undo-annotation > [aria-hidden]','undo'],['#redo-annotation > [aria-hidden]','redo']]) document.querySelector(selector)?.replaceWith(actionIcon(name));
  for(const [selector,name] of [['#workspace-menu > summary','more'],['#annotation-tools-close','close'],['#reference-window-reset','reset'],['#timeline-prev','prev'],['#timeline-next','next']]) {
    const el=document.querySelector(selector);if(el) setActionIcon(el,name);
  }
  for(const element of document.querySelectorAll('[data-icon]')) element.replaceWith(actionIcon(element.dataset.icon));
  for(const element of document.querySelectorAll('[data-icon-before]')) element.prepend(actionIcon(element.dataset.iconBefore));
}
