"""commands_relay —— 转发相关指令集，集中管理。

把「对外转发/发布」能力做成直达命令，无需经 LLM 转一手：
- /forward —— 管理文件转发群（here/add/list/rm）
- /notify  —— 发文本通知到主控或某转发群（可顺手置顶）
- /pin /unpin —— 置顶/取消置顶某条已发消息
- /sendfile —— 把服务器文件发到主控或某转发群

回执一律纯文本：别名/路径/群标题可能含 _ * ` 等会破坏 Markdown 实体的字符
（曾导致 send 抛 BadRequest），故只有静态用法说明用 Markdown。

目标解析复用 BotApp：不带 target=主控（你）；带别名/chat_id=对应转发群。
所有命令仍由 bot.py 用 _owner_only 包一层，且转发群里只 /forward 放行（见 handlers）。
"""
from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger("tgclaude")


def _b(ctx):
    return ctx.application.bot_data["botapp"]


# ── /forward：管理「文件转发群」────────────────────────────────────────────
_FORWARD_USAGE = (
    "📤 *文件转发群* —— 接入的其他群只接收文件/通知、不参与对话，全程只由你控制。\n\n"
    "在目标群里发 `/forward here [别名]` 把*当前群*登记为转发群（最省事）。\n"
    "或在主控聊里：`/forward add <chat_id> [别名]`\n\n"
    "列表: `/forward list`\n"
    "删除: `/forward rm <别名|chat_id>`\n\n"
    "登记后，`/notify`、`/sendfile` 指定 target=别名 即可发到那个群（不指定=发给你）。\n"
    "⚠️ bot 须已在目标群里（建议给它发消息权限；置顶需置顶权限）。"
)


async def cmd_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    args = ctx.args or []
    chat = update.effective_chat
    if not args:
        await update.message.reply_text(_FORWARD_USAGE, parse_mode=ParseMode.MARKDOWN)
        return
    sub = args[0].lower()

    if sub == "list":
        if not b.forward_groups:
            await update.message.reply_text("还没登记任何转发群。/forward 看用法")
            return
        lines = ["📤 已登记的文件转发群"]
        for cid, alias in b.forward_groups.items():
            lines.append(f"· {alias} — {cid}")
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "here":
        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text(
                "/forward here 要在目标群里发。私聊里请用 /forward add <chat_id>")
            return
        if chat.id == b.group_id:
            await update.message.reply_text("这是主控群，不能登记为转发群。")
            return
        alias = args[1] if len(args) > 1 else (chat.title or str(chat.id))
        alias = b.add_forward_group(chat.id, alias)
        await update.message.reply_text(
            f"✅ 已把本群登记为文件转发群「{alias}」({chat.id})。\n"
            "之后 /notify、/sendfile target=该别名即可发到这里。本群成员消息我一律不响应。")
        return

    if sub == "add":
        if len(args) < 2:
            await update.message.reply_text("用法: /forward add <chat_id> [别名]")
            return
        try:
            cid = int(args[1])
        except ValueError:
            await update.message.reply_text("chat_id 必须是整数（群 id 形如 -100123…）")
            return
        if cid == b.group_id:
            await update.message.reply_text("这是主控群，不能登记为转发群。")
            return
        alias = b.add_forward_group(cid, args[2] if len(args) > 2 else None)
        await update.message.reply_text(
            f"✅ 已登记转发群「{alias}」({cid})。/notify、/sendfile target=该别名即可发。")
        return

    if sub == "rm":
        if len(args) < 2:
            await update.message.reply_text("用法: /forward rm <别名|chat_id>")
            return
        cid = b.remove_forward_group(args[1])
        if cid is None:
            await update.message.reply_text(f"没找到转发群 '{args[1]}'")
        else:
            await update.message.reply_text(f"🗑 已移除转发群 {cid}")
        return

    await update.message.reply_text(_FORWARD_USAGE, parse_mode=ParseMode.MARKDOWN)


