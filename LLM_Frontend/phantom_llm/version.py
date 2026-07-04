"""Version metadata for phantom-llm."""

from __future__ import annotations

import hashlib
import os
import time

__version__ = "0.4.0"
DESCRIPTION = "Phantom LLM_Frontend service"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _scan_sources() -> tuple[float, int]:
    max_mt = 0.0
    count = 0
    for root, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            count += 1
            try:
                max_mt = max(max_mt, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                pass
    return max_mt, count


_BUILD_MT, _N_FILES = _scan_sources()
BUILD_TS = _BUILD_MT or time.time()


def fingerprint() -> str:
    raw = f"{__version__}|{BUILD_TS:.0f}|{_N_FILES}".encode()
    return hashlib.sha1(raw).hexdigest()[:7]


def snapshot() -> dict:
    return {
        "name": "LLM_Frontend",
        "version": __version__,
        "description": DESCRIPTION,
        "build_ts": int(BUILD_TS),
        "build_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BUILD_TS)),
        "fingerprint": fingerprint(),
        "source_files": _N_FILES,
    }


def line() -> str:
    s = snapshot()
    return f"{s['name']} v{s['version']} · {s['description']} · {s['build_time']} · {s['fingerprint']}"
