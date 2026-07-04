"""botapp —— BotApp 核心类：把 SessionManager 接到 Telegram。

职责：论坛话题管理、权限闸、worker 输出镜像、run_turn 流式驱动。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application

from phantom_llm.manager import SessionManager
from phantom_llm.session import TurnSink
from ui.ask_question import AskSession
from phantom_console.event_log import BUS
from ui.live_message import LiveMessage, tool_preview

log = logging.getLogger("tgclaude")


RELAY_SYSTEM_PROMPT = """\
你是「转发 Bot」——一个对外播报与发布的专职助手，有自己独立的 Telegram 话题。
主人在这个话题里跟你对话，你负责把通知/构建结果/公告转发到对外的群，并按需置顶。

你能用的工具（经 tool search 发现）：
- send_notification(text, target?, pin?): 发文本通知。target 不填=发给主人（本话题）；
  填某个已登记转发群的别名/ID=发到那个群。pin=true 顺手置顶。返回含 message_id。
- pin_message(message_id, target?, unpin?): 置顶/取消置顶某条已发消息。
- send_file(path, caption?, target?): 把服务器文件发到主人或某转发群。
- list_forward_targets(): 看有哪些已登记的对外转发群（别名）。

工作方式：
- 你是"对外发布"的窗口。主人让你播报什么，你就组织好措辞发到对应群。
- 构建通知要清晰：项目、结果（✅/❌）、用时、产物路径/版本，必要时附文件。
- 重要公告可置顶（pin=true）；过期的可 unpin。
- 拿不准发到哪个群就先 list_forward_targets() 看别名，或问主人。
- 发送类工具一次返回即终态，"已发送"开头即成功，绝不因为没看到回执就重发。
- 回话简洁，用中文。
"""


@dataclass
class PendingPermission:
    future: asyncio.Future
    worker: str
    tool: str
    created: float = field(default_factory=time.time)


@dataclass
class QueuedMsg:
    """会话忙时排队的一条消息。可被「插入」到当前 turn（影响决策）或取消。"""
    token: str
    text: str
    chat_id: int
    thread_id: int | None
    note_msg_id: int | None = None  # 「已排队」提示消息 id，插入/取消时回编辑
    created: float = field(default_factory=time.time)


class LlmFrontendError(RuntimeError):
    pass


class LlmFrontendBusy(LlmFrontendError):
    pass


@dataclass
class LlmFrontendRunResult:
    text: str
    events: list[dict] = field(default_factory=list)


class LlmFrontendClient:
    """Small synchronous-urllib boundary for the local LLM_Frontend API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def state(self) -> dict:
        return await asyncio.to_thread(self._request, "GET", "/state")

    async def run(self, session: str, text: str, *, autospawn: bool = True) -> str:
        result = await self.run_with_events(session, text, autospawn=autospawn)
        return result.text

    async def run_with_events(self, session: str, text: str, *,
                              autospawn: bool = True) -> LlmFrontendRunResult:
        data = await asyncio.to_thread(
            self._request,
            "POST",
            "/run",
            {"session": session, "text": text, "autospawn": autospawn, "events": True},
            900,
        )
        if not data.get("ok"):
            raise LlmFrontendError(str(data.get("error") or data))
        events = [ev for ev in (data.get("events") or []) if isinstance(ev, dict)]
        return LlmFrontendRunResult(text=str(data.get("text") or ""), events=events)

    async def run_stream(self, session: str, text: str, *,
                         autospawn: bool = True):
        url = f"{self.base_url}/run/stream"
        payload = {"session": session, "text": text, "autospawn": autospawn}
        timeout = aiohttp.ClientTimeout(total=900, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(url, json=payload) as resp:
                if resp.status == 409:
                    message = await resp.text()
                    raise LlmFrontendBusy(message or "LLM_Frontend session busy")
                if resp.status >= 400:
                    message = await resp.text()
                    raise LlmFrontendError(message or f"LLM_Frontend HTTP {resp.status}")
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        yield {"type": "note", "text": line}
                        continue
                    if isinstance(event, dict):
                        yield event

    async def interrupt(self, session: str) -> bool:
        data = await asyncio.to_thread(
            self._request, "POST", "/interrupt", {"session": session}, 10)
        return bool(data.get("ok"))

    async def resolve_permission(self, token: str, decision: str) -> bool:
        data = await asyncio.to_thread(
            self._request, "POST", "/permission", {"token": token, "decision": decision}, 30)
        return bool(data.get("ok"))

    async def answer_question(self, token: str, updated_input: dict | None) -> bool:
        data = await asyncio.to_thread(
            self._request, "POST", "/ask", {
                "token": token,
                "updated_input": updated_input,
                "cancelled": updated_input is None,
            }, 30)
        return bool(data.get("ok"))

    async def spawn_worker(self, name: str, *, mode: str | None = None,
                           provider: str | None = None, model: str | None = None,
                           resume_session_id: str | None = None,
                           system_append: str | None = None,
                           agents_set: str | None = None,
                           cwd: str | None = None) -> dict:
        data = await asyncio.to_thread(
            self._request,
            "POST",
            "/worker",
            {
                "name": name,
                "mode": mode,
                "provider": provider,
                "model": model,
                "resume_session_id": resume_session_id,
                "system_append": system_append,
                "agents_set": agents_set,
                "cwd": cwd,
            },
            30,
        )
        if not data.get("ok"):
            raise LlmFrontendError(str(data.get("error") or data))
        return data

    async def stop_worker(self, name: str) -> bool:
        data = await asyncio.to_thread(self._request, "DELETE", f"/worker/{quote(name, safe='')}", None, 30)
        return bool(data.get("ok"))

    async def compact(self, session: str) -> str:
        data = await asyncio.to_thread(
            self._request,
            "POST",
            f"/sessions/{quote(session, safe='')}/compact",
            None,
            900,
        )
        if not data.get("ok"):
            raise LlmFrontendError(str(data.get("error") or data.get("message") or data))
        return str(data.get("message") or "✓ 已压缩对话历史")

    async def set_session_model(self, session: str, model: str | None) -> str:
        data = await asyncio.to_thread(
            self._request,
            "POST",
            f"/sessions/{quote(session, safe='')}/model",
            {"model": model},
            30,
        )
        if not data.get("ok"):
            raise LlmFrontendError(str(data.get("error") or data.get("message") or data))
        return str(data.get("message") or "OK")

    async def set_mode(self, session: str, mode: str) -> str:
        data = await asyncio.to_thread(
            self._request,
            "POST",
            "/mode",
            {"session": session, "mode": mode},
            30,
        )
        if not data.get("ok"):
            raise LlmFrontendError(str(data.get("error") or data.get("message") or data))
        return f"权限模式 → {data.get('mode') or mode}"

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float = 10) -> dict:
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace") or "{}"
        except HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            message = raw
            try:
                data = json.loads(raw or "{}")
                if isinstance(data, dict):
                    message = str(data.get("error") or data.get("message") or raw)
            except json.JSONDecodeError:
                pass
            if e.code == 409:
                raise LlmFrontendBusy(message or "LLM_Frontend session busy") from e
            raise LlmFrontendError(message or f"LLM_Frontend HTTP {e.code}") from e
        except URLError as e:
            raise LlmFrontendError(f"LLM_Frontend 不可用: {e.reason}") from e
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            raise LlmFrontendError(f"LLM_Frontend 返回非 JSON: {raw[:120]}") from e


