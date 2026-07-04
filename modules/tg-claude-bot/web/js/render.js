/* render.js — DOM 渲染：turn 卡片、文本、工具行、代码块 */

const S = {};           // session -> turn state
const toolEls = {};     // "${session}/${toolId}" -> DOM element
const todosBy = {};     // session -> todo[]
const permEls = {};     // "${session}/${token}" -> DOM element

function st(name) {
  if (!S[name]) S[name] = { turn: null, hasBody: false, lastText: null };
  return S[name];
}
/* ── Kiro helpers ── */
let _lastDateSep = null;

function insertDateSep(ts) {
  const d = timestampDate(ts);
  const now = new Date();
  let label;
  if (d.toDateString() === now.toDateString()) label = '今天';
  else if (new Date(now - 86400000).toDateString() === d.toDateString()) label = '昨天';
  else label = d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
  if (_lastDateSep === label) return;
  _lastDateSep = label;
  const sep = document.createElement('div'); sep.className = 'date-sep';
  sep.textContent = label;
  sep.dataset.date = label;
  $('feed').appendChild(sep);
}

function addMsgActions(turnDiv, text) {
  const actions = document.createElement('div'); actions.className = 'msg-actions';
  const copyBtn = document.createElement('button');
  copyBtn.innerHTML = '⎘'; copyBtn.title = '复制';
  copyBtn.addEventListener('click', e => {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      copyBtn.classList.add('copied');
      setTimeout(() => copyBtn.classList.remove('copied'), 1500);
    }).catch(() => {});
  });
  actions.appendChild(copyBtn);
  turnDiv.appendChild(actions);
}

function addBubbleCopy(bub, text) {
  const btn = document.createElement('button'); btn.className = 'copy-btn';
  btn.textContent = '⎘'; btn.title = '复制';
  btn.addEventListener('click', e => {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      btn.classList.add('copied');
      setTimeout(() => btn.classList.remove('copied'), 1500);
    }).catch(() => {});
  });
  bub.appendChild(btn);
}

function resetAll() {
  for (const k of Object.keys(S)) delete S[k];
  for (const k of Object.keys(toolEls)) delete toolEls[k];
  for (const k of Object.keys(permEls)) delete permEls[k];
  _lastDateSep = null;
  $('feed').innerHTML = '';
  if (typeof removeAllTypingIndicators === 'function') removeAllTypingIndicators();
}

/* ── Turn 容器 ── */
function newTurn(s) {
  // Clean up previous indicator, show new one in this turn
  if (typeof removeTypingIndicator === 'function') removeTypingIndicator(s);
  const startMs = timestampMs(s.startTs);
  insertDateSep(startMs);
  const div = document.createElement('div'); div.className = 'turn';
  const meta = document.createElement('div'); meta.className = 'turn meta';
  meta.innerHTML = '<span class="name">' + esc(s.name) + '</span>' +
    '<span class="time">' + fmtLocalTime(startMs) + '</span>' +
    '<span class="status running" data-role="status">' + stText('running') + '</span>';
  s.durEl = meta.querySelector('.status');
  s.statusEl = meta.querySelector('.time');
  div.appendChild(meta);
  const body = document.createElement('div'); body.className = 'turn body';
  s.bodyEl = body;
  div.appendChild(body);
  s.turn = div;
  s.turnText = '';   // accumulate text for copy
  s.hasBody = false;
  s.toolCount = 0;
  s.lastText = null;
  $('feed').appendChild(div);
}

function ensureTurn(s, ev) {
  if (ev.ts && !s.startTs) s.startTs = ev.ts;
  if (!s.turn || (ev.type === 'turn_start')) {
    if (s.turn && ev.type === 'turn_start') {
      // 新 turn：结束上一个
      finalizeTurn(s, 'done');
    }
    newTurn(s);
    // Show typing indicator for the new turn
    if (typeof showTypingIndicator === 'function') showTypingIndicator(s);
  }
  return s.turn;
}

