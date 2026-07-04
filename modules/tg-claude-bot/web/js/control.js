/* control.js — LLM 控制面板（Metro Pivot：运行 / 能力 / 维护 三组横切）*/

let ctrlCache = null;
let ctrlTab = 'run';   // 当前 Pivot 分组（跨重渲染保持）

async function loadControl(s) {
  const name = s || session || 'main';
  try {
    const r = await fetch('/api/control?session=' + encodeURIComponent(name) + '&key=' + KEY);
    ctrlCache = await r.json();
  } catch (e) {
    ctrlCache = null;
  }
}

function showControl() {
  const ov = $('ctrl-overlay');
  ov.classList.remove('hidden');

  if (!ctrlCache) {
    loadControl(session).then(renderControlCard);
  } else {
    renderControlCard();
  }
}

function renderControlCard() {
  const c = ctrlCache;
  const card = $('ctrl-card-inner');
  if (!c) { card.innerHTML = '<div class="ctrl-loading">加载中…</div>'; return; }

  const selRow = (label, name, val, opts) =>
    '<div class="row"><span class="key">' + label + '</span>' +
    '<select data-key="' + name + '">' +
    opts.map(o => '<option value="' + esc(String(o)) + '"' +
      (String(o) === String(val) ? ' selected' : '') + '>' +
      esc(String(o) || '默认') + '</option>').join('') +
    '</select></div>';

  const togRow = (label, name, on) =>
    '<div class="row"><span class="key">' + label + '</span>' +
    '<button class="tog' + (on ? ' on' : '') + '" data-key="' + name +
    '" data-val="' + (!on) + '">' + (on ? '● 开启' : '○ 关闭') + '</button></div>';

  const chips = (arr) => '<div class="chips">' +
    (arr && arr.length
      ? arr.map(x => '<span class="chip on">' + esc(x) + '</span>').join('')
      : '<span class="chip">无</span>') + '</div>';

  // 运行组：模型行为
  let run = '';
  run += selRow('模型', 'model', c.model,
    ['default', 'opus', 'sonnet', 'haiku', 'deepseek-v4-pro', 'deepseek-v4-flash']);
  run += selRow('权限模式', 'permission_mode', c.permission_mode,
    ['default', 'bypassPermissions', 'acceptEdits', 'plan']);
  run += selRow('子代理集', 'subagent_model', c.subagent_model || 'default',
    ['default', 'scout', 'magi']);
  run += selRow('Effort', 'effort', c.effort || '', ['', 'low', 'medium', 'high']);
  run += togRow('Fast Mode', 'fast_mode', !!c.fast_mode);
  run += togRow('Thinking', 'thinking', !!c.thinking);

  // 能力组：MCP / SKILL
  let cap = '';
  cap += '<div class="row col"><span class="key">MCP 服务</span>' + chips(c.mcp_servers) + '</div>';
  cap += '<div class="row col"><span class="key">SKILL · ' + (c.skills || []).length +
    '</span>' + chips((c.skills || []).map(s => s.name)) + '</div>';

  // 维护组：上下文压缩
  let maint = '';
  maint += '<div class="row"><span class="key">上下文</span>' +
    '<button class="act" data-act="compact">🗜 压缩历史</button></div>';

  const groups = [['run', '运行', run], ['cap', '能力', cap], ['maint', '维护', maint]];
  if (!groups.some(g => g[0] === ctrlTab)) ctrlTab = 'run';

  // 会话信息常驻顶部（不进 Pivot 分组）
  let html = '<div class="row"><span class="key">会话</span><span class="val">' +
    esc(c.session) + (c.is_orchestrator ? ' · 主对话' : '') + '</span></div>';
  html += '<div class="ctrl-pivot">' +
    groups.map(g => '<button class="cpv' + (g[0] === ctrlTab ? ' active' : '') +
      '" data-grp="' + g[0] + '">' + g[1] + '</button>').join('') + '</div>';
  html += groups.map(g => '<div class="ctrl-group" data-grp="' + g[0] + '"' +
    (g[0] === ctrlTab ? '' : ' hidden') + '>' + g[2] + '</div>').join('');

  card.innerHTML = html;

  // Pivot 切换：只切显隐 + 高亮，不重渲（控件监听保持）
  card.querySelectorAll('.cpv').forEach(btn =>
    btn.addEventListener('click', () => {
      ctrlTab = btn.dataset.grp;
      card.querySelectorAll('.cpv').forEach(b => b.classList.toggle('active', b.dataset.grp === ctrlTab));
      card.querySelectorAll('.ctrl-group').forEach(grp => { grp.hidden = (grp.dataset.grp !== ctrlTab); });
    }));

  card.querySelectorAll('select').forEach(sel =>
    sel.addEventListener('change', () => ctrlAction(sel.dataset.key, sel.value)));
  card.querySelectorAll('button.tog').forEach(btn =>
    btn.addEventListener('click', () => ctrlAction(btn.dataset.key, btn.dataset.val === 'true')));
  card.querySelectorAll('button.act').forEach(btn =>
    btn.addEventListener('click', () => ctrlAction(btn.dataset.act)));
}

async function ctrlAction(key, value) {
  try {
    const body = { session: session || 'main', key: key };
    if (value !== undefined) body.value = value;
    const r = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
      body: JSON.stringify(body)
    });
    const j = await r.json();
    toast(j.message || j.error || (j.ok ? 'OK' : '失败'));
    // 刷新面板回显最新真实状态（开关/下拉据后端值复位，避免与实际不同步）
    await loadControl(session);
    if (!$('ctrl-overlay').classList.contains('hidden')) renderControlCard();
  } catch (e) {
    toast('操作失败');
  }
}

// 关闭按钮
document.addEventListener('DOMContentLoaded', () => {
  $('ctrl-close').addEventListener('click', () => {
    $('ctrl-overlay').classList.add('hidden');
  });
  $('ctrl-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) $('ctrl-overlay').classList.add('hidden');
  });
});

// 控制面板按钮
$('ctrlBtn').addEventListener('click', () => {
  loadControl(session).then(showControl);
});
