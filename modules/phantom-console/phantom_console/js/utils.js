/* utils.js — 共享工具函数 */

function $(id) { return document.getElementById(id); }
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* ── 状态符号（与 TG 统一）── */
const ST_ICON = { running: '◐', done: '✓', interrupted: '⊘', error: '✗' };
const ST_LABEL = { running: '运行中', done: '完成', interrupted: '中断', error: '出错' };

function stText(key, extra) {
  const icon = ST_ICON[key] || '';
  const label = ST_LABEL[key] || key;
  return extra ? icon + ' ' + label + ' · ' + extra : icon + ' ' + label;
}

function timestampMs(value, fallbackMs) {
  const fallback = fallbackMs == null ? Date.now() : fallbackMs;
  if (value == null || value === '') return fallback;
  if (value instanceof Date) {
    const ms = value.getTime();
    return Number.isFinite(ms) ? ms : fallback;
  }
  const raw = String(value).trim();
  const n = typeof value === 'number' ? value : (/^-?\d+(\.\d+)?$/.test(raw) ? Number(raw) : NaN);
  if (Number.isFinite(n)) {
    if (n <= 0) return fallback;
    return n < 100000000000 ? n * 1000 : n;
  }
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function timestampDate(value, fallbackMs) {
  return new Date(timestampMs(value, fallbackMs));
}

function fmtLocalTime(value) {
  return timestampDate(value).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}

/* 危险命令粗检：远端 allow 前二次确认 */
function isDangerousPerm(text) {
  const s = (text || '').toLowerCase();
  return /\brm\s+-rf\b/.test(s)
    || /\.ssh/.test(s)
    || /curl\s+[^\n]*\|\s*(ba)?sh/.test(s)
    || /\bsudo\b/.test(s)
    || /(?:\/etc\/|\/root\/|~\/\.ssh)/.test(s)
    || />\s*\/etc/.test(s);
}

let notifGranted = null;

function maybeNotifyPending(perms, asks) {
  if (!perms && !asks) return;
  if (!('Notification' in window)) return;
  if (Notification.permission === 'granted') notifGranted = true;
  else if (Notification.permission === 'default' && notifGranted === null) {
    Notification.requestPermission().then(p => { notifGranted = p === 'granted'; });
    return;
  }
  if (!notifGranted) return;
  const parts = [];
  if (perms) parts.push(perms + ' 个审批待决');
  if (asks) parts.push(asks + ' 个提问待答');
  try {
    new Notification('Phantom Console', {
      body: parts.join(' · '),
      tag: 'phantom-pending',
      renotify: true,
    });
  } catch (e) {}
}

/* ── Markdown 渲染 ── */
/* ── Markdown rendering (marked.js) ── */
let _markedReady = false;
const _markedQueue = [];

function _initMarked() {
  if (typeof marked === 'undefined') return;
  if (_markedReady) return;
  _markedReady = true;
  marked.setOptions({ breaks: true, gfm: true });
  // Custom renderer: wrap code blocks with our codewrap + copy button
  const renderer = new marked.Renderer();
  renderer.code = function({ text, lang, escaped }) {
    const content = (text || '').trimEnd();
    // Skip empty code blocks — LLMs sometimes emit bare ``` fences
    if (!content) return '';
    const cls = lang ? 'language-' + lang : '';
    const cid = 'c' + Math.random().toString(36).slice(2, 8);
    // marked.js pre-escapes text by default (escaped=true). Don't double-escape.
    const codeHtml = escaped ? content : esc(content);
    return '<div class="codewrap"><div class="codehead"><span>' + esc(lang || 'code') + '</span><button class="cp" data-target="' + cid + '">📋</button></div><pre><code id="' + cid + '" class="' + cls + '">' + codeHtml + '</code></pre></div>';
  };
  marked.use({ renderer });
  // Flush queued renders
  _markedQueue.forEach(fn => fn());
  _markedQueue.length = 0;
}

function md(s) {
  if (!s) return '';
  if (!_markedReady) {
    _initMarked();
    if (!_markedReady) {
      // marked.js not loaded yet — queue a re-render for the next call
      return '<p>' + esc(s) + '</p>';
    }
  }
  try {
    const html = marked.parse(s);
    return typeof html === 'string' ? html : '<p>' + esc(s) + '</p>';
  } catch (e) {
    return '<p>' + esc(s) + '</p>';
  }
}

/* ── Diff 着色 ── */
function diffPaint(el) {
  const lines = (el.textContent || '').split('\n');
  el.innerHTML = lines.map(ln => {
    if (/^\+[^+]/.test(ln)) return '<span class="diff-add">' + esc(ln) + '</span>';
    if (/^-[^-]/.test(ln)) return '<span class="diff-del">' + esc(ln) + '</span>';
    if (/^@@/.test(ln)) return '<span class="diff-hunk">' + esc(ln) + '</span>';
    return esc(ln);
  }).join('\n');
}

function highlightUnder(root) {
  const nodes = Array.from(root.querySelectorAll('.codewrap pre code')).filter(el =>
    !el.classList.contains('hljs') && !el.dataset.hl
  );
  if (!nodes.length) return;
  const run = (deadline) => {
    const start = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    while (nodes.length) {
      const el = nodes.shift();
      if (!el || el.dataset.hl) continue;
      el.dataset.hl = '1';
      const txt = el.textContent || '';
      if (/^diff |^--- |^\+\+\+ |^@@ /.test(txt)) {
        diffPaint(el);
      } else if (typeof hljs !== 'undefined') {
        try {
          const cls = el.className || '';
          if (cls.includes('language-')) {
            const r = hljs.highlight(txt, { language: cls.replace('language-', '') });
            el.innerHTML = r.value;
          } else if (txt.length < 12000) {
            const r = hljs.highlightAuto(txt);
            el.innerHTML = r.value;
          }
        } catch(e) { el.textContent = txt; }
      }
      const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
      if (nodes.length && ((deadline && deadline.timeRemaining && deadline.timeRemaining() < 4) || now - start > 6)) {
        schedule();
        break;
      }
    }
  };
  const schedule = () => {
    if (typeof requestIdleCallback === 'function') requestIdleCallback(run, { timeout: 300 });
    else setTimeout(() => run(null), 0);
  };
  schedule();
}

/* ── Toast ── */
function toast(msg, ms = 2400) {
  const t = $('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._tid);
  t._tid = setTimeout(() => t.classList.remove('show'), ms);
}

/* ── 复制按钮 ── */
document.addEventListener('click', e => {
  if (!e.target.classList.contains('cp')) return;
  const el = document.getElementById(e.target.dataset.target);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent || '').then(() => {
    e.target.textContent = '✓';
    setTimeout(() => { e.target.textContent = '📋'; }, 1600);
  }).catch(() => {});
});

/* ── Scroll ── */
function feedScrollKey(sn) {
  return 'phantom_scroll_' + (sn || '__all__');
}

/* ── WebSocket transport (Kiro: dual-channel, low-latency) ── */
let _ws = null; let _wsCb = null; let _wsReconnectTimer = null;

function wsConnect(callback, url) {
  if (_ws && _ws.readyState === WebSocket.OPEN) return;
  _wsCb = callback;
  const apiBase = url || (typeof API_BASE !== 'undefined' ? API_BASE : '');
  if (!apiBase) return;
  const wsUrl = apiBase.replace(/^http/, 'ws') + '/api/ws?since=' + (typeof since !== 'undefined' ? since : 0)
    + '&key=' + (typeof KEY !== 'undefined' ? KEY : '')
    + (typeof session !== 'undefined' && session ? '&session=' + encodeURIComponent(session) : '');
  try {
    _ws = new WebSocket(wsUrl);
    _ws.onopen = () => { console.log('[ws] connected'); };
    _ws.onmessage = (e) => {
      try {
        const j = JSON.parse(e.data);
        if (_wsCb) _wsCb(j);
      } catch (ex) {}
    };
    _ws.onclose = () => {
      _ws = null;
      if (typeof _settings !== 'undefined' && (_settings.transport === 'ws' || _settings.transport === 'auto')) {
        _wsReconnectTimer = setTimeout(() => wsConnect(callback, url), 3000);
      }
    };
    _ws.onerror = () => { _ws?.close(); };
  } catch (e) {}
}

function wsDisconnect() {
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
  if (_ws) { _ws.onclose = null; _ws.close(); _ws = null; }
}

function updateTransport(mode) {
  if (mode === 'ws') {
    wsConnect(_wsCb);
  } else if (mode === 'poll') {
    wsDisconnect();
  }
  // 'auto': keep both — WS preferred, poll as fallback
}

function nearBottom() {
  const feed = $('feed');
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
}
let _scrollBottomRaf = null;
function scrollBottom() {
  const feed = $('feed');
  if (!feed) return;
  if (_scrollBottomRaf) return;
  _scrollBottomRaf = requestAnimationFrame(() => {
    _scrollBottomRaf = null;
    feed.scrollTop = feed.scrollHeight;
  });
}

let scrollSaveTimer = null;
$('feed').addEventListener('scroll', () => {
  $('jump').classList.toggle('show', !nearBottom());
  clearTimeout(scrollSaveTimer);
  scrollSaveTimer = setTimeout(() => {
    try {
      if (typeof session !== 'undefined') {
        localStorage.setItem(feedScrollKey(session), String($('feed').scrollTop));
      }
    } catch (e) {}
  }, 200);
}, { passive: true });
$('jump').addEventListener('click', scrollBottom);