function finalizeTurn(s, status) {
  if (!s.turn) return;
  flushTextRaf(s);      // render any pending text before finalizing
  if (typeof removeTypingIndicator === 'function') removeTypingIndicator(s);
  const el = s.durEl;
  if (!el) return;
  el.classList.remove('running');
  if (status === 'done') {
    el.classList.add('done');
    el.textContent = stText('done');
  } else if (status === 'interrupted') {
    el.classList.add('error');
    el.textContent = stText('interrupted');
  } else {
    el.classList.add('error');
    el.textContent = ST_ICON.error + ' ' + (status || ST_LABEL.error);
  }
  // Remove streaming cursor from last text block
  if (s.lastText) s.lastText.classList.remove('streaming-cursor', 'streaming');
  // Close thinking + tool groups
  if (s._thinkEl) s._thinkEl.open = false;
  closeCot(s);
  // Add copy action for the accumulated turn text
  if (s.turnText) addMsgActions(s.turn, s.turnText);
  // Mark any running tool steps as completed
  if (s._cotBody) {
    s._cotBody.querySelectorAll('.cot-step.running').forEach(step => {
      step.classList.remove('running');
      step.classList.add('completed');
      const icon = step.querySelector('.cs-icon');
      if (icon) icon.textContent = '✓';
    });
  }
}

/* ── 用户气泡 ── */
function renderUser(ev) {
  const bub = document.createElement('div');
  bub.className = 'ububble' + (ev.steer ? ' steer' : '');
  const text = (ev.text || '').slice(0, 2000);

  if (ev.steer) {
    // 插话：渲染进当前回合的 body，用突出样式体现"插入对话流"的动作
    const sn = ev.session || 'main';
    const s = st(sn);
    if (s.turn && s.bodyEl) {
      bub.innerHTML = '<span class="steer-tag">⚡ 插话</span>' +
        '<span class="steer-text">' + esc(text) + '</span>';
      s.bodyEl.appendChild(bub);
      s.hasBody = true;
    } else {
      bub.textContent = '⤷ 插话 · ' + text;
      insertDateSep(ev.ts);
      $('feed').appendChild(bub);
    }
  } else {
    bub.textContent = text;
    insertDateSep(ev.ts || Date.now());
    $('feed').appendChild(bub);
  }
  addBubbleCopy(bub, text);
  scrollBottom();
}

/* ── 文本块 ── */
function renderMarkdownBlock(s, el, raw, sync) {
  if (!el || !el.parentNode) return;
  if ((el.dataset.rendered || '') === raw && el.dataset.renderMode === 'html') {
    if (typeof showTypingIndicator === 'function') showTypingIndicator(s, el);
    return;
  }
  if (sync && typeof removeAllTypingIndicators === 'function') removeAllTypingIndicators();
  s._lastRendered = raw;
  const afterCommit = () => {
    if (typeof showTypingIndicator === 'function') showTypingIndicator(s, el);
  };
  if (typeof rendererEngine !== 'undefined' && rendererEngine) {
    if (sync && typeof rendererEngine.flushMarkdownInto === 'function') {
      rendererEngine.flushMarkdownInto(el, raw, { afterCommit });
    } else if (!sync && typeof rendererEngine.renderStreamingTextInto === 'function') {
      rendererEngine.renderStreamingTextInto(el, raw, { afterCommit });
    } else {
      rendererEngine.renderMarkdownInto(el, raw, { afterCommit });
    }
    return;
  }
  el.dataset.rendered = raw;
  el.dataset.renderMode = 'html';
  el.innerHTML = md(raw);
  el.classList.add('streaming-cursor', 'streaming');
  highlightUnder(el);
  afterCommit();
  if (nearBottom()) scrollBottom();
}

function scheduleTextRender(s, textEl) {
  if (s._textRaf || s._textTimer) return;
  const fps = (typeof rendererEngine !== 'undefined' && rendererEngine && rendererEngine.targetFps) || 30;
  const minDelay = Math.max(16, Math.floor(1000 / fps));
  const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
  const due = Math.max(0, minDelay - (now - (s._lastTextDispatchAt || 0)));
  const dispatch = () => {
    s._textTimer = null;
    s._textRaf = requestAnimationFrame(() => {
      s._textRaf = null;
      s._lastTextDispatchAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
      if (!textEl || !textEl.parentNode) return;
      renderMarkdownBlock(s, textEl, textEl.dataset.raw || '', false);
    });
  };
  if (due > 4) s._textTimer = setTimeout(dispatch, due);
  else dispatch();
}

