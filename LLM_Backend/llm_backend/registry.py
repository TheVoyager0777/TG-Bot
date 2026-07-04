"""Backend discovery and factory helpers."""

from __future__ import annotations

from . import claude_code
from .claude_code import ClaudeCodeCliBackend
from .codex_cli import CodexCliBackend


SDK_BACKEND_ALIASES = set(claude_code.ALIASES)
ALL_BACKENDS = ("claude-code", "codex")


def normalize(name: str | None) -> str:
    raw = (name or "claude-code").strip().lower().replace("_", "-")
    if raw in SDK_BACKEND_ALIASES:
        return "claude-code"
    if raw == "codex-cli":
        return "codex"
    if raw == "codex":
        return "codex"
    return raw


def is_sdk_backend(name: str | None) -> bool:
    """Claude Code backend runs through SDK when interactive callbacks are needed."""
    return normalize(name) == "claude-code"


def make_backend(name: str | None, config: dict | None = None):
    normalized = normalize(name)
    if normalized == "codex":
        return CodexCliBackend(config)
    if normalized == "claude-code":
        return ClaudeCodeCliBackend(config)
    raise ValueError(f"unknown LLM backend: {name}")


def backend_info(active: str | None, config: dict | None = None) -> dict:
    config = config or {}
    active_name = normalize(active)
    codex = CodexCliBackend(config).info()
    claude = claude_code.info(config)
    return {
        "active": active_name,
        "capability_keys": [
            "stream",
            "interrupt",
            "resume_session",
            "tools",
            "permission_mode",
            "model",
            "cwd",
            "attachments",
        ],
        "available": {
            "claude-code": claude,
            "codex": codex,
        },
    }
