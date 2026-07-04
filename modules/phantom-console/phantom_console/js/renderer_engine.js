/* renderer_engine.js — independent async render engine: worker pool parse + GPU-friendly DOM commit */
(function (global) {
  'use strict';

  function fallbackEsc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fallbackMd(raw) {
    if (typeof global.md === 'function') return global.md(raw || '');
    return '<p>' + fallbackEsc(raw || '') + '</p>';
  }

  function fallbackHighlight(root) {
    if (typeof global.highlightUnder === 'function') global.highlightUnder(root);
  }

  function makeWorkerSource() {
    return [
      "const ESC = (s) => String(s == null ? '' : s)",
      "  .replace(/&/g, '&amp;')",
      "  .replace(/</g, '&lt;')",
      "  .replace(/>/g, '&gt;')",
      "  .replace(/\\\"/g, '&quot;')",
      "  .replace(/'/g, '&#39;');",
      "",
      "let workerMarked = null;",
      "try {",
      "  importScripts('https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.0/marked.min.js');",
      "  workerMarked = self.marked || null;",
      "  if (workerMarked) {",
      "    workerMarked.setOptions({ breaks: true, gfm: true });",
      "    const renderer = new workerMarked.Renderer();",
      "    renderer.code = function(args) {",
      "      const text = args && args.text || '';",
      "      const lang = args && args.lang || '';",
      "      const escaped = args && args.escaped;",
      "      const content = String(text || '').trimEnd();",
      "      if (!content) return '';",
      "      const cls = lang ? 'language-' + lang : '';",
      "      const cid = 'c' + Math.random().toString(36).slice(2, 8);",
      "      const codeHtml = escaped ? content : ESC(content);",
      "      return '<div class=\"codewrap\"><div class=\"codehead\"><span>' + ESC(lang || 'code') + '</span><button class=\"cp\" data-target=\"' + cid + '\">📋</button></div><pre><code id=\"' + cid + '\" class=\"' + cls + '\">' + codeHtml + '</code></pre></div>';",
      "    };",
      "    workerMarked.use({ renderer });",
      "  }",
      "} catch (e) {",
      "  workerMarked = null;",
      "}",
      "",
      "const RX_CODE = new RegExp('`([^`]+)`', 'g');",
      "const RX_BOLD = new RegExp('\\\\*\\\\*([^*]+)\\\\*\\\\*', 'g');",
      "const RX_EM = new RegExp('\\\\*([^*]+)\\\\*', 'g');",
      "const RX_FENCE = new RegExp('```([^\\\\n`]*)\\\\n([\\\\s\\\\S]*?)```', 'g');",
      "const RX_PARAS = new RegExp('\\\\n{2,}');",
      "const RX_LINE = new RegExp('\\\\n', 'g');",
      "",
      "function inline(s) {",
      "  return ESC(s)",
      "    .replace(RX_CODE, '<code>$1</code>')",
      "    .replace(RX_BOLD, '<strong>$1</strong>')",
      "    .replace(RX_EM, '<em>$1</em>');",
      "}",
      "",
      "function renderMarkdown(raw) {",
      "  raw = String(raw || '');",
      "  if (workerMarked) {",
      "    try {",
      "      const html = workerMarked.parse(raw);",
      "      return typeof html === 'string' ? html : '<p>' + ESC(raw) + '</p>';",
      "    } catch (e) {}",
      "  }",
      "  const blocks = [];",
      "  let pos = 0;",
      "  let m;",
      "  while ((m = RX_FENCE.exec(raw))) {",
      "    if (m.index > pos) blocks.push({ type: 'text', text: raw.slice(pos, m.index) });",
      "    blocks.push({ type: 'code', lang: (m[1] || 'code').trim(), code: m[2] || '' });",
      "    pos = RX_FENCE.lastIndex;",
      "  }",
      "  if (pos < raw.length) blocks.push({ type: 'text', text: raw.slice(pos) });",
      "  if (!blocks.length) return '<p></p>';",
      "  return blocks.map((b) => {",
      "    if (b.type === 'code') {",
      "      const lang = ESC(b.lang || 'code');",
      "      const cls = b.lang ? 'language-' + ESC(b.lang) : '';",
      "      const cid = 'c' + Math.random().toString(36).slice(2, 8);",
      "      return '<div class=\"codewrap\"><div class=\"codehead\"><span>' + lang + '</span><button class=\"cp\" data-target=\"' + cid + '\">📋</button></div><pre><code id=\"' + cid + '\" class=\"' + cls + '\">' + ESC(b.code.trimEnd()) + '</code></pre></div>';",
      "    }",
      "    const parts = String(b.text || '').split(RX_PARAS).map((p) => p.trim()).filter(Boolean);",
      "    return parts.map((p) => '<p>' + inline(p).replace(RX_LINE, '<br>') + '</p>').join('');",
      "  }).join('') || '<p></p>';",
      "}",
      "",
      "self.onmessage = (e) => {",
      "  const msg = e.data || {};",
      "  if (msg.type !== 'render') return;",
      "  try {",
      "    self.postMessage({ id: msg.id, html: renderMarkdown(msg.raw || ''), ok: true });",
      "  } catch (err) {",
      "    self.postMessage({ id: msg.id, html: '<p>' + ESC(msg.raw || '') + '</p>', ok: false, error: String(err && err.message || err) });",
      "  }",
      "};"
    ].join('\n');
  }

  function safeSetting(key) {
    try { return global.localStorage && global.localStorage.getItem(key); } catch (e) { return ''; }
  }

  function webglProfile() {
    const info = { available: false, renderer: '' };
    try {
      const canvas = document.createElement('canvas');
      const attrs = { failIfMajorPerformanceCaveat: true, antialias: false, depth: false, stencil: false };
      const gl = canvas.getContext('webgl2', attrs) ||
        canvas.getContext('webgl', attrs) || canvas.getContext('experimental-webgl', attrs);
      if (!gl) return info;
      info.available = true;
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) info.renderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '');
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
    } catch (e) {}
    return info;
  }

  function detectQualityProfile() {
    const nav = global.navigator || {};
    const forced = String(safeSetting('phantom_render_quality') || '').toLowerCase();
    if (forced === 'low' || forced === 'medium' || forced === 'high') {
      return { tier: forced, forced: true, reason: 'localStorage override' };
    }
    const cores = Number(nav.hardwareConcurrency || 0);
    const memory = Number(nav.deviceMemory || 0);
    const conn = nav.connection || nav.mozConnection || nav.webkitConnection || null;
    const reduced = !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
    const coarse = !!(global.matchMedia && global.matchMedia('(pointer: coarse)').matches);
    const saveData = !!(conn && conn.saveData);
    const webgl = webglProfile();
    const renderer = String(webgl.renderer || '');
    const software = !webgl.available || /swiftshader|llvmpipe|softpipe|software|mesa|microsoft basic|warp|offscreen/i.test(renderer);
    const reasons = [];
    let score = 1;
    if (reduced) { score -= 3; reasons.push('reduced-motion'); }
    if (saveData) { score -= 2; reasons.push('save-data'); }
    if (software) { score -= 4; reasons.push(webgl.available ? 'software-gpu' : 'no-webgl'); }
    if (cores) {
      if (cores <= 2) { score -= 3; reasons.push('low-core'); }
      else if (cores <= 4) { score -= 1; reasons.push('mid-core'); }
      else score += 1;
    }
    if (memory) {
      if (memory <= 2) { score -= 2; reasons.push('low-memory'); }
      else if (memory <= 4) { score -= 1; reasons.push('mid-memory'); }
    }
    if (coarse && cores && cores <= 4) { score -= 1; reasons.push('coarse-low-core'); }
    const tier = score <= -3 ? 'low' : (score <= 1 ? 'medium' : 'high');
    return { tier, cores, memory, webgl: webgl.available, renderer, software, reduced, saveData, reason: reasons.join(',') || 'default' };
  }

  function RendererEngine() {
    this.workers = [];
    this.nextWorker = 0;
    this.seq = 0;
    this.pending = new Map();
    this.idleTimers = new WeakMap();
    this.asyncEnabled = false;
    this.gpuEnabled = false;
    this.profile = detectQualityProfile();
    this.targetFps = 30;
    this.idleMarkdownDelay = 1200;
    this.maxWorkers = 2;
    this.init();
  }

  RendererEngine.prototype.init = function () {
    this.applyQualityProfile();
    this.startWorkers();
  };

  RendererEngine.prototype.applyQualityProfile = function () {
    const root = document.documentElement;
    if (!root) return;
    const profile = this.profile || detectQualityProfile();
    const tier = profile.tier || 'medium';
    const cores = Number(profile.cores || (global.navigator && global.navigator.hardwareConcurrency) || 0);
    root.classList.remove('renderer-accelerated', 'renderer-low-power', 'renderer-medium', 'renderer-high', 'renderer-no-gpu');
    root.dataset.renderTier = tier;
    if (profile.software || profile.webgl === false) root.classList.add('renderer-no-gpu');
    if (tier === 'low') {
      root.classList.add('renderer-low-power');
      this.targetFps = 15;
      this.idleMarkdownDelay = 2400;
      this.maxWorkers = (global.Worker && cores !== 1) ? 1 : 0;
      this.gpuEnabled = false;
    } else if (tier === 'medium') {
      root.classList.add('renderer-medium');
      this.targetFps = 24;
      this.idleMarkdownDelay = 1800;
      this.maxWorkers = 1;
      this.gpuEnabled = false;
    } else {
      root.classList.add('renderer-high', 'renderer-accelerated');
      this.targetFps = 30;
      this.idleMarkdownDelay = 1200;
      this.maxWorkers = Math.max(1, Math.min(2, (cores ? Math.max(1, Math.floor(cores / 2)) : 1)));
      this.gpuEnabled = !!profile.webgl && !profile.software;
    }
  };

  RendererEngine.prototype.enableHardwareAcceleration = function () {
    this.profile = { tier: 'high', forced: true, reason: 'manual-enable' };
    this.applyQualityProfile();
  };

  RendererEngine.prototype.startWorkers = function () {
    if (!global.Worker || !global.Blob || !global.URL) return;
    for (let i = 0; i < this.maxWorkers; i++) this.spawnWorker();
    this.asyncEnabled = this.workers.length > 0;
  };

  RendererEngine.prototype.spawnWorker = function () {
    try {
      const blob = new Blob([makeWorkerSource()], { type: 'application/javascript' });
      const url = URL.createObjectURL(blob);
      const worker = new Worker(url);
      URL.revokeObjectURL(url);
      worker.onmessage = this.onWorkerMessage.bind(this);
      worker.onerror = () => this.retireWorker(worker);
      this.workers.push(worker);
    } catch (e) {}
  };

  RendererEngine.prototype.retireWorker = function (worker) {
    const idx = this.workers.indexOf(worker);
    if (idx >= 0) this.workers.splice(idx, 1);
    try { worker && worker.terminate(); } catch (e) {}
    if (this.nextWorker >= this.workers.length) this.nextWorker = 0;
    this.asyncEnabled = this.workers.length > 0;
  };

  RendererEngine.prototype.pickWorker = function () {
    if (!this.workers.length) return null;
    const worker = this.workers[this.nextWorker % this.workers.length];
    this.nextWorker = (this.nextWorker + 1) % this.workers.length;
    return worker;
  };

  RendererEngine.prototype.onWorkerMessage = function (e) {
    const msg = e.data || {};
    const job = this.pending.get(msg.id);
    if (!job) return;
    this.pending.delete(msg.id);
    if (job.el.dataset.renderJob !== String(msg.id)) return;
    this.commitHtml(job.el, job.raw, msg.html || fallbackMd(job.raw), job.options);
  };

  RendererEngine.prototype.commitHtml = function (el, raw, html, options) {
    if (!el || !el.parentNode) return;
    const near = typeof global.nearBottom === 'function' ? global.nearBottom() : true;
    el.dataset.rendered = raw || '';
    el.dataset.renderMode = 'html';
    el.innerHTML = html || '';
    el.classList.add('streaming-cursor', 'streaming');
    fallbackHighlight(el);
    if (options && typeof options.afterCommit === 'function') {
      try { options.afterCommit(el); } catch (e) {}
    }
    if (near && typeof global.scrollBottom === 'function') global.scrollBottom();
  };

  RendererEngine.prototype.clearIdleMarkdownFor = function (el) {
    const timer = this.idleTimers.get(el);
    if (timer) {
      clearTimeout(timer);
      this.idleTimers.delete(el);
    }
  };

  RendererEngine.prototype.cancelPendingFor = function (el) {
    if (!el || !this.pending.size) return;
    this.pending.forEach((job, id) => {
      if (job.el === el) this.pending.delete(id);
    });
  };

  RendererEngine.prototype.markPlainText = function (el, raw, options) {
    if (!el || !el.parentNode) return;
    const near = typeof global.nearBottom === 'function' ? global.nearBottom() : true;
    raw = raw || '';
    this.cancelPendingFor(el);
    const prev = el.dataset.rendered || '';
    const wasText = el.dataset.renderMode === 'text';
    const marker = el.querySelector && el.querySelector('.typing-indicator');
    el.dataset.renderJob = 'text-' + (++this.seq);
    el.dataset.rendered = raw;
    el.dataset.renderMode = 'text';
    if (raw.startsWith(prev) && prev && wasText) {
      const delta = raw.slice(prev.length);
      if (delta) {
        if (marker && marker.parentNode === el) el.insertBefore(document.createTextNode(delta), marker);
        else el.appendChild(document.createTextNode(delta));
      }
    } else {
      if (marker) marker.remove();
      if (el.textContent !== raw) el.textContent = raw;
    }
    el.classList.add('streaming-cursor', 'streaming');
    if (options && typeof options.afterCommit === 'function') {
      try { options.afterCommit(el); } catch (e) {}
    }
    if (near && typeof global.scrollBottom === 'function') global.scrollBottom();
  };

  RendererEngine.prototype.scheduleIdleMarkdown = function (el, raw, options) {
    this.clearIdleMarkdownFor(el);
    const timer = setTimeout(() => {
      this.idleTimers.delete(el);
      if (!el || !el.parentNode) return;
      if ((el.dataset.raw || '') !== (raw || '')) return;
      this.renderMarkdownInto(el, raw, options);
    }, this.idleMarkdownDelay);
    this.idleTimers.set(el, timer);
  };

  RendererEngine.prototype.renderStreamingTextInto = function (el, raw, options) {
    options = options || {};
    raw = raw || '';
    if (!el) return;
    if ((el.dataset.rendered || '') !== raw || el.dataset.renderMode !== 'text') {
      this.markPlainText(el, raw, options);
    } else if (options.afterCommit) {
      try { options.afterCommit(el); } catch (e) {}
    }
    this.scheduleIdleMarkdown(el, raw, options);
  };

  RendererEngine.prototype.renderMarkdownInto = function (el, raw, options) {
    options = options || {};
    raw = raw || '';
    if (!el) return;
    this.clearIdleMarkdownFor(el);
    if ((el.dataset.rendered || '') === raw && el.dataset.renderMode === 'html') {
      if (options.afterCommit) options.afterCommit(el);
      return;
    }
    const worker = this.pickWorker();
    if (worker && this.asyncEnabled) {
      this.cancelPendingFor(el);
      const id = ++this.seq;
      el.dataset.renderJob = String(id);
      this.pending.set(id, { el, raw, options });
      try {
        worker.postMessage({ type: 'render', id, raw });
        return;
      } catch (e) {
        this.pending.delete(id);
        this.retireWorker(worker);
      }
    }
    this.cancelPendingFor(el);
    el.dataset.renderJob = 'sync-' + (++this.seq);
    this.commitHtml(el, raw, fallbackMd(raw), options);
  };

  RendererEngine.prototype.flushMarkdownInto = function (el, raw, options) {
    options = options || {};
    raw = raw || '';
    if (!el) return;
    this.clearIdleMarkdownFor(el);
    this.cancelPendingFor(el);
    el.dataset.renderJob = 'sync-' + (++this.seq);
    const html = fallbackMd(raw);
    this.commitHtml(el, raw, html, options);
  };

  RendererEngine.prototype.stats = function () {
    return {
      independent: true,
      qualityTier: this.profile && this.profile.tier || 'unknown',
      profileReason: this.profile && this.profile.reason || '',
      noGpu: !!(this.profile && (this.profile.software || this.profile.webgl === false)),
      hardwareAccelerated: !!this.gpuEnabled,
      asyncWorkers: this.workers.length,
      maxWorkers: this.maxWorkers,
      asyncWorker: this.workers.length > 0,
      pending: this.pending.size,
    };
  };

  global.RendererEngine = RendererEngine;
  global.rendererEngine = global.rendererEngine || new RendererEngine();
})(window);