function renderText(s, ev) {
  ensureTurn(s, ev);
  if (!ev.text) return;
  if (s.lastText) s.lastText.classList.remove('streaming-cursor', 'streaming');
  if (!s.lastText || s.lastText.dataset.para !== '1') {
    const div = document.createElement('div');
    div.className = 'txt';
    div.dataset.para = '1';
    div.dataset.raw = '';
    div.dataset.rendered = '';
    div.dataset.renderMode = '';
    s.bodyEl.appendChild(div);
    s.lastText = div;
  }
  // Accumulate raw text incrementally
  const textEl = s.lastText;
  const prevRaw = textEl.dataset.raw || '';
  textEl.dataset.raw = prevRaw + ev.text;
  s.turnText = (s.turnText || '') + ev.text;

  // Target-FPS throttle: plain text during stream, Markdown after idle/final flush.
  scheduleTextRender(s, textEl);
}

/* Flush any pending text render before a paragraph boundary (tool/thinking/turn_end) */
function flushTextRaf(s) {
  if (s._textTimer) {
    clearTimeout(s._textTimer);
    s._textTimer = null;
  }
  if (s._textRaf) {
    cancelAnimationFrame(s._textRaf);
    s._textRaf = null;
  }
  if (!s.lastText) return;
  renderMarkdownBlock(s, s.lastText, s.lastText.dataset.raw || '', true);
}

/* ── CoT: vertical list + overflow fold ── */
const COT_MAX_VISIBLE = 8;

function ensureCot(s) {
  if (!s._cotEl || s._cotEl.parentNode !== s.bodyEl) {
    s._cotEl = null;
  }
  if (!s._cotEl) {
    const wrap = document.createElement('div'); wrap.className = 'cot open';
    wrap.innerHTML = '<div class="cot-header"><span class="cot-chevron">▶</span> 工具调用</div>' +
      '<div class="cot-body"></div>';
    s.bodyEl.appendChild(wrap);
    s._cotEl = wrap;
    s._cotBody = wrap.querySelector('.cot-body');
    s._cotCount = 0;
    s._cotOverflow = null;
    wrap.querySelector('.cot-header').addEventListener('click', () => {
      wrap.classList.toggle('open');
    });
  }
  return s._cotBody;
}

function _refreshCot(s) {
  const body = s._cotBody;
  if (!body) return;
  const all = Array.from(body.querySelectorAll('.cot-step'));
  const total = all.length;
  const hdr = s._cotEl.querySelector('.cot-header');
  if (hdr) {
    hdr.childNodes.forEach(c => { if (c.nodeType === 3) c.remove(); });
    hdr.appendChild(document.createTextNode(' 工具调用 (' + total + ' 步)'));
  }
  if (s._cotOverflow) { s._cotOverflow.remove(); s._cotOverflow = null; }
  // Fold overflow: hide beyond COT_MAX_VISIBLE, offer "unfold" to vertical list
  if (total > COT_MAX_VISIBLE && !body.classList.contains('unfolded')) {
    all.forEach((step, i) => {
      if (i >= COT_MAX_VISIBLE) step.style.display = 'none';
      else step.style.display = '';
    });
    const overflow = document.createElement('button');
    overflow.className = 'cot-overflow';
    overflow.textContent = '+' + (total - COT_MAX_VISIBLE) + ' more';
    overflow.addEventListener('click', () => {
      body.classList.add('unfolded');
      all.forEach(s => s.style.display = '');
      // Replace overflow button with collapse button
      overflow.textContent = '收起';
      overflow.className = 'cot-overflow cot-collapse';
      overflow.addEventListener('click', function collapse() {
        body.classList.remove('unfolded');
        overflow.removeEventListener('click', collapse);
        _refreshCot(s); // rebuild chips + overflow
      }, { once: true });
    });
    body.appendChild(overflow);
    s._cotOverflow = overflow;
  }
}

