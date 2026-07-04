"""handlers —— Telegram update 路由：权限守卫、文本/文件/回调分发。"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from commands import fs
from commands import llm
from ui import keyboard

log = logging.getLogger("tgclaude")


def _owner_only(handler):
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != ctx.application.bot_data["owner_id"]:
            log.warning("denied uid=%s", uid)
            return
        from core.botapp import BotApp
        b: BotApp = ctx.application.bot_data["botapp"]
        chat = update.effective_chat
        text = (update.message.text or "") if (update.message and update.message.text) else ""
        # 转发群：只作单向文件/通知出口。owner 在里面只有 /forward 可用（改别名/移除），
        # 其余命令与普通对话一律静默忽略——不在转发群里跑会话或系统命令。
        if chat and b.is_forward_group(chat.id):
            if not text.startswith("/forward"):
                return
            return await handler(update, ctx)
        # 其它群消息：只接受「主控群」（且仅来自 owner，上面已校验）。
        # 陌生群一律拒绝——杜绝把 bot 拉进陌生群就能驱动它。
        # 例外：owner 在陌生群里发 /forward（用于把当前群登记为转发群）放行，
        # 否则 chicken-and-egg：没登记前 /forward here 自己也会被挡。
        if chat and chat.type in ("group", "supergroup"):
            allowed = b.forum and chat.id == b.group_id
            if not allowed:
                if not text.startswith("/forward"):
                    log.warning("denied chat=%s (not control/forward group)", chat.id)
                    return
        return await handler(update, ctx)
    return wrapped


def _b(ctx):
    return ctx.application.bot_data["botapp"]


# ── 命令注册表（底栏按钮 → 命令派发）─────────────────────────────────────────
_CMD_REGISTRY: dict = {}


def build_command_registry(session_commands: dict, sys_commands: dict,
                            relay_commands: dict | None = None,
                            service_commands: dict | None = None) -> dict:
    from commands import prompts
    reg = {}
    reg.update(session_commands)
    reg.update(sys_commands)
    reg.update(relay_commands or {})
    reg.update(service_commands or {})
    reg.update(prompts.PROMPT_COMMANDS)
    reg.update({n: fn for n, fn in llm.LLM_COMMANDS})
    return reg


def init_registry(session_commands: dict, sys_commands: dict,
                  relay_commands: dict | None = None,
                  service_commands: dict | None = None):
    global _CMD_REGISTRY
    _CMD_REGISTRY = build_command_registry(
        session_commands, sys_commands, relay_commands, service_commands)


async def _run_command(name: str, args: list[str], update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fn = _CMD_REGISTRY.get(name)
    if fn is None:
        return
    ctx.args = list(args)
    await fn(update, ctx)


# ── on_text / on_file / on_callback ──────────────────────────────────────────
@_owner_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await keyboard.handle_text(update, ctx, run_command=_run_command):
        return
    # 提示词「编辑中」：把这条文本存为提示词正文，而非新一轮对话
    from commands import prompts
    if await prompts.maybe_capture_prompt_text(update, ctx):
        return
    tid = update.message.message_thread_id
    # 若该会话正有进行中的 AskUserQuestion，把这条消息当作自定义答案，而非新一轮 prompt
    sess = _b(ctx).active_ask_for_thread(tid)
    if sess is not None:
        await sess.on_text(update.message.text)
        return
    await _b(ctx).run_turn(update.message.text, update.effective_chat.id, tid)


@_owner_only
async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await fs.handle_file(update, ctx)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if (q.from_user.id if q.from_user else None) != ctx.application.bot_data["owner_id"]:
        await q.answer("未授权")
        return
    data = q.data or ""
    if data.startswith("console:"):
        handler = ctx.application.bot_data.get("console_callback_handler")
        if handler is None:
            await q.answer("控制台回调未就绪")
            return
        await handler(q, ctx, data)
        return

    if data.startswith("perm:"):
        _, token, decision = data.split(":", 2)
        ok = _b(ctx).resolve_permission(token, decision)
        label = {"allow": "✅ 已允许", "deny": "⛔ 已拒绝", "always": "✅ 永久允许"}.get(decision, decision)
        # 过期/重复点击不能谎报"已允许"——审批 future 早已超时或被处理
        await q.answer(label if ok else "⌛ 该请求已失效（超时或已处理）")
        if ok:
            try:
                await q.edit_message_text(f"{q.message.text}\n\n→ {label}")
            except Exception:
                pass
        return

    if data.startswith("ls:"):
        await fs.handle_ls_callback(q, data)
        return

    if data.startswith("askq:"):
        # askq:<token>:<action>:<idx>
        try:
            _, token, action, idx = data.split(":", 3)
            ok = await _b(ctx).resolve_ask(token, action, int(idx))
        except Exception:
            ok = False
        await q.answer("已记录" if ok else "该提问已结束")
        return

    if data.startswith("q:"):
        # q:<token>:<steer|cancel> —— 排队消息的插入/取消
        try:
            _, token, action = data.split(":", 2)
            toast = await _b(ctx).resolve_queued(token, action)
        except Exception:
            toast = None
        await q.answer(toast or "该排队消息已失效")
        return

    if data.startswith("detail:bump:"):
        # detail:bump:<chat_id>:<thread_id|0> —— 私聊详情页"刷到对话末尾"
        await q.answer("已刷新到对话末尾")
        await _b(ctx).bump_detail(data)
        return

    if data.startswith("detail:expand:") or data.startswith("detail:collapse:"):
        # detail:expand|collapse:<chat_id>:<thread_id|0> —— 详情页底部"展开/收起"
        # 带上按钮所在消息 id：同一话题多张详情页时精确路由到被点的那张
        mid = q.message.message_id if q.message else None
        ok = await _b(ctx).toggle_detail(data, msg_id=mid)
        action = "已展开" if data.startswith("detail:expand:") else "已收起"
        await q.answer(action if ok else "该详情页已过期")
        return


    if await keyboard.handle_callback(q, ctx, run_command=_run_command, update=update):
        return


@_owner_only
async def on_unhandled_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fallback: unrecognized /commands → treat as prompt to current session.
    先处理 /cmd@botname 形式：PTB 的 CommandHandler 在群里对带 @他人bot 后缀的命令
    不匹配，会落到这里；若去掉 @suffix 后是已知命令，按命令分派，避免把 /stop 当对话发出去。"""
    text = (update.message.text or "") if update.message else ""
    if text.startswith("/"):
        head = text.split(maxsplit=1)[0]
        bare = head[1:].split("@", 1)[0]  # 去掉前导 / 和 @botname 后缀
        if bare in _CMD_REGISTRY:
            args = text.split()[1:]
            await _run_command(bare, args, update, ctx)
            return
    tid = update.message.message_thread_id
    await _b(ctx).run_turn(update.message.text, update.effective_chat.id, tid)
