/* sidebar.js — 侧边栏：Todo / Brain / Queue 面板 */

const T_STATUS_ICONS = {
  completed: ST_ICON.done, done: ST_ICON.done,
  in_progress: ST_ICON.running, active: ST_ICON.running, running: ST_ICON.running,
  cancelled: ST_ICON.interrupted, skipped: ST_ICON.interrupted,
  pending: '○', error: ST_ICON.error,
};

function renderTodos() {
  const container = $('todos');
  if (!container) return;
  let html = '';

  // 按 session 分组
  const sessions = Object.keys(todosBy);
  if (!sessions.length) {
    html = '<div style="color:var(--text3);font-size:11px;">暂无待办</div>';
    if (container._prevHtml === html) return;
    container._prevHtml = html;
    container.innerHTML = html;
    return;
  }

  // Tab: 当前 active session 优先
  const active = session; // from app.js global
  const ordered = active && sessions.includes(active)
    ? [active, ...sessions.filter(s => s !== active)]
    : sessions;

  ordered.forEach(sname => {
    const todos = todosBy[sname] || [];
    if (!todos.length) return;
    const done = todos.filter(t => t.status === 'completed' || t.status === 'done').length;
    const total = todos.length;
    const pct = total ? Math.round(done / total * 100) : 0;

    html += '<div class="sec-title">📋 ' + esc(sname) + ' (' + done + '/' + total + ')</div>';
    html += '<div class="tprog"><div class="fill" style="width:' + pct + '%"></div></div>';

    todos.forEach((t, i) => {
      const icon = T_STATUS_ICONS[t.status] || '○';
      const cls = t.status === 'completed' || t.status === 'done' ? 'completed'
        : (t.status === 'in_progress' || t.status === 'active' ? 'in_progress' : '');
      html += '<div class="todo-item ' + cls + '">' +
        '<span class="icon">' + icon + '</span>' +
        '<span class="label">' + esc(t.text || t.label || '') + '</span>' +
        '<span class="del" data-sid="' + esc(sname) + '" data-idx="' + i + '">✕</span>' +
        '</div>';
    });
  });

  // Diff guard: skip rebuild if identical
  if (container._prevHtml === html) return;
  container._prevHtml = html;
  container.innerHTML = html;

  // 绑定删除按钮（本地即时移除 + 通知后端）
  container.querySelectorAll('.del').forEach(el => {
    el.addEventListener('click', async () => {
      const sid = el.dataset.sid;
      const idx = parseInt(el.dataset.idx);
      if (todosBy[sid]) {
        todosBy[sid].splice(idx, 1);
        if (!todosBy[sid].length) delete todosBy[sid];
        renderTodos();
      }
      try {
        await fetch('/api/todo/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
          body: JSON.stringify({ session: sid, index: idx })
        });
      } catch (e) {}
      toast('已删除');
    });
  });
}

/* ── 待办手动查询 / 清空（侧栏按钮）── */
// 查询：从事件日志重新派生各 session 的最新待办快照（无需后端改造即可用）。
async function loadTodos() {
  try {
    const r = await fetch('/api/events?since=0&limit=1000&wait=0&key=' + KEY);
    const j = await r.json();
    const latest = {};
    (j.events || []).forEach(ev => {
      const sn = ev.session || 'main';
      if (ev.type === 'todo') {
        latest[sn] = ev.items || [];
      } else if (ev.type === 'tool' && ev.input && Array.isArray(ev.input.todos)) {
        latest[sn] = ev.input.todos.map(t => ({
          text: t.content || t.activeForm || t.subject || t.description || '',
          status: (t.status || t.state || 'pending').toLowerCase(),
        }));
      }
    });
    Object.keys(latest).forEach(k => { todosBy[k] = latest[k]; });
    renderTodos();
    toast('待办已刷新');
  } catch (e) { toast('刷新失败'); }
}

function clearTodos() {
  if (session) delete todosBy[session];
  else Object.keys(todosBy).forEach(k => delete todosBy[k]);
  renderTodos();
  toast('已清空当前视图待办');
}

