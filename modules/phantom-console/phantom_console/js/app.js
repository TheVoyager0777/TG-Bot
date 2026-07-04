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
let stateLoaded = false;
// API_BASE 由 index.html 注入(<script>var API_BASE=</script>)或 ?api= 参数覆盖
// 不在此声明 let，避免与注入的 var 冲突导致 SyntaxError
let stateCache = { sessions: [] };
// 已渲染事件的 seq 去重：防止 pickSession 的历史回放与正在跑的长轮询重叠 → 双渲染。
// resetAll/切换时清空；超量时整清（since 单调前进，老 seq 不会再被请求）。
let seenSeq = new Set();

function eventDedupeKey(ev) {
  const sq = ev && ev.seq;
  if (sq == null) return null;
  if (Number(sq) < 0) {
    return [
      ev.source || 'synthetic',
      ev.session || 'main',
      ev.session_id || '',
      sq,
      ev.type || '',
    ].join('/');
  }
  return String(sq);
}

function apiUrl(path) {
  if (API_BASE) return normalizeBase(API_BASE) + path;
  return path;
}

function normalizeBase(url) {
  if (!url) return '';
  try { return new URL(String(url), location.href).origin; }
  catch (e) { return String(url).replace(/\/+$/, ''); }
}

function ensureSelectedSessionExists() {
  if (!session) return;
  const names = ((stateCache && stateCache.sessions) || []).map(s => s.name);
  if (!names.includes(session)) {
    session = null;
    try { localStorage.setItem('phantom_session', '__all__'); } catch (e) {}
    if (typeof updateComposerPlaceholder === 'function') updateComposerPlaceholder();
  }
}

function applyStateSnapshot(data, extraSessionNames) {
  stateCache = data || { sessions: [] };
  stateCache.sessions = Array.isArray(stateCache.sessions) ? stateCache.sessions.slice() : [];
  const seen = new Set(stateCache.sessions.map(s => s && s.name).filter(Boolean));
  (extraSessionNames || []).forEach(name => {
    const n = String(name || '').trim();
    if (!n || seen.has(n)) return;
    seen.add(n);
    stateCache.sessions.push({ name: n, busy: false, turns: 0, provider: 'history', historyOnly: true });
  });
  stateLoaded = true;
  ensureSelectedSessionExists();
  activeAsks = new Set((stateCache.active_asks || []).map(a => a.token));
  renderSessionTabs(stateCache.sessions || []);
}

function currentHttpOrigin() {
  return /^https?:$/.test(location.protocol) ? location.origin : '';
}

function hostForUrl(host) {
  return host && host.includes(':') && !host.startsWith('[') ? '[' + host + ']' : host;
}

function isDirectHost(host) {
  const h = String(host || '').replace(/^\[|\]$/g, '').toLowerCase();
  if (!h) return false;
  if (h === 'localhost' || h.endsWith('.local') || !h.includes('.')) return true;
  const parts = h.split('.').map(n => Number(n));
  if (parts.length !== 4 || parts.some(n => !Number.isInteger(n) || n < 0 || n > 255)) return false;
  const [a, b] = parts;
  return a === 10 || a === 127 || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127);
}

function sameHostApiBase() {
  const port = Number(typeof CONSOLE_API_PORT !== 'undefined' ? CONSOLE_API_PORT : 0);
  if (!port || !location.hostname || !isDirectHost(location.hostname)) return '';
  return location.protocol + '//' + hostForUrl(location.hostname) + ':' + port;
}

function discoveryBases() {
  const bases = [];
  const add = (u) => {
    const v = normalizeBase(u || '');
    if (v && !bases.includes(v)) bases.push(v);
  };
  add(currentHttpOrigin());
  add(sameHostApiBase());
  add(API_BASE);
  add(DISCO_BASE);
  add(typeof DISCO_BASE_HINT !== 'undefined' ? DISCO_BASE_HINT : '');
  return bases;
}

/* 事件渲染入口（带 seq 去重）。两条来源（历史回放 + 长轮询）共用，保证不重复。 */
function renderEvent(ev) {
  const key = eventDedupeKey(ev);
  if (key != null) {
    if (seenSeq.has(key)) return;
    seenSeq.add(key);
    if (seenSeq.size > 10000) seenSeq = new Set();
  }
  // 单事件渲染异常绝不能中断整批：否则 since 不前进 → 长轮询反复重放 backlog
  // （历史「消息全被重放」的根因正是某个事件 handle 抛错冒泡出 forEach）。
  try { handle(ev); } catch (err) { console.error('handle failed:', ev && ev.type, err); }
}

function sessionNamesForHistory(snapshot) {
  const names = [];
  const add = (name) => {
    const n = String(name || '').trim();
    if (n && !names.includes(n)) names.push(n);
  };
  ((snapshot || stateCache || {}).sessions || []).forEach(s => add(s && s.name));
  if (!names.includes('main')) names.unshift('main');
  return names.slice(0, 12);
}

