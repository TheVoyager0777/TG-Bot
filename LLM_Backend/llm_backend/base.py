"""Shared types for CLI backend adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable


TextCallback = Callable[[str], Awaitable[None]]
EventCallback = Callable[[dict], Awaitable[None]]


@dataclass
class BackendRequest:
    prompt: str
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    permission_mode: str | None = None
    session_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class BackendResult:
    text: str
    session_id: str | None = None
    raw: dict = field(default_factory=dict)


class BackendError(RuntimeError):
    """Raised when a configured backend cannot complete a request."""


async def iter_stdout_lines(
    stream: asyncio.StreamReader,
    *,
    chunk_size: int = 262144,
) -> AsyncIterator[bytes]:
    """Yield newline-delimited stdout records without StreamReader.readline limits."""
    pending = bytearray()
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            if pending:
                yield bytes(pending)
            break
        pending.extend(chunk)
        while True:
            pos = pending.find(b"\n")
            if pos < 0:
                break
            line = bytes(pending[:pos + 1])
            del pending[:pos + 1]
            yield line
