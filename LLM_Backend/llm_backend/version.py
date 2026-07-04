"""Version metadata for LLM_Backend."""

from __future__ import annotations

VERSION = "0.3.0"
DESCRIPTION = "Phantom LLM CLI backend adapters for Claude Code and Codex"


def as_dict() -> dict:
    return {
        "name": "LLM_Backend",
        "version": VERSION,
        "description": DESCRIPTION,
        "backends": ["claude-code", "codex"],
    }


def line() -> str:
    return f"LLM_Backend {VERSION} - {DESCRIPTION}"
