"""webapp —— Mini App 控制台服务：aiohttp 吃 event_log.BUS，长轮询推流。

设计：
- 同进程同事件循环（bot.py post_init 里启动），无独立部署。
- 鉴权双轨：
  1) key=<HMAC(bot_token)> 查询参数——浏览器/局域网直开（/console 命令可取链接）；
  2) Telegram WebApp initData 签名校验 + user.id == owner——经 TG web_app 按钮打开时。
- 数据端点用长轮询（/api/events?since=N）：比 SSE 更耐隧道/代理折腾。
- 无公网 IP 场景：配合 cloudflared/tailscale 隧道（见 tunnel.sh），或局域网直连。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from aiohttp import web

from phantom_console.event_log import BUS, coalesce_events
from phantom_console.tasks import get_task_manager

# Wire task manager to the event bus
get_task_manager().set_bus(BUS)

log = logging.getLogger("tgclaude.webapp")

_HERE = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = _HERE
_INDEX_HTML = os.path.join(_HERE, "index.html")
_JS_DIR = os.path.join(_HERE, "js")
_CSS_DIR = os.path.join(_HERE, "css")
_ICONS_DIR = os.path.join(_HERE, "icons")
_WELLKNOWN_DIR = os.path.join(_HERE, ".well-known")

# ── JS Bundle: 合并所有 JS 文件为一个请求(减少 8 个 RTT) ──
_JS_BUNDLE_ORDER = [
    "utils.js", "renderer_engine.js", "render.js", "events.js", "sidebar.js",
    "control.js", "composer.js", "askq.js", "settings.js", "app.js", "hub.js",
]
_js_bundle_cache: str = ""
_js_bundle_etag: str = ""


def _build_js_bundle() -> tuple[str, str]:
    """拼接所有 JS 文件为单一 bundle,带 ETag 缓存。"""
    global _js_bundle_cache, _js_bundle_etag
    parts = []
    for fn in _JS_BUNDLE_ORDER:
        fp = os.path.join(_JS_DIR, fn)
        if os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                parts.append(f"// ── {fn} ──\n{f.read()}")
    body = "\n".join(parts)
    etag = hashlib.md5(body.encode()).hexdigest()[:12]
    _js_bundle_cache = body
    _js_bundle_etag = etag
    return body, etag

# /connect 跳板页：Telegram 按钮只认 http/https，本页把凭据转成
# phantom-control:// 深链并自动唤起 Phantom Control app。
# 优化：平台检测 + intent:// fallback + 加载动画 + 未安装引导 + 自动超时判断。
_CONNECT_HTML = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Phantom Control</title>
<style>
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:#0a0a0a;color:#fff;font-family:'Segoe UI',system-ui,-apple-system,'Noto Sans SC',sans-serif;
 display:flex;flex-direction:column;align-items:center;justify-content:center;
 gap:0;padding:24px;text-align:center;overflow:hidden}
.brand{margin-bottom:32px}
.w{font-weight:100;font-size:clamp(36px,12vw,72px);letter-spacing:10px;padding-left:10px;line-height:1;
 background:linear-gradient(135deg,#0172df 0%,#00d4ff 100%);-webkit-background-clip:text;
 -webkit-text-fill-color:transparent;background-clip:text}
.s{font-weight:300;letter-spacing:6px;opacity:.7;font-size:12px;margin-top:6px}
.card{background:#161616;border:1px solid #222;border-radius:16px;padding:28px 24px;
 max-width:340px;width:100%;display:flex;flex-direction:column;align-items:center;gap:16px}
.status{font-size:14px;opacity:.8;min-height:20px;transition:opacity .3s}
.spinner{width:28px;height:28px;border:3px solid #333;border-top-color:#0172df;
 border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.btn{display:inline-block;padding:14px 28px;background:linear-gradient(135deg,#0172df,#00a8e8);
 color:#fff;font-size:15px;letter-spacing:1px;text-decoration:none;font-weight:600;
 border-radius:10px;border:none;cursor:pointer;transition:transform .1s,box-shadow .2s;
 box-shadow:0 4px 20px rgba(1,114,223,.3)}
.btn:active{transform:scale(.96)}
.btn:hover{box-shadow:0 6px 28px rgba(1,114,223,.5)}
.btn-outline{background:transparent;border:1px solid #444;color:#ccc;box-shadow:none;
 font-size:13px;padding:10px 20px}
.btn-outline:hover{border-color:#0172df;color:#fff}
.hidden{display:none}
.hint{opacity:.5;font-size:11px;max-width:280px;line-height:1.6;margin-top:4px}
.row{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
</style></head><body>
<div class="brand"><div class="w">phantom</div><div class="s">CONTROL</div></div>
<div class="card">
 <div class="spinner" id="sp"></div>
 <div class="status" id="st">正在唤起应用…</div>
 <a id="go" class="btn hidden" href="__DEEPLINK__">打开 Phantom Control</a>
 <div class="row hidden" id="fallback">
  <a class="btn-outline" href="__INTENT__">系统跳转</a>
  <button class="btn-outline" onclick="copy()">复制链接</button>
 </div>
 <div class="hint hidden" id="hint">未能自动唤起？请确认已安装 Phantom Control。<br>
  也可复制深链到浏览器打开。</div>
</div>
<script>
var dl="__DEEPLINK__";
var intent="__INTENT__";
var isAndroid=/android/i.test(navigator.userAgent);
var launched=false;

function show(id){document.getElementById(id).classList.remove('hidden')}
function hide(id){document.getElementById(id).classList.add('hidden')}
function setText(id,t){document.getElementById(id).textContent=t}
function copy(){
 navigator.clipboard.writeText(dl).then(function(){setText('st','✓ 已复制到剪贴板')})
  .catch(function(){prompt('复制此链接:',dl)});
}

// 尝试唤起
function tryLaunch(){
 var t0=Date.now();
 // 监听页面失焦（app 启动会触发 blur/visibilitychange）
 function onBlur(){launched=true;hide('sp');setText('st','✓ 已跳转');cleanup()}
 window.addEventListener('blur',onBlur);
 document.addEventListener('visibilitychange',function(){
  if(document.hidden){launched=true;hide('sp');setText('st','✓ 已跳转');cleanup()}
 });
 function cleanup(){window.removeEventListener('blur',onBlur)}

 // Android: 先用 intent:// 更可靠；iOS/其它: 直接 custom scheme
 if(isAndroid && intent){
  location.href=intent;
 } else {
  var iframe=document.createElement('iframe');
  iframe.style.display='none';iframe.src=dl;document.body.appendChild(iframe);
  setTimeout(function(){if(!launched) location.href=dl;},100);
 }

 // 超时判断是否唤起成功
 setTimeout(function(){
  if(launched) return;
  hide('sp');
  setText('st','未检测到应用响应');
  show('go');show('fallback');show('hint');
 },2500);
}

setTimeout(tryLaunch,200);
</script>
</body></html>"""


def make_console_key(bot_token: str) -> str:
    """从 bot token 派生的常驻访问钥（泄露 = 能看会话流，务必只发给 owner）。"""
    return hmac.new(bot_token.encode(), b"phantom-console-v1",
                    hashlib.sha256).hexdigest()[:32]


def check_init_data(init_data: str, bot_token: str, owner_id: int) -> bool:
    """校验 Telegram WebApp initData 签名并核对 owner（官方 HMAC 流程）。"""
    try:
        data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recv_hash = data.pop("hash", "")
        if not recv_hash:
            return False
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(),
                          hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return False
        user = json.loads(data.get("user") or "{}")
        return int(user.get("id") or 0) == int(owner_id)
    except Exception:
        return False


