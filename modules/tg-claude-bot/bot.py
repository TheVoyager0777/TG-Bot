#!/usr/bin/env python3
"""bot.py — 入口：加载配置、组装 BotApp + handlers、启动 polling。

跑: python3 bot.py [config.toml]
"""
from __future__ import annotations

import logging
import os
import sys
import asyncio

from project_paths import ensure_split_projects

ensure_split_projects()

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from core.botapp import BotApp
from handlers import _owner_only, on_text, on_file, on_callback, on_unhandled_command, init_registry
from commands.session import SESSION_COMMANDS
from commands.sys import SYS_COMMANDS
from commands.relay import RELAY_COMMANDS
from commands.prompts import PROMPT_COMMANDS
from commands.services import SERVICE_COMMANDS, build_services_status_text
from commands import llm
from ui import keyboard
from infra import version
from phantom_console.event_log import BUS
from phantom_console.tasks import get_task_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tgclaude")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


class ConsoleIpcServer:
    """Local-only bridge used by the external phantom-console service."""

    def __init__(self, botapp: BotApp, console, host: str = "127.0.0.1", port: int = 8877):
        self.botapp = botapp
        self.console = console
        self.host = host
        self.port = port
        self.runner = None

    def _authed(self, request) -> bool:
        import hmac

        key = request.query.get("key") or request.headers.get("X-Console-Key", "")
        return bool(key and hmac.compare_digest(key, self.console.key))

    async def _json_body(self, request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    async def events(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            since = int(request.query.get("since", 0))
        except ValueError:
            since = 0
        try:
            limit = int(request.query.get("limit", 1000))
        except ValueError:
            limit = 1000
        session = request.query.get("session") or None
        wait = request.query.get("wait") == "1"
        if wait:
            evs = await BUS.wait(since, timeout=25.0, session=session)
        elif since == 0:
            evs = BUS.history_backlog(limit=limit, session=session)
        else:
            evs = BUS.backlog(since, limit=limit, session=session)
        evs = self.console._with_cc_history_backfill(evs, since, limit, session)
        return web.json_response({
            "seq": BUS.seq,
            "sessions": BUS.sessions(),
            "events": evs,
            "state": self.botapp.web_state(),
        })

    async def state(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response(self.botapp.web_state())

    async def send(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "空消息"}, status=400)
        if len(text) > 16000:
            return web.json_response({"error": "消息过长"}, status=400)
        session = (body.get("session") or "").strip() or "main"
        try:
            status = await self.botapp.web_send(session, text)
            return web.json_response({"ok": True, "status": status})
        except KeyError as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            log.warning("ipc web_send failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def stop_turn(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        session = (body.get("session") or "").strip() or None
        stopped = await self.botapp.web_stop(session)
        return web.json_response({"ok": True, "stopped": stopped})

    async def perm(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        token = (body.get("token") or "").strip()
        decision = (body.get("decision") or "").strip()
        if decision not in ("allow", "always", "deny"):
            return web.json_response({"error": "decision 须为 allow|always|deny"}, status=400)
        ok = self.botapp.resolve_permission(token, decision)
        return web.json_response({"ok": ok})

    async def queue(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        token = (body.get("token") or "").strip()
        action = (body.get("action") or "").strip()
        if action not in ("steer", "cancel"):
            return web.json_response({"error": "action 须为 steer|cancel"}, status=400)
        toast = await self.botapp.resolve_queued(token, action)
        return web.json_response({"ok": toast is not None, "toast": toast or "该排队消息已失效"})

    async def ask(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        token = (body.get("token") or "").strip()
        if not token:
            return web.json_response({"error": "token 必填"}, status=400)
        result = await self.botapp.web_ask(token, body.get("answers") or {})
        return web.json_response({"ok": result == "ok", "message": result})

    async def control_get(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        session = request.query.get("session") or None
        return web.json_response(self.botapp.web_control_get(session))

    async def control_set(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        session = (body.get("session") or "").strip() or "main"
        key = (body.get("key") or "").strip()
        if not key:
            return web.json_response({"error": "key 必填"}, status=400)
        msg = await self.botapp.web_control_set(session, key, body.get("value"))
        return web.json_response({"ok": True, "message": msg})

    async def todo_get(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response(self.botapp.web_todo_get(request.query.get("session") or None))

    async def todo_delete(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        msg = self.botapp.web_todo_delete(
            (body.get("session") or "").strip() or "main",
            body.get("index", -1),
        )
        return web.json_response({"ok": msg.startswith("已删除"), "message": msg})

    async def task_start(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        cmd = (body.get("command") or "").strip()
        if not cmd:
            return web.json_response({"error": "missing command"}, status=400)
        tid = await get_task_manager().submit(
            body.get("session") or "main",
            body.get("label") or cmd[:60],
            cmd,
            body.get("cwd") or "/tmp",
        )
        return web.json_response({"ok": True, "task_id": tid})

    async def task_get(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        task = get_task_manager().get_task(request.match_info.get("tid", ""))
        if task is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(task)

    async def tasks_list(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response({"tasks": get_task_manager().list_tasks(request.query.get("session") or None)})

    async def task_kill(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        ok = await get_task_manager().kill(request.match_info.get("tid", ""))
        return web.json_response({"ok": ok})

    async def cc_resume(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        session_id = (body.get("session_id") or "").strip()
        project = (body.get("project") or "").strip()
        name = (body.get("name") or "").strip() or f"cc-{session_id[:8]}"
        if not session_id:
            return web.json_response({"error": "session_id 必填"}, status=400)
        cwd = self.console._CC_CWDS.get(project, self.console._CC_CWDS.get("Workspace", ""))
        result = await self.botapp.web_cc_resume(session_id, cwd, name)
        return web.json_response(result)

    async def cc_active(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        workers = self.botapp.web_cc_active()
        loop = asyncio.get_event_loop()
        for w in workers:
            sid, fp = self.console._cc_resolve_session_file(
                w.get("session_id") or "",
                w.get("name") or "",
            )
            if sid and not w.get("session_id"):
                w["session_id"] = sid
            if fp:
                w["recent"] = await loop.run_in_executor(
                    None, self.console._cc_recent_msgs, fp, 3)
            else:
                w["recent"] = []
        return web.json_response({"workers": workers})

    async def cc_stop(self, request):
        from aiohttp import web

        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._json_body(request)
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "name 必填"}, status=400)
        result = await self.botapp.web_cc_stop(name)
        return web.json_response(result)

    async def start(self):
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/api/events", self.events)
        app.router.add_get("/api/state", self.state)
        app.router.add_post("/api/send", self.send)
        app.router.add_post("/api/stop", self.stop_turn)
        app.router.add_post("/api/perm", self.perm)
        app.router.add_post("/api/queue", self.queue)
        app.router.add_post("/api/ask", self.ask)
        app.router.add_get("/api/control", self.control_get)
        app.router.add_post("/api/control", self.control_set)
        app.router.add_get("/api/todo", self.todo_get)
        app.router.add_post("/api/todo/delete", self.todo_delete)
        app.router.add_post("/api/task", self.task_start)
        app.router.add_get("/api/task/{tid}", self.task_get)
        app.router.add_get("/api/tasks", self.tasks_list)
        app.router.add_post("/api/task/{tid}/kill", self.task_kill)
        app.router.add_post("/api/cc-resume", self.cc_resume)
        app.router.add_get("/api/cc-active", self.cc_active)
        app.router.add_post("/api/cc-stop", self.cc_stop)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        log.info("console ipc serving: http://%s:%s", self.host, self.port)

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None


BOT_COMMANDS = [
    BotCommand("start", "▸🤖LLM｜用法说明 + 唤出菜单键盘"),
    BotCommand("version", "查看 bot 版本/构建时间/fingerprint"),
    BotCommand("menu", "唤出菜单（私聊底栏 / 群里 inline）"),
    BotCommand("workers", "列出所有 worker 及状态"),
    BotCommand("prompts", "提示词池：列出/调用当前会话的提示词"),
    BotCommand("attach", "私聊模式: 直连某 worker"),
    BotCommand("main", "切回主对话"),
    BotCommand("console", "🖥 控制台动态入口（按需分配最新 URL）"),
    BotCommand("app", "📱 快速跳转 Phantom Control App"),
    BotCommand("forward", "管理文件转发群（其他群只收文件）"),
    BotCommand("notify", "发通知到主控/转发群（可置顶）"),
    BotCommand("sendfile", "发文件到主控/转发群"),
    BotCommand("pin", "置顶某条已发消息"),
    BotCommand("unpin", "取消置顶某条消息"),
    BotCommand("stop", "中断当前会话这一轮"),
    BotCommand("mode", "切权限模式 default|bypassPermissions|…"),
    BotCommand("model", "切模型档位 opus|sonnet|haiku|default"),
    BotCommand("fast", "fast mode 加速输出 on|off|toggle"),
    BotCommand("context", "上下文窗口用量统计"),
    BotCommand("compact", "压缩对话历史，省 token"),
    BotCommand("providers", "LLM 端点列表 / 切换"),
    BotCommand("backend", "切换 Claude Code / Codex CLI 后端"),
    BotCommand("provider", "增删 LLM 端点配置"),
    BotCommand("testllm", "测试所有 LLM 端点可用性"),
    BotCommand("status", "▸🛠系统｜构建状态 (ph status)"),
    BotCommand("report", "构建用时报告 (ph report)"),
    BotCommand("build", "后台跑 ph build [args]"),
    BotCommand("buildlog", "📋 构建日志 HUB [project] [level]"),
    BotCommand("buildstate", "🏗 各构建实时状态"),
    BotCommand("sys", "主机 CPU/内存/负载"),
    BotCommand("disk", "磁盘占用"),
    BotCommand("top", "占用最高的进程"),
    BotCommand("devices", "adb 设备列表"),
    BotCommand("svc", "管理子模块服务 console/llm"),
    BotCommand("svcstatus", "查看 bot/console/llm 状态与版本"),
    BotCommand("ls", "目录浏览器:多选/解压/存取文件"),
    BotCommand("sharedmem", "共享记忆:worker 文件改动台账"),
]


async def _post_init(app: Application):
    b: BotApp = app.bot_data["botapp"]
    await b.start()
    app.bot_data["console_callback_handler"] = handle_console_callback
    init_registry(SESSION_COMMANDS, SYS_COMMANDS, RELAY_COMMANDS, SERVICE_COMMANDS)
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)
    # Mini App 控制台（可选）：同事件循环起 aiohttp，吃 event_log 总线
    cfg = app.bot_data["cfg"]
    web_cfg = cfg.get("webapp") or {}
    external_console = os.environ.get("PHANTOM_CONSOLE_EXTERNAL", "").lower() in (
        "1", "true", "yes", "on")
    console_line = ""
    if web_cfg.get("enabled") or external_console:
        try:
            from phantom_console.server import Console
            _bus_log = os.path.expanduser("~/.config/tg-bus-log.jsonl")
            BUS._log_path = _bus_log
            _loaded = BUS.load_history(_bus_log, n=8000)
            if _loaded:
                log.info("bus history: %d events replayed", _loaded)
            console = Console(cfg["telegram"]["token"],
                              int(cfg["telegram"]["owner_id"]),
                              port=int(web_cfg.get("port", 8765)),
                              static_port=int(web_cfg.get("static_port", 8766)),
                              webctl=cfg.get("webctl") or {})
            console.botapp = b   # 启用 /api/send /api/stop /api/state
            app.bot_data["console"] = console
            if external_console:
                ipc_port = int(os.environ.get("PHANTOM_CONSOLE_IPC_PORT", "8877"))
                ipc = ConsoleIpcServer(b, console, port=ipc_port)
                await ipc.start()
                app.bot_data["console_ipc"] = ipc
                log.info("console external service mode: %s", console.local_url())
                console_line = "\n🖥 Console: external service mode; /console 取链接"
            else:
                await console.start()
                log.info("console url: %s", console.local_url())
                console_line = "\n🖥 Console: embedded mode; /console 取链接"
            # 自动读取 phantom-network 通讯录，回退到旧 cf-tunnels 兼容文件。
            _ts_path = os.path.expanduser("~/.config/phantom-network/addressbook.json")
            try:
                if os.path.exists(_ts_path):
                    import json as _json
                    _ts = _json.load(open(_ts_path))
                    pools = _ts.get("pools") or {}
                    static_url = (pools.get("console.static") or {}).get("primary") or ""
                    api_url = (pools.get("console.api") or {}).get("primary") or ""
                    if static_url and api_url:
                        web_cfg["public_url"] = static_url
                        web_cfg["api_url"] = api_url
                else:
                    compat = os.path.expanduser("~/.config/tg-cf-tunnels.json")
                    if os.path.exists(compat):
                        import json as _json
                        _ts = _json.load(open(compat))
                        if _ts.get("static_url") and _ts.get("api_url"):
                            web_cfg["public_url"] = _ts["static_url"]
                            web_cfg["api_url"] = _ts["api_url"]
            except Exception:
                pass
        except Exception as e:
            log.warning("console start failed: %s", e)
    mode = "论坛模式" if b.forum else "私聊模式"
    try:
        delay = float(os.environ.get("PHANTOM_STARTUP_STATUS_DELAY", "1.5"))
        if delay > 0:
            await asyncio.sleep(delay)
        status_text = await build_services_status_text(cfg["_config_path"])
        greet = (f"*PhantomControlPlane online* ({mode})\n"
                 f"build: `{version.build_str()}`\n"
                 f"{console_line}\n\n"
                 f"{status_text}")
        kw = {}
        if b.forum:
            greet += "\n\n`/menu` 唤出菜单"
        else:
            kw["reply_markup"] = keyboard.reply_root()
        await app.bot.send_message(b._target_chat(), greet,
                                   parse_mode=ParseMode.MARKDOWN, **kw)
    except Exception as e:
        log.warning("greet failed: %s", e)


def _console_live_urls(console, cfg: dict) -> tuple[str, str]:
    """Return latest (static, api) tunnel URLs, with config snapshot as fallback."""
    web = cfg.get("webapp") or {}
    try:
        public, api = console.live_tunnel_urls()
    except Exception:
        public, api = "", ""
    return (public or (web.get("public_url") or ""),
            api or (web.get("api_url") or ""))


def _console_contact_status_lines() -> list[str]:
    import json as _json
    import urllib.parse as _urlparse

    path = os.path.expanduser("~/.config/phantom-network/addressbook.json")
    if not os.path.exists(path):
        return ["phantom-network 通讯录：未发现，入口会回退到启动快照。"]

    try:
        data = _json.load(open(path))
        pools = data.get("pools") or {}
    except Exception:
        return ["phantom-network 通讯录：读取失败，入口会回退到启动快照。"]

    names = [
        ("console.static", "静态入口"),
        ("console.api", "数据入口"),
    ]
    lines = ["phantom-network 通讯录："]
    contacts = data.get("contacts") or {}

    def best_domain(contact: dict) -> dict:
        domains = [
            d for d in (contact.get("domains") or [])
            if d.get("url") and d.get("status") == "online"
        ]
        if not domains:
            return {}

        def score(item: dict) -> tuple[int, float, int]:
            try:
                latency = float(item.get("latency_ms"))
            except (TypeError, ValueError):
                latency = 999999.0
            freshness = int(item.get("last_ok") or item.get("last_checked")
                            or item.get("last_seen") or 0)
            return (latency, -freshness)

        return min(domains, key=score)

    for name, label in names:
        pool = pools.get(name) or {}
        lanes = pool.get("lanes") or 0
        if isinstance(lanes, list):
            lane_count = len(lanes)
            online_count = sum(1 for lane in lanes if lane.get("status") == "online")
        else:
            lane_count = int(lanes or 0)
            online = pool.get("online") or []
            online_count = len(online) if isinstance(online, list) else int(bool(online))
        domains = pool.get("domains") or []
        if not lane_count and isinstance(domains, list):
            lane_count = len(domains)
        best = best_domain(contacts.get(name) or {})
        online = pool.get("online") or []
        primary = (best.get("url") or (online[0] if online else "") or "")
        host = _urlparse.urlparse(primary).netloc or primary.replace("https://", "").replace("http://", "")
        latency = best.get("latency_ms")
        lane = best.get("lane")
        if isinstance(latency, (int, float)):
            detail = f" · lane {lane} · {latency:.0f}ms" if lane is not None else f" · {latency:.0f}ms"
        else:
            detail = f" · lane {lane}" if lane is not None else ""
        if host:
            lines.append(f"• {label}: {online_count}/{lane_count} online · {host}{detail}")
        else:
            lines.append(f"• {label}: {online_count}/{lane_count} online · 未分配")
    return lines


def _console_panel_text(console=None, cfg: dict | None = None) -> str:
    lines = [
        "🖥 <b>Phantom Console</b>",
        "",
        "入口不再固定在这条消息里。点击按钮时会读取最新通讯录并分配当前可用 URL。",
        "旧消息也可以继续使用；重新进入前先点对应按钮重新分配。",
        "",
    ]
    lines.extend(_console_contact_status_lines())
    if console is not None:
        try:
            if console.lan_ip():
                lines.append("• 本地入口: 可用")
        except Exception:
            pass
    lines.extend([
        "",
        "⚠️ 打开入口时会下发访问凭据，请只在 owner 会话使用。",
    ])
    return "\n".join(lines)


def _console_panel_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 浏览器入口", callback_data="console:browser"),
            InlineKeyboardButton("🖥 Mini App", callback_data="console:miniapp"),
        ],
        [
            InlineKeyboardButton("📱 App 入口", callback_data="console:app"),
            InlineKeyboardButton("📶 本地入口", callback_data="console:local"),
        ],
        [InlineKeyboardButton("🔄 刷新状态", callback_data="console:status")],
    ])


def _console_back_markup(action: str, open_button=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    if open_button is not None:
        rows.append([open_button])
    rows.append([
        InlineKeyboardButton("🔄 重新分配", callback_data=f"console:{action}"),
        InlineKeyboardButton("⬅️ 返回", callback_data="console:home"),
    ])
    return InlineKeyboardMarkup(rows)


async def _console_edit_or_reply(q, text: str, markup):
    try:
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except Exception:
        if q.message:
            await q.message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )


async def handle_console_callback(q, ctx, data: str):
    """Handle dynamic /console access buttons.

    Telegram URL buttons are static once sent, so the first click must be a
    callback. The callback reads phantom-network's latest addressbook and then
    renders a fresh open button for this moment.
    """
    import html as _html
    from telegram import InlineKeyboardButton, WebAppInfo

    console = ctx.application.bot_data.get("console")
    if console is None:
        await q.answer("控制台未启用")
        return

    cfg = ctx.application.bot_data["cfg"]
    action = (data.split(":", 1)[1] or "home") if ":" in data else "home"
    if action in ("home", "status"):
        await q.answer("已刷新")
        await _console_edit_or_reply(q, _console_panel_text(console, cfg), _console_panel_markup())
        return

    await q.answer("正在分配最新入口…")
    public, api = _console_live_urls(console, cfg)

    if action == "browser":
        url = ""
        if public and api:
            url = console.dual_tunnel_url(public, api)
        elif public:
            url = console.url_with_key(public)
        elif api:
            url = console.url_with_key(api)
        if not url:
            text = (
                "🌐 <b>浏览器入口暂不可用</b>\n\n"
                "当前没有在线远程域名。可以先使用本地入口，或稍后重新分配。"
            )
            markup = _console_back_markup("browser")
        else:
            text = (
                "🌐 <b>浏览器入口已分配</b>\n\n"
                "下面的打开按钮刚刚从通讯录生成。下次重新进入前，请先点“重新分配”。\n\n"
                f"静态入口: <code>{_html.escape(public or '未分配')}</code>\n"
                f"数据入口: <code>{_html.escape(api or public or '未分配')}</code>\n\n"
                "⚠️ 按钮链接含访问凭据，请勿转发。"
            )
            markup = _console_back_markup(
                "browser",
                InlineKeyboardButton("🌐 打开最新浏览器入口", url=url),
            )
        await _console_edit_or_reply(q, text, markup)
        return

    if action == "miniapp":
        if not public:
            text = (
                "🖥 <b>Mini App 入口暂不可用</b>\n\n"
                "当前没有在线静态入口。可以先使用本地入口，或稍后重新分配。"
            )
            markup = _console_back_markup("miniapp")
        else:
            miniapp_url = public.rstrip("/")
            if api:
                miniapp_url += "/?api=" + api.rstrip("/")
            chat_type = getattr(q.message.chat, "type", "") if q.message else ""
            if chat_type == "private":
                button = InlineKeyboardButton(
                    "🖥 打开最新 Mini App",
                    web_app=WebAppInfo(url=miniapp_url),
                )
                text = (
                    "🖥 <b>Mini App 入口已分配</b>\n\n"
                    "下面的 Mini App 按钮刚刚从通讯录生成。下次重新进入前，请先点“重新分配”。"
                )
            else:
                browser_url = console.dual_tunnel_url(public, api) if api else console.url_with_key(public)
                button = InlineKeyboardButton("🌐 打开浏览器入口", url=browser_url)
                text = (
                    "🖥 <b>Mini App 入口已分配</b>\n\n"
                    "群聊内无法直接打开 Telegram Mini App，已提供同一时刻生成的浏览器入口。"
                )
            markup = _console_back_markup("miniapp", button)
        await _console_edit_or_reply(q, text, markup)
        return

    if action == "app":
        try:
            bridge = console.connect_bridge_url()
            deeplink = console.connect_deeplink()
        except Exception:
            bridge, deeplink = "", ""
        if bridge:
            text = (
                "📱 <b>App 入口已分配</b>\n\n"
                "下面的打开按钮刚刚从通讯录生成，会携带当前数据通道与访问凭据。"
            )
            markup = _console_back_markup(
                "app",
                InlineKeyboardButton("📱 打开 Phantom Control", url=bridge),
            )
        elif deeplink:
            text = (
                "📱 <b>App 深链已分配</b>\n\n"
                f"<code>{_html.escape(deeplink)}</code>\n\n"
                "当前没有可用 HTTPS 跳板，请复制深链到系统浏览器打开。"
            )
            markup = _console_back_markup("app")
        else:
            text = (
                "📱 <b>App 入口暂不可用</b>\n\n"
                "当前没有可用数据通道。请稍后重新分配。"
            )
            markup = _console_back_markup("app")
        await _console_edit_or_reply(q, text, markup)
        return

    if action == "local":
        try:
            lan_url = console.lan_url()
        except Exception:
            lan_url = ""
        try:
            local_url = console.local_url()
        except Exception:
            local_url = ""
        lines = ["📶 <b>本地入口已分配</b>", ""]
        if lan_url:
            lines.append(f"局域网: <code>{_html.escape(lan_url)}</code>")
        else:
            lines.append("局域网: 未检测到可用私网地址")
        if local_url:
            lines.append(f"回环: <code>{_html.escape(local_url)}</code>")
        lines.extend([
            "",
            "本地入口不依赖 cloudflared；仅在同一网络或本机可用。",
            "⚠️ 链接含访问凭据，请勿转发。",
        ])
        await _console_edit_or_reply(q, "\n".join(lines), _console_back_markup("local"))
        return

    await q.answer("未知入口动作")


async def cmd_console(update: Update, ctx):
    """/console —— 发稳定入口面板；点击按钮时动态分配最新 URL。"""
    console = ctx.application.bot_data.get("console")
    if console is None:
        await update.message.reply_text(
            "控制台未启用：config.toml 加\n[webapp]\nenabled = true\nport = 8765")
        return
    await update.message.reply_text(
        _console_panel_text(console, ctx.application.bot_data["cfg"]),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_console_panel_markup())


async def cmd_app(update: Update, ctx):
    """/app —— 快速跳转 Phantom Control app（一键导入凭据）。"""
    console = ctx.application.bot_data.get("console")
    if console is None:
        await update.message.reply_text("控制台未启用，无法生成 App 链接。")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        deeplink = console.connect_deeplink()
        bridge = console.connect_bridge_url()
    except Exception:
        deeplink, bridge = "", ""

    if not deeplink:
        await update.message.reply_text("无法生成深链（API 地址不可达）。")
        return

    rows = []
    lines = ["📱 <b>Phantom Control</b>", ""]

    if bridge and update.effective_chat.type == "private":
        rows.append([InlineKeyboardButton("📱 打开 App", url=bridge)])
        lines.append("点击按钮自动导入凭据并跳转 App。")
    else:
        lines.append(f"<code>{deeplink}</code>")
        lines.append("")
        lines.append("复制链接到系统浏览器打开，或确认 App 已安装后点击。")

    lines.append("")
    lines.append("<i>凭据含访问钥，别外传。</i>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def _post_shutdown(app: Application):
    ipc = app.bot_data.get("console_ipc")
    if ipc is not None:
        try:
            await ipc.stop()
        except Exception as e:
            log.warning("console ipc stop failed: %s", e)
    console = app.bot_data.get("console")
    if console is not None:
        try:
            await console.stop()
        except Exception as e:
            log.warning("console stop failed: %s", e)
    b: BotApp = app.bot_data.get("botapp")
    if b:
        # 仅当 start() 跑完才存盘。init/bootstrap 阶段崩溃时 start() 没跑、状态全空，
        # 此时存盘会把 relay/MAGI 的 session+论坛话题抹空，下次重启重建 → 重复话题。
        if b.started:
            b.save_state()
        else:
            log.warning("post_shutdown: 启动未完成，跳过存盘（防抹空状态）")
        try:
            await b.stop_proxy()
        except Exception as e:
            log.warning("proxy shutdown failed: %s", e)
        await b.mgr.shutdown()


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "config.toml")
    if not os.path.exists(cfg_path):
        sys.exit(f"config not found: {cfg_path} (copy config.example.toml)")
    cfg = load_config(cfg_path)
    cfg["_config_path"] = os.path.abspath(cfg_path)
    cfg["_providers_path"] = os.path.join(
        os.path.dirname(os.path.abspath(cfg_path)), "providers.toml")
    owner_id = int(cfg["telegram"]["owner_id"])
    if owner_id == 0:
        log.warning("owner_id=0: 将拒绝所有人。先发消息看日志 'denied uid='")

    app = (Application.builder().token(cfg["telegram"]["token"])
           .concurrent_updates(True)
           .post_init(_post_init).post_shutdown(_post_shutdown))
    # 出站代理（可选）：socks5://… 或 http://…。bot 调用与 getUpdates 长轮询
    # 都走代理，避免国内直连 api.telegram.org 延迟抖动撞 bootstrap 超时。
    proxy = (cfg.get("telegram") or {}).get("proxy", "").strip()
    if proxy:
        log.info("using proxy for Telegram: %s", proxy)
        app = (app.proxy(proxy).get_updates_proxy(proxy)
               .connect_timeout(20.0).read_timeout(20.0)
               .get_updates_read_timeout(40.0))
    app = app.build()
    botapp = BotApp(cfg, app, owner_id)
    app.bot_data.update(cfg=cfg, owner_id=owner_id, botapp=botapp)

    # 会话 + 系统 + 转发命令
    all_cmds = {**SESSION_COMMANDS, **SYS_COMMANDS, **RELAY_COMMANDS,
                **PROMPT_COMMANDS, **SERVICE_COMMANDS}
    for name, fn in all_cmds.items():
        app.add_handler(CommandHandler(name, _owner_only(fn)))
    # LLM 命令
    for name, fn in llm.LLM_COMMANDS:
        app.add_handler(CommandHandler(name, _owner_only(fn)))
    # Mini App 控制台链接
    app.add_handler(CommandHandler("console", _owner_only(cmd_console)))
    app.add_handler(CommandHandler("app", _owner_only(cmd_app)))
    # 通用 handlers
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_unhandled_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))

    log.info("starting polling (owner_id=%s)…", owner_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
