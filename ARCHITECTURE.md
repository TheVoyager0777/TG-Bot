# Phantom Control Projects

This workspace splits the previous monolithic `tg-claude-bot` checkout into
five project directories:

- `modules/tg-claude-bot/`: Telegram bot entrypoint, handlers, Telegram UI, system and
  file/relay commands, service unit, and compatibility shims.
- `modules/phantom-console/`: Web/PWA console, aiohttp API/static server, EventBus,
  background task streaming, and console assets.
- `LLM_Frontend/`: LLM daemon/API, SessionManager, provider routing, model
  discovery, shared-memory hooks, prompt pool, MCP tools, and LLM command handlers.
- `LLM_Backend/`: independent CLI backend adapters for Claude Code and Codex.
- `modules/infiniproxy/`: OpenAI-compatible to Anthropic API proxy service and admin UI,
  managed as its own submodule with live `.env` and user database state.

The bot can still run directly from `modules/tg-claude-bot/bot.py`.  It injects
project paths at startup, and old import paths such as `web.server`,
`core.manager`, `commands.llm`, `infra.event_log`, and `tools.*` remain as thin
compatibility shims.

New code should import the owning package directly:

- `phantom_console.server.Console`
- `phantom_console.event_log.BUS`
- `phantom_console.tasks.get_task_manager`
- `phantom_llm.manager.SessionManager`
- `phantom_llm.session.WorkerSession`
- `phantom_llm.telegram_commands.LLM_COMMANDS`
- `llm_backend.registry`

Independent lifecycle commands:

- Console: `python3 -m phantom_console.cli serve|start|status|stop`
- LLM Frontend: `python3 -m phantom_llm.daemon serve|start|status|stop`
- Infiniproxy: `python3 -m phantom_infiniproxy.cli serve|start|status|stop`
