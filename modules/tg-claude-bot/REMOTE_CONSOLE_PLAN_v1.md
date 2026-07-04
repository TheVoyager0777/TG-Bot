# 远程控制台升格 · 分阶段落地计划 v1

> 由 MAGI 三脑（MELCHIOR-1 / BALTHASAR-2 / CASPER-3）独立审议草案 v0 后收敛重排。
> v0 的「按技术层横切」被三脑一致否决；v1 改为「安全闸门前置 + 最小外出闭环 + 功能纵切加宽」。
>
> 目标：主人长期外出时，靠手机/浏览器完整操控 Claude agent 班组。
> 代码基线：`/home/voyager/桌面/Workspace/tg-claude-bot`
> 状态约定：每阶段验收必须全绿才进下一阶段；阶段 0+1 全绿前，公网入口由代码硬锁禁止开启。

---

## 设计原则（三脑共识）

1. **安全是闸门不是阶段**：能力面收口与安全加固同批落地，杜绝「阶段 0 完成、阶段 1 未完时挂公网」的暴露窗口。
2. **不靠纪律靠代码**：非回环监听必须读 `secured.flag`（stage0 AND stage1 双位），缺失即 abort，不是 warn。
3. **纵切而非横切**：每个阶段产出端到端可用的能力，不留「端点做好了没界面」「界面做好了没安全」的半成品。
4. **单一真相源**：复用现有 `BUS 事件 → 无状态投影` 架构，远程端不另起第二套状态机。

---

## 阶段 0 ｜ 安全闸门 + 架构地基（前置 · 阻塞一切）

> 完成判据：公网入口可安全开启的全部前提就位。本阶段不全绿，`secured.flag` 的 stage0 位不得置 1。

### 0.A 鉴权与会话
- [ ] **钥匙与 bot_token 解耦**：随机 32B key 单独存盘（chmod 600 + .gitignore），`/console rotate` 一键轮换不动 token。`webapp.py:30-33`
- [ ] **凭据撤出 URL**：首屏 `?token=` 一次性 bootstrap → 立即换 `Set-Cookie: __Host-console=…; HttpOnly; Secure; SameSite=Strict`，之后 `/api/*` 仅认 cookie/`X-Console-Key` header，URL 永不出现 key。`webapp.py:65-73,213` `console.html:256,443,609,706,723`
- [ ] **会话 TTL**：cookie 8–24h 过期 + 空闲 30min 失效 + `/console revoke` 立即吊销全部 session。
- [ ] **失败限速 + 封禁**：同 IP 5 次/60s → 静默封 1h；落审计。`webapp.py:199`
- [ ] **审计日志（append-only）**：perm/send/stop/mode 全量落盘（IP/UA/决策/工具名/input 摘要哈希）。依赖 0.E 事件完整性。
- [ ] **Mini App 强通道**：TG 入口强制 `initData` 校验 + `auth_date` 5min 漂移检查。`webapp.py:36-52`

### 0.B 传输层
- [ ] **监听改 `127.0.0.1`**：外部流量只能经隧道入站，禁局域网直连绕过鉴权。`webapp.py:201`
- [ ] **隧道升级**：cloudflared quick tunnel（随机临时域名）→ 固定域名 + Cloudflare Access（Email OTP / SSO / WARP）**或** Tailscale Funnel + ACL。quick tunnel 仅限 ≤24h 应急。`tunnel.sh`
- [ ] **public_url 出 config.toml**：进 `secrets.toml`（chmod 600 + .gitignore）。`config.toml:30`

### 0.C 能力面收口（BALTHASAR 一票否决项 · 必须与安全同批）
- [ ] **orchestrator 默认不带 `--dangerously-skip-permissions`**：仅主人本机 TG 现场显式 `/mode` 切换时才注入。`agents.py:749-750,814`
- [ ] **远端工具只读白名单**：`/api/perm` 仅放行 `Read/Glob/Grep/TodoWrite/NotebookRead`；`Bash/Edit/Write/WebFetch/Task/mcp__*` 远端不可批，强制返回「去 TG 点」。`webapp.py:153`
- [ ] **远端禁 `always`**：`/api/perm` 仅允许 `allow|deny`；`always` 是 TG 特权决策。`webapp.py:165`
- [ ] **危险工具 TG 二次确认**：Bash/Edit/Write/WebFetch 即使 TG 批准，input 命中 `rm -rf`/`>.ssh`/`curl|sh`/`sudo`/写 `/etc /root /home/*/.ssh` 等模式时强制双确认，远端永不放行。（依赖工具危险等级表先行）
- [ ] **send_file/send_notification 路径白名单**：限 `cwd` 子树 + `/opt/ph-out`，禁读 `~/.ssh ~/.claude/settings.json /etc /root` 与 token 文件。`agents.py:822-823`
- [ ] **auto_allow TTL**：`always` 仅当 worker 当 session 内有效（≤15min + 会话级 reset），重启清空，不继承到远端发起调用。`botapp.py:508`

