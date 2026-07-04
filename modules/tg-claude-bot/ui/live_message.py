"""live_message —— Kiro 风格主气泡。

呈现模型（对齐 Kiro IDE 聊天面板）：
1) 状态头：◐ 运行中 · 12s · 🔧 3  →  ✓ 完成 · 32s（+ 一行 usage 统计）
2) 工具时间线：最近 N 条工具各占一行，原地变态 ◐ → ✓ / ✗ / ⊘（Kiro 的 tool rows）
3) 思考折叠：💭 单行摘要（完成态隐藏，详情页保留全量）
4) 正文流：运行中显示最新一段纯文本；完成态用 Telegram HTML 渲染 markdown

性能契约：append/event/note 绝不内联做 TG 网络 I/O（SDK 消费循环不被网络卡住），
全部经 dirty + trailing 任务异步刷新；只有 finalize / 心跳 / trailing 真正编辑消息。
全部历史压入 DetailPage（详情消息）。Todo 走专属 TodoBubble 不混本主气泡。
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import time

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.error import BadRequest
from telegram.ext import Application

from ui import kiro_ui
from ui.detail_page import DetailPage
from infra.event_log import BUS
from ui.todo_bubble import TodoBubble, todo_text

log = logging.getLogger("tgclaude.ui")

TG_LIMIT = 3900
_PARSE_ERR_HINTS = ("parse", "entit", "tag")


def _is_parse_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(h in s for h in _PARSE_ERR_HINTS)


def _safe_todos(todos: list) -> list[dict]:
    """TodoWrite input → JSON 安全的精简列表（事件总线/控制台用）。"""
    out = []
    for t in todos or []:
        if not isinstance(t, dict):
            continue
        st = (t.get("status") or t.get("state") or "pending").lower()
        out.append({"text": todo_text(t, st), "status": st})
    return out[:50]


class LiveMessage:
    """单 turn 的"主气泡"：节流编辑同一条 TG 消息。"""
    EDIT_INTERVAL = 1.1
    HEARTBEAT = 5.0
    # 心跳自熄：超过这么久没有任何新事件仍在"运行中"，停掉心跳编辑（气泡定格，
    # 防止半挂的上游让 bot 对一条静止消息无限期每 5s 打一次 TG API）。
    IDLE_GIVEUP = 1800.0
    # 主气泡里保留的工具时间线行数（再早的滚进详情页）
    MAX_TOOL_ROWS = 6
    # 主气泡正文窗口（流式 / 完成态单卡）；全文在详情页与独立回答消息里
    TEXT_WINDOW_LIVE = 1500
    TEXT_WINDOW_FINAL = 3500

    SEP = "─────"

    ST_RUN = "running"
    ST_INT = "interrupted"
    ST_DONE = "done"
    ST_ERR = "error"

    _ST_LABEL = {ST_INT: "⊘ 已中断", ST_ERR: "✗ 出错", ST_DONE: "✓ 完成"}

    def __init__(self, app: Application, chat_id: int, prefix: str = "",
                 thread_id: int | None = None,
                 *, group_id: int | None = None, session_label: str = "",
                 detail: DetailPage | None = None):
        self.app = app
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.prefix = prefix
        # "最新槽"：正文 / 思考；工具改为时间线（最近 MAX_TOOL_ROWS 条）
        self.cur_text = ""             # 最新一段流式正文
        self.prev_text = ""            # 最近被翻页的一段（完成态正文兜底）
        self.cur_thinking: str = ""    # 最新一行思考 / 注记
        # 工具时间线：tool_use_id -> {tool, input, phase, summary}（dict 保序）。
        # tool_rows 是主气泡显示窗口（最多 MAX_TOOL_ROWS 条）；
        # _tool_all 保整个 turn 的全量（两段式收尾的收据卡时间线用）。
        self.tool_rows: dict[str, dict] = {}
        self._tool_all: dict[str, dict] = {}
        self.stats_line = ""           # 完成态 usage 统计（set_result 填）
        self.msg_id: int | None = None
        self.last_edit = 0.0
        self.last_activity = time.time()
        self.dirty = False
        self._lock = asyncio.Lock()
        self.status = self.ST_RUN
        self.started = time.time()
        self._finalized = False
        self._hb_task: asyncio.Task | None = None
        self._trailing_task: asyncio.Task | None = None
        # 详情页：所有滚走的内容压到这里。可外部预设以便复用。
        self.detail = detail or DetailPage(
            app, chat_id, thread_id,
            group_id=group_id, session_label=session_label or "session")
        self._xml_warned = False
        # 子代理详情页：每个 parent_tool_use_id 一个，复用同一 chat/thread。
        self.subagent_pages: dict[str, DetailPage] = {}
        # Todo 专属气泡：第一次见 TodoWrite 时懒创建，后续 TodoWrite 编辑同一条
        self._session_label = session_label or "session"
        self.todo_bubble: TodoBubble | None = None
        BUS.emit(self._session_label, "turn_start")

    # ── 心跳 / 节流 ──────────────────────────────────────────────────────────
    def start_heartbeat(self):
        if self._hb_task is None:
            self._hb_task = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self):
        try:
            while self.status == self.ST_RUN:
                await asyncio.sleep(self.HEARTBEAT)
                if self.status != self.ST_RUN:
                    break
                if (time.time() - self.last_activity) > self.IDLE_GIVEUP:
                    log.debug("heartbeat idle-giveup (chat=%s)", self.chat_id)
                    break
                await self._flush(force=True)
        except asyncio.CancelledError:
            pass

    def _stop_heartbeat(self):
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
        self._hb_task = None
        if self._trailing_task and not self._trailing_task.done():
            self._trailing_task.cancel()
        self._trailing_task = None

    def _mark_dirty(self):
        """热路径统一入口：只记脏 + 安排尾随刷新，绝不内联碰网络。"""
        self.dirty = True
        self.last_activity = time.time()
        self._schedule_trailing()

    def _schedule_trailing(self):
        if self._trailing_task and not self._trailing_task.done():
            return
        delay = self.EDIT_INTERVAL - (time.time() - self.last_edit)
        delay = max(0.05, delay)

        async def _run():
            try:
                await asyncio.sleep(delay)
                if self.dirty:
                    await self._flush(force=True)
            except asyncio.CancelledError:
                pass
        self._trailing_task = asyncio.create_task(_run())

    def set_status(self, status: str):
        self.status = status

    def set_result(self, msg) -> None:
        """轮末 ResultMessage → 一行 usage 统计，finalize 时并入状态头下方。"""
        try:
            self.stats_line = kiro_ui.format_result_stats(msg)
            if self.stats_line:
                BUS.emit(self._session_label, "result", stats=self.stats_line)
        except Exception as e:
            log.debug("set_result skip: %s", e)

    # ── 状态头 ────────────────────────────────────────────────────────────────
    def _duration(self) -> str:
        el = int(time.time() - self.started)
        m, s = divmod(el, 60)
        return f"{m}m{s:02d}s" if m else f"{el}s"

    def _status_header(self) -> str:
        n_tools = len(self._tool_all)
        tools = f" · 🔧 {n_tools}" if n_tools else ""
        if self.status == self.ST_RUN:
            spin = "◐◓◑◒"[int((time.time() - self.started) * 2) % 4]
            return f"{spin} 运行中 · {self._duration()}{tools}"
        return f"{self._ST_LABEL.get(self.status, self.status)} · {self._duration()}{tools}"

    # ── 写入（热路径：无网络 I/O）────────────────────────────────────────────
    async def append(self, text: str):
        """流式正文：累加到 cur_text。同步把这段也压详情页（按段合并）。
        SDK 的每次 on_text = 一个完整 TextBlock（≈段落），块间补段落分隔，
        避免相邻消息块被无缝粘连（"…done.Now I'll…"）。"""
        if not text:
            return
        if self.cur_text and not self.cur_text.endswith("\n"):
            self.cur_text += "\n\n"
        self.cur_text += text
        self._maybe_warn_xml_protocol(text)
        BUS.emit(self._session_label, "text", text=text)
        await self.detail.add_text(text)
        self._mark_dirty()

    def _rotate_text(self):
        """段落语义边界（新工具/新思考到来）：当前 cur_text 视作上一段完成的输出。
        旧段已在 append 时进了 DetailPage；这里留一份在 prev_text 供完成态兜底。"""
        if self.cur_text:
            self.prev_text = self.cur_text
        self.cur_text = ""

    def _maybe_warn_xml_protocol(self, new_chunk: str):
        if self._xml_warned:
            return
        # 只扫尾部窗口（新 chunk + 跨界余量），避免长正文 O(n²) 重复全扫
        window = self.cur_text[-(len(new_chunk) + 64):]
        markers = ("<invoke name=", "</invoke>",
                   "<function_calls>", "</function_calls>")
        if any(m in window for m in markers):
            self._xml_warned = True
            self.cur_thinking = "⚠️ provider 漏 tool_use 协议（XML 流出）— 试 /providers 换端点"

    async def event(self, ev):
        """结构化事件：tool / thinking / subagent_text。
        参数 ev 可以是 dict 或纯字符串（旧路径兼容：当作 note）。"""
        if isinstance(ev, str):
            await self.note(ev)
            return
        kind = ev.get("kind")
        # 子代理产出：分流到子代理详情页，不进主气泡
        if kind == "subagent_text":
            parent = ev.get("parent") or ""
            page = self._ensure_subagent_page(parent)
            BUS.emit(self._session_label, "subagent_text",
                     parent=parent[:8], text=ev.get("text", "")[:2000])
            await page.add_text(ev.get("text", ""))
            return
        if kind == "tool":
            parent = ev.get("parent") or ""
            tid = ev.get("id") or ""
            phase = ev.get("phase", "running")
            tool = ev.get("tool", "")
            tinput = ev.get("input", {}) or {}
            summary = " ".join(str(ev.get("summary") or "").split())
            # TodoWrite：旁路到 TodoBubble，不进主气泡时间线
            if tool == "TodoWrite" and not parent:
                if phase == "running":
                    todos = tinput.get("todos") if isinstance(tinput, dict) else None
                    if todos:
                        if self.todo_bubble is None:
                            self.todo_bubble = TodoBubble(
                                self.app, self.chat_id, self.thread_id,
                                session_label=self._session_label)
                        await self.todo_bubble.update(todos)
                        BUS.emit(self._session_label, "todo",
                                 items=_safe_todos(todos))
                await self.detail.add_tool(tool, "Updating task list", phase,
                                           tool_id=tid, tool_input=tinput or None)
                return
            label = self._tool_label(tool, tinput, phase)
            if parent:
                # 子代理在干的工具：写到子代理页，不影响主气泡
                page = self._ensure_subagent_page(parent)
                await page.add_tool(tool, label, phase, tool_id=tid,
                                    tool_input=tinput or None, summary=summary)
                BUS.emit(self._session_label, "tool", id=tid, label=label,
                         phase=phase, summary=summary, parent=parent[:8],
                         input=tinput or None)
                return
            # 主线工具：进时间线（原地变态）。新工具 = 段落边界。
            # _tool_all 保全量（含已滚出窗口的），结果回执仍能原地更新。
            row = self._tool_all.get(tid)
            if row is not None:
                row["phase"] = phase
                if tool:
                    row["tool"] = tool
                if summary:
                    row["summary"] = summary
            else:
                row = {"tool": tool, "input": tinput, "phase": phase,
                       "summary": summary}
                self._tool_all[tid] = row
                self.tool_rows[tid] = row     # 同一 dict 引用，窗口内原地变态
                while len(self.tool_rows) > self.MAX_TOOL_ROWS:
                    self.tool_rows.pop(next(iter(self.tool_rows)))
                self._rotate_text()
            BUS.emit(self._session_label, "tool", id=tid, label=label,
                     phase=phase, summary=summary, parent="",
                     input=tinput or None)
            await self.detail.add_tool(tool, label, phase, tool_id=tid,
                                       tool_input=tinput or None, summary=summary)
            self._mark_dirty()
            return
        if kind == "thinking":
            parent = ev.get("parent") or ""
            text = (ev.get("text") or "").strip()
            if not text:
                return
            if parent:
                page = self._ensure_subagent_page(parent)
                await page.add_thinking(text)
                return
            self.cur_thinking = text
            self._rotate_text()
            BUS.emit(self._session_label, "thinking", text=text[:1000])
            await self.detail.add_thinking(text)
            self._mark_dirty()
            return
        # 未知 kind：当 note 处理
        if ev.get("text"):
            await self.note(str(ev["text"]))

    async def note(self, text: str):
        """系统注记（权限授予/对话框已结束/排队中…）。"""
        text = (text or "").strip()
        if not text:
            return
        BUS.emit(self._session_label, "note", text=text[:500])
        await self.detail.add_note(text)
        # Don't overwrite cur_thinking — notes are separate from reasoning
        self._mark_dirty()

    # ── 子代理页 ──────────────────────────────────────────────────────────────
    def _ensure_subagent_page(self, parent_id: str) -> DetailPage:
        page = self.subagent_pages.get(parent_id)
        if page is None:
            label = f"子代理 #{parent_id[:8]}"
            page = DetailPage(
                self.app, self.chat_id, self.thread_id,
                group_id=getattr(self.detail, "group_id", None),
                session_label=label)
            self.subagent_pages[parent_id] = page
        return page

    def has_subagent_pages(self) -> bool:
        return bool(self.subagent_pages)

    # ── 渲染 ──────────────────────────────────────────────────────────────────
    def _tool_label(self, tool: str, tinput: dict, phase: str) -> str:
        try:
            past = phase in ("completed", "error")
            return kiro_ui.tool_label(tool, tinput, past=past)
        except Exception:
            return tool or "tool"

    @staticmethod
    def _row_line(row: dict, summary_limit: int = 80) -> str:
        """单条工具行：图标 + 标签 (+ · 结果摘要)。"""
        try:
            line = kiro_ui.tool_line(
                row.get("tool", ""), row.get("input"), row.get("phase", "running"))
        except Exception:
            line = row.get("tool", "tool")
        s = row.get("summary") or ""
        if s and row.get("phase") in ("completed", "error"):
            if len(s) > summary_limit:
                s = s[:summary_limit - 1] + "…"
            line += f" · {s}"
        return line

    def _timeline_lines(self) -> list[str]:
        lines = []
        hidden = len(self._tool_all) - len(self.tool_rows)
        if hidden > 0:
            lines.append(f"  ⋯ 早前 {hidden} 个工具见详情")
        for row in self.tool_rows.values():
            lines.append(self._row_line(row))
        return lines

    def _thinking_block(self) -> str:
        if not self.cur_thinking:
            return ""
        t = self.cur_thinking
        if t.startswith(("ℹ", "⚠")):
            return t
        return kiro_ui.thinking_line(t)

    def _text_block(self) -> str:
        if not self.cur_text:
            return ""
        body = self.cur_text.rstrip()
        # 主气泡只留正文末尾一段；超长截断（详情页有完整版）
        if len(body) > self.TEXT_WINDOW_LIVE:
            body = "…(早段见详情)…\n" + body[-(self.TEXT_WINDOW_LIVE - 100):]
        return body

    def _compose(self) -> str:
        """运行中 / 纯文本兜底渲染。"""
        sections = [self._status_header()]
        if self.status != self.ST_RUN and self.stats_line:
            sections.append(self.stats_line)
        tl = self._timeline_lines()
        if tl:
            sections.append(self.SEP)
            sections.extend(tl)
        for blk in (self._thinking_block(), self._text_block()):
            if blk:
                sections.append(self.SEP)
                sections.append(blk)
        return "\n".join(sections)

    def _thinking_html(self) -> str:
        blk = self._thinking_block()
        if not blk:
            return ""
        if blk.startswith(("ℹ", "⚠")):  # 注记/告警保持原样式
            return _html.escape(blk)
        # "💭 xxx" → spoiler 折叠（点击显形，对应 Kiro 收起的 reasoning）
        return "💭 <tg-spoiler>" + _html.escape(blk[len("💭 "):]) + "</tg-spoiler>"

    def _compose_html(self, final: bool = False) -> str:
        """富文本渲染（Kiro：工具时间线 + markdown 正文）。
        运行中带思考行；完成态收起思考（详情页保留全量）、并入 usage 统计。"""
        sections = [_html.escape(self._status_header())]
        if final and self.stats_line:
            sections.append(_html.escape(self.stats_line))
        tl = self._timeline_lines()
        if tl:
            sections.append(self.SEP)
            sections.extend(_html.escape(l) for l in tl)
        if not final:
            tb = self._thinking_html()
            if tb:
                sections.append(self.SEP)
                sections.append(tb)
        body = (self.cur_text if not final
                else (self.cur_text or self.prev_text)).rstrip()
        if body:
            if final and len(body) > self.TEXT_WINDOW_FINAL:
                body = "…(早段见详情)…\n" + body[-(self.TEXT_WINDOW_FINAL - 100):]
            elif not final and len(body) > self.TEXT_WINDOW_LIVE:
                body = "…(早段见详情)…\n" + body[-(self.TEXT_WINDOW_LIVE - 100):]
            sections.append(self.SEP)
            sections.append(kiro_ui.md_to_html(body))
        return "\n".join(sections)

    def _build_markup(self) -> InlineKeyboardMarkup | None:
        """主气泡按钮：📄 详情；若有子代理活动，再加跳子代理详情的按钮。"""
        rows = [[self.detail.link_button()]]
        sub_btns = []
        for pid, page in list(self.subagent_pages.items())[:4]:
            try:
                sub_btns.append(page.link_button())
            except Exception:
                pass
        if sub_btns:
            row: list = []
            for b in sub_btns:
                row.append(b)
                if len(row) == 2:
                    rows.append(row); row = []
            if row:
                rows.append(row)
        return InlineKeyboardMarkup(rows)

    # ── flush / render ────────────────────────────────────────────────────────
    async def _flush(self, force: bool = False):
        async with self._lock:
            now = time.time()
            if not force and (now - self.last_edit) < self.EDIT_INTERVAL:
                self.dirty = True
                self._schedule_trailing()
                return
            await self._render_rich(final=False)
            self.last_edit = now
            self.dirty = False

    async def _render_rich(self, final: bool):
        """HTML 优先渲染；仅当超长/实体解析失败时回退纯文本。"""
        try:
            await self._render(self._compose_html(final=final),
                               parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await self._render(self._compose())
            except Exception as e:
                log.debug("plain render skip: %s", e)

    async def _render(self, body: str, parse_mode: str | None = None):
        if parse_mode is None:
            body = (self.prefix + body).rstrip() or "…"
        else:
            body = (_html.escape(self.prefix) + body).rstrip() or "…"
        if len(body) > TG_LIMIT:
            if parse_mode is not None:
                # 富文本超长不能硬截（会切断标签）：抛给调用方回退纯文本
                raise ValueError("html body too long")
            body = body[:TG_LIMIT - 1] + "…"
        kw: dict = {}
        if parse_mode is not None:
            kw["parse_mode"] = parse_mode
        markup = self._build_markup()
        for attempt in range(3):
            try:
                if self.msg_id is None:
                    send_kw = dict(kw)
                    if self.thread_id is not None:
                        send_kw["message_thread_id"] = self.thread_id
                    m = await self.app.bot.send_message(
                        self.chat_id, body, reply_markup=markup, **send_kw)
                    self.msg_id = m.message_id
                else:
                    await self.app.bot.edit_message_text(
                        body, chat_id=self.chat_id, message_id=self.msg_id,
                        reply_markup=markup, **kw)
                return   # 成功
            except BadRequest as e:
                if parse_mode is not None and _is_parse_error(e):
                    raise
                log.debug("render skip: %s", e)
                return   # 硬错误不重试
            except (asyncio.TimeoutError, RetryAfter) as e:
                delay = getattr(e, "retry_after", 2.0) if isinstance(e, RetryAfter) else 2.0
                log.debug("render transient, retry %d/3 in %.1fs: %s",
                         attempt + 1, delay, e)
                await asyncio.sleep(delay)
            except Exception as e:
                log.debug("render skip: %s", e)
                return   # 未知错误不重试

    def _receipt_html(self) -> str:
        """两段式收尾的「收据卡」：状态头 + 统计 + 可展开工具时间线（原生折叠）。"""
        sections = [_html.escape(self._status_header())]
        if self.stats_line:
            sections.append(_html.escape(self.stats_line))
        rows = list(self._tool_all.values())
        if rows:
            shown = rows[-30:]
            lines = []
            if len(rows) > len(shown):
                lines.append(f"⋯ 早前 {len(rows) - len(shown)} 个工具见详情")
            lines.extend(self._row_line(r) for r in shown)
            sections.append("<blockquote expandable>"
                            + _html.escape("\n".join(lines))
                            + "</blockquote>")
        return "\n".join(sections)

    async def _send_answer_message(self) -> bool:
        """把最终回答作为独立干净消息发出（Kiro：成品即正文，无状态头杂音）。"""
        body = (self.cur_text or self.prev_text).rstrip()
        if not body:
            return False
        if len(body) > 3500:
            body = "…(早段见详情)…\n" + body[-3400:]
        kw: dict = {}
        if self.thread_id is not None:
            kw["message_thread_id"] = self.thread_id
        html_body = (_html.escape(self.prefix) + kiro_ui.md_to_html(body)).strip()
        try:
            await self.app.bot.send_message(
                self.chat_id, html_body, parse_mode=ParseMode.HTML, **kw)
            return True
        except BadRequest as e:
            if not _is_parse_error(e):
                log.debug("answer send skip: %s", e)
                return False
        except Exception as e:
            log.debug("answer send failed: %s", e)
            return False
        try:
            await self.app.bot.send_message(
                self.chat_id, (self.prefix + body).strip()[:TG_LIMIT], **kw)
            return True
        except Exception as e:
            log.debug("answer plain send failed: %s", e)
            return False

    async def finalize(self, status: str | None = None):
        # 幂等：重复 finalize（桥接流轮换/stop 双路径等）不得重发回答消息
        if self._finalized:
            return
        self._finalized = True
        if status:
            self.status = status
        elif self.status == self.ST_RUN:
            self.status = self.ST_DONE
        self._stop_heartbeat()
        BUS.emit(self._session_label, "turn_end", status=self.status,
                 stats=self.stats_line)
        async with self._lock:
            # 两段式收尾：正常完成 + 有工具活动 + 有正文 → 运行卡缩成收据，
            # 最终回答另发干净消息（历史变成「问 → 答」的节奏）。
            # 纯文本轮（无工具）/中断/出错 → 维持单卡定格（避免无谓的两条消息）。
            two_stage = (self.status == self.ST_DONE and self._tool_all
                         and (self.cur_text or self.prev_text).strip()
                         and self.msg_id is not None)
            if two_stage and await self._send_answer_message():
                try:
                    await self._render(self._receipt_html(),
                                       parse_mode=ParseMode.HTML)
                except Exception:
                    try:
                        await self._render(self._compose())
                    except Exception as e:
                        log.debug("receipt plain render skip: %s", e)
            else:
                await self._render_rich(final=True)
            self.last_edit = time.time()
            self.dirty = False
        try:
            await self.detail.finalize()
        except Exception:
            pass
        for page in self.subagent_pages.values():
            try:
                await page.finalize()
            except Exception:
                pass
        if self.todo_bubble is not None:
            try:
                await self.todo_bubble.finalize()
            except Exception:
                pass


# ── 兼容旧导出 ────────────────────────────────────────────────────────────────
def short_input(d: dict, n: int = 80) -> str:
    if not d:
        return ""
    for key in ("command", "file_path", "path", "pattern", "query", "url", "name", "message"):
        if key in d:
            v = str(d[key])
            return v[:n] + ("…" if len(v) > n else "")
    s = ", ".join(f"{k}={str(v)[:30]}" for k, v in list(d.items())[:3])
    return s[:n]


def tool_preview(tool: str, d: dict) -> str:
    if tool == "Bash":
        return f"```\n{d.get('command','')[:1000]}\n```"
    if tool in ("Write", "Edit"):
        fp = d.get("file_path", "?")
        if "old_string" in d:
            return f"`{fp}`\nreplace {len(d.get('old_string',''))}→{len(d.get('new_string',''))} chars"
        if "content" in d:
            return f"`{fp}`\nwrite {len(d.get('content',''))} chars"
        return f"`{fp}`"
    return short_input(d, 300)
