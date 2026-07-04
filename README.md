# PhantomControlPlane

集成了 LLM 功能和远程控制台的超级 Bot。

Unified repository for the Phantom Telegram control plane.

## Modules

- `modules/tg-claude-bot`: Telegram owner-only bot, command routing, Telegram UI, and compatibility shims.
- `modules/phantom-console`: Web/PWA console service and EventBus/task streaming.
- `LLM_Frontend`: LLM session daemon/API, provider routing, MCP tools, and Telegram LLM command handlers.
- `LLM_Backend`: CLI backend adapters for Claude Code and Codex.
- `modules/infiniproxy`: OpenAI-compatible to Anthropic API proxy service, managed as an independent submodule.

## Common Commands

From the repo root:

```bash
python3 modules/tg-claude-bot/bot.py modules/tg-claude-bot/config.toml
```

Manage split services:

```bash
PYTHONPATH=modules/phantom-console python3 -m phantom_console.cli status
PYTHONPATH=LLM_Frontend:LLM_Backend python3 -m phantom_llm.daemon status
PYTHONPATH=modules/infiniproxy python3 -m phantom_infiniproxy.cli status
```

Inside Telegram, the bot exposes:

- `/svc status|start|stop|restart [console|llm|infiniproxy|all]`
- `/svcstatus`
- `/backend [claude-code|codex]`

## User Systemd

Install the chained user services:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable tg-claude-bot.service phantom-console.service llm-frontend.service phantom-infiniproxy.service
systemctl --user start tg-claude-bot.service
```

`tg-claude-bot.service` has `Wants=`/`After=` dependencies on
`phantom-console.service`, `llm-frontend.service`, and
`phantom-infiniproxy.service`, so starting the bot chains the submodules first.
On bot startup it sends a Telegram status message showing bot/console/llm/
infiniproxy state and version metadata.

Runtime config is intentionally not stored in this repo.  Create
`modules/tg-claude-bot/config.toml` and, if needed,
`modules/tg-claude-bot/providers.toml` before starting the units.  Infiniproxy
runtime secrets and user database live under `modules/infiniproxy/.env` and
`modules/infiniproxy/proxy_users.db`; both are gitignored.

See `manifest.json` for authoritative module metadata.
