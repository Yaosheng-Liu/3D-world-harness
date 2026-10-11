// Existing CLI threads keep their history; only future turns change settings.
export function setupModelSettings({api, onWorkspace, isSubmitting=()=>false, onSaving=()=>{}}) {
  const id=name=>document.getElementById(name);
  const entry=id('model-settings-button'), current=id('model-settings-current');
  const form=id('model-settings-form'), model=id('session-model'), effort=id('session-effort');
  const save=id('save-model-settings'), retry=id('refresh-session-models'), status=id('model-settings-status');
  const effortNames={minimal:'最少',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最大',ultra:'极高',none:'无'};
  let workspace={}, models=[], baseline=null, dirty=false, loading=false, saving=false, message='', loadError=false;
  const same=(a,b)=>a?.model===b?.model && a?.reasoning_effort===b?.reasoning_effort;
  function selected() {return models.find(item=>item.model===model.value);}
  function fillEfforts(value='') {
    const item=selected();
    effort.replaceChildren(new Option(item?.default_reasoning_effort
      ? `模型默认（${effortNames[item.default_reasoning_effort] || item.default_reasoning_effort}）` : '模型默认',''));
    for(const key of item?.supported_reasoning_efforts || []) effort.add(new Option(effortNames[key] || key,key));
    effort.value=[...effort.options].some(o=>o.value===value)?value:'';
  }
  function fillModels() {
    const choice=dirty?model.value:workspace.model_settings?.model;
    model.replaceChildren(new Option('选择模型',''));
    for(const item of models) model.add(new Option(item.display_name || item.model,item.model));
    if(choice && !models.some(item=>item.model===choice)) {
      const unavailable=new Option(`${choice}（当前不可选）`,choice);unavailable.disabled=true;model.add(unavailable);
    }
    model.value=choice || '';
    fillEfforts(dirty?effort.value:workspace.model_settings?.reasoning_effort || '');
  }
  function render() {
    const supported=workspace.model_settings_supported===true;
    entry.hidden=!supported;
    const settings=workspace.model_settings || {};
    const label=models.find(item=>item.model===settings.model)?.display_name || settings.model || '启动时默认模型';
    current.textContent=label+(settings.reasoning_effort?` · ${effortNames[settings.reasoning_effort] || settings.reasoning_effort}`:'');
    const busy=workspace.model_settings_busy || isSubmitting();
    const locked=!supported || busy || loading || saving;
    model.disabled=locked || !models.length;
    effort.disabled=locked || !selected()?.supported_reasoning_efforts?.length;
    save.disabled=locked || !dirty || !selected();
    retry.disabled=loading || saving;
    save.textContent=saving?'保存中…':'应用到下一条反馈';
    form.setAttribute('aria-busy',String(saving || loading));
    status.textContent=saving?'正在保存…':busy?'请先完成、停止或处理当前及排队中的反馈。':loading?'正在读取可用模型…':message || '沿用当前会话，场景、标记和聊天记录保留。';
    status.dataset.error=String(loadError);
  }
  function update(next) {
    const changed=!same(workspace.model_settings,next.model_settings);
    workspace=next;
    if(baseline===null || changed && (!dirty || !saving)) {
      dirty=false;baseline={...next.model_settings};
      fillModels();
    }
    render();
  }
  async function open() {
    if(loading || saving || !workspace.model_settings_supported) return;
    loading=true;message='';loadError=false;render();
    try {
      // Refresh both the catalog and the current saved selection on every visit.
      const [catalog, latest]=await Promise.all([api('/api/workspace/models'),api('/api/workspace/state')]);
      models=catalog.models || [];onWorkspace(latest);fillModels();
      if(!models.length) message='当前账号没有可用的视觉模型，请稍后刷新。';
    } catch(error) {message='读取失败：'+error.message+'。可点击刷新重试。';loadError=true;}
    finally {loading=false;render();}
  }
  model.addEventListener('change',()=>{dirty=true;message='';fillEfforts();render();});
  effort.addEventListener('change',()=>{dirty=true;message='';render();});
  retry.addEventListener('click',open);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(save.disabled) return;
    const body={model:model.value,reasoning_effort:effort.value || null,expected_settings:baseline};
    saving=true;loadError=false;onSaving(true);render();
    try {
      const result=await api('/api/workspace/model-settings',{method:'POST',body});
      dirty=false;onWorkspace(result.workspace);message='已保存，下一条反馈使用新的设置。';
    } catch(error) {
      message='未能确认保存：'+error.message;loadError=true;
      // A lost response may still have committed. Never silently replay a write.
      try {onWorkspace(await api('/api/workspace/state'));} catch { /* Leave the draft visible for retry. */ }
    } finally {saving=false;onSaving(false);render();}
  });
  render();
  return {update,open};
}
