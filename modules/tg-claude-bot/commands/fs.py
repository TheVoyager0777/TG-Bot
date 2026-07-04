"""commands_fs —— 文件系统相关命令：目录浏览器、文件存取、解压。

与 bot.py 解耦：这里只定义 handler + callback 逻辑 + 状态，
由 bot.py 注册和派发。不在模块加载期 import bot。
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger("tgclaude.fs")

# ── 状态 ───────────────────────────────────────────────────────────────────────
_LS_PATHS: dict[str, str] = {}   # token -> 绝对路径
_LS_PER_PAGE = 25
# 多选状态：chat_id -> {"path": str, "selected": set, "msg_id": int, "store_mode": bool}
_LS_STATE: dict[int, dict] = {}

_ARCHIVE_EXTS = {
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz", ".7z", ".rar", ".gz", ".bz2", ".xz",
}


# ── 工具函数 ───────────────────────────────────────────────────────────────────
def _is_archive(name: str) -> bool:
    nl = name.lower()
    return any(nl.endswith(ext) for ext in _ARCHIVE_EXTS)


def _ls_tok(path: str) -> str:
    t = uuid.uuid4().hex[:10]
    _LS_PATHS[t] = path
    if len(_LS_PATHS) > 4000:
        for k in list(_LS_PATHS)[:2000]:
            _LS_PATHS.pop(k, None)
    return t


def _ls_resolve(tok: str) -> str | None:
    return _LS_PATHS.get(tok)


def _ls_listing(path: str):
    """返回 (dirs, files)，各为 (名字, 绝对路径) 列表，已排序。"""
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append((e.name, e.path))
                    else:
                        files.append((e.name, e.path))
                except OSError:
                    files.append((e.name, e.path))
    except (PermissionError, FileNotFoundError, NotADirectoryError) as ex:
        return None, str(ex)
    dirs.sort(key=lambda x: x[0].lower())
    files.sort(key=lambda x: x[0].lower())
    return dirs, files


def _ls_keyboard(path: str, page: int = 0, selected: set | None = None):
    dirs, files = _ls_listing(path)
    if dirs is None:
        return None, f"⚠️ 无法打开:\n`{path}`\n{files}"
    selected = selected or set()
    entries = [("📁 " + n, p, True) for n, p in dirs] + \
              [("📄 " + n, p, False) for n, p in files]
    total = len(entries)
    pages = max(1, (total + _LS_PER_PAGE - 1) // _LS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = entries[page * _LS_PER_PAGE:(page + 1) * _LS_PER_PAGE]
    rows = []
    for label, p, is_dir in chunk:
        if is_dir:
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"ls:cd:{_ls_tok(p)}")])
        else:
            check = "☑️ " if p in selected else ""
            rows.append([
                InlineKeyboardButton(check + label[:45], callback_data=f"ls:sel:{_ls_tok(p)}"),
                InlineKeyboardButton("📥", callback_data=f"ls:get:{_ls_tok(p)}"),
            ])
    if selected:
        action_row = [
            InlineKeyboardButton(f"🗑 清除({len(selected)})", callback_data="ls:clr"),
            InlineKeyboardButton("📋 路径", callback_data="ls:cpsel"),
        ]
        archives = [p for p in selected if _is_archive(os.path.basename(p))]
        if archives:
            action_row.append(InlineKeyboardButton(f"📦 解压({len(archives)})", callback_data="ls:unzip"))
        rows.append(action_row)
    nav = []
    parent = os.path.dirname(path.rstrip("/")) or "/"
    if os.path.realpath(parent) != os.path.realpath(path):
        nav.append(InlineKeyboardButton("⬆️ 上级", callback_data=f"ls:cd:{_ls_tok(parent)}"))
    if pages > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"ls:pg:{_ls_tok(path)}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="ls:nop"))
        nav.append(InlineKeyboardButton("▶️", callback_data=f"ls:pg:{_ls_tok(path)}:{page+1}"))
    nav.append(InlineKeyboardButton("📋 此目录", callback_data=f"ls:cp:{_ls_tok(path)}"))
    nav.append(InlineKeyboardButton("📤 存入", callback_data=f"ls:store:{_ls_tok(path)}"))
    if nav:
        rows.append(nav)
    sel_info = f" · 选中 {len(selected)} 项" if selected else ""
    text = f"🗂 `{path}`\n{len(dirs)} 目录 · {len(files)} 文件{sel_info}"
    return InlineKeyboardMarkup(rows), text


# ── 解压 ───────────────────────────────────────────────────────────────────────
async def _decompress_file(archive_path: str, dest_dir: str) -> str:
    name = archive_path.lower()
    if name.endswith(".zip"):
        cmd = ["unzip", "-o", archive_path, "-d", dest_dir]
    elif name.endswith(".7z"):
        cmd = ["7z", "x", archive_path, f"-o{dest_dir}", "-y"]
    elif name.endswith(".rar"):
        cmd = ["unrar", "x", "-o+", archive_path, dest_dir + "/"]
    elif any(name.endswith(e) for e in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
        cmd = ["tar", "xf", archive_path, "-C", dest_dir]
    elif name.endswith(".gz"):
        import gzip, shutil as _sh
        out = os.path.join(dest_dir, os.path.basename(archive_path)[:-3])
        with gzip.open(archive_path, "rb") as fi, open(out, "wb") as fo:
            _sh.copyfileobj(fi, fo)
        return out
    elif name.endswith(".bz2"):
        import bz2, shutil as _sh
        out = os.path.join(dest_dir, os.path.basename(archive_path)[:-4])
        with bz2.open(archive_path, "rb") as fi, open(out, "wb") as fo:
            _sh.copyfileobj(fi, fo)
        return out
    elif name.endswith(".xz"):
        import lzma, shutil as _sh
        out = os.path.join(dest_dir, os.path.basename(archive_path)[:-3])
        with lzma.open(archive_path, "rb") as fi, open(out, "wb") as fo:
            _sh.copyfileobj(fi, fo)
        return out
    else:
        raise ValueError(f"不支持的格式: {os.path.basename(archive_path)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(out_bytes.decode("utf-8", "replace")[:200])
    return dest_dir


# ── /ls 命令 ───────────────────────────────────────────────────────────────────
def _b(ctx):
    return ctx.application.bot_data["botapp"]


async def cmd_ls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """目录浏览器。/ls [起始路径]，默认从 claude cwd 开始。"""
    b = _b(ctx)
    start = (ctx.args[0] if ctx.args else None) or b.cfg["claude"].get("cwd") or "/"
    start = os.path.abspath(os.path.expanduser(start))
    if not os.path.isdir(start):
        start = os.path.dirname(start) or "/"
    chat_id = update.effective_chat.id
    _LS_STATE[chat_id] = {"path": start, "selected": set()}
    kb, text = _ls_keyboard(start, 0)
    if kb is None:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    msg = await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    _LS_STATE[chat_id]["msg_id"] = msg.message_id


# ── ls callback 处理 ──────────────────────────────────────────────────────────
async def handle_ls_callback(q, data: str):
    """处理 ls: 前缀的 callback_query。返回 True 表示已处理。"""
    parts = data.split(":")
    action = parts[1]
    chat_id = q.message.chat_id

    if action == "nop":
        await q.answer()
        return

    state = _LS_STATE.get(chat_id) or {"path": "/", "selected": set()}

    if action == "clr":
        state["selected"] = set()
        _LS_STATE[chat_id] = state
        await q.answer("已清除选择")
        kb, text = _ls_keyboard(state["path"], 0, state["selected"])
        if kb:
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        return

    if action == "cpsel":
        sel = state.get("selected", set())
        if not sel:
            await q.answer("没有选中文件")
            return
        await q.answer(f"已发送 {len(sel)} 个路径")
        paths_text = "\n".join(f"`{p}`" for p in sorted(sel))
        await q.message.reply_text(paths_text, parse_mode=ParseMode.MARKDOWN)
        return

    if action == "unzip":
        sel = state.get("selected", set())
        archives = [p for p in sel if _is_archive(os.path.basename(p))]
        if not archives:
            await q.answer("选中文件中没有压缩包")
            return
        await q.answer(f"正在解压 {len(archives)} 个文件…")
        results = []
        for arc in archives:
            dest = state["path"]
            try:
                await _decompress_file(arc, dest)
                results.append(f"✅ `{os.path.basename(arc)}` → `{dest}`")
            except Exception as e:
                results.append(f"❌ `{os.path.basename(arc)}`: {str(e)[:80]}")
        state["selected"] -= set(archives)
        await q.message.reply_text("📦 *解压结果*\n" + "\n".join(results), parse_mode=ParseMode.MARKDOWN)
        kb, text = _ls_keyboard(state["path"], 0, state["selected"])
        if kb:
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        return

    if action == "store":
        tok = parts[2] if len(parts) > 2 else ""
        path = _ls_resolve(tok)
        if path:
            state["path"] = path
            state["store_mode"] = True
            _LS_STATE[chat_id] = state
            await q.answer("📤 存入模式")
            await q.message.reply_text(
                f"📤 *存入模式已激活*\n目标: `{path}`\n\n现在发送文件/图片，将保存到此目录。",
                parse_mode=ParseMode.MARKDOWN)
        else:
            await q.answer("链接过期，请重新 /ls", show_alert=True)
        return

    if action == "get":
        tok = parts[2] if len(parts) > 2 else ""
        path = _ls_resolve(tok)
        if path is None:
            await q.answer("链接已过期", show_alert=True)
            return
        if not os.path.isfile(path):
            await q.answer("文件不存在", show_alert=True)
            return
        size = os.path.getsize(path)
        if size > 50 * 1024 * 1024:
            await q.answer("文件超过 50MB，TG 无法发送", show_alert=True)
            return
        await q.answer("正在发送…")
        try:
            await q.message.reply_document(document=open(path, "rb"),
                                           filename=os.path.basename(path),
                                           caption=f"`{path}`",
                                           parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await q.message.reply_text(f"❌ 发送失败: {html.escape(str(e))}")
        return

    if action == "sel":
        tok = parts[2] if len(parts) > 2 else ""
        path = _ls_resolve(tok)
        if path is None:
            await q.answer("链接已过期", show_alert=True)
            return
        sel = state.setdefault("selected", set())
        if path in sel:
            sel.discard(path)
            await q.answer("取消选中")
        else:
            sel.add(path)
            await q.answer(f"已选中 ({len(sel)})")
        _LS_STATE[chat_id] = state
        kb, text = _ls_keyboard(state["path"], 0, sel)
        if kb:
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        return

    # cp / cd / pg
    tok = parts[2] if len(parts) > 2 else ""
    path = _ls_resolve(tok)
    if path is None:
        await q.answer("链接已过期，请重新 /ls", show_alert=True)
        return
    if action == "f":
        await q.answer("路径已发送")
        try:
            await q.message.reply_text(f"`{path}`", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(path)
        return
    if action == "cp":
        await q.answer("路径已发送")
        await q.message.reply_text(f"`{path}`", parse_mode=ParseMode.MARKDOWN)
        return
    page = int(parts[3]) if action == "pg" and len(parts) > 3 else 0
    if action == "cd":
        state["path"] = path
        state["selected"] = set()
        _LS_STATE[chat_id] = state
    await q.answer()
    kb, text = _ls_keyboard(path, page, state.get("selected"))
    if kb is None:
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


# ── on_file：文件接收（存入模式 / 默认交给会话）─────────────────────────────────
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """收文件/图片：ls 存入模式 → 保存到浏览目录；否则 → uploads + 交给会话。"""
    b = _b(ctx)
    msg = update.message
    tid = msg.message_thread_id
    chat_id = update.effective_chat.id
    fobj = None
    fname = None
    if msg.document:
        fobj = msg.document
        fname = msg.document.file_name or f"doc_{msg.document.file_unique_id}"
    elif msg.photo:
        fobj = msg.photo[-1]
        fname = f"photo_{fobj.file_unique_id}.jpg"
    if fobj is None:
        return
    safe = os.path.basename(fname).replace("/", "_") or "file"

    # ls 存入模式
    ls_state = _LS_STATE.get(chat_id)
    if ls_state and ls_state.get("store_mode"):
        dest_dir = ls_state["path"]
        ls_state["store_mode"] = False
        dest = os.path.join(dest_dir, safe)
        try:
            tf = await fobj.get_file()
            await tf.download_to_drive(dest)
        except Exception as e:
            await ctx.bot.send_message(chat_id, f"💥 保存失败: {html.escape(str(e))}",
                                       message_thread_id=tid)
            return
        size = os.path.getsize(dest)
        await ctx.bot.send_message(
            chat_id, f"✅ 已保存 `{safe}`（{size} 字节）→ `{dest_dir}`",
            message_thread_id=tid, parse_mode=ParseMode.MARKDOWN)
        kb, text = _ls_keyboard(dest_dir, 0, ls_state.get("selected"))
        if kb:
            try:
                old_msg_id = ls_state.get("msg_id")
                if old_msg_id:
                    await ctx.bot.edit_message_text(
                        text, chat_id=chat_id, message_id=old_msg_id,
                        reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        return

    # 默认：保存到 uploads + 交给会话
    repo = b.cfg["claude"].get("cwd") or os.getcwd()
    updir = os.path.join(repo, ".agentmem", "uploads")
    os.makedirs(updir, exist_ok=True)
    dest = os.path.join(updir, f"{int(time.time())}_{safe}")
    try:
        tf = await fobj.get_file()
        await tf.download_to_drive(dest)
    except Exception as e:
        await ctx.bot.send_message(chat_id, f"💥 下载失败: {html.escape(str(e))}",
                                   message_thread_id=tid)
        return
    size = os.path.getsize(dest)
    caption = (msg.caption or "").strip()
    await ctx.bot.send_message(
        chat_id, f"📎 已接收 `{safe}`（{size} 字节）→ 交给会话分析",
        message_thread_id=tid, parse_mode=ParseMode.MARKDOWN)
    instr = (f"我刚上传了一个文件，已保存到本地路径：{dest}\n"
             f"请用 Read 工具读取并分析它。")
    if caption:
        instr += f"\n我的说明/要求：{caption}"
    await b.run_turn(instr, chat_id, tid)
