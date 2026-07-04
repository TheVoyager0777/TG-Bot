# phantom-network

`phantom-network` owns the project network contact book and Cloudflare quick
tunnel domain pool.

It gives submodules stable logical names, for example:

- `console.static`
- `console.api`
- `infiniproxy.api`
- `llm.frontend`

Each contact stores a local target, role, lane count, public domain candidates,
health metadata, and the currently preferred public domain. Consumers resolve the
logical name instead of pinning a temporary `trycloudflare.com` URL.

## Commands

```bash
python3 -m phantom_network.cli serve --config ../tg-claude-bot/config.toml
python3 -m phantom_network.cli status
python3 -m phantom_network.cli contacts
python3 -m phantom_network.cli resolve console.api
python3 -m phantom_network.cli register infiniproxy.api http://127.0.0.1:8010 --module infiniproxy --lanes 2
```

Runtime state:

- `~/.config/phantom-network/addressbook.json`
- `~/.config/tg-cf-tunnels.json` compatibility export for older console/bot code

Quick tunnels are still temporary Cloudflare domains. The durable part is the
contact-book indirection plus continuous pool refresh. Fixed domains still require
Cloudflare named tunnels or another stable edge provider.
