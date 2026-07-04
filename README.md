# TG-Bot

集成 LLM 会话、Telegram 控制、远程 Web/PWA 控制台、服务编排、网络联系人和 API 代理能力的 Phantom 控制面仓库。

本仓库名为 `PhantomControlPlane`，GitHub 仓库为 `TG-Bot`。它把原本集中在 Telegram bot 中的能力拆分成多个可独立运行的服务：Telegram owner-only bot、Web 控制台、LLM 前端守护进程、CLI 后端适配层、InfiniProxy API 代理，以及 Phantom Network 联系人/隧道登记服务。

## 项目定位

TG-Bot 是一个面向个人/私有环境的远程智能控制平面。核心目标是：

- 通过 Telegram 私聊给唯一 owner 提供安全入口。
- 把 Claude Code / Codex 等 CLI 后端封装成可流式交互的 LLM 会话。
- 提供 Web/PWA 控制台查看会话、事件流、任务状态和交互过程。
- 通过 systemd user units 编排 bot、console、LLM frontend、network 和 proxy 服务。
- 通过 InfiniProxy 暴露 OpenAI/Anthropic 兼容 API 和若干第三方服务代理入口。
- 保持运行时密钥、用户数据库、缓存和本地 Android 工具链不进入 Git。

## 架构总览

```text
Telegram owner
    |
    v
modules/tg-claude-bot
    |-- command handlers: /svc, /backend, /version, session/file/system commands
    |-- Telegram UI: permission buttons, live messages, task/detail views
    |
    +--> modules/phantom-console  : Web/PWA console and event stream
    +--> modules/phantom-network  : contact book and Cloudflare tunnel domain pool
    +--> LLM_Frontend             : local LLM session daemon/API
             |
             v
        LLM_Backend               : Claude Code / Codex CLI adapters

modules/infiniproxy
    |
    +--> OpenAI-compatible /v1/chat/completions
    +--> Anthropic-compatible /v1/messages
    +--> admin UI, API keys, search/scrape/TTS/STT proxy endpoints
```

## 目录结构

```text
.
├── README.md
├── ARCHITECTURE.md
├── manifest.json
├── package.json
├── playwright.config.js
├── tests/
├── systemd/user/
├── LLM_Backend/
├── LLM_Frontend/
└── modules/
    ├── tg-claude-bot/
    ├── phantom-console/
    ├── phantom-network/
    └── infiniproxy/
```

`APP/` 是本机 Android/外部工作区位置，当前被 `.gitignore` 排除，不属于本仓库核心受管源码。

## 模块说明

| 模块 | 路径 | 版本 | 作用 |
| --- | --- | --- | --- |
| Telegram Bot | `modules/tg-claude-bot` | `1.10.1` | Telegram owner-only 入口、命令路由、Telegram UI、服务控制、兼容 shim。 |
| Phantom Console | `modules/phantom-console` | `0.2.0` | Web/PWA 控制台，提供 aiohttp API、静态页面、事件流和任务展示。 |
| Phantom Network | `modules/phantom-network` | `0.1.0` | 联系人簿、逻辑服务名解析、Cloudflare quick tunnel 域名池。 |
| LLM Frontend | `LLM_Frontend` | `0.4.0` | LLM 会话守护进程、本地 HTTP API、provider 路由、worker/session 管理。 |
| LLM Backend | `LLM_Backend` | `0.3.0` | Claude Code / Codex CLI 后端适配，处理流式输出、中断、恢复、工具权限等。 |
| InfiniProxy | `modules/infiniproxy` | `0.1.0` | OpenAI/Anthropic 兼容 API 代理、admin UI、API key 管理和第三方服务代理。 |

## 主要能力

- Telegram 私聊控制：只允许 `owner_id` 指定的用户操作。
- 服务生命周期管理：从 Telegram 内启动、停止、重启和查询子服务。
- LLM 会话：支持 Claude Code 与 Codex 后端切换。
- 工具权限闸门：默认由 Telegram 按钮审批敏感工具调用。
- Web 控制台：浏览器/PWA 访问事件流、会话输出和过程抽屉。
- Worker/班组模式：LLM Frontend 支持多 session/worker 管理。
- Provider 路由：通过 `providers.toml` 管理 Anthropic 协议兼容端点。
- InfiniProxy：提供 `/v1/chat/completions`、`/v1/messages`、admin、搜索、抓取、TTS/STT 等代理接口。
- systemd user 部署：bot 服务会链式拉起 console、network、llm 和 infiniproxy。

## Telegram 命令

常用命令：

```text
/svc status|start|stop|restart [console|llm|infiniproxy|network|all]
/svcstatus
/version
/backend [claude-code|codex]
```