class Console:
    def __init__(self, bot_token: str, owner_id: int, port: int = 8765, static_port: int = 0,
                 webctl: dict | None = None):
        self.bot_token = bot_token
        self.owner_id = owner_id
        self.port = port            # API 端口（需鉴权）
        self.static_port = static_port or (port + 1)  # 静态资源端口（免鉴权）
        self.key = make_console_key(bot_token)
        self.runner: web.AppRunner | None = None       # API server runner
        self.static_runner: web.AppRunner | None = None  # Static server runner
        self.botapp = None
        webctl = dict(webctl or {})
        self.webctl_enabled = bool(webctl.get("enabled", True))
        self.webctl_autostart = bool(webctl.get("autostart", True))
        self.webctl_path = Path(webctl.get(
            "path", "/home/voyager/桌面/Workspace/Platform_Phantom/WebCTL")).expanduser()
        self.webctl_port = int(webctl.get("port", 8080) or 8080)
        self.webctl_host = str(webctl.get("host", "127.0.0.1") or "127.0.0.1")
        self.webctl_url = str(
            webctl.get("url")
            or os.environ.get("WEBCTL_URL")
            or f"http://{self.webctl_host}:{self.webctl_port}"
        ).rstrip("/")
        self.webctl_pidfile = Path(webctl.get(
            "pidfile", str(Path.home() / ".config" / "phantom-console" / "webctl.pid"))).expanduser()
        self.webctl_logfile = Path(webctl.get(
            "logfile", str(Path.home() / ".config" / "phantom-console" / "webctl.log"))).expanduser()
        self.ipc_url = os.environ.get(
            "PHANTOM_CONSOLE_IPC_URL", "http://127.0.0.1:8877").rstrip("/")

    # ── 鉴权 ──────────────────────────────────────────────────────────────────
    def _authed(self, request: web.Request) -> bool:
        key = request.query.get("key") or request.headers.get("X-Console-Key", "")
        if key and hmac.compare_digest(key, self.key):
            return True
        init_data = (request.query.get("initData")
                     or request.headers.get("X-Init-Data", ""))
        if init_data:
            return check_init_data(init_data, self.bot_token, self.owner_id)
        return False

    async def _ipc_proxy(self, request: web.Request) -> web.Response:
        """Proxy bot-owned web APIs to the local bot IPC when console is external."""
        import aiohttp as _aio

        url = self.ipc_url + request.rel_url.path_qs
        headers = {"X-Console-Key": self.key}
        content_type = request.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        try:
            body = await request.read() if request.can_read_body else None
            async with _aio.ClientSession(
                timeout=_aio.ClientTimeout(total=35)
            ) as session:
                async with session.request(
                    request.method,
                    url,
                    data=body,
                    headers=headers,
                ) as resp:
                    payload = await resp.read()
                    resp_content_type = resp.headers.get("Content-Type")
                    content_type = None
                    charset = None
                    if resp_content_type:
                        parts = [p.strip() for p in resp_content_type.split(";")]
                        content_type = parts[0] or None
                        for part in parts[1:]:
                            if part.lower().startswith("charset="):
                                charset = part.split("=", 1)[1].strip() or None
                    return web.Response(
                        status=resp.status,
                        body=payload,
                        content_type=content_type,
                        charset=charset,
                    )
        except Exception as e:
            log.debug("console ipc proxy failed: %s", e)
            return web.json_response({"error": "bot 未就绪"}, status=503)

    # ── 路由 ──────────────────────────────────────────────────────────────────
    async def page(self, request: web.Request) -> web.Response:
        # 页面壳不鉴权：Telegram Mini App 打开时 initData 只在加载后的 JS 里，
        # 首个 GET 带不上任何凭据（带 key 会 403 死在门口）。HTML 本身无敏感数据，
        # 所有数据端点（/api/*）仍然严格过 key/initData。
        try:
            with open(_INDEX_HTML, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return web.Response(status=500, text="index.html missing")
        return web.Response(
            text=self._inject_runtime_config(html, self._request_origin(request), ""),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def static_js(self, request: web.Request) -> web.Response:
        """Serve JS files from web/js/ with cache headers."""
        filename = request.match_info.get("filename", "")
        if not filename or ".." in filename:
            return web.Response(status=404, text="not found")
        fp = os.path.join(_JS_DIR, filename)
        if not os.path.isfile(fp):
            return web.Response(status=404, text="not found")
        try:
            with open(fp, encoding="utf-8") as f:
                body = f.read()
            etag = hashlib.md5(body.encode()).hexdigest()[:12]
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304)
            return web.Response(text=body, content_type="application/javascript",
                                headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
        except OSError:
            return web.Response(status=500, text="read error")

    async def static_css(self, request: web.Request) -> web.Response:
        """Serve CSS files from web/css/ with cache headers."""
        filename = request.match_info.get("filename", "")
        if not filename or ".." in filename:
            return web.Response(status=404, text="not found")
        fp = os.path.join(_CSS_DIR, filename)
        if not os.path.isfile(fp):
            return web.Response(status=404, text="not found")
        try:
            with open(fp, encoding="utf-8") as f:
                body = f.read()
            etag = hashlib.md5(body.encode()).hexdigest()[:12]
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304)
            return web.Response(text=body, content_type="text/css",
                                headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
        except OSError:
            return web.Response(status=500, text="read error")

    async def bundle_js(self, request: web.Request) -> web.Response:
        """单文件 JS bundle：减少 8 个请求到 1 个（省 ~4s RTT）。"""
        global _js_bundle_cache, _js_bundle_etag
        if not _js_bundle_cache:
            _build_js_bundle()
        if request.headers.get("If-None-Match") == _js_bundle_etag:
            return web.Response(status=304)
        return web.Response(text=_js_bundle_cache, content_type="application/javascript",
                            headers={"ETag": _js_bundle_etag, "Cache-Control": "public, max-age=60"})

    # ── PWA：manifest / service worker / 图标（独立 app 安装所需，免鉴权静态资源）──
    async def manifest_file(self, request: web.Request) -> web.Response:
        """PWA manifest（app 元数据：名称/图标/独立窗口/主题色）。"""
        fp = os.path.join(_WEB_DIR, "manifest.webmanifest")
        try:
            with open(fp, encoding="utf-8") as f:
                return web.Response(text=f.read(),
                                    content_type="application/manifest+json")
        except OSError:
            return web.Response(status=404, text="not found")

    async def sw_file(self, request: web.Request) -> web.Response:
        """Service Worker —— 必须根作用域，故走 /sw.js（不放 /js 下，否则只能控 /js）。"""
        fp = os.path.join(_WEB_DIR, "sw.js")
        try:
            with open(fp, encoding="utf-8") as f:
                resp = web.Response(text=f.read(),
                                    content_type="application/javascript")
            resp.headers["Service-Worker-Allowed"] = "/"
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except OSError:
            return web.Response(status=404, text="not found")

    async def static_icon(self, request: web.Request) -> web.Response:
        """PWA 图标（PNG 二进制）。"""
        filename = request.match_info.get("filename", "")
        if not filename or ".." in filename or "/" in filename:
            return web.Response(status=404, text="not found")
        fp = os.path.join(_ICONS_DIR, filename)
        if not os.path.isfile(fp):
            return web.Response(status=404, text="not found")
        try:
            with open(fp, "rb") as f:
                return web.Response(body=f.read(), content_type="image/png")
        except OSError:
            return web.Response(status=500, text="read error")

    async def assetlinks(self, request: web.Request) -> web.Response:
        """Digital Asset Links —— TWA/Android APK 域名归属校验。
        放 web/.well-known/assetlinks.json（含 APK 签名 SHA256），不存在则 404。"""
        fp = os.path.join(_WELLKNOWN_DIR, "assetlinks.json")
        try:
            with open(fp, encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="application/json")
        except OSError:
            return web.Response(status=404, text="not found")

    async def connect_page(self, request: web.Request) -> web.Response:
        """https 跳板页：?api=&key= → phantom-control:// 深链并自动唤起 Phantom Control app。
        Telegram 内联按钮只接受 http/https，故用本页中转到自定义 scheme。
        优化：生成 intent:// fallback（Android Chrome 支持）+ 平台检测 + 超时降级。"""
        api = request.query.get("api") or ""
        key = request.query.get("key") or ""
        # disco: 备用发现端点（static 隧道），app 连不上 api 时 fallback
        public, _api = self.live_tunnel_urls()
        disco = public.rstrip("/") if (public and public.rstrip("/") != api) else ""
        disco_qs = ("&disco=" + urllib.parse.quote(disco, safe="")) if disco else ""
        deeplink = ("phantom-control://open?api=" + urllib.parse.quote(api, safe="")
                    + "&key=" + urllib.parse.quote(key, safe="") + disco_qs)
        # Android intent:// scheme: 更可靠地唤起或跳到应用商店
        intent_url = ("intent://open?api=" + urllib.parse.quote(api, safe="")
                      + "&key=" + urllib.parse.quote(key, safe="") + disco_qs
                      + "#Intent;scheme=phantom-control;package=prime.phantom.control;"
                      "category=android.intent.category.BROWSABLE;end")
        html = (_CONNECT_HTML
                .replace("__DEEPLINK__", deeplink)
                .replace("__INTENT__", intent_url))
        return web.Response(text=html, content_type="text/html")

    async def events(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            since = int(request.query.get("since", 0))
        except ValueError:
            since = 0
        wait = request.query.get("wait") == "1"
        try:
            limit = int(request.query.get("limit", 1000))
        except ValueError:
            limit = 1000
        session = request.query.get("session") or None
        if wait:
            evs = await BUS.wait(since, timeout=25.0, session=session)
        elif since == 0:
            evs = BUS.history_backlog(limit=limit, session=session)
        else:
            evs = BUS.backlog(since, limit=limit, session=session)
        evs = self._with_cc_history_backfill(evs, since, limit, session)
        # 附带 state 快照：客户端不再需要单独轮询 /api/state，省一个 RTT/请求
        resp_data: dict = {
            "seq": BUS.seq, "sessions": BUS.sessions(), "events": evs}
        if self.botapp:
            try:
                resp_data["state"] = self.botapp.web_state()
            except Exception:
                pass
        return web.json_response(resp_data)

    async def ws_events(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket event stream — lower latency alternative to long-poll."""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        session = request.query.get("session") or None
        since = int(request.query.get("since", BUS.seq))
        last_seq = since
        # Send initial backlog if client is catching up
        backlog = BUS.backlog(since, limit=500, session=session)
        if backlog:
            await ws.send_json({"events": backlog, "seq": BUS.seq})
            last_seq = BUS.seq
        try:
            while True:
                try:
                    evs = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: BUS.backlog(last_seq, limit=100, session=session)),
                        timeout=25.0)
                except asyncio.TimeoutError:
                    evs = []
                if evs:
                    await ws.send_json({"events": evs, "seq": BUS.seq})
                    last_seq = BUS.seq
                    # Inject state if botapp available
                    if self.botapp:
                        try:
                            await ws.send_json({"state": self.botapp.web_state(), "sessions": BUS.sessions()})
                        except Exception:
                            pass
                # Check for client messages (pings)
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.1)
                    if msg.type == web.WSMsgType.CLOSE:
                        break
                    if msg.type == web.WSMsgType.ERROR:
                        break
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    break
        except Exception:
            pass
        return ws

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "seq": BUS.seq})

    # ── Background tasks ────────────────────────────────────────────────────

    async def task_start(self, request: web.Request) -> web.Response:
        """POST /api/task — 提交后台任务 {"command":"...", "session":"...", "label":"..."}"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        cmd = (body.get("command") or "").strip()
        if not cmd:
            return web.json_response({"error": "missing command"}, status=400)
        session = body.get("session") or "main"
        label = body.get("label") or cmd[:60]
        cwd = body.get("cwd") or "/tmp"
        mgr = get_task_manager()
        tid = await mgr.submit(session, label, cmd, cwd)
        return web.json_response({"ok": True, "task_id": tid})

    async def task_get(self, request: web.Request) -> web.Response:
        """GET /api/task/{tid} — 单个任务状态"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        tid = request.match_info.get("tid", "")
        task = get_task_manager().get_task(tid)
        if task is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(task)

    async def tasks_list(self, request: web.Request) -> web.Response:
        """GET /api/tasks — 列出所有任务"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        session = request.query.get("session") or None
        return web.json_response({"tasks": get_task_manager().list_tasks(session)})

    async def task_kill(self, request: web.Request) -> web.Response:
        """POST /api/task/{tid}/kill — 终止运行中的任务"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        tid = request.match_info.get("tid", "")
        ok = await get_task_manager().kill(tid)
        return web.json_response({"ok": ok})

    # ── State ────────────────────────────────────────────────────────────────

    async def state(self, request: web.Request) -> web.Response:
        """会话忙闲快照（控制台顶栏 + 发送目标下拉用）。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            return web.json_response(self.botapp.web_state())
        except Exception as e:
            log.warning("state failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def send(self, request: web.Request) -> web.Response:
        """从控制台发 prompt：{"session": "main", "text": "..."}。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "空消息"}, status=400)
        if len(text) > 16000:
            return web.json_response({"error": "消息过长"}, status=400)
        session = (body.get("session") or "").strip()
        if not session:
            return web.json_response({"error": "session 必填"}, status=400)
        try:
            status = await self.botapp.web_send(session, text)
            return web.json_response({"ok": True, "status": status})
        except KeyError as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            log.warning("web send failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def stop_turn(self, request: web.Request) -> web.Response:
        """中断会话：{"session": "main"}；session 留空 = 停所有在跑的。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        session = (body.get("session") or "").strip() or None
        stopped = await self.botapp.web_stop(session)
        return web.json_response({"ok": True, "stopped": stopped})

    async def perm(self, request: web.Request) -> web.Response:
        """权限闸决策：{"token": "...", "decision": "allow|always|deny"}。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        token = (body.get("token") or "").strip()
        decision = (body.get("decision") or "").strip()
        if decision not in ("allow", "always", "deny"):
            return web.json_response({"error": "decision 须为 allow|always|deny"},
                                     status=400)
        ok = self.botapp.resolve_permission(token, decision)
        return web.json_response({"ok": ok})

    async def queue(self, request: web.Request) -> web.Response:
        """排队消息操作：{"token": "...", "action": "steer|cancel"}。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        token = (body.get("token") or "").strip()
        action = (body.get("action") or "").strip()
        if action not in ("steer", "cancel"):
            return web.json_response({"error": "action 须为 steer|cancel"}, status=400)
        toast = await self.botapp.resolve_queued(token, action)
        return web.json_response({"ok": toast is not None, "toast": toast or "该排队消息已失效"})

    # ── 🆕 AskUserQuestion web 回答 ───────────────────────────────────────────
    async def ask(self, request: web.Request) -> web.Response:
        """从 web 控制台回答 AskUserQuestion：{"token":"...", "answers":{...}}。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        token = (body.get("token") or "").strip()
        answers = body.get("answers") or {}
        if not token:
            return web.json_response({"error": "token 必填"}, status=400)
        result = await self.botapp.web_ask(token, answers)
        return web.json_response({"ok": result == "ok", "message": result})

    # ── 🆕 LLM 控制面板 ──────────────────────────────────────────────────────
    async def control_get(self, request: web.Request) -> web.Response:
        """读取某会话的控制面板设置。session 选填（默认 main）。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        session = request.query.get("session") or None
        try:
            return web.json_response(self.botapp.web_control_get(session))
        except Exception as e:
            log.warning("control_get failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def control_set(self, request: web.Request) -> web.Response:
        """修改某会话的控制面板设置：{"session":"main","key":"model","value":"sonnet"}。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        session = (body.get("session") or "").strip() or "main"
        key = (body.get("key") or "").strip()
        if not key:
            return web.json_response({"error": "key 必填"}, status=400)
        value = body.get("value")
        try:
            msg = await self.botapp.web_control_set(session, key, value)
            return web.json_response({"ok": True, "message": msg})
        except Exception as e:
            log.warning("control_set failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    # ── 🆕 待办事项 ──────────────────────────────────────────────────────────
    async def todo_get(self, request: web.Request) -> web.Response:
        """读取待办事项。session 选填（默认返回全部）。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        session = request.query.get("session") or None
        try:
            return web.json_response(self.botapp.web_todo_get(session))
        except Exception as e:
            log.warning("todo_get failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def todo_delete(self, request: web.Request) -> web.Response:
        """删除某条待办（暂未支持）。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        session = (body.get("session") or "").strip() or "main"
        index = body.get("index", -1)
        try:
            msg = self.botapp.web_todo_delete(session, index)
            ok = msg.startswith("已删除")
            return web.json_response({"ok": ok, "message": msg})
        except Exception as e:
            log.warning("todo_delete failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def resolve(self, request: web.Request) -> web.Response:
        """/api/resolve — 返回当前有效的 API base URL + key。
        App 侧定期调用此端点检测隧道是否轮换，自动更新本地缓存的 api 基址。
        鉴权：需带 key（已有 key 才能续签，防未授权探测）。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        base = self._api_base_from_request(request) or self.app_api_base()
        static_url = self._static_base_from_request(request)
        if not static_url:
            static_url, _ = self.live_tunnel_urls()
        return web.json_response({
            "api": base,
            "static": static_url.rstrip("/") if static_url else "",
            "key": self.key,
            "ts": int(time.time()),
        })

    # ── Claude Code 会话接管 ─────────────────────────────────────────────────
    _CC_PROJECTS = {
        "Workspace": os.path.expanduser("~/.claude/projects/-home-voyager----Workspace"),
        "Platform_Phantom": os.path.expanduser("~/.claude/projects/-home-voyager----Platform-Phantom"),
    }
    # 从 project 目录名反推实际 cwd
    _CC_CWDS = {
        "Workspace": "/home/voyager/桌面/Workspace",
        "Platform_Phantom": "/home/voyager/桌面/Platform_Phantom",
    }

    @staticmethod
    def _cc_msg_text(obj: dict) -> str:
        """从一行 jsonl 消息对象提取纯文本（user/assistant 通用）。"""
        msg = obj.get("message", {})
        content = msg.get("content", []) if isinstance(msg, dict) else []
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    parts.append(str(c["text"]))
            return " ".join(parts)
        return ""

    @staticmethod
    def _cc_block_text(block: dict) -> str:
        if not isinstance(block, dict):
            return ""
        if isinstance(block.get("text"), str):
            return block["text"]
        if isinstance(block.get("thinking"), str):
            return block["thinking"]
        c = block.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for item in c:
                if isinstance(item, dict):
                    t = item.get("text") or item.get("content")
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    @classmethod
    def _cc_tool_summary(cls, obj: dict, block: dict) -> str:
        result = obj.get("toolUseResult")
        if isinstance(result, dict):
            pieces = []
            stdout = str(result.get("stdout") or "").strip()
            stderr = str(result.get("stderr") or "").strip()
            if stdout:
                pieces.append(stdout)
            if stderr:
                pieces.append(stderr)
            if result.get("interrupted"):
                pieces.append("interrupted")
            return "\n".join(pieces).strip()
        return cls._cc_block_text(block).strip()

    @classmethod
    def _cc_dialog_events(cls, fp: str, n: int = 80,
                          tail_bytes: int = 4 * 1024 * 1024) -> list[dict]:
        """Read Claude Code jsonl tail and rebuild recent UI events.

        Claude Code persists one assistant turn as a chain of JSONL rows
        (thinking/tool_use/tool_result/text). Replaying only text loses the
        process pane and can split one reply into several bubbles, so this
        parser reconstructs EventBus-shaped events from the row sequence.
        """
        try:
            size = os.path.getsize(fp)
            with open(fp, "rb") as f:
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        except Exception:
            return []

        rows: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") in {"user", "assistant"}:
                rows.append(obj)

        events: list[dict] = []
        in_turn = False
        tool_names: dict[str, str] = {}
        tool_inputs: dict[str, dict] = {}

        def src(obj: dict, etype: str, **data) -> dict:
            return {
                "ts": obj.get("timestamp") or round(time.time(), 3),
                "type": etype,
                "source": "claude-code-history",
                **data,
            }

        def close_turn(obj: dict | None = None) -> None:
            nonlocal in_turn, tool_names, tool_inputs
            if not in_turn:
                return
            basis = obj or {}
            events.append(src(basis, "turn_end", status="done"))
            in_turn = False
            tool_names = {}
            tool_inputs = {}

        def content_blocks(obj: dict) -> list:
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            if isinstance(content, list):
                return content
            if isinstance(content, str):
                return [{"type": "text", "text": content}]
            return []

        for obj in rows:
            typ = obj.get("type")
            blocks = content_blocks(obj)
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in blocks
            )
            if typ == "user" and not has_tool_result:
                close_turn(obj)
                text = cls._cc_msg_text(obj).strip()
                if text:
                    events.append(src(obj, "user", text=text[:16000]))
                continue

            if typ == "assistant" and not in_turn:
                events.append(src(obj, "turn_start"))
                in_turn = True

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    text = cls._cc_block_text(block).strip()
                    if text:
                        events.append(src(obj, "thinking", text=text[:12000]))
                elif btype == "tool_use":
                    tid = str(block.get("id") or "").strip()
                    tool = str(block.get("name") or "tool")
                    tinput = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if tid:
                        tool_names[tid] = tool
                        tool_inputs[tid] = tinput
                    events.append(src(
                        obj, "tool", id=tid or f"cc-tool-{len(tool_names) + 1}",
                        tool=tool, input=tinput, phase="running",
                    ))
                elif btype == "tool_result":
                    tid = str(block.get("tool_use_id") or "").strip()
                    summary = cls._cc_tool_summary(obj, block)
                    phase = "error" if obj.get("isError") else "completed"
                    events.append(src(
                        obj, "tool", id=tid or f"cc-result-{len(tool_names) + 1}",
                        tool=tool_names.get(tid, "tool"),
                        input=tool_inputs.get(tid, {}),
                        phase=phase, summary=summary[:8000],
                    ))
                elif btype == "text":
                    text = cls._cc_block_text(block)
                    if text.strip():
                        if not in_turn:
                            events.append(src(obj, "turn_start"))
                            in_turn = True
                        events.append(src(obj, "text", text=text[:32000]))

        close_turn(rows[-1] if rows else None)
        return events[-max(1, n):]

    @classmethod
    def _cc_recent_msgs(cls, fp: str, n: int = 3, tail_bytes: int = 65536) -> list[dict]:
        """seek 文件尾部，提取最近 n 条 user/assistant 对话（role + text + ts）。
        jsonl 文件可能很大，只读尾部 tail_bytes 字节，O(1) 不全扫。"""
        try:
            size = os.path.getsize(fp)
            with open(fp, "rb") as f:
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    f.readline()  # 丢弃可能被截断的半行
                raw = f.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        msgs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue
            text = cls._cc_msg_text(obj)
            if not text.strip():
                continue
            msgs.append({"role": t, "text": text.strip()[:200],
                         "ts": obj.get("timestamp", "")})
        return msgs[-n:]

    @classmethod
    def _cc_dialog_msgs(cls, fp: str, n: int = 80, tail_bytes: int = 4 * 1024 * 1024) -> list[dict]:
        """读取 Claude Code jsonl 尾部的最近 n 条 user/assistant 文本消息。"""
        try:
            size = os.path.getsize(fp)
            with open(fp, "rb") as f:
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        msgs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("type")
            if role not in ("user", "assistant"):
                continue
            text = cls._cc_msg_text(obj).strip()
            if not text:
                continue
            msgs.append({
                "role": role,
                "text": text,
                "ts": obj.get("timestamp", ""),
            })
        return msgs[-n:]

    @staticmethod
    def _norm_cwd(cwd: str | None) -> str:
        if not cwd:
            return ""
        return os.path.realpath(os.path.expanduser(str(cwd)))

    def _cc_project_dirs(self, project: str | None = None) -> dict[str, str]:
        """Return Claude Code transcript directories, keyed by project/path label."""
        root = Path.home() / ".claude" / "projects"
        project = (project or "").strip()
        if project:
            if project in self._CC_PROJECTS:
                return {project: self._CC_PROJECTS[project]}
            candidate = root / project
            if candidate.is_dir():
                return {project: str(candidate)}
            p = Path(project).expanduser()
            if p.is_dir():
                return {project: str(p)}
            return {}

        dirs = dict(self._CC_PROJECTS)
        try:
            for p in root.iterdir():
                if p.is_dir():
                    dirs.setdefault(p.name, str(p))
        except OSError:
            pass
        return dirs

    @staticmethod
    def _cc_session_cwd(fp: str, max_lines: int = 300) -> str:
        try:
            with open(fp, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        return cwd.strip()
        except OSError:
            pass
        return ""

    def _cc_cwd_matches(self, fp: str, cwd: str | None) -> bool:
        wanted = self._norm_cwd(cwd)
        if not wanted:
            return True
        found = self._norm_cwd(self._cc_session_cwd(fp))
        return bool(found and found == wanted)

    def _cc_resolve_session_file(
        self,
        session_id: str | None = "",
        session_name: str | None = "",
        project: str | None = "",
        cwd: str | None = "",
    ) -> tuple[str, str | None]:
        """Resolve a Claude Code jsonl file by full id, cc-<id-prefix>, project dir, and cwd."""
        sid = (session_id or "").strip()
        project_dirs = self._cc_project_dirs(project)
        if not project_dirs:
            project_dirs = self._cc_project_dirs()

        if sid:
            for pdir in project_dirs.values():
                fp = os.path.join(pdir, sid + ".jsonl")
                if os.path.exists(fp) and self._cc_cwd_matches(fp, cwd):
                    return sid, fp

        prefixes = []
        if sid and len(sid) >= 8:
            prefixes.append(sid)
        name = (session_name or "").strip()
        if name.startswith("cc-") and len(name) > 3:
            prefixes.append(name[3:])

        import glob as _glob
        seen_prefixes = set()
        for prefix in prefixes:
            if len(prefix) < 8 or prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            candidates = []
            for pdir in project_dirs.values():
                for fp in _glob.glob(os.path.join(pdir, prefix + "*.jsonl")):
                    full = os.path.basename(fp).replace(".jsonl", "")
                    if full.startswith(prefix) and self._cc_cwd_matches(fp, cwd):
                        candidates.append((os.path.getmtime(fp), full, fp))
            if candidates:
                candidates.sort(reverse=True)
                _, full, fp = candidates[0]
                return full, fp
        return sid, None

    def cc_history_events_for_session(
        self,
        session_name: str,
        session_id: str | None = "",
        limit: int = 80,
        project: str | None = "",
        cwd: str | None = "",
    ) -> list[dict]:
        """Build synthetic EventBus events from a taken-over Claude Code session."""
        sid, fp = self._cc_resolve_session_file(session_id, session_name, project, cwd)
        if not fp:
            return []
        events = self._cc_dialog_events(fp, n=max(1, min(limit, 240)))
        events = coalesce_events(events)
        for ev in events:
            ev["session"] = session_name
            ev["session_id"] = sid
        base = -len(events)
        for idx, ev in enumerate(events):
            ev["seq"] = base + idx
        return events

    def _with_cc_history_backfill(
        self,
        events: list[dict],
        since: int,
        limit: int,
        session: str | None,
    ) -> list[dict]:
        """Prepend Claude Code jsonl history to first cc-* session replay."""
        if since > 0 or not session or not session.startswith("cc-"):
            return events
        if limit <= 0:
            return events
        hist_budget = min(120, max(20, limit // 3))
        session_id = ""
        cwd = ""
        if self.botapp is not None:
            try:
                for item in self.botapp.web_cc_active():
                    if item.get("name") == session:
                        session_id = item.get("session_id") or ""
                        cwd = item.get("cwd") or ""
                        break
            except Exception:
                pass
        hist = self.cc_history_events_for_session(session, session_id=session_id, limit=hist_budget, cwd=cwd)
        if not hist:
            return events
        if not events:
            return hist[-limit:]
        if events and any(ev.get("type") == "turn_start" for ev in events):
            seen = {
                ((ev.get("text") or "").strip(), ev.get("type"))
                for ev in events
                if ev.get("type") in {"user", "text"} and (ev.get("text") or "").strip()
            }
            prefix: list[dict] = []
            for ev in hist:
                sig = ((ev.get("text") or "").strip(), ev.get("type"))
                if sig in seen:
                    continue
                prefix.append(ev)
            return coalesce_events(prefix + events)
        event_budget = max(0, limit - len(hist))
        merged = hist[-limit:] if event_budget <= 0 else hist + events[-event_budget:]
        return coalesce_events(merged)

    def _scan_cc_sessions(self, project: str | None = None,
                          cwd: str | None = None) -> list[dict]:
        """扫描 Claude Code jsonl 会话文件，提取摘要（id / 首条用户消息 / 时间戳 /
        最近一条消息概要，供可接管列表卡片查找）。"""
        import glob as _glob
        results = []
        targets = self._cc_project_dirs(project)
        for pname, pdir in targets.items():
            if not os.path.isdir(pdir):
                continue
            for fp in _glob.glob(os.path.join(pdir, "*.jsonl")):
                if not self._cc_cwd_matches(fp, cwd):
                    continue
                sid = os.path.basename(fp).replace(".jsonl", "")
                title = ""
                first_ts = ""
                last_ts = ""
                try:
                    # 用 mtime 作为 last_ts 的快速近似（精确但慢的方式是读文件尾）
                    mtime = os.path.getmtime(fp)
                    last_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
                    # 只读前 30 行找 title 和 first_ts（节省 IO）
                    with open(fp, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f):
                            if i > 30:
                                break
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if not title and obj.get("type") == "user":
                                t = self._cc_msg_text(obj)
                                if t:
                                    title = t[:120]
                            ts = obj.get("timestamp", "")
                            if ts and not first_ts:
                                first_ts = ts
                            if title and first_ts:
                                break
                except Exception:
                    continue
                if not title:
                    title = f"(会话 {sid[:8]}…)"
                # 最近一条消息概要（卡片用，方便查找）
                last_msg = ""
                last_role = ""
                recent = self._cc_recent_msgs(fp, n=1)
                if recent:
                    last_role = recent[-1].get("role", "")
                    last_msg = recent[-1].get("text", "")[:100]
                transcript_cwd = self._cc_session_cwd(fp)
                results.append({
                    "id": sid,
                    "project": pname,
                    "title": title,
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                    "last_msg": last_msg,
                    "last_role": last_role,
                    "cwd": transcript_cwd or self._CC_CWDS.get(pname, ""),
                    "transcript_file": fp,
                })
        # 按最后活跃时间倒序
        results.sort(key=lambda x: x.get("last_ts", ""), reverse=True)
        return results

    async def cc_sessions(self, request: web.Request) -> web.Response:
        """GET /api/cc-sessions — 列出可接管的 Claude Code 会话历史。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        project = request.query.get("project") or None
        cwd = request.query.get("cwd") or None
        limit = int(request.query.get("limit", "50"))
        loop = asyncio.get_event_loop()
        sessions = await loop.run_in_executor(None, self._scan_cc_sessions, project, cwd)
        return web.json_response({"sessions": sessions[:limit]})

    async def cc_resume(self, request: web.Request) -> web.Response:
        """POST /api/cc-resume — 接管一个 Claude Code 会话。
        body: {"session_id": "...", "project": "...", "name": "..."}
        在 bot 里 spawn 一个新 worker，其 resume_session_id = 给定 id，cwd = 对应项目。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        session_id = (body.get("session_id") or "").strip()
        project = (body.get("project") or "").strip()
        name = (body.get("name") or "").strip()
        requested_cwd = (body.get("cwd") or "").strip()
        if not session_id:
            return web.json_response({"error": "session_id 必填"}, status=400)
        resolved_sid, transcript_file = self._cc_resolve_session_file(
            session_id=session_id,
            session_name=name,
            project=project,
            cwd=requested_cwd,
        )
        if not transcript_file:
            return web.json_response({
                "ok": False,
                "error": "找不到匹配的 Claude Code 聊天存储文件",
                "session_id": session_id,
                "project": project,
                "cwd": requested_cwd,
            }, status=404)
        session_id = resolved_sid or session_id
        cwd = self._cc_session_cwd(transcript_file) or requested_cwd or self._CC_CWDS.get(project, "")
        if not name:
            name = f"cc-{session_id[:8]}"
        try:
            result = await self.botapp.web_cc_resume(session_id, cwd, name, transcript_file=transcript_file)
            return web.json_response(result)
        except Exception as e:
            log.warning("cc_resume failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    def _cc_session_file(self, session_id: str) -> str | None:
        """按 session_id 在两个 project 目录里找对应 jsonl 文件路径。"""
        for pdir in self._CC_PROJECTS.values():
            fp = os.path.join(pdir, session_id + ".jsonl")
            if os.path.exists(fp):
                return fp
        return None

    async def cc_active(self, request: web.Request) -> web.Response:
        """GET /api/cc-active — 列出当前已接管的 cc-* worker 及其最近三条对话历史。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            actives = self.botapp.web_cc_active()  # [{name, session_id, cwd, busy, turns}]
        except Exception as e:
            log.warning("cc_active failed: %s", e)
            return web.json_response({"workers": []})
        # 为每个已接管 worker 附最近三条对话历史（读其 session 文件尾部）
        loop = asyncio.get_event_loop()
        for w in actives:
            sid, fp = self._cc_resolve_session_file(
                w.get("session_id") or "",
                w.get("name") or "",
                cwd=w.get("cwd") or "",
            )
            if sid and not w.get("session_id"):
                w["session_id"] = sid
            if fp:
                w["transcript_file"] = fp
                w["recent"] = await loop.run_in_executor(
                    None, self._cc_recent_msgs, fp, 3)
            else:
                w["recent"] = []
        return web.json_response({"workers": actives})

    async def cc_stop(self, request: web.Request) -> web.Response:
        """POST /api/cc-stop — 停止并移除一个已接管的 cc-* worker。body: {"name": "..."}"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if self.botapp is None:
            return await self._ipc_proxy(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "name 必填"}, status=400)
        try:
            result = await self.botapp.web_cc_stop(name)
            return web.json_response(result)
        except Exception as e:
            log.warning("cc_stop failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    # ── WebCTL 代理（Phantom Console → Phantom WebCTL API 桥接）──────────────
    # WebCTL 是独立 Flask 进程 (localhost:8080)，Console 鉴权后透传请求，
    # 使移动端 Mini App / 隧道用户无需直连 WebCTL 端口。
    _webctl_session: "aiohttp.ClientSession | None" = None

    @property
    def _WEBCTL_BASE(self) -> str:
        return self.webctl_url

    @staticmethod
    def _pid_running(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _read_webctl_pid(self) -> int | None:
        try:
            return int(self.webctl_pidfile.read_text().strip())
        except Exception:
            return None

    def _webctl_health_ok(self, timeout: float = 1.5) -> bool:
        for path in ("/api/build-internals/state", "/"):
            try:
                with urlopen(self.webctl_url + path, timeout=timeout) as resp:
                    if 200 <= resp.status < 500:
                        return True
            except (OSError, URLError, TimeoutError):
                continue
        return False

    async def _ensure_webctl(self) -> None:
        if not self.webctl_enabled:
            return
        if self._webctl_health_ok():
            log.info("webctl: already reachable at %s", self.webctl_url)
            return
        if not self.webctl_autostart:
            log.warning("webctl: not reachable at %s and autostart disabled", self.webctl_url)
            return
        server_py = self.webctl_path / "server.py"
        if not server_py.exists():
            log.warning("webctl: server.py not found at %s", server_py)
            return
        pid = self._read_webctl_pid()
        if self._pid_running(pid):
            log.info("webctl: pid %s exists, waiting for %s", pid, self.webctl_url)
        else:
            self.webctl_pidfile.parent.mkdir(parents=True, exist_ok=True)
            self.webctl_logfile.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.setdefault("PH_WEBCTL_PORT", str(self.webctl_port))
            env.setdefault("PYTHONUNBUFFERED", "1")
            cmd = [sys.executable, str(server_py), "--port", str(self.webctl_port)]
            log_file = open(self.webctl_logfile, "ab")
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.webctl_path),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            self.webctl_pidfile.write_text(str(proc.pid))
            log.info("webctl: started pid=%s url=%s log=%s", proc.pid, self.webctl_url, self.webctl_logfile)
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if self._webctl_health_ok(timeout=1.0):
                log.info("webctl: ready at %s", self.webctl_url)
                return
            await asyncio.sleep(0.5)
        log.warning("webctl: not ready after startup wait at %s", self.webctl_url)

    async def _webctl_proxy(self, request: web.Request) -> web.Response:
        """通用 WebCTL 反向代理：/api/webctl/{path} → WebCTL /api/{path}。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return await self._webctl_proxy_inner(request)

    async def _webctl_proxy_noauth(self, request: web.Request) -> web.Response:
        """免鉴权 WebCTL API 反代：供 iframe 内 JS fetch 使用（同域 static app）。
        路由 /webctl/api/{path} → WebCTL /api/{path}"""
        return await self._webctl_proxy_inner(request)

    async def _webctl_proxy_inner(self, request: web.Request) -> web.Response:
        import aiohttp as _aio
        if self._webctl_session is None or self._webctl_session.closed:
            self._webctl_session = _aio.ClientSession(
                timeout=_aio.ClientTimeout(total=10))
        sub = request.match_info.get("path", "")
        qs = request.query_string
        url = f"{self._WEBCTL_BASE}/api/{sub}"
        if qs:
            url += "?" + qs
        try:
            if request.method == "POST":
                body = await request.read()
                async with self._webctl_session.post(
                        url, data=body,
                        headers={"Content-Type": "application/json"}) as r:
                    data = await r.read()
                    return web.Response(body=data, status=r.status,
                                        content_type=r.content_type or "application/json")
            else:
                async with self._webctl_session.get(url) as r:
                    data = await r.read()
                    return web.Response(body=data, status=r.status,
                                        content_type=r.content_type or "application/json")
        except Exception as e:
            log.warning("webctl proxy %s failed: %s", sub, e)
            return web.json_response({"error": f"WebCTL 不可达: {e}"}, status=502)

    async def _webctl_ws_proxy(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket 反代 /webctl/ws → ws://localhost:8080/ws"""
        import aiohttp as _aio
        ws_response = web.WebSocketResponse()
        await ws_response.prepare(request)
        try:
            session = _aio.ClientSession()
            async with session.ws_connect(f"{self._WEBCTL_BASE.replace('http','ws')}/ws") as ws_upstream:
                async def _fwd_up():
                    async for msg in ws_upstream:
                        if msg.type == _aio.WSMsgType.TEXT:
                            await ws_response.send_str(msg.data)
                        elif msg.type == _aio.WSMsgType.BINARY:
                            await ws_response.send_bytes(msg.data)
                        elif msg.type in (_aio.WSMsgType.CLOSE, _aio.WSMsgType.ERROR):
                            break

                async def _fwd_down():
                    async for msg in ws_response:
                        if msg.type == web.WSMsgType.TEXT:
                            await ws_upstream.send_str(msg.data)
                        elif msg.type == web.WSMsgType.BINARY:
                            await ws_upstream.send_bytes(msg.data)
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break

                import asyncio
                await asyncio.gather(_fwd_up(), _fwd_down(), return_exceptions=True)
        except Exception as e:
            log.debug("webctl ws proxy error: %s", e)
        finally:
            try:
                await session.close()
            except Exception:
                pass
            if not ws_response.closed:
                await ws_response.close()
        return ws_response

    async def _webctl_page_proxy(self, request: web.Request) -> web.Response:
        """全页面反代 /webctl/{path} → WebCTL UI 页面。
        解决外网访问时 localhost:8080 不可达的问题：前端通过 Phantom Console
        域名访问 /webctl/... 即可打开 WebCTL 面板。
        HTML 响应中的绝对路径自动重写（/static/ → /webctl/static/ 等）。
        API app 上走正常鉴权；static app 免鉴权（同域 iframe,用户已在 webapp 内）。"""
        # static app 路由无鉴权(同域 iframe 场景), api app 需验证
        if request.app.get("_is_api") and not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return await self._webctl_page_proxy_inner(request)

    async def _webctl_page_proxy_inner(self, request: web.Request) -> web.Response:
        import aiohttp as _aio
        if self._webctl_session is None or self._webctl_session.closed:
            self._webctl_session = _aio.ClientSession(
                timeout=_aio.ClientTimeout(total=15))
        sub = request.match_info.get("path", "")
        qs = request.query_string
        url = f"{self._WEBCTL_BASE}/{sub}"
        if qs:
            url += "?" + qs
        try:
            async with self._webctl_session.get(url) as r:
                data = await r.read()
                ct = r.content_type or "text/html"
                # HTML 响应：重写路径 + 注入基路径修复脚本
                if "html" in ct and r.status == 200:
                    text = data.decode("utf-8", errors="replace")
                    # src="/static/..." → src="/webctl/static/..."
                    text = text.replace('src="/static/', 'src="/webctl/static/')
                    text = text.replace("src='/static/", "src='/webctl/static/")
                    # href="/path" 导航链接 → href="/webctl/path"
                    text = re.sub(
                        r'href="(/(?!webctl/)[a-z][a-z0-9_-]*)"',
                        r'href="/webctl\1"', text)
                    text = re.sub(
                        r"href='(/(?!webctl/)[a-z][a-z0-9_-]*)'",
                        r"href='/webctl\1'", text)
                    # 注入 <base> 标签确保相对路径正确, 加 click 拦截器防止遗漏的绝对路径跳出 iframe
                    # 移动端适配: 仅解决 WebView 中的滚动锁死和导航适配问题,
                    # 不覆盖页面自身的响应式布局规则
                    mobile_css = (
                        '<style id="webctl-mobile">'
                        # 全局导航栏(.ph-gnav)保留 — 它是移动端唯一的页面切换入口
                        # 但让它更紧凑
                        '@media(max-width:520px){'
                        '.ph-gnav{padding:0 10px;height:32px}'
                        '.ph-gnav-brand{font-size:11px;margin-right:12px}'
                        '.ph-gnav-link{padding:6px 8px;font-size:11px}'
                        '}'
                        # 核心修复: 解除 overflow:hidden 锁定
                        # nav.js 给 hub/icecc 等页面设了 inline body overflow:hidden,
                        # 在 TG WebView 中导致内容无法滚动
                        '@media(max-width:680px){'
                        'html{overflow-y:auto!important;height:auto!important}'
                        'body{overflow-y:auto!important;height:auto!important;min-height:100dvh!important}'
                        # Dashboard .app 容器: 解除固定高度让内容可滚
                        '.app{height:auto!important;min-height:100dvh!important;overflow:visible!important}'
                        '.app .main{overflow:visible!important}'
                        '.app .content{overflow:visible!important}'
                        '}'
                        '</style>'
                    )
                    inject = (
                        '<base href="/webctl/">'
                        + mobile_css +
                        '<script>'
                        'document.addEventListener("click",function(e){'
                        'var a=e.target.closest("a[href]");'
                        'if(!a)return;'
                        'var h=a.getAttribute("href");'
                        'if(h&&h.startsWith("/")&&!h.startsWith("/webctl")){'
                        'e.preventDefault();location.href="/webctl"+h;}'
                        '});'
                        '</script>'
                    )
                    text = text.replace('<head>', '<head>' + inject, 1)
                    # 内嵌脚本中的 fetch('/api/...') 也重写
                    text = text.replace("'/api/", "'/webctl/api/")
                    text = text.replace('"/api/', '"/webctl/api/')
                    text = text.replace('`/api/', '`/webctl/api/')
                    # WebSocket/EventSource: /ws → /webctl/ws
                    text = text.replace('.host}/ws', '.host}/webctl/ws')
                    text = text.replace("'/ws'", "'/webctl/ws'")
                    text = text.replace('"/ws"', '"/webctl/ws"')
                    return web.Response(text=text, status=r.status,
                                        content_type=ct)
                # JS 响应：重写路径引用
                if "javascript" in ct and r.status == 200:
                    text = data.decode("utf-8", errors="replace")
                    # 绝对路径 '/hub' → '/webctl/hub' 等
                    text = re.sub(
                        r"""(['"])/((hub|configurator|ci|build-internals|icecc|docs-viewer|static)\b)""",
                        lambda m: m.group(1) + '/webctl/' + m.group(2),
                        text)
                    # fetch('/api/...') → fetch('/webctl/api/...')
                    text = text.replace("'/api/", "'/webctl/api/")
                    text = text.replace('"/api/', '"/webctl/api/')
                    text = text.replace('`/api/', '`/webctl/api/')
                    return web.Response(text=text, status=r.status,
                                        content_type=ct)
                return web.Response(body=data, status=r.status, content_type=ct)
        except Exception as e:
            log.warning("webctl page proxy /%s failed: %s", sub, e)
            return web.Response(status=502, content_type="text/html",
                text=f"<h3>WebCTL 不可达</h3><p>{e}</p><p><a href='/'>返回</a></p>")

    async def webctl_overview(self, request: web.Request) -> web.Response:
        """GET /api/webctl/overview — 聚合 WebCTL 概要（面板用快照）。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        import aiohttp as _aio
        if self._webctl_session is None or self._webctl_session.closed:
            self._webctl_session = _aio.ClientSession(
                timeout=_aio.ClientTimeout(total=5))
        base = self._WEBCTL_BASE
        out = {"ok": False}
        try:
            async with self._webctl_session.get(f"{base}/api/overview") as r:
                if r.status == 200:
                    out = await r.json()
                    out["ok"] = True
                else:
                    out["error"] = f"status {r.status}"
        except Exception as e:
            out["error"] = str(e)
        return web.json_response(out)

    async def webctl_page(self, request: web.Request) -> web.Response:
        """GET /api/webctl/url — 返回 WebCTL 各页面地址（供前端 iframe/链接用）。
        返回相对路径 /webctl/... 走反代，外网也能访问。"""
        if not self._authed(request):
            return web.json_response({"error": "forbidden"}, status=403)
        # 用反代前缀而非 localhost，外网设备能通过同域访问
        base = "/webctl"
        return web.json_response({
            "base": base,
            "pages": {
                "dashboard": f"{base}/",
                "hub": f"{base}/hub",
                "configurator": f"{base}/configurator",
                "icecc": f"{base}/icecc",
                "docs": f"{base}/docs-viewer",
            }
        })

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        """为 API 响应注入 CORS 头 + gzip 压缩。慢隧道下压缩 = 有效拓宽带宽：
        /api/events 的事件 JSON / 初始 backlog 可达数百 KB，gzip 后常缩到 1/5~1/10。"""
        if request.method == "OPTIONS":
            return web.Response(status=204,
                headers={"Access-Control-Allow-Origin": "*",
                         "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                         "Access-Control-Allow-Headers": "X-Console-Key,X-Init-Data,Content-Type"})
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        # 浏览器都带 Accept-Encoding: gzip；enable_compression 仅在客户端支持时才压。
        # WebSocket/stream responses do not have a response body for aiohttp to compress.
        try:
            if isinstance(resp, web.WebSocketResponse) or not isinstance(resp, web.Response):
                return resp
            if resp.status in {204, 304} or getattr(resp, "body", None) is None:
                return resp
            resp.enable_compression()
        except Exception:
            pass
        return resp

    @web.middleware
    async def _static_mw(self, request: web.Request, handler):
        """静态资源也 gzip，并允许 appassets/双隧道场景跨源做发现请求。"""
        if request.method == "OPTIONS":
            return web.Response(status=204,
                headers={"Access-Control-Allow-Origin": "*",
                         "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                         "Access-Control-Allow-Headers": "X-Console-Key,X-Init-Data,Content-Type"})
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        try:
            if isinstance(resp, web.WebSocketResponse) or not isinstance(resp, web.Response):
                return resp
            if resp.status in {204, 304} or getattr(resp, "body", None) is None:
                return resp
            resp.enable_compression()
        except Exception:
            pass
        return resp

    def _request_host_parts(self, request: web.Request) -> tuple[str, int | None, str]:
        host_header = (
            request.headers.get("X-Forwarded-Host")
            or request.headers.get("Host")
            or ""
        ).strip()
        if not host_header:
            return "", None, ""
        try:
            parsed = urllib.parse.urlsplit("//" + host_header)
            return parsed.hostname or "", parsed.port, host_header
        except ValueError:
            return host_header.split(":", 1)[0], None, host_header

    def _request_origin(self, request: web.Request) -> str:
        _host, _port, host_header = self._request_host_parts(request)
        if not host_header:
            return ""
        proto = (
            request.headers.get("X-Forwarded-Proto")
            or request.headers.get("X-Forwarded-Protocol")
            or request.scheme
            or "http"
        ).split(",", 1)[0].strip() or "http"
        return f"{proto}://{host_header}".rstrip("/")

    @staticmethod
    def _format_host(host: str) -> str:
        return f"[{host}]" if ":" in host and not host.startswith("[") else host

    @staticmethod
    def _is_direct_host(host: str) -> bool:
        h = (host or "").strip().strip("[]").lower()
        if not h:
            return False
        if h == "localhost" or h.endswith(".local") or "." not in h:
            return True
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            return False
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
        return ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")

    def _api_base_from_request(self, request: web.Request) -> str:
        host, _port, _raw = self._request_host_parts(request)
        if not self._is_direct_host(host):
            return ""
        return f"http://{self._format_host(host)}:{self.port}"

    def _static_base_from_request(self, request: web.Request) -> str:
        host, _port, _raw = self._request_host_parts(request)
        if not self._is_direct_host(host):
            return ""
        return f"http://{self._format_host(host)}:{self.static_port}"

    def _inject_runtime_config(self, html: str, api_base: str, disco_base: str = "") -> str:
        script = (
            "<script>"
            f"var API_BASE = {json.dumps((api_base or '').rstrip('/'))};"
            f"var DISCO_BASE_HINT = {json.dumps((disco_base or '').rstrip('/'))};"
            f"var CONSOLE_API_PORT = {int(self.port)};"
            f"var CONSOLE_STATIC_PORT = {int(self.static_port)};"
            "</script>"
        )
        return html.replace("<!-- API_BASE -->", script)

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    # ── Hot Reload：监控 web/ 目录文件变化，通过 EventBus 推送刷新事件 ──
    _hot_task: "asyncio.Task | None" = None
    _webctl_task: "asyncio.Task | None" = None
    _hot_mtimes: dict[str, float] = {}

    async def _hot_reload_loop(self):
        """轮询 web/ 目录下所有静态资源 mtime，变化时 emit hot_reload 事件。
        客户端收到后自动热替换 CSS 或整页刷新。"""
        watch_dirs = [_WEB_DIR, _JS_DIR, _CSS_DIR]
        exts = {".html", ".js", ".css", ".json"}
        # 首次建立 mtime 快照
        for d in watch_dirs:
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not any(fn.endswith(e) for e in exts):
                    continue
                fp = os.path.join(d, fn)
                try:
                    self._hot_mtimes[fp] = os.path.getmtime(fp)
                except OSError:
                    pass
        while True:
            await asyncio.sleep(1.5)
            changed = []
            for d in watch_dirs:
                if not os.path.isdir(d):
                    continue
                for fn in os.listdir(d):
                    if not any(fn.endswith(e) for e in exts):
                        continue
                    fp = os.path.join(d, fn)
                    try:
                        mt = os.path.getmtime(fp)
                    except OSError:
                        continue
                    prev = self._hot_mtimes.get(fp)
                    if prev is None:
                        self._hot_mtimes[fp] = mt
                    elif mt != prev:
                        self._hot_mtimes[fp] = mt
                        changed.append(fn)
            if changed:
                # JS 文件变化时刷新 bundle 缓存
                if any(f.endswith(".js") for f in changed):
                    _build_js_bundle()
                # 判断变化类型：纯 CSS 变化可热替换，其余需整页刷新
                css_only = all(f.endswith(".css") for f in changed)
                BUS.emit("__system__", "hot_reload",
                         files=changed, css_only=css_only,
                         ts=round(time.time(), 3))
                log.debug("hot_reload: %s (css_only=%s)", changed, css_only)

    async def start(self) -> str:
        # ── API server (port) — 鉴权，兼做单隧道兼容 ──
        api_app = web.Application(middlewares=[self._cors_middleware])
        api_app["_is_api"] = True  # 区分 api_app / static_app（webctl 鉴权用）
        api_app.router.add_get("/", self.page)  # 单隧道兼容
        api_app.router.add_get("/manifest.webmanifest", self.manifest_file)
        api_app.router.add_get("/sw.js", self.sw_file)
        api_app.router.add_get("/icons/{filename}", self.static_icon)
        api_app.router.add_get("/.well-known/assetlinks.json", self.assetlinks)
        api_app.router.add_get("/connect", self.connect_page)
        api_app.router.add_get("/js/bundle.js", self.bundle_js)
        api_app.router.add_get("/js/{filename}", self.static_js)
        api_app.router.add_get("/css/{filename}", self.static_css)
        api_app.router.add_get("/api/events", self.events)
        api_app.router.add_get("/api/health", self.health)
        api_app.router.add_get("/api/state", self.state)
        api_app.router.add_post("/api/send", self.send)
        api_app.router.add_post("/api/stop", self.stop_turn)
        api_app.router.add_post("/api/perm", self.perm)
        api_app.router.add_post("/api/queue", self.queue)
        api_app.router.add_post("/api/task", self.task_start)
        api_app.router.add_get("/api/task/{tid}", self.task_get)
        api_app.router.add_get("/api/tasks", self.tasks_list)
        api_app.router.add_post("/api/task/{tid}/kill", self.task_kill)
        api_app.router.add_post("/api/ask", self.ask)
        api_app.router.add_get("/api/control", self.control_get)
        api_app.router.add_post("/api/control", self.control_set)
        api_app.router.add_get("/api/todo", self.todo_get)
        api_app.router.add_post("/api/todo/delete", self.todo_delete)
        api_app.router.add_get("/api/ws", self.ws_events)
        api_app.router.add_get("/api/resolve", self.resolve)
        api_app.router.add_get("/api/cc-sessions", self.cc_sessions)
        api_app.router.add_post("/api/cc-resume", self.cc_resume)
        api_app.router.add_get("/api/cc-active", self.cc_active)
        api_app.router.add_post("/api/cc-stop", self.cc_stop)
        # WebCTL 代理
        api_app.router.add_get("/api/webctl/overview", self.webctl_overview)
        api_app.router.add_get("/api/webctl/url", self.webctl_page)
        api_app.router.add_get("/api/webctl/{path:.*}", self._webctl_proxy)
        api_app.router.add_post("/api/webctl/{path:.*}", self._webctl_proxy)
        # WebCTL 全页面反代：外网通过 Phantom Console 域名直达 WebCTL UI
        api_app.router.add_get("/webctl/{path:.*}", self._webctl_page_proxy)
        api_app.router.add_get("/webctl", self._webctl_page_proxy)
        self.runner = web.AppRunner(api_app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        api_url = f"http://127.0.0.1:{self.port}"

        # ── Static server (static_port) — 免鉴权 ──
        static_app = web.Application(middlewares=[self._static_mw])
        static_app.router.add_get("/", self.static_page)
        static_app.router.add_get("/go", self.go_launch)
        static_app.router.add_get("/manifest.webmanifest", self.manifest_file)
        static_app.router.add_get("/sw.js", self.sw_file)
        static_app.router.add_get("/icons/{filename}", self.static_icon)
        static_app.router.add_get("/.well-known/assetlinks.json", self.assetlinks)
        static_app.router.add_get("/connect", self.connect_page)
        static_app.router.add_get("/api/resolve", self.resolve)  # 隧道自愈：static 也暴露
        static_app.router.add_get("/js/bundle.js", self.bundle_js)
        static_app.router.add_get("/js/{filename}", self.static_js)
        static_app.router.add_get("/css/{filename}", self.static_css)
        # WebCTL 页面反代也注册在 static app（同域 iframe 不跨域,TG WebApp 兼容）
        static_app.router.add_get("/webctl/api/{path:.*}", self._webctl_proxy_noauth)
        static_app.router.add_post("/webctl/api/{path:.*}", self._webctl_proxy_noauth)
        static_app.router.add_get("/webctl/ws", self._webctl_ws_proxy)
        static_app.router.add_get("/webctl/{path:.*}", self._webctl_page_proxy)
        static_app.router.add_get("/webctl", self._webctl_page_proxy)
        self.static_runner = web.AppRunner(static_app, access_log=None)
        await self.static_runner.setup()
        site2 = web.TCPSite(self.static_runner, "0.0.0.0", self.static_port)
        await site2.start()
        static_url = f"http://127.0.0.1:{self.static_port}"

        log.info("console: api=%s, static=%s", api_url, static_url)

        # 启动 Hot Reload 文件监控
        self._hot_task = asyncio.ensure_future(self._hot_reload_loop())
        self._webctl_task = asyncio.ensure_future(self._ensure_webctl())

        return static_url  # 返回静态 URL（人可直接打开）

    async def stop(self):
        if self._hot_task is not None:
            self._hot_task.cancel()
            self._hot_task = None
        if self._webctl_task is not None:
            self._webctl_task.cancel()
            self._webctl_task = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        if self.static_runner is not None:
            await self.static_runner.cleanup()
            self.static_runner = None

    async def static_page(self, request: web.Request) -> web.Response:
        """免鉴权页面壳：把 api_base 注入 HTML 中。"""
        api_base = request.query.get("api") or ""
        if not api_base:
            api_base = self._api_base_from_request(request)
        # fallback: URL 没带 ?api= 时(PWA 独立启动/旧链接),自动读 live API 隧道域名
        if not api_base:
            _, live_api = self.live_tunnel_urls()
            api_base = live_api.rstrip("/") if live_api else ""
        try:
            with open(_INDEX_HTML, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return web.Response(status=500, text="index.html missing")
        return web.Response(
            text=self._inject_runtime_config(html, api_base, self._request_origin(request)),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def go_launch(self, request: web.Request) -> web.Response:
        """一键启动双隧道 → 等待就绪 → 302 跳转到完整 URL。
        超时则返回重试页面。"""
        import shutil
        cf = shutil.which("cloudflared")
        if not cf:
            return web.Response(status=500, content_type="text/html",
                text="<h3>cloudflared 未安装</h3><p>安装后重试</p>")

        # 杀掉旧 cloudflared（避免端口冲突）
        try:
            os.system("pkill -f 'cloudflared tunnel.*localhost' 2>/dev/null || true")
            await asyncio.sleep(1)
        except Exception:
            pass

        api_port = self.port
        static_port = self.static_port
        tdir = os.environ.get("TMPDIR", "/tmp")

        # 起两条隧道
        try:
            api_proc = await asyncio.create_subprocess_exec(
                cf, "tunnel", "--url", f"http://localhost:{api_port}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            static_proc = await asyncio.create_subprocess_exec(
                cf, "tunnel", "--url", f"http://localhost:{static_port}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except Exception as e:
            return web.Response(status=500, content_type="text/html",
                text=f"<h3>隧道启动失败</h3><p>{e}</p><a href='/go'>重试</a>")

        # 等待域名（最多 12 秒）
        api_domain = static_domain = ""
        for _ in range(60):
            await asyncio.sleep(0.2)
            if not api_domain:
                line = (await api_proc.stdout.readline()).decode(errors="replace")
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m:
                    api_domain = m.group(0)
            if not static_domain:
                line = (await static_proc.stdout.readline()).decode(errors="replace")
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m:
                    static_domain = m.group(0)
            if api_domain and static_domain:
                break

        if api_domain and static_domain:
            target = (static_domain.rstrip("/") + "/?api="
                      + api_domain.rstrip("/") + "&key=" + self.key)
            raise web.HTTPFound(target)

        # 超时：返回重试页面
        try: api_proc.kill()
        except: pass
        try: static_proc.kill()
        except: pass
        return web.Response(status=200, content_type="text/html",
            text=('<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="2">'
                  '<style>body{font-family:sans-serif;text-align:center;padding-top:80px;'
                  'background:#131318;color:#dcdcdc}</style></head>'
                  '<body><h2>⏳ 隧道启动中…</h2><p>自动重试中</p></body></html>'))

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.static_port}/?api=http://127.0.0.1:{self.port}"

    def lan_ip(self) -> str:
        """探测本机的「局域网」IPv4：优先 192.168/10/172.16-31（私网），
        排除回环 / link-local / docker(172.17) / 代理 fake-ip(198.18)。
        主用 `hostname -I` 枚举全部网卡地址——本机挂了代理/TUN 时，连 8.8.8.8 的
        路由探测会返回 fake-ip(198.18.x)、gethostname() 只给 127.0.1.1，都拿不到真 LAN IP。"""
        import socket
        import subprocess
        cands: list[str] = []
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=3)
            cands.extend(out.stdout.split())
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            cands.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                cands.append(info[4][0])
        except Exception:
            pass

        def ok(ip: str) -> bool:
            return not (ip.startswith("127.") or ip.startswith("169.254.")
                        or ip.startswith("198.18.") or ip.startswith("172.17."))

        def score(ip: str) -> int:
            if ip.startswith("192.168."):
                return 0
            if ip.startswith("10."):
                return 1
            if ip.startswith("172."):
                try:
                    return 2 if 16 <= int(ip.split(".")[1]) <= 31 else 9
                except (ValueError, IndexError):
                    return 9
            return 9

        seen = [c for c in dict.fromkeys(cands) if ok(c)]
        seen.sort(key=score)
        return seen[0] if (seen and score(seen[0]) < 9) else ""

    def lan_url(self) -> str:
        """局域网单端口直连 URL（API 端口直接出页面 + /api/*，免跨域、免隧道）。"""
        ip = self.lan_ip()
        return f"http://{ip}:{self.port}/?key={self.key}" if ip else ""

    @staticmethod
    def _best_domain_url(contact: dict) -> str:
        """Pick the healthiest domain: online first, then lowest latency."""
        domains = [
            d for d in (contact.get("domains") or [])
            if d.get("url") and d.get("status") == "online"
        ]
        if not domains:
            return ""

        def score(item: dict) -> tuple[int, float, int]:
            try:
                latency = float(item.get("latency_ms"))
            except (TypeError, ValueError):
                latency = 999999.0
            freshness = int(item.get("last_ok") or item.get("last_checked")
                            or item.get("last_seen") or 0)
            return (latency, -freshness)

        best = min(domains, key=score)
        return str(best.get("url") or "").rstrip("/")

    def live_tunnel_urls(self) -> tuple[str, str]:
        """实时重读网络通讯录/兼容隧道域名。返回最健康的 (static_url, api_url)。"""
        import json as _json
        addressbook = os.path.expanduser("~/.config/phantom-network/addressbook.json")
        try:
            if os.path.exists(addressbook):
                d = _json.load(open(addressbook))
                contacts = d.get("contacts") or {}
                pools = d.get("pools") or {}
                static_url = self._best_domain_url(contacts.get("console.static") or {})
                api_url = self._best_domain_url(contacts.get("console.api") or {})
                static_pool = pools.get("console.static") or {}
                api_pool = pools.get("console.api") or {}
                static_online = static_pool.get("online") or []
                api_online = api_pool.get("online") or []
                if not static_url and static_online:
                    static_url = str(static_online[0] or "").rstrip("/")
                if not api_url and api_online:
                    api_url = str(api_online[0] or "").rstrip("/")
                return (static_url, api_url)
        except Exception:
            pass
        path = os.path.expanduser("~/.config/tg-cf-tunnels.json")
        try:
            if os.path.exists(path):
                d = _json.load(open(path))
                return (d.get("static_url") or "", d.get("api_url") or "")
        except Exception:
            pass
        return ("", "")

    def url_with_key(self, base: str) -> str:
        base = base.rstrip("/")
        return f"{base}/?key={self.key}"

    def dual_tunnel_url(self, static_base: str, api_base: str) -> str:
        """双隧道 URL：静态 UI 公开 + API 私有。"""
        return (static_base.rstrip("/") + "/?api="
                + api_base.rstrip("/") + "&key=" + self.key)

    # ── Phantom Control 原生 app：凭据下发（数据通道地址 + key）──
    def app_api_base(self) -> str:
        """原生 app 数据通道应连的 api 基址：双隧道→api 私有；单隧道→public；否则 LAN。"""
        public, api = self.live_tunnel_urls()
        if api:
            return api.rstrip("/")
        if public:
            return public.rstrip("/")
        ip = self.lan_ip()
        return f"http://{ip}:{self.port}" if ip else ""

    def connect_deeplink(self) -> str:
        """phantom-control://open?api=&key=&disco= —— Phantom Control app 深链。
        disco= 是备用发现端点（static 隧道），api 不可达时 app 可 fallback 到 disco 查 /api/resolve。"""
        base = self.app_api_base()
        public, _api = self.live_tunnel_urls()
        link = ("phantom-control://open?api=" + urllib.parse.quote(base, safe="")
                + "&key=" + urllib.parse.quote(self.key, safe=""))
        if public and public.rstrip("/") != base:
            link += "&disco=" + urllib.parse.quote(public.rstrip("/"), safe="")
        return link

    def connect_bridge_url(self) -> str:
        """https 跳板页 URL（TG 内联按钮用；跳板再转 phantomctl://）。无可达 host 时返回空。"""
        public, api = self.live_tunnel_urls()
        host = public or api or self.app_api_base()
        if not host:
            return ""
        apibase = self.app_api_base()
        return (host.rstrip("/") + "/connect?api=" + urllib.parse.quote(apibase, safe="")
                + "&key=" + urllib.parse.quote(self.key, safe=""))


def tunnel_hint() -> str:
    """探测可用隧道工具，给出免公网 IP 的暴露方案提示。"""
    import shutil
    lines = []
    if shutil.which("cloudflared"):
        lines.append("✅ cloudflared 已装：./tunnel.sh 一键起隧道（临时 HTTPS 域名）")
    else:
        lines.append("· cloudflared（推荐，免账号临时域名）："
                     "https://github.com/cloudflare/cloudflared/releases 下载后 ./tunnel.sh")
    if shutil.which("tailscale"):
        lines.append("✅ tailscale 已装：tailscale serve --bg <port> 拿 ts.net 私有 HTTPS")
    else:
        lines.append("· tailscale（更私密，tailnet 内可达）：https://tailscale.com/download")
    return "\n".join(lines)
