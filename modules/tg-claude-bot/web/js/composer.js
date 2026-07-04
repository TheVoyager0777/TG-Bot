/* composer.js — 输入框、发送、停止、Session 切换 */

const DRAFT_PREFIX = 'phantom_draft_';

function draftKey(sn) {
  return DRAFT_PREFIX + (sn || '__all__');
}

function saveDraft() {
  try {
    localStorage.setItem(draftKey(session), $('input').value);
  } catch (e) {}
}

function clearDraft() {
  try { localStorage.removeItem(draftKey(session)); } catch (e) {}
}

function loadDraft() {
  try {
    const v = localStorage.getItem(draftKey(session));
    $('input').value = v || '';
    fitTextarea();
  } catch (e) {}
}

function updateComposerPlaceholder() {
  const el = $('input');
  if (!el) return;
  const target = session || '全部会话（只读）';
  el.placeholder = '→ ' + target + '  (Enter 发送 · Shift+Enter 换行)';
}

async function doSend() {
  const text = $('input').value.trim();
  if (!text) return;
  if (!session) {
    toast('请选择具体会话后再发送（“全部”仅用于查看）');
    return;
  }
  if (typeof stateLoaded !== 'undefined' && !stateLoaded && typeof refreshState === 'function') {
    try { await refreshState(); } catch (e) {}
  }
  const sessions = (stateCache && stateCache.sessions) || [];
  const sessionInfo = sessions.find(s => s.name === session);
  const validNames = new Set(sessions.map(s => s.name));
  validNames.add('main');
  if (sessionInfo && sessionInfo.historyOnly) {
    toast('这是历史会话；请先在接管面板重新接管后再发送');
    return;
  }
  if (!validNames.has(session)) {
    toast('当前会话已不存在，请重新选择会话');
    session = null;
    try { localStorage.setItem('phantom_session', '__all__'); } catch (e) {}
    renderSessionTabs(stateCache.sessions || []);
    loadControl('main');
    updateComposerPlaceholder();
    return;
  }
  $('input').value = '';
  clearDraft();
  fitTextarea();
  const s = session;
  try {
    const r = await fetch('/api/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
      body: JSON.stringify({ session: s, text })
    });
    const j = await r.json();
    if (j.status === 'queued') toast('已排队（会话正忙）');
    if (j.error) toast(j.error);
  } catch (e) {
    toast('发送失败');
  }
  try { tg?.HapticFeedback?.impactOccurred?.('light'); } catch (e) {}
}

function fitTextarea() {
  const el = $('input');
  el.style.height = '36px';
  el.style.height = Math.min(el.scrollHeight, 130) + 'px';
}

function updateStopBtn() {
  const btn = $('stopBtn');
  let busy = false;
  Object.values(S).forEach(s => { if (s.turn && s.durEl && s.durEl.classList.contains('running')) busy = true; });
  btn.style.display = busy ? 'block' : 'none';
}

function renderSessionTabs(sessions) {
  // Debounce: onResume calls refreshState() then event fetch, both can trigger
  // render within the same frame → flicker.  Coalesce calls within 80ms.
  if (renderSessionTabs._timer) clearTimeout(renderSessionTabs._timer);
  renderSessionTabs._timer = setTimeout(() => _renderSessionTabsNow(sessions), 80);
}
renderSessionTabs._timer = null;

function _renderSessionTabsNow(sessions) {
  const tabs = $('sessionTabs');
  const names = sessions.map(s => s.name);
  if (!names.includes('main')) names.unshift('main');

  // 「全部」标签：session=null 时高亮，点它回到聚合视图。其余标签仅在精确命中
  // 当前 session 时高亮——不再用 session||'main' 把 main 误高亮成「全部」（那正是
  // 状态栏/标签与实际视图对不上的根源）。
  let html = '<button class="stab' + (session ? '' : ' active') +
    '" data-session="__all__">全部</button>';
  html += names.map(n => {
    const info = sessions.find(s => s.name === n) || {};
    const active = n === session ? ' active' : '';
    const busy = info.busy ? ' busy' : '';
    const badge = info.queued ? '<span class="badge">' + info.queued + '</span>' : '';
    return '<button class="stab' + active + busy + '" data-session="' + esc(n) + '">' +
      esc(n) + badge + '</button>';
  }).join('');

  // 仅在内容变化时重写 DOM，避免 refreshState 周期性重建导致闪烁
  if (tabs._prevHtml === html) return;
  tabs._prevHtml = html;
  tabs.innerHTML = html;

  tabs.querySelectorAll('.stab').forEach(btn => {
    btn.addEventListener('click', () => {
      const v = btn.dataset.session === '__all__' ? null : btn.dataset.session;
      pickSession(v);
    });
  });
}

