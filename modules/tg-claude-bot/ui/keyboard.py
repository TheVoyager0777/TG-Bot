"""keyboard —— 底部菜单，双渲染：私聊用 ReplyKeyboard，论坛用 inline。

为什么两套渲染共用一份布局：
  · 私聊(DM)：ReplyKeyboard 能焊在输入框底部、常驻，体验最接近参考 bot；
    但它在超级群/论坛里桌面端常唤不出（Telegram Desktop 的固有行为）。
  · 论坛(群)：改用 inline 键盘（挂在 /menu 消息上的按钮），桌面/手机、
    群/私聊表现完全一致，100% 可点，且能精确发到当前话题线程。

单一事实源是 PANELS：每个面板是若干行按钮，按钮 = (label, action)。
action 形如 "nav:<panel>" 切面板，或 "cmd:<name>[ <arg>...]" 跑命令。
两种渲染都从同一份 PANELS 生成，所以布局只维护一处，绝不分叉。
"""
from __future__ import annotations

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                       ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# 端点动态按钮的 action 前缀（后接 provider 名）
PROV_ACT = "cmd:providers"

# ── 单一事实源：面板 = (标题, [行]), 行 = [(label, action)] ────────────────
# action: "nav:<panel_id>" 切面板 | "cmd:<name> [args...]" 跑命令
PANELS: dict[str, tuple[str, list[list[tuple[str, str]]]]] = {
    "root": ("⌨️ 主菜单", [
        [("🤖 LLM", "nav:llm"), ("🛠 系统", "nav:sys")],
        [("⏹ 停止", "cmd:stop"), ("🧮 上下文", "cmd:context")],
        [("👷 Workers", "cmd:workers"), ("📊 状态", "cmd:status")],
    ]),
    "llm": ("🤖 LLM / Agent —— 围绕 Claude 对话", [
        [("🎛️ 权限模式", "nav:mode"), ("⚡ Fast 加速", "nav:fast")],
        [("🧠 模型", "nav:model"), ("🌐 端点", "nav:providers")],
        [("📝 提示词池", "nav:workerhub"), ("🧮 上下文", "cmd:context")],
        [("🗜 压缩历史", "cmd:compact"), ("🏥 测试端点", "cmd:testllm")],
        [("👷 Workers", "cmd:workers"), ("⏹ 停止", "cmd:stop")],
        [("↩️ 主对话", "cmd:main"), ("⬅️ 返回", "nav:root")],
    ]),
    "sys": ("🛠 系统 / Bot —— 主机 · 构建 · 工具", [
        [("📊 状态", "cmd:status"), ("📈 报告", "cmd:report")],
        [("🔨 构建", "cmd:build"), ("💻 系统", "cmd:sys")],
        [("💾 磁盘", "cmd:disk"), ("🔝 进程", "cmd:top")],
        [("📱 设备", "cmd:devices"), ("📁 文件", "cmd:ls")],
        [("🧠 共享记忆", "cmd:sharedmem"), ("📤 转发", "nav:relay")],
        [("📱 App", "cmd:app"), ("🖥 控制台", "cmd:console"), ("🔧 WebCTL", "cmd:webctl")],
        [("⬅️ 返回", "nav:root")],
    ]),
    "relay": ("📤 转发 / 发布 —— 对外群只收文件·通知，全由你控制", [
        [("📋 转发群列表", "cmd:forward list"), ("📜 转发用法", "cmd:forward")],
        [("📣 发通知用法", "cmd:notify"), ("📎 发文件用法", "cmd:sendfile")],
        [("📌 置顶用法", "cmd:pin"), ("🔓 取消置顶用法", "cmd:unpin")],
        [("⬅️ 系统菜单", "nav:sys")],
        [("⬅️ 主菜单", "nav:root")],
    ]),
    "mode": ("🎛️ 权限模式 —— 切当前会话 + 新 worker 默认", [
        [("🟢 default", "cmd:mode default"), ("🟡 acceptEdits", "cmd:mode acceptEdits")],
        [("🔴 bypass 全自动", "cmd:mode bypassPermissions"), ("📋 plan", "cmd:mode plan")],
        [("⬅️ LLM 菜单", "nav:llm")],
    ]),
    "fast": ("⚡ Fast 加速输出 —— 重连生效，持久化", [
        [("⚡ 开启", "cmd:fast on"), ("🐢 关闭", "cmd:fast off")],
        [("🔄 切换", "cmd:fast toggle")],
        [("⬅️ LLM 菜单", "nav:llm")],
    ]),
    # providers 面板动态生成（见 _providers_panel），这里占位用于 nav 校验
    "providers": ("🌐 LLM 端点", []),
    # model 面板动态生成（见 _model_panel），占位用于 nav 校验
    "model": ("🧠 模型档位", []),
    # 提示词池：worker hub + 各 owner 的池，均动态生成（见 _workerhub_panel/_prompts_panel）
    "workerhub": ("📝 提示词池", []),
    "prompts": ("📝 提示词池", []),
}