async function fetchEventsHistory(name, limit) {
  const q = '/api/events?since=0&limit=' + encodeURIComponent(String(limit || 500)) +
    '&wait=0&key=' + encodeURIComponent(KEY || '') +
    (name ? '&session=' + encodeURIComponent(name) : '');
  const r = await fetch(apiUrl(q), {
    signal: AbortSignal.timeout(10000),
    cache: 'no-store',
  });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

function applyHistoryResponse(j) {
  if (!j) return;
  const events = (j.events || []).slice().sort((a, b) => {
    const as = Number(a.seq || 0);
    const bs = Number(b.seq || 0);
    if (as !== bs) return as - bs;
    return Number(a.ts || 0) - Number(b.ts || 0);
  });
  events.forEach(ev => {
    if (session && ev.session !== session) return;
    renderEvent(ev);
  });
  if ((j.seq || 0) > since) since = j.seq;
  if (j.state) applyStateSnapshot(j.state, j.sessions || []);
}

async function loadHistoryReplay() {
  let first = null;
  try {
    first = await fetchEventsHistory(session, session ? 700 : 700);
  } catch (e) {
    await resolveApiBase(true);
    try { first = await fetchEventsHistory(session, session ? 700 : 700); }
    catch (err) { return null; }
  }

  replaying = true;
  try {
    applyHistoryResponse(first);
    if (!session) {
      const names = sessionNamesForHistory(first && first.state);
      const already = new Set((first.events || []).map(ev => ev.session).filter(Boolean));
      const targets = names.filter(name => !already.has(name) || name === 'main');
      const batches = await Promise.allSettled(targets.map(name => fetchEventsHistory(name, 260)));
      batches.forEach(res => {
        if (res.status === 'fulfilled') applyHistoryResponse(res.value);
      });
    }
  } finally {
    replaying = false;
  }
  return first;
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

  // 服务端按 session 过滤后再截断；全部视图额外按 session 分片补历史，避免高频会话挤掉其它会话。
  await loadHistoryReplay();
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
  const endpoints = discoveryBases();
  if (!endpoints.length) return;
  // 并行 race 所有端点，最先 200 的胜出；不再串行等每个超时
  const results = await Promise.allSettled(endpoints.map(base =>
    fetch(base + '/api/resolve?key=' + encodeURIComponent(KEY || ''), {
      signal: AbortSignal.timeout(timeout),
      cache: 'no-store',
    })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json().then(j => ({ base, j })); })
  ));
  for (const res of results) {
    if (res.status !== 'fulfilled') continue;
    const { base, j } = res.value;
    _resolveFails = 0;
    if (j.api && j.api !== API_BASE) {
      console.log('[resolve] API base migrated:', API_BASE, '→', j.api);
      API_BASE = normalizeBase(j.api);
      try { localStorage.setItem('phantom_api', API_BASE); } catch (e) {}
      $('statusText').textContent = '通道已更新';
    }
    if (j.static && j.static !== DISCO_BASE) {
      DISCO_BASE = normalizeBase(j.static);
      try { localStorage.setItem('phantom_disco', DISCO_BASE); } catch (e) {}
    }
    if (!API_BASE) {
      API_BASE = base;
      try { localStorage.setItem('phantom_api', API_BASE); } catch (e) {}
    }
    return;
  }
  _resolveFails++;
}

/* 快速探针：验证当前 API_BASE 存活（3s），失败立即触发 resolve。
   成功时顺便拿 state 数据(省一个 RTT)。 */
async function probeApi() {
  // initData 模式也需要探针——不再 gate 在 KEY 上
  if (!KEY && !(tg && tg.initData)) return null;
  if (!API_BASE) await resolveApiBase(true);
  if (!API_BASE) return null;
  try {
    const r = await fetch(API_BASE + '/api/state?key=' + encodeURIComponent(KEY || ''), {
      signal: AbortSignal.timeout(3000),
      cache: 'no-store',
    });
    if (r.ok) {
      const data = await r.json();
      // 提前把 state 数据应用,省去后续 refreshState 的 RTT
      applyStateSnapshot(data);
      $('dot').classList.add('on');
      if ($('statusText').textContent !== '已连接') $('statusText').textContent = '已连接';
      _resolveFails = 0;
      return data;
    }
  } catch (e) { /* 超时或网络错误 */ }
  // 当前 API 不可达，立即 resolve
  console.log('[boot] API_BASE stale, resolving...');
  await resolveApiBase(true);
  if (API_BASE) {
    try {
      const r = await fetch(API_BASE + '/api/state?key=' + encodeURIComponent(KEY || ''), {
        signal: AbortSignal.timeout(3000),
        cache: 'no-store',
      });
      if (r.ok) {
        const data = await r.json();
        applyStateSnapshot(data);
        $('dot').classList.add('on');
        if ($('statusText').textContent !== '已连接') $('statusText').textContent = '已连接';
        _resolveFails = 0;
        return data;
      }
    } catch (e) {}
  }
  return null;
}

/* ── Fluent: typing indicator — only in active turn, controlled by turn lifecycle ── */

let activeTypingIndicator = null;

