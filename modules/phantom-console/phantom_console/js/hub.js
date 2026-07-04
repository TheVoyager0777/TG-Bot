/* hub.js — Metro Panorama Hub：全屏多面板【无界横向滚动】+ 视差大标题 + 实时汇总磁贴。
 *
 * Metro Panorama 的精髓：一个比屏幕宽很多的画布，多个内容面板横向排开，背景大标题
 * 比面板滚得慢（视差）。叠加全屏视图（顶栏 ⊞ HUB 开 / ✕ / ESC 关），数据全部复用现有
 * 全局（stateCache 会话 / todosBy 待办 / #dot 连接），不改既有逻辑。
 *
 * 刷新模式（重要）：打开期间每 3s 拉一次数据，但【按面板增量刷新】——每个面板算数据
 * 签名，没变就不重写 innerHTML（否则磁贴入场动画每 3s 重放、滚动位置/点击态被重置 =
 * 不合理的"全局刷新"）。各面板按数据签名增量刷新，结构不变不重建。
 * 新心跳文本，不重建。全程 try/catch，出错只影响 Hub。 */

let hubTimer = null;
let hubLastScrollAt = 0;
let hubPanelRenderTimer = 0;
let hubRenderBusy = false;
let _hubActiveLastFetch = 0;
// 各面板上次渲染的数据签名（心跳秒数等高频字段不计入 → 不触发重建）
const hubSig = { ov: '', td: '', se: '', act: '' };

function hubLowPower() {
  const root = document.documentElement;
  return !!(root && (root.classList.contains('renderer-low-power') ||
    root.classList.contains('renderer-no-gpu') || root.dataset.renderTier === 'low'));
}
function hubPollMs() { return hubLowPower() ? 15000 : 8000; }
function hubNetworkMs(kind) {
  if (kind === 'build') return hubLowPower() ? 30000 : 15000;
  if (kind === 'active') return hubLowPower() ? 20000 : 10000;
  return hubPollMs();
}
function hubPanelId() {
  const p = hubPanelEls()[hubCurrentPanel()];
  const body = p && p.querySelector && p.querySelector('.hub-body');
  return body && body.id || '';
}
function hubSchedulePanelRender() {
  if (hubPanelRenderTimer) clearTimeout(hubPanelRenderTimer);
  hubPanelRenderTimer = setTimeout(() => {
    hubPanelRenderTimer = 0;
    hubRender();
  }, hubLowPower() ? 900 : 420);
}

/* ── Metro 强调色选色器（WP 经典 accent 自选）。每项 = 主/亮/暗 三色，套到 --accent/2/3，
 * localStorage 记忆，启动即套用。选色面板是静态的，只构建一次、永不参与刷新。 */
const HUB_ACCENTS = [
  { a: '#0078d7', b: '#3b9dff', c: '#005a9e', n: '蓝' },
  { a: '#0099bc', b: '#34c0dd', c: '#00718c', n: '青' },
  { a: '#00b294', b: '#2dd3b5', c: '#00866e', n: '翠' },
  { a: '#107c10', b: '#3aa83a', c: '#0b5a0b', n: '绿' },
  { a: '#b4009e', b: '#e23bcb', c: '#85006f', n: '品红' },
  { a: '#5c2d91', b: '#8a5cc7', c: '#42206a', n: '紫' },
  { a: '#d83b01', b: '#ff6a33', c: '#a82d00', n: '橙' },
  { a: '#e81123', b: '#ff4757', c: '#b00d1b', n: '红' },
  { a: '#4267b2', b: '#6b89e0', c: '#2f4d8c', n: '钴' },
  { a: '#498205', b: '#6bb12e', c: '#356000', n: '松' },
  { a: '#e3008c', b: '#ff4db8', c: '#a80068', n: '玫' },
  { a: '#6b69d6', b: '#9694ee', c: '#4a48a8', n: '靛' }
];
function hubAccentIdx() {
  let i = 0;
  try { i = parseInt(localStorage.getItem('phantom_accent') || '0', 10) || 0; } catch (e) {}
  return (i >= 0 && i < HUB_ACCENTS.length) ? i : 0;
}
function hubApplyAccent(i, save) {
  const p = HUB_ACCENTS[i];
  if (!p) return;
  const r = document.documentElement.style;
  r.setProperty('--accent', p.a);
  r.setProperty('--accent2', p.b);
  r.setProperty('--accent3', p.c);
  if (save) { try { localStorage.setItem('phantom_accent', String(i)); } catch (e) {} }
  const el = $('hubTheme');
  if (el) el.querySelectorAll('.swatch').forEach((s, k) => s.classList.toggle('on', k === i));
}
function hubRenderTheme() {
  const el = $('hubTheme');
  if (!el || el.dataset.built) return;
  el.dataset.built = '1';   // 静态面板：只构建一次，永不参与周期刷新
  const cur = hubAccentIdx();
  let html = '<div class="hub-swatches">';
  HUB_ACCENTS.forEach((p, i) => {
    html += '<button class="swatch' + (i === cur ? ' on' : '') + '" title="' + esc(p.n) +
      '" data-i="' + i + '" style="background:' + p.a + '"></button>';
  });
  html += '</div><div class="hub-note">选个 Metro 强调色，自动记住（localStorage）。</div>';
  el.innerHTML = html;
  el.querySelectorAll('.swatch').forEach(s =>
    s.addEventListener('click', () => hubApplyAccent(+s.dataset.i, true)));
}

