"""commands_session —— 会话管理命令：/start /main /menu /attach /workers /sharedmem /ls。"""
from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.manager import SessionManager
from commands import fs
from ui import keyboard

log = logging.getLogger("tgclaude")


def _b(ctx):
    return ctx.application.bot_data["botapp"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    from infra import version
    tip = ("💡 群里发 /menu 唤出菜单：🤖 LLM 与 🛠 系统 两类一键直达。"
           if b.forum else
           "💡 底部「菜单」键盘已就绪：🤖 LLM 与 🛠 系统 两类一键直达。")
    text = (
        "🤖 *Claude agent team 已就绪*\n"
        f"`{version.build_str()}`\n"
        f"cwd: `{b.cfg['claude'].get('cwd')}` · mode: `{b.mgr.default_mode}`"
        f" · fast: `{'on' if b.mgr.fast_mode else 'off'}`\n\n"
        "默认你在跟*主对话*聊，它能创建并调度多个 worker 帮你干活。\n\n"
        "*对话*\n"
        "  直接发文本 = 发给当前会话\n"
        "  /menu — 唤出菜单 · /main — 切回主对话\n"
        "  /attach <name> — 直连某个 worker\n"
        "  /workers — 列出所有 worker\n"
        "  /stop — 中断当前会话这一轮\n\n"
        "*LLM*\n"
        "  /mode <default|acceptEdits|bypassPermissions|plan>\n"
        "  /fast <on|off|toggle> — 加速输出\n"
        "  /context — 上下文窗口用量统计\n"
        "  /compact — 压缩对话历史，省 token\n"
        "  /providers — 端点列表/切换 · /provider — 增删\n\n"
        "*监控*\n"
        "  /status /report /sys /disk /top /devices\n"
        "  /build [args] — 后台跑 ph build\n"
        "  /svc status|start|stop|restart [console|llm|all] — 子模块服务\n"
        "  /svcstatus — 查看 bot/console/llm 状态与版本\n\n" + tip)
    kw = {"parse_mode": ParseMode.MARKDOWN}
    if not b.forum:
        kw["reply_markup"] = keyboard.reply_root()
    await update.message.reply_text(text, **kw)


async def cmd_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _b(ctx).attached = SessionManager.ORCH
    await update.message.reply_text("↩️ 已切回主对话")


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if b.forum:
        title, kb = keyboard.inline_panel("root", b)
        tid = update.message.message_thread_id if update.message else None
        await ctx.bot.send_message(update.effective_chat.id, title,
                                   reply_markup=kb, message_thread_id=tid)
    else:
        await update.message.reply_text("⌨️ 主菜单", reply_markup=keyboard.reply_root())


async def cmd_attach(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if not ctx.args:
        await update.message.reply_text("用法: /attach <worker名>")
        return
    name = ctx.args[0]
    if b.mgr.get(name) is None or name == SessionManager.ORCH:
        names = ", ".join(w["name"] for w in b.mgr.list_workers()) or "(无)"
        await update.message.reply_text(f"没有 worker «{name}»。现有: {names}")
        return
    b.attached = name
    await update.message.reply_text(f"🔗 已直连 worker «{name}»，发消息直接给它。/main 回主对话")


async def cmd_workers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    ws = b.mgr.list_workers()
    cur = b.attached
    lines = [f"当前直连: *{cur}*", ""]
    if not ws:
        lines.append("还没有 worker（跟主对话说\"建个 worker 干活\"它会创建）")
    for w in ws:
        st = "🟡忙" if w["busy"] else "🟢闲"
        mark = "👉 " if w["name"] == cur else "   "
        lines.append(f"{mark}*{w['name']}* {st} · {w['turns']}轮 · idle {w['idle_s']}s")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _fmt_age_zh(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)}秒前"
    if sec < 3600:
        return f"{int(sec/60)}分钟前"
    if sec < 86400:
        return f"{int(sec/3600)}小时前"
    return f"{int(sec/86400)}天前"


async def cmd_sharedmem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    mem = b.mgr.shared_mem
    if mem is None:
        await update.message.reply_text("未启用共享记忆")
        return
    args = ctx.args or []
    now = time.time()

    def fmt(recs):
        out = []
        for r in recs:
            out.append(f"`{r.path}`\n  ← {r.worker} ({r.op}, {_fmt_age_zh(now - r.ts)})")
        return "\n".join(out)

    if args:
        q = args[0]
        recs = mem.who_touched(q) or mem.search(q, limit=40)
        if not recs:
            await update.message.reply_text(f"`{q}`：无改动记录", parse_mode=ParseMode.MARKDOWN)
            return
        await update.message.reply_text(
            f"🗂 *{q}* 改动（{len(recs)} 条）\n" + fmt(recs), parse_mode=ParseMode.MARKDOWN)
        return

    s = mem.summary()
    by = "、".join(f"{w}:{n}" for w, n in s["by_worker"][:6]) or "(无)"
    recs = mem.recent(limit=15)
    head = (f"🧠 *共享记忆*\n累计改动 {s['total_edits']} 次 / {s['distinct_files']} 个文件\n"
            f"按 worker: {by}\n\n最近 {len(recs)} 条：\n")
    await update.message.reply_text(head + (fmt(recs) or "(暂无)"),
                                    parse_mode=ParseMode.MARKDOWN)


cmd_ls = fs.cmd_ls


async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """显示 bot 版本/构建时间/fingerprint/运行时长。重启后比对就能确认是否拉到新代码。"""
    from infra import version
    await update.message.reply_text(version.info_block(), parse_mode=ParseMode.MARKDOWN)


SESSION_COMMANDS = {
    "start": cmd_start, "menu": cmd_menu, "main": cmd_main,
    "attach": cmd_attach, "workers": cmd_workers,
    "sharedmem": cmd_sharedmem, "ls": cmd_ls, "version": cmd_version,
}
