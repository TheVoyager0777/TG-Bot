# Module Architecture

This project is organized as a control plane made of small services and adapters. The goal is to keep user-facing command surfaces, runtime services, LLM orchestration, backend adapters, and API proxying separate enough that each can evolve without dragging the others with it.

## Organization Principles

- Keep one owner per capability. A module should own a clear runtime surface or library contract.
- Prefer local package imports over legacy shim paths in new code.
- Keep Telegram rendering, Web rendering, LLM orchestration, and CLI execution separate.
- Keep runtime state and secrets out of the source tree.
- Use `manifest.json` when automation needs to discover modules, ports, entrypoints, or service ownership.

## Domains

| Domain | Modules | Responsibility |
| --- | --- | --- |
| `control-entry` | `modules/tg-claude-bot` | Owner-facing Telegram commands, approval UI, and service orchestration. |
| `runtime-services` | `modules/phantom-console`, `modules/phantom-network` | Local APIs for console rendering, event streams, task state, contact registration, and tunnel resolution. |
| `llm-stack` | `LLM_Frontend`, `LLM_Backend` | Session lifecycle, provider routing, worker orchestration, and Claude Code/Codex CLI adapters. |
| `api-proxy` | `modules/infiniproxy` | OpenAI/Anthropic-compatible API proxy, admin UI, API keys, and client compatibility helpers. |
| `operations` | `systemd/user`, `tests`, project metadata | User services, smoke tests, and deployment metadata. |

## Runtime Flow

```text
Telegram owner
  -> tg-claude-bot
     -> phantom-console for Web/PWA event display
     -> phantom-network for logical contact/tunnel lookup
     -> LLM_Frontend for session and worker execution
        -> LLM_Backend for Claude Code/Codex process adapters

External/API clients
  -> infiniproxy
     -> OpenAI-compatible, Anthropic-compatible, admin, search/scrape/TTS/STT endpoints
```

## Module Boundaries

### `modules/tg-claude-bot`

Owns Telegram-only behavior:

- Bot startup and Telegram handlers.
- Owner-only access control.
- Command routing and Telegram UI rendering.
- Service control commands.
- Compatibility shims for legacy bot imports.

It should delegate LLM execution to `LLM_Frontend`, Web event rendering to `phantom-console`, and network contact/tunnel lookup to `phantom-network`.

### `modules/phantom-console`

Owns browser-facing console behavior:

- aiohttp API/static server.
- Web/PWA assets.
- Event bus and task streaming.
- Console authentication key validation.

It should not own Telegram-specific rendering or LLM process execution.

### `modules/phantom-network`

Owns service discovery and tunnel contact metadata:

- Address book state.
- Contact registration and resolution.
- Cloudflare quick tunnel domain pool.
- Compatibility export for legacy console links.

It should not run bot commands or render UI.

### `LLM_Frontend`

Owns LLM orchestration:

- Session lifecycle.
- Worker/peer management.
- Provider routing and model discovery.
- Local HTTP API consumed by bot and console surfaces.
- Permission/question bridges that call back to the owning UI layer.

It should not directly own Telegram command registration or CLI-specific process details.

### `LLM_Backend`

Owns CLI backend adapters:

- Claude Code adapter.
- Codex adapter.
- Shared backend contract for streaming, interrupt, resume, tools, permission mode, cwd, model, and attachments.

It should be UI-agnostic and callable by `LLM_Frontend`.

### `modules/infiniproxy`

Owns proxy/API compatibility:

- OpenAI-compatible and Anthropic-compatible endpoints.
- Admin UI and API key management.
- Client wrappers and environment helpers.
- Third-party proxy integrations such as Firecrawl, Tavily, SerpAPI, and ElevenLabs.

It should remain independently runnable from the Telegram/LLM control stack.

## Dependency Rules

Preferred direction:

```text
tg-claude-bot -> phantom-console
tg-claude-bot -> phantom-network
tg-claude-bot -> LLM_Frontend
LLM_Frontend -> LLM_Backend
```

Avoid introducing reverse imports from service modules into `tg-claude-bot`. Use callbacks, local HTTP APIs, or narrow package contracts when a lower layer needs to notify a higher layer.

`modules/infiniproxy` is intentionally parallel to the Telegram/LLM stack. It can be managed by the bot as a service, but its proxy implementation should not depend on bot internals.

## Compatibility Shims

Some legacy imports are intentionally preserved under `modules/tg-claude-bot`:

```text
web.server
core.manager
commands.llm
infra.event_log
tools.*
```

New code should import the owning packages directly:

```python
from phantom_console.server import Console
from phantom_console.event_log import BUS
from phantom_console.tasks import get_task_manager
from phantom_llm.manager import SessionManager
from phantom_llm.session import WorkerSession
from phantom_llm.telegram_commands import LLM_COMMANDS
from llm_backend import registry
```

## Adding A Module

When adding a new module:

1. Put runnable services under `modules/<name>/` unless they are a cross-cutting library like `LLM_Backend`.
2. Add a module-local `README.md`.
3. Add `pyproject.toml` or equivalent package metadata when the module is installable.
4. Register the module in `manifest.json` with `id`, `path`, `domain`, `layer`, `kind`, `entrypoints`, and `owns`.
5. Add user service units under `systemd/user/` only if the module should run as a persistent service.
6. Keep runtime state, credentials, local caches, generated files, and vendor toolchains in `.gitignore`.

## Current Cleanup Notes

- `manifest.json` now groups modules into domains and records ownership responsibilities.
- `modules/README.md` provides the directory-level entrypoint for module browsing.
- Root `ARCHITECTURE.md` stays as a short compatibility pointer; this document is the fuller boundary reference.
