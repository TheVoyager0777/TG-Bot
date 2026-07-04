/* events.js — 事件分发：handle() 根据 type 路由到 render 函数 */

function handle(ev) {
  const name = ev.session || 'main';
  const s = st(name);
  s.name = name;

  switch (ev.type) {
    case 'user':
      renderUser(ev);
      break;

    case 'turn_start':
      // 如果上一个 turn 没有 body 且只有用户气泡，复用
      if (s.turn && !s.hasBody) {
        finalizeTurn(s, 'done');
      }
      s.startTs = ev.ts || Date.now();
      newTurn(s);
      if (nearBottom()) scrollBottom();
      break;

    case 'text':
      s.hasBody = true;
      renderText(s, ev);
      break;

    case 'subagent_text':
      s.hasBody = true;
      // 子代理输出 → 独立思考块，不与主文本混合
      renderThinking(s, { text: '[' + (ev.label || 'sub') + '] ' + (ev.text || '') });
      break;

    case 'tool':
      // TodoWrite 有两条产线：LiveMessage 旁路成独立 'todo' 事件（不发 tool 事件）；
      // 外部/原始产线发 tool 事件且带 input.todos。后者在此就地转待办，避免漏更新。
      if (ev.tool === 'TodoWrite' || (ev.input && ev.input.todos)) {
        if (ev.input && Array.isArray(ev.input.todos)) {
          todosBy[name] = ev.input.todos.map(t => ({
            text: t.content || t.activeForm || t.subject || t.description || '',
            status: (t.status || t.state || 'pending').toLowerCase(),
          }));
          renderTodos();
        }
        break;
      }
      s.hasBody = true;
      renderTool(s, ev);
      if (nearBottom()) scrollBottom();
      break;

    case 'thinking':
      s.hasBody = true;
      renderThinking(s, ev);
      break;

    case 'note':
      s.hasBody = true;
      renderNote(s, ev);
      break;

    case 'todo':
      todosBy[name] = ev.items || [];
      renderTodos();
      break;

    case 'perm':
      renderPerm(s, ev);
      break;

    case 'perm_done':
      renderPermDone(s, ev);
      break;

    case 'result':
      renderStats(s, ev);
      break;

    case 'turn_end':
      if (s.turn) {
        finalizeTurn(s, ev.status || 'done');
        const label = ev.status === 'interrupted' ? stText('interrupted') : stText('done');
        // Truncate stats for compact badge display — full stats visible on hover
        const shortStats = (ev.stats || '').replace(/[🪙⏱🔁]?\s*\d+[k]?[↑↓→←]\S*/g, '').slice(0, 30).trim();
        s.durEl.textContent = shortStats ? label + ' · ' + shortStats : label;
        s.durEl.title = ev.stats || label;
      }
      // 完成所有待定 codeview 渲染
      if (s.bodyEl) highlightUnder(s.bodyEl);
      s.turn = null;
      s.startTs = null;
      break;

    case 'ask_question':
      if (ev.phase === 'start') {
        // 记入待结案表。只有「实时」到达(booted 且非回放)才自动弹窗；任何历史
        // 回放(首屏 / 切会话 pickSession)期间 replaying=true，绝不弹——这正是
        // 「切会话又把旧提问弹出来」的根因。首屏挂起的提问由 showPendingAsk()
        // 按后端 activeAsks 补弹一次。
        askPending[ev.token] = { questions: ev.questions, session: name };
        if (booted && !replaying) showAskQuestion(ev.token, ev.questions, name);
        else if (typeof updatePendingBar === 'function') updatePendingBar();
      } else {
        // answered / cancelled / timeout：结案并关窗
        delete askPending[ev.token];
        hideAskQuestion(ev.token);
        if (typeof updatePendingBar === 'function') updatePendingBar();
      }
      break;

    case 'bg_task':
      // 后台任务状态更新
      updateBgTask(ev.task || ev);
      break;

    case 'bg_task_output':
      // 后台任务实时输出 → 渲染到对应回合
      if (ev.session && S[ev.session]?.turn) {
        renderText(S[ev.session], { text: '[' + (ev.label || 'task') + '] ' + (ev.text || '').slice(0, 500) });
      }
      break;

    case 'hot_reload':
      // 热更新：仅实时事件生效，回放历史时忽略
      if (!booted || replaying) break;
      if (ev.css_only) {
        document.querySelectorAll('link[rel="stylesheet"][href^="/css/"]').forEach(link => {
          const href = link.getAttribute('href').split('?')[0];
          link.setAttribute('href', href + '?_t=' + Date.now());
        });
        console.log('[HMR] CSS hot-swapped:', ev.files);
      } else {
        console.log('[HMR] full reload:', ev.files);
        location.reload();
      }
      break;
  }
}
