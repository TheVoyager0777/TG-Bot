"""commands_prompts —— 提示词池命令：列出/调用/新增/编辑/删除每会话的命名提示词。

设计：
- 每个会话（worker 名 / "main" / 共享池 "*"）有自己的命名提示词。共享池对所有会话可见。
- 菜单里「📝 提示词池」先列存活会话（worker hub），点进某会话看它的池；
  每条提示词可一键「调用」（=把正文当一轮 prompt 发给该会话）或「编辑/删除」。
- 新增/编辑走「捕获下一条文本」：点按钮后 bot 记下 pending 状态，主人下一条消息即正文。
  正文首行可写「名字: 」来命名；不写则沿用旧名或自动取首句。

callback 走索引而非名字（名字可能含空格/unicode，撑不进 callback_data）：
渲染面板时按当前列表顺序编号，点击时按 owner 重新取列表 + 索引定位。
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger("tgclaude")


def _b(ctx):
    return ctx.application.bot_data["botapp"]


def _pool(b):
    return getattr(b.mgr, "prompts", None)


def _owner_entries(b, owner: str) -> list[dict]:
    p = _pool(b)
    if p is None:
        return []
    return p.list(owner, include_shared=True)


def _resolve_idx(b, owner: str, idx: int) -> dict | None:
    items = _owner_entries(b, owner)
    if 0 <= idx < len(items):
        return items[idx]
    return None


# ── /promptrun <owner> <idx>：调用一条提示词（把正文发给该会话）────────────
async def cmd_promptrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if len(ctx.args) < 2:
        await update.message.reply_text("用法: /promptrun <owner> <idx>")
        return
    owner, idx = ctx.args[0], ctx.args[1]
    try:
        entry = _resolve_idx(b, owner, int(idx))
    except ValueError:
        entry = None
    if entry is None:
        await update.message.reply_text("该提示词已失效，请重开菜单")
        return
    text = entry["text"]
    # 把正文当作一轮 prompt 发给 owner 会话。owner=="main" → orchestrator。
    chat_id = update.effective_chat.id
    # 论坛模式下发到该 owner 的话题；否则发到当前 chat（DM）
    thread_id = None
    if b.forum:
        from core.manager import SessionManager
        if owner == SessionManager.ORCH or owner == "main":
            # 主对话不只活在 General：若命令/菜单就发自某个映射到主对话的话题
            # （File 话题、未注册话题），对话留在原话题，不甩去 General。
            # 桥接话题/已关闭话题除外（前者会把 prompt 灌进 Cursor 大脑）。
            cur_tid = update.message.message_thread_id if update.message else None
            if (cur_tid is not None
                    and cur_tid not in b.closed_threads
                    and not b.bridge.is_bridge_thread(cur_tid)
                    and b.worker_for_thread(cur_tid) == SessionManager.ORCH):
                thread_id = cur_tid
        else:
            thread_id = await b.ensure_topic(owner)
    await update.message.reply_text(
        f"▶️ 调用提示词「{entry['name']}」→ «{owner}»")
    await b.run_turn(text, chat_id, thread_id)


# ── /promptnew <owner> ：新增（捕获下一条文本为正文）─────────────────────
async def cmd_promptnew(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if not ctx.args:
        await update.message.reply_text("用法: /promptnew <owner>")
        return
    owner = ctx.args[0]
    key = (update.effective_chat.id, update.message.message_thread_id)
    b.pending_prompt[key] = {"owner": owner, "name": None}
    await update.message.reply_text(
        f"✏️ 新增提示词到 «{owner}»。\n"
        f"发一条消息作为正文。首行写「名字: 」可命名，否则自动取名。\n"
        f"发 /cancel 取消。", parse_mode=None)


# ── /promptedit <owner> <idx> ：编辑（捕获下一条文本覆盖正文）────────────
async def cmd_promptedit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if len(ctx.args) < 2:
        await update.message.reply_text("用法: /promptedit <owner> <idx>")
        return
    owner, idx = ctx.args[0], ctx.args[1]
    try:
        entry = _resolve_idx(b, owner, int(idx))
    except ValueError:
        entry = None
    if entry is None:
        await update.message.reply_text("该提示词已失效，请重开菜单")
        return
    # 共享条目在某 worker 名下编辑 → 落成该 worker 的覆盖项（own 覆盖 shared）
    key = (update.effective_chat.id, update.message.message_thread_id)
    b.pending_prompt[key] = {"owner": owner, "name": entry["name"]}
    await update.message.reply_text(
        f"✏️ 编辑「{entry['name']}」（{owner}）。当前正文：\n"
        f"———\n{entry['text'][:500]}\n———\n"
        f"发一条新消息覆盖正文。/cancel 取消。")


# ── /promptdel <owner> <idx> ：删除（仅删 owner 自己的，shared 不动）───────
async def cmd_promptdel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    p = _pool(b)
    if p is None or len(ctx.args) < 2:
        await update.message.reply_text("用法: /promptdel <owner> <idx>")
        return
    owner, idx = ctx.args[0], ctx.args[1]
    try:
        entry = _resolve_idx(b, owner, int(idx))
    except ValueError:
        entry = None
    if entry is None:
        await update.message.reply_text("该提示词已失效")
        return
    if entry.get("scope") == "shared" and owner != "*":
        await update.message.reply_text(
            f"「{entry['name']}」是共享池条目，不能从 «{owner}» 删除。"
            f"去共享池(*)删，或新建同名覆盖它。")
        return
    ok = p.delete(owner, entry["name"])
    await update.message.reply_text(
        f"🗑 已删除「{entry['name']}」" if ok else "删除失败")


# ── /cancel ：取消进行中的提示词编辑 ──────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    key = (update.effective_chat.id, update.message.message_thread_id)
    if b.pending_prompt.pop(key, None) is not None:
        await update.message.reply_text("已取消提示词编辑")
    else:
        await update.message.reply_text("当前没有进行中的编辑")


# ── /prompts [owner] ：文本列出某会话的提示词池 ──────────────────────────
async def cmd_prompts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    p = _pool(b)
    if p is None:
        await update.message.reply_text("未启用提示词池")
        return
    owner = ctx.args[0] if ctx.args else b.target_name_for(update)
    items = _owner_entries(b, owner)
    if not items:
        await update.message.reply_text(
            f"«{owner}» 暂无提示词。/promptnew {owner} 新增。")
        return
    lines = [f"📝 «{owner}» 提示词池（{len(items)} 条）：", ""]
    for i, e in enumerate(items):
        tag = "🌐" if e.get("scope") == "shared" else "•"
        preview = e["text"].strip().replace("\n", " ")
        if len(preview) > 50:
            preview = preview[:50] + "…"
        lines.append(f"{tag} [{i}] *{e['name']}* — {preview}")
    lines.append(f"\n/promptrun {owner} <idx> 调用 · /promptnew {owner} 新增")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── 捕获「编辑中」的下一条文本（由 handlers.on_text 在分派前调用）────────
async def maybe_capture_prompt_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """若当前 (chat,thread) 处于提示词编辑态，把这条文本存为提示词正文，返回 True。"""
    b = _b(ctx)
    key = (update.effective_chat.id, update.message.message_thread_id)
    st = b.pending_prompt.get(key)
    if st is None:
        return False
    p = _pool(b)
    if p is None:
        b.pending_prompt.pop(key, None)
        return False
    text = update.message.text or ""
    name = st.get("name")
    body = text
    # 新增且未定名：支持首行「名字: 正文…」或「名字:\n正文」
    if name is None:
        first, sep, rest = text.partition("\n")
        if ":" in first and len(first) <= 80:
            nm, _, inline = first.partition(":")
            nm = nm.strip()
            if nm:
                name = nm
                body = (inline.strip() + ("\n" + rest if rest else "")).strip() or rest.strip()
        if name is None:
            # 自动取名：首句前 24 字
            auto = text.strip().split("\n", 1)[0][:24] or "未命名"
            name = auto
    b.pending_prompt.pop(key, None)
    try:
        p.save(st["owner"], name, body or text)
    except ValueError as e:
        await update.message.reply_text(f"保存失败: {e}")
        return True
    await update.message.reply_text(
        f"✅ 已保存提示词「{name}」到 «{st['owner']}»。"
        f"\n用 /promptrun {st['owner']} <idx> 调用，或在菜单里点它。")
    return True


PROMPT_COMMANDS = {
    "prompts": cmd_prompts,
    "promptrun": cmd_promptrun,
    "promptnew": cmd_promptnew,
    "promptedit": cmd_promptedit,
    "promptdel": cmd_promptdel,
    "cancel": cmd_cancel,
}
