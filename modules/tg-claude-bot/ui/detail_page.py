"""detail_page —— 会话详情页：独立 TG 消息累积全部事件流，主气泡按钮跳转到此。

Kiro 对齐：
- 事件按时间线一行一条：工具行原地变态（存 input，渲染时按 phase 算时态标签）、
  思考折叠单行、正文条目收成索引行（首行 + 行数），富文本（HTML）呈现。
- HTML 解析失败自动回退纯文本，绝不让一条消息卡死整页更新。

性能契约（与 live_message 对齐）：add_*（热路径）零网络 I/O——只入列 + 记脏；
真正的发送/编辑只发生在节流 flush 任务、ensure/bump/set_collapsed（冷路径）
与 finalize 里。text 条目用 chunk 列表累积（O(1) 追加，渲染时 join），
单条封顶 TEXT_ENTRY_CAP 防长 turn 无界内存。
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application

from ui import kiro_ui

log = logging.getLogger("tgclaude.detail")

_ICONS = {"running": "◐", "completed": "✓", "error": "✗", "rejected": "⊘"}
_SEP = "─────"
_PARSE_ERR_HINTS = ("parse", "entit", "tag")


def _is_parse_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(h in s for h in _PARSE_ERR_HINTS)


class DetailPage:
    """每条会话(chat_id, thread_id)绑定一个独立 Telegram 消息作为"详情页"。
    主气泡只显示最新条目；这里按时间顺序累积全部事件流，主气泡按钮跳到这里。"""

    PAGE_LIMIT = 3900
    KEEP_ENTRIES = 250
    EDIT_INTERVAL = 1.5
    TEXT_ENTRY_CAP = 6000  # 单条 text 条目最大累积字符（防无界内存）

    def __init__(self, app: Application, chat_id: int, thread_id: int | None = None,
                 *, group_id: int | None = None, session_label: str = "",
                 lazy_text_only: bool = False):
        self.app = app
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.group_id = group_id
        self.session_label = session_label or "session"
        # 纯文本懒发：条目全是 text 且页面还没落地时不自动发消息（防止与主气泡
        # 内容重复刷屏，桥接话题的纯文本回复用）。bump/ensure 仍可显式发出。
        self.lazy_text_only = lazy_text_only
        self.msg_id: int | None = None

        self._entries: list[dict] = []
        self._tool_idx: dict[str, int] = {}  # tool_id -> index in _entries
        self._dirty = False
        self._seq = 0          # 写入版本号：flush 期间有新写入则不清脏标记
        self._lock = asyncio.Lock()
        self._last_edit = 0.0
        self._flush_task: asyncio.Task | None = None
        # 收起/展开状态：默认展开。收起时只显示一行占位 + 一个展开按钮。
        self.collapsed = False

    # ---- 写入（热路径：零网络 I/O）----
    def _mark_dirty(self):
        self._seq += 1
        self._dirty = True
        self._schedule_flush()

    async def add_text(self, text: str) -> None:
        """追加模型输出文本。相邻 text 条目按段落合并（chunk 列表，O(1)）。"""
        text = text.rstrip()
        if not text:
            return
        last = self._entries[-1] if self._entries else None
        if last is not None and last["kind"] == "text":
            if last["size"] < self.TEXT_ENTRY_CAP:
                last["chunks"].append(text)
                last["size"] += len(text)
            else:
                last["omitted"] += 1
        else:
            self._entries.append({"kind": "text", "chunks": [text],
                                  "size": len(text), "omitted": 0,
                                  "ts": time.time()})
            self._trim()
        self._mark_dirty()

    async def add_tool(self, tool: str, label: str, phase: str, *,
                       tool_id: str = "", tool_input: dict | None = None,
                       summary: str = "") -> None:
        """追加工具调用事件。tool_id 相同时原地更新 phase（保留首报的 input，
        渲染时按最终 phase 算时态，避免结果回执无 input 导致标签降级）。
        summary：结果回执的一行摘要（✓ Ran: npm test · 42 passed）。"""
        if tool_id and tool_id in self._tool_idx:
            entry = self._entries[self._tool_idx[tool_id]]
            entry["phase"] = phase
            if tool_input is not None:
                entry["input"] = tool_input
            if summary:
                entry["summary"] = summary
        else:
            self._entries.append({"kind": "tool", "tool": tool, "label": label,
                                  "input": tool_input, "phase": phase,
                                  "summary": summary, "ts": time.time()})
            if tool_id:
                self._tool_idx[tool_id] = len(self._entries) - 1
            self._trim()
        self._mark_dirty()

    async def add_thinking(self, text: str) -> None:
        """追加一行思考(截断到 ~140 字)。"""
        text = text.strip()
        if not text:
            return
        if len(text) > 140:
            text = text[:137] + "…"
        self._entries.append({"kind": "thinking", "content": text, "ts": time.time()})
        self._trim()
        self._mark_dirty()

    async def add_note(self, text: str) -> None:
        """追加系统/状态注记。"""
        text = text.strip()
        if not text:
            return
        self._entries.append({"kind": "note", "content": text, "ts": time.time()})
        self._trim()
        self._mark_dirty()

    # ---- 跳转控制（冷路径：直接发送）----
    async def ensure(self) -> int | None:
        """确保详情消息已发出；返回 message_id。"""
        if self.msg_id is not None:
            return self.msg_id
        async with self._lock:
            if self.msg_id is None:
                html_body, plain_body = self._compose_pair()
                if not plain_body:
                    html_body = plain_body = self._placeholder()
                await self._send_new(html_body, plain_body)
        return self.msg_id

    def _placeholder(self) -> str:
        return f"📄 «{self.session_label}» 详情"

    def _toggle_markup(self) -> "InlineKeyboardMarkup":
        """详情消息底部"展开/收起"切换按钮。"""
        tid = self.thread_id or 0
        if self.collapsed:
            label = "🔽 展开"
            data = f"detail:expand:{self.chat_id}:{tid}"
        else:
            label = "🔼 收起"
            data = f"detail:collapse:{self.chat_id}:{tid}"
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(label, callback_data=data)]])

    async def set_collapsed(self, collapsed: bool) -> None:
        """切换收起/展开。直接编辑详情消息为新的渲染态（含切换按钮）。"""
        if self.collapsed == collapsed:
            return
        self.collapsed = collapsed
        if self.msg_id is None:
            return
        async with self._lock:
            html_body, plain_body = self._compose_pair()
            if not plain_body:
                html_body = plain_body = self._placeholder()
            await self._edit(html_body, plain_body)

    def link_button(self) -> InlineKeyboardButton:
        """给主气泡用的"📄 详情"按钮。超级群=URL 深链；其余=callback bump。"""
        # 真超级群的 chat_id 形如 -100xxxxxxxx，abs() 减 10^12 得正数。
        # 普通基础群（-12345 这种）算出来会 ≤0，构造的 t.me/c/ 链接非法——
        # 退化到 callback 模式更稳。
        if self.group_id and self.msg_id:
            abs_id = abs(self.group_id) - 1000000000000
            if abs_id > 0:
                if self.thread_id:
                    url = f"https://t.me/c/{abs_id}/{self.thread_id}/{self.msg_id}"
                else:
                    url = f"https://t.me/c/{abs_id}/{self.msg_id}"
                return InlineKeyboardButton("📄 详情", url=url)
        # 私聊/无 group_id/非超级群: callback
        tid = self.thread_id or 0
        return InlineKeyboardButton(
            "📄 详情", callback_data=f"detail:bump:{self.chat_id}:{tid}")

    async def bump(self) -> None:
        """重发详情消息到对话末尾。「移动」语义：新消息落地后删掉旧消息，
        保证一个 turn 永远只有一张详情页，反复点「详情」不会堆出重复气泡。"""
        async with self._lock:
            html_body, plain_body = self._compose_pair()
            if not plain_body:
                html_body = plain_body = self._placeholder()
            old_id = self.msg_id
            self.msg_id = None  # 强制走"发新消息"路径
            await self._send_new(html_body, plain_body)
            if old_id is not None and self.msg_id is not None and self.msg_id != old_id:
                try:
                    await self.app.bot.delete_message(self.chat_id, old_id)
                except Exception as e:
                    log.debug("bump delete old skip: %s", e)
            elif self.msg_id is None:
                self.msg_id = old_id  # 发送失败：保住旧页，别变成无主

    async def finalize(self) -> None:
        """turn 结束：刷最后状态，停止节流。"""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None
        if self._dirty or self._entries:
            await self._do_flush(force=True)

    # ---- 内部 ----
    def _trim(self):
        overflow = len(self._entries) - self.KEEP_ENTRIES
        if overflow > 0:
            self._entries = self._entries[overflow:]
            self._tool_idx = {k: v - overflow for k, v in self._tool_idx.items()
                              if v >= overflow}

    def _schedule_flush(self):
        """节流：安排一次延迟 flush。已有在途任务则等它收尾时自查重排。"""
        if self._flush_task and not self._flush_task.done():
            return
        delay = max(0.05, self.EDIT_INTERVAL - (time.time() - self._last_edit))

        async def _run():
            try:
                await asyncio.sleep(delay)
                await self._do_flush()
            except asyncio.CancelledError:
                return
            finally:
                self._flush_task = None
            # flush 期间若有新写入（seq 前进、dirty 复位失败），续排下一轮
            if self._dirty:
                self._schedule_flush()

        self._flush_task = asyncio.create_task(_run())

    async def _do_flush(self, force: bool = False):
        async with self._lock:
            if not self._dirty and not force:
                return
            seq = self._seq
            # 懒发模式：页面未落地且内容只有纯文本 → 不发（主气泡已有同样内容）
            if (self.lazy_text_only and self.msg_id is None
                    and all(e.get("kind") == "text" for e in self._entries)):
                if self._seq == seq:
                    self._dirty = False
                return
            html_body, plain_body = self._compose_pair()
            if plain_body:
                await self._send_or_edit(html_body, plain_body)
                self._last_edit = time.time()
            if self._seq == seq:
                self._dirty = False

    # ---- 发送/编辑（HTML 优先，解析失败回退纯文本）----
    async def _send_or_edit(self, html_body: str, plain_body: str):
        if self.msg_id is None:
            await self._send_new(html_body, plain_body)
        else:
            await self._edit(html_body, plain_body)

    async def _send_new(self, html_body: str, plain_body: str):
        kw: dict = {"reply_markup": self._toggle_markup()}
        if self.thread_id is not None:
            kw["message_thread_id"] = self.thread_id
        try:
            m = await self.app.bot.send_message(
                self.chat_id, html_body, parse_mode=ParseMode.HTML, **kw)
            self.msg_id = m.message_id
            self._last_edit = time.time()
            return
        except BadRequest as e:
            if not _is_parse_error(e):
                log.debug("detail send skip: %s", e)
                return
        except Exception as e:
            log.debug("detail send failed: %s", e)
            return
        try:  # HTML 解析失败 → 纯文本兜底
            m = await self.app.bot.send_message(self.chat_id, plain_body, **kw)
            self.msg_id = m.message_id
            self._last_edit = time.time()
        except Exception as e:
            log.debug("detail plain send failed: %s", e)

    async def _edit(self, html_body: str, plain_body: str):
        kw = {"chat_id": self.chat_id, "message_id": self.msg_id,
              "reply_markup": self._toggle_markup()}
        try:
            await self.app.bot.edit_message_text(
                html_body, parse_mode=ParseMode.HTML, **kw)
            self._last_edit = time.time()
            return
        except BadRequest as e:
            if not _is_parse_error(e):
                log.debug("detail edit skip: %s", e)  # 含 message not modified
                return
        except Exception as e:
            log.debug("detail edit skip: %s", e)
            return
        try:
            await self.app.bot.edit_message_text(plain_body, **kw)
            self._last_edit = time.time()
        except Exception as e:
            log.debug("detail plain edit skip: %s", e)

    # ---- 渲染 ----
    def _tool_entry_label(self, entry: dict) -> str:
        """工具条目标签：有 input 用 kiro 词法按 phase 算时态；否则用首报 label。"""
        phase = entry.get("phase", "running")
        if entry.get("input") is not None:
            try:
                label = kiro_ui.tool_label(
                    entry.get("tool", ""), entry.get("input"),
                    past=phase in ("completed", "error"))
            except Exception:
                label = entry.get("label") or entry.get("tool", "?")
        else:
            label = entry.get("label") or entry.get("tool", "?")
        if phase == "error":
            label += " — failed"
        elif phase == "rejected":
            label += " — rejected"
        s = entry.get("summary") or ""
        if s and phase in ("completed", "error"):
            if len(s) > 48:
                s = s[:47] + "…"
            label += f" · {s}"
        return label

    def _entry_pair(self, entry: dict) -> tuple[str, str]:
        """单条目 → (html行, plain行)。"""
        kind = entry["kind"]
        ts = time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0)))
        if kind == "tool":
            icon = _ICONS.get(entry.get("phase", "running"), "◐")
            label = self._tool_entry_label(entry)
            plain = f"{icon} {ts} {label}"
            return f"{icon} {ts} {_html.escape(label)}", plain
        if kind == "thinking":
            c = entry.get("content", "")
            return f"💭 <i>{_html.escape(c)}</i>", f"💭 {c}"
        if kind == "text":
            content = "\n\n".join(entry.get("chunks") or [])
            content_lines = content.split("\n")
            first = content_lines[0]
            n_more = sum(1 for l in content_lines[1:] if l.strip())
            if entry.get("omitted"):
                n_more += entry["omitted"]
            suffix = f" (…{n_more} more lines)" if n_more else ""
            try:
                first_html = kiro_ui.inline_html(first)
            except Exception:
                first_html = _html.escape(first)
            return (f"✎ {first_html}{_html.escape(suffix)}",
                    f"✎ {first}{suffix}")
        if kind == "note":
            c = entry.get("content", "")
            return f"ℹ {ts} {_html.escape(c)}", f"ℹ {ts} {c}"
        return "", ""

    def _compose_pair(self) -> tuple[str, str]:
        """渲染累积条目 → (HTML, 纯文本回退)。长度核算用纯文本（=可见长度）。"""
        if not self._entries:
            return "", ""
        n = len(self._entries)
        if self.collapsed:
            plain = f"📄 «{self.session_label}» 详情 · {n}条 · 已收起"
            html = (f"📄 «<b>{_html.escape(self.session_label)}</b>» 详情"
                    f" · {n}条 · 已收起")
            return html, plain
        head_plain = f"📄 «{self.session_label}» 详情 · {n}条"
        head_html = f"📄 «<b>{_html.escape(self.session_label)}</b>» 详情 · {n}条"

        rendered: list[tuple[str, str]] = []  # (html, plain)
        prev_kind: str | None = None
        for entry in self._entries:
            kind = entry["kind"]
            if prev_kind and prev_kind != kind:
                rendered.append((_SEP, _SEP))
            prev_kind = kind
            pair = self._entry_pair(entry)
            if pair[1]:
                rendered.append(pair)

        # 总长度裁剪（按纯文本=可见长度），从最新往回保留
        total = len(head_plain) + 2
        kept: list[tuple[str, str]] = []
        dropped = 0
        for pair in reversed(rendered):
            if total + len(pair[1]) + 1 > self.PAGE_LIMIT:
                dropped = len(rendered) - len(kept)
                break
            kept.append(pair)
            total += len(pair[1]) + 1
        kept.reverse()

        html_lines = [head_html, ""]
        plain_lines = [head_plain, ""]
        if dropped:
            mark = f"…(早期 {dropped} 条已截断)"
            html_lines += [_html.escape(mark), ""]
            plain_lines += [mark, ""]
        for h, p in kept:
            html_lines.append(h)
            plain_lines.append(p)
        return "\n".join(html_lines), "\n".join(plain_lines)


def parse_bump_callback(data: str) -> tuple[int, int | None] | None:
    """解析 "detail:bump:<chat_id>:<thread_id|0>" callback_data。"""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "detail" or parts[1] != "bump":
        return None
    try:
        chat_id = int(parts[2])
        tid_raw = int(parts[3])
        thread_id = tid_raw if tid_raw != 0 else None
        return (chat_id, thread_id)
    except (ValueError, IndexError):
        return None


def parse_toggle_callback(data: str) -> tuple[str, int, int | None] | None:
    """解析 "detail:expand|collapse:<chat_id>:<thread_id|0>"。
    返回 (action, chat_id, thread_id)，action ∈ {"expand","collapse"}。"""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "detail" or parts[1] not in ("expand", "collapse"):
        return None
    try:
        chat_id = int(parts[2])
        tid_raw = int(parts[3])
        thread_id = tid_raw if tid_raw != 0 else None
        return (parts[1], chat_id, thread_id)
    except (ValueError, IndexError):
        return None


if __name__ == "__main__":
    # Smoke test: exercise 写入合并 + _compose_pair + helpers，无网络
    class _FakeBot:
        async def send_message(self, *a, **kw): pass
        async def edit_message_text(self, *a, **kw): pass

    class _FakeApp:
        bot = _FakeBot()

    async def _main():
        page = DetailPage(_FakeApp(), chat_id=123456, thread_id=7,
                          session_label="smoke-test")
        await page.add_text("Hello **world**\nSecond line")
        await page.add_text("merged paragraph")        # 应并入上一条 text
        await page.add_tool("Bash", "Running: ls /tmp", "running",
                            tool_id="t1", tool_input={"command": "ls /tmp"})
        await page.add_tool("Bash", "", "completed", tool_id="t1")
        await page.add_thinking("Let me check the directory...")
        await page.add_note("Permission granted")
        await page.add_text("Done. Found `3` files.")

        assert len(page._entries) == 5, page._entries  # 两条 text 合并成一条
        html_body, plain_body = page._compose_pair()
        assert "✓" in plain_body and "Ran: ls /tmp" in plain_body  # 过去式
        assert "<b>world</b>" in html_body
        assert "<code>3</code>" in html_body
        print(plain_body)
        print()
        print(html_body)

        # text 条目封顶
        page2 = DetailPage(_FakeApp(), chat_id=1, session_label="cap")
        await page2.add_text("x" * 5000)
        await page2.add_text("y" * 5000)   # 越过 cap
        await page2.add_text("z")          # 该被计入 omitted
        e = page2._entries[0]
        assert e["omitted"] == 1 and len(e["chunks"]) == 2
        print("[OK] text entry cap")

    asyncio.get_event_loop().run_until_complete(_main())

    # parse_bump_callback
    assert parse_bump_callback("detail:bump:123:456") == (123, 456)
    assert parse_bump_callback("detail:bump:123:0") == (123, None)
    assert parse_bump_callback("invalid") is None
    print("[OK] parse_bump_callback")
    assert parse_toggle_callback("detail:expand:9:0") == ("expand", 9, None)
    print("[OK] parse_toggle_callback")
    # link_button: forum
    p2 = DetailPage(_FakeApp(), chat_id=99, thread_id=5,
                    group_id=-1001234567890, session_label="forum")
    p2.msg_id = 42
    assert "t.me/c/1234567890/5/42" in p2.link_button().url
    print(f"[OK] forum URL: {p2.link_button().url}")
    # link_button: DM callback
    p3 = DetailPage(_FakeApp(), chat_id=99, thread_id=None, session_label="dm")
    assert p3.link_button().callback_data == "detail:bump:99:0"
    print(f"[OK] DM callback: {p3.link_button().callback_data}")
    print("\nAll smoke tests passed.")
