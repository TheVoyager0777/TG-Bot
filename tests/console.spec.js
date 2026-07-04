const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const toml = require('toml');
const { expect, test } = require('@playwright/test');

const repoRoot = path.resolve(__dirname, '..');
const defaultConfig = path.join(repoRoot, 'modules/tg-claude-bot/config.toml');

function consoleKey() {
  const cfgPath = process.env.PHANTOM_BOT_CONFIG || defaultConfig;
  const cfg = toml.parse(fs.readFileSync(cfgPath, 'utf8'));
  const token = cfg.telegram && cfg.telegram.token;
  if (!token) throw new Error(`telegram.token not found in ${cfgPath}`);
  return crypto.createHmac('sha256', token).update('phantom-console-v1').digest('hex').slice(0, 32);
}

async function latestSeq(request, apiBase, key) {
  const res = await request.get(`${apiBase}/api/events?since=0&limit=1&wait=0&session=main`, {
    headers: { 'X-Console-Key': key },
  });
  await expect(res).toBeOK();
  return (await res.json()).seq || 0;
}

async function pollEvents(request, apiBase, key, since) {
  const res = await request.get(`${apiBase}/api/events?since=${since}&limit=100&wait=1&session=main`, {
    headers: { 'X-Console-Key': key },
    timeout: 35000,
  });
  await expect(res).toBeOK();
  return res.json();
}

test('typing indicator is limited to the latest text block in one assistant turn', async ({ page }) => {
  await page.goto('/?key=ui-smoke&api=http%3A%2F%2F127.0.0.1%3A8875', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.handle === 'function' && typeof window.resetAll === 'function');

  const state = await page.evaluate(async () => {
    try { if (window.abortCtl) window.abortCtl.abort(); } catch (e) {}
    const splash = document.getElementById('splash');
    if (splash) splash.remove();
    window.replaying = true;
    resetAll();

    const waitFrame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    handle({ type: 'turn_start', session: 'ui-smoke', ts: Date.now() / 1000 });
    handle({ type: 'text', session: 'ui-smoke', text: '重复文本' });
    await waitFrame();
    handle({ type: 'tool', session: 'ui-smoke', id: 't1', tool: 'Read', phase: 'running', input: { file_path: '/tmp/a.txt' } });
    handle({ type: 'text', session: 'ui-smoke', text: '重复文本' });
    await waitFrame();

    const blocks = Array.from(document.querySelectorAll('.turn .turn-body > .txt')).map(el => ({
      text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
      html: el.innerHTML,
      hasIndicator: !!el.querySelector('.typing-indicator'),
      isMarked: el.classList.contains('has-typing-indicator'),
      isStreaming: el.classList.contains('streaming'),
    }));
    const beforeEnd = {
      indicatorCount: document.querySelectorAll('.typing-indicator').length,
      markedCount: document.querySelectorAll('.txt.has-typing-indicator').length,
      blocks,
    };

    handle({ type: 'turn_end', session: 'ui-smoke', status: 'done' });
    return {
      beforeEnd,
      afterEnd: {
        indicatorCount: document.querySelectorAll('.typing-indicator').length,
        markedCount: document.querySelectorAll('.txt.has-typing-indicator').length,
        streamingCount: document.querySelectorAll('.txt.streaming').length,
      },
    };
  });

  expect(state.beforeEnd.indicatorCount).toBe(1);
  expect(state.beforeEnd.markedCount).toBe(1);
  expect(state.beforeEnd.blocks).toHaveLength(2);
  expect(state.beforeEnd.blocks[0].hasIndicator).toBeFalsy();
  expect(state.beforeEnd.blocks[0].isStreaming).toBeFalsy();
  expect(state.beforeEnd.blocks[1].hasIndicator).toBeTruthy();
  expect(state.beforeEnd.blocks[1].isMarked).toBeTruthy();
  expect(state.beforeEnd.blocks[1].isStreaming).toBeTruthy();
  expect(state.beforeEnd.blocks[1].html).toContain('重复文本');
  expect(state.afterEnd.indicatorCount).toBe(0);
  expect(state.afterEnd.markedCount).toBe(0);
  expect(state.afterEnd.streamingCount).toBe(0);
});

test('console streams assistant text and keeps real process events behind drawer', async ({ page, request, baseURL }) => {
  const key = consoleKey();
  const apiBase = process.env.PHANTOM_CONSOLE_API || 'http://127.0.0.1:8875';
  const marker = `PW_STREAM_${Date.now()}`;

  const sw = await request.get(`${baseURL}/sw.js`);
  await expect(sw).toBeOK();
  expect(await sw.text()).toContain("phantom-shell-v13");
  expect((sw.headers()['cache-control'] || '').toLowerCase()).toContain('no-store');

  await page.goto(`/?key=${encodeURIComponent(key)}&api=${encodeURIComponent(apiBase)}`);
  await expect(page.locator('#feed')).toBeVisible();

  let since = await latestSeq(request, apiBase, key);
  const send = await request.post(`${apiBase}/api/send`, {
    headers: { 'X-Console-Key': key },
    data: { session: 'main', text: `请只输出：${marker}` },
  });
  await expect(send).toBeOK();

  let sawTextBeforeEnd = false;
  let sawProcessEvent = false;
  for (let i = 0; i < 12; i += 1) {
    const batch = await pollEvents(request, apiBase, key, since);
    since = batch.seq || since;
    const events = batch.events || [];
    const textIndex = events.findIndex(ev => ev.type === 'text' && String(ev.text || ''));
    const endIndex = events.findIndex(ev => ev.type === 'turn_end');
    if (events.some(ev => ['thinking', 'tool', 'result', 'note'].includes(ev.type))) {
      sawProcessEvent = true;
    }
    if (textIndex >= 0 && (endIndex < 0 || textIndex < endIndex)) {
      sawTextBeforeEnd = true;
    }
    if (endIndex >= 0) break;
  }
  expect(sawTextBeforeEnd).toBeTruthy();

  const turn = page.locator('.turn').filter({ hasText: marker }).last();
  await expect(turn).toBeVisible({ timeout: 90000 });
  await expect(turn).not.toContainText('CLI 会话被占用');
  await expect(turn.locator(':scope > .turn-meta')).toHaveCount(1);
  await expect(turn.locator(':scope > .turn-body')).toHaveCount(1);

  const drawer = turn.locator('.process-drawer');
  if (sawProcessEvent) {
    await expect(drawer).toHaveCount(1);
    await expect(drawer.locator('.process-toggle')).toContainText('展开过程');
    await expect(turn.locator(':scope > .turn-body > .think-group, :scope > .turn-body > .cot, :scope > .turn-body > .stats, :scope > .turn-body > .note')).toHaveCount(0);
    const processItems = drawer.locator('.process-panel > .think-group, .process-panel > .cot, .process-panel > .stats, .process-panel > .note');
    expect(await processItems.count()).toBeGreaterThan(0);
    const thinkingBlocks = drawer.locator('.think-block');
    if (await thinkingBlocks.count()) {
      await expect(thinkingBlocks).toHaveCount(1);
    }
  }
});