function hubOpen() {
  const h = $('hub');
  if (!h) return;
  h.classList.remove('hidden');
  // Low/no-GPU: skip intro animation; it creates huge repaint bursts on HUB open.
  if (!hubLowPower()) {
    h.classList.add('intro');
    setTimeout(() => h.classList.remove('intro'), 700);
  } else {
    h.classList.remove('intro');
  }
  const sc = $('hubScroller');
  if (sc) sc.scrollLeft = 0;
  const title = $('hubTitle');
  if (title) title.style.transform = 'translateX(0)';
  hubUpdateNav();   // 回到第一面板高亮
  // 重置签名 → 打开时强制当前面板渲染一次最新态；低功耗下网络面板按需懒刷新
  hubSig.ov = hubSig.td = hubSig.se = hubSig.act = '';
  hubRender({ force: true });
  if (hubTimer) clearInterval(hubTimer);
  hubTimer = setInterval(hubRender, hubPollMs());
  try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch (e) {}
}

function hubClose() {
  const h = $('hub');
  if (!h || h.classList.contains('hidden')) return;
  h.classList.add('hidden');
  if (hubTimer) { clearInterval(hubTimer); hubTimer = null; }
}

async function hubRender(opts) {
  const force = !!(opts && opts.force);
  const h = $('hub');
  if (!force && (!h || h.classList.contains('hidden') || document.hidden)) return;
  if (!force && Date.now() - hubLastScrollAt < (hubLowPower() ? 850 : 360)) return;
  if (hubRenderBusy && !force) return;
  hubRenderBusy = true;
  try {
    hubRenderOverview();
    hubRenderTodos();
    hubRenderSessions();
    const panel = hubPanelId();
    const eagerNetwork = force && !hubLowPower();
    if (eagerNetwork || panel === 'hubTakeover') {
      try { await hubInitTakeover(); } catch (e) { /* ignore */ }
    }
    if (eagerNetwork || panel === 'hubActive') {
      try { await hubRefreshActive(force); } catch (e) { /* ignore */ }
    }
    if (eagerNetwork || panel === 'hubBuild') {
      try { await hubRenderBuild(force); } catch (e) { /* ignore */ }
    }
  } catch (e) { /* 渲染异常不外溢 */ }
  finally { hubRenderBusy = false; }
}

function htile(cls, k, v, s) {
  return '<div class="htile ' + (cls || '') + '">' +
    '<div class="hk">' + esc(k) + '</div>' +
    '<div class="hv">' + esc(String(v)) + '</div>' +
    '<div class="hs">' + esc(s || '') + '</div></div>';
}

function hubRenderOverview() {
  const el = $('hubOverview');
  if (!el) return;
  const online = !!($('dot') && $('dot').classList.contains('on'));
  const sessions = (typeof stateCache !== 'undefined' && stateCache.sessions) || [];
  const busy = sessions.filter(s => s.busy).length;
  const queued = sessions.reduce((a, s) => a + (s.queued || 0), 0);
  let tdDone = 0, tdTotal = 0;
  Object.values(typeof todosBy !== 'undefined' ? todosBy : {}).forEach(items => {
    (items || []).forEach(t => { tdTotal++; if (t.status === 'completed' || t.status === 'done') tdDone++; });
  });
  // 已接管会话数（cc-* worker）
  const ccCount = sessions.filter(s => (s.name || '').indexOf('cc-') === 0).length;
  const sig = [online, busy, sessions.length, tdDone, tdTotal, queued, ccCount].join('|');
  if (sig === hubSig.ov && el.firstChild) return;
  hubSig.ov = sig;
  let html = '<div class="hub-tiles">';
  html += htile(online ? 'ok' : 'err', '连接', online ? '在线' : '离线', online ? 'connected' : 'reconnecting');
  html += htile(busy ? 'warn' : 'dim', '忙', busy + '/' + sessions.length, '生成中 / 会话');
  html += htile(ccCount ? '' : 'dim', '已接管', ccCount, 'cc 会话');
  html += htile(tdTotal ? '' : 'dim', '待办', tdTotal ? (tdDone + '/' + tdTotal) : '0', '完成 / 总');
  html += htile(queued ? 'warn' : 'dim', '排队', queued, queued ? '待投递' : '空');
  html += '</div>';
  el.innerHTML = html;
}

