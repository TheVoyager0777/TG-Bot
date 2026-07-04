"""ask_question —— 把 Claude Code 的 AskUserQuestion 工具渲染成 TG 交互。

AskUserQuestion 走 can_use_tool 回调（不是普通工具执行）。它的 input 形如：
  {"questions": [
      {"question": "...", "header": "短标签", "multiSelect": false,
       "options": [{"label": "...", "description": "..."}, ...]},
      ... (1~4 个问题)
  ]}
答案要写回 input 的 `answers` 字段：{questionText: answerString}，经
PermissionResultAllow(updated_input=...) 回灌；CLI 据此拼 tool_result 给模型。

交互模型（TG inline 按钮，逐题进行）：
- 单选：点一个 option 即作答并进入下一题。
- 多选：点 option 切换勾选，点「✅ 提交」确认本题（多选答案用「、」连接）。
- 每题都带「⏭ 跳过」；用户也可直接发一条文本消息作为「Other」自定义答案。
- 全部作答（或取消）后 future 完成，返回填好 answers 的 input。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application

log = logging.getLogger("tgclaude.askq")


def _norm_questions(tool_input: dict) -> list[dict]:
    qs = tool_input.get("questions")
    if isinstance(qs, list) and qs:
        return [q for q in qs if isinstance(q, dict)]
    # 兜底：个别 provider 可能直接给单问题平铺
    if tool_input.get("question"):
        return [tool_input]
    return []


class AskSession:
    """一次 AskUserQuestion 交互的状态机。BotApp 为每个请求建一个，
    回调（按钮点击 / 文本输入）驱动它推进，全部作答后 resolve future。"""

    def __init__(self, app: Application, chat_id: int, worker: str,
                 tool_input: dict, thread_id: int | None = None):
        self.app = app
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.worker = worker
        self.tool_input = tool_input
        self.questions = _norm_questions(tool_input)
        self.token = uuid.uuid4().hex[:12]
        self.qi = 0                       # 当前题目索引
        self.answers: dict[str, str] = {} # questionText -> answer
        self.selected: set[int] = set()   # 多选当前题已勾选的 option 下标
        self.msg_id: int | None = None
        self.created = time.time()
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.done = False

    # ---- 渲染 ----
    def _cur(self) -> dict | None:
        if 0 <= self.qi < len(self.questions):
            return self.questions[self.qi]
        return None

    def _options(self, q: dict) -> list[dict]:
        opts = q.get("options")
        return [o for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []

    def _text(self) -> str:
        q = self._cur()
        if q is None:
            return "❓ 问题已全部作答"
        hdr = q.get("header") or "提问"
        n = len(self.questions)
        multi = bool(q.get("multiSelect"))
        lines = [f"❓ *{self.worker}* 提问 ({self.qi + 1}/{n}) · `{hdr}`",
                 "", q.get("question", "")]
        opts = self._options(q)
        if opts:
            lines.append("")
            for i, o in enumerate(opts):
                mark = "☑" if (multi and i in self.selected) else "▫"
                lbl = o.get("label", f"选项{i+1}")
                desc = o.get("description", "")
                lines.append(f"{mark} *{lbl}*" + (f" — {desc}" if desc else ""))
        if multi:
            lines.append("\n_多选：点选项切换勾选，选完点提交_")
        lines.append("\n_或直接发一条消息作为自定义答案_")
        return "\n".join(lines)

    def _keyboard(self) -> InlineKeyboardMarkup:
        q = self._cur() or {}
        multi = bool(q.get("multiSelect"))
        rows = []
        for i, o in enumerate(self._options(q)):
            lbl = o.get("label", f"选项{i+1}")
            if multi and i in self.selected:
                lbl = "✅ " + lbl
            rows.append([InlineKeyboardButton(
                lbl[:60], callback_data=f"askq:{self.token}:opt:{i}")])
        tail = []
        if multi:
            tail.append(InlineKeyboardButton(
                "✅ 提交", callback_data=f"askq:{self.token}:submit:0"))
        tail.append(InlineKeyboardButton(
            "⏭ 跳过", callback_data=f"askq:{self.token}:skip:0"))
        tail.append(InlineKeyboardButton(
            "✖ 取消", callback_data=f"askq:{self.token}:cancel:0"))
        rows.append(tail)
        return InlineKeyboardMarkup(rows)

    async def _render(self):
        kw = {"reply_markup": self._keyboard(), "parse_mode": ParseMode.MARKDOWN}
        if self.thread_id is not None:
            kw["message_thread_id"] = self.thread_id
        try:
            if self.msg_id is None:
                m = await self.app.bot.send_message(self.chat_id, self._text(), **kw)
                self.msg_id = m.message_id
            else:
                await self.app.bot.edit_message_text(
                    self._text(), chat_id=self.chat_id, message_id=self.msg_id, **{
                        k: v for k, v in kw.items() if k in ("reply_markup", "parse_mode")})
        except Exception as e:
            # markdown 解析失败兜底为纯文本
            log.debug("askq render md failed (%s), retry plain", e)
            kw.pop("parse_mode", None)
            try:
                if self.msg_id is None:
                    m = await self.app.bot.send_message(self.chat_id, self._text(), **kw)
                    self.msg_id = m.message_id
                else:
                    await self.app.bot.edit_message_text(
                        self._text(), chat_id=self.chat_id, message_id=self.msg_id,
                        reply_markup=self._keyboard())
            except Exception as e2:
                log.warning("askq render failed: %s", e2)

    async def start(self) -> asyncio.Future:
        if not self.questions:
            self._finish()  # 没问题可问，直接放行（无答案）
            return self.future
        await self._render()
        return self.future

    # ---- 推进 ----
    def _record(self, answer: str):
        q = self._cur()
        if q is not None:
            self.answers[q.get("question", f"q{self.qi}")] = answer

    async def _advance(self):
        """进入下一题或收尾。"""
        self.qi += 1
        self.selected = set()
        if self._cur() is None:
            await self._finalize_msg()
            self._finish()
        else:
            await self._render()

    async def _finalize_msg(self):
        """全部作答后把消息定格成摘要（去掉按钮）。"""
        if self.msg_id is None:
            return
        if self.answers:
            summary = "\n".join(f"• {k} → {v}" for k, v in self.answers.items())
            body = f"✅ *{self.worker}* 的提问已回答：\n{summary}"
        else:
            body = f"⏭ *{self.worker}* 的提问未作答"
        try:
            await self.app.bot.edit_message_text(
                body, chat_id=self.chat_id, message_id=self.msg_id,
                parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await self.app.bot.edit_message_text(
                    body.replace("*", ""), chat_id=self.chat_id, message_id=self.msg_id)
            except Exception:
                pass

    def _build_updated_input(self) -> dict:
        """把 answers 写回 input（CLI 读 input.answers 拼 tool_result）。"""
        out = dict(self.tool_input)
        out["answers"] = dict(self.answers)
        return out

    def _finish(self, cancelled: bool = False):
        if self.done:
            return
        self.done = True
        if self.future.done():
            return
        if cancelled and not self.answers:
            self.future.set_result(None)  # 完全取消 → 不带答案放行
        else:
            self.future.set_result(self._build_updated_input())

    # ---- 外部事件入口（BotApp 回调里调用）----
    async def on_button(self, action: str, idx: int):
        q = self._cur()
        if q is None:
            return
        multi = bool(q.get("multiSelect"))
        opts = self._options(q)
        if action == "opt":
            if multi:
                self.selected.symmetric_difference_update({idx})  # toggle
                await self._render()
            else:
                if 0 <= idx < len(opts):
                    self._record(opts[idx].get("label", f"选项{idx+1}"))
                await self._advance()
        elif action == "submit":
            if multi:
                labels = [opts[i].get("label", f"选项{i+1}")
                          for i in sorted(self.selected) if 0 <= i < len(opts)]
                if labels:
                    self._record("、".join(labels))
                await self._advance()
        elif action == "skip":
            await self._advance()
        elif action == "cancel":
            await self._finalize_msg()
            self._finish(cancelled=True)

    async def on_text(self, text: str):
        """用户直接发文本作为当前题的自定义答案。"""
        if self._cur() is None:
            return
        self._record(text.strip())
        await self._advance()


