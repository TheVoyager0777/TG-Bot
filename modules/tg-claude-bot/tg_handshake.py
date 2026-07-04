#!/usr/bin/env python3
"""tg_handshake.py — 外部 session 握手：在 TG 论坛创建专属话题，直接与 bot 对话。

用法:
  tg_handshake.py up <name>     创建外部 session（论坛话题 + worker），等待就绪
  tg_handshake.py ask <name> "<议题>"  向指定外部 session 的 worker 发消息并等回复
  tg_handshake.py send <name> "<消息>" 同上（别名）
  tg_handshake.py down <name>   关闭外部 session
  tg_handshake.py list          列出活跃的外部 session

原理:
  1. `up` → 写 JSON 到 ~/.claude/ext-handshake-inbox/<session_id>.json
  2. Bot watcher 读到 → spawn worker + 创建论坛话题 → 在话题里确认握手
  3. 握手信息写回 ~/.claude/ext-handshake-outbox/<session_id>.json
  4. CLI 轮询等待握手确认
  5. `ask` → 写任务到 ~/.claude/ext-session-inbox/<session_id>.json
  6. Bot worker 处理 → 回复写回 ~/.claude/ext-session-outbox/<session_id>_reply.json

示例:
  tg_handshake.py up audit-master
  tg_handshake.py ask audit-master "深度审计 build/BUILD.phantom 模块加载顺序"
  tg_handshake.py down audit-master
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
HS_INBOX  = HOME / ".claude" / "ext-handshake-inbox"
HS_OUTBOX = HOME / ".claude" / "ext-handshake-outbox"
SESSION_INBOX  = HOME / ".claude" / "ext-session-inbox"
SESSION_OUTBOX = HOME / ".claude" / "ext-session-outbox"
TIMEOUT = 120   # handshake timeout (bot may need to spawn worker + create topic)


def _ensure_dirs():
    for d in (HS_INBOX, SESSION_INBOX):
        d.mkdir(parents=True, exist_ok=True)
    HS_OUTBOX.mkdir(parents=True, exist_ok=True)
    SESSION_OUTBOX.mkdir(parents=True, exist_ok=True)


# ── handshake: up / down / list ────────────────────────────────────────────
def handshake_up(name: str, system_prompt: str = "") -> str | None:
    """创建外部 session，返回 session_id，失败返回 None。"""
    _ensure_dirs()
    sid = uuid.uuid4().hex[:12]
    req = {
        "session_id": sid,
        "name": name.strip(),
        "action": "up",
        "system_prompt": system_prompt.strip(),
        "timestamp": time.time(),
    }
    (HS_INBOX / f"{sid}.json").write_text(json.dumps(req, ensure_ascii=False, indent=2))
    print(f"📤 握手请求已发送 → bot (session: {name}, id: {sid})")

    # Wait for bot response
    deadline = time.time() + TIMEOUT
    out_file = HS_OUTBOX / f"{sid}.json"
    dots = 0
    while time.time() < deadline:
        if out_file.exists():
            try:
                data = json.loads(out_file.read_text())
                out_file.unlink()
                if data.get("status") == "ready":
                    tid = data.get("thread_id", "?")
                    print(f"\r✅ 握手成功 → 论坛话题 #{tid}, worker='{name}'")
                    print(f"   session_id: {sid}")
                    return sid
                else:
                    print(f"\r❌ 握手失败: {data.get('error', 'unknown')}", file=sys.stderr)
                    return None
            except (json.JSONDecodeError, OSError):
                time.sleep(1)
                continue
        dots = (dots + 1) % 30
        print(f"\r⏳ 等待 bot 创建 session{dots * '.'}{' ' * (30 - dots)}", end="", flush=True)
        time.sleep(2)
    print(f"\r⏰ 握手超时 ({TIMEOUT}s)", file=sys.stderr)
    return None


def handshake_down(name: str):
    """关闭外部 session。"""
    _ensure_dirs()
    sid = uuid.uuid4().hex[:12]
    req = {
        "session_id": sid,
        "name": name.strip(),
        "action": "down",
        "timestamp": time.time(),
    }
    (HS_INBOX / f"down-{sid}.json").write_text(json.dumps(req, ensure_ascii=False, indent=2))
    print(f"📤 关闭请求已发送 → bot (session: {name})")
    # non-blocking — bot will clean up


def handshake_list():
    """列出活跃的外部 session（从 outbox 的持久化状态读取）。"""
    state_file = HOME / ".claude" / "ext-sessions.json"
    if not state_file.exists():
        print("(无活跃外部 session)")
        return
    try:
        sessions = json.loads(state_file.read_text())
        if not sessions:
            print("(无活跃外部 session)")
            return
        print(f"{'NAME':<20} {'SESSION_ID':<14} {'WORKER':<20} {'THREAD':<10}")
        print("-" * 64)
        for name, info in sessions.items():
            print(f"{name:<20} {info.get('session_id','?'):<14} {info.get('worker','?'):<20} {info.get('thread_id','?'):<10}")
    except Exception as e:
        print(f"读取失败: {e}", file=sys.stderr)


# ── session interaction: ask / send ────────────────────────────────────────
def session_ask(name: str, prompt: str, timeout: int = 600) -> bool:
    """向外部 session 发消息并等待回复。"""
    _ensure_dirs()
    tid = uuid.uuid4().hex[:12]
    task = {
        "task_id": tid,
        "name": name.strip(),
        "prompt": prompt.strip(),
        "timestamp": time.time(),
    }
    (SESSION_INBOX / f"{tid}.json").write_text(json.dumps(task, ensure_ascii=False, indent=2))
    print(f"📤 消息已发送 → {name} (task_id: {tid})")
    print(f"   消息: {prompt[:100]}...")

    out_file = SESSION_OUTBOX / f"{tid}_reply.json"
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if out_file.exists():
            try:
                data = json.loads(out_file.read_text())
                out_file.unlink()
                print("\r" + " " * 60)
                print("─" * 60)
                print(data.get("reply", data.get("error", "(empty)")))
                print("─" * 60)
                return data.get("status") == "ok"
            except (json.JSONDecodeError, OSError):
                time.sleep(1)
                continue
        dots = (dots + 1) % 30
        print(f"\r⏳ 等待 {name} 回复{dots * '.'}{' ' * (30 - dots)}", end="", flush=True)
        time.sleep(3)
    print(f"\r⏰ 等待超时 ({timeout}s) — 回复可能仍在处理中", file=sys.stderr)
    return False


# ── CLI ────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        handshake_list()

    elif cmd == "up":
        name = sys.argv[2] if len(sys.argv) > 2 else "ext-session"
        sp = sys.argv[3] if len(sys.argv) > 3 else ""
        handshake_up(name, sp)

    elif cmd == "down":
        name = sys.argv[2] if len(sys.argv) > 2 else ""
        if not name:
            print("用法: tg_handshake.py down <name>", file=sys.stderr)
            sys.exit(1)
        handshake_down(name)

    elif cmd in ("ask", "send"):
        if len(sys.argv) < 4:
            print(f"用法: tg_handshake.py {cmd} <name> \"<消息>\"", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[2]
        prompt = " ".join(sys.argv[3:])
        session_ask(name, prompt)

    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
