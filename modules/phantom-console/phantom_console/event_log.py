"""event_log —— 进程级会话事件总线（Mini App 控制台的数据源）。

LiveMessage 在产生 UI 事件的同时往这里旁路一份规范化事件；
webapp 的长轮询端点从这里消费。设计：

- 环形缓冲（maxlen 条）：重连的客户端能拿到近期回放。
- 单调 seq：客户端带 since 续传，断线不丢不重。
- emit() 是同步零等待（热路径友好）；订阅端用 Future 唤醒。
- JSONL 持久化：append-only 日志，重启后 load_history() 回放最近 N 条到缓冲。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque


def coalesce_events(events: list[dict]) -> list[dict]:
    """Merge adjacent text/thinking deltas for compact history responses."""
    out: list[dict] = []
    for ev in events:
        if (out and ev.get("type") in {"text", "thinking"} and
                out[-1].get("type") == ev.get("type") and
                (out[-1].get("session") or "main") == (ev.get("session") or "main") and
                out[-1].get("source") == ev.get("source") and
                not ev.get("parent") and not out[-1].get("parent")):
            prev = out[-1]
            prev["text"] = str(prev.get("text") or "") + str(ev.get("text") or "")
            prev["seq"] = ev.get("seq", prev.get("seq"))
            prev["ts"] = ev.get("ts", prev.get("ts"))
            continue
        out.append(dict(ev))
    return out


class EventBus:
    def __init__(self, maxlen: int = 12000, log_path: str = ""):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._waiters: list[asyncio.Future] = []
        self._log_path = log_path
        self._emit_count = 0

    # ── 生产端（同步、零等待）────────────────────────────────────────────────
    def emit(self, session: str, etype: str, **data) -> None:
        self._seq += 1
        ev = {"seq": self._seq, "ts": round(time.time(), 3),
              "session": session or "main", "type": etype, **data}
        self._buf.append(ev)
        # JSONL 持久化 + 定期轮转（不阻塞热路径）
        if self._log_path:
            try:
                line = json.dumps(ev, ensure_ascii=False) + "\n"
                with open(self._log_path, "a") as f:
                    f.write(line)
                self._emit_count += 1
                # 每 ~200 条轮转一次。流式 text/thinking 事件很密，保留窗口
                # 需要足够大，避免历史回放落在一个 turn 的中间。
                if self._emit_count % 200 == 0:
                    self._rotate_log(keep=20000)
            except Exception:
                pass
        # 唤醒等待者
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    # ── 消费端 ────────────────────────────────────────────────────────────────
    def backlog(self, since: int = 0, limit: int = 1000,
                session: str | None = None) -> list[dict]:
        """seq > since 的事件（最多 limit 条）。
        session 非空 → 服务端先按 session 过滤再截 limit，保证拿到该 session 完整近期
        历史（不会被其它高频 session 挤出窗口）。
        __system__ 会话的事件（如 hot_reload）始终包含，不受 session 过滤。"""
        if session:
            out = [ev for ev in self._buf
                   if ev["seq"] > since and (
                       (ev.get("session") or "main") == session
                       or ev.get("session") == "__system__")]
        else:
            out = [ev for ev in self._buf if ev["seq"] > since]
        return out[-limit:]

    def history_backlog(self, limit: int = 1000,
                        session: str | None = None) -> list[dict]:
        """Recent history adjusted to render as whole bubbles."""
        if session:
            out = [ev for ev in self._buf
                   if (ev.get("session") or "main") == session]
        else:
            out = [ev for ev in self._buf if ev.get("session") != "__system__"]
        if not out:
            return []

        def normalized(seg: list[dict]) -> list[dict]:
            if not seg:
                return []
            if seg[0].get("type") in {"user", "turn_start"}:
                return seg
            if not any(e.get("type") == "turn_end" for e in seg):
                return []
            recovered = dict(seg[0])
            recovered["seq"] = min(e.get("seq", 0) for e in seg) - 1
            recovered["type"] = "turn_start"
            recovered["source"] = "event-log-recovered"
            recovered.pop("text", None)
            return [recovered] + seg

        segments: list[list[dict]] = []
        cur: list[dict] = []

        def finish() -> None:
            nonlocal cur
            seg = normalized(cur)
            if seg:
                segments.append(seg)
            cur = []

        for ev in out:
            typ = ev.get("type")
            if typ == "user":
                finish()
                cur = [ev]
                continue
            if typ == "turn_start":
                if cur and cur[-1].get("type") == "user":
                    cur.append(ev)
                else:
                    finish()
                    cur = [ev]
                continue
            if not cur:
                cur = [ev]
            else:
                cur.append(ev)
            if typ == "turn_end":
                finish()

        finish()
        if not segments:
            return []

        max_events = max(limit, 1)
        min_segments = min(8, len(segments))
        picked: list[list[dict]] = []
        total = 0
        for seg in reversed(segments):
            picked.append(seg)
            total += len(seg)
            if len(picked) >= min_segments and total >= max_events:
                break
        picked.reverse()
        return coalesce_events([ev for seg in picked for ev in seg])

    def _rotate_log(self, keep: int = 2500):
        """保留 JSONL 尾部 keep 行，防无限增长。"""
        try:
            with open(self._log_path) as f:
                lines = f.readlines()
            if len(lines) > keep * 2:
                with open(self._log_path, "w") as f:
                    f.writelines(lines[-keep:])
        except Exception:
            pass

    def load_history(self, path: str = "", n: int = 500) -> int:
        """从 JSONL 日志回放最近 n 条事件到缓冲。返回加载条数。"""
        p = path or self._log_path
        if not p or not os.path.exists(p):
            return 0
        try:
            lines = []
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
            count = 0
            for line in lines[-n:]:
                try:
                    ev = json.loads(line)
                    self._buf.append(ev)
                    if ev.get("seq", 0) > self._seq:
                        self._seq = ev["seq"]
                    count += 1
                except Exception:
                    pass
            return count
        except Exception:
            return 0

    @property
    def seq(self) -> int:
        return self._seq

    def sessions(self) -> list[str]:
        return sorted({ev["session"] for ev in self._buf})

    async def wait(self, since: int, timeout: float = 25.0,
                   session: str | None = None) -> list[dict]:
        """长轮询：有 seq > since 的事件立即返回，否则最多等 timeout 秒。
        session 非空时只关心该 session 的事件（其它 session 不唤醒本轮）。"""
        evs = self.backlog(since, session=session)
        if evs:
            return evs
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await asyncio.wait_for(fut, remaining)
            except asyncio.TimeoutError:
                break
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)
            evs = self.backlog(since, session=session)
            if evs:
                return evs
        return self.backlog(since, session=session)


# 进程级单例
BUS = EventBus()
