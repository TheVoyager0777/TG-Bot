# Modules

This directory contains long-running services and service-adjacent modules owned by the Phantom control plane.

## Layout

| Module | Domain | Purpose |
| --- | --- | --- |
| `tg-claude-bot/` | `control-entry` | Telegram owner-only bot, command routing, Telegram UI, and service orchestration. |
| `phantom-console/` | `runtime-services` | Web/PWA console service, event bus, static UI, and task streaming. |
| `phantom-network/` | `runtime-services` | Contact book and Cloudflare quick tunnel domain pool. |
| `infiniproxy/` | `api-proxy` | OpenAI/Anthropic-compatible proxy service, admin UI, and client compatibility helpers. |

`LLM_Frontend/` and `LLM_Backend/` live at the repository root because they form a shared LLM stack rather than a service module under `modules/`.

## Conventions

- Each module should have a `README.md`.
- Installable Python modules should have a `pyproject.toml`.
- Persistent services should have a matching user unit in `../systemd/user/`.
- Runtime config, secrets, databases, generated output, caches, and local toolchains should stay out of Git.
- Cross-module boundaries are documented in `../docs/module-architecture.md`.