function hubRenderTodos() {
  const el = $('hubTodos');
  if (!el) return;
  const all = typeof todosBy !== 'undefined' ? todosBy : {};
  const names = Object.keys(all).filter(n => (all[n] || []).length);
  const sig = names.map(n => n + ':' +
    (all[n] || []).map(t => (t.status || '') + (t.text || '')).join('~')).join('|');
  if (sig === hubSig.td && el.firstChild) return;
  hubSig.td = sig;
  if (!names.length) { el.innerHTML = '<div class="hub-empty">暂无待办</div>'; return; }
  let html = '';
  names.forEach(n => {
    const items = all[n] || [];
    const done = items.filter(t => t.status === 'completed' || t.status === 'done').length;
    html += '<div class="hub-tdgroup"><div class="hub-tdhead">' + esc(n) + ' · ' + done + '/' + items.length + '</div>';
    items.forEach(t => {
      const s2 = (t.status || '').toLowerCase();
      const ic = (s2 === 'completed' || s2 === 'done') ? '✓' : s2 === 'in_progress' ? '◐'
        : (s2 === 'cancelled' || s2 === 'canceled') ? '⊘' : '○';
      html += '<div class="hub-td ' + esc(s2) + '"><span>' + ic + '</span><span>' + esc(t.text || '') + '</span></div>';
    });
    html += '</div>';
  });
  el.innerHTML = html;
}

function hubRenderSessions() {
  const el = $('hubSessions');
  if (!el) return;
  const sessions = (typeof stateCache !== 'undefined' && stateCache.sessions) || [];
  const sig = sessions.map(s => s.name + ':' + (s.busy ? 1 : 0) +
    ':' + (s.turns || 0) + ':' + (s.queued || 0)).join('|');
  if (sig === hubSig.se && el.firstChild) return;
  hubSig.se = sig;
  if (!sessions.length) { el.innerHTML = '<div class="hub-empty">无会话</div>'; return; }
  let html = '<div class="hub-tiles">';
  sessions.forEach(s => {
    html += htile(s.busy ? 'warn' : 'ok', s.name, s.busy ? '生成中' : '空闲',
      (s.turns != null ? s.turns + ' 轮' : '') + (s.queued ? ' · 排队 ' + s.queued : ''));
  });
  html += '</div>';
  el.innerHTML = html;
}

/* ── CC 会话接管面板（Claude Code session takeover）──────────────────────── */
let hubCCCache = null;   // 缓存已拉取的 CC sessions 列表
let hubCCFilter = '';    // 当前项目筛选
let hubCCLastErr = '';   // 最近一次拉取失败原因（空态显示给用户，便于排障）

async function hubFetchCCSessions(project) {
  hubCCLastErr = '';
  try {
    // 用 apiUrl() 拼地址（与长轮询/状态请求完全同一路径）：app 内嵌 WebView 下
    // 页面源是 appassets.phantom，必须显式拼 API_BASE 远程隧道地址，不能靠相对路径。
    let q = '/api/cc-sessions?limit=80&key=' + encodeURIComponent(KEY || '');
    if (project) q += '&project=' + encodeURIComponent(project);
    const url = (typeof apiUrl === 'function') ? apiUrl(q) : q;
    if (!API_BASE && url.indexOf('http') !== 0) {
      hubCCLastErr = 'API 地址未配置（请用 TG /app 重新导入凭据）';
      return [];
    }
    const r = await fetch(url, { signal: AbortSignal.timeout(20000) });
    if (!r.ok) { hubCCLastErr = '请求失败 HTTP ' + r.status; return []; }
    const j = await r.json();
    return j.sessions || [];
  } catch (e) {
    hubCCLastErr = '网络错误: ' + (e && e.name === 'TimeoutError' ? '超时' : (e && e.message || e));
    return [];
  }
}

