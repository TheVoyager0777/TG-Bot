"""Codex CLI backend adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .base import (
    BackendError,
    BackendRequest,
    BackendResult,
    EventCallback,
    TextCallback,
    iter_stdout_lines,
)


NAME = "codex"
_HELP_CACHE: dict[str, str] = {}


def capabilities() -> dict:
    return {
        "stream": "stdout text chunks or parsed JSONL text when codex_json=true",
        "interrupt": True,
        "resume_session": "existing session_id via codex exec resume; new session id is only returned if JSONL exposes session_id",
        "tools": "Codex CLI built-in tools and MCP config; no per-request tool allowlist in adapter",
        "permission_mode": "codex_sandbox/codex_approval for new exec runs; resume subcommand does not expose those flags",
        "model": True,
        "cwd": "new exec uses --cd and process cwd; resume uses process cwd only",
        "attachments": "CLI supports --image, adapter has no attachment request field",
    }


class CodexCliBackend:
    """Run one prompt through `codex exec`.

    This backend is intentionally process-scoped: each request runs one
    non-interactive Codex CLI invocation. When a session id is supplied, the
    adapter uses the local CLI's non-interactive resume subcommand.
    """

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self.bin = str(self.config.get("codex_bin") or "codex")
        self._proc: asyncio.subprocess.Process | None = None

    def _resolve_bin(self) -> str | None:
        if os.path.isabs(self.bin) or os.sep in self.bin:
            return self.bin if os.path.exists(self.bin) and os.access(self.bin, os.X_OK) else None
        return shutil.which(self.bin)

    @staticmethod
    def _help_for(exe: str, *args: str) -> str:
        key = "\0".join((exe, *args))
        cached = _HELP_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            proc = subprocess.run(
                [exe, *args, "-h"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            )
            out = proc.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        _HELP_CACHE[key] = out
        return out

    def _supports(self, exe: str, flag: str, *, resume: bool = False) -> bool:
        args = ("exec", "resume") if resume else ("exec",)
        return flag in self._help_for(exe, *args)

    def available(self) -> bool:
        exe = self._resolve_bin()
        if not exe:
            return False
        help_text = self._help_for(exe, "exec")
        return "Usage: codex exec" in help_text and "Run Codex non-interactively" in help_text

    def info(self) -> dict:
        exe = self._resolve_bin()
        return {
            "name": NAME,
            "mode": "cli-exec",
            "bin": self.bin,
            "resolved_bin": exe,
            "available": self.available(),
            "description": "Codex CLI non-interactive backend via codex exec",
            "capabilities": capabilities(),
        }

    @staticmethod
    def _toml_str(value: str) -> str:
        return json.dumps(str(value))

    @staticmethod
    def _codex_base_url(raw: str) -> str:
        raw = str(raw or "").strip()
        if not raw:
            return ""
        parsed = urlsplit(raw)
        path = parsed.path.rstrip("/")
        for suffix in ("/messages", "/chat/completions", "/responses"):
            if path.endswith(suffix):
                path = path[: -len(suffix)].rstrip("/")
        if not path.endswith("/v1"):
            path = f"{path}/v1" if path else "/v1"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @staticmethod
    def _env_first(env: dict[str, str], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = str(env.get(key) or "").strip()
            if value:
                return value
        return ""

    def _apply_provider_overrides(self, cmd: list[str], req: BackendRequest, env: dict[str, str]) -> None:
        base_url = self._env_first(env, ("CODEX_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"))
        token = self._env_first(env, ("CODEX_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"))
        provider_label = str(env.get("PHANTOM_PROVIDER") or env.get("ANTHROPIC_PROVIDER") or "phantom").strip() or "phantom"
        provider_id = "phantom"
        if base_url:
            cmd += [
                "--config", f"model_provider={self._toml_str(provider_id)}",
                "--config", f"model_providers.{provider_id}.name={self._toml_str(provider_label)}",
                "--config", f"model_providers.{provider_id}.base_url={self._toml_str(self._codex_base_url(base_url))}",
                "--config", f"model_providers.{provider_id}.wire_api={self._toml_str(str(self.config.get('codex_wire_api') or 'responses'))}",
                "--config", f"model_providers.{provider_id}.requires_openai_auth=true",
            ]
            if token:
                env_key = "PHANTOM_CODEX_AUTH_TOKEN"
                env[env_key] = token
                cmd += ["--config", f"model_providers.{provider_id}.env_key={self._toml_str(env_key)}"]

        if not (req.model or self.config.get("codex_model")):
            model = self._env_first(env, (
                "CODEX_MODEL",
                "OPENAI_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            ))
            if model:
                cmd += ["--model", model]

        effort = str(
            self.config.get("codex_reasoning_effort")
            or self.config.get("model_reasoning_effort")
            or env.get("CODEX_REASONING_EFFORT")
            or ""
        ).strip()
        if effort:
            cmd += ["--config", f"model_reasoning_effort={self._toml_str(effort)}"]

    def _command(self, req: BackendRequest, env: dict[str, str]) -> list[str]:
        exe = self._resolve_bin()
        if not exe:
            raise BackendError(f"codex CLI not found: {self.bin}")
        if req.session_id:
            cmd = [exe, "exec", "resume", "--skip-git-repo-check"]
        else:
            cmd = [exe, "exec", "--color", "never", "--skip-git-repo-check"]
            if req.cwd:
                cmd += ["--cd", req.cwd]
        model = req.model or self.config.get("codex_model")
        if model:
            cmd += ["--model", str(model)]
        if not req.session_id:
            approval = str(self.config.get("codex_approval") or "").strip()
            bypass = approval == "never" and self._supports(exe, "--dangerously-bypass-approvals-and-sandbox")
            sandbox = self.config.get("codex_sandbox")
            if sandbox and not bypass:
                cmd += ["--sandbox", str(sandbox)]
            if approval and self._supports(exe, "--ask-for-approval"):
                cmd += ["--ask-for-approval", str(approval)]
            elif bypass:
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            profile = self.config.get("codex_profile")
            if profile:
                cmd += ["--profile", str(profile)]
        elif str(self.config.get("codex_approval") or "").strip() == "never" and self._supports(exe, "--dangerously-bypass-approvals-and-sandbox", resume=True):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        self._apply_provider_overrides(cmd, req, env)
        if self.config.get("codex_json"):
            cmd.append("--json")
        extra_key = "codex_resume_extra_args" if req.session_id else "codex_extra_args"
        extra_args = self.config.get(extra_key) or []
        if isinstance(extra_args, str):
            extra_args = extra_args.split()
        cmd += [str(x) for x in extra_args]
        if req.session_id:
            cmd.append(str(req.session_id))
        cmd.append("-")
        return cmd

    async def run(
        self,
        req: BackendRequest,
        on_text: TextCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> BackendResult:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in (req.env or {}).items()})
        cmd = self._command(req, env)
        cwd = req.cwd or None
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
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
            raw_events: list[Any] = []
            use_json = bool(self.config.get("codex_json"))
            async for line in iter_stdout_lines(proc.stdout):
                text = line.decode("utf-8", errors="replace")
                if use_json:
                    parsed_text = self._extract_json_text(text, raw_events)
                    if parsed_text:
                        chunks.append(parsed_text)
                        if on_text:
                            await on_text(parsed_text)
                    continue
                chunks.append(text)
                if on_text:
                    await on_text(text)
            rc = await proc.wait()
            out = "".join(chunks).strip()
            if rc != 0:
                raise BackendError(out or f"codex exec failed rc={rc}")
            session_id = req.session_id or self._extract_session_id(raw_events)
            return BackendResult(
                text=out,
                session_id=session_id,
                raw={"events": raw_events} if raw_events else {},
            )
        finally:
            self._proc = None

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

    @staticmethod
    def _extract_json_text(line: str, raw_events: list[Any]) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        raw_events.append(event)
        if isinstance(event, dict):
            for key in ("text", "message", "output", "delta"):
                value = event.get(key)
                if isinstance(value, str):
                    return value
            item = event.get("item")
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    return value
        return ""

    @staticmethod
    def _extract_session_id(raw_events: list[Any]) -> str | None:
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            value = event.get("session_id")
            if isinstance(value, str) and value:
                return value
            item = event.get("item")
            if isinstance(item, dict):
                value = item.get("session_id")
                if isinstance(value, str) and value:
                    return value
        return None
