"""version —— bot 版本与构建信息。

设计要点：
- __version__：手工 bump 的语义版本号（重大改动时改这个）。
- BUILD_TS：自动取代码目录里所有 .py 的最大 mtime，作为"构建时间戳"。
  改了任意一个 .py 重启后，这个戳就会推进——天然反映"代码是否换了一波"。
- START_TS：进程启动瞬间冻结，可以减出"已运行 X 秒"。
- FINGERPRINT：BUILD_TS+文件数的短哈希，肉眼比对一眼能看出是不是同一份代码。
"""
from __future__ import annotations

import hashlib
import os
import time

__version__ = "1.10.1"
DESCRIPTION = "Telegram control bot for Phantom agent services"

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（mitigate move to infra/）


def _scan_sources() -> tuple[float, int]:
    """扫本目录所有 .py 的最大 mtime + 总文件数。__pycache__ 排除掉。"""
    max_mt = 0.0
    n = 0
    for fn in os.listdir(_HERE):
        if not fn.endswith(".py"):
            continue
        try:
            mt = os.path.getmtime(os.path.join(_HERE, fn))
        except OSError:
            continue
        if mt > max_mt:
            max_mt = mt
        n += 1
    return max_mt, n


_BUILD_MT, _N_FILES = _scan_sources()
BUILD_TS = _BUILD_MT or time.time()
START_TS = time.time()


def _fp() -> str:
    raw = f"{BUILD_TS:.0f}|{_N_FILES}".encode()
    return hashlib.sha1(raw).hexdigest()[:7]


FINGERPRINT = _fp()


def build_str() -> str:
    """像 'v1.4.0 · 2026-06-11 14:23 · 7a3b9c1' 这样的紧凑标识。"""
    bt = time.strftime("%Y-%m-%d %H:%M", time.localtime(BUILD_TS))
    return f"v{__version__} · {bt} · {FINGERPRINT}"


def snapshot() -> dict:
    return {
        "name": "tg-claude-bot",
        "version": __version__,
        "description": DESCRIPTION,
        "build_ts": int(BUILD_TS),
        "build_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BUILD_TS)),
        "fingerprint": FINGERPRINT,
        "source_files": _N_FILES,
    }


def uptime_str() -> str:
    s = int(time.time() - START_TS)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s%60}s"
    if s < 86400:
        return f"{s//3600}h{(s%3600)//60}m"
    return f"{s//86400}d{(s%86400)//3600}h"


def info_block() -> str:
    """多行版本/构建/运行信息块，给 /version 用。"""
    return (
        f"🤖 *tg-claude-bot*\n"
        f"version: `{__version__}`\n"
        f"desc:    `{DESCRIPTION}`\n"
        f"build:   `{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(BUILD_TS))}`\n"
        f"fp:      `{FINGERPRINT}`  ({_N_FILES} .py files)\n"
        f"started: `{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(START_TS))}`\n"
        f"uptime:  `{uptime_str()}`")


if __name__ == "__main__":
    print(build_str())
    print()
    print(info_block())