function addCotStep(s, icon, label, phase, detail) {
  const body = ensureCot(s);
  const step = document.createElement('div');
  step.className = 'cot-step ' + (phase || 'running');
  step.innerHTML = '<span class="cs-icon">' + esc(icon) + '</span>' +
    '<span class="cs-label">' + esc((label || '').slice(0, 60)) + '</span>';
  step.title = (label || '').slice(0, 200);
  step._phase = phase;
  step._detail = (detail || '').slice(0, 2000);
  step._detailEl = null;

  step.addEventListener('click', () => {
    // Hide any other open detail in same CoT
    s._cotEl.querySelectorAll('.cot-step-detail.show').forEach(el => {
      if (el !== step._detailEl) el.classList.remove('show');
    });
    if (!step._detailEl) {
      step._detailEl = document.createElement('div');
      step._detailEl.className = 'cot-step-detail show';
      step._detailEl.textContent = step._detail || '(暂无详情)';
      // Unfolded (vertical list): place detail right below the clicked step.
      // Collapsed (chips): place detail at bottom of .cot wrapper, below all chips.
      if (body.classList.contains('unfolded')) {
        step.after(step._detailEl);
      } else {
        s._cotEl.appendChild(step._detailEl);
      }
    } else {
      if (step._detailEl.textContent !== (step._detail || '').slice(0, 2000)) {
        step._detailEl.textContent = (step._detail || '(暂无详情)').slice(0, 2000);
      }
      step._detailEl.classList.toggle('show');
    }
  });

  body.appendChild(step);
  s._cotCount = (s._cotCount || 0) + 1;
  _refreshCot(s);
  return step;
}

function closeCot(s) {
  if (s._cotEl) {
    s._cotEl.classList.remove('open');
  }
}

/* ── 工具行 ── */
const T_ICONS = { running: ST_ICON.running, completed: ST_ICON.done, error: ST_ICON.error, rejected: ST_ICON.interrupted };
const T_LABELS = {
  Read: (d, p) => (p ? 'Read ' : 'Reading ') + (d.file_path || d.path || 'file').split('/').pop(),
  Write: (d, p) => (p ? 'Wrote ' : 'Writing ') + (d.file_path || d.path || 'file').split('/').pop(),
  Edit: (d, p) => (p ? 'Edited ' : 'Editing ') + (d.file_path || d.path || 'file').split('/').pop(),
  Bash: (d, p) => {
    const c = (d.command || '').slice(0, 60);
    return (p ? 'Ran: ' : 'Running: ') + (c || 'command');
  },
  Grep: (d, p) => (p ? 'Searched "' : 'Searched ') + '"' + (d.pattern || d.query || '').slice(0, 40) + '"',
  Glob: (d, p) => (p ? 'Searched files "' : 'Searching files "') + (d.pattern || '').slice(0, 40) + '"',
  Task: (d, p) => (p ? 'Ran sub-agent: ' : 'Running sub-agent: ') + (d.description || d.subagent_type || '').slice(0, 40),
  TodoWrite: () => 'Updating task list',
};

function toolLabel(tool, input, past) {
  tool = tool || '';
  const fn = T_LABELS[tool];
  if (fn) return fn(input || {}, past);
  if (!tool) return 'tool';
  return tool.startsWith('mcp__') ? tool.split('__').pop() : tool;
}