function hubRenderTakeover(sessions, loading) {
  const el = $('hubTakeover');
  if (!el) return;
  // Diff guard: skip rebuild if data unchanged
  const sig = loading ? '__loading__' : (sessions || []).map(s => s.id + '|' + (s.last_ts || 0)).join(',') + '|' + hubCCFilter;
  if (sig === el._sig) return;
  el._sig = sig;
  let html = '<div class="hub-takeover-bar">';
  html += '<select id="ccProjectFilter">';
  html += '<option value="">全部项目</option>';
  html += '<option value="Workspace"' + (hubCCFilter === 'Workspace' ? ' selected' : '') + '>Workspace</option>';
  html += '<option value="Platform_Phantom"' + (hubCCFilter === 'Platform_Phantom' ? ' selected' : '') + '>Platform_Phantom</option>';
  html += '</select>';
  html += '<button id="ccRefreshBtn" class="metro-btn-sm">刷新</button>';
  html += '</div>';
  if (loading) {
    html += '<div class="hub-empty">加载中…</div>';
    el.innerHTML = html;
    hubBindTakeoverEvts();
    return;
  }
  if (!sessions || !sessions.length) {
    const msg = hubCCLastErr ? ('⚠ ' + hubCCLastErr) : '无可用会话';
    html += '<div class="hub-empty">' + esc(msg) + '</div>';
    el.innerHTML = html;
    hubBindTakeoverEvts();
    return;
  }
  html += '<div class="cc-sessions">';
  try {
    sessions.forEach(s => {
      const age = s.last_ts ? hubTimeAgo(s.last_ts) : '?';
      const title = (s.title || '').length > 50 ? s.title.slice(0, 50) + '…' : (s.title || '(无标题)');
      const sid = String(s.id || '');
      // 最近一条消息概要（方便查找）
      const roleIcon = s.last_role === 'assistant' ? '🤖' : (s.last_role === 'user' ? '👤' : '');
      const lastMsg = (s.last_msg || '').trim();
      const cwd = String(s.cwd || '');
      html += '<div class="cc-item" data-id="' + esc(sid) + '" data-project="' + esc(s.project || '') + '" data-cwd="' + esc(cwd) + '">';
      html += '<div class="cc-title">' + esc(title) + '</div>';
      if (lastMsg) {
        html += '<div class="cc-last">' + esc(roleIcon) + ' ' + esc(lastMsg.slice(0, 80)) + (lastMsg.length > 80 ? '…' : '') + '</div>';
      }
      html += '<div class="cc-meta">' + esc(s.project || '') + ' · ' + esc(age) + ' · ' + esc(sid.slice(0, 8)) + '…' + (cwd ? ' · ' + esc(cwd) : '') + '</div>';
      html += '<button class="cc-resume-btn" data-id="' + esc(sid) + '" data-project="' + esc(s.project || '') + '" data-cwd="' + esc(cwd) + '">接管</button>';
      html += '</div>';
    });
  } catch (e) {
    html += '<div class="hub-empty">⚠ 渲染出错: ' + esc(e && e.message || e) + '</div>';
  }
  html += '</div>';
  el.innerHTML = html;
  hubBindTakeoverEvts();
}

function hubTimeAgo(value) {
  try {
    const sec = Math.max(0, Math.floor((Date.now() - timestampMs(value)) / 1000));
    if (sec < 60) return sec + '秒前';
    if (sec < 3600) return Math.floor(sec / 60) + '分钟前';
    if (sec < 86400) return Math.floor(sec / 3600) + '小时前';
    return Math.floor(sec / 86400) + '天前';
  } catch (e) { return '?'; }
}

function hubBindTakeoverEvts() {
  const sel = document.getElementById('ccProjectFilter');
  if (sel) sel.addEventListener('change', async () => {
    hubCCFilter = sel.value;
    hubRenderTakeover(null, true);
    const sessions = await hubFetchCCSessions(hubCCFilter || null);
    hubCCCache = sessions;
    hubRenderTakeover(sessions);
  });
  const rb = document.getElementById('ccRefreshBtn');
  if (rb) rb.addEventListener('click', async () => {
    rb.textContent = '…';
    const sessions = await hubFetchCCSessions(hubCCFilter || null);
    hubCCCache = sessions;
    hubRenderTakeover(sessions);
  });
  document.querySelectorAll('.cc-resume-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const sid = btn.dataset.id;
      const project = btn.dataset.project;
      const cwd = btn.dataset.cwd || '';
      btn.textContent = '…';
      btn.disabled = true;
      try {
        const rurl = (typeof apiUrl === 'function') ? apiUrl('/api/cc-resume?key=' + encodeURIComponent(KEY || '')) : '/api/cc-resume';
        const r = await fetch(rurl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid, project: project, cwd: cwd })
        });
        const j = await r.json();
        if (j.ok) {
          btn.textContent = '✓ ' + (j.name || '');
          if (typeof toast === 'function') toast('已接管: ' + (j.name || sid.slice(0, 8)));
          if (typeof refreshState === 'function') await refreshState(); // 立刻刷新顶部会话标签
          hubSig.ov = hubSig.se = '';
          hubSig.act = '';                          // 强制「已接管」面板下次重建
          try { hubRenderOverview(); hubRenderSessions(); } catch (e) {}
          try { await hubRefreshActive(); } catch (e) {}   // 立刻刷新已接管列表
        } else {
          btn.textContent = '✗ ' + (j.error || '失败');
          setTimeout(() => { btn.textContent = '接管'; btn.disabled = false; }, 2000);
        }
      } catch (e) {
        btn.textContent = '✗ 网络错误';
        setTimeout(() => { btn.textContent = '接管'; btn.disabled = false; }, 2000);
      }
    });
  });
}

async function hubInitTakeover() {
  const el = $('hubTakeover');
  if (!el) return;
  // 已渲染过就不重复拉（避免每 3s 刷新重置筛选/滚动）；用 DOM 是否已建为准，
  // 不用 hubCCCache（空数组也算"拉过"会永久锁死在空态）。
  if (el.dataset.built === '1') return;
  el.dataset.built = '1';
  // 先渲染工具条 + loading（即便 fetch 慢/失败，项目下拉和刷新键也立刻可用）
  hubRenderTakeover(null, true);
  try {
    hubCCCache = await hubFetchCCSessions(hubCCFilter || null);
  } catch (e) {
    hubCCCache = [];
  }
  hubRenderTakeover(hubCCCache);
}

