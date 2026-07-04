# tg-claude-bot

Telegram ⇄ Claude Code 远程桥。单用户私聊，远程驱动 Claude 会话 + 看构建/主机状态。

> 项目拆分说明：Telegram bot 本体仍在本目录；Web/PWA 控制台已拆到
> `../phantom-console`；LLM 前端已拆到仓库根的 `LLM_Frontend`，CLI 后端拆到
> `LLM_Backend`。旧 import 路径保留兼容 shim，新的代码优先直接 import
> `phantom_console.*`、`phantom_llm.*` 或 `llm_backend.*`。workspace 级说明见根目录
> `ARCHITECTURE.md`。

## 它能干什么

- **驱动 LLM_Frontend 会话**：给 bot 发任何文本 = prompt。bot 只展示首字/处理中/最终结果，核心聊天由 `LLM_Frontend` 调用 `LLM_Backend` 的 Claude Code 或 Codex CLI 后端。
- **工具授权闸**：`permission_mode=default` 时，Claude 每要跑一个工具（Bash/Write/Edit…）就弹一条带「✅ Allow / ⛔ Deny / Allow+不再问」按钮的 TG 消息，你点了才执行。只读工具（Read/Grep/Glob…）默认放行，可在 config 调。
- **构建监控**：`/status`（ph + icecc + ccache）、`/report`（ninja 用时）、`/build`（后台跑 `ph build`，完成推送结果尾巴）。
- **主机监控**：`/sys`（CPU/内存/负载/uptime）、`/disk`、`/top`、`/devices`（adb）。

## 快速开始