更多命令实现位于：

- `modules/tg-claude-bot/commands/`
- `LLM_Frontend/phantom_llm/telegram_commands.py`

## 本地端口与 API

| 服务 | 默认地址 | 典型接口 |
| --- | --- | --- |
| Phantom Console API | `127.0.0.1:8765` | `/health`, `/api/events`, `/api/send` |
| Phantom Console Static | `127.0.0.1:8766` | Web/PWA 静态资源 |
| Phantom Network | `127.0.0.1:8890` | `/health`, `/contacts`, `/resolve/{name}`, `/register` |
| LLM Frontend | `127.0.0.1:8799` | `/health`, `/state`, `/run`, `/backend`, `/sessions`, `/providers` |
| InfiniProxy | `127.0.0.1:8010` | `/health`, `/admin`, `/v1/messages`, `/v1/chat/completions` |

## 快速开始

### 1. 准备依赖

建议环境：

- Python `>=3.10`
- Node.js 与 npm，用于 Playwright smoke test
- Telegram Bot token 与 owner 用户 ID
- 可选：`claude` CLI、`codex` CLI、`cloudflared`

安装 Python 依赖的一种方式：

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -e modules/phantom-console
pip install -e modules/phantom-network
pip install -e LLM_Frontend
pip install -e modules/infiniproxy
pip install -r modules/tg-claude-bot/requirements.txt
```

安装测试依赖：

```bash
npm install
```

### 2. 创建 Telegram bot 配置

```bash
cp modules/tg-claude-bot/config.example.toml modules/tg-claude-bot/config.toml
```

编辑 `modules/tg-claude-bot/config.toml`：

```toml
[telegram]
token = "123456:ABC-DEF..."
owner_id = 123456789

[llm_backend]
active = "claude-code" # or "codex"
```

`config.toml` 已被 `.gitignore` 排除，不应提交。

### 3. 可选：配置 LLM provider

```bash
cp modules/tg-claude-bot/providers.example.toml modules/tg-claude-bot/providers.toml
```

`providers.toml` 用于管理 Anthropic Messages API 协议兼容端点，例如 Anthropic 官方、第三方中转、自建反代或 LiteLLM 网关。它可能包含 token，也已被 `.gitignore` 排除。

### 4. 可选：配置 InfiniProxy

```bash
cp modules/infiniproxy/.env.example modules/infiniproxy/.env
```

填入：

```dotenv
AIAPI_URL=http://127.0.0.1:8010
AIAPI_KEY=your-proxy-api-key-here
OPENAI_BASE_URL=http://127.0.0.1:8010/v1
OPENAI_API_KEY=your-proxy-api-key-here
ANTHROPIC_BASE_URL=http://127.0.0.1:8010/v1
ANTHROPIC_API_KEY=your-proxy-api-key-here
```

`modules/infiniproxy/.env` 和 `modules/infiniproxy/proxy_users.db` 属于运行时 secret/state，不应提交。

## 前台运行

直接运行 bot：

```bash
PYTHONPATH=modules/tg-claude-bot:modules/phantom-console:modules/phantom-network:LLM_Frontend:LLM_Backend:modules/infiniproxy \
python3 modules/tg-claude-bot/bot.py modules/tg-claude-bot/config.toml
```

分别运行子服务：

```bash
PYTHONPATH=modules/phantom-console:modules/tg-claude-bot \
python3 -m phantom_console.cli serve --config modules/tg-claude-bot/config.toml

PYTHONPATH=modules/phantom-network:modules/tg-claude-bot \
python3 -m phantom_network.cli serve --config modules/tg-claude-bot/config.toml --netns none --no-netns-autostart

PYTHONPATH=LLM_Frontend:LLM_Backend:modules/tg-claude-bot \
python3 -m phantom_llm.daemon serve --config modules/tg-claude-bot/config.toml --host 127.0.0.1 --port 8799

PYTHONPATH=modules/infiniproxy \
python3 -m phantom_infiniproxy.cli serve --env-file modules/infiniproxy/.env --host 127.0.0.1 --port 8010
```

查询状态：

```bash
PYTHONPATH=modules/phantom-console python3 -m phantom_console.cli status
PYTHONPATH=modules/phantom-network python3 -m phantom_network.cli status
PYTHONPATH=LLM_Frontend:LLM_Backend python3 -m phantom_llm.daemon status
PYTHONPATH=modules/infiniproxy python3 -m phantom_infiniproxy.cli status
```

## systemd user 部署

仓库提供用户级 systemd unit：

```text
systemd/user/tg-claude-bot.service
systemd/user/phantom-console.service
systemd/user/phantom-network.service
systemd/user/llm-frontend.service
systemd/user/phantom-llm.service
systemd/user/phantom-infiniproxy.service
```

安装：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable tg-claude-bot.service phantom-console.service phantom-network.service llm-frontend.service phantom-infiniproxy.service
systemctl --user start tg-claude-bot.service
```

