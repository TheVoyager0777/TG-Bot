/* askq.js — AskUserQuestion 模态对话框 */

let askState = null; // { token, questions, qi, answers, selected, session }
const askPending = {}; // token -> {questions, session}：尚未结案的提问（含历史回放登记的）

// 首屏历史回放完成后补弹一次仍未结案的提问（取最近一条）。
// 用于「页面打开时已经有一个挂起的 AskUserQuestion」的场景。
function showPendingAsk() {
  // 只补弹后端「仍在等待回答」的提问（activeAsks 来自 /api/state）；历史里
  // 未结案的旧提问（进程早重启、future 已超时）不再自动弹窗骚扰用户。
  const toks = Object.keys(askPending).filter(t => activeAsks.has(t));
  if (!toks.length) return;
  const t = toks[toks.length - 1];
  showAskQuestion(t, askPending[t].questions, askPending[t].session);
}

function showAskQuestion(token, questions, sn) {
  if (!questions || !questions.length) return;
  askState = { token, questions, qi: 0, answers: {}, selected: new Set(), session: sn || 'main' };
  renderAskQuestion();
  $('askq-overlay').classList.remove('hidden');
  if (typeof updatePendingBar === 'function') updatePendingBar();
}

function hideAskQuestion(token) {
  if (askState && askState.token === token) {
    askState = null;
    $('askq-overlay').classList.add('hidden');
    if (typeof updatePendingBar === 'function') updatePendingBar();
  }
}

function renderAskQuestion() {
  const s = askState;
  if (!s) return;
  const ov = $('askq-overlay');
  const q = s.questions[s.qi];
  if (!q) return;
  // 兼容两套提问 schema：Claude SDK AskUserQuestion(multiSelect/header/options[].label+description)
  // 与 Cursor 插件 MCP ask_question(allow_multiple/options[].id+label，无 header/description)。
  const multi = !!(q.multiSelect || q.allow_multiple);
  const n = s.questions.length;
  // Metro 进度条：已答(on)/当前(cur)/未答 分段
  const prog = Array.from({ length: n }, (_, i) =>
    '<i class="' + (i < s.qi ? 'on' : (i === s.qi ? 'cur' : '')) + '"></i>').join('');

  ov.innerHTML = '<div id="askq-card">' +
    '<h3>❓ ' + esc(s.session) + ' 提问</h3>' +
    (n > 1 ? '<div class="qprog">' + prog + '</div>' : '') +
    '<div class="qheader">' + (s.qi + 1) + '/' + n + ' · ' + esc(q.header || q.questionHeader || '提问') + '</div>' +
    '<div class="qbody">' + esc(q.question || q.prompt || '') + '</div>' +
    '<div class="qopts">' + (q.options || []).map((o, i) =>
      '<div class="qopt' + (s.selected.has(i) ? ' sel' : '') + '" data-idx="' + i + '">' +
      esc(o.label || o.id || ('选项' + (i + 1))) +
      (o.description ? ' <small style="color:var(--text3)">— ' + esc(o.description) + '</small>' : '') +
      '</div>'
    ).join('') + '</div>' +
    '<div class="qact">' +
    (multi ? '<button class="primary" id="askq-submit">提交多选</button>' :
      '<button class="skip" id="askq-skip">跳过</button>') +
    '<button class="skip" id="askq-cancel">取消全部</button>' +
    '</div>' +
    (multi ? '<div class="askq-hint">多选：点选项切换勾选，选完点提交</div>' : '') +
    '<textarea id="askq-custom" class="askq-custom" placeholder="或输入自定义答案..." rows="2"></textarea>' +
    '<button id="askq-custom-send" class="askq-custom-send">发送自定义答案</button>' +
    '</div>';

  // 选项点击
  ov.querySelectorAll('.qopt').forEach(opt => {
    opt.addEventListener('click', () => {
      const idx = parseInt(opt.dataset.idx);
      if (multi) {
        if (s.selected.has(idx)) s.selected.delete(idx);
        else s.selected.add(idx);
        renderAskQuestion();
      } else {
        // 单选：立即记录并前进
        const o = (q.options || [])[idx] || {};
        recordAnswer(q.question, o.label || o.id || '');
        advanceOrFinish();
      }
    });
  });

  // 提交多选
  const submitBtn = ov.querySelector('#askq-submit');
  if (submitBtn) {
    submitBtn.addEventListener('click', () => {
      const labels = Array.from(s.selected).map(i => {
        const o = (q.options || [])[i] || {};
        return o.label || o.id || '';
      }).join(', ');
      recordAnswer(q.question, labels);
      advanceOrFinish();
    });
  }

  // 跳过
  const skipBtn = ov.querySelector('#askq-skip');
  if (skipBtn) skipBtn.addEventListener('click', advanceOrFinish);

  // 取消
  ov.querySelector('#askq-cancel').addEventListener('click', async () => {
    if (await submitAnswers(s.token, {})) {
      hideAskQuestion(s.token);
      toast('已取消');
    } else {
      toast('取消提交失败');
    }
  });

  // 自定义答案
  ov.querySelector('#askq-custom-send').addEventListener('click', () => {
    const custom = ov.querySelector('#askq-custom').value.trim();
    if (!custom) return;
    recordAnswer(q.question, custom);
    advanceOrFinish();
  });
}

function recordAnswer(question, answer) {
  if (askState) askState.answers[question] = answer;
}

function advanceOrFinish() {
  if (!askState) return;
  askState.selected = new Set();
  askState.qi++;
  if (askState.qi >= askState.questions.length) {
    const token = askState.token;
    const answers = { ...askState.answers };
    submitAnswers(token, answers).then(ok => {
      if (ok) {
        hideAskQuestion(token);
        toast('已提交回答');
      } else {
        toast('回答提交失败');
      }
    });
  } else {
    renderAskQuestion();
  }
}

async function submitAnswers(token, answers) {
  try {
    const resp = await fetch(apiUrl('/api/ask'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Console-Key': KEY },
      body: JSON.stringify({ token: token, answers: answers })
    });
    return resp.ok;
  } catch (e) {
    return false;
  }
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
  $('askq-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) {
      // 不自动关闭 — 用户必须明确操作
    }
  });
});
