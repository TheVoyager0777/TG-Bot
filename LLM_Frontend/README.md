# LLM_Frontend

LLM session/API layer for the Phantom Telegram control plane.

This project owns worker sessions, SessionManager, provider routing,
model discovery, shared-memory hooks, prompt pools, MCP tools, and the
Telegram LLM command handlers.

CLI execution is selected through the sibling `LLM_Backend` package.  The
current backend is configured in `[llm_backend]` as `claude-code` or `codex`.

During the split from `tg-claude-bot`, the old `core.manager`,
`core.session`, `commands.llm`, `infra.router`, `infra.model_discovery`,
`infra.sharedmem`, `tools.*`, and `bridge.prompt_pool` import paths remain
as compatibility shims in the bot project.

## Lifecycle

Foreground local daemon:

```bash
PYTHONPATH=/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Frontend:/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Backend \
python3 -m phantom_llm.daemon serve \
  --config /home/voyager/桌面/Workspace/PhantomControlPlane/modules/tg-claude-bot/config.toml \
  --host 127.0.0.1 --port 8799
```

Standalone mode denies interactive tool approvals by default and does not allow
`bypassPermissions` unless started with `--allow-bypass`.

Background:

```bash
PYTHONPATH=/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Frontend:/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Backend \
python3 -m phantom_llm.daemon start --config /path/to/config.toml
PYTHONPATH=/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Frontend:/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Backend \
python3 -m phantom_llm.daemon status
PYTHONPATH=/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Frontend:/home/voyager/桌面/Workspace/PhantomControlPlane/LLM_Backend \
python3 -m phantom_llm.daemon stop
```

Equivalent installed command: `phantom-llm serve|start|status|stop`.

Local API while running:

- `GET /health`
- `GET /state`
- `GET /backend`
- `POST /backend {"name":"claude-code|codex"}`
- `POST /run {"session":"main","text":"..."}`
- `POST /worker {"name":"worker-name"}`
- `DELETE /worker/{name}`
- `POST /interrupt {"session":"main"}`
- `POST /shutdown`