function renderTool(s, ev) {
  ensureTurn(s, ev);
  flushTextRaf(s);      // render pending text before tool boundary
  s.lastText = null;
  const key = ev.session + '/' + ev.id;
  let row = toolEls[key];
  const phase = ev.phase || 'running';
  const past = phase === 'completed' || phase === 'error';
  const label = ev.label || toolLabel(ev.tool, ev.input, past);
  const icon = T_ICONS[phase] || '◐';
  // Build detail: input params + output/summary
  let detail = '';
  if (ev.input) {
    detail += '📥 Input:\n' + (typeof ev.input === 'string' ? ev.input : JSON.stringify(ev.input, null, 1));
  }
  if (ev.summary) {
    if (detail) detail += '\n\n';
    detail += '📤 Output: ' + ev.summary;
  }

  // Kiro: group tools into Chain-of-Thought
  if (!row) {
    row = addCotStep(s, icon, label, phase, detail);
    row._key = key;
    row._phase = phase;
    row._detail = detail;
    toolEls[key] = row;
    s.toolCount = (s.toolCount || 0) + 1;
  } else {
    // Update existing step in-place (no DOM rebuild)
    row.className = 'cot-step ' + phase;
    row._phase = phase;
    const iconEl = row.querySelector('.cs-icon');
    if (iconEl) iconEl.textContent = T_ICONS[phase] || icon;
    const labelEl = row.querySelector('.cs-label');
    if (labelEl) labelEl.textContent = label.slice(0, 40);
    // Accumulate detail: input on first run, result/output on completion
    if (detail && detail !== row._detail) {
      row._detail = (row._detail ? row._detail + '\n\n' : '') + detail;
    }
    // Update live detail panel if already open
    if (row._detailEl && row._detailEl.classList.contains('show')) {
      if (row._detailEl.textContent !== row._detail.slice(0, 2000)) {
        row._detailEl.textContent = row._detail.slice(0, 2000);
      }
    }
  }
  if (nearBottom()) scrollBottom();
}

/* ── 思考 / 笔记 / 统计 ── */
/* ── Thinking group: separate collapsible section (Kiro: reasoning chain) ── */
function ensureThinkGroup(s) {
  if (!s._thinkEl || s._thinkEl.parentNode !== s.bodyEl) {
    s._thinkEl = null; s._thinkCount = 0;
  }
  if (!s._thinkEl) {
    const wrap = document.createElement('details');
    wrap.className = 'think-group'; wrap.open = true;
    wrap.innerHTML = '<summary><span class="tg-chevron">▶</span> 💬 思考过程</summary>' +
      '<div class="tg-body"></div>';
    s.bodyEl.appendChild(wrap);
    s._thinkEl = wrap;
    s._thinkBody = wrap.querySelector('.tg-body');
  }
  return s._thinkBody;
}

function renderThinking(s, ev) {
  ensureTurn(s, ev);
  flushTextRaf(s);
  s.lastText = null;
  const body = ensureThinkGroup(s);
  const full = ev.text || '';
  const block = document.createElement('div');
  block.className = 'think-block';
  block.textContent = full.slice(0, 2000);
  body.appendChild(block);
  s._thinkCount = (s._thinkCount || 0) + 1;
  const sum = s._thinkEl.querySelector('summary');
  if (sum) {
    sum.childNodes.forEach(c => { if (c.nodeType === 3) c.remove(); });
    sum.appendChild(document.createTextNode(' 思考过程 (' + s._thinkCount + ' 段)'));
  }
}

function renderNote(s, ev) {
  ensureTurn(s, ev);
  s.lastText = null;   // 段落边界
  const div = document.createElement('div');
  div.className = 'note';
  div.textContent = (ev.text || '').slice(0, 300);
  s.bodyEl.appendChild(div);
}

function renderStats(s, ev) {
  ensureTurn(s, ev);
  const div = document.createElement('div');
  div.className = 'stats';
  // stats 是 Python 端预格式化的一行字符串（如 "⏱ 12.3s · 🔁 5轮 · 🪙 1.2k↑/3.4k↓"）
  const stats = ev.stats || '';
  if (stats) {
    stats.split(' · ').forEach(chip => {
      if (chip.trim()) div.innerHTML += '<span>' + esc(chip.trim()) + '</span>';
    });
  }
  s.bodyEl.appendChild(div);
}