class BotApp:
    # 专用「文件接收」话题的虚拟名（非真实 worker；所有 send_file 落到此话题）
    FILE_TOPIC = "File-Receiver"
    # 转发 bot 的专职 worker 名：在论坛里有自己的话题，负责对外通知/转发调度
    RELAY = "relay"
    # MAGI 三脑复核 worker 名：内部持 MELCHIOR-1 / BALTHASAR-2 / CASPER-3 三脑做表决
    MAGI = "MAGI"

    def __init__(self, cfg: dict, app: Application, owner_id: int):
        self.cfg = cfg
        self.app = app
        self.owner_id = owner_id
        self.mgr = SessionManager(cfg)
        self.mgr.permission_cb = self._ask_permission
        self.mgr.ask_question_cb = self._ask_question
        self.mgr.notify_cb = self._mirror_worker
        self.mgr.notify_main_cb = self._post_to_main
        self.mgr.snapshot_extra = self._snapshot_extra
        self.mgr.send_file_cb = self._send_file_to_owner
        self.mgr.send_notification_cb = self._send_notification
        self.mgr.pin_cb = self._pin_message
        # orchestrator 工具用：列出已注册的转发群（cid -> 别名）
        self.mgr.list_forward_targets = lambda: dict(self.forward_groups)
        self.pending: dict[str, PendingPermission] = {}
        # 会话忙时的消息队列：session_name -> [QueuedMsg]。turn 跑完自动抽下一条续跑；
        # 期间用户可点按钮把某条「插入」到当前 turn（steer，影响决策）或取消。
        self.msg_queue: dict[str, list["QueuedMsg"]] = {}
        self._queue_index: dict[str, "QueuedMsg"] = {}  # token -> QueuedMsg（按钮回查）
        # 提示词池「编辑中」状态：(chat_id, thread_id) -> dict(owner, name|None)。
        # 命中时下一条文本消息被当作提示词正文存入，而非新一轮对话。
        self.pending_prompt: dict[tuple, dict] = {}
        # 模型动态发现缓存：每个 provider 有效期 5 分钟。菜单按它出按钮。
        from phantom_llm.model_discovery import ModelCache
        self.model_cache = ModelCache()
        # 已 finalize 的 turn 详情页残留缓存：(chat_id, thread_id|0) -> (DetailPage, expire_ts)。
        # turn 结束 60s 内，主气泡的"📄 详情"按钮仍能点开旧 turn 的滚动记录；
        # 60s 过后清掉，按钮点了就是 silent no-op（用户行为已不再期待此 turn 详情）。
        # 已收尾 turn 的详情页缓存：key=(chat,thread) → [(DetailPage, expire_ts)…]
        # 多槽：同话题可积累多张详情页，旧页按钮在 TTL 内仍可展开/收起。
        self._recent_details: dict[tuple[int, int], list] = {}
        # 进行中的 AskUserQuestion 交互：token -> AskSession；另存 worker->token 便于文本路由
        self.ask_sessions: dict[str, "AskSession"] = {}
        self._ask_by_worker: dict[str, str] = {}
        self.attached = SessionManager.ORCH
        self._mirror_live: dict[str, LiveMessage] = {}
        self._active_live: dict[str, LiveMessage] = {}
        self._frontend_dialog_dest: dict[str, tuple[int, int | None]] = {}
        self._frontend_known_sessions: set[str] = set()
        llm_frontend = dict(cfg.get("llm_frontend") or {})
        self.llm_frontend_url = str(
            llm_frontend.get("url")
            or os.environ.get("PHANTOM_LLM_FRONTEND_URL")
            or "http://127.0.0.1:8799").rstrip("/")
        self.llm_frontend = LlmFrontendClient(self.llm_frontend_url)
        self.llm_frontend_external_chat = bool(
            llm_frontend.get("external_chat", True))
        self._frontend_active: set[str] = set()
        # 论坛模式
        self.group_id = int(cfg["telegram"].get("group_chat_id", 0) or 0)
        self.forum = self.group_id != 0
        self.topics: dict[str, int | None] = {}
        self.thread2worker: dict[int, str] = {}
        self.closed_threads: set[int] = set()
        self._topic_locks: dict[str, asyncio.Lock] = {}
        # infiniproxy is managed as an independent submodule service.  The bot
        # only checks the configured port and never owns the proxy process.
        self._proxy_proc: subprocess.Popen | None = None
        self._proxy_path = cfg.get("infiniproxy", {}).get(
            "path", os.path.join(os.path.dirname(__file__), "..", "..", "infiniproxy", "proxy_server.py"))
        self._proxy_port = int(cfg.get("infiniproxy", {}).get("port", 8010))
        # start() 是否跑完。PTB initialize/bootstrap 阶段超时崩溃时 _post_init 不会执行，
        # 此时 _post_shutdown 若存盘会把好状态抹空 → 下次重启重建 relay/MAGI 另开话题。
        self.started = False
        # 文件转发目标群（接入的「其他群」）：chat_id -> 别名。
        # 这些群只作单向文件转发出口：群成员的任何消息/命令一律不响应，
        # 仅当 owner 显式指定目标时才把文件 send_document 过去。动态增删 + 持久化。
        self.forward_groups: dict[int, str] = {}
        for item in (cfg["telegram"].get("forward_group_ids") or []):
            try:
                # 配置可写裸 id，或 "id:别名"
                if isinstance(item, str) and ":" in item:
                    cid_s, alias = item.split(":", 1)
                    self.forward_groups[int(cid_s)] = alias.strip()
                else:
                    self.forward_groups[int(item)] = str(item)
            except (ValueError, TypeError):
                log.warning("bad forward_group_ids entry: %r", item)
        # MAGI 文件 IPC：~/claude/magi-inbox/*.json → MAGI worker → ~/.claude/magi-outbox/
        self._magi_inbox = os.path.expanduser("~/.claude/magi-inbox")
        self._magi_outbox = os.path.expanduser("~/.claude/magi-outbox")
        self._magi_watcher_task: asyncio.Task | None = None
        # 外部 session 握手 + 对话 IPC
        self._ext_hs_inbox = os.path.expanduser("~/.claude/ext-handshake-inbox")
        self._ext_hs_outbox = os.path.expanduser("~/.claude/ext-handshake-outbox")
        self._ext_sess_inbox = os.path.expanduser("~/.claude/ext-session-inbox")
        self._ext_sess_outbox = os.path.expanduser("~/.claude/ext-session-outbox")
        self._ext_sessions: dict[str, dict] = {}  # name → {session_id, worker, thread_id}
        self._ext_ws_state = os.path.expanduser("~/.claude/ext-sessions.json")
        self._ext_hs_watcher: asyncio.Task | None = None
        self._ext_sess_watcher: asyncio.Task | None = None

    # ── 论坛话题管理 ──────────────────────────────────────────────────────────
    async def ensure_topic(self, worker: str) -> int | None:
        if not self.forum or worker == SessionManager.ORCH:
            return None
        if worker in self.topics and self.topics[worker] is not None:
            return self.topics[worker]
        lock = self._topic_locks.setdefault(worker, asyncio.Lock())
        async with lock:
            if worker in self.topics and self.topics[worker] is not None:
                return self.topics[worker]
            try:
                if worker == self.RELAY:
                    label = "📣 Relay Bot"
                elif worker == self.MAGI:
                    label = "🧠 MAGI"
                else:
                    label = f"👷 {worker}"
                t = await self.app.bot.create_forum_topic(self.group_id, name=label)
                tid = t.message_thread_id
                self.topics[worker] = tid
                self.thread2worker[tid] = worker
                self.closed_threads.discard(tid)
                return tid
            except Exception as e:
                log.warning("create_forum_topic(%s) failed: %s", worker, e)
                return None

    async def ensure_file_topic(self) -> int | None:
        """确保「文件接收」专用话题存在，返回其 thread_id。所有 send_file 发到这里。
        该话题不对应真实 worker：其 thread 映射到 ORCH，用户在里面发消息即跟主对话说。"""
        if not self.forum:
            return None
        name = self.FILE_TOPIC
        if self.topics.get(name) is not None:
            return self.topics[name]
        lock = self._topic_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if self.topics.get(name) is not None:
                return self.topics[name]
            try:
                t = await self.app.bot.create_forum_topic(self.group_id, name="📁 File Receiver")
                tid = t.message_thread_id
                self.topics[name] = tid
                # 映射到 ORCH：在文件话题发消息 = 跟主对话说，而非"worker 不存在"
                self.thread2worker[tid] = SessionManager.ORCH
                self.closed_threads.discard(tid)
                return tid
            except Exception as e:
                log.warning("create file topic failed: %s", e)
                return None

    async def close_topic(self, worker: str):
        tid = self.topics.pop(worker, None)
        self._topic_locks.pop(worker, None)
        if tid is not None:
            self.thread2worker.pop(tid, None)
            self.closed_threads.add(tid)
            try:
                await self.app.bot.close_forum_topic(self.group_id, tid)
            except Exception:
                pass

    def _target_chat(self) -> int:
        return self.group_id if self.forum else self.owner_id

    # ── 文件转发群管理（接入的其他群，仅作单向文件出口）──────────────────────
    def is_forward_group(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self.forward_groups

    def is_control_chat(self, chat_id: int | None) -> bool:
        """是否是「主控」聊天：主控群 / owner 私聊。命令与对话只在这里生效。"""
        if chat_id is None:
            return False
        if self.forum:
            return chat_id == self.group_id
        return chat_id == self.owner_id

    def resolve_forward_target(self, key: str | None) -> int | None:
        """把目标标识(别名或裸 chat_id 字符串)解析成 chat_id；None/空 表示主控。
        未匹配返回 0（调用方据此报错，与「主控=None」区分开）。"""
        if not key:
            return None
        key = str(key).strip()
        if not key or key.lower() in ("main", "owner", "主控", "self"):
            return None
        # 别名匹配（大小写不敏感）
        for cid, alias in self.forward_groups.items():
            if alias.lower() == key.lower():
                return cid
        # 裸 chat_id
        try:
            cid = int(key)
            if cid in self.forward_groups:
                return cid
        except ValueError:
            pass
        return 0  # 未知目标

    def add_forward_group(self, chat_id: int, alias: str | None = None) -> str:
        alias = (alias or "").strip() or str(chat_id)
        self.forward_groups[int(chat_id)] = alias
        self.save_state()
        self._export_forward_groups()  # 同步导出给 CLI 工具
        return alias

    def remove_forward_group(self, key: str) -> int | None:
        """按别名或 chat_id 删除。返回被删的 chat_id，未找到返回 None。"""
        cid = self.resolve_forward_target(key)
        if cid in (None, 0):
            return None
        self.forward_groups.pop(cid, None)
        self.save_state()
        self._export_forward_groups()  # 同步导出给 CLI 工具
        return cid

    def worker_for_thread(self, thread_id: int | None) -> str:
        if thread_id is None:
            return SessionManager.ORCH
        return self.thread2worker.get(thread_id, SessionManager.ORCH)

    def target_name_for(self, update) -> str:
        if self.forum:
            tid = update.message.message_thread_id if update.message else None
            return self.worker_for_thread(tid)
        return self.attached

    def target_for(self, update):
        name = self.target_name_for(update)
        tid = (update.message.message_thread_id if (self.forum and update.message)
               else None)
        return self.mgr.get(name), name, tid

    # ── infiniproxy 生命周期 ───────────────────────────────────────────────────
    async def _start_proxy(self):
        """Check the independently managed infiniproxy service port."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            if sock.connect_ex(("localhost", self._proxy_port)) == 0:
                log.info("infiniproxy: external service detected on port %d", self._proxy_port)
                return
            log.warning("infiniproxy: external service not listening on port %d", self._proxy_port)
        except Exception:
            log.warning("infiniproxy: external service check failed on port %d", self._proxy_port)
        finally:
            sock.close()

    async def stop_proxy(self):
        """停止 infiniproxy 子进程。幂等。"""
        if self._proxy_proc is None:
            return
        log.info("infiniproxy: stopping pid=%d", self._proxy_proc.pid)
        try:
            self._proxy_proc.terminate()
            try:
                self._proxy_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proxy_proc.kill()
                self._proxy_proc.wait(timeout=3)
        except Exception as e:
            log.warning("infiniproxy: stop error: %s", e)
        self._proxy_proc = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    async def start(self):
        self.mgr.restore_settings()
        st = self.mgr.load_state()
        extra = st.get("extra") or {}
        # 先把论坛话题映射灌进内存——必须早于 start_orchestrator/restore_workers：
        # 否则恢复窗口内任何 save_state（如 orchestrator 连上拿到新 session_id 触发）
        # 会 snapshot 出空 topics 覆盖好文件，relay/MAGI 下次重启就无 thread 可复用、被重建。
        for name, tid in (extra.get("topics") or {}).items():
            if tid is not None:
                self.topics[name] = tid
                # 文件话题映射到 ORCH（非真实 worker）；其余话题映射到对应 worker
                self.thread2worker[tid] = (
                    SessionManager.ORCH if name == self.FILE_TOPIC else name)
        # 恢复持久化的转发群（与 config 静态项合并；持久化的优先，体现在线增删）
        for cid_s, alias in (extra.get("forward_groups") or {}).items():
            try:
                self.forward_groups[int(cid_s)] = alias
            except (ValueError, TypeError):
                pass
        # 启动时导出一次 forward_groups 供 CLI 工具读取
        self._export_forward_groups()
        # Check the external protocol translation proxy before worker restore.
        await self._start_proxy()
        # 整段恢复期间抑制存盘：起 orchestrator + 恢复 worker + 建话题全就绪后才落一次盘，
        # 期间任何半成品 save 都被吞掉（防覆盖）。退出 with 时自动补存一次完整状态。
        with self.mgr.suspend_saves():
            await self.mgr.start_orchestrator(
                resume_session_id=st.get("orchestrator_session_id"),
                model=st.get("orchestrator_model"))
            try:
                restored = await self.mgr.restore_workers(st)
                if restored:
                    log.info("restored workers: %s", restored)
                    await self.app.bot.send_message(
                        self._target_chat(),
                        f"♻️ 已恢复 {len(restored)} 个 worker 会话：{', '.join(restored)}")
            except Exception as e:
                log.warning("restore_workers failed: %s", e)
            # 确保「转发 bot」专职 worker 与其论坛话题就绪（幂等：话题已在内存则复用）
            try:
                await self.ensure_relay()
            except Exception as e:
                log.warning("ensure_relay failed: %s", e)
            # 确保 MAGI 三脑表决 worker 与其论坛话题就绪（幂等）
            try:
                await self.ensure_magi()
            except Exception as e:
                log.warning("ensure_magi failed: %s", e)
        # 恢复外部 session（论坛话题映射 + 重建 worker）
        try:
            await self._load_ext_sessions()
        except Exception as e:
            log.warning("load ext sessions failed: %s", e)
        # 启动 MAGI 文件 IPC watcher（CLI → MAGI 三脑审计桥接）
        if self._magi_watcher_task is None:
            self._magi_watcher_task = asyncio.create_task(self._magi_file_watcher())
            log.info("magi-ipc watcher started")
        # 启动外部 session 握手 watcher（CLI → 论坛话题直连）
        if self._ext_hs_watcher is None:
            self._ext_hs_watcher = asyncio.create_task(self._ext_handshake_watcher())
            log.info("ext-handshake watcher started")
        # 启动外部 session 对话 watcher（CLI → worker 消息路由）
        if self._ext_sess_watcher is None:
            self._ext_sess_watcher = asyncio.create_task(self._ext_session_watcher())
            log.info("ext-session watcher started")
        # 全部就绪：标记启动完成，shutdown 才允许存盘（防 init 崩溃抹空状态）
        self.started = True


    # ── 转发 bot 专职 worker（relay）──────────────────────────────────────────
    async def ensure_relay(self):
        """确保 relay worker 存在并（论坛下）有自己的话题。幂等：已存在则跳过。
        relay 负责对外通知/文件转发/置顶的调度，与主对话隔离上下文。"""
        if self.mgr.get(self.RELAY) is None:
            await self.mgr.spawn_worker(self.RELAY, system_append=RELAY_SYSTEM_PROMPT)
            log.info("relay worker spawned")
        if self.forum:
            await self.ensure_topic(self.RELAY)

    # ── MAGI 三脑表决 worker（magi）──────────────────────────────────────────
    async def ensure_magi(self):
        """确保 MAGI worker 存在并（论坛下）有自己的话题。幂等：已存在则跳过。
        MAGI 内部持 MELCHIOR-1 / BALTHASAR-2 / CASPER-3 三脑（agents_set='magi'），
        系统提示在 spawn 时固化进去——主人/其他 worker 直接 send_to_worker('MAGI', 议题)
        即可触发三脑表决。"""
        from phantom_llm.session import MAGI_SYSTEM_APPEND
        if self.mgr.get(self.MAGI) is None:
            await self.mgr.spawn_worker(
                self.MAGI,
                system_append=MAGI_SYSTEM_APPEND,
                agents_set="magi")
            log.info("MAGI worker spawned (3-brain panel)")
        if self.forum:
            await self.ensure_topic(self.MAGI)

    def _snapshot_extra(self) -> dict:
        return {
            "topics": {k: v for k, v in self.topics.items() if v is not None},
            "forward_groups": {str(k): v for k, v in self.forward_groups.items()},
        }

    def save_state(self):
        self.mgr.save_state()

    # ── MAGI 文件 IPC（CLI → MAGI 三脑审计）────────────────────────────────
    async def _magi_file_watcher(self):
        """后台异步任务：轮询 ~/.claude/magi-inbox/，发现新任务→送 MAGI→写回结果。"""
        import json, time as _time
        while True:
            try:
                os.makedirs(self._magi_inbox, exist_ok=True)
                os.makedirs(self._magi_outbox, exist_ok=True)
                for fn in sorted(os.listdir(self._magi_inbox)):
                    if not fn.endswith(".json"):
                        continue
                    fpath = os.path.join(self._magi_inbox, fn)
                    out = os.path.join(self._magi_outbox, fn)
                    try:
                        data = json.loads(open(fpath).read())
                        tid = data.get("task_id", fn.replace(".json", ""))
                        topic = data.get("topic", "").strip()
                        if not topic:
                            os.remove(fpath)
                            continue
                        magi = self.mgr.get(self.MAGI)
                        if magi is None:
                            json.dump({"task_id": tid, "error": "MAGI worker not ready"},
                                      open(out, "w"), ensure_ascii=False)
                            os.remove(fpath)
                            continue
                        if magi.busy:
                            continue  # wait for MAGI to finish current turn
                        log.info("magi-ipc: processing %s — %s", tid, topic[:60])
                        full_text = []
                        class _Sink:
                            async def on_start(self2): pass
                            async def on_text(self2, t: str): full_text.append(t)
                            async def on_event(self2, ev): pass
                            on_done = None
                        result = await magi.run(topic, _Sink())
                        report = "".join(full_text) or result or "(MAGI returned empty)"
                        json.dump({"task_id": tid, "report": report},
                                  open(out, "w"), ensure_ascii=False, indent=2)
                        os.remove(fpath)
                        log.info("magi-ipc: %s done (%d chars)", tid, len(report))
                    except Exception as e:
                        log.warning("magi-ipc: %s error — %s", fn, e)
                        try:
                            json.dump({"task_id": fn.replace(".json", ""), "error": str(e)},
                                      open(out, "w"), ensure_ascii=False)
                            os.remove(fpath)
                        except Exception:
                            pass
            except Exception as e:
                log.warning("magi-ipc watcher loop error: %s", e)
            await asyncio.sleep(3)  # poll interval

    # ── 外部 session 握手（CLI → bot 论坛话题）──────────────────────────────
    async def _ext_handshake_watcher(self):
        """后台轮询 ~/.claude/ext-handshake-inbox/，处理 up/down 请求。"""
        import json as _json
        while True:
            try:
                os.makedirs(self._ext_hs_inbox, exist_ok=True)
                os.makedirs(self._ext_hs_outbox, exist_ok=True)
                for fn in sorted(os.listdir(self._ext_hs_inbox)):
                    if not fn.endswith(".json"):
                        continue
                    fpath = os.path.join(self._ext_hs_inbox, fn)
                    out = os.path.join(self._ext_hs_outbox, fn)
                    try:
                        data = _json.loads(open(fpath).read())
                        sid = data.get("session_id", fn.replace(".json", ""))
                        name = data.get("name", "").strip()
                        action = data.get("action", "up")
                        if not name:
                            os.remove(fpath); continue

                        if action == "down":
                            self._ext_destroy_session(name)
                            _json.dump({"session_id": sid, "status": "closed"},
                                       open(out, "w"), ensure_ascii=False)
                            os.remove(fpath)
                            continue

                        # action == "up": create worker + forum topic
                        if name in self._ext_sessions:
                            existing = self._ext_sessions[name]
                            tid = existing.get("thread_id", "?")
                            _json.dump({"session_id": sid, "status": "ready",
                                        "thread_id": tid, "worker": name,
                                        "msg": "reusing existing session"},
                                       open(out, "w"), ensure_ascii=False)
                            os.remove(fpath)
                            continue

                        # Resolve system prompt: if the handshake carries one, use it;
                        # otherwise use a reasonable default for external sessions.
                        sp = data.get("system_prompt", "").strip()
                        if not sp:
                            sp = (
                                "你是 Platform_Phantom 构建系统审计与运维助手，通过 CLI 外部握手接入。\n"
                                "你的论坛话题是主人与你的专用对话通道。主人会在此派发审计/构建/调试任务。\n"
                                "你可调用 MAGI 三脑表决做深度复核：用 peer 工具 message_worker('MAGI', '议题：...')。\n"
                                "回复简洁、技术向、中文。")
                        await self.mgr.spawn_worker(name, system_append=sp)
                        await self.ensure_topic(name)
                        tid = self.topics.get(name)
                        self._ext_sessions[name] = {
                            "session_id": sid, "worker": name,
                            "thread_id": str(tid or ""),
                            "system_prompt": sp}
                        self._save_ext_sessions()
                        # Confirm handshake in the forum topic
                        if tid:
                            await self.app.bot.send_message(
                                self.group_id, f"🤝 外部 session `{name}` 已握手就绪\n"
                                f"   用途: 本地 CLI ↔ bot 论坛话题直连\n"
                                f"   session_id: `{sid}`\n"
                                f"   话题 id: #{tid}",
                                message_thread_id=tid)
                        _json.dump({"session_id": sid, "status": "ready",
                                    "thread_id": str(tid or ""), "worker": name},
                                   open(out, "w"), ensure_ascii=False)
                        log.info("ext-handshake: %s created (thread=%s)", name, tid)
                        os.remove(fpath)
                    except Exception as e:
                        log.warning("ext-handshake: %s error — %s", fn, e)
                        try:
                            _json.dump({"session_id": fn.replace(".json", ""),
                                        "status": "error", "error": str(e)},
                                       open(out, "w"), ensure_ascii=False)
                            os.remove(fpath)
                        except Exception:
                            pass
            except Exception as e:
                log.warning("ext-handshake watcher loop error: %s", e)
            await asyncio.sleep(2)

    async def _ext_session_watcher(self):
        """后台轮询 ~/.claude/ext-session-inbox/，把消息路由到对应 worker 并等回复。"""
        import json as _json
        while True:
            try:
                os.makedirs(self._ext_sess_inbox, exist_ok=True)
                os.makedirs(self._ext_sess_outbox, exist_ok=True)
                for fn in sorted(os.listdir(self._ext_sess_inbox)):
                    if not fn.endswith(".json"):
                        continue
                    fpath = os.path.join(self._ext_sess_inbox, fn)
                    tid = fn.replace(".json", "")
                    out = os.path.join(self._ext_sess_outbox, f"{tid}_reply.json")
                    try:
                        data = _json.loads(open(fpath).read())
                        name = data.get("name", "").strip()
                        prompt = data.get("prompt", "").strip()
                        if not name or not prompt:
                            os.remove(fpath); continue
                        if name not in self._ext_sessions:
                            _json.dump({"task_id": tid, "status": "error",
                                        "error": f"session '{name}' not active — run tg_handshake.py up {name} first"},
                                       open(out, "w"), ensure_ascii=False)
                            os.remove(fpath); continue
                        worker = self.mgr.get(name)
                        if worker is None or worker.busy:
                            continue  # wait for next poll

                        BUS.emit(name, "user", text=prompt[:4000])
                        log.info("ext-session: routing to %s — %s", name, prompt[:60])
                        full = []
                        class _Sink:
                            async def on_start(self2):
                                BUS.emit(name, "turn_start")
                            async def on_text(self2, t: str):
                                full.append(t)
                                BUS.emit(name, "text", text=t)
                            async def on_event(self2, ev):
                                if not isinstance(ev, dict):
                                    return
                                kind = ev.get("kind", "note")
                                BUS.emit(name, kind,
                                         **{k: v for k, v in ev.items() if k != "kind"})
                                # TodoWrite：额外补一条规范化 todo 事件，驱动控制台侧栏
                                if (kind == "tool" and ev.get("tool") == "TodoWrite"
                                        and ev.get("phase") == "running"):
                                    inp = ev.get("input") or {}
                                    todos = inp.get("todos") if isinstance(inp, dict) else None
                                    if isinstance(todos, list):
                                        from ui.live_message import _safe_todos
                                        BUS.emit(name, "todo", items=_safe_todos(todos))
                            async def on_done(self2, msg):
                                BUS.emit(name, "turn_end", status="done")
                        sink = _Sink()
                        result = await worker.run(prompt, sink)
                        reply = "".join(full) or result or "(empty)"
                        _json.dump({"task_id": tid, "status": "ok", "reply": reply},
                                   open(out, "w"), ensure_ascii=False, indent=2)
                        os.remove(fpath)
                        log.info("ext-session: %s done (%d chars)", name, len(reply))
                    except Exception as e:
                        log.warning("ext-session: %s error — %s", fn, e)
                        try:
                            _json.dump({"task_id": tid, "status": "error",
                                        "error": str(e)}, open(out, "w"), ensure_ascii=False)
                            os.remove(fpath)
                        except Exception:
                            pass
            except Exception as e:
                log.warning("ext-session watcher loop error: %s", e)
            await asyncio.sleep(2)

    def _ext_destroy_session(self, name: str):
        """关闭外部 session：停止 worker，从内存和持久化中删除。"""
        if name not in self._ext_sessions:
            return
        try:
            self.mgr.stop_worker(name)
        except Exception:
            pass
        self._ext_sessions.pop(name, None)
        self.topics.pop(name, None)
        self._save_ext_sessions()

    def _save_ext_sessions(self):
        """持久化外部 session 状态到 JSON。"""
        import json as _json
        try:
            _json.dump(self._ext_sessions,
                       open(self._ext_ws_state, "w"), ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("ext-sessions save failed: %s", e)

    async def _load_ext_sessions(self):
        """启动时恢复外部 session：重建 WorkerSession + 论坛话题映射。"""
        import json as _json
        try:
            if os.path.exists(self._ext_ws_state):
                self._ext_sessions = _json.loads(open(self._ext_ws_state).read())
                for name, info in self._ext_sessions.items():
                    # 重建 WorkerSession（若尚未被 restore_workers 恢复过）
                    if self.mgr.get(name) is None:
                        try:
                            sp = info.get("system_prompt", "").strip()
                            if not sp:
                                sp = ("你是 Platform_Phantom 构建系统审计与运维助手，通过 CLI 外部握手接入。\n"
                                      "你的论坛话题是主人与你的专用对话通道。")
                            await self.mgr.spawn_worker(name, system_append=sp)
                            log.info("ext-session: %s re-spawned", name)
                        except Exception as e:
                            log.warning("ext-session: %s re-spawn failed: %s", name, e)
                    # 恢复论坛话题映射
                    tid = int(info.get("thread_id", 0) or 0)
                    if tid:
                        self.topics[name] = tid
                        self.thread2worker[tid] = name
        except Exception as e:
            log.warning("ext-sessions load failed: %s", e)

    def _export_forward_groups(self):
        """同步导出 forward_groups 到独立 JSON 文件供 CLI 工具读取"""
        import json
        from pathlib import Path
        state_file = Path(__file__).parent.parent / "tg_forward_state.json"
        data = {"forward_groups": {v: str(k) for k, v in self.forward_groups.items()}}
        try:
            state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning(f"导出 forward_groups 失败: {e}")

    # ── 主气泡续用：把对话框结果同步进当前活动气泡（不另开新气泡）──────────────
    async def _note_to_live(self, worker: str, text: str):
        live = self._active_live.get(worker) or self._mirror_live.get(worker)
        if live is None:
            return
        try:
            await live.note(text)
        except Exception as e:
            log.debug("note_to_live skip: %s", e)

    # ── 详情按钮：私聊模式刷到对话末尾 ────────────────────────────────────────
    DETAIL_TTL = 300.0    # 收尾后详情页按钮存活时长
    DETAIL_SLOTS = 8      # 每个 (chat, thread) 最多缓存的详情页数

    def stash_recent_detail(self, chat_id: int, thread_id: int | None, detail):
        """收尾 turn 的详情页入缓存（多槽），让旧页的展开/收起按钮在 TTL 内可用。"""
        key = (chat_id, thread_id or 0)
        now = time.time()
        slots = [(d, exp) for d, exp in self._recent_details.get(key, [])
                 if exp > now and d is not detail]
        slots.append((detail, now + self.DETAIL_TTL))
        self._recent_details[key] = slots[-self.DETAIL_SLOTS:]
        # 顺手清全表过期键
        for k in list(self._recent_details):
            alive = [(d, e) for d, e in self._recent_details[k] if e > now]
            if alive:
                self._recent_details[k] = alive
            else:
                self._recent_details.pop(k, None)

    def _cached_details(self, chat_id: int, thread_id: int | None) -> list:
        """按 key 取缓存里仍存活的详情页（新→旧）。"""
        now = time.time()
        slots = self._recent_details.get((chat_id, thread_id or 0), [])
        return [d for d, exp in reversed(slots) if exp > now]

    async def bump_detail(self, callback_data: str):
        """处理 detail:bump:<chat_id>:<thread_id|0>。在对应会话的主气泡里取
        DetailPage 实例并 bump（重发到对话末尾）。
        优先从活动气泡找；找不到从 60s 残留缓存里找；都没有就 silent no-op。"""
        from ui.detail_page import parse_bump_callback
        parsed = parse_bump_callback(callback_data)
        if not parsed:
            return
        chat_id, tid = parsed
        # 先查活动气泡（_active_live + _mirror_live）
        candidates = list(self._active_live.values()) + list(self._mirror_live.values())
        for live in candidates:
            if live.chat_id != chat_id:
                continue
            if (live.thread_id or 0) != (tid or 0):
                continue
            try:
                await live.detail.bump()
            except Exception as e:
                log.debug("bump_detail (live) skip: %s", e)
            return
        # 查残留缓存（已 finalize 的 turn）：取最新一张
        for detail in self._cached_details(chat_id, tid)[:1]:
            try:
                await detail.bump()
            except Exception as e:
                log.debug("bump_detail (recent) skip: %s", e)
            return

    async def toggle_detail(self, callback_data: str, msg_id: int | None = None) -> bool:
        """处理 detail:expand|collapse:<chat_id>:<thread_id|0>。
        让对应会话的 DetailPage 切换 collapsed 状态并重渲染。
        msg_id = 按钮所在消息 id：同一 thread 积累多张详情页（桥接话题常见）时
        精确路由到被点的那张，而不是永远动最新一张。返回是否成功切换。"""
        from ui.detail_page import parse_toggle_callback
        parsed = parse_toggle_callback(callback_data)
        if not parsed:
            return False
        action, chat_id, tid = parsed
        collapsed = (action == "collapse")
        # 收集该 (chat, thread) 下所有可达的 DetailPage：活动气泡 + 残留缓存
        details = []
        candidates = list(self._active_live.values()) + list(self._mirror_live.values())
        for live in candidates:
            if live.chat_id == chat_id and (live.thread_id or 0) == (tid or 0):
                details.append(live.detail)
        details.extend(self._cached_details(chat_id, tid))
        # msg_id 精确匹配：被点的那张还在 → 只动它；不在（已被新气泡顶掉/过缓存期）
        # → 返回 False 让上层提示「已过期」，而不是误动最新一张。
        if msg_id is not None:
            details = [d for d in details if d.msg_id == msg_id]
        for d in details[:1]:
            try:
                await d.set_collapsed(collapsed)
                return True
            except Exception as e:
                log.debug("toggle_detail skip: %s", e)
        return False

    # ── 交互对话框目的地：跟着当前 turn 所在话题走 ───────────────────────────
    async def _dialog_dest(self, worker: str) -> tuple[int, int | None]:
        """权限弹窗 / AskUserQuestion 的目的地 (chat_id, thread_id)。

        对话框必须弹在「对话正在进行的话题」里：主对话(ORCH)不只在 General 跑——
        File 话题与一切未注册话题都映射到主对话，turn 的气泡留在原话题，若按
        worker 家话题路由（ORCH→None），对话框就飞到 General。这里优先取该
        worker 当前活动气泡（_active_live / _mirror_live）的位置；没有活动气泡
        （如 peer 消息驱动的 turn 在首条输出前就请求权限）才回退家话题。"""
        chat = self._target_chat()
        if not self.forum:
            return chat, None
        live = self._active_live.get(worker) or self._mirror_live.get(worker)
        if live is not None:
            return live.chat_id, live.thread_id
        if worker in self._frontend_dialog_dest:
            return self._frontend_dialog_dest[worker]
        if worker != SessionManager.ORCH:
            return chat, await self.ensure_topic(worker)
        return chat, None

    # ── 权限闸 ────────────────────────────────────────────────────────────────
    async def _ask_permission(self, worker: str, tool: str, tool_input: dict,
                              *, return_decision: bool = False) -> bool | str:
        token = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[token] = PendingPermission(future=fut, worker=worker, tool=tool)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 允许", callback_data=f"perm:{token}:allow"),
                InlineKeyboardButton("⛔ 拒绝", callback_data=f"perm:{token}:deny"),
            ],
            [InlineKeyboardButton("✅ 允许且不再问(此工具)", callback_data=f"perm:{token}:always")],
        ])
        who = "主对话" if worker == SessionManager.ORCH else f"worker «{worker}»"
        text = f"🔐 *{who}* 请求工具 `{tool}`\n{tool_preview(tool, tool_input)}"
        BUS.emit(worker, "perm", token=token, tool=tool,
                 preview=tool_preview(tool, tool_input)[:400])
        chat, tid = await self._dialog_dest(worker)
        kw = {"parse_mode": ParseMode.MARKDOWN, "reply_markup": kb}
        if tid is not None:
            kw["message_thread_id"] = tid
        msg = None
        try:
            msg = await self.app.bot.send_message(chat, text, **kw)
        except Exception:
            kw.pop("parse_mode", None)
            try:
                msg = await self.app.bot.send_message(chat, f"🔐 {who} 请求工具 {tool}", **kw)
            except Exception as e:
                log.warning("perm prompt send failed: %s", e)
        try:
            decision = await asyncio.wait_for(fut, timeout=600)
            return decision if return_decision else decision != "deny"
        except asyncio.TimeoutError:
            self.pending.pop(token, None)
            BUS.emit(worker, "perm_done", token=token, decision="timeout")
            # 收掉残留按钮：过期的弹窗点了只会回「已失效」，留着徒增困惑
            if msg is not None:
                try:
                    await self.app.bot.edit_message_text(
                        f"⌛ {who} 的工具 `{tool}` 审批超时（已拒绝）",
                        chat_id=chat, message_id=msg.message_id,
                        parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    # markdown 失败（worker 名/工具名含特殊字符）至少把键盘摘掉
                    try:
                        await self.app.bot.edit_message_reply_markup(
                            chat_id=chat, message_id=msg.message_id, reply_markup=None)
                    except Exception:
                        pass
            return "deny" if return_decision else False

    def resolve_permission(self, token: str, decision: str) -> bool:
        p = self.pending.pop(token, None)
        if not p or p.future.done():
            return False
        BUS.emit(p.worker, "perm_done", token=token, decision=decision)
        if decision == "always":
            w = self.mgr.get(p.worker)
            if w:
                w.auto_allow.add(p.tool)
        p.future.set_result(decision)
        # 把对话框结果同步进活动气泡，不另开新气泡（仅 note 一行）
        verb = {"deny": "⛔ 拒绝", "always": "✅ 永久允许", "allow": "✅ 已允许"}.get(decision, decision)
        asyncio.create_task(self._note_to_live(p.worker, f"{verb} `{p.tool}`"))
        return True

    # ── AskUserQuestion 交互 ─────────────────────────────────────────────────
    async def _ask_question(self, worker: str, tool_input: dict) -> dict | None:
        """渲染 AskUserQuestion 为 TG 按钮，收集回答，返回填好 answers 的 input。
        返回 None 表示用户取消/超时（放行但不带答案）。"""
        # 防重：同一 worker 若已有进行中的提问，先收掉旧的——否则 TG 里会同时挂
        # 两个问题对话框（旧的死按钮还在、新的又冒出来）。webapp 侧也据此关旧弹窗。
        old_token = self._ask_by_worker.get(worker)
        if old_token:
            old = self.ask_sessions.get(old_token)
            if old is not None and not old.done:
                try:
                    old._finish(cancelled=True)
                    await old._finalize_msg()
                except Exception:
                    pass
            self.ask_sessions.pop(old_token, None)
            BUS.emit(worker, "ask_question", token=old_token, phase="cancelled")
        chat, tid = await self._dialog_dest(worker)
        sess = AskSession(self.app, chat, worker, tool_input, thread_id=tid)
        self.ask_sessions[sess.token] = sess
        # 同一 worker 同时只跟一个提问交互（文本输入据此路由到正确 token）
        self._ask_by_worker[worker] = sess.token
        # 通知 web 控制台弹出问题对话框
        BUS.emit(worker, "ask_question", token=sess.token, phase="start",
                 questions=sess.questions)
        try:
            await sess.start()
            result = await asyncio.wait_for(sess.future, timeout=900)
            # 同步一行注记进活动气泡（不另开新气泡），让用户看到对话框已结束、LLM 续跑
            if result and result.get("answers"):
                summary = "、".join(f"{k[:24]}=「{v[:24]}」" for k, v in
                                    list(result["answers"].items())[:2])
                await self._note_to_live(worker, f"❓ 已回答：{summary}")
                BUS.emit(worker, "ask_question", token=sess.token, phase="answered",
                         answers=result.get("answers", {}))
            else:
                await self._note_to_live(worker, "❓ 提问未作答（已放行）")
                BUS.emit(worker, "ask_question", token=sess.token, phase="cancelled")
            return result
        except asyncio.TimeoutError:
            sess._finish(cancelled=True)
            # 把问题消息定格（去按钮），不留可点但永远「已结束」的残留键盘
            try:
                await sess._finalize_msg()
            except Exception:
                pass
            await self._note_to_live(worker, "❓ 提问超时（放行）")
            BUS.emit(worker, "ask_question", token=sess.token, phase="timeout")
            return None
        finally:
            self.ask_sessions.pop(sess.token, None)
            if self._ask_by_worker.get(worker) == sess.token:
                self._ask_by_worker.pop(worker, None)

    async def web_ask(self, token: str, answers: dict) -> str:
        """从 web 控制台回答 AskUserQuestion。返回 ok 或错误描述。"""
        sess = self.ask_sessions.get(token)
        if sess is None:
            return "该提问已结束或超时"
        try:
            # 将 web 答案填入 AskSession，再 build updated_input（含 tool_input + answers）
            sess.answers = answers or {}
            await sess._finalize_msg()  # 定格 TG 按钮消息
            result = sess._build_updated_input()
            sess.future.set_result(result)
            return "ok"
        except Exception as e:
            return f"提交失败: {e}"

    def web_control_get(self, session: str | None = None) -> dict:
        """读取会话的控制面板设置。session=None → 返回全局设置。"""
        name = session or SessionManager.ORCH
        w = self.mgr.get(name)
        # 模型
        model = w.model if w else None
        if not model:
            model = self.mgr.cfg["claude"].get("model") or "default"
        # 权限模式
        perm_mode = w.mode if (w and w.mode) else self.mgr.default_mode
        # effort
        effort = self.mgr.cfg["claude"].get("effort") or ""
        # fast mode
        fast = self.mgr.fast_mode
        # 子代理模型
        agents_set = w.agents_set if w else None
        # MCP 服务器
        mcp = []
        if w:
            if w.is_orchestrator:
                mcp = ["team", "file"]
            else:
                mcp = ["file", "peer"]
        # SKILL — 从 Claude 配置读取
        skills = self._list_skills() if w and w.is_orchestrator else []
        return {
            "session": name,
            "model": model,
            "permission_mode": perm_mode,
            "effort": effort,
            "thinking": getattr(self.mgr, "thinking_enabled", False),
            "fast_mode": fast,
            "subagent_model": agents_set or "default",
            "mcp_servers": mcp,
            "skills": skills,
            "is_orchestrator": w.is_orchestrator if w else False,
        }

    def _list_skills(self) -> list[dict]:
        """列出当前可用的 SKILL（从 SDK settings 或文件系统探测）。"""
        import glob as _glob
        skills = []
        # 项目级 .claude/skills
        cwd = self.mgr.cfg["claude"].get("cwd") or os.getcwd()
        for d in (os.path.join(cwd, ".claude", "skills"),
                  os.path.expanduser("~/.claude/skills")):
            for f in sorted(_glob.glob(os.path.join(d, "*.md")) if os.path.isdir(d) else []):
                skills.append({"name": os.path.splitext(os.path.basename(f))[0],
                               "source": "project" if cwd in f else "user"})
        return skills

    async def web_control_set(self, session: str, key: str, value) -> str:
        """从 web 控制台修改会话设置。返回 ok 或错误描述。"""
        name = session or SessionManager.ORCH
        w = self.mgr.get(name)
        if self.llm_frontend_external_chat:
            try:
                status = await self._frontend_session_status(name)
            except Exception as e:
                log.warning("frontend control lookup %s failed: %s", name, e)
                status = None
            if name != SessionManager.ORCH and status is None:
                return f"会话 '{name}' 不存在"
            try:
                if key == "model":
                    v = None if value in (None, "", "default") else str(value)
                    return await self.llm_frontend.set_session_model(name, v)
                elif key == "permission_mode":
                    v = str(value)
                    if v not in ("default", "bypassPermissions", "acceptEdits", "plan"):
                        return f"无效权限模式: {v}"
                    return await self.llm_frontend.set_mode(name, v)
                elif key == "compact":
                    return await self.llm_frontend.compact(name)
                elif key in ("fast_mode", "effort", "thinking", "subagent_model"):
                    return "该设置暂不支持外部 LLM_Frontend 会话"
                else:
                    return f"未知设置项: {key}"
            except Exception as e:
                log.warning("frontend web_control_set(%s, %s) failed: %s", name, key, e)
                return f"设置失败: {e}"
        if w is None:
            return f"会话 '{name}' 不存在"
        try:
            if key == "model":
                v = None if value in (None, "", "default") else str(value)
                return await self.mgr.set_session_model(name, v)
            elif key == "permission_mode":
                v = str(value)
                if v not in ("default", "bypassPermissions", "acceptEdits", "plan"):
                    return f"无效权限模式: {v}"
                if w.client:
                    await w.client.set_permission_mode(v)
                w.mode = v
                if w.is_orchestrator:
                    self.mgr.default_mode = v
                self.mgr.save_state()
                return f"权限模式 → {v}"
            elif key == "fast_mode":
                v = bool(value) if not isinstance(value, bool) else value
                return await self.mgr.set_fast_mode(v)
            elif key == "effort":
                v = str(value)
                # effort 需重连生效
                self.mgr.cfg["claude"]["effort"] = v
                return f"effort → {v}（下次重连生效）"
            elif key == "thinking":
                v = bool(value) if not isinstance(value, bool) else value
                self.mgr.thinking_enabled = v
                return f"thinking → {'开' if v else '关'}（下次重连生效）"
            elif key == "subagent_model":
                # 子代理预设集（default / scout / magi …）。connect 时随 options 带入，
                # 故改完需重连才换上新的子代理集合。
                v = None if value in (None, "", "default") else str(value)
                w.agents_set = v
                self.mgr.save_state()
                return f"子代理集 → {v or 'default'}（下次重连生效）"
            elif key == "compact":
                return await w.compact()
            else:
                return f"未知设置项: {key}"
        except Exception as e:
            log.warning("web_control_set(%s, %s) failed: %s", name, key, e)
            return f"设置失败: {e}"

    def _latest_todos_map(self) -> dict[str, list[dict]]:
        """从事件总线派生每个 session 的「最新」待办快照（todo 事件 / 带 todos 的
        TodoWrite 工具事件，后到者覆盖）。EventBus 即真相源，无需另存一份。"""
        out: dict[str, list[dict]] = {}
        for ev in BUS.backlog(0):
            t = ev.get("type")
            s = ev.get("session", "main")
            if t == "todo":
                out[s] = ev.get("items", []) or []
            elif t == "tool":
                inp = ev.get("input")
                if isinstance(inp, dict) and isinstance(inp.get("todos"), list):
                    norm = []
                    for it in inp["todos"]:
                        if not isinstance(it, dict):
                            continue
                        norm.append({
                            "text": (it.get("content") or it.get("activeForm")
                                     or it.get("subject") or it.get("description") or ""),
                            "status": (it.get("status") or it.get("state")
                                       or "pending").lower(),
                        })
                    out[s] = norm
        return out

    def web_todo_get(self, session: str | None = None) -> dict:
        """读取待办事项。session=None → 返回所有 session 的最新快照。"""
        m = self._latest_todos_map()
        if session:
            return {"session": session, "todos": m.get(session, [])}
        return {"todos_by_session": m}

    def web_todo_delete(self, session: str, index: int) -> str:
        """删除某 session 的某条 todo：从最新快照移除该项并广播更新事件，
        所有连着的控制台随之同步刷新侧栏。"""
        name = session or SessionManager.ORCH
        items = list(self._latest_todos_map().get(name, []))
        if not (isinstance(index, int) and 0 <= index < len(items)):
            return "索引越界或该待办已不存在"
        removed = items.pop(index)
        BUS.emit(name, "todo", items=items)  # 广播：webapp 收到后刷新侧栏
        return f"已删除：{str(removed.get('text', ''))[:30]}"

    async def resolve_ask(self, token: str, action: str, idx: int) -> bool:
        sess = self.ask_sessions.get(token)
        if not sess:
            return False
        await sess.on_button(action, idx)
        return True

    def active_ask_for_thread(self, thread_id: int | None) -> "AskSession | None":
        """该话题/会话当前是否有进行中的提问（用于把文本消息当作自定义答案）。
        论坛下额外比对话题：提问贴在哪个话题，只认那个话题里的文本——
        General 与 File 话题同样映射到主对话，不做比对的话在别的话题随口一句
        会被吞成「自定义答案」。"""
        worker = self.worker_for_thread(thread_id) if self.forum else self.attached
        token = self._ask_by_worker.get(worker)
        sess = self.ask_sessions.get(token) if token else None
        if sess is None:
            return None
        if self.forum and (sess.thread_id or 0) != (thread_id or 0):
            return None
        return sess

    # ── worker 输出镜像 ───────────────────────────────────────────────────────
    async def _retire_mirror(self, worker: str, status: str | None = None):
        """收尾并移除某 worker 的镜像 live：停心跳 + 最后刷一次，避免心跳任务泄漏
        持续编辑过期消息。"""
        live = self._mirror_live.pop(worker, None)
        if live is not None:
            try:
                await live.finalize(status=status)
            except Exception as e:
                log.debug("retire mirror %s: %s", worker, e)

    async def _mirror_worker(self, worker: str, kind: str, text: str):
        chat = self._target_chat()
        # start/done/close 是纯收尾事件，不需要话题——避免「close 时反而先建
        # 一个话题再立刻关掉」的无谓 API 调用与闪现话题。
        if kind == "start":
            await self._retire_mirror(worker)
            return
        if kind == "done":
            # worker 一轮跑完：把统计写进镜像气泡并定格（否则心跳会无限期续编辑）
            live = self._mirror_live.get(worker)
            if live is not None and text is not None and not isinstance(text, str):
                live.set_result(text)
            await self._retire_mirror(worker)
            return
        if kind == "close":
            await self._retire_mirror(worker)
            if self.forum:
                await self.close_topic(worker)
            return
        tid = await self.ensure_topic(worker) if self.forum else None
        if kind == "spawn" and worker not in self._mirror_live:
            kw = {"message_thread_id": tid} if tid is not None else {}
            await self.app.bot.send_message(chat, f"👷 «{worker}» {text}", **kw)
            return
        live = self._mirror_live.get(worker)
        if live is None:
            prefix = "" if self.forum else f"👷 «{worker}» › "
            gid = self.group_id if self.forum else None
            live = LiveMessage(self.app, chat, prefix=prefix, thread_id=tid,
                               group_id=gid, session_label=worker)
            self._mirror_live[worker] = live
            live.start_heartbeat()  # 后台 worker 镜像也要心跳，否则末段文本卡 dirty
        if kind == "event":
            await live.event(text)
        else:
            await live.append(text)

    # ── LLM 发文件给主人（或转发到指定群）────────────────────────────────────
    async def _send_file_to_owner(self, path: str, caption: str = "",
                                  target: str | None = None):
        """发文件。target=None/空 → 主控（论坛下落 relay 专属话题）；
        否则解析为某个转发群的 chat_id，文件单向发到那个群。"""
        # 纯文本 caption：path 与调用方给的 caption 都可能含 _ * ` 等会破坏 Markdown
        # 实体的字符（曾导致 send 抛 BadRequest），文件说明本就是装饰性，不用解析。
        cap = path + (f"\n{caption}" if caption else "")
        chat, tid = await self._resolve_dest(target)
        kw = {"message_thread_id": tid} if tid is not None else {}
        # ── 文件传输超时策略 ──
        # 底线: 30MB 文件在慢上行链路（1Mbps≈0.125MB/s）上约需 240s。
        # 公式按 ~0.2MB/s 极悲观速率估算 write_timeout（上限 900s）。
        # connect/pool/read 也等比放大，避免大文件在 pool 排队时 connect 先超时。
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        # 基准: 每 256KB 给 1 秒 → ~0.25MB/s；最小 90s，最大 900s
        write_to = max(90.0, min(900.0, size / (256 * 1024)))
        # connect/pool 也随文件大小放大，至少 60s，至多 300s
        conn_to = max(60.0, min(300.0, size / (1024 * 1024)))
        with open(path, "rb") as fh:
            await self.app.bot.send_document(
                chat, document=fh,
                filename=os.path.basename(path),
                caption=cap[:1024],
                write_timeout=write_to, read_timeout=write_to,
                connect_timeout=conn_to, pool_timeout=conn_to, **kw)

    # ── 解析「发送目标」→ (chat_id, message_thread_id) ───────────────────────
    async def _resolve_dest(self, target: str | None) -> tuple[int, int | None]:
        """把 target 标识解析为 (chat_id, thread_id)。
        None/main → 主控（论坛下落 relay 专属话题）；别名/id → 对应转发群（无话题）。
        未知目标抛 ValueError。"""
        if target:
            cid = self.resolve_forward_target(target)
            if cid == 0:
                known = "、".join(self.forward_groups.values()) or "(无)"
                raise ValueError(f"未知转发目标 '{target}'；已注册: {known}")
            if cid is not None:
                return cid, None  # 转发群：直接发到群，无话题
        # 主控：论坛模式落到 relay 专属话题，否则 owner 私聊
        chat = self._target_chat()
        if self.forum:
            tid = await self.ensure_topic(self.RELAY)
            return chat, tid
        return chat, None

    # ── 发文本通知（转发群/主控/relay 话题）──────────────────────────────────
    async def _send_notification(self, text: str, target: str | None = None) -> int:
        """把一段文本通知发到目标，返回 message_id（供置顶用）。纯文本，避免解析坑。"""
        chat, tid = await self._resolve_dest(target)
        kw = {"message_thread_id": tid} if tid is not None else {}
        msg = await self.app.bot.send_message(chat, text[:4096], **kw)
        return msg.message_id

    # ── 置顶 / 取消置顶 ───────────────────────────────────────────────────────
    async def _pin_message(self, target: str | None, message_id: int,
                           unpin: bool = False) -> str:
        chat, _tid = await self._resolve_dest(target)
        try:
            if unpin:
                await self.app.bot.unpin_chat_message(chat, message_id=message_id)
                return f"已取消置顶 (msg {message_id})"
            await self.app.bot.pin_chat_message(
                chat, message_id=message_id, disable_notification=False)
            return f"已置顶 (msg {message_id})"
        except Exception as e:
            return f"置顶失败: {e}（bot 可能无置顶权限）"

    # ── run_turn：流式驱动一轮对话 ────────────────────────────────────────────
    async def run_turn(self, prompt: str, chat_id: int, thread_id: int | None = None):
        # 转发群是「只出文件」的单向出口：不在这里跑对话。普通消息直接静默忽略，
        # 不回怼（否则 owner 在群里随便说句话都被刷屏）。要对话请去主控群/私聊。
        if self.is_forward_group(chat_id):
            return
        if self.forum:
            if thread_id is not None and thread_id in self.closed_threads:
                await self.app.bot.send_message(
                    chat_id, "⚠️ 该 worker 已关闭，此话题不再接收消息。请到 General 话题跟主对话说。",
                    message_thread_id=thread_id)
                return
            target_name = self.worker_for_thread(thread_id)
        else:
            target_name = self.attached
        out_thread = thread_id if self.forum else None
        if self.llm_frontend_external_chat:
            target_name = await self._resolve_frontend_target_name(
                target_name, chat_id, out_thread)
            if await self._frontend_session_busy(target_name):
                await self._enqueue_message(target_name, prompt, chat_id, out_thread)
            else:
                await self._execute_frontend_turn(target_name, prompt, chat_id, out_thread)
            return
        target = self.mgr.get(target_name)
        if target is None:
            # 回退主对话接管，但回复仍留在用户发消息的话题——不丢去 General。
            # target_name 也要一并切到 ORCH：busy 判定、活动气泡、消息队列都按
            # 名字索引，沿用旧名字会把 ORCH 的 turn 挂在不存在的会话名下。
            await self.app.bot.send_message(
                chat_id, f"⚠️ 目标 «{target_name}» 不存在，本话题改由主对话接管",
                message_thread_id=out_thread)
            target = self.mgr.orchestrator
            target_name = SessionManager.ORCH
            if not self.forum:
                self.attached = SessionManager.ORCH
        if target.busy or target_name in self._active_live:
            # 不再弹「繁忙」：把消息排队（turn 跑完自动续跑），并给出「插入当前对话
            # (影响决策)」与「取消」按钮。插入=steer，把这条灌进进行中的 turn。
            await self._enqueue_message(target_name, prompt, chat_id, out_thread)
            return
        await self._execute_turn(target, target_name, prompt, chat_id, out_thread)

    async def _execute_frontend_turn(self, target_name: str, prompt: str,
                                     chat_id: int, out_thread: int | None):
        """Drive core chat through LLM_Frontend and keep Telegram UI minimal."""
        prefix = "" if (target_name == SessionManager.ORCH or self.forum) else f"👷 «{target_name}» › "
        self._frontend_active.add(target_name)
        BUS.emit(target_name, "user", text=prompt[:4000])
        BUS.emit(target_name, "turn_start")
        kw = {"message_thread_id": out_thread} if out_thread is not None else {}
        msg = None
        turn_status = "error"
        final_text = ""
        emitted_text = False

        def emit_frontend_event(ev: dict) -> str:
            nonlocal final_text, emitted_text
            etype = str(ev.get("type") or ev.get("kind") or "").strip()
            if etype == "notice":
                etype = "note"
            if etype == "turn_start":
                return etype
            if etype == "final":
                final_text = str(ev.get("text") or "")
                return etype
            if etype == "text":
                chunk = str(ev.get("text") or "")
                if chunk:
                    emitted_text = True
                    final_text += chunk
                    BUS.emit(target_name, "text", text=chunk)
                return etype
            if etype in {"tool", "thinking", "note", "todo", "subagent_text", "result", "perm_done"}:
                payload = {k: v for k, v in ev.items() if k not in {"type", "kind"}}
                if etype == "note" and "text" in payload:
                    payload["text"] = str(payload["text"])[:2000]
                BUS.emit(target_name, etype, **payload)
            return etype

        async def handle_frontend_permission(ev: dict) -> None:
            token = str(ev.get("token") or "")
            tool = str(ev.get("tool") or "tool")
            tool_input = ev.get("input") if isinstance(ev.get("input"), dict) else {}
            decision = str(await self._ask_permission(
                target_name, tool, tool_input, return_decision=True))
            if token:
                await self.llm_frontend.resolve_permission(token, decision)

        async def handle_frontend_question(ev: dict) -> None:
            token = str(ev.get("token") or "")
            tool_input = ev.get("input") if isinstance(ev.get("input"), dict) else {}
            if not tool_input:
                tool_input = {"questions": ev.get("questions") or []}
            updated = await self._ask_question(target_name, tool_input)
            if token:
                await self.llm_frontend.answer_question(token, updated)

        try:
            self._frontend_dialog_dest[target_name] = (chat_id, out_thread)
            msg = await self.app.bot.send_message(chat_id, f"{prefix}▌", **kw)
            await self.app.bot.edit_message_text(
                f"{prefix}▌\n\n处理中…",
                chat_id=chat_id,
                message_id=msg.message_id,
                parse_mode=None,
            )
            async for ev in self.llm_frontend.run_stream(target_name, prompt):
                etype = emit_frontend_event(ev)
                if etype == "perm":
                    await handle_frontend_permission(ev)
                    continue
                if etype == "ask_question" and str(ev.get("phase") or "") == "start":
                    await handle_frontend_question(ev)
                    continue
                if etype == "error":
                    raise LlmFrontendError(str(ev.get("error") or "LLM_Frontend stream error"))
            text = final_text
            if not emitted_text and text:
                BUS.emit(target_name, "text", text=text)
            turn_status = "done"
            final = self._format_frontend_final(prefix, text)
            await self._edit_or_send_final(chat_id, msg.message_id, final, out_thread)
        except asyncio.CancelledError:
            turn_status = "interrupted"
            raise
        except Exception as e:
            log.exception("frontend turn failed")
            BUS.emit(target_name, "note", text=f"错误: {str(e)[:500]}")
            err = f"{prefix}💥 错误: {html.escape(str(e))}"
            if msg is not None:
                try:
                    await self.app.bot.edit_message_text(
                        err, chat_id=chat_id, message_id=msg.message_id, parse_mode="HTML")
                except Exception:
                    await self.app.bot.send_message(chat_id, err, message_thread_id=out_thread,
                                                    parse_mode="HTML")
            else:
                await self.app.bot.send_message(chat_id, err, message_thread_id=out_thread,
                                                parse_mode="HTML")
        finally:
            BUS.emit(target_name, "turn_end", status=turn_status)
            self._frontend_active.discard(target_name)
            self._frontend_dialog_dest.pop(target_name, None)
            self.save_state()
        await self._drain_queue(target_name)

    async def _frontend_state(self) -> dict:
        return await self.llm_frontend.state()

    async def _frontend_session_status(self, name: str) -> dict | None:
        data = await self._frontend_state()
        for item in data.get("sessions") or []:
            if item.get("name") == (name or SessionManager.ORCH):
                return item
        return None

    async def _frontend_session_exists(self, name: str) -> bool:
        if (name or SessionManager.ORCH) == SessionManager.ORCH:
            return True
        return await self._frontend_session_status(name) is not None

    async def _frontend_session_busy(self, name: str) -> bool:
        if name in self._frontend_active:
            return True
        try:
            status = await self._frontend_session_status(name)
        except LlmFrontendError as e:
            log.warning("frontend busy lookup %s failed: %s", name, e)
            return False
        return bool(status and status.get("busy"))

    async def _resolve_frontend_target_name(self, target_name: str, chat_id: int,
                                            out_thread: int | None) -> str:
        try:
            exists = await self._frontend_session_exists(target_name)
        except LlmFrontendError as e:
            log.warning("frontend target lookup %s failed: %s", target_name, e)
            exists = True
        if exists or target_name == SessionManager.ORCH:
            return target_name
        await self.app.bot.send_message(
            chat_id,
            f"⚠️ 目标 «{target_name}» 不存在，本话题改由主对话接管",
            message_thread_id=out_thread)
        if not self.forum:
            self.attached = SessionManager.ORCH
        return SessionManager.ORCH

    async def _frontend_run(self, session: str, text: str) -> str:
        return await self.llm_frontend.run(session or SessionManager.ORCH, text)

    @staticmethod
    def _format_frontend_final(prefix: str, text: str) -> str:
        final = (text or "").strip() or "（无输出）"
        return prefix + final

    async def _edit_or_send_final(self, chat_id: int, message_id: int,
                                  text: str, out_thread: int | None):
        limit = 3900
        head = text[:limit]
        try:
            await self.app.bot.edit_message_text(
                head,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=None,
                disable_web_page_preview=True,
            )
        except Exception:
            await self.app.bot.send_message(chat_id, head, message_thread_id=out_thread,
                                            disable_web_page_preview=True)
        rest = text[limit:]
        while rest:
            part, rest = rest[:3900], rest[3900:]
            await self.app.bot.send_message(chat_id, part, message_thread_id=out_thread,
                                            disable_web_page_preview=True)

    async def _execute_turn(self, target, target_name: str, prompt: str,
                            chat_id: int, out_thread: int | None):
        """真正跑一轮：建 live、驱动 run、收尾，然后抽干该会话的排队消息续跑。"""
        prefix = "" if (target.is_orchestrator or self.forum) else f"👷 «{target.name}» › "
        # 控制台对话流：用户输入也进事件总线（在 turn_start 之前发）
        BUS.emit(target_name, "user", text=prompt[:4000])
        # 论坛模式带 group_id 给 DetailPage 拼 t.me 深链；DM 模式 group_id=None → callback
        gid = self.group_id if self.forum else None
        live = LiveMessage(
            self.app, chat_id, prefix=prefix, thread_id=out_thread,
            group_id=gid, session_label=target_name)
        self._active_live[target_name] = live
        live.start_heartbeat()
        async def on_text(t): await live.append(t)
        async def on_event(ev): await live.event(ev)
        async def on_done(msg):
            # 统计并入主气泡状态头（Kiro 式收尾），不再单发一条 footer 消息
            live.set_result(msg)
        sink = TurnSink(on_text=on_text, on_event=on_event, on_done=on_done)
        try:
            await target.run(prompt, sink)
            await live.finalize()
        except asyncio.CancelledError:
            await live.finalize(status=LiveMessage.ST_INT)
            raise
        except Exception as e:
            log.exception("turn failed")
            await live.finalize(status=LiveMessage.ST_ERR)
            await self.app.bot.send_message(chat_id, f"💥 错误: {html.escape(str(e))}",
                                            message_thread_id=out_thread)
        finally:
            self._active_live.pop(target_name, None)
            # 详情页入多槽缓存：主气泡/详情页按钮在 TTL 内仍能跳/展开/收起
            try:
                self.stash_recent_detail(chat_id, out_thread, live.detail)
            except Exception:
                pass
            self.save_state()
        # 这一轮结束：先抽用户排队消息，再抽同伴对等消息（若有），串行续跑。
        await self._drain_queue(target_name)
        await self.mgr.drain_peer_inbox(target_name)

    # ── worker → main 投递：把 worker 经 peer 工具发给主对话的消息排进主对话队列 ─
    async def _post_to_main(self, sender: str, message: str) -> str:
        """SessionManager.notify_main_cb 的实现。worker 用 message_worker(name='main',...)
        触发本路径。
        - 主对话空闲 → 立即作为新一轮 prompt 跑（消息加上来源标记，避免主对话误以为
          是主人发的指令）
        - 主对话忙 → 走 _enqueue_message 排队（主人能看到入队气泡 + 插入/取消按钮）
        论坛模式：发到主控群 General（thread_id=None）；DM 模式：发到 owner 私聊。"""
        ORCH = SessionManager.ORCH
        target = self.mgr.orchestrator
        if target is None:
            return "error: 主对话未就绪"
        chat_id = self._target_chat()
        out_thread = None  # 主对话 = General 话题 / DM 主线
        # 标记来源，让主对话/主人看到「来自 worker xxx」而不是主人本人指令
        wrapped = (f"📨 [来自班组 worker «{sender}»] —— 这条消息是 worker 发给主对话的，"
                   f"不是主人的指令，请按协作请求处理。\n\n{message}")
        if self.llm_frontend_external_chat:
            if await self._frontend_session_busy(ORCH):
                await self._enqueue_message(ORCH, wrapped, chat_id, out_thread)
                return f"主对话忙，已排队（来自 {sender}）"
            asyncio.create_task(
                self._execute_frontend_turn(ORCH, wrapped, chat_id, out_thread))
            return f"已投递给主对话（来自 {sender}，立即处理）"
        if target.busy or ORCH in self._active_live:
            await self._enqueue_message(ORCH, wrapped, chat_id, out_thread)
            return f"主对话忙，已排队（来自 {sender}）"
        # 主对话空闲：fire-and-forget 起一轮，避免阻塞 worker 的 turn
        asyncio.create_task(
            self._execute_turn(target, ORCH, wrapped, chat_id, out_thread))
        return f"已投递给主对话（来自 {sender}，立即处理）"

    # ── 消息队列：忙时排队 + 插入(steer) ──────────────────────────────────────
    async def _enqueue_message(self, name: str, text: str, chat_id: int,
                               thread_id: int | None):
        qm = QueuedMsg(token=uuid.uuid4().hex[:12], text=text,
                       chat_id=chat_id, thread_id=thread_id)
        self.msg_queue.setdefault(name, []).append(qm)
        self._queue_index[qm.token] = qm
        pos = len(self.msg_queue[name])
        if self.llm_frontend_external_chat:
            buttons = [
                InlineKeyboardButton("⏫ 提前到队首", callback_data=f"q:{qm.token}:steer"),
                InlineKeyboardButton("🗑 取消", callback_data=f"q:{qm.token}:cancel"),
            ]
            hint = "点「提前到队首」可让它排在下一轮最前。"
        else:
            buttons = [
                InlineKeyboardButton("⚡ 插入当前对话(影响决策)", callback_data=f"q:{qm.token}:steer"),
                InlineKeyboardButton("🗑 取消", callback_data=f"q:{qm.token}:cancel"),
            ]
            hint = "想立刻影响当前这轮决策就点「插入」。"
        kb = InlineKeyboardMarkup([buttons])
        preview = text.strip().replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        kw = {"reply_markup": kb}
        if thread_id is not None:
            kw["message_thread_id"] = thread_id
        try:
            m = await self.app.bot.send_message(
                chat_id,
                f"📥 会话忙，已排队（第 {pos} 条，跑完自动续上）：\n「{preview}」\n"
                f"{hint}",
                **kw)
            qm.note_msg_id = m.message_id
        except Exception as e:
            log.debug("enqueue notice failed: %s", e)

    async def _drain_queue(self, name: str):
        """抽该会话队首的一条排队消息接着跑（递归续到队列空）。"""
        q = self.msg_queue.get(name)
        if not q:
            return
        qm = q.pop(0)
        self._queue_index.pop(qm.token, None)
        if not q:
            self.msg_queue.pop(name, None)
        if qm.note_msg_id is not None:
            try:
                await self.app.bot.edit_message_reply_markup(
                    qm.chat_id, message_id=qm.note_msg_id, reply_markup=None)
            except Exception:
                pass
        if self.llm_frontend_external_chat:
            await self._execute_frontend_turn(name, qm.text, qm.chat_id, qm.thread_id)
            return
        target = self.mgr.get(name)
        if target is None:
            return
        await self._execute_turn(target, name, qm.text, qm.chat_id, qm.thread_id)

    async def resolve_queued(self, token: str, action: str) -> str | None:
        """处理排队消息的「插入(steer)」/「取消」按钮。返回给用户的 toast 文案。"""
        qm = self._queue_index.get(token)
        if qm is None:
            return None
        # 从队列摘除这条
        owner = self._token_owner(token)
        if owner and owner in self.msg_queue:
            try:
                self.msg_queue[owner].remove(qm)
            except ValueError:
                pass
            if not self.msg_queue[owner]:
                self.msg_queue.pop(owner, None)
        self._queue_index.pop(token, None)
        # 收掉按钮
        if qm.note_msg_id is not None:
            try:
                await self.app.bot.edit_message_reply_markup(
                    qm.chat_id, message_id=qm.note_msg_id, reply_markup=None)
            except Exception:
                pass
        if action == "cancel":
            return "🗑 已取消该排队消息"
        # steer：插入进行中的 turn
        if self.llm_frontend_external_chat:
            if await self._frontend_session_busy(owner or SessionManager.ORCH):
                self.msg_queue.setdefault(owner or SessionManager.ORCH, []).insert(0, qm)
                self._queue_index[qm.token] = qm
                return "前端会话处理中，已提前到队首"
            await self._execute_frontend_turn(owner or SessionManager.ORCH, qm.text, qm.chat_id, qm.thread_id)
            return "（该轮已结束，按新一轮处理）"
        target = self.mgr.get(owner) if owner else None
        if target is None or not target.busy:
            # 这轮已结束：退化成普通一轮（直接跑）
            if target is not None:
                await self._execute_turn(target, owner, qm.text, qm.chat_id, qm.thread_id)
            return "（该轮已结束，按新一轮处理）"
        ok = await target.steer(qm.text)
        if ok:
            BUS.emit(owner or "main", "user", text=qm.text[:4000], steer=True)
        return "⚡ 已插入当前对话" if ok else "插入失败（该轮可能刚结束）"

    def _token_owner(self, token: str) -> str | None:
        for name, lst in self.msg_queue.items():
            for qm in lst:
                if qm.token == token:
                    return name
        return None

    # ── Mini App 控制台入口（webapp.py 调用，owner 已鉴权）────────────────────
    async def web_send(self, session: str, text: str) -> str:
        """从控制台发 prompt 给指定会话。回声 + 输出都会照常落到 TG（论坛话题 /
        私聊），控制台靠事件总线同步看到。返回 started|queued。

        控制台必须显式指定真实会话；聚合视图/历史标签不能静默落到 main，
        否则用户在别的会话里输入会被错误投递到主对话。"""
        name = (session or "").strip()
        if not name:
            raise KeyError("请选择具体会话后再发送")
        target = None
        if self.llm_frontend_external_chat:
            if not await self._frontend_session_exists(name):
                log.info("web_send target %s is gone; refusing send", name)
                raise KeyError(f"会话 «{name}» 不存在")
        else:
            target = self.mgr.get(name)
            if target is None:
                log.info("web_send target %s is gone; refusing send", name)
                raise KeyError(f"会话 «{name}» 不存在")
        chat_id = self._target_chat()
        out_thread = await self.ensure_topic(name) if self.forum else None
        # 控制台没有自己的消息气泡，在 TG 留一条来源标记，主人翻历史能对上号
        try:
            preview = text.strip().replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "…"
            kw = {"message_thread_id": out_thread} if out_thread else {}
            await self.app.bot.send_message(chat_id, f"🖥 控制台 › {preview}", **kw)
        except Exception as e:
            log.debug("web echo failed: %s", e)
        if self.llm_frontend_external_chat:
            if await self._frontend_session_busy(name):
                await self._enqueue_message(name, text, chat_id, out_thread)
                return "queued"
            asyncio.get_running_loop().create_task(
                self._execute_frontend_turn(name, text, chat_id, out_thread))
            return "started"
        if target.busy or name in self._active_live:
            await self._enqueue_message(name, text, chat_id, out_thread)
            return "queued"
        asyncio.get_running_loop().create_task(
            self._execute_turn(target, name, text, chat_id, out_thread))
        return "started"

    async def web_stop(self, session: str | None = None) -> list[str]:
        """从控制台中断会话。session=None → 停掉所有在跑的会话。返回被停的名单。"""
        stopped = []
        if self.llm_frontend_external_chat:
            names = [session] if session else []
            try:
                state = await self._frontend_state()
                if session:
                    names = [session]
                else:
                    names = [s["name"] for s in state.get("sessions") or [] if s.get("busy")]
            except LlmFrontendError as e:
                log.warning("frontend state for stop failed: %s", e)
            for name in names:
                try:
                    if await self.llm_frontend.interrupt(name or SessionManager.ORCH):
                        live = self._active_live.get(name)
                        if live:
                            live.set_status(live.ST_INT)
                        stopped.append(name)
                except LlmFrontendError as e:
                    log.warning("frontend web interrupt %s failed: %s", name, e)
            return stopped
        names = ([session] if session else
                 [SessionManager.ORCH] + [ws["name"] for ws in self.mgr.list_workers()])
        for name in names:
            w = self.mgr.get(name)
            if not (w and w.busy and w.client):
                continue
            try:
                await w.client.interrupt()
                live = self._active_live.get(name)
                if live:
                    live.set_status(live.ST_INT)
                stopped.append(name)
            except Exception as e:
                log.warning("web interrupt %s failed: %s", name, e)
        return stopped

    def _queue_preview(self, name: str) -> list[dict]:
        out = []
        for qm in self.msg_queue.get(name, []):
            preview = qm.text.strip().replace("\n", " ")
            out.append({"token": qm.token,
                        "preview": preview[:80] + ("…" if len(preview) > 80 else "")})
        return out

    @staticmethod
    def _hide_console_session_name(name: str) -> bool:
        return str(name or "").startswith("llm-events-")

    def _frontend_web_sessions_snapshot(self) -> list[dict] | None:
        if not self.llm_frontend_external_chat:
            return None
        try:
            data = self.llm_frontend._request("GET", "/state", timeout=2)
        except Exception as e:
            log.debug("frontend web state fallback: %s", e)
            if self._frontend_known_sessions:
                return [{
                    "name": name,
                    "busy": name in self._frontend_active,
                    "turns": 0,
                    "provider": "frontend",
                    "queued": len(self.msg_queue.get(name, [])),
                    "queue": self._queue_preview(name),
                } for name in sorted(self._frontend_known_sessions)
                    if not self._hide_console_session_name(name)]
            return None
        sessions = []
        seen = set()
        for item in data.get("sessions") or []:
            name = item.get("name") or SessionManager.ORCH
            if self._hide_console_session_name(name):
                continue
            seen.add(name)
            sessions.append({
                "name": name,
                "busy": bool(item.get("busy")) or name in self._frontend_active,
                "turns": item.get("turns") or 0,
                "provider": item.get("provider"),
                "queued": len(self.msg_queue.get(name, [])),
                "queue": self._queue_preview(name),
            })
        for name in sorted(self._frontend_known_sessions - seen):
            if self._hide_console_session_name(name):
                continue
            sessions.append({
                "name": name,
                "busy": name in self._frontend_active,
                "turns": 0,
                "provider": "frontend",
                "queued": len(self.msg_queue.get(name, [])),
                "queue": self._queue_preview(name),
            })
        return sessions

    def web_state(self) -> dict:
        """控制台状态快照：orchestrator + workers 忙闲、provider、排队消息。"""
        sessions = self._frontend_web_sessions_snapshot()
        if sessions is None:
            orch = self.mgr.orchestrator
            sessions = [{
                "name": SessionManager.ORCH,
                "busy": bool(orch and orch.busy),
                "turns": getattr(orch, "turns", 0) if orch else 0,
                "provider": (orch.provider if orch and orch.provider
                             else (self.mgr.router.active if self.mgr.router else None)),
                "queued": len(self.msg_queue.get(SessionManager.ORCH, [])),
                "queue": self._queue_preview(SessionManager.ORCH),
            }]
            for ws in self.mgr.list_workers():
                if self._hide_console_session_name(ws["name"]):
                    continue
                sessions.append({
                    "name": ws["name"], "busy": ws["busy"], "turns": ws["turns"],
                    "provider": ws.get("provider"),
                    "queued": len(self.msg_queue.get(ws["name"], [])),
                    "queue": self._queue_preview(ws["name"]),
                })
        # 外部握手 session（CLI 直连），若无 WorkerSession 则兜底列出
        seen = {s["name"] for s in sessions}
        ext_msgs = self.msg_queue
        for ename, einfo in self._ext_sessions.items():
            if ename in seen:
                continue
            w = self.mgr.get(ename)
            sessions.append({
                "name": ename,
                "busy": bool(w and w.busy),
                "turns": getattr(w, "turns", 0) if w else 0,
                "provider": "ext-handshake",
                "queued": len(ext_msgs.get(ename, [])),
                "queue": self._queue_preview(ename),
            })
            seen.add(ename)
        for hname in BUS.sessions():
            if hname in seen or self._hide_console_session_name(hname):
                continue
            sessions.append({
                "name": hname,
                "busy": False,
                "turns": 0,
                "provider": "history",
                "queued": 0,
                "queue": [],
                "historyOnly": True,
            })
            seen.add(hname)
        # 仍在等待回答的 AskUserQuestion（webapp 据此决定加载时是否补弹模态：
        # 只补弹「后端真的还在等」的，历史里未结案的旧提问不再骚扰用户）。
        active_asks = [
            {"token": tok, "session": getattr(s, "worker", "main")}
            for tok, s in self.ask_sessions.items()
            if not getattr(s, "done", False)
        ]
        # Background tasks
        try:
            from phantom_console.tasks import get_task_manager
            bg_tasks = get_task_manager().list_tasks()
        except Exception:
            bg_tasks = []
        return {"sessions": sessions, "forum": self.forum,
                "mode": self.mgr.default_mode, "active_asks": active_asks,
                "bg_tasks": bg_tasks}

    # ── 会话池（三端同源：webapp / TG / 插件面板都读写 card relay 的 sessions+target）──
    async def web_cc_resume(self, session_id: str, cwd: str, name: str,
                            transcript_file: str | None = None) -> dict:
        """接管本地 Claude Code 会话：按 transcript session_id resume，继续写同一 JSONL。"""
        session_id = (session_id or "").strip()
        name = (name or f"cc-{session_id[:12]}").strip()
        norm_cwd = os.path.realpath(cwd) if cwd else ""

        def same_sid(value: str | None) -> bool:
            return bool(value and session_id and str(value).strip() == session_id)

        def same_cwd(value: str | None) -> bool:
            if not norm_cwd:
                return True
            return bool(value and os.path.realpath(str(value)) == norm_cwd)

        if self.llm_frontend_external_chat:
            try:
                state = await self._frontend_state()
                sessions = state.get("sessions") or []
                names = {item.get("name") for item in sessions if item.get("name")}
                for item in sessions:
                    sid = item.get("session_id_full") or item.get("session_id")
                    if same_sid(sid) and same_cwd(item.get("cwd")):
                        actual_name = item.get("name") or name
                        self._frontend_known_sessions.add(actual_name)
                        return {
                            "ok": True,
                            "name": actual_name,
                            "session_id": session_id,
                            "cwd": item.get("cwd") or cwd,
                            "transcript_file": transcript_file or "",
                            "already_active": True,
                            "session": item,
                        }
                if name in names:
                    name = f"cc-{session_id[:12]}"
                    if name in names:
                        return {"ok": False, "error": f"worker «{name}» 已存在且不是该 session"}
                data = await self.llm_frontend.spawn_worker(
                    name,
                    mode="default",
                    resume_session_id=session_id,
                    cwd=cwd,
                )
                session = data.get("session") or {}
                actual_name = data.get("name") or session.get("name") or name
                self._frontend_known_sessions.add(actual_name)
                return {
                    "ok": True,
                    "name": actual_name,
                    "session_id": session_id,
                    "cwd": cwd,
                    "transcript_file": transcript_file or "",
                    "session": session,
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}
        # 检查同一 transcript 是否已接管；已接管则复用 worker，不创建新会话。
        for ws in self.mgr.list_workers():
            full = self.mgr.get(ws.get("name"))
            if full and same_sid(full.session_id or full.resume_session_id) and same_cwd(full.cwd):
                return {
                    "ok": True,
                    "name": full.name,
                    "session_id": session_id,
                    "cwd": full.cwd or cwd,
                    "transcript_file": transcript_file or "",
                    "already_active": True,
                }
        if self.mgr.get(name):
            name = f"cc-{session_id[:12]}"
            if self.mgr.get(name):
                return {"ok": False, "error": f"worker «{name}» 已存在且不是该 session"}
        try:
            w = await self.mgr.spawn_worker(
                name,
                mode="default",
                resume_session_id=session_id,
                cwd=cwd,
            )
            return {"ok": True, "name": name, "session_id": session_id,
                    "cwd": cwd, "transcript_file": transcript_file or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def web_cc_active(self) -> list[dict]:
        """列出当前已接管的 cc-* worker（接管会话才有 cwd 指向外部项目目录）。
        判定：worker 名以 'cc-' 开头（接管约定前缀）。"""
        if self.llm_frontend_external_chat:
            sessions = self._frontend_web_sessions_snapshot() or []
            out = []
            try:
                raw = self.llm_frontend._request("GET", "/state", timeout=2)
                by_name = {
                    (item.get("name") or ""): item
                    for item in raw.get("sessions") or []
                    if item.get("name")
                }
            except Exception:
                by_name = {}
            for item in sessions:
                name = item.get("name", "")
                if not name.startswith("cc-"):
                    continue
                full = by_name.get(name) or {}
                out.append({
                    "name": name,
                    "session_id": full.get("session_id_full") or full.get("session_id") or "",
                    "cwd": full.get("cwd") or "",
                    "busy": item.get("busy", False),
                    "turns": item.get("turns", 0),
                })
            return out
        out = []
        for w in self.mgr.list_workers():
            name = w.get("name", "")
            if not name.startswith("cc-"):
                continue
            full = self.mgr.get(name)
            out.append({
                "name": name,
                "session_id": (full.session_id if full else "") or "",
                "cwd": (full.cwd if full else "") or "",
                "busy": w.get("busy", False),
                "turns": w.get("turns", 0),
            })
        return out

    async def web_cc_stop(self, name: str) -> dict:
        """停止并移除一个已接管的 cc-* worker。"""
        if not name.startswith("cc-"):
            return {"ok": False, "error": "只能停止已接管会话（cc- 前缀）"}
        if self.llm_frontend_external_chat:
            try:
                ok = await self.llm_frontend.stop_worker(name)
                if ok:
                    self._frontend_known_sessions.discard(name)
                return {"ok": ok, "name": name}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        if self.mgr.get(name) is None:
            return {"ok": False, "error": f"worker «{name}» 不存在"}
        try:
            ok = await self.mgr.stop_worker(name)
            return {"ok": ok, "name": name}
        except Exception as e:
            return {"ok": False, "error": str(e)}
