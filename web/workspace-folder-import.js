// Mount before setupWorkspaceSidebar so the existing drawer owns this page too.
export function setupWorkspaceFolderImport({
  api, isEnabled=()=>true, getInitialPath=()=>'', onImported=()=>{}, onError=()=>{}
}={}) {
  const dialog=document.getElementById('projects-dialog');
  const panel=dialog?.querySelector('.sidebar-panel');
  const home=document.getElementById('sidebar-home') || dialog?.querySelector('[data-sidebar-view="home"]');
  const actions=home?.querySelector('.sidebar-list-heading > div');
  if(!panel || !actions || typeof api!=='function') throw new Error('无法初始化文件夹入口。');

  const make=(tag,className,text)=>{
    const node=document.createElement(tag);
    if(className) node.className=className;
    if(text!==undefined) node.textContent=text;
    return node;
  };
  function button(text,className='') {
    const node=make('button',className,text);node.type='button';return node;
  }
  function folderIcon() {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('fill','none');
    svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.5');
    svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');
    svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d','M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3V7Z');
    svg.append(path);return svg;
  }

  const launcher=button('');launcher.id='sidebar-open-folder';
  launcher.dataset.sidebarPage='import';launcher.title='打开文件夹';
  launcher.setAttribute('aria-label','打开文件夹');launcher.append(folderIcon());
  actions.insertBefore(launcher,actions.firstChild);

  const page=make('section','sidebar-page sidebar-detail sidebar-folder-import');
  page.id='sidebar-folder-import';page.dataset.sidebarView='import';page.hidden=true;page.inert=true;
  page.setAttribute('aria-labelledby','folder-import-title');
  const heading=make('div','sidebar-page-heading');
  const back=button('‹ 返回场景');back.dataset.sidebarHome='';
  const title=make('h2','','打开文件夹');title.id='folder-import-title';heading.append(back,title);
  const body=make('div','sidebar-page-body');
  const help=make('p','folder-import-help','浏览工作台服务所在机器的文件夹；局域网访问时，这里显示的是服务机器的目录。');
  help.id='folder-import-help';
  const form=make('form','folder-import-form');
  const label=make('label','','文件夹路径');label.htmlFor='folder-import-path';
  const pathRow=make('div','folder-import-path-row');
  const input=make('input');input.type='text';input.id='folder-import-path';
  input.autocomplete='off';input.spellcheck=false;input.placeholder='输入或浏览文件夹路径';
  input.setAttribute('aria-describedby','folder-import-help folder-import-description');
  const browse=button('前往','folder-import-browse');browse.id='folder-import-browse';
  browse.title='浏览输入的路径';pathRow.append(input,browse);
  const navigation=make('div','folder-import-navigation');
  const up=button('↑ 上一级','folder-import-up');up.id='folder-import-up';
  const browseStatus=make('p','muted folder-import-browse-status');
  browseStatus.id='folder-import-browse-status';browseStatus.setAttribute('role','status');
  navigation.append(up,browseStatus);
  const list=make('ul','folder-import-directory-list');list.id='folder-import-directories';
  list.setAttribute('aria-label','子文件夹');
  const empty=make('p','muted folder-import-empty','选择要打开的文件夹。');
  const description=make('p','folder-import-description','导入时会自动检查实例；不通过的实例会显示原因，其他实例加入场景列表。');
  description.id='folder-import-description';
  const submit=button('打开并导入','folder-import-submit');submit.type='submit';submit.id='folder-import-submit';
  const readyResult=make('section','folder-ready-result');readyResult.id='folder-ready-result';readyResult.hidden=true;
  readyResult.setAttribute('aria-label','导入检查结果');
  const readySummary=make('p','folder-ready-summary');readySummary.id='folder-ready-summary';readySummary.setAttribute('role','status');
  const readyInstances=make('div','folder-ready-instances');readyInstances.id='folder-ready-instances';
  const readyNotes=make('div','folder-ready-notes');readyResult.append(readySummary,readyInstances,readyNotes);
  const resultStatus=make('p','folder-import-result');resultStatus.id='folder-import-result';
  resultStatus.setAttribute('role','status');
  const errorDetails=make('details','folder-import-errors');errorDetails.hidden=true;
  const errorSummary=make('summary');const errorList=make('ul');errorDetails.append(errorSummary,errorList);
  form.append(label,pathRow,navigation,list,empty,description,submit,resultStatus,readyResult,errorDetails);
  body.append(help,form);page.append(heading,body);panel.append(page);

  const storageKey='workspace-folder-import-pending-v1';
  const pending=new Map();
  try {
    const saved=JSON.parse(sessionStorage.getItem(storageKey) || '[]');
    if(Array.isArray(saved)) for(const entry of saved) {
      if(Array.isArray(entry) && typeof entry[0]==='string' && /^[a-f0-9]{32}$/.test(entry[1])) pending.set(entry[0],entry[1]);
    }
  } catch { /* Private browsing may make session storage unavailable. */ }
  function savePending() {
    try {sessionStorage.setItem(storageKey,JSON.stringify([...pending]));} catch {}
  }
  function requestId() {
    if(globalThis.crypto?.randomUUID) return crypto.randomUUID().replaceAll('-','');
    const bytes=new Uint8Array(16);
    if(globalThis.crypto?.getRandomValues) crypto.getRandomValues(bytes);
    else for(let index=0;index<bytes.length;index++) bytes[index]=Math.floor(Math.random()*256);
    return [...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');
  }
  let currentPath='',parentPath=null,directories=[],browseEpoch=0;
  let loading=false,importing=false,loaded=false,active=false;
  function enabled() {return !!isEnabled();}
  function notifyError(error) {
    try {onError(error.message || String(error),error);} catch {}
  }
  function clearResult() {
    resultStatus.textContent='';resultStatus.classList.remove('is-error');
    errorDetails.hidden=true;errorDetails.open=false;errorList.replaceChildren();
  }
  function clearReadyResult() {
    readyResult.hidden=true;readySummary.textContent='';readySummary.classList.remove('is-error');
    readyInstances.replaceChildren();readyNotes.replaceChildren();
  }
  function renderControls() {
    const available=enabled(),busy=loading || importing,path=input.value.trim();
    launcher.hidden=!available;launcher.disabled=importing;
    input.disabled=!available || importing;
    browse.disabled=!available || busy;
    up.disabled=!available || busy || !parentPath || path!==currentPath;
    submit.disabled=!available || busy || !path;
    submit.textContent=importing?'正在打开…':'打开并导入';
    page.setAttribute('aria-busy',String(busy));
    for(const entry of list.querySelectorAll('button')) entry.disabled=!available || busy;
  }
  function renderDirectories() {
    list.replaceChildren();
    const matches=input.value.trim()===currentPath;
    if(matches) for(const directory of directories) {
      if(typeof directory?.name!=='string' || typeof directory?.path!=='string') continue;
      const item=make('li');const entry=button('','folder-import-directory');
      entry.append(folderIcon(),make('span','folder-import-directory-name',directory.name));
      const arrow=make('span','folder-import-directory-arrow','›');arrow.setAttribute('aria-hidden','true');
      entry.append(arrow);entry.title=directory.path;
      entry.addEventListener('click',()=>load(directory.path));item.append(entry);list.append(item);
    }
    empty.hidden=!!list.children.length || loading;
    empty.textContent=matches && loaded?'这个文件夹没有子文件夹。':'点击「前往」浏览这个路径，或直接打开并导入。';
    renderControls();
  }
  async function load(path) {
    if(importing || !enabled()) {renderControls();return null;}
    const chosen=path===undefined ? input.value.trim() || String(getInitialPath() || '').trim() : String(path || '').trim();
    const ticket=++browseEpoch;
    input.value=chosen;loading=true;clearResult();clearReadyResult();browseStatus.classList.remove('is-error');
    browseStatus.textContent='正在读取…';renderDirectories();
    try {
      const result=await api('/api/projects/folders',{method:'POST',body:chosen?{path:chosen}:{}});
      if(ticket!==browseEpoch) return null;
      if(typeof result.path!=='string') throw new Error('服务没有返回可浏览的文件夹路径。');
      currentPath=result.path;parentPath=typeof result.parent==='string'?result.parent:null;
      directories=Array.isArray(result.directories)?result.directories:[];
      input.value=currentPath;loaded=true;
      browseStatus.textContent=result.truncated?'子文件夹较多，仅显示部分；可输入完整路径前往。':'';
      return result;
    } catch(error) {
      if(ticket!==browseEpoch) return null;
      browseStatus.textContent='无法浏览：'+(error.message || String(error));
      browseStatus.classList.add('is-error');notifyError(error);return null;
    } finally {
      if(ticket===browseEpoch) {loading=false;renderDirectories();}
    }
  }
  const readyLabels={ready:'通过',warning:'有提醒',partial:'部分可导入',blocked:'需修复',empty:'没有找到实例'};
  function shortFile(value) {return String(value).split(/[\\/]/).filter(Boolean).at(-1) || String(value);}
  function sizeLabel(bytes) {
    if(!Number.isFinite(bytes) || bytes<0) return null;
    return bytes>=1024*1024?(bytes/(1024*1024)).toFixed(1)+' MB':Math.ceil(bytes/1024)+' KB';
  }
  function metricLabel(metrics) {
    if(!metrics || typeof metrics!=='object') return '';
    const values=[],size=sizeLabel(metrics.glb_bytes);
    if(size) values.push('GLB '+size);
    for(const [key,label] of [['mesh_count','网格'],['view_count','机位'],['frame_count','帧'],['camera_count','相机'],['static_image_count','参考图'],['animation_count','动画']]) {
      if(Number.isInteger(metrics[key]) && (metrics[key]>0 || key==='camera_count' && metrics.frame_count>0)) values.push(metrics[key]+' '+label);
    }
    return values.join(' · ');
  }
  function showReadyResult(result) {
    clearReadyResult();
    if(!result || typeof result!=='object') return;
    const counters=result.counters || {},instances=Array.isArray(result.instances)?result.instances:[];
    if(!counters.warning && !counters.blocked && !result.truncated && !['empty','blocked'].includes(result.status)) return;
    readyResult.hidden=false;
    readySummary.classList.toggle('is-error',['blocked','empty'].includes(result.status));
    const status=readyLabels[result.status] || '检查完成';
    readySummary.textContent=status+' · 通过 '+Number(counters.ready || 0)+' · 提醒 '+Number(counters.warning || 0)+' · 需修复 '+Number(counters.blocked || 0);
    if(result.status==='empty') readySummary.textContent='未识别到 ready 实例。请检查目录路径，或补充明确的 ready 清单。';
    if(result.truncated) readySummary.textContent+='。检查达到扫描边界，请选择更具体的文件夹继续。';
    const reported=instances.filter(instance=>instance.status!=='ready');
    for(const instance of reported.slice(0,100)) {
      const item=make('details','folder-ready-instance');item.dataset.status=instance.status || 'blocked';
      const summary=make('summary');
      summary.append(make('span','folder-ready-badge',readyLabels[instance.status] || '需检查'),make('span','folder-ready-name',instance.name || shortFile(instance.path || '') || '未命名实例'));
      const content=make('div','folder-ready-instance-body');
      if(instance.validation_basis==='managed') content.append(make('p','folder-ready-metrics','已保存场景'));
      const metrics=metricLabel(instance.metrics);if(metrics) content.append(make('p','folder-ready-metrics',metrics));
      if(typeof instance.path==='string') content.append(make('p','folder-ready-source',instance.path));
      const checks=make('ul','folder-ready-checks');
      for(const value of (Array.isArray(instance.checks)?instance.checks:[]).slice(0,50)) {
        const entry=make('li','folder-ready-check');entry.dataset.status=value.status || 'warning';
        entry.append(make('span','folder-ready-check-status',({pass:'通过',warning:'提醒',error:'需修复'})[value.status] || '提示'),
          make('span','folder-ready-check-message',typeof value.message==='string'?value.message:'请检查这一项。'));
        if(typeof value.file==='string') {const file=make('span','folder-ready-file',shortFile(value.file));file.title=value.file;entry.append(file);}
        // Compact scalars are useful; matrices and frame arrays stay out of the drawer.
        if(value.details && typeof value.details==='object' && !Array.isArray(value.details)) {
          const extra=Object.entries(value.details).filter(([key,detail])=>
            !['camera_to_world','world_to_camera','intrinsics','path','file'].includes(key) &&
            (typeof detail==='number' && Number.isFinite(detail) || typeof detail==='string' && detail.length<=160 || typeof detail==='boolean')).slice(0,8);
          if(extra.length) {
            const detail=make('details','folder-ready-extra');detail.append(make('summary','','更多信息'));
            const labels={glb_bytes:'模型字节数',mesh_count:'网格',node_count:'节点',animation_count:'动画',frames:'参考帧',cameras:'带相机帧'};
            const text=make('p','');text.textContent=extra.map(([key,value])=>(labels[key] || key)+': '+String(value)).join(' · ');detail.append(text);entry.append(detail);
          }
        }
        checks.append(entry);
      }
      content.append(checks);item.append(summary,content);readyInstances.append(item);
    }
    const notes=[...(Array.isArray(result.errors)?result.errors:[]),...(Array.isArray(result.skipped)?result.skipped:[])];
    if(notes.length) {
      const details=make('details','folder-ready-scan-notes');details.append(make('summary','',`跳过与扫描提示 ${notes.length}`));
      const entries=make('ul');
      for(const note of notes.slice(0,100)) {
        const item=make('li');
        if(typeof note.path==='string') item.append(make('strong','',shortFile(note.path)));
        item.append(make('span','',note.error || note.reason || '已跳过。'));entries.append(item);
      }
      details.append(entries);readyNotes.append(details);
    }
    if(reported.length>100) readyNotes.append(make('p','','仅显示前 100 个提示实例，请选择更具体的文件夹继续。'));
  }
  function showResult(result) {
    const imported=Array.isArray(result.imported)?result.imported:[];
    const skipped=Array.isArray(result.skipped)?result.skipped:[];
    const errors=Array.isArray(result.errors)?result.errors:[];
    const details=[...errors,...skipped.map(item=>({...item,error:item.reason || '已跳过。'}))];
    clearResult();
    if(!imported.length && !skipped.length && !errors.length) {
      resultStatus.textContent='这个文件夹中没有可导入的已准备好实例。';
    } else {
      resultStatus.textContent=`已导入 ${imported.length} · 已跳过 ${skipped.length} · 失败 ${errors.length}`;
      if(!imported.length && skipped.length && !errors.length) resultStatus.textContent+='。没有新增场景。';
    }
    const modelOnly=(Array.isArray(result.projects)?result.projects:[]).filter(project=>{
      const source=project?.import_source;
      return source && !source.manifest && !(Array.isArray(source.reference_images) && source.reference_images.length);
    }).length;
    if(modelOnly) resultStatus.textContent+=`。其中 ${modelOnly} 个实例只包含模型，尚无参考图。`;
    if(result.truncated)resultStatus.textContent+='。扫描达到边界，尚未遍历全部目录；请选择更具体的文件夹继续。';
    if(errors.length) {
      resultStatus.classList.toggle('is-error',!(Array.isArray(result.projects) && result.projects.length));
      if(typeof errors[0]?.error==='string') resultStatus.textContent+='。'+errors[0].error;
    }
    if(result.ready_check?.can_import===false && !(Array.isArray(result.projects) && result.projects.length)) {
      resultStatus.classList.add('is-error');
      if(result.ready_check.status==='empty') resultStatus.textContent='导入失败：未识别到已准备好的实例。';
    }
    if(details.length) {
      errorDetails.hidden=false;errorSummary.textContent=`查看 ${details.length} 项跳过与失败详情`;
      for(const error of details.slice(0,100)) {
        const item=make('li');
        if(typeof error?.path==='string') item.append(make('strong','',error.path));
        item.append(make('span','',typeof error?.error==='string'?error.error:'导入失败。'));
        errorList.append(item);
      }
      if(details.length>100)errorList.append(make('li','','仅展示前 100 项；可选择更具体的文件夹继续。'));
    }
    return errors.length;
  }
  async function importFolder() {
    const path=input.value.trim();
    if(importing || loading || !enabled() || !path) {renderControls();return null;}
    if(!pending.has(path)) {pending.set(path,requestId());savePending();}
    const id=pending.get(path);++browseEpoch;importing=true;clearResult();clearReadyResult();
    resultStatus.textContent='正在检查并导入已准备好的实例…';renderControls();
    try {
      let result;
      try {
        result=await api('/api/projects/import-folder',{method:'POST',body:{path,request_id:id}});
        if(!showResult(result)) {pending.delete(path);savePending();}
        showReadyResult(result.ready_check);
      } catch(error) {
        resultStatus.textContent='导入结果未确认：'+(error.message || String(error))+'。再次点击会重试同一次请求。';
        resultStatus.classList.add('is-error');notifyError(error);return null;
      }
      try {await onImported(result);} catch(error) {
        resultStatus.textContent+='。场景列表刷新失败，请刷新列表。';notifyError(error);
      }
      return result;
    } finally {importing=false;renderControls();}
  }
  form.addEventListener('submit',event=>{event.preventDefault();importFolder();});
  browse.addEventListener('click',()=>load(input.value.trim()));
  up.addEventListener('click',()=>{if(parentPath) load(parentPath);});
  input.addEventListener('input',()=>{
    // Typing a new destination invalidates the old browse response immediately.
    ++browseEpoch;loading=false;browseStatus.textContent='';browseStatus.classList.remove('is-error');
    clearReadyResult();clearResult();renderDirectories();
  });
  function onPage(value) {
    active=value==='import';
    if(!active) {
      ++browseEpoch;
      if(loading) {loading=false;browseStatus.textContent='';renderDirectories();}
      else renderControls();
      return;
    }
    renderControls();
    if(!loaded && !loading && !importing) return load();
  }
  renderControls();
  return {load,open:load,importFolder,onPage,
    refresh:()=>active?load():renderControls(),refreshAvailability:renderControls};
}
