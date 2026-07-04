"""Background task manager — long-running shell tasks with EventBus streaming.

Tasks are submitted via API, run as subprocesses, and emit real-time events
to the console EventBus.  The console polls /api/tasks for snapshot state and
receives live output via long-poll / WebSocket event streams.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# ── Data ────────────────────────────────────────────────────────────────────────

@dataclass
class BgTask:
    id: str
    session: str
    label: str
    command: str
    cwd: str
    status: str = "queued"   # queued | running | done | error | killed
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    output_buf: list[str] = field(default_factory=list)
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session": self.session,
            "label": self.label,
            "command": self.command,
            "cwd": self.cwd,
            "status": self.status,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "output_tail": "".join(self.output_buf[-20:])[-2000:],
            "duration_s": round(
                (self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at else 0,
        }


# ── Manager ─────────────────────────────────────────────────────────────────────

class BgTaskManager:
    """In-memory background task queue with EventBus integration."""

    def __init__(self, bus=None, max_output_lines: int = 200):
        self._tasks: Dict[str, BgTask] = {}
        self._bus = bus
        self._max_output_lines = max_output_lines

    def set_bus(self, bus):
        self._bus = bus

    def _emit(self, ev_type: str, **kw):
        if self._bus is None:
            return
        try:
            self._bus.emit(kw.get("session", "main"), ev_type, **kw)
        except Exception:
            pass

    async def submit(self, session: str, label: str, command: str,
                     cwd: str = "/tmp") -> str:
        """Queue a background task. Returns task id."""
        import uuid
        tid = uuid.uuid4().hex[:8]
        task = BgTask(
            id=tid, session=session, label=label,
            command=command, cwd=cwd,
            status="queued", created_at=time.time(),
        )
        self._tasks[tid] = task
        self._emit("bg_task", task=task.to_dict())
        # Start immediately (queued state emitted, now run)
        asyncio.create_task(self._run(task))
        return tid

    async def _run(self, task: BgTask):
        task.status = "running"
        task.started_at = time.time()
        self._emit("bg_task", task=task.to_dict())
        try:
            proc = await asyncio.create_subprocess_shell(
                task.command,
                cwd=task.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            task.pid = proc.pid
            self._emit("bg_task", task=task.to_dict())

            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                task.output_buf.append(text)
                if len(task.output_buf) > self._max_output_lines:
                    task.output_buf = task.output_buf[-self._max_output_lines:]
                self._emit("bg_task_output", id=task.id, session=task.session,
                           text=text, label=task.label)

            await proc.wait()
            task.exit_code = proc.returncode
            task.status = "done" if proc.returncode == 0 else "error"
        except Exception as exc:
            task.status = "error"
            task.output_buf.append(f"\n[ERROR] {exc}\n")
        finally:
            task.finished_at = time.time()
            task.pid = None
            self._emit("bg_task", task=task.to_dict())

    async def kill(self, tid: str) -> bool:
        """Kill a running task by id."""
        task = self._tasks.get(tid)
        if not task or task.status not in ("running", "queued"):
            return False
        if task.pid:
            try:
                os.killpg(os.getpgid(task.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        task.status = "killed"
        task.finished_at = time.time()
        task.pid = None
        self._emit("bg_task", task=task.to_dict())
        return True

    def list_tasks(self, session: Optional[str] = None) -> list[dict]:
        """Return all tasks, optionally filtered by session."""
        tasks = self._tasks.values()
        if session:
            tasks = [t for t in tasks if t.session == session]
        # Sort: running first, then by creation time desc
        order = {"running": 0, "queued": 1, "done": 2, "error": 2, "killed": 2}
        return [t.to_dict() for t in sorted(
            tasks, key=lambda t: (order.get(t.status, 2), -t.created_at))]

    def get_task(self, tid: str) -> Optional[dict]:
        task = self._tasks.get(tid)
        return task.to_dict() if task else None

    def prune(self, max_age_s: float = 3600.0):
        """Remove completed tasks older than max_age_s."""
        now = time.time()
        for tid, t in list(self._tasks.items()):
            if t.status in ("done", "error", "killed"):
                if t.finished_at and (now - t.finished_at) > max_age_s:
                    del self._tasks[tid]


# ── Singleton ───────────────────────────────────────────────────────────────────

_TASK_MGR: Optional[BgTaskManager] = None


def get_task_manager() -> BgTaskManager:
    global _TASK_MGR
    if _TASK_MGR is None:
        _TASK_MGR = BgTaskManager()
    return _TASK_MGR
