"""Claude Code CLI backend adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from typing import Any

from .base import (
    BackendError,
    BackendRequest,
    BackendResult,
    EventCallback,
    TextCallback,
    iter_stdout_lines,
)


NAME = "claude-code"
ALIASES = {"claude", "claude-code", "claude_code", "claude-cli", "claude-code-cli"}


def is_session_in_use_error(text: str) -> bool:
    """Claude CLI refuses concurrent use of the same session id."""
    return "Session ID" in text and "already in use" in text


def available(bin_name: str = "claude") -> bool:
    return shutil.which(bin_name) is not None


def _config_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _session_persistence_enabled(config: dict | None) -> bool:
    config = config or {}
    for key in (
        "persist_session",
        "resume_session",
        "cli_persist_session",
        "claude_persist_session",
    ):
        if key in config:
            return _config_truthy(config.get(key))
    return False


def capabilities() -> dict:
    return {
        "stream": "stream-json partial text/thinking chunks by default",
        "interrupt": True,
        "resume_session": "existing sessions via claude --resume; new sessions via --session-id",
        "tools": "Claude Code CLI tools; configurable with claude_extra_args",
        "permission_mode": "request/config via --permission-mode",
        "model": True,
        "cwd": True,
        "attachments": "CLI supports file resources, adapter has no attachment request field",
    }


class ClaudeCodeCliBackend:
    """Run one prompt through `claude --print`."""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self.bin = str(self.config.get("claude_bin") or "claude")
        self._proc: asyncio.subprocess.Process | None = None

    def available(self) -> bool:
        return available(self.bin)

    def info(self) -> dict:
        return {
            "name": NAME,
            "aliases": sorted(ALIASES),
            "mode": "cli-print",
            "bin": self.bin,
            "available": self.available(),
            "description": "Claude Code CLI non-interactive backend via claude --print",
            "capabilities": capabilities(),
        }

    def _command(self, req: BackendRequest) -> list[str]:
        exe = shutil.which(self.bin)
        if not exe:
            raise BackendError(f"Claude Code CLI not found: {self.bin}")
        cmd = [
            exe,
            "--print",
            "--input-format",
            "text",
        ]
        output_format = str(self.config.get("claude_output_format") or "stream-json")
        cmd += ["--output-format", output_format]
        if output_format == "stream-json":
            cmd += ["--verbose", "--include-partial-messages"]
        settings_file = self.config.get("claude_settings_file")
        if settings_file:
            cmd += ["--settings", str(settings_file)]
        setting_sources = self.config.get("claude_setting_sources")
        if setting_sources is not None:
            if isinstance(setting_sources, (list, tuple)):
                setting_sources = ",".join(str(x) for x in setting_sources)
            cmd += ["--setting-sources", str(setting_sources)]
        persist_session = _session_persistence_enabled(self.config)
        if not persist_session and self.config.get("claude_no_session_persistence", True):
            cmd.append("--no-session-persistence")
        resume_existing = bool((req.metadata or {}).get("resume_existing_session"))
        if req.session_id and resume_existing:
            cmd += ["--resume", req.session_id]
        elif req.session_id:
            cmd += ["--session-id", req.session_id]
        elif persist_session:
            sid = req.metadata.get("new_session_id") if req.metadata else None
            if sid:
                cmd += ["--session-id", str(sid)]
        if req.model:
            cmd += ["--model", str(req.model)]
        mode = req.permission_mode or self.config.get("claude_permission_mode")
        if mode:
            cmd += ["--permission-mode", str(mode)]
        if self.config.get("claude_bare"):
            cmd.append("--bare")
        for path in self.config.get("claude_add_dirs") or []:
            cmd += ["--add-dir", str(path)]
        extra_args = self.config.get("claude_extra_args") or []
        if isinstance(extra_args, str):
            extra_args = extra_args.split()
        cmd += [str(x) for x in extra_args]
        cmd.append("-")
        return cmd

    async def run(
        self,
        req: BackendRequest,
        on_text: TextCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> BackendResult:
        persist_session = _session_persistence_enabled(self.config)
        if persist_session and not req.session_id:
            req.session_id = str(uuid.uuid4())
            req.metadata["new_session_id"] = req.session_id
        cmd = self._command(req)
        env = os.environ.copy()
        env.update({k: str(v) for k, v in (req.env or {}).items()})
        claude_home = self.config.get("claude_home") or self.config.get("claude_home_dir")
        if claude_home:
            os.makedirs(str(claude_home), exist_ok=True)
            env["HOME"] = str(claude_home)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=req.cwd or None,
            env=env,
        )
        self._proc = proc
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            proc.stdin.write(req.prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            chunks: list[str] = []
            raw: list[Any] = []
            parse_state: dict[str, Any] = {}
            use_stream_json = str(self.config.get("claude_output_format") or "stream-json") == "stream-json"
            async for line in iter_stdout_lines(proc.stdout):
                text = line.decode("utf-8", errors="replace")
                if use_stream_json:
                    parsed_text, parsed_event = self._handle_stream_json_line(text, raw, parse_state)
                    if parsed_text:
                        chunks.append(parsed_text)
                        if on_text and not is_session_in_use_error(parsed_text):
                            await on_text(parsed_text)
                        continue
                    if parsed_event and on_event:
                        await on_event(parsed_event)
                    continue
                chunks.append(text)
                if on_text and not is_session_in_use_error(text):
                    await on_text(text)
            rc = await proc.wait()
            out = "".join(chunks).strip()
            if rc != 0:
                if not out and raw:
                    out = "\n".join(json.dumps(ev, ensure_ascii=False) for ev in raw[-5:])
                raise BackendError(out or f"claude --print failed rc={rc}")
            return BackendResult(text=out, session_id=req.session_id if persist_session else None,
                                 raw={"events": raw} if raw else {})
        finally:
            self._proc = None

    @staticmethod
    def _handle_stream_json_line(
        line: str,
        raw: list[Any],
        state: dict[str, Any] | None = None,
    ) -> tuple[str, dict | None]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line, None
        raw.append(event)
        state = state if state is not None else {}
        etype = event.get("type")

        def parent_from(*items: Any) -> str | None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                parent = item.get("parent_tool_use_id") or item.get("parent")
                if parent:
                    return str(parent)
            return None

        def text_key(parent: str | None, index: Any = None) -> str:
            # Assistant snapshot content can omit non-text blocks, so its text
            # indexes do not necessarily match stream_event indexes. Dedupe text
            # by conversation scope instead of block index.
            if parent:
                return f"sub/{parent}"
            return "main"

        def record_text_delta(parent: str | None, index: Any, text: str) -> None:
            if not text:
                return
            seen = state.setdefault("text_by_block", {})
            key = text_key(parent, index)
            seen[key] = str(seen.get(key) or "") + text

        def snapshot_suffix(parent: str | None, index: Any, text: str) -> str:
            if not text:
                return ""
            seen = state.setdefault("text_by_block", {})
            key = text_key(parent, index)
            prev = str(seen.get(key) or "")
            if text.startswith(prev):
                suffix = text[len(prev):]
            elif prev and prev.endswith(text):
                suffix = ""
            else:
                suffix = text
            seen[key] = text
            return suffix

        parent = parent_from(event)
        if etype == "stream_event":
            inner = event.get("event") or {}
            parent = parent_from(event, inner)
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                parent = parent_from(event, inner, delta)
                index = inner.get("index", 0)
                if delta.get("type") == "text_delta":
                    text = str(delta.get("text") or "")
                    record_text_delta(parent, index, text)
                    if parent and text:
                        return "", {"type": "subagent_text", "parent": parent, "text": text}
                    return text, None
                if delta.get("type") == "thinking_delta":
                    text = str(delta.get("thinking") or "")
                    return "", ({
                        "type": "thinking",
                        "text": text,
                        "block_id": inner.get("index", 0),
                        "parent": parent,
                    } if text else None)
            if inner.get("type") == "content_block_start":
                block = inner.get("content_block") or {}
                parent = parent_from(event, inner, block)
                if block.get("type") == "tool_use":
                    return "", {
                        "type": "tool",
                        "id": block.get("id") or f"tool-{inner.get('index', 0)}",
                        "tool": block.get("name") or "tool",
                        "input": block.get("input") or {},
                        "parent": parent,
                    }
            return "", None
        if etype == "assistant":
            msg = event.get("message") or {}
            parent = parent_from(event, msg)
            if isinstance(msg, dict):
                top_parts: list[str] = []
                sub_parts: dict[str, list[str]] = {}
                for index, block in enumerate(msg.get("content") or []):
                    if not (isinstance(block, dict) and block.get("type") == "text" and block.get("text")):
                        continue
                    block_parent = parent_from(event, msg, block) or parent
                    suffix = snapshot_suffix(block_parent, index, str(block.get("text") or ""))
                    if not suffix:
                        continue
                    if block_parent:
                        sub_parts.setdefault(block_parent, []).append(suffix)
                    else:
                        top_parts.append(suffix)
                if top_parts:
                    return "".join(top_parts), None
                if sub_parts:
                    sub_parent, parts = next(iter(sub_parts.items()))
                    return "", {"type": "subagent_text", "parent": sub_parent, "text": "".join(parts)}
            return "", None
        if etype == "error" or event.get("is_error"):
            msg = event.get("message") or event.get("error") or event.get("result") or "Claude Code CLI error"
            return str(msg), None
        if etype == "result":
            if event.get("is_error"):
                msg = event.get("result") or "; ".join(map(str, event.get("errors") or [])) or "Claude Code CLI error"
                return str(msg), None
            return "", {
                "type": "result",
                "stats": f"tokens={((event.get('usage') or {}).get('input_tokens') or 0)}→{((event.get('usage') or {}).get('output_tokens') or 0)} cost=${float(event.get('total_cost_usd') or 0):.4f}",
            }
        return "", None

    async def interrupt(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return True


def info(config: dict | None = None) -> dict:
    return ClaudeCodeCliBackend(config).info()
