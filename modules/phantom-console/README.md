# phantom-console

Web/PWA console for the Phantom Telegram control plane.

This project owns the aiohttp console server, static Mini App/PWA assets,
the shared EventBus, and background task streaming used by the console.

During the split from `tg-claude-bot`, the old `web.*`, `infra.event_log`,
and `core.tasks` import paths remain as compatibility shims in the bot
project.

## Lifecycle

Foreground:

```bash
PYTHONPATH=/home/voyager/桌面/Workspace/phantom-console \
python3 -m phantom_console.cli serve \
  --config /home/voyager/桌面/Workspace/tg-claude-bot/config.toml
```

Background:

```bash
PYTHONPATH=/home/voyager/桌面/Workspace/phantom-console \
python3 -m phantom_console.cli start --config /path/to/config.toml
PYTHONPATH=/home/voyager/桌面/Workspace/phantom-console \
python3 -m phantom_console.cli status
PYTHONPATH=/home/voyager/桌面/Workspace/phantom-console \
python3 -m phantom_console.cli stop
```

Equivalent installed command: `phantom-console serve|start|status|stop`.
