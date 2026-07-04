# Phantom Control Architecture

This file is kept as the short architecture pointer for older references.

The current module boundary document is `docs/module-architecture.md`, and the
machine-readable module map is `manifest.json`.

## Domains

- `control-entry`: `modules/tg-claude-bot`
- `runtime-services`: `modules/phantom-console`, `modules/phantom-network`
- `llm-stack`: `LLM_Frontend`, `LLM_Backend`
- `api-proxy`: `modules/infiniproxy`
- `operations`: `systemd/user`, `tests`, project metadata

## Import Rule

New code should import the owning package directly rather than legacy shim paths:

- `phantom_console.server.Console`
- `phantom_console.event_log.BUS`
- `phantom_console.tasks.get_task_manager`
- `phantom_llm.manager.SessionManager`
- `phantom_llm.session.WorkerSession`
- `phantom_llm.telegram_commands.LLM_COMMANDS`
- `llm_backend.registry`