document.addEventListener('DOMContentLoaded', () => {
  const rf = $('todoRefresh'); if (rf) rf.addEventListener('click', loadTodos);
  const cl = $('todoClear'); if (cl) cl.addEventListener('click', clearTodos);
});

/* fmtAge：相对时间格式化（hub.js 等复用）。 */
function fmtAge(s) {
  if (s == null) return '';
  if (s < 0) s = 0;
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  return Math.round(s / 3600) + 'h';
}

/* 会话池（cursor brain pool）已移除；保留 refreshBrain 空桩供 app.js 定时调用。 */
function refreshBrain() { /* removed: cursor session pool */ }

/* ── Queue 面板 ── */
function renderQueue(queues) {
  const sec = document.getElementById('queueSec');
  if (!sec) return;
  let html = '<div class="sec-title">⏳ 排队</div>';
  let has = false;
  Object.entries(queues || {}).forEach(([name, q]) => {
    if (!q || !q.length) return;
    has = true;
    html += '<div style="font-weight:600;font-size:11px;">' + esc(name) + '</div>';
    q.forEach(qm => {
      html += '<div style="font-size:10px;padding:2px 0;display:flex;justify-content:space-between;">' +
        '<span>' + esc((qm.preview || '').slice(0, 40)) + '</span>' +
        '<span><button data-token="' + qm.token + '" data-act="steer" class="qbtn">⤷</button>' +
        '<button data-token="' + qm.token + '" data-act="cancel" class="qbtn">✕</button></span>' +
        '</div>';
    });
  });
  if (!has) html += '<div style="color:var(--text3);font-size:11px;">空</div>';
  if (sec._prevHtml === html) return;
  sec._prevHtml = html;
  sec.innerHTML = html;

  sec.querySelectorAll('.qbtn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await fetch('/api/queue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
          body: JSON.stringify({ token: btn.dataset.token, action: btn.dataset.act })
        });
      } catch (e) {}
    });
  });
}

/* ── Background tasks panel ── */
let _bgTasks = {};

function updateBgTask(task) {
  if (!task || !task.id) return;
  _bgTasks[task.id] = task;
  renderBgTasks();
}

function renderBgTasks() {
  const sec = document.getElementById('bgTasksSec');
  if (!sec) return;
  const tasks = Object.values(_bgTasks).sort((a, b) => {
    const order = { running: 0, queued: 1 };
    return (order[a.status] || 2) - (order[b.status] || 2);
  });
  if (!tasks.length) {
    sec.classList.add('hidden');
    return;
  }
  sec.classList.remove('hidden');
  const list = document.getElementById('bgTasksList');
  if (!list) return;
  const icons = { queued: '⏳', running: '◐', done: '✓', error: '✗', killed: '⊘' };
  const html = tasks.slice(0, 8).map(t => {
    const icon = icons[t.status] || '○';
    const cls = 'bgt-' + t.status;
    const dur = t.duration_s ? t.duration_s + 's' : '';
    return '<div class="bgt-item ' + cls + '">' +
      '<span class="bgt-icon">' + icon + '</span>' +
      '<span class="bgt-label">' + esc(t.label || t.command || 'task') + '</span>' +
      (dur ? '<span class="bgt-dur">' + dur + '</span>' : '') +
      (t.status === 'running' ? '<button class="bgt-kill" data-tid="' + t.id + '">✕</button>' : '') +
      '</div>';
  }).join('');
  if (list._prevHtml === html) return;
  list._prevHtml = html;
  list.innerHTML = html;
  // Kill button handlers
  list.querySelectorAll('.bgt-kill').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await fetch('/api/task/' + btn.dataset.tid + '/kill', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY }
        });
      } catch (e) {}
    });
  });
}

// Poll tasks every 10s
setInterval(async () => {
  try {
    const r = await fetch('/api/tasks?key=' + KEY);
    const j = await r.json();
    (j.tasks || []).forEach(t => { _bgTasks[t.id] = t; });
    renderBgTasks();
  } catch (e) {}
}, 10000);