function typingHostFor(textEl) {
  if (!textEl) return null;
  const candidates = Array.from(textEl.querySelectorAll('p, li, blockquote, h1, h2, h3, h4'))
    .filter(el => (el.textContent || '').trim());
  return candidates.length ? candidates[candidates.length - 1] : textEl;
}

function directTextBlocksFor(s) {
  if (!s || !s.bodyEl) return [];
  return Array.from(s.bodyEl.children).filter(el => el.classList.contains('txt'));
}

function latestTextBlockFor(s) {
  const blocks = directTextBlocksFor(s);
  return blocks.length ? blocks[blocks.length - 1] : null;
}

function normalizeTypingIndicators(s, keep) {
  if (!s || !s.bodyEl) return;
  s.bodyEl.querySelectorAll('.typing-indicator').forEach(el => {
    if (!keep || el !== keep) el.remove();
  });
  s.bodyEl.querySelectorAll('.txt.has-typing-indicator').forEach(el => {
    if (!keep || !el.contains(keep)) el.classList.remove('has-typing-indicator');
  });
}

/* Show typing dots at the tail of the latest streamed assistant text block. */
function showTypingIndicator(s, textEl) {
  const target = textEl || (s && s.lastText);
  if (!s || !s.bodyEl || !target) return;
  const latest = latestTextBlockFor(s);
  if (latest !== target) {
    removeTypingIndicator(s);
    return;
  }
  removeTypingIndicator(s);
  const host = typingHostFor(target);
  if (!host) return;
  const el = document.createElement('span');
  el.className = 'typing-indicator';
  el.setAttribute('aria-label', 'AI 正在输入');
  el.innerHTML = '<span></span><span></span><span></span>';
  host.appendChild(el);
  target.classList.add('has-typing-indicator');
  normalizeTypingIndicators(s, el);
  activeTypingIndicator = el;
  if (nearBottom()) scrollBottom();
}

/* Remove typing indicator from a specific turn */
function removeTypingIndicator(s) {
  if (s && s.bodyEl) {
    s.bodyEl.querySelectorAll('.typing-indicator').forEach(el => el.remove());
    s.bodyEl.querySelectorAll('.txt.has-typing-indicator')
      .forEach(el => el.classList.remove('has-typing-indicator'));
  }
  if (activeTypingIndicator && !document.body.contains(activeTypingIndicator)) activeTypingIndicator = null;
}

/* Remove ALL orphaned typing indicators (called on session switch/feed clear) */
function removeAllTypingIndicators() {
  document.querySelectorAll('.typing-indicator').forEach(el => el.remove());
  document.querySelectorAll('.txt.has-typing-indicator')
    .forEach(el => el.classList.remove('has-typing-indicator'));
  activeTypingIndicator = null;
}

/* ── 状态轮询 ── */
async function refreshState() {
  try {
    const r = await fetch(apiUrl('/api/state?key=' + KEY));
    applyStateSnapshot(await r.json());
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

      (j.events || []).forEach(ev => {
        if (session && ev.session !== session) return;
        renderEvent(ev);
      });
      if (j.seq > since) since = j.seq;  // 处理完再前进游标

      // 利用 events 响应附带的 state 快照直接更新 UI，省去独立的 refreshState 请求
      if (j.state) {
        applyStateSnapshot(j.state, j.sessions || []);
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
  if (qApi) API_BASE = normalizeBase(qApi);
  // 同理持久化 / 回落 API 基址（独立启动壳由静态隧道注入空串 → 用上次记住的 api 域）
  if (API_BASE) { try { localStorage.setItem('phantom_api', API_BASE); } catch (e) {} }
  else { try { API_BASE = localStorage.getItem('phantom_api') || ''; } catch (e) {} }
  API_BASE = normalizeBase(API_BASE);

  // disco: 备用发现端点
  const qDisco = params.get('disco');
  if (qDisco) { DISCO_BASE = normalizeBase(qDisco); try { localStorage.setItem('phantom_disco', DISCO_BASE); } catch (e) {} }
  else { try { DISCO_BASE = localStorage.getItem('phantom_disco') || ''; } catch (e) {} }
  DISCO_BASE = normalizeBase(DISCO_BASE);
  if (!DISCO_BASE && typeof DISCO_BASE_HINT !== 'undefined') DISCO_BASE = normalizeBase(DISCO_BASE_HINT);

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
  if (sameHostApiBase() && API_BASE !== sameHostApiBase()) await resolveApiBase(true);

  // 历史回放 + loadControl 并行发；首屏关键路径只等历史请求。
  const histReq = loadHistoryReplay();
  if (!stateLoaded) refreshState();   // probeApi 已成功则跳过
  loadControl('main');
  if (window.innerWidth < 700) $('side').classList.add('hidden');

  const j = await histReq;
  if (j && j.state) {
    const queues = {};
    stateCache.sessions.forEach(s => { if (s.queued) queues[s.name] = s.queue; });
    renderQueue(queues);
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
      await loadHistoryReplay();
      scrollBottom();
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
