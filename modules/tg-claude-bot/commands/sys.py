"""commands_sys —— 系统监控命令：/status /report /sys /disk /top /devices /build。"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from infra import monitor

log = logging.getLogger("tgclaude")


def _b(ctx):
    return ctx.application.bot_data["botapp"]


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = _b(ctx).cfg["monitor"]
    await update.message.reply_text(await monitor.ph_status(m["repo"], m["project"]),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = _b(ctx).cfg["monitor"]
    await update.message.reply_text(await monitor.ph_report(m["repo"], m["project"]),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_sys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # host_status 内部 cpu_percent(interval=0.4) 是阻塞采样，必须下线程
    await update.message.reply_text(await asyncio.to_thread(monitor.host_status),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_disk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = _b(ctx).cfg
    paths = ["/", c["monitor"].get("out_dir", "/opt/ph-out"), c["claude"].get("cwd", "/")]
    await update.message.reply_text(
        await asyncio.to_thread(monitor.disk_status, paths),
        parse_mode=ParseMode.MARKDOWN)


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(monitor.top_procs, 10),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_devices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(monitor.adb_devices),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_build(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = _b(ctx).cfg["monitor"]
    ph = os.path.join(m["repo"], "build", "ph")
    chat_id = update.effective_chat.id
    cmd = [ph, "build", "--project", m["project"], *(ctx.args or [])]
    await update.message.reply_text(f"🚀 启动: `{' '.join(cmd[1:])}`", parse_mode=ParseMode.MARKDOWN)

    async def _run():
        t0 = time.time()
        res = await monitor.run_cmd(cmd, cwd=m["repo"], timeout=60 * 90)
        tail = res.out.strip()[-1500:]
        status = "✅" if res.rc == 0 else f"❌ rc={res.rc}"
        await ctx.application.bot.send_message(
            chat_id, f"{status} *ph build* 用时 {(time.time()-t0)/60:.1f}m\n```\n{tail}\n```",
            parse_mode=ParseMode.MARKDOWN)
    asyncio.create_task(_run())


# ── Build-log HUB integration ──
# Queries the WebCTL log buffer (HTTP) for recent build log entries + states.
# Configurable via cfg["monitor"]["log_buffer_url"] (default localhost:8080).

def _log_buffer_base(ctx) -> str:
    m = _b(ctx).cfg.get("monitor", {})
    return m.get("log_buffer_url", "http://localhost:8080").rstrip("/")


_LV_EMOJI = {"trace": "·", "info": "ℹ️", "warn": "⚠️", "error": "❌", "fatal": "💀"}


async def cmd_buildlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/buildlog [project] [level] — recent build log lines from the HUB buffer."""
    import urllib.parse
    import urllib.request

    base = _log_buffer_base(ctx)
    args = ctx.args or []
    params = {"limit": "25"}
    # heuristics: an arg that looks like a level → level filter; else project
    levels = {"info", "warn", "error", "fatal", "trace"}
    for a in args:
        if a.lower() in levels:
            params["level"] = a.lower() if a.lower() != "error" else "error,fatal"
        else:
            params["project"] = a
    url = f"{base}/api/log/recent?{urllib.parse.urlencode(params)}"

    def _fetch():
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                import json
                return json.loads(r.read())
        except Exception as exc:
            return {"error": str(exc)}

    data = await asyncio.to_thread(_fetch)
    if "error" in data:
        await update.message.reply_text(f"❌ 无法连接日志缓冲区: {data['error']}\n`{url}`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    entries = data.get("entries", [])
    if not entries:
        await update.message.reply_text("📭 无匹配的日志条目")
        return
    lines = []
    for e in reversed(entries[:25]):  # oldest first for readability
        ts = e["ts"][11:19]
        emo = _LV_EMOJI.get(e["level"], "·")
        proj = f"{e['project']}:{e['variant']}" if e.get("variant") else e.get("project", "")
        phase = f"[{e['phase']}]" if e.get("phase") else ""
        lines.append(f"{emo} `{ts}` {proj} {phase} {e['msg']}")
    text = "📋 *Build Log* (最近 %d 条)\n" % len(lines) + "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…(截断)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_buildstate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/buildstate — current build states from the HUB buffer."""
    import urllib.request

    base = _log_buffer_base(ctx)
    url = f"{base}/api/log/states"

    def _fetch():
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                import json
                return json.loads(r.read())
        except Exception as exc:
            return {"error": str(exc)}

    data = await asyncio.to_thread(_fetch)
    if "error" in data:
        await update.message.reply_text(f"❌ 无法连接日志缓冲区: {data['error']}",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    states = data.get("states", [])
    if not states:
        await update.message.reply_text("📭 暂无构建状态")
        return
    emo = {"ok": "✅", "fail": "❌", "running": "🔄", "pending": "⏳"}
    lines = []
    for s in states[:30]:
        proj = f"{s['project']}:{s['variant']}" if s.get("variant") else s.get("project", "")
        st = s.get("status", "pending")
        phase = s.get("phase", "")
        prog = ""
        if s.get("total_phases"):
            prog = f" ({s.get('done_phases',0)}/{s['total_phases']})"
        rc = f" rc={s['rc']}" if s.get("rc") else ""
        lines.append(f"{emo.get(st,'·')} *{proj}* — {phase}{prog}{rc}")
    text = "🏗 *Build States*\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_webctl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """WebCTL 构建控制台快速入口。"""
    import aiohttp
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    cfg = ctx.application.bot_data.get("cfg") or _b(ctx).cfg
    local_base = (cfg.get("webctl") or {}).get("url", "http://localhost:8080")

    # 远程 URL：走 static 隧道的 /webctl 反代（TG WebApp 需要 HTTPS 公网 URL）
    console = ctx.application.bot_data.get("console")
    if console:
        public, _ = console.live_tunnel_urls()
    else:
        public = ""
    remote_base = (public.rstrip("/") + "/webctl") if public else ""

    # Quick health check (用本地地址)
    status_text = ""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{local_base}/api/overview", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    data = await r.json()
                    running = sum(1 for v in (data.get("progress") or {}).values()
                                  if v.get("status") == "running")
                    projs = len(data.get("projects") or {})
                    status_text = f"✅ 在线 · {projs} 项目 · {running} 构建中"
                else:
                    status_text = f"⚠️ 响应异常 ({r.status})"
    except Exception:
        status_text = "❌ WebCTL 不可达"

    # 按钮 base: 优先用远程隧道 URL
    btn_base = remote_base or local_base

    lines = [
        "🔧 <b>Phantom WebCTL</b>",
        "",
        status_text,
        "",
        f"🌐 <code>{local_base}</code>",
    ]
    if remote_base:
        lines.append(f"🔗 <code>{remote_base}</code>")

    rows = []
    if update.effective_chat.type == "private":
        rows.append([
            InlineKeyboardButton("📊 监控面板", web_app=WebAppInfo(url=f"{btn_base}/")),
            InlineKeyboardButton("📋 构建日志", web_app=WebAppInfo(url=f"{btn_base}/hub")),
        ])
        rows.append([
            InlineKeyboardButton("⚙ 构建配置", web_app=WebAppInfo(url=f"{btn_base}/configurator")),
            InlineKeyboardButton("🖥 ICECC", web_app=WebAppInfo(url=f"{btn_base}/icecc")),
        ])
        rows.append([
            InlineKeyboardButton("📚 文档中心", web_app=WebAppInfo(url=f"{btn_base}/docs-viewer")),
        ])
    else:
        # In groups, use URL buttons instead of web_app
        rows.append([
            InlineKeyboardButton("📊 面板", url=f"{btn_base}/"),
            InlineKeyboardButton("📋 日志", url=f"{btn_base}/hub"),
            InlineKeyboardButton("⚙ 配置", url=f"{btn_base}/configurator"),
        ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None)


SYS_COMMANDS = {
    "status": cmd_status, "report": cmd_report,
    "sys": cmd_sys, "disk": cmd_disk, "top": cmd_top,
    "devices": cmd_devices, "build": cmd_build,
    "buildlog": cmd_buildlog, "buildstate": cmd_buildstate,
    "webctl": cmd_webctl,
}
