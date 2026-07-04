#!/usr/bin/env python3
"""tg_magi.py — CLI 工具: 向 TG bot 的 MAGI 三脑表决系统发送议题并获取报告。

用法:
  tg_magi.py audit "<议题描述>"    发送议题到 MAGI 三脑审计，等待报告
  tg_magi.py ask "<问题>"          同上

原理:
  写 JSON 任务文件到 ~/.claude/magi-inbox/，bot 的异步 watcher 读到后路由到
  MAGI worker（三脑并行表决），结果写回 ~/.claude/magi-outbox/<task_id>.json，
  本工具轮询等待结果文件并打印。

示例:
  tg_magi.py audit "审计 build/package_anykernel.sh 的模块打包逻辑是否正确"
  tg_magi.py ask "检查 phantom_common.ko 是否应先于 phantom_clock.ko 加载"
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

INBOX = Path.home() / ".claude" / "magi-inbox"
OUTBOX = Path.home() / ".claude" / "magi-outbox"
TIMEOUT = 300  # 5 min


def submit_task(topic: str) -> str:
    tid = uuid.uuid4().hex[:12]
    task = {
        "task_id": tid,
        "topic": topic.strip(),
        "timestamp": time.time(),
    }
    INBOX.mkdir(parents=True, exist_ok=True)
    task_file = INBOX / f"{tid}.json"
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2))
    print(f"📤 议题已提交 → MAGI (task_id: {tid})")
    print(f"   议题: {topic[:80]}...")
    return tid


def wait_result(tid: str, timeout: int = TIMEOUT) -> dict | None:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    result_file = OUTBOX / f"{tid}.json"
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
                # Cleanup
                result_file.unlink()
                return data
            except (json.JSONDecodeError, OSError):
                time.sleep(1)
                continue
        dots = (dots + 1) % 40
        print(f"\r⏳ 等待 MAGI 三脑表决{dots * '.'}{' ' * (40 - dots)}", end="", flush=True)
        time.sleep(2)
    print("\r⏰ 超时 — MAGI 未在 {timeout}s 内返回结果", file=sys.stderr)
    return None


def print_report(data: dict):
    report = data.get("report", data.get("result", str(data)))
    print("\r" + " " * 60)  # clear dots line
    print("─" * 60)
    print(report)
    print("─" * 60)
    if data.get("verdict"):
        print(f"最终裁决: {data['verdict']}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    topic = " ".join(sys.argv[2:])

    if cmd in ("audit", "ask"):
        tid = submit_task(topic)
        result = wait_result(tid)
        if result:
            print_report(result)
        else:
            print(f"\n可稍后查看: {OUTBOX / tid}.json", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
