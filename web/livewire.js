// Reference-only Livewire. Temporary paths never enter drafts or feedback.
export function setupReferenceLivewire({getImage,canvas,getSource,enabled,begin,onCommit,onChange,onError}) {
  const status=document.getElementById('livewire-status'),message=document.getElementById('livewire-message');
  const finishButton=document.getElementById('livewire-finish'),cancelButton=document.getElementById('livewire-cancel');
  let session=null,worker=null,sequence=0,previewVersion=0,previewRunning=false;
  const pending=new Map();
  function notify() { render();onChange(); }
  function render() {
    status.hidden=!enabled();
    const state=session?.busy ? 'busy' : session ? 'tracing' : 'idle';
    if(status.dataset.state!==state)status.dataset.state=state;
    const text=session?.busy ? '计算边缘…' : session?.anchors.length
      ? '继续沿边缘点选' : '点选轮廓起点';
    if(message.textContent!==text)message.textContent=text;
    finishButton.hidden=cancelButton.hidden=!session;
    finishButton.disabled=!session || session.busy || session.segments.length===0;
  }
  function cancel(redraw=true) {
    previewVersion++;previewRunning=false;
    session=null;worker?.terminate();worker=null;
    for(const {reject} of pending.values()) reject(new Error('cancelled'));
    pending.clear();render();if(redraw)onChange();
  }
  function sync() {
    if(session && (!enabled() || session.key!==getSource()?.key)) cancel(false);
    render();
  }
  function valid(current) { return current===session && enabled() && current.key===getSource()?.key; }
  function call(type,data={},transfer=[]) {
    return new Promise((resolve,reject)=>{
      const id=++sequence;pending.set(id,{resolve,reject});
      try { worker.postMessage({type,id,...data},transfer); }
      catch(error) { pending.delete(id);reject(error); }
    });
  }
  function startWorker() {
    worker=new Worker(new URL('./livewire-worker.js',import.meta.url),{type:'module'});
    worker.onmessage=({data})=>{
      const task=pending.get(data.id);if(!task)return;pending.delete(data.id);
      if(data.error)task.reject(new Error(data.error));else task.resolve(data);
    };
    worker.onerror=()=>{
      cancel();onError('智能轮廓计算失败，请重新选取起点。');
    };
  }
  function pixel(point,current) { return {x:Math.round(point.x*(current.width-1)),y:Math.round(point.y*(current.height-1))}; }
  function normalize(points,current,from,to) {
    if(points.length===1 && (from.x!==to.x || from.y!==to.y))return [{...from},{...to}];
    const result=points.map(p=>({x:current.width>1?p.x/(current.width-1):0,y:current.height>1?p.y/(current.height-1):0}));
    if(result.length){result[0]={...from};result[result.length-1]={...to};}
    return result;
  }
  async function path(current,to) {
    const reply=await call('path',pixel(to,current));
    return normalize(reply.points,current,current.anchors.at(-1),to);
  }
  async function seed(current) { await call('seed',pixel(current.anchors.at(-1),current)); }
  async function firstAnchor(point) {
    if(!begin())return;
    const source=getSource(),image=getImage();
    if(!source || !image.complete || !image.naturalWidth)throw new Error('参考图尚未加载完成。');
    const scale=Math.min(1,512/Math.max(image.naturalWidth,image.naturalHeight));
    const width=Math.max(1,Math.round(image.naturalWidth*scale)),height=Math.max(1,Math.round(image.naturalHeight*scale));
    const bitmap=document.createElement('canvas');bitmap.width=width;bitmap.height=height;
    const context=bitmap.getContext('2d',{willReadFrequently:true});context.drawImage(image,0,0,width,height);
    const pixels=context.getImageData(0,0,width,height).data;
    session={key:source.key,width,height,anchors:[{...point}],segments:[],preview:[],cursor:null,busy:true,finishPending:false};
    const current=session;startWorker();notify();
    await call('init',{width,height,pixels:pixels.buffer},[pixels.buffer]);if(!valid(current))return;
    await seed(current);if(!valid(current))return;
    current.busy=false;current.finishPending=false;notify();preview();
  }
  function closeEnough(a,b) {
    const rect=canvas.getBoundingClientRect();return Math.hypot((a.x-b.x)*rect.width,(a.y-b.y)*rect.height)<10;
  }
  async function anchor(point) {
    sync();if(!enabled())return;
    try {
      if(!session){await firstAnchor(point);return;}
      const current=session;
      if(current.busy)return;
      if(current.anchors.length>1 && closeEnough(point,current.anchors[0])) { await finish(true);return; }
      if(closeEnough(point,current.anchors.at(-1)))return;
      if(current.anchors.length>=32){onError('一条轮廓最多 32 个锚点，请先完成当前轮廓。');return;}
      current.busy=true;current.preview=[];previewVersion++;notify();
      const segment=await path(current,point);if(!valid(current))return;
      current.segments.push(segment);current.anchors.push({...point});notify();
      await seed(current);if(!valid(current))return;
      current.busy=false;notify();
      if(current.finishPending)await finish();else preview();
    } catch(error) { if(error.message!=='cancelled'){cancel();onError(error.message);} }
  }
  function points() { return session?.segments.flatMap((segment,i)=>i?segment.slice(1):segment) || []; }
  async function preview() {
    const current=session;
    if(!current || current.busy || !current.cursor || previewRunning)return;
    const version=previewVersion,to={...current.cursor};previewRunning=true;
    try {
      const segment=await path(current,to);
      if(valid(current) && version===previewVersion && !current.busy){current.preview=segment;notify();}
    } catch(error) { if(error.message!=='cancelled' && valid(current)){cancel();onError(error.message);} }
    finally { if(valid(current)){previewRunning=false;if(version!==previewVersion)preview();} }
  }
  function move(point) {
    sync();if(!session)return;
    session.cursor=point;previewVersion++;preview();
  }
  async function finish(closed=false) {
    const current=session;if(!current || !valid(current))return;
    if(current.busy){current.finishPending=true;return;}
    current.finishPending=false;
    if(!current.segments.length)return;
    try {
      current.busy=true;previewVersion++;notify();
      if(closed){const segment=await path(current,current.anchors[0]);if(!valid(current))return;current.segments.push(segment);}
      const result=simplify(points(),current.width,current.height);
      cancel(false);onCommit(result);notify();
    } catch(error) { if(error.message!=='cancelled'){cancel();onError(error.message);} }
  }
  async function back() {
    const current=session;if(!current || current.busy)return;
    if(current.anchors.length===1){cancel();return;}
    current.anchors.pop();current.segments.pop();current.preview=[];current.busy=true;previewVersion++;notify();
    try { await seed(current);if(valid(current)){current.busy=false;notify();if(current.finishPending)await finish();else preview();} }
    catch(error) { if(error.message!=='cancelled'){cancel();onError(error.message);} }
  }
  function draw(context,width,height) {
    if(!session)return;
    context.save();context.lineWidth=2;context.strokeStyle='#bd4c37';context.lineCap='round';context.lineJoin='round';
    const trace=(path,dashed)=>{
      if(path.length<2)return;context.setLineDash(dashed?[5,4]:[]);context.beginPath();
      path.forEach((p,i)=>i?context.lineTo(p.x*width,p.y*height):context.moveTo(p.x*width,p.y*height));context.stroke();
    };
    trace(points(),false);trace(session.preview,true);context.setLineDash([]);
    for(const point of session.anchors){context.beginPath();context.arc(point.x*width,point.y*height,4,0,Math.PI*2);context.fillStyle='#f5f4ef';context.fill();context.stroke();}
    context.restore();
  }
  finishButton.addEventListener('click',()=>finish());cancelButton.addEventListener('click',()=>cancel());
  render();
  return {anchor,move,finish,back,cancel,sync,draw,get active(){return !!session;},get busy(){return !!session?.busy;},
    get anchors(){return session?.anchors || [];},get points(){return points();},get previewPoints(){return session?.preview || [];}};
}

// Keep shape corners while fitting the existing 256-point freehand protocol.
function simplify(points,width,height) {
  if(points.length<=256)return points;
  const squaredDistance=(p,a,b)=>{
    const vx=(b.x-a.x)*width,vy=(b.y-a.y)*height,px=(p.x-a.x)*width,py=(p.y-a.y)*height;
    const t=Math.max(0,Math.min(1,(px*vx+py*vy)/(vx*vx+vy*vy || 1)));
    return (px-t*vx)**2+(py-t*vy)**2;
  };
  function reduce(tolerance) {
    const keep=new Set([0,points.length-1]),stack=[[0,points.length-1]];
    while(stack.length){const [a,b]=stack.pop();let best=tolerance*tolerance,index=-1;
      for(let i=a+1;i<b;i++){const d=squaredDistance(points[i],points[a],points[b]);if(d>best){best=d;index=i;}}
      if(index>=0){keep.add(index);stack.push([a,index],[index,b]);}
    }
    return [...keep].sort((a,b)=>a-b).map(i=>points[i]);
  }
  let tolerance=.6,result=reduce(tolerance);
  while(result.length>256){tolerance*=1.6;result=reduce(tolerance);}
  return result;
}