/* ── 已接管会话管理面板（hubActive）──────────────────────────────────────
 * 列出当前活跃的 cc-* worker，每个带「停止」按钮 + 最近三条对话历史预览。
 * 与接管面板分离：接管=拉起新会话，已接管=管理在跑的会话。 */
let hubActiveBusy = false;
async function hubRefreshActive(force) {
  const el = $('hubActive');
  if (!el) return;
  const now = Date.now();
  if (!force && hubSig.act && el.firstChild && now - _hubActiveLastFetch < hubNetworkMs('active')) return;
  if (hubActiveBusy) return;
  hubActiveBusy = true;
  _hubActiveLastFetch = now;
  try {
    let workers = [];
    try {
      const url = (typeof apiUrl === 'function')
        ? apiUrl('/api/cc-active?key=' + encodeURIComponent(KEY || ''))
        : '/api/cc-active?key=' + encodeURIComponent(KEY || '');
      const r = await fetch(url, { signal: AbortSignal.timeout(20000) });
      if (r.ok) { const j = await r.json(); workers = j.workers || []; }
    } catch (e) { /* 拉取失败显示空态 */ }
    // 结构签名：worker 名 + busy 状态 + 最近消息条数，变化才重建（防 3s 闪烁）
    const sig = workers.map(w => w.name + ':' + (w.busy ? 1 : 0) + ':' +
      ((w.recent && w.recent.length) || 0)).join('|');
    if (sig === hubSig.act && el.firstChild) return;
    hubSig.act = sig;
    if (!workers.length) {
      el.innerHTML = '<div class="hub-empty">暂无已接管会话<br><span style="font-size:11px;opacity:.6">去「接管」面板拉起一个</span></div>';
      return;
    }
    let html = '<div class="cc-actives">';
    workers.forEach(w => {
      const name = String(w.name || '');
      const stat = w.busy ? '🟡 忙' : '🟢 闲';
      html += '<div class="cca-item">';
      html += '<div class="cca-head"><span class="cca-name">' + esc(name) + '</span>'
        + '<span class="cca-stat">' + esc(stat) + ' · ' + esc(String(w.turns || 0)) + '轮</span>'
        + '<button class="cca-stop" data-name="' + esc(name) + '">停止</button></div>';
      html += '<div class="cca-cwd">' + esc(w.cwd || '') + '</div>';
      const recent = w.recent || [];
      if (recent.length) {
        html += '<div class="cca-recent">';
        recent.forEach(m => {
          const icon = m.role === 'assistant' ? '🤖' : '👤';
          const t = (m.text || '').slice(0, 120);
          html += '<div class="cca-msg"><span class="cca-role">' + icon + '</span>'
            + '<span class="cca-text">' + esc(t) + (((m.text || '').length > 120) ? '…' : '') + '</span></div>';
        });
        html += '</div>';
      } else {
        html += '<div class="cca-recent cca-empty">（无对话历史）</div>';
      }
      html += '</div>';
    });
    html += '</div>';
    el.innerHTML = html;
    el.querySelectorAll('.cca-stop').forEach(btn => {
      btn.addEventListener('click', async () => {
        const nm = btn.dataset.name;
        if (!nm) return;
        btn.textContent = '…'; btn.disabled = true;
        try {
          const url = (typeof apiUrl === 'function')
            ? apiUrl('/api/cc-stop?key=' + encodeURIComponent(KEY || ''))
            : '/api/cc-stop?key=' + encodeURIComponent(KEY || '');
          const r = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: nm })
          });
          const j = await r.json();
          if (j.ok) {
            if (typeof toast === 'function') toast('已停止 ' + nm);
            if (typeof refreshState === 'function') await refreshState();
            hubSig.ov = hubSig.se = hubSig.act = '';   // 强制下次重建
            try { hubRenderOverview(); hubRenderSessions(); } catch (e) {}
            await hubRefreshActive();
          } else {
            btn.textContent = '✗'; setTimeout(() => { btn.textContent = '停止'; btn.disabled = false; }, 1500);
          }
        } catch (e) {
          btn.textContent = '✗'; setTimeout(() => { btn.textContent = '停止'; btn.disabled = false; }, 1500);
        }
      });
    });
  } finally {
    hubActiveBusy = false;
  }
}