# ── 参数小工具：从 args 里抽出 to=<别名> 与 pin 开关，剩下的当正文 ──────────
def _extract_target(args: list[str]) -> tuple[str | None, bool, list[str]]:
    """返回 (target, pin, 剩余args)。识别 to=别名 / target=别名 / pin 这几个前缀 token。"""
    target = None
    pin = False
    rest = []
    for a in args:
        low = a.lower()
        if low.startswith("to=") or low.startswith("target="):
            target = a.split("=", 1)[1].strip() or None
        elif low == "pin":
            pin = True
        else:
            rest.append(a)
    return target, pin, rest


# ── /notify：发文本通知到主控或某转发群（可顺手置顶）────────────────────────
async def cmd_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "📣 *发通知*\n用法: `/notify [to=别名] [pin] <文本…>`\n"
            "不带 to= 发给你；to=某转发群别名发到那个群；pin 顺手置顶。\n"
            "例: `/notify to=PH_DEV pin 新版已发布`",
            parse_mode=ParseMode.MARKDOWN)
        return
    target, pin, rest = _extract_target(args)
    text = " ".join(rest).strip()
    if not text:
        await update.message.reply_text("通知正文为空。用法见 /notify")
        return
    dest = f"转发群「{target}」" if target else "你"
    try:
        mid = await b._send_notification(text, target)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except Exception as e:
        await update.message.reply_text(f"❌ 发送失败: {e}")
        return
    extra = ""
    if pin:
        extra = "；" + await b._pin_message(target, mid, False)
    await update.message.reply_text(f"✅ 已发到{dest} (msg {mid}){extra}")


# ── /pin /unpin：置顶/取消置顶某条已发消息 ────────────────────────────────
async def cmd_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _pin_impl(update, ctx, unpin=False)


async def cmd_unpin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _pin_impl(update, ctx, unpin=True)


async def _pin_impl(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, unpin: bool):
    b = _b(ctx)
    args = ctx.args or []
    verb = "取消置顶" if unpin else "置顶"
    target, _pin, rest = _extract_target(args)
    if not rest or not rest[0].lstrip("-").isdigit():
        await update.message.reply_text(
            f"用法: /{'unpin' if unpin else 'pin'} <message_id> [to=别名]\n"
            "message_id 来自 /notify 的回执（msg 后面的数字）")
        return
    mid = int(rest[0])
    res = await b._pin_message(target, mid, unpin)
    await update.message.reply_text(f"{'🔓' if unpin else '📌'} {res}")


# ── /sendfile：把服务器文件发到主控或某转发群 ─────────────────────────────
async def cmd_sendfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "📎 *发文件*\n用法: `/sendfile <绝对路径> [to=别名] [说明…]`\n"
            "不带 to= 发给你；to=某转发群别名发到那个群。",
            parse_mode=ParseMode.MARKDOWN)
        return
    target, _pin, rest = _extract_target(args)
    if not rest:
        await update.message.reply_text("缺少文件路径。用法见 /sendfile")
        return
    path = rest[0]
    caption = " ".join(rest[1:]).strip()
    if not os.path.isfile(path):
        await update.message.reply_text(f"❌ 文件不存在: {path}")
        return
    dest = f"转发群「{target}」" if target else "你"
    note = await update.message.reply_text(f"📤 正在发送到{dest}…")
    try:
        await b._send_file_to_owner(path, caption, target)
        await note.edit_text(f"✅ 已发送 {os.path.basename(path)} → {dest}")
    except ValueError as e:
        await note.edit_text(f"❌ {e}")
    except Exception as e:
        # 大文件超时多半已送达，明确告知别重发
        from telegram.error import TimedOut
        if isinstance(e, TimedOut):
            await note.edit_text(f"⏳ 上传超时但很可能已送达 → {dest}，请勿重发；确认未到再重试")
        else:
            await note.edit_text(f"❌ 发送失败: {e}")


RELAY_COMMANDS = {
    "forward": cmd_forward,
    "notify": cmd_notify,
    "pin": cmd_pin,
    "unpin": cmd_unpin,
    "sendfile": cmd_sendfile,
}
