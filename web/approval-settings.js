// Explicit, per-session browser preference. Every response uses the existing capability-checked API.
export function setupApprovalSettings({api, onWorkspace}) {
  const panel = document.getElementById('approval-settings');
  const select = document.getElementById('approval-mode');
  const status = document.getElementById('approval-mode-status');
  const kinds = new Set(['item/commandExecution/requestApproval', 'item/fileChange/requestApproval']);
  const modelingTools = new Set(['workspace_preview_world', 'workspace_inspect_world', 'workspace_build_world', 'workspace_evaluate_world']);
  let workspace = {}, key = '', running = false, message = '';

  function mode() { return key && localStorage.getItem(key) === 'automatic' ? 'automatic' : 'manual'; }
  function render() {
    panel.hidden = workspace.delivery_mode !== 'appserver' || !workspace.session_id;
    select.disabled = !navigator.locks || !key;
    select.value = mode();
    status.textContent = !navigator.locks ? '当前浏览器不支持自动确认，请继续手动审批。'
      : message || (mode() === 'automatic' ? '自动确认已开启；关闭页面后暂停。' : '每次操作由你点击允许或拒绝。');
  }
  function eligible(request, state) {
    if (request.details_truncated || request.source_thread_id && request.source_thread_id !== state.thread_id) return false;
    if (kinds.has(request.kind)) return true;
    const details = request.details || {}, schema = details.requestedSchema;
    const tool = details.message?.match(/^Allow the scene_feedback MCP server to run tool "([a-z_]+)"\?$/)?.[1];
    return request.kind === 'mcpServer/elicitation/request' && details.serverName === 'scene_feedback' &&
      details._meta?.codex_approval_kind === 'mcp_tool_call' && modelingTools.has(tool) && details.mode === 'form' &&
      schema?.type === 'object' && Object.keys(schema.properties || {}).length === 0 && !schema.required?.length;
  }
  async function process() {
    if (running || panel.hidden || !navigator.locks || mode() !== 'automatic' ||
        !workspace.approvals?.some(request => eligible(request, workspace))) return;
    running = true;
    const scope = key, session = workspace.session_id, thread = workspace.thread_id;
    try {
      // Multiple open tabs share both the lock and attempt receipts; an uncertain response is never replayed.
      await navigator.locks.request(scope, {ifAvailable: true}, async lock => {
        if (!lock || key !== scope || mode() !== 'automatic') return;
        let latest = await api('/api/workspace/state');
        if (latest.session_id !== session || latest.thread_id !== thread) return;
        for (const request of latest.approvals || []) {
          if (key !== scope || mode() !== 'automatic') break;
          if (!eligible(request, latest)) continue;
          const receipt = `${scope}:attempt:${request.approval_id}`;
          if (localStorage.getItem(receipt)) continue;
          localStorage.setItem(receipt, 'submitted');
          await api(`/api/workspace/approvals/${encodeURIComponent(request.approval_id)}/respond`, {
            method: 'POST', body: {decision: 'accept'},
          });
          localStorage.setItem(receipt, 'responded');
          message = '最近一次操作已自动允许，可在执行记录中查看。';
        }
        latest = await api('/api/workspace/state');
        if (key === scope && latest.session_id === session) onWorkspace(latest);
      });
    } catch (error) {
      localStorage.setItem(scope, 'manual');
      if (key === scope) message = `自动确认已暂停：${error.message}。请核对待确认操作后手动处理。`;
    } finally {
      running = false;
      render();
    }
  }
  function update(next) {
    const nextKey = next.session_id ? `harness:approval-mode:${next.project_id || next.project_dir}:${next.session_id}` : '';
    if (key !== nextKey) message = '';
    workspace = next;
    key = nextKey;
    render();
    process();
  }
  select.addEventListener('change', () => {
    localStorage.setItem(key, select.value);
    message = '';
    render();
    process();
  });
  window.addEventListener('storage', event => {
    if (event.key === key) { message = ''; render(); process(); }
  });
  render();
  return {update};
}