### 0.D 注入面 / 异常隔离
- [ ] **console.html XSS 必修**：`esc` 增 `"` `'` `` ` ``；链接正则改 `encodeURI` + protocol 白名单(https) + 完整转义；lang 类名走白名单；复核 hljs innerHTML 注入路径。加 CSP + `X-Content-Type-Options:nosniff` + `X-Frame-Options`。`console.html:267,295-296,316` `webapp.py:84-85`
- [ ] **permission_cb 加 try/except**：仿 ask_question_cb，回调异常 → log + 默认 **deny**（守护本能）；公网模式下「无闸 fallback」也改 deny。`agents.py:349-350`
- [ ] **_ask_permission 广义 except**：不止 TimeoutError，任何异常 → emit perm_done + 返回 False。`botapp.py:485-495`
- [ ] **pending token 内存 TTL**：60min 强清扫防重放。

### 0.E 事件总线地基（MELCHIOR：审计/重连/脱敏的共同前置）
- [ ] **busy-check 并发锁原子化**：每 target 一把 `asyncio.Lock`，把「busy 检查 + 入队 or 起 turn」整段包住（run_turn/web_send/_post_to_main/_drain_queue 四入口）。今天 TG 双消息背靠背即可触发，非 web 引入。`botapp.py:704,770,901`
- [ ] **EventBus 加 `min_seq` + `dropped` 信号**：`backlog(since)` 在 `since < min_seq` 时返回 `{dropped:true,oldest:N}`，前端据此触发快照重建。`event_log.py:19,36-39`
- [ ] **BUS 事件 schema 化 + 字段级脱敏标签**：`emit` 由裸 kwargs 改为带字段敏感标记；脱敏中间件（屏蔽 api_key/token/password/bearer/sk-/ghp_/私钥/.ssh 路径）落在其上。`event_log.py:25`
- [ ] **emit 注入 `turn_id`**：`uuid4().hex[:8]`，前端按 `(session,turn_id)` 分流，根治 mirror/active 同 session 交错 + 空 tool_id 撞键。`agents.py:359-435` `live_message.py:112,220-288`
- [ ] **空 tool_id 兜底**：ToolUseBlock 无 id 时生成 `_anon_{counter}` 稳定本地 id。`agents.py:396`

### 0.F 总开关（硬锁）
- [ ] **secured.flag 启动闸**：监听非 `127.0.0.1` 时强制读本地 `secured.flag`（含 stage0/stage1 两独立位，AND 才放行公网监听），缺失/未齐即 abort。隧道侧 ACL 单独留一道，代码闸是兜底不可替换。

---

## 阶段 1 ｜ 最小外出闭环（端到端可用）

> 验收判据（CASPER 定义）：主人在 4G 网下能完整跑完一条闭环——
> **看一轮 → 批一次危险工具(经 TG 二次确认) → 答一次 ask → 网络抖动后恢复 → 紧急 kill**。
> 全绿后 `secured.flag` stage1 位方可置 1，公网入口才允许开启。

- [ ] **in-flight 快照端点(A2)**：扩 `/api/state` 内联每个 active session 当前 `LiveMessage` 可序列化快照（cur_text/tool_rows/status/stats），`epoch` 切换 / `dropped=true` 时优先吃快照再吃增量。`live_message.py:88-95`
- [ ] **ask 端点远端化**：`POST /api/ask {token,action,idx}` → `BotApp.resolve_ask`，console.html 渲染 inline 按钮、经 updated_input 回传 answers（参考 tg-bot-stream-askquestion-fix 已踩坑）。`botapp.py:547`
- [ ] **待审批可见性兜底（BALTHASAR：安全项）**：feed 顶 sticky「🔐 N 个审批待决 ▾」+ 浏览器原生 Notification（授权后）；心跳超时无人应 → 降级 deny，防 agent 永久卡死被拖死会话（DoS 面）。
- [ ] **触控目标 ≥44px**：perm 卡 allow/deny 单按钮独占一行 + 增大字号；危险款显式 modal 二次确认。`console.html`
- [ ] **全停 + 危险操作 confirm**：顶栏常驻「⛔ 全停」按钮（confirm dialog）+ 外出模式 + Kill switch（TG 一句话杀进程+撤 cookie+撤 key+关隧道，不依赖控制台自身）。
- [ ] **localStorage 持久化**：tab 选择 / since 游标 / 草稿 / 滚动位置，断网回来增量拉、回到同一姿态。
- [ ] **文案符号与 TG 统一**：共用 ICON 表（`◐ 运行中 / ✓ 完成 / ⊘ 已中断 / ✗ 出错`），消除 TG/web 中英不齐。改动成本极低，凡碰到的 UI 文件顺手统一。`kiro_ui.py:23-29` `live_message.py:72` `console.html:484-486`

---

## 阶段 2 ｜ 功能纵切加宽（每组端点连同 UI 一起进）

> 原则：杜绝「端点做完没界面」空窗。每组 = 端点(配 BUS emit) + UI + TG 回声 + 审计落盘，完工即可用。

- [ ] **模式组**：`POST /api/mode`（per-session）+ UI。`bypassPermissions` 远端禁触，仅 TG。
- [ ] **模型/档位组**：`POST /api/model`（`set_session_model` 即时切档不丢上下文）+ `POST /api/fast` + UI。`agents.py:990,1063`
- [ ] **provider 组**：`POST /api/provider`（`set_worker_provider/set_active_provider`）+ provider 测试(`/testllm`) UI（外出最怕端点全挂）。`agents.py:1006,1030`
- [ ] **worker 管理组**：`POST /api/spawn`（`spawn_worker`）+ `POST /api/kill`（`stop_worker`）+ 提示词池接入 + UI。`agents.py:841,869`
- [ ] **会话维护组**：`POST /api/compact`（`WorkerSession.compact`）+ `GET /api/context`（`context_usage`）+ UI。`agents.py:457,467`
- [ ] **backlog 续传健全**：maxlen 4000 → 公网模式收紧（重连只回放近 5min），配合 0.E 的 dropped 信号闭环。`event_log.py:19`

---

## 阶段 3 ｜ 锦上添花（无则可用，有则更舒服）

- [ ] **PWA 化**：manifest + service worker（缓存 shell、记住 since、离线显示最后状态、加主屏）。
- [ ] **多 worker 并排视图**：≥768px 横屏分栏，竖屏副屏弹出。**前提：turn_id 已在阶段 0 生效**，否则视图越多串台越快。
- [ ] **文件上传**：手机相册 → worker，`POST /api/upload` + web_send 接受 attachments（受 0.C send_file 白名单约束）。
- [ ] **详情页/transcript 全量浏览**：接 DetailPage 内容到 webapp 独立 tab。
- [ ] **会话条状态可视化**：每会话一行「当前工具 / ⏱ 时间 / 工具数」+ 心跳年龄 + bot 进程健康。
- [ ] **运维二级 tab**：系统状态/磁盘/设备/构建（commands_sys 组）、共享记忆浏览编辑。
- [ ] **主题 / 字号调节**。

---

## 阶段依赖图（关键串行链）

```
turn_id 注入(0.E) ──┬─→ 多 worker 视图(3)
                    └─→ spawn/kill 端点(2)   ［turn_id 必须先行，否则 console 渲染层返工］