/* ── 面板导航（Metro Pivot 分页）：导航点 + ← / → 键 + scroll 自动高亮当前面板 ── */
function hubPanelEls() {
  const sc = $('hubScroller');
  return sc ? Array.prototype.slice.call(sc.querySelectorAll('.hub-panel')) : [];
}
function hubCurrentPanel() {
  const sc = $('hubScroller'), ps = hubPanelEls();
  if (!sc || !ps.length) return 0;
  let best = 0, bestD = Infinity;
  ps.forEach((p, i) => {
    const d = Math.abs((p.offsetLeft - 44) - sc.scrollLeft);   // 44 = #hubScroller 左 padding
    if (d < bestD) { bestD = d; best = i; }
  });
  return best;
}
function hubGoPanel(i) {
  const ps = hubPanelEls();
  if (!ps.length) return;
  i = Math.max(0, Math.min(ps.length - 1, i));
  ps[i].scrollIntoView({ behavior: hubLowPower() ? 'auto' : 'smooth', inline: 'start', block: 'nearest' });
  hubSchedulePanelRender();
}
function hubBuildNav() {
  const nav = $('hubNav');
  if (!nav) return;
  const n = hubPanelEls().length;
  let html = '';
  for (let i = 0; i < n; i++) html += '<button data-i="' + i + '"' + (i === 0 ? ' class="on"' : '') + '></button>';
  nav.innerHTML = html;
  nav.querySelectorAll('button').forEach(b => b.addEventListener('click', () => hubGoPanel(+b.dataset.i)));
}
function hubUpdateNav() {
  const nav = $('hubNav');
  if (!nav) return;
  const cur = hubCurrentPanel();
  nav.querySelectorAll('button').forEach((b, i) => b.classList.toggle('on', i === cur));
}


/* ── HUB Scrubber：长按导航点展开为横向滑动条 ── */
var hubScrubberEl = null;
var hubScrubberThumb = null;
var hubScrubberDragging = false;

function hubBuildScrubber() {
  var nav = $('hubNav');
  if (!nav || hubScrubberEl) return;
  hubScrubberEl = document.createElement('div');
  hubScrubberEl.className = 'hub-scrubber';
  hubScrubberEl.innerHTML = '<div class="hub-scrubber-track"><div class="hub-scrubber-thumb"></div></div>';
  var hub = $('hub');
  if (hub) hub.insertBefore(hubScrubberEl, nav);
  hubScrubberThumb = hubScrubberEl.querySelector('.hub-scrubber-thumb');
  hubScrubberEl.addEventListener('mousedown', hubScrubberStart);
  hubScrubberEl.addEventListener('touchstart', hubScrubberStart, { passive: false });
  document.addEventListener('mousemove', hubScrubberMove);
  document.addEventListener('touchmove', hubScrubberMove, { passive: false });
  document.addEventListener('mouseup', hubScrubberEnd);
  document.addEventListener('touchend', hubScrubberEnd);
}

function hubAlignScrubberToDots() {
  var nav = $('hubNav');
  if (!nav || !hubScrubberEl) return;
  var btns = nav.querySelectorAll('button');
  if (!btns.length) return;
  var navRect = nav.getBoundingClientRect();
  var first = btns[0].getBoundingClientRect();
  var last = btns[btns.length - 1].getBoundingClientRect();
  var w = last.right - first.left + 4;
  var left = first.left - navRect.left - 2;
  hubScrubberEl.style.left = left + 'px';
  hubScrubberEl.style.width = w + 'px';
  hubScrubberEl.style.bottom = '8px';
}

function hubUpdateScrubber() {
  if (!hubScrubberThumb || !hubScrubberEl) return;
  var sc = $('hubScroller'), ps = hubPanelEls();
  if (!sc || !ps.length) return;
  var trackW = hubScrubberEl.querySelector('.hub-scrubber-track').offsetWidth;
  if (!trackW) return;
  var total = sc.scrollWidth - sc.clientWidth || 1;
  var ratio = total > 0 ? sc.scrollLeft / total : 0;
  ratio = Math.max(0, Math.min(1, ratio));
  hubScrubberThumb.style.left = (ratio * trackW) + 'px';
}

function hubScrubberStart(e) {
  hubScrubberDragging = true;
  e.preventDefault(); e.stopPropagation();
}

function hubScrubberMove(e) {
  if (!hubScrubberDragging || !hubScrubberEl) return;
  var x = e.touches ? e.touches[0].clientX : e.clientX;
  var track = hubScrubberEl.querySelector('.hub-scrubber-track');
  var rect = track.getBoundingClientRect();
  var ratio = Math.max(0, Math.min(1, (x - rect.left) / (rect.width || 1)));
  var sc = $('hubScroller');
  if (sc) sc.scrollLeft = ratio * (sc.scrollWidth - sc.clientWidth);
  hubUpdateScrubber();
  hubLastScrollAt = Date.now();
}

function hubScrubberEnd() {
  if (!hubScrubberDragging) return;
  hubScrubberDragging = false;
  setTimeout(function() {
    if (!hubScrubberDragging && hubScrubberEl) hubScrubberEl.classList.remove('show');
  }, 500);
}