`tg-claude-bot.service` 声明了：

```text
Wants=phantom-console.service phantom-network.service llm-frontend.service phantom-infiniproxy.service
After=phantom-console.service phantom-network.service llm-frontend.service phantom-infiniproxy.service
```

因此启动 bot 会链式拉起依赖服务。

注意：当前 unit 文件包含本机绝对路径 `/home/voyager/桌面/Workspace/PhantomControlPlane`。如果克隆到其他目录，需要先替换 unit 里的 `WorkingDirectory`、`PYTHONPATH`、`ExecStart` 和 `ConditionPathExists`。

常用 systemd 命令：

```bash
systemctl --user status tg-claude-bot.service
systemctl --user restart tg-claude-bot.service
journalctl --user -u tg-claude-bot.service -f
```

## 开发与测试

项目级 Playwright smoke tests：

```bash
npm run test:e2e
npm run test:e2e:console
```

`tests/console.spec.js` 会读取 `modules/tg-claude-bot/config.toml` 中的 Telegram token 生成 console key，并默认连接 `http://127.0.0.1:8875` 作为 console API。可用环境变量覆盖：

```bash
PHANTOM_BOT_CONFIG=modules/tg-claude-bot/config.toml \
PHANTOM_CONSOLE_API=http://127.0.0.1:8765 \
npm run test:e2e:console
```

InfiniProxy 还包含大量兼容性与端到端测试文件，主要位于：

```text
modules/infiniproxy/test_*.py
modules/infiniproxy/tests/
```

其中部分测试需要真实代理服务、API key 或第三方服务环境变量。

## 安全与运行时状态

以下文件和目录按设计不提交：

```text
modules/tg-claude-bot/config.toml
modules/tg-claude-bot/providers.toml
modules/tg-claude-bot/.runtime/
modules/infiniproxy/.env
modules/infiniproxy/.env.*
modules/infiniproxy/proxy_users.db*
modules/infiniproxy/k8s/secrets.yaml
.agentmem/
state/
run/
node_modules/
test-results/
APP/
```

安全默认值：

- Telegram bot 只服务 `owner_id`。
- LLM Frontend standalone daemon 默认拒绝交互式工具审批。
- `bypassPermissions` 需要显式开启，且只应在完全信任的环境中使用。
- Provider token、Telegram token、InfiniProxy API key 和数据库都应保留在本机或部署环境 secret 中。

## 兼容层说明

拆分前的老导入路径仍保留为 thin compatibility shims，例如：

```text
web.server
core.manager
commands.llm
infra.event_log
tools.*
```

新代码应优先导入所属模块：

```python
from phantom_console.server import Console
from phantom_console.event_log import BUS
from phantom_console.tasks import get_task_manager
from phantom_llm.manager import SessionManager
from phantom_llm.session import WorkerSession
from phantom_llm.telegram_commands import LLM_COMMANDS
from llm_backend import registry
```

## 故障排查

- Bot 启动后无响应：确认 `config.toml` 中 `telegram.token` 和 `owner_id` 正确。
- systemd unit 不启动：检查 unit 内绝对路径是否匹配当前 clone 路径。
- Console 测试失败：确认 console API 已启动，且 `PHANTOM_CONSOLE_API` 指向正确端口。
- LLM 无输出：确认 `claude` 或 `codex` CLI 可执行文件在 `PATH` 中，且 `[llm_backend] active` 配置正确。
- InfiniProxy 失败：确认 `modules/infiniproxy/.env` 存在，端口 `8010` 未被占用。
- Cloudflare/network 功能异常：确认 `phantom-network` 已启动，并按需安装 `cloudflared`。

## 参考文件

- `manifest.json`：权威模块元数据、端口、入口和运行时状态。
- `ARCHITECTURE.md`：拆分后的模块边界和导入建议。
- `modules/tg-claude-bot/README.md`：Telegram bot 细节。
- `modules/phantom-console/README.md`：Web/PWA console 细节。
- `modules/phantom-network/README.md`：联系人和隧道域名池细节。
- `LLM_Frontend/README.md`：LLM Frontend 细节。
- `LLM_Backend/README.md`：CLI 后端适配细节。
- `modules/infiniproxy/README.md`：InfiniProxy API 代理细节。