1. 跟 [@BotFather](https://t.me/BotFather) 建一个 bot，拿 token。
2. 装依赖（已装过可跳过）：
   ```bash
   python3 -m pip install --user -r requirements.txt
   ```
3. 配置：
   ```bash
   cp config.example.toml config.toml
   # 填 token；owner_id 先留 0，启动后给 bot 发条消息，
   # 看终端日志里的 "denied uid=<你的id>"，把它填回 owner_id。
   ```
4. 跑：
   ```bash
   python3 bot.py            # 默认读同目录 config.toml
   python3 bot.py /path/to/config.toml
   ```
5. TG 里 `/start`。

## 命令

| 命令 | 作用 |
|------|------|
| 任意文本 | 作为 prompt 发给 Claude |
| `/new` | 清空上下文，开新会话 |
| `/stop` | 中断当前回合 |
| `/mode <m>` | 切权限模式 default/acceptEdits/bypassPermissions/plan |
| `/status` `/report` | ph 构建状态 / 用时报告 |
| `/build [args]` | 后台 `ph build --project <P> [args]`，完成推送 |
| `/sys` `/disk` `/top` `/devices` | 主机 / 磁盘 / 进程 / adb |
| `/console` | Mini App 控制台访问链接（实时旁观会话流）|
| `/svc status/start/stop/restart [console\|llm\|all]` | 管理拆分后的子模块服务 |
| `/svcstatus` | 显示 bot/console/llm 的运行状态、版本号与版本描述 |
| `/backend [claude-code\|codex]` | 查看或切换 CLI 后端 |

## Mini App 控制台

bot 进程内置一个 aiohttp 控制台（`[webapp]` 配置段开关），Kiro 风格暗色页面实时
渲染会话事件流：流式正文、工具卡原地变态（含结果摘要）、思考折叠、待办侧栏。
不只旁观，还能**发令**：底部输入条直接给任意会话发 prompt（忙时自动排队）、
✋ 中断在跑回合；会话标签实时显示忙碌脉冲 + 排队数。

API：`GET /api/events`（长轮询事件流）、`GET /api/state`（会话忙闲）、
`POST /api/send`、`POST /api/stop`——全部过 key/initData 鉴权。

- 私聊里 `/console` 附「打开控制台（Mini App）」按钮——Telegram 内嵌打开，走 initData 签名鉴权
- 局域网直开：`/console` 取带 key 的链接（key 从 bot token 派生，等同「旁观+发令」权限，别外传）
- 无公网 IP：`./tunnel.sh` 起 cloudflared 临时 HTTPS 域名（或 tailscale serve），
  把域名填进 `config.toml [webapp].public_url` 后 `/console` 会一并给出隧道链接
- 鉴权双轨：`?key=` 参数，或经 TG web_app 按钮打开时校验 initData 签名 + owner id

### 当前部署

| 项 | 值 |
|----|----|
| 本机端口 | `8788`（按需调整；避开被占端口）|
| 局域网入口 | `http://127.0.0.1:<port>/?key=<ACCESS_KEY>` |
| 公网入口 | `https://<your-tunnel-domain>/?key=<ACCESS_KEY>` |
| 隧道 | cloudflared quick tunnel，systemd 用户单元 `cf-tunnel.service` |
| 健康检查 | `<入口域名>/api/health` → `{"ok": true}` |

> ⚠️ **不要把真实 key / 公网域名写进本 README 或任何提交进 git 的文件。**
> 访问链接里的 `key` 等同「会话旁观 + 发令（可让 Claude 在宿主跑 shell）」权限。
> key 从 bot token 派生：**换 bot token 即作废所有旧链接**——这就是轮换方式。
> 用 `/console` 命令在 Telegram 私聊里现取带 key 的最新链接，别把它贴到聊天/文档/截图。

quick tunnel 域名是**临时的**：机器重启或 `cf-tunnel` 重启后域名会变。恢复流程：

```bash
# 1. 重起隧道（transient unit，不随开机自启）
systemd-run --user --unit=cf-tunnel \
  ~/.local/bin/cloudflared tunnel --url http://localhost:8788 --no-autoupdate
# 2. 抓新域名
journalctl --user -u cf-tunnel --since "-1min" | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
# 3. 填进 config.toml [webapp].public_url，然后
systemctl --user restart tg-claude-bot
```

要固定域名：注册 Cloudflare 账号走 named tunnel（免费，需自有域名），或 tailscale serve
（tailnet 内私有 HTTPS，域名固定但需客户端入网）。

## 安全须知

- **bot 能让 Claude 在你机器上跑 shell。** 只有 `owner_id` 那一个 Telegram 账号能用，其它消息直接丢弃；回调按钮也校验 uid。
- token 泄露 = 别人能冒充你给 bot 发指令（但仍要过 owner_id，所以拿不到 token 的人控制不了 bot；拿到 token 也只是能往这个 bot 发，控制权仍在 owner_id 白名单 + 你点按钮）。仍然：**别把 config.toml 提交进 git**（已在示例里注明）。
- 想全自动无人值守：`permission_mode = "bypassPermissions"` 或 `/mode bypassPermissions`。这会让 Claude 不经你确认就跑任何工具，**只在完全信任的环境用**。
- 默认 `default` 模式：破坏性/写操作都要你按按钮，审批超时 10 分钟自动拒绝。

## 常驻运行（systemd --user）

```bash
mkdir -p ~/.config/systemd/user
cp /home/voyager/桌面/Workspace/PhantomControlPlane/systemd/user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable tg-claude-bot.service phantom-console.service llm-frontend.service phantom-infiniproxy.service
systemctl --user start tg-claude-bot.service
systemctl --user status tg-claude-bot.service
journalctl --user -u tg-claude-bot -f      # 看日志
```
开机自启（无需登录）：`loginctl enable-linger $USER`。

`tg-claude-bot.service` 会链式拉起 `phantom-console.service`、`llm-frontend.service`
和 `phantom-infiniproxy.service`；bot 启动完成后会主动发一条状态消息，列出组件运行状态和版本。

## 文件

- `bot.py` — 主程序（TG 处理 + 常驻 Claude 会话 + 权限闸 + 流式输出）
- `monitor.py` — 主机/构建状态采集
- `config.example.toml` — 配置模板
- `tg-claude-bot.service` — bot 的 systemd --user 单元模板