# 模型档位按钮（与 commands_llm.MODEL_TIERS 对应）。值=传给 /model 的参数。
_MODEL_TIERS = [("🟣 Opus", "opus"), ("🔵 Sonnet", "sonnet"),
                ("🟢 Haiku", "haiku"), ("⚪ 默认", "default")]


def _providers_panel(b, worker_name=None) -> tuple[str, list[list[tuple[str, str]]]]:
    """端点面板按当前 router 动态生成：每个 provider 一个切换按钮。

    上下文感知：在某 worker 的话题里打开时，✅ 标记的是「该 worker 当前用的
    provider」、点击切的也是该 worker（cmd:providers 会按所在话题定位到 set_worker_provider）；
    在 General/主对话/私聊主会话里打开时，作用于全局活跃端点。"""
    rows: list[list[tuple[str, str]]] = []
    r = getattr(b.mgr, "router", None)
    has = r is not None and getattr(r, "providers", None)
    # 判断本菜单作用于哪个会话：worker 话题 → 该 worker；否则 → 全局
    w = b.mgr.get(worker_name) if worker_name else None
    is_worker = w is not None and not w.is_orchestrator
    if has:
        # worker 没单独设 provider 时，实际跑在全局活跃端点上，故回退显示 active
        current = (w.provider or r.active) if is_worker else r.active
    else:
        current = None
    if has:
        row: list[tuple[str, str]] = []
        for n in r.providers.keys():
            label = f"✅ {n}" if n == current else f"🔌 {n}"
            row.append((label, f"cmd:providers {n}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
    rows.append([("📜 端点列表", "cmd:providers"), ("🛠 增删配置", "cmd:provider")])
    rows.append([("⬅️ LLM 菜单", "nav:llm")])
    if not has:
        title = "🌐 未启用 provider 路由（无 providers.toml）"
    elif is_worker:
        title = (f"🌐 端点 · 作用于 worker «{worker_name}»\n"
                 f"点一个切*该 worker* 的 provider（重连，上下文保留）\n当前: {current}")
    else:
        title = (f"🌐 端点 · 作用于全局（主对话）\n"
                 f"点一个切为全局活跃（主对话重连生效）\n当前: {current}")
    return title, rows


def _model_panel(b, worker_name=None) -> tuple[str, list[list[tuple[str, str]]]]:
    """模型档位面板：上下文感知。先列档位别名(opus/sonnet/haiku/default)，
    再追加从当前 provider /v1/models 动态发现的具体模型名（缓存有效期 5min，
    过期/缺失时由调用方触发后台 refresh）。✅ 标当前会话用的实际值。"""
    name = worker_name or b.mgr.ORCH
    w = b.mgr.get(name)
    current = (w.model if w else None) or "default"

    rows: list[list[tuple[str, str]]] = []

    # 第一行：档位别名（永远显示，回退用）
    row: list[tuple[str, str]] = []
    for label, tier in _MODEL_TIERS:
        mark = "✅ " if tier == current else ""
        row.append((mark + label, f"cmd:model {tier}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)

    # 第二段：当前活跃 provider 的具体模型清单（动态发现 + 静态回退）
    extra_title = ""
    r = getattr(b.mgr, "router", None)
    cache = getattr(b, "model_cache", None)
    prov_name = "(未配 provider)"
    discovered: list[str] = []
    fresh = False
    if r is not None and r.providers:
        # 选作用域：worker 自己的 provider，没设就用全局活跃
        prov_name = (w.provider if (w and not w.is_orchestrator) else None) or r.active or ""
        prov = r.get(prov_name) if prov_name else None
        if prov is not None and cache is not None:
            entry = cache.get(prov_name)
            if entry:
                fresh = entry.get("fresh", False)
                if entry.get("models"):
                    discovered = list(entry["models"])
            if not discovered:
                discovered = cache.fallback_models(prov)
            # 没 fresh 缓存：触发后台 refresh，本次菜单先用 fallback
            if cache.should_refresh(prov_name):
                import asyncio as _aio
                async def _bg(): await cache.refresh(prov)
                try: _aio.create_task(_bg())
                except RuntimeError: pass
        extra_title = f" · provider={prov_name}" + (" (实时)" if fresh else " (回退)")

    if discovered:
        rows.append([("—— 具体模型 ——", "nav:model")])  # 占位分隔，点击即重渲
        cur_actual = (w.model if w else None) or ""
        row = []
        # 限制最多 8 个具体模型，避免按钮泛滥
        for m in discovered[:8]:
            mark = "✅ " if m == cur_actual else "  "
            label = m if len(m) <= 24 else m[:23] + "…"
            row.append((mark + label, f"cmd:model {m}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)

    rows.append([("🔄 刷新模型清单", "cmd:model_refresh")])
    rows.append([("⬅️ LLM 菜单", "nav:llm")])

    who = "主对话" if (w is None or w.is_orchestrator) else f"worker «{name}»"
    title = (f"🧠 模型 · 作用于{who}{extra_title}\n"
             f"档位别名（左）or 具体模型名（下）— 点即切，SDK set_model 即时生效\n"
             f"当前: {current}")
    return title, rows


def _workerhub_panel(b) -> tuple[str, list[list[tuple[str, str]]]]:
    """提示词池入口：动态列出存活会话（主对话 + 每个 worker）+ 共享池。
    点一个进它的提示词池。worker 标注忙/闲。"""
    rows: list[list[tuple[str, str]]] = []
    rows.append([("🗣 主对话(main)", "nav:prompts:main"), ("🌐 共享池", "nav:prompts:*")])
    ws = b.mgr.list_workers()
    if ws:
        row: list[tuple[str, str]] = []
        for w in ws:
            st = "🟡" if w["busy"] else "🟢"
            row.append((f"{st} {w['name']}", f"nav:prompts:{w['name']}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
    rows.append([("⬅️ LLM 菜单", "nav:llm")])
    n = len(ws)
    title = (f"📝 提示词池 · 存活会话 {n} 个 worker + 主对话\n"
             f"点一个会话查看/调用它的提示词；🌐 共享池对所有会话可见。")
    if not ws:
        title += "\n（还没有 worker；可先跟主对话说\"建个 worker\"）"
    return title, rows


def _prompts_panel(b, owner: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """某 owner 的提示词池：每条一行（调用 / 编辑 / 删除），底部新增。"""
    p = getattr(b.mgr, "prompts", None)
    rows: list[list[tuple[str, str]]] = []
    items = p.list(owner, include_shared=True) if p else []
    for i, e in enumerate(items):
        tag = "🌐" if e.get("scope") == "shared" else "▶️"
        label = e["name"]
        if len(label) > 16:
            label = label[:16] + "…"
        rows.append([
            (f"{tag} {label}", f"cmd:promptrun {owner} {i}"),
            ("✏️", f"cmd:promptedit {owner} {i}"),
            ("🗑", f"cmd:promptdel {owner} {i}"),
        ])
    rows.append([("➕ 新增提示词", f"cmd:promptnew {owner}")])
    rows.append([("⬅️ 会话列表", "nav:workerhub"), ("⬅️ LLM 菜单", "nav:llm")])
    if p is None:
        title = "📝 未启用提示词池"
    else:
        title = (f"📝 «{owner}» 提示词池（{len(items)} 条）\n"
                 f"▶️ 调用 = 把正文发给该会话 · ✏️ 编辑 · 🗑 删除 · 🌐=共享池条目")
    return title, rows


def _resolve(panel_id: str, b, worker_name=None) -> tuple[str, list[list[tuple[str, str]]]]:
    if panel_id == "providers":
        return _providers_panel(b, worker_name)
    if panel_id == "model":
        return _model_panel(b, worker_name)
    if panel_id == "workerhub":
        return _workerhub_panel(b)
    if panel_id.startswith("prompts:"):
        return _prompts_panel(b, panel_id.split(":", 1)[1])
    if panel_id == "prompts":
        # 无 owner 后缀：按当前话题对应会话
        return _prompts_panel(b, worker_name or b.mgr.ORCH)
    return PANELS.get(panel_id, PANELS["root"])


# ── 私聊：ReplyKeyboard 渲染（标签即文本，点击发文本，靠 handle_text 拦截）──
def _reply_markup(rows) -> ReplyKeyboardMarkup:
    labels = [[lbl for lbl, _ in row] for row in rows]
    return ReplyKeyboardMarkup(
        labels, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="输入消息 = 发给当前会话…")


def reply_root() -> ReplyKeyboardMarkup:
    return _reply_markup(PANELS["root"][1])


# label -> action 反查表（私聊 ReplyKeyboard 点击回来是文本，要据此找 action）
def _label_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for _pid, (_t, rows) in PANELS.items():
        for row in rows:
            for lbl, act in row:
                idx[lbl] = act
    return idx


_LABEL_TO_ACTION = _label_index()


# ── 论坛：inline 渲染（callback_data 带 action，点击走 on_callback）─────────
def _inline_markup(rows) -> InlineKeyboardMarkup:
    btns = [[InlineKeyboardButton(lbl, callback_data="kb:" + act) for lbl, act in row]
            for row in rows]
    return InlineKeyboardMarkup(btns)


def inline_panel(panel_id: str, b, worker_name=None) -> tuple[str, InlineKeyboardMarkup]:
    title, rows = _resolve(panel_id, b, worker_name)
    return title, _inline_markup(rows)


# ── 动作执行：把 action 字符串变成「切面板」或「跑命令」──────────────────
async def _dispatch_action(action: str, *, b, run_command, update, ctx,
                           inline_q=None) -> bool:
    """执行一个 action。inline_q 非空表示来自 inline 按钮（要 edit 原消息切面板）。"""
    if action.startswith("nav:"):
        panel_id = action[4:]
        # 端点/模型面板要按「菜单所在话题对应的 worker」渲染（✅ 标记该 worker 的选择）
        worker_name = None
        if panel_id in ("providers", "model"):
            try:
                worker_name = b.target_name_for(update)
            except Exception:
                worker_name = None
        # 动态面板（按钮含运行期文本，不在静态 label 索引里）：DM 下也用 inline 渲染，
        # 否则 ReplyKeyboard 点回来的纯文本无法反查 action，会被当成普通对话发出去。
        is_dynamic = (panel_id in ("providers", "model", "workerhub")
                      or panel_id.startswith("prompts"))
        if inline_q is not None:
            title, kb = inline_panel(panel_id, b, worker_name)
            try:
                await inline_q.edit_message_text(title, reply_markup=kb)
            except Exception:
                pass
        elif is_dynamic:
            title, kb = inline_panel(panel_id, b, worker_name)
            tid = (update.message.message_thread_id
                   if getattr(update, "message", None) else None)
            kw = {"reply_markup": kb}
            if tid is not None:
                kw["message_thread_id"] = tid
            await update.message.reply_text(title, **kw)
        else:
            title, rows = _resolve(panel_id, b, worker_name)
            await update.message.reply_text(title, reply_markup=_reply_markup(rows))
        return True
    if action.startswith("cmd:"):
        rest = action[4:].split(" ")
        name, args = rest[0], rest[1:]
        await run_command(name, args, update, ctx)
        return True
    return False


# ── 私聊入口：on_text 调它，命中底栏按钮返回 True ────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, run_command) -> bool:
    text = (update.message.text or "").strip()
    if not text:
        return False
    action = _LABEL_TO_ACTION.get(text)
    if action is None:
        # 端点动态按钮（✅/🔌 name）不在静态索引里，单独识别
        if text.startswith("🔌 ") or text.startswith("✅ "):
            name = text.split(" ", 1)[1].strip()
            if name:
                await run_command("providers", [name], update, ctx)
                return True
        return False
    b = ctx.application.bot_data["botapp"]
    return await _dispatch_action(action, b=b, run_command=run_command,
                                  update=update, ctx=ctx)


# ── 论坛入口：on_callback 调它，处理 "kb:" 前缀 ─────────────────────────
class _CallbackUpdateProxy:
    """让命令处理器在 inline 回调里照常工作。

    cmd_* 大量用 `update.message.reply_text(...)` 和 `update.message.message_thread_id`，
    但 CallbackQuery 的 update.message 是 None。这里把 `.message` 指向按钮所挂的
    菜单消息（q.message，它的 thread 正是当前话题/worker），其余属性透传给真 update。
    """
    __slots__ = ("_u", "message")

    def __init__(self, real_update, menu_message):
        self._u = real_update
        self.message = menu_message

    def __getattr__(self, name):
        return getattr(self._u, name)


async def handle_callback(q, ctx: ContextTypes.DEFAULT_TYPE, *, run_command, update) -> bool:
    data = q.data or ""
    if not data.startswith("kb:"):
        return False
    await q.answer()
    action = data[3:]
    b = ctx.application.bot_data["botapp"]
    proxy = _CallbackUpdateProxy(update, q.message)
    await _dispatch_action(action, b=b, run_command=run_command,
                           update=proxy, ctx=ctx, inline_q=q)
    return True