/* Long-press on nav dots → scrubber expands, thumb snaps to nearest dot */
function hubInitNavLongPress() {
  var nav = $('hubNav');
  if (!nav) return;
  var timer = null;
  function showScrubberAtDot(e) {
    if (!hubScrubberEl) return;
    hubAlignScrubberToDots();
    // Find nearest dot to cursor, snap hub scroll + thumb
    var x = e.touches ? e.touches[0].clientX : e.clientX;
    var btns = nav.querySelectorAll('button');
    var ps = hubPanelEls();
    var sc = $('hubScroller');
    if (btns.length && ps.length && sc) {
      var bestI = 0, bestD = Infinity;
      btns.forEach(function(b, i) {
        var r = b.getBoundingClientRect();
        var d = Math.abs(x - (r.left + r.width / 2));
        if (d < bestD) { bestD = d; bestI = i; }
      });
      bestI = Math.min(bestI, ps.length - 1);
      sc.scrollLeft = (bestI / (ps.length - 1 || 1)) * (sc.scrollWidth - sc.clientWidth);
      hubLastScrollAt = Date.now();
    }
    hubScrubberEl.classList.add('show');
    hubUpdateScrubber();
    // Immediately enter drag — no need to lift finger
    hubScrubberDragging = true;
  }
  nav.addEventListener('mousedown', function(e) { timer = setTimeout(function() { showScrubberAtDot(e); }, 420); });
  nav.addEventListener('mouseup', function() { clearTimeout(timer); });
  nav.addEventListener('mouseleave', function() { clearTimeout(timer); });
  nav.addEventListener('touchstart', function(e) { timer = setTimeout(function() { showScrubberAtDot(e); }, 420); }, { passive: true });
  nav.addEventListener('touchend', function() { clearTimeout(timer); });
  nav.addEventListener('touchcancel', function() { clearTimeout(timer); });
}

/* 视差 + 横滑：背景大标题随横滑慢移；鼠标滚轮纵→横（桌面友好）；← / → 翻页。 */
function hubInit() {
  const btn = $('hubBtn'), close = $('hubClose'), sc = $('hubScroller'), title = $('hubTitle');
  if (btn) btn.addEventListener('click', hubOpen);
  if (close) close.addEventListener('click', hubClose);
  hubBuildNav();
  hubBuildScrubber();
  hubInitNavLongPress();
  hubRenderTheme();   // 静态选色面板：构建一次
  // Metro App Bar 命令键（刷新 / 跳到主题面板 / 关闭）
  const cmdR = $('hubCmdRefresh');
  if (cmdR) cmdR.addEventListener('click', () => {
    hubSig.ov = hubSig.td = hubSig.se = hubSig.act = '';
    hubRender({ force: true });
    if (typeof toast === 'function') toast('已刷新');
  });
  const cmdT = $('hubCmdTheme');
  if (cmdT) cmdT.addEventListener('click', () => {
    const ps = hubPanelEls();
    const i = ps.findIndex(p => p.querySelector('#hubTheme'));
    hubGoPanel(i >= 0 ? i : ps.length - 1);
  });
  const cmdC = $('hubCmdClose');
  if (cmdC) cmdC.addEventListener('click', hubClose);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { hubClose(); return; }
    const h = $('hub');
    if (!h || h.classList.contains('hidden')) return;
    if (e.key === 'ArrowRight') { hubGoPanel(hubCurrentPanel() + 1); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { hubGoPanel(hubCurrentPanel() - 1); e.preventDefault(); }
  });
  if (sc) {
    let raf = 0;
    sc.addEventListener('scroll', () => {
      hubLastScrollAt = Date.now();
      if (!raf) raf = requestAnimationFrame(() => {
        raf = 0;
        if (title && !hubLowPower()) title.style.transform = 'translateX(' + (-sc.scrollLeft * 0.28) + 'px)';
        hubUpdateNav();
        hubUpdateScrubber();
      });
      hubSchedulePanelRender();
    }, { passive: true });
    sc.addEventListener('wheel', e => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        sc.scrollLeft += e.deltaY;
        hubLastScrollAt = Date.now();
        e.preventDefault();
      }
    }, { passive: false });
  }
}
document.addEventListener('DOMContentLoaded', hubInit);

/* ── WebCTL 构建面板（/api/webctl/overview 桥接数据）───────────────────────── */
let _hubBuildCache = null;
let _hubBuildSig = '';
let _hubBuildLastFetch = 0;

