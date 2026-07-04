"""Local import path helpers for split sibling projects.

The bot can run directly from this checkout without installing sibling
projects in editable mode.  Compatibility shims call `ensure_split_projects`
before importing `phantom_console` or `phantom_llm`.
"""

from __future__ import annotations

import os
import sys


def ensure_split_projects() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    workspace = os.path.dirname(root)
    repo = os.path.dirname(workspace)
    candidates = [
        os.path.join(repo, "LLM_Frontend"),
        os.path.join(repo, "LLM_Backend"),
        os.path.join(repo, "modules", "phantom-console"),
        os.path.join(repo, "modules", "infiniproxy"),
        os.path.join(workspace, "phantom-console"),
        os.path.join(workspace, "phantom-llm"),
        os.path.join(workspace, "modules", "phantom-console"),
        os.path.join(workspace, "modules", "phantom-llm"),
    ]
    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
