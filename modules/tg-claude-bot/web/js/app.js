/* app.js — 主循环、状态管理、初始化 */

let since = 0;
let session = null;  // null=显示全部 session
let autoScroll = true;
let epoch = 0;
let abortCtl = null;
let KEY = '';
let tg = null;       // Telegram WebApp SDK 实例（boot 时赋值）
let DISCO_BASE = '';  // 备用发现端点（static 隧道），API 不可达时 fallback
let booted = false;  // 首屏历史回放完成前不自动弹 AskQuestion 模态（避免回放旧问题打扰）
let activeAsks = new Set();  // 后端仍在等待回答的 askq token（来自 /api/state）
let replaying = false;       // 正在回放历史(首屏/切会话) → 期间绝不自动弹 AskQuestion 模态
// API_BASE 由 index.html 注入(<script>var API_BASE=</script>)或 ?api= 参数覆盖
// 不在此声明 let，避免与注入的 var 冲突导致 SyntaxError
let stateCache = { sessions: [] };
const busSessions = new Set();
// 已渲染事件的 seq 去重：防止 pickSession 的历史回放与正在跑的长轮询重叠 → 双渲染。
// resetAll/切换时清空；超量时整清（since 单调前进，老 seq 不会再被请求）。
let seenSeq = new Set();

function apiUrl(path) {
  if (API_BASE) return API_BASE + path;
  return path;
}

/* 事件渲染入口（带 seq 去重）。两条来源（历史回放 + 长轮询）共用，保证不重复。 */
function renderEvent(ev) {
  const sq = ev.seq;
  if (sq != null) {
    if (seenSeq.has(sq)) return;
    seenSeq.add(sq);
    if (seenSeq.size > 10000) seenSeq = new Set();
  }
  // 单事件渲染异常绝不能中断整批：否则 since 不前进 → 长轮询反复重放 backlog
  // （历史「消息全被重放」的根因正是某个事件 handle 抛错冒泡出 forEach）。
  try { handle(ev); } catch (err) { console.error('handle failed:', ev && ev.type, err); }
}

/* ── Session 切换：中断在途长轮询 + 拉该 session 完整历史 + 对齐 since ── */
let _sessionSwitching = false;
async function pickSession(name) {
  if (session === name) return;
  if (_sessionSwitching) return;         // prevent concurrent switches
  _sessionSwitching = true;
  try {
    localStorage.setItem(feedScrollKey(session), String($('feed').scrollTop));
  } catch (e) {}
  session = name;
  try {
    localStorage.setItem('phantom_session', name === null ? '__all__' : name);
  } catch (e) {}
  renderSessionTabs(stateCache.sessions);
  updateStopBtn();
  loadControl(name || 'main');

  // 作废在途长轮询那一轮（防其返回后按旧 session/旧 since 重渲）
  epoch++;
  if (abortCtl) { try { abortCtl.abort(); } catch (e) {} }

  resetAll();
  seenSeq.clear();

  // 服务端按 session 过滤后再截断 → 拿到该 session 完整近期历史（不被高频 session 挤出窗口）
  try {
    const q = '/api/events?since=0&limit=500&wait=0&key=' + KEY +
      (name ? '&session=' + encodeURIComponent(name) : '');
    const r = await fetch(apiUrl(q));
    const j = await r.json();
    replaying = true;
    (j.events || []).forEach(ev => {
      if (session && ev.session !== session) return;
      renderEvent(ev);
    });
    replaying = false;
    // 对齐长轮询游标到当前全局 seq：loop 只取此后的新事件，与历史回放无缝衔接、不重复
    if ((j.seq || 0) > since) since = j.seq;
  } catch (e) { replaying = false; }
  let restored = false;
  try {
    const v = localStorage.getItem(feedScrollKey(name));
    if (v != null) {
      $('feed').scrollTop = parseInt(v, 10) || 0;
      restored = true;
    }
  } catch (e) {}
  if (!restored) scrollBottom();
  if (typeof loadDraft === 'function') loadDraft();
  if (typeof updateComposerPlaceholder === 'function') updateComposerPlaceholder();
  if (typeof updatePendingBar === 'function') updatePendingBar();
  _sessionSwitching = false;
}

