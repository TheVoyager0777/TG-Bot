"""todo_bubble —— Todo 专属常驻气泡（Kiro 任务清单风格）。

每次 turn 中第一次出现 TodoWrite 工具调用就建一条独立 TG 消息，
后续 TodoWrite 复用同一条编辑（不再开新气泡）。turn 结束自动定格。

呈现对齐 Kiro：状态图标与工具行同一套（✓ ◐ ○ ⊘），完成项删除线、
进行中加粗，头部进度条 + 计数。HTML 解析失败自动回退纯文本。

性能契约：update()（热路径）零网络 I/O——只记 pending + 安排节流任务；
真正发送/编辑只在节流任务与 finalize 里。
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import time

from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application

log = logging.getLogger("tgclaude.todo")

TG_LIMIT = 3900
_PARSE_ERR_HINTS = ("parse", "entit", "tag")

# 与 kiro_ui.ICON 同一状态语言
_DONE = ("completed", "done")
_ACTIVE = ("in_progress", "active", "running")
_CANCEL = ("cancelled", "canceled", "skipped")


def _icon(status: str) -> str:
    s = (status or "").lower()
    if s in _DONE:
        return "✓"
    if s in _ACTIVE:
        return "◐"
    if s in _CANCEL:
        return "⊘"
    return "○"


def todo_text(t: dict, status: str) -> str:
    # SDK schema 用 activeForm / content；旧版/兼容用 description / subject
    if status in _ACTIVE:
        text = (t.get("activeForm") or t.get("subject") or
                t.get("content") or t.get("description") or "")
    else:
        text = (t.get("subject") or t.get("content")
                or t.get("description") or t.get("activeForm") or "")
    text = str(text).strip().replace("\n", " ")
    if len(text) > 120:
        text = text[:120] + "…"
    return text


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return ""
    fill = round(done / total * width)
    fill = max(0, min(width, fill))
    return "▰" * fill + "▱" * (width - fill)


def render_todos_pair(todos: list, *, label: str = "") -> tuple[str, str]:
    """把 TodoWrite 的 input.todos 渲染成 (HTML, 纯文本回退)。"""
    if not isinstance(todos, list):
        return "", ""
    items = [t for t in todos if isinstance(t, dict)]
    total = len(items)
    n_done = sum(1 for t in items
                 if (t.get("status") or t.get("state") or "").lower() in _DONE)

    head_plain = f"📋 待办 «{label}»" if label else "📋 待办"
    head_html = (f"📋 <b>待办</b> «{_html.escape(label)}»" if label
                 else "📋 <b>待办</b>")
    if total:
        head_plain += f" · {n_done}/{total}"
        head_html += f" · {n_done}/{total}"

    html_lines = [head_html]
    plain_lines = [head_plain]
    bar = _progress_bar(n_done, total)
    if bar:
        html_lines.append(bar)
        plain_lines.append(bar)
    html_lines.append("")
    plain_lines.append("")

    for t in items:
        st = (t.get("status") or t.get("state") or "pending").lower()
        ico = _icon(st)
        text = todo_text(t, st)
        esc = _html.escape(text)
        if st in _DONE or st in _CANCEL:
            html_lines.append(f"{ico} <s>{esc}</s>")
        elif st in _ACTIVE:
            html_lines.append(f"{ico} <b>{esc}</b>")
        else:
            html_lines.append(f"{ico} {esc}")
        plain_lines.append(f"{ico} {text}")
    return "\n".join(html_lines), "\n".join(plain_lines)


def render_todos(todos: list, *, label: str = "") -> str:
    """纯文本版（兼容旧调用方）。"""
    return render_todos_pair(todos, label=label)[1]


class TodoBubble:
    """Todo 专属可编辑消息。LiveMessage 旁路：每个 turn 一条，TodoWrite 出现时
    懒创建，后续 TodoWrite 编辑同一条。"""
    EDIT_INTERVAL = 0.8

    def __init__(self, app: Application, chat_id: int,
                 thread_id: int | None = None, *, session_label: str = ""):
        self.app = app
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.session_label = session_label
        self.msg_id: int | None = None
        self._lock = asyncio.Lock()
        self.last_edit = 0.0
        self._last_plain = ""
        self._flush_task: asyncio.Task | None = None
        self._pending: tuple[str, str] | None = None  # (html, plain)

    # ── 热路径：零网络 I/O ───────────────────────────────────────────────────
    async def update(self, todos: list):
        pair = render_todos_pair(todos, label=self.session_label)
        if not pair[1]:
            return
        if pair[1] == self._last_plain:
            return  # 没变化，省一次编辑
        self._pending = pair
        self._schedule_flush()

    def _schedule_flush(self):
        if self._flush_task and not self._flush_task.done():
            return
        delay = max(0.05, self.EDIT_INTERVAL - (time.time() - self.last_edit))

        async def _run():
            try:
                await asyncio.sleep(delay)
                await self._flush()
            except asyncio.CancelledError:
                return
            finally:
                self._flush_task = None
            if self._pending is not None:
                self._schedule_flush()

        self._flush_task = asyncio.create_task(_run())

    # ── 冷路径：真正发送/编辑（HTML 优先，解析失败回退纯文本）────────────────
    async def _flush(self):
        async with self._lock:
            pair = self._pending
            if pair is None or pair[1] == self._last_plain:
                self._pending = None
                return
            self._pending = None
            await self._do_send(*pair)

    async def _do_send(self, html_body: str, plain_body: str):
        if len(plain_body) > TG_LIMIT:  # 可见长度按纯文本核算
            plain_body = plain_body[:TG_LIMIT - 1] + "…"
            html_body = plain_body  # 截断后的 HTML 标签可能破损，直接降级纯文本
            parse_mode = None
        else:
            parse_mode = ParseMode.HTML
        sent = await self._try_send(html_body, parse_mode)
        if not sent and parse_mode is not None:
            sent = await self._try_send(plain_body, None)
        if sent:
            self.last_edit = time.time()
            self._last_plain = plain_body

    async def _try_send(self, body: str, parse_mode) -> bool:
        kw: dict = {}
        if parse_mode is not None:
            kw["parse_mode"] = parse_mode
        try:
            if self.msg_id is None:
                send_kw = dict(kw)
                if self.thread_id is not None:
                    send_kw["message_thread_id"] = self.thread_id
                m = await self.app.bot.send_message(self.chat_id, body, **send_kw)
                self.msg_id = m.message_id
            else:
                await self.app.bot.edit_message_text(
                    body, chat_id=self.chat_id, message_id=self.msg_id, **kw)
            return True
        except BadRequest as e:
            s = str(e).lower()
            if parse_mode is not None and any(h in s for h in _PARSE_ERR_HINTS):
                return False  # 解析错 → 调用方回退纯文本
            log.debug("todo render skip: %s", e)  # 含 message not modified
            return True
        except Exception as e:
            log.debug("todo render skip: %s", e)
            return True  # 网络类错误不重发，等下一次 update

    async def finalize(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None
        await self._flush()


if __name__ == "__main__":
    sample = [
        {"subject": "读相关文件", "activeForm": "读相关文件中", "status": "completed"},
        {"subject": "重构 detail", "activeForm": "重构 <live_message> 中",
         "status": "in_progress"},
        {"subject": "跑回归测试", "activeForm": "跑回归测试中", "status": "pending"},
        {"subject": "旧方案", "status": "cancelled"},
    ]
    html_body, plain_body = render_todos_pair(sample, label="main")
    print(plain_body)
    print()
    print(html_body)
    assert "▰" in plain_body and "1/4" in plain_body
    assert "<s>读相关文件</s>" in html_body
    assert "<b>重构 &lt;live_message&gt; 中</b>" in html_body
    assert render_todos(sample) == render_todos_pair(sample)[1]
    print("\n[OK] todo_bubble smoke tests passed.")
