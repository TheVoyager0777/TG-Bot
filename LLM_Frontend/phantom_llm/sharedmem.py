"""
sharedmem.py — agent team 的共享记忆/协调台账（SQLite）。

目的：多个 worker 并行干活时，记录"谁在什么时候改了哪个文件"，让别的 worker
（或主对话）能快速查询某处是否被动过，避免并行踩同一文件。

- 事件驱动稀疏索引：只记 worker 实际改过的路径，不全量扫目录（工作树 50w+ 文件）。
- 由 SDK 的 PostToolUse hook 自动写入（Write/Edit/NotebookEdit 成功后）。
- PreToolUse hook 查询：worker 要改某文件前，看近期是否别人动过 → 预警。
- SQLite WAL 模式 + 每次操作短连接，适配多 worker 并发写（同进程异步）。

表 file_edits：每次文件改动一条
  id, worker, path, op(write/edit/notebook), ts(epoch), turn, detail
索引视图 latest：每个 path 的最近一次改动（查询"谁最后动的"）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass


@dataclass
class EditRecord:
    worker: str
    path: str
    op: str
    ts: float
    turn: int
    detail: str


class SharedMemory:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = os.path.realpath(repo_root)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS file_edits (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker  TEXT NOT NULL,
                    path    TEXT NOT NULL,
                    op      TEXT NOT NULL,
                    ts      REAL NOT NULL,
                    turn    INTEGER DEFAULT 0,
                    detail  TEXT DEFAULT ''
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_path ON file_edits(path)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_worker ON file_edits(worker)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON file_edits(ts)")

    def _norm(self, path: str) -> str:
        """归一化为相对 repo_root 的路径，便于跨 worker 比对。"""
        if not path:
            return path
        ap = os.path.realpath(path) if os.path.isabs(path) else os.path.realpath(
            os.path.join(self.repo_root, path))
        try:
            return os.path.relpath(ap, self.repo_root)
        except ValueError:
            return ap  # 不在 repo 下，存绝对路径

    # ---- 写入（PostToolUse hook 调用）----
    def record(self, worker: str, path: str, op: str, turn: int = 0, detail: str = ""):
        rel = self._norm(path)
        with self._conn() as c:
            c.execute(
                "INSERT INTO file_edits(worker,path,op,ts,turn,detail) VALUES(?,?,?,?,?,?)",
                (worker, rel, op, time.time(), turn, detail[:200]))

    # ---- 查询 ----
    def who_touched(self, path: str, exclude_worker: str | None = None,
                    within_s: float | None = None) -> list[EditRecord]:
        """某文件的改动历史（最近在前）。exclude_worker 排除自己。"""
        rel = self._norm(path)
        q = "SELECT * FROM file_edits WHERE path=?"
        args: list = [rel]
        if exclude_worker:
            q += " AND worker!=?"
            args.append(exclude_worker)
        if within_s:
            q += " AND ts>=?"
            args.append(time.time() - within_s)
        q += " ORDER BY ts DESC"
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def recent(self, limit: int = 30, worker: str | None = None) -> list[EditRecord]:
        q = "SELECT * FROM file_edits"
        args: list = []
        if worker:
            q += " WHERE worker=?"
            args.append(worker)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def search(self, path_prefix: str, limit: int = 50) -> list[EditRecord]:
        """按路径前缀查（如 'vendor/' 看 vendor 树所有改动）。"""
        rel = self._norm(path_prefix)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM file_edits WHERE path LIKE ? ORDER BY ts DESC LIMIT ?",
                (rel + "%", limit)).fetchall()
            return [self._row(r) for r in rows]

    def summary(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM file_edits").fetchone()[0]
            files = c.execute("SELECT COUNT(DISTINCT path) FROM file_edits").fetchone()[0]
            by_worker = c.execute(
                "SELECT worker, COUNT(*) n FROM file_edits GROUP BY worker ORDER BY n DESC"
            ).fetchall()
        return {"total_edits": total, "distinct_files": files,
                "by_worker": [(r["worker"], r["n"]) for r in by_worker]}

    @staticmethod
    def _row(r: sqlite3.Row) -> EditRecord:
        return EditRecord(worker=r["worker"], path=r["path"], op=r["op"],
                          ts=r["ts"], turn=r["turn"], detail=r["detail"])


# 写文件类工具 → 操作名映射
_WRITE_TOOLS = {"Write": "write", "Edit": "edit", "MultiEdit": "edit",
                "NotebookEdit": "notebook"}


def _extract_path(tool_input: dict) -> str | None:
    for k in ("file_path", "path", "notebook_path", "filePath"):
        v = tool_input.get(k)
        if v:
            return v
    return None


def _fmt_age(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)}秒前"
    if sec < 3600:
        return f"{int(sec/60)}分钟前"
    if sec < 86400:
        return f"{int(sec/3600)}小时前"
    return f"{int(sec/86400)}天前"


def build_hooks(mem: "SharedMemory", worker_name: str, warn_within_s: float = 1800,
                turn_getter=None):
    """为某 worker 造 PreToolUse(预警) + PostToolUse(记录) hook。

    turn_getter: 可选 callable，返回当前 turn 号，记进台账。
    warn_within_s: 预警时间窗（默认 30 分钟内别人改过才警告）。
    返回 {"PreToolUse":[HookMatcher...], "PostToolUse":[...]}。
    """
    from claude_agent_sdk import HookMatcher
    import time as _t

    async def pre_hook(inp, tool_use_id, ctx):
        if inp.get("tool_name") not in _WRITE_TOOLS:
            return {}
        path = _extract_path(inp.get("tool_input", {}) or {})
        if not path:
            return {}
        # 别人近期改过这文件吗？
        others = mem.who_touched(path, exclude_worker=worker_name, within_s=warn_within_s)
        if not others:
            return {}
        now = _t.time()
        latest = others[0]
        msg = (f"⚠️ 共享记忆预警：文件 {mem._norm(path)} 最近被其他 worker 改过——"
               f"'{latest.worker}' 于 {_fmt_age(now - latest.ts)}（{latest.op}）"
               f"。共 {len(others)} 次他人改动。改前请确认不会覆盖其工作。")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": msg}}

    async def post_hook(inp, tool_use_id, ctx):
        tool = inp.get("tool_name")
        if tool not in _WRITE_TOOLS:
            return {}
        path = _extract_path(inp.get("tool_input", {}) or {})
        if not path:
            return {}
        # tool_response 有错误就不记（失败的改动不算数）
        resp = inp.get("tool_response")
        if isinstance(resp, dict) and resp.get("is_error"):
            return {}
        turn = 0
        if turn_getter:
            try:
                turn = int(turn_getter())
            except Exception:
                turn = 0
        try:
            mem.record(worker_name, path, _WRITE_TOOLS[tool], turn=turn)
        except Exception:
            pass  # 台账写失败不能影响 worker 干活
        return {}

    return {
        "PreToolUse": [HookMatcher(hooks=[pre_hook])],
        "PostToolUse": [HookMatcher(hooks=[post_hook])],
    }