/* ── 隧道域名轮换自愈 ── */
let _resolveFails = 0;
async function resolveApiBase(fast) {
  // initData 模式(KEY='')也需要域名发现——不再 gate 在 KEY 上
  if (!KEY && !(tg && tg.initData)) return;
  const timeout = fast ? 3000 : 6000;
  const endpoints = [API_BASE, DISCO_BASE].filter(u => u && u !== '');
  if (!endpoints.length) return;
  // 并行 race 所有端点，最先 200 的胜出；不再串行等每个超时
  const results = await Promise.allSettled(endpoints.map(base =>
    fetch(base + '/api/resolve?key=' + KEY, { signal: AbortSignal.timeout(timeout) })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json().then(j => ({ base, j })); })
  ));
  for (const res of results) {
    if (res.status !== 'fulfilled') continue;
    const { base, j } = res.value;
    _resolveFails = 0;
    if (j.api && j.api !== API_BASE) {
      console.log('[resolve] API base migrated:', API_BASE, '→', j.api);
      API_BASE = j.api;
      try { localStorage.setItem('phantom_api', API_BASE); } catch (e) {}
      $('statusText').textContent = '通道已更新';
    }
    if (j.static && j.static !== DISCO_BASE) {
      DISCO_BASE = j.static;
      try { localStorage.setItem('phantom_disco', DISCO_BASE); } catch (e) {}
    }
    return;
  }
  _resolveFails++;
}

/* 快速探针：验证当前 API_BASE 存活（3s），失败立即触发 resolve。
   成功时顺便拿 state 数据(省一个 RTT)。 */
async function probeApi() {
  if (!API_BASE) return null;
  // initData 模式也需要探针——不再 gate 在 KEY 上
  if (!KEY && !(tg && tg.initData)) return null;
  try {
    const r = await fetch(API_BASE + '/api/state?key=' + KEY, { signal: AbortSignal.timeout(3000) });
    if (r.ok) {
      const data = await r.json();
      // 提前把 state 数据应用,省去后续 refreshState 的 RTT
      stateCache = data;
      activeAsks = new Set((stateCache.active_asks || []).map(a => a.token));
      renderSessionTabs(stateCache.sessions || []);
      return data;
    }
  } catch (e) { /* 超时或网络错误 */ }
  // 当前 API 不可达，立即 resolve
  console.log('[boot] API_BASE stale, resolving...');
  await resolveApiBase(true);
  return null;
}

/* ── Fluent: typing indicator — only in active turn, controlled by turn lifecycle ── */

/* Show typing dots at the tail of the active turn body */
function showTypingIndicator(s) {
  if (!s || !s.bodyEl) return;
  let el = s.bodyEl.querySelector('.typing-indicator');
  if (!el) {
    el = document.createElement('div'); el.className = 'typing-indicator';
    el.innerHTML = '<span></span><span></span><span></span>';
  }
  s.bodyEl.appendChild(el); // reposition to end
  if (nearBottom()) scrollBottom();
}

/* Remove typing indicator from a specific turn */
function removeTypingIndicator(s) {
  if (s && s.bodyEl) {
    const el = s.bodyEl.querySelector('.typing-indicator');
    if (el) el.remove();
  }
}

/* Remove ALL orphaned typing indicators (called on session switch/feed clear) */
function removeAllTypingIndicators() {
  document.querySelectorAll('.typing-indicator').forEach(el => el.remove());
}