/* ── 权限卡 ── */
function renderPerm(s, ev) {
  ensureTurn(s, ev);
  s.lastText = null;
  const div = document.createElement('div');
  div.className = 'perm';
  // 后端 perm 事件带的是 preview 字段（非 cmd/input）；老代码读错字段导致预览空白。
  const preview = String(ev.preview || ev.cmd || ev.input || '').slice(0, 400);
  div.innerHTML = '<div class="toolname">🔐 ' + esc(ev.tool || '') + '</div>' +
    '<div class="preview">' + esc(preview) + '</div>' +
    '<div class="btns">' +
    '<button class="allow" data-token="' + ev.token + '" data-dec="allow">✓ Allow</button>' +
    '<button class="always" data-token="' + ev.token + '" data-dec="always">✓ Always</button>' +
    '<button class="deny" data-token="' + ev.token + '" data-dec="deny">✗ Deny</button>' +
    '</div>';
  s.bodyEl.appendChild(div);
  permEls[ev.session + '/' + ev.token] = div;
  if (typeof updatePendingBar === 'function') updatePendingBar();

  div.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', async () => {
      const t = btn.dataset.token, d = btn.dataset.dec;
      if ((d === 'allow' || d === 'always') && isDangerousPerm(preview)) {
        if (!confirm('⚠️ 此命令看起来有风险，确定允许执行？\n\n' + preview.slice(0, 300))) return;
      }
      try {
        await fetch('/api/perm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
          body: JSON.stringify({ token: t, decision: d })
        });
      } catch(e) {}
      try { tg?.HapticFeedback?.impactOccurred?.('light'); } catch (e) {}
    });
  });
}

function renderPermDone(s, ev) {
  const el = permEls[ev.session + '/' + ev.token];
  if (!el) return;
  el.classList.add('resolved');
  const btns = el.querySelector('.btns');
  if (btns) {
    btns.innerHTML = '<span class="verdict">→ ' + esc(ev.decision || '') + '</span>';
  }
  delete permEls[ev.session + '/' + ev.token];
  if (typeof updatePendingBar === 'function') updatePendingBar();
}

/* ── 待处理操作条（审批 + 提问）── */
function pendingCounts() {
  const perms = Object.keys(permEls).length;
  let asks = 0;
  if (typeof askState !== 'undefined' && askState) asks++;
  if (typeof askPending !== 'undefined' && typeof activeAsks !== 'undefined') {
    Object.keys(askPending).forEach(t => {
      if (activeAsks.has(t) && (!askState || askState.token !== t)) asks++;
    });
  }
  return { perms, asks, total: perms + asks };
}

function scrollToPending() {
  // 优先滚到 feed 里第一条未决审批；没有审批就把挂起的提问模态重新弹出来。
  const perm = $('feed') && $('feed').querySelector('.perm:not(.resolved)');
  if (perm) {
    perm.scrollIntoView({ behavior: 'smooth', block: 'center' });
    perm.classList.add('flash');
    setTimeout(() => perm.classList.remove('flash'), 1200);
    return;
  }
  if (typeof showPendingAsk === 'function') showPendingAsk();
}

function updatePendingBar() {
  const bar = $('pendingBar');
  if (!bar) return;
  const { perms, asks, total } = pendingCounts();
  const sig = total ? perms + '|' + asks : '';
  // Diff guard: skip if nothing changed
  if (sig === bar._sig) return;
  bar._sig = sig;
  if (!total) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    const hb = $('hubBtn');
    if (hb) hb.classList.remove('has-pending');
    return;
  }
  bar.classList.remove('hidden');
  const parts = [];
  if (perms) parts.push('🔐 ' + perms + ' 审批');
  if (asks) parts.push('❓ ' + asks + ' 提问');
  bar.innerHTML = '<button type="button" id="pendingBarBtn">' +
    '<span class="pb-live"></span>' +
    '<span class="pb-text">' + parts.join('　·　') + ' 待处理</span>' +
    '<span class="pb-go">查看 ▸</span>' +
    '</button>';
  const btn = bar.querySelector('#pendingBarBtn');
  if (btn) btn.onclick = () => scrollToPending();
  const hb = $('hubBtn');
  if (hb) hb.classList.toggle('has-pending', total > 0);
  maybeNotifyPending(perms, asks);
}

/* ── 时间更新 ── */
setInterval(() => {
  const now = Date.now();
  Object.values(S).forEach(s => {
    if (s.durEl && s.durEl.classList.contains('running') && s.startTs) {
      const sec = Math.max(0, Math.floor((now - timestampMs(s.startTs, now)) / 1000));
      const min = Math.floor(sec / 60);
      s.durEl.textContent = stText('running',
        (min ? min + 'm' : '') + (sec % 60) + 's' + (s.toolCount ? ' · 🔧 ' + s.toolCount : ''));
    }
  });
}, 1000);