/* ── 初始化事件 ── */
document.addEventListener('DOMContentLoaded', () => {
  $('sendBtn').addEventListener('click', doSend);
  $('input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });
  let draftTimer = null;
  $('input').addEventListener('input', () => {
    fitTextarea();
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraft, 300);
  });
  updateComposerPlaceholder();

  $('stopBtn').addEventListener('click', async () => {
    try {
      const body = session ? JSON.stringify({ session }) : '{}';
      const r = await fetch('/api/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
        body
      });
      const j = await r.json();
      toast('已中断: ' + (j.stopped || []).join(', '));
    } catch (e) {
      toast('中断失败');
    }
  });

  $('stopAllBtn').addEventListener('click', async () => {
    if (!confirm('确定中断所有正在运行的会话？')) return;
    try {
      const r = await fetch('/api/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
        body: '{}'
      });
      const j = await r.json();
      const names = (j.stopped || []).join(', ');
      toast(names ? '已全停: ' + names : '当前无运行中会话');
      try { tg?.HapticFeedback?.notificationOccurred?.('warning'); } catch (e) {}
    } catch (e) {
      toast('全停失败');
    }
  });

  // 侧边栏切换
  $('sidebarToggle').addEventListener('click', () => {
    $('side').classList.toggle('hidden');
  });

  // ── Track composer height for CSS variable --composer-h ──
  const composerObs = new ResizeObserver(() => {
    const h = document.getElementById('composer').offsetHeight;
    document.documentElement.style.setProperty('--composer-h', h + 'px');
  });
  composerObs.observe(document.getElementById('composer'));

  // ── 键盘自适应：visualViewport resize 时调整 composer 位置 ──
  if (window.visualViewport) {
    const onVVResize = () => {
      const vv = window.visualViewport;
      const offsetBottom = window.innerHeight - (vv.offsetTop + vv.height);
      document.getElementById('composer').style.bottom = offsetBottom + 'px';
      const feed = document.getElementById('feed');
      feed.scrollTop = feed.scrollHeight;
    };
    window.visualViewport.addEventListener('resize', onVVResize);
    window.visualViewport.addEventListener('scroll', onVVResize);
  }

  // ── Kiro: drag-and-drop file upload ──
  let dragDepth = 0;
  const dropOverlay = document.getElementById('dropOverlay');
  document.addEventListener('dragenter', e => {
    if (!hasFiles(e.dataTransfer)) return;
    dragDepth++;
    dropOverlay.classList.add('show');
  });
  document.addEventListener('dragleave', e => {
    if (!hasFiles(e.dataTransfer)) return;
    dragDepth--;
    if (dragDepth <= 0) { dragDepth = 0; dropOverlay.classList.remove('show'); }
  });
  document.addEventListener('dragover', e => { if (hasFiles(e.dataTransfer)) e.preventDefault(); });
  document.addEventListener('drop', e => {
    e.preventDefault();
    dragDepth = 0;
    dropOverlay.classList.remove('show');
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    handleDropFiles(e.dataTransfer.files);
  });

  // ── Kiro: paste image/file support ──
  $('input').addEventListener('paste', e => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === 'file') files.push(item.getAsFile());
    }
    if (files.length) { e.preventDefault(); handleDropFiles(files); }
  });

  function hasFiles(dt) {
    if (!dt || !dt.types) return false;
    return Array.from(dt.types).some(t => t === 'Files');
  }

  async function handleDropFiles(files) {
    const input = $('input');
    const placeholder = input.placeholder;
    input.placeholder = '上传中…';
    let inserted = [];
    for (const f of files) {
      try {
        const form = new FormData();
        form.append('file', f);
        const r = await fetch('/api/upload?key=' + KEY, { method: 'POST', body: form });
        const j = await r.json();
        if (j.ok && j.url) {
          const link = j.type?.startsWith('image/') ? '![' + (j.name || f.name) + '](' + j.url + ')' : '[' + (j.name || f.name) + '](' + j.url + ')';
          inserted.push(link);
        }
      } catch (e) { /* skip failed uploads */ }
    }
    if (inserted.length) {
      input.value = (input.value ? input.value + '\n' : '') + inserted.join('\n');
      fitTextarea();
      saveDraft();
    }
    input.placeholder = placeholder;
  }
});