/* ── 状态轮询 ── */
async function refreshState() {
  try {
    const r = await fetch(apiUrl('/api/state?key=' + KEY));
    stateCache = await r.json();
    activeAsks = new Set((stateCache.active_asks || []).map(a => a.token));
    busSessions.forEach(n => {
      if (!stateCache.sessions.find(s => s.name === n)) {
        stateCache.sessions.push({ name: n, busy: false, turns: 0, queued: 0, queue: [] });
      }
    });
    renderSessionTabs(stateCache.sessions);
    updateStopBtn();
    const queues = {};
    stateCache.sessions.forEach(s => { if (s.queued) queues[s.name] = s.queue; });
    renderQueue(queues);
    if (typeof updatePendingBar === 'function') updatePendingBar();
  } catch (e) {}
}

/* ── 长轮询主循环 ── */
async function loop() {
  while (true) {
    try {
      const ec = ++epoch;
      const ac = new AbortController();
      abortCtl = ac;

      const url = apiUrl('/api/events?since=' + since + '&wait=1&key=' + KEY +
        (session ? '&session=' + encodeURIComponent(session) : ''));
      const r = await fetch(url, { signal: ac.signal });
      if (r.status === 403) {
        $('dot').classList.remove('on');
        if ($('statusText').textContent !== '鉴权失败') $('statusText').textContent = '鉴权失败';
        await new Promise(r => setTimeout(r, 5000));
        continue;
      }
      const j = await r.json();
      if (ec !== epoch) continue; // 本轮被新切换取消

      $('dot').classList.add('on');
      // 状态栏只表达「连接情况」，不再显示会话名（会话在输入框上方的标签里反映）。
      if ($('statusText').textContent !== '已连接') $('statusText').textContent = '已连接';
      _resolveFails = 0;   // 连接恢复，重置退避计数

      (j.sessions || []).forEach(n => busSessions.add(n));
      (j.events || []).forEach(ev => {
        if (session && ev.session !== session) return;
        renderEvent(ev);
      });
      if (j.seq > since) since = j.seq;  // 处理完再前进游标

      // 利用 events 响应附带的 state 快照直接更新 UI，省去独立的 refreshState 请求
      if (j.state) {
        stateCache = j.state;
        activeAsks = new Set((stateCache.active_asks || []).map(a => a.token));
        busSessions.forEach(n => {
          if (!stateCache.sessions.find(s => s.name === n)) {
            stateCache.sessions.push({ name: n, busy: false, turns: 0, queued: 0, queue: [] });
          }
        });
        renderSessionTabs(stateCache.sessions);
        const queues = {};
        stateCache.sessions.forEach(s => { if (s.queued) queues[s.name] = s.queue; });
        renderQueue(queues);
        if (typeof updatePendingBar === 'function') updatePendingBar();
        // Sync bg tasks from state
        if (stateCache.bg_tasks) {
          stateCache.bg_tasks.forEach(t => { if (typeof updateBgTask === 'function') updateBgTask(t); });
          if (typeof renderBgTasks === 'function') renderBgTasks();
        }
      }

      if (nearBottom()) scrollBottom();
      updateStopBtn();
    } catch (e) {
      if (e.name === 'AbortError') continue;
      // 网络临时断开：渐进退避重试，不复位 since（断线期间事件仍在环形缓冲，重连补齐）
      $('dot').classList.remove('on');
      if ($('statusText').textContent !== '重连中…') $('statusText').textContent = '重连中…';
      _resolveFails++;
      if (_resolveFails >= 2) await resolveApiBase(true);
      // 首次断连快速重试(500ms)，连续失败渐进退避(最大8s)
      const backoff = Math.min(500 * Math.pow(2, _resolveFails - 1), 8000);
      await new Promise(r => setTimeout(r, backoff));
    }
  }
}