BUS schema(0.E) ──→ 脱敏中间件(0.E) ──→ 所有新端点 emit(2)
EventBus dropped(0.E) ──→ 审计日志(0.A) / in-flight 快照(1) / backlog 续传(2)
busy 并发锁(0.E) ──→ ［P0 完成即远端双路并发，必须就位］

secured.flag(0.F) = stage0 ∧ stage1  ──→ 公网监听放行
```

## 验收门（gate）

| 门 | 条件 | 后果 |
|---|---|---|
| Gate-0 | 阶段 0 全绿 | `secured.flag.stage0=1`；仍只允许回环监听 |
| Gate-1 | 阶段 1 闭环验收通过 | `secured.flag.stage1=1`；**公网入口此刻起方可开启** |
| Gate-2 | 阶段 2 各功能组逐组验收 | 远程操控达「功能完备」 |
| Gate-3 | 阶段 3 | 体验打磨，非阻塞 |

---

## 三脑保留意见（记录在案）

- **BALTHASAR-2**：v0 漏掉的 `send_file 路径白名单` 与 `auto_allow TTL` 已在 v1 的 0.C 补回；4 条能力面红线已从 P1 上移至 0.C；硬锁(0.F)已加。此前对 v0 的反对票基于这些缺失，v1 已逐条响应。**任一条在实现阶段被砍 → 反对票重新生效。**
- **MELCHIOR-1**：A1/并发锁/turn_id/BUS schema 已全部上移至 0.E。spawn/kill 与 turn_id 的依赖倒置已通过「turn_id 先行」消解。
- **CASPER-3**：阶段 1 已改为「最小外出闭环」纵切，待审批推送/ask/续传/全停/触控已前移；阶段 2 改为功能纵切（端点+UI 同组）。