async function hubRenderBuild(force) {
  const el = $('hubBuild');
  if (!el) return;
  const now = Date.now();
  if (force || !_hubBuildCache || now - _hubBuildLastFetch >= hubNetworkMs('build')) {
    _hubBuildLastFetch = now;
    try {
      const r = await fetch(apiUrl('/api/webctl/overview') + (KEY ? '?key=' + KEY : ''));
      if (r.ok) _hubBuildCache = await r.json();
      else _hubBuildCache = { ok: false, error: 'status ' + r.status };
    } catch (e) {
      _hubBuildCache = { ok: false, error: String(e.message || e) };
    }
  }
  const d = _hubBuildCache;
  if (!d) return;
  if (!d.ok) {
    const sig = 'err:' + (d.error || '');
    if (sig === _hubBuildSig) return;
    _hubBuildSig = sig;
    el.innerHTML = '<div class="hub-empty">⚠ WebCTL: ' + esc(d.error || '不可达') + '</div>' +
      '<div class="hub-note">确认服务端 WebCTL 已启动（端口 8080）</div>';
    return;
  }
  // 解析数据
  const projs = d.projects || {};
  const progress = d.progress || {};
  const projCount = Object.keys(projs).length;
  const running = Object.values(progress).filter(v => v.status === 'running');
  const runCount = running.length;
  const sig = projCount + '|' + runCount + '|' + running.map(r => r.project + ':' + (r.progress || 0)).join(',');
  if (sig === _hubBuildSig && el.firstChild) return;
  _hubBuildSig = sig;
  let html = '<div class="hub-tiles">';
  html += htile(projCount ? '' : 'dim', '项目', projCount, '已注册');
  html += htile(runCount ? 'warn' : 'dim', '构建中', runCount, runCount ? '进行中' : '空闲');
  html += '</div>';
  // 构建进度列表
  if (runCount) {
    html += '<div class="hub-tdgroup"><div class="hub-tdhead">活跃构建</div>';
    for (const b of running) {
      const pct = Math.round((b.progress || 0) * 100);
      const lbl = (b.project || '?') + (b.variant ? ':' + b.variant : '') + ' — ' + (b.phase || '') + ' ' + pct + '%';
      html += '<div class="hub-td in_progress"><span>◐</span><span>' + esc(lbl) + '</span></div>';
    }
    html += '</div>';
  }
  // 项目列表
  const projNames = Object.keys(projs);
  if (projNames.length) {
    html += '<div class="hub-tdgroup"><div class="hub-tdhead">项目</div>';
    for (const name of projNames.slice(0, 10)) {
      const p = projs[name] || {};
      const info = (p.kernel_version ? 'v' + p.kernel_version + ' ' : '') + (p.defconfig || '');
      html += '<div class="hub-td"><span>⬢</span><span>' + esc(name) + (info ? ' <small style="opacity:.5">' + esc(info) + '</small>' : '') + '</span></div>';
    }
    html += '</div>';
  }
  // 快捷链接（用 span 模拟按钮,避免 <a> 在 TG WebView 中触发外部跳转）
  html += '<div class="hub-note" style="margin-top:12px">';
  html += '<span class="webctl-link" data-path="/" style="color:var(--accent);text-decoration:underline;cursor:pointer;margin-right:12px">📊 面板</span>';
  html += '<span class="webctl-link" data-path="/hub" style="color:var(--accent);text-decoration:underline;cursor:pointer;margin-right:12px">📋 日志</span>';
  html += '<span class="webctl-link" data-path="/configurator" style="color:var(--accent);text-decoration:underline;cursor:pointer">⚙ 配置</span>';
  html += '</div>';
  el.innerHTML = html;
  // 绑定事件（不用 inline onclick,防止 TG WebView 拦截行为）
  el.querySelectorAll('.webctl-link').forEach(s => {
    s.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); hubOpenWebctl(s.dataset.path); });
  });
}

function hubOpenWebctl(path) {
  // 在当前页面内用 iframe overlay 展示 WebCTL（同域 static 反代,免鉴权）
  const url = '/webctl' + path;
  // 创建全屏 overlay
  let ov = document.getElementById('webctl-overlay');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'webctl-overlay';
    ov.style.cssText = 'position:fixed;inset:0;z-index:9999;background:var(--bg,#131318);display:flex;flex-direction:column';
    // 顶栏: 返回按钮
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;align-items:center;padding:8px 12px;background:var(--card-bg,#1e1e24);border-bottom:1px solid var(--border,#333)';
    bar.innerHTML = '<button id="webctl-back" style="background:none;border:none;color:var(--accent,#4fc3f7);font-size:15px;cursor:pointer;padding:4px 8px">← 返回</button>'
      + '<span style="flex:1;text-align:center;color:var(--fg,#dcdcdc);font-size:13px;opacity:.7">WebCTL</span>';
    ov.appendChild(bar);
    // iframe
    const iframe = document.createElement('iframe');
    iframe.id = 'webctl-frame';
    iframe.style.cssText = 'flex:1;border:none;width:100%;background:var(--bg,#131318)';
    ov.appendChild(iframe);
    document.body.appendChild(ov);
    document.getElementById('webctl-back').onclick = () => { ov.style.display = 'none'; };
  }
  ov.style.display = 'flex';
  document.getElementById('webctl-frame').src = url;
}

/* 启动即套用上次选的 Metro 强调色（早于交互，避免闪默认蓝）*/
try { hubApplyAccent(hubAccentIdx(), false); } catch (e) {}