/* ── 启动 ── */
async function boot() {
  const params = new URLSearchParams(location.search);
  // 凭据解析：URL 参数优先并持久化；无参时回落 localStorage——这样【从主屏独立启动】
  // （PWA start_url 无 ?key/?api）也能凭上次配置直连，不必每次重新拼 URL。
  KEY = params.get('key') || '';
  if (KEY) { try { localStorage.setItem('phantom_key', KEY); } catch (e) {} }
  else { try { KEY = localStorage.getItem('phantom_key') || ''; } catch (e) {} }

  try {
    if (window.Telegram?.WebApp) {
      tg = window.Telegram.WebApp;
      tg.ready(); tg.expand();
      if (tg.initData) KEY = '';
      // 跟随 TG 明/暗主题：把 colorScheme 落到 body[data-tg-scheme]，CSS 据此微调中性色。
      try {
        const applyScheme = () => {
          document.body.dataset.tgScheme = tg.colorScheme || '';
          // Only re-resolve when user set auto — TG theme then drives the resolved scheme
          if (document.body.classList.contains('scheme-auto')) {
            applySettings();
          }
        };
        applyScheme();
        if (tg.onEvent) tg.onEvent('themeChanged', applyScheme);
      } catch (e) {}
    }
  } catch (e) {}

  if (typeof API_BASE === 'undefined') API_BASE = '';
  const qApi = params.get('api');
  if (qApi) API_BASE = qApi;
  // 同理持久化 / 回落 API 基址（独立启动壳由静态隧道注入空串 → 用上次记住的 api 域）
  if (API_BASE) { try { localStorage.setItem('phantom_api', API_BASE); } catch (e) {} }
  else { try { API_BASE = localStorage.getItem('phantom_api') || ''; } catch (e) {} }

  // disco: 备用发现端点
  const qDisco = params.get('disco');
  if (qDisco) { DISCO_BASE = qDisco; try { localStorage.setItem('phantom_disco', qDisco); } catch (e) {} }
  else { try { DISCO_BASE = localStorage.getItem('phantom_disco') || ''; } catch (e) {} }

  try {
    const saved = localStorage.getItem('phantom_session');
    if (saved === '__all__') session = null;
    else if (saved) session = saved;
  } catch (e) {}

  // 全局 fetch patch
  const origFetch = window.fetch;
  window.fetch = function(url, opts = {}) {
    opts.headers = opts.headers || {};
    if (typeof url === 'string' && url.startsWith('/api/') && API_BASE) {
      url = API_BASE + url;
    }
    if (KEY && !opts.headers['X-Console-Key']) {
      opts.headers['X-Console-Key'] = KEY;
    }
    // Telegram WebApp 模式：注入 initData 供后端 HMAC 校验鉴权
    if (tg && tg.initData && !opts.headers['X-Init-Data']) {
      opts.headers['X-Init-Data'] = tg.initData;
    }
    return origFetch.call(this, url, opts);
  };

  // 连接提速：先探针验证 API_BASE 是否存活，失败立即 resolve 到新域名（≤3s 超时）
  // probeApi 成功时顺便初始化 stateCache（省一个独立请求）
  await probeApi();

  // 历史回放 + loadControl 并行发；首屏关键路径只等历史请求。
  const histReq = fetch(apiUrl('/api/events?since=0&limit=400&wait=0&key=' + KEY +
    (session ? '&session=' + encodeURIComponent(session) : '')),
    { signal: AbortSignal.timeout(10000) })
    .then(r => r.json()).catch(() => null);
  if (!stateCache) refreshState();   // probeApi 已成功则跳过
  loadControl('main');
  if (window.innerWidth < 700) $('side').classList.add('hidden');

  const j = await histReq;
  if (j) {
    replaying = true;
    (j.events || []).forEach(ev => renderEvent(ev));
    replaying = false;
    since = j.seq || 0;
    // events 响应附带 state：确保 session tabs/queue 最新
    if (j.state) {
      stateCache = j.state;
      activeAsks = new Set((stateCache.active_asks || []).map(a => a.token));
      renderSessionTabs(stateCache.sessions || []);
      const queues = {};
      stateCache.sessions.forEach(s => { if (s.queued) queues[s.name] = s.queue; });
      renderQueue(queues);
    }
  }
  booted = true;
  showPendingAsk();                    // 历史里仍未结案的提问，回放完后补弹一次
  if (typeof loadDraft === 'function') loadDraft();
  if (typeof updateComposerPlaceholder === 'function') updateComposerPlaceholder();
  if (typeof updatePendingBar === 'function') updatePendingBar();
  let restoredScroll = false;
  try {
    const v = localStorage.getItem(feedScrollKey(session));
    if (v != null) {
      $('feed').scrollTop = parseInt(v, 10) || 0;
      restoredScroll = true;
    }
  } catch (e) {}
  if (!restoredScroll) scrollBottom();
  setInterval(refreshState, 30000);    // 兜底刷新（正常靠长轮询响应附带 state）
  setInterval(resolveApiBase, 60000);  // 隧道域名轮换自愈
  setTimeout(hideSplash, 600);         // 引导完成 → 淡出 Zune 启动屏（留够亮相时间）

  // 后台恢复感知：Mini App 切后台后 JS 被冻结，长轮询 TCP 可能断开。
  // 恢复可见时立即中止在途 fetch 让 loop 快速重连，并刷新状态/隧道域名。
  // 若 feed DOM 在后台被回收（WebView 内存压力），重新拉历史回放。
  // 防抖: TG WebView 中 visibilitychange / pageshow / activated 可能连环触发，
  // 2s 内多次调用只执行最后一次，避免重复 abort + 重拉导致界面闪烁。
  let _resumeTimer = null;
  const onResume = async () => {
    if (_resumeTimer) { clearTimeout(_resumeTimer); _resumeTimer = null; }
    if (abortCtl) { try { abortCtl.abort(); } catch (e) {} }
    await resolveApiBase(true);
    refreshState();
    const feed = $('feed');
    // 仅在 feed 为空时全量重拉（WebView 内存回收场景）
    // 非空时 loop 的长轮询会自动补齐 since 之后的缺口，无需额外拉取
    if (!feed.children.length) {
      seenSeq.clear();
      try {
        const q = '/api/events?since=0&limit=500&wait=0&key=' + KEY +
          (session ? '&session=' + encodeURIComponent(session) : '');
        const r = await fetch(apiUrl(q));
        const j = await r.json();
        replaying = true;
        (j.events || []).forEach(ev => {
          if (session && ev.session !== session) return;
          renderEvent(ev);
        });
        replaying = false;
        if ((j.seq || 0) > since) since = j.seq;
        if (j.state) { stateCache = j.state; }
        scrollBottom();
      } catch (e) { replaying = false; }
    }
  };
  const _debouncedResume = () => {
    if (_resumeTimer) clearTimeout(_resumeTimer);
    _resumeTimer = setTimeout(onResume, 600);
  };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _debouncedResume();
  });
  // iOS WebView 或部分 Android 不可靠触发 visibilitychange，pageshow 兜底
  window.addEventListener('pageshow', (e) => { if (e.persisted) _debouncedResume(); });
  // Telegram Mini App 生命周期：activated（6.9+）在最小化恢复时触发
  if (tg && tg.onEvent) {
    try { tg.onEvent('activated', _debouncedResume); } catch (e) {}
  }

  loop();
}

/* Zune 启动屏摘除（幂等）：boot 完成调用；另设 3s 兜底防 boot 卡死把人锁在 splash。*/
function hideSplash() {
  const sp = document.getElementById('splash');
  if (!sp || sp.classList.contains('gone')) return;
  sp.classList.add('gone');
  setTimeout(() => { if (sp.parentNode) sp.parentNode.removeChild(sp); }, 600);
}
setTimeout(hideSplash, 3000);

document.addEventListener('DOMContentLoaded', boot);

/* PWA：注册 Service Worker（离线开壳 + 独立 app）。仅 https / localhost 生效；
 * 失败静默（CDN/隧道环境多变，SW 不可用不应阻塞主功能）。*/
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}
