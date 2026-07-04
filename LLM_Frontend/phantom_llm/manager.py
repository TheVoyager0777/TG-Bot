"""core.manager — SessionManager：管理 orchestrator + 多个 worker。

- 管理 orchestrator(主对话) + 多个 worker，多会话常驻。
- orchestrator 通过 in-process MCP 工具调度 worker（spawn/send/list/stop/peek）。
- 持久化 worker 状态（session_id / provider / model）到 sessions.json，重启后恢复。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import time

try:
    from llm_backend.registry import backend_info, normalize as normalize_backend
except Exception:  # pragma: no cover - legacy standalone compatibility
    def normalize_backend(name: str | None) -> str:
        return (name or "claude-code").strip().lower().replace("_", "-")

    def backend_info(active: str | None, _config: dict | None = None) -> dict:
        return {"active": normalize_backend(active), "available": {}}

from phantom_llm.session import (
    TurnSink,
    WorkerSession,
    SendFileCallback,
    _cli_session_persistence_enabled,
    _short,
    DEFAULT_SUBAGENTS,
    MAGI_AGENTS,
    MAGI_SYSTEM_APPEND,
    WORKER_TEAMING_PROMPT,
)

from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
)

log = logging.getLogger("tgclaude.agents")


# ── SessionManager：管理 orchestrator + 多个 worker ──────────────────────────
class SessionManager:
    ORCH = "main"  # orchestrator 的固定名字
    INTERNAL_WORKER_PREFIXES = ("llm-events-",)

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.workers: dict[str, WorkerSession] = {}
        self.orchestrator: WorkerSession | None = None
        # worker 之间的对等消息收件箱：target_name -> [(sender, message), ...]。
        # 非阻塞投递：发信方 turn 不等收信方（避免 A↔B 死锁）。收信方空闲即派发，
        # 忙则暂存，待其当前 turn 结束由 drain_peer_inbox 抽干续跑。
        self.peer_inbox: dict[str, list[tuple[str, str]]] = {}
        # bot 层注入：弹 TG 权限按钮 (worker_name, tool, input) -> bool
        self.permission_cb = None
        # bot 层注入：AskUserQuestion 渲染按钮收集回答 (worker_name, input) -> updated_input|None
        self.ask_question_cb = None
        # bot 层注入：worker 后台跑完/有产出时通知 owner (worker_name, text) -> None
        self.notify_cb = None
        # bot 层注入：worker 经 peer 工具给主对话发消息时调用 (sender, message) -> result_str。
        # BotApp 实现：把消息排进主对话的下一轮 prompt（忙则入队），返回投递状态描述。
        self.notify_main_cb = None
        # bot 层注入：LLM 发文件给主人 (path, caption, target) -> None
        self.send_file_cb: SendFileCallback | None = None
        # bot 层注入：列出可用的文件转发群 () -> {chat_id: 别名}
        self.list_forward_targets = None
        # bot 层注入：发文本通知到目标(转发群/主控/relay) (text, target) -> message_id
        self.send_notification_cb = None
        # bot 层注入：置顶/取消置顶 (target, message_id, unpin) -> str(结果说明)
        self.pin_cb = None
        # bot 层注入：snapshot 时附带的额外可恢复数据（如论坛话题映射）
        self.snapshot_extra = None
        # 存盘抑制闸：启动/恢复窗口里置 True，让 save_state 变 no-op。
        # 关键：start_orchestrator/restore_workers 期间 self.workers、bot.topics 都还没
        # 填好，若此时 orchestrator 连上拿到（fork 出的）新 session_id 触发 on_session_id→
        # save_state，会把整份持久化文件覆盖成「空 workers + 空 topics」，导致下次重启
        # relay/MAGI 无从恢复、被当新 worker 重建并另开论坛话题。恢复全部就绪后再解闸存一次。
        self._save_suspended = False
        self.auto_allow: set[str] = set(cfg["claude"].get("auto_allow_tools", []))
        self.default_mode = cfg["claude"].get("permission_mode", "default")
        self.backend_config: dict = dict(cfg.get("llm_backend") or {})
        self.active_backend = normalize_backend(
            self.backend_config.get("active") or self.backend_config.get("backend") or "claude-code")
        # fast mode：CLI 默认开启加速输出；可被 /fast 关闭（持久化）
        self.fast_mode = bool(cfg["claude"].get("fast_mode", True))
        # thinking：模型推理/思考过程，默认开启
        self.thinking_enabled = bool(cfg["claude"].get("thinking", True))
        # LLM provider 路由（可选；providers.toml 不存在则为 None，行为同以前）
        self.router = None
        prov_path = cfg.get("_providers_path")
        if prov_path:
            try:
                from phantom_llm.router import Router
                self.router = Router.from_file(prov_path)
                if self.router:
                    log.info("router: %d provider(s), active=%s",
                             len(self.router.providers), self.router.active)
            except Exception as e:
                log.warning("router load failed: %s", e)
        # settings.json 的 env 段会覆盖 options.env，导致 provider 路由失效。
        # 当路由层接管端点时，只使用项目内生成的 isolated settings；
        # 不读取 ~/.claude/settings.json，避免服务行为依赖用户级配置。
        self.feature_env: dict[str, str] = self._load_project_feature_env()
        runtime_dir = Path(
            cfg.get("llm_frontend", {}).get("runtime_dir")
            or Path(cfg.get("_providers_path") or ".").resolve().parent / ".runtime"
        )
        self._isolated_settings_dir = Path(
            cfg.get("llm_frontend", {}).get("settings_dir")
            or runtime_dir / "claude-settings"
        )
        self._isolated_home_dir = Path(
            cfg.get("llm_frontend", {}).get("claude_home_dir")
            or runtime_dir / "claude-home"
        )
        # 共享记忆/协调台账：多 worker 文件改动的稀疏索引（SQLite）。
        # 默认放工作目录下 .agentmem/shared.db；可被 config 覆盖。
        self.shared_mem = None
        try:
            from phantom_llm.sharedmem import SharedMemory
            repo = c if (c := cfg["claude"].get("cwd")) else os.getcwd()
            db = cfg.get("_sharedmem_path") or os.path.join(repo, ".agentmem", "shared.db")
            self.shared_mem = SharedMemory(db, repo)
            log.info("shared memory: %s (repo=%s)", db, repo)
        except Exception as e:
            log.warning("shared memory init failed: %s", e)
        # worker 会话持久化：进程重启后按 session_id resume 恢复对话。
        repo2 = cfg["claude"].get("cwd") or os.getcwd()
        self.state_path = cfg.get("_state_path") or os.path.join(
            repo2, ".agentmem", "sessions.json")
        # 每会话的提示词池：worker 名/"main"/"*"(共享) -> 命名提示词。可在菜单里调用/编辑。
        self.prompts = None
        try:
            from phantom_llm.prompt_pool import PromptPool, SHARED, default_seed
            pp = cfg.get("_prompts_path") or os.path.join(
                repo2, ".agentmem", "prompts.json")
            self.prompts = PromptPool(pp)
            # 首次：给共享池(*)播一份通用种子，方便主人直接用/改
            if not self.prompts.owners():
                seed = default_seed().get(SHARED, {})
                for nm, entry in seed.items():
                    try:
                        self.prompts.save(SHARED, nm, entry.get("text", ""))
                    except Exception:
                        pass
            log.info("prompt pool: %s (%s)", pp, self.prompts.stats())
        except Exception as e:
            log.warning("prompt pool init failed: %s", e)

    # ---- 会话持久化（重启后恢复 worker 对话）----
    @classmethod
    def _is_internal_worker_name(cls, name: str | None) -> bool:
        return any(str(name or "").startswith(prefix) for prefix in cls.INTERNAL_WORKER_PREFIXES)

    def snapshot(self) -> dict:
        """当前所有 worker 的可恢复状态。orchestrator 的 session_id 也存。"""
        workers = []
        for w in self.workers.values():
            if self._is_internal_worker_name(w.name):
                continue
            worker_sid = (w.session_id or w.resume_session_id) if self._persist_session_id(w.backend_config) else None
            workers.append({"name": w.name, "provider": w.provider, "mode": w.mode,
                            "model": w.model,
                            "session_id": worker_sid,
                            "system_append": w.system_append,
                            "agents_set": w.agents_set,
                            "cwd": w.cwd})
        orch_sid = None
        if self.orchestrator and self._persist_session_id(self.orchestrator.backend_config):
            orch_sid = self.orchestrator.session_id
        orch_model = self.orchestrator.model if self.orchestrator else None
        snap = {"orchestrator_session_id": orch_sid,
                "orchestrator_model": orch_model, "workers": workers}
        # 全局「指令操作」状态：/mode 设的默认模式、/providers 选的活跃端点。
        # 重启后要在 start_orchestrator / restore_workers 之前先恢复，新会话才用对。
        snap["settings"] = {
            "default_mode": self.default_mode,
            "active_provider": self.router.active if self.router else None,
            "active_backend": self.active_backend,
            "fast_mode": self.fast_mode,
        }
        # bot 层可注入额外可恢复数据（如论坛话题映射）
        if self.snapshot_extra:
            try:
                snap["extra"] = self.snapshot_extra()
            except Exception:
                pass
        return snap

    @staticmethod
    def _snapshot_is_empty(snap: dict) -> bool:
        """半成品判定：既无 worker、又无 orchestrator session、又无论坛话题。
        典型成因=PTB initialize/bootstrap 阶段（_post_init 之前）就超时崩溃，
        b.start() 从未跑、self.workers/topics 全空，此刻若落盘会把好文件清空。"""
        if snap.get("workers"):
            return False
        if snap.get("orchestrator_session_id"):
            return False
        if (snap.get("extra") or {}).get("topics"):
            return False
        return True

    def save_state(self):
        import json
        # 启动/恢复窗口：抑制存盘，避免半成品状态覆盖好文件（见 _save_suspended 注释）。
        if self._save_suspended:
            return
        try:
            snap = self.snapshot()
            # 护栏：空快照绝不覆盖「已填好」的盘。否则一次 init 崩溃触发的 shutdown
            # 存盘会把 relay/MAGI 的 session + 论坛话题映射抹成空，下次重启被当新 worker
            # 重建、另开论坛话题 → 重复话题堆积。空覆空则放行（无损）。
            if self._snapshot_is_empty(snap):
                prev = self.load_state()
                if not self._snapshot_is_empty(prev):
                    log.warning("save_state: 拒绝用空快照覆盖已填充状态（疑似 init 崩溃 shutdown）")
                    return
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            log.warning("save_state failed: %s", e)

    def load_state(self) -> dict:
        import json
        try:
            with open(self.state_path) as f:
                return json.load(f) or {}
        except Exception:
            return {}

    @contextlib.contextmanager
    def suspend_saves(self):
        """启动/恢复期间抑制存盘的上下文管理器：进入即闸上，退出解闸并补存一次。
        把整段「起 orchestrator + 恢复 worker + 建话题」包起来，期间任何 save_state
        都被吞掉（防半成品覆盖好文件），全部就绪后退出时落一次完整盘。"""
        prev = self._save_suspended
        self._save_suspended = True
        try:
            yield
        finally:
            self._save_suspended = prev
            if not prev:
                self.save_state()

    def restore_settings(self) -> dict:
        """在拉起任何会话之前恢复全局指令状态（/mode 默认模式、/providers 活跃端点）。
        必须早于 start_orchestrator/restore_workers，否则新会话仍用 config 默认值。"""
        st = self.load_state()
        s = st.get("settings") or {}
        if s.get("default_mode"):
            self.default_mode = s["default_mode"]
        if "fast_mode" in s:
            self.fast_mode = bool(s["fast_mode"])
        if s.get("active_backend"):
            self.active_backend = normalize_backend(s["active_backend"])
        ap = s.get("active_provider")
        if ap and self.router and self.router.get(ap):
            self.router.set_active(ap)
        return s

    async def restore_workers(self, state: dict | None = None) -> list[str]:
        """读 state 文件，重新拉起上次的 worker 并 resume 其对话。返回恢复的名字。
        可传入已加载的 state（启动时复用 start() 那份），避免重新读盘读到
        恢复窗口内被半成品 save 覆盖过的文件。"""
        st = state if state is not None else self.load_state()
        restored = []
        for wd in st.get("workers", []):
            name = wd.get("name")
            if not name or name in self.workers or self._is_internal_worker_name(name):
                continue
            try:
                await self.spawn_worker(name, mode=wd.get("mode"),
                                        provider=wd.get("provider"),
                                        model=wd.get("model"),
                                        resume_session_id=wd.get("session_id"),
                                        system_append=wd.get("system_append"),
                                        agents_set=wd.get("agents_set"),
                                        cwd=wd.get("cwd"))
                restored.append(name)
            except Exception as e:
                log.warning("restore worker '%s' failed: %s", name, e)
        return restored

    # 端点相关 env key（交给路由层管，不从 settings 继承）
    _ENDPOINT_ENV_KEYS = {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_BETAS",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    }

    def _load_project_feature_env(self) -> dict[str, str]:
        """Project-scoped Claude env overrides.

        Endpoint/model keys remain owned by providers.toml so routing stays
        deterministic. This intentionally does not read ~/.claude/settings.json.
        """
        envd = self.cfg.get("claude", {}).get("env") or {}
        if not isinstance(envd, dict):
            return {}
        return {k: str(v) for k, v in envd.items() if k not in self._ENDPOINT_ENV_KEYS}

    @staticmethod
    def _persist_session_id(backend_config: dict | None) -> bool:
        """Whether the selected external CLI backend should resume sessions."""
        return _cli_session_persistence_enabled(backend_config)

    def _drop_transient_session_id(self, w: WorkerSession) -> None:
        if not self._persist_session_id(w.backend_config):
            w._clear_session_id()

    def _isolated_settings_for(self, provider: str | None) -> str | None:
        """Write a provider-scoped Claude settings file under the project tree."""
        if not (self.router and self.router.providers):
            return None
        env = {**self.feature_env, **self.router.env_for(provider)}
        p = self.router.get(provider or self.router.active) or self.router.get(self.router.active)
        if p:
            models = p.models or {}
            alias_names = {
                # These are Claude Code-compatible alias labels; the provider
                # model envs below map them to the real upstream model names.
                "opus": "claude-opus-4-7",
                "sonnet": "claude-opus-4-7",
                "haiku": "claude-haiku-4-6",
            }
            # Claude Code currently consults both *_MODEL and *_MODEL_NAME.
            for tier in ("opus", "sonnet", "haiku"):
                value = models.get(tier)
                if not value:
                    continue
                key = tier.upper()
                env[f"ANTHROPIC_DEFAULT_{key}_MODEL"] = str(value)
                env.setdefault(f"ANTHROPIC_DEFAULT_{key}_MODEL_NAME", alias_names[tier])
        # Preserve the known working service-level defaults without reading user
        # settings. The typo key is intentional for compatibility with the
        # existing manual environment.
        env.setdefault("CLAUDE_CODE_ATTRIBUTION_HEADER", "false")
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "true")
        env.setdefault("CLAUDE_CODE_DIASBLE_NONSTREAMING_FALLBACK", "true")
        env.setdefault("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")
        env.setdefault("ENABLE_TOOL_SEARCH", "true")
        effort = self.cfg.get("claude", {}).get("effort")
        if effort:
            env.setdefault("CLAUDE_CODE_EFFORT_LEVEL", str(effort))

        self._isolated_settings_dir.mkdir(parents=True, exist_ok=True)
        name = provider or (self.router.active if self.router else "default") or "default"
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
        path = self._isolated_settings_dir / f"{safe}.json"
        tmp = path.with_suffix(".json.tmp")
        hooks = {
            "WorktreeCreate": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/home/voyager/.claude/hooks/worktree-create.sh",
                        }
                    ]
                }
            ],
            "WorktreeRemove": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/home/voyager/.claude/hooks/worktree-remove.sh",
                        }
                    ]
                }
            ],
        }
        tmp.write_text(json.dumps({"env": env, "hooks": hooks}, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)

    def _backend_config_for(self, provider: str | None = None) -> dict:
        cfg = dict(self.backend_config)
        settings = self._isolated_settings_for(provider)
        if settings:
            cfg["claude_settings_file"] = settings
            cfg["claude_setting_sources"] = []
            cfg.setdefault("claude_home", str(self._isolated_home_dir))
            if not any(k in cfg for k in (
                "persist_session",
                "resume_session",
                "cli_persist_session",
                "claude_persist_session",
                "codex_persist_session",
            )):
                cfg["claude_persist_session"] = True
        return cfg

    # ---- options 构造 ----
    def _base_kwargs(self, mode: str | None = None, *, allow_bypass: bool = False,
                     provider: str | None = None, model: str | None = None) -> dict:
        c = self.cfg["claude"]
        eff_mode = mode or self.default_mode
        # 路由层有 provider 时：接管端点，隔离 settings env（否则被 figure 覆盖）。
        # 路由层空/无时：保持原行为，正常读 settings。
        routing_active = self.router is not None and bool(self.router.providers)
        kw: dict = dict(
            cwd=c.get("cwd"),
            permission_mode=eff_mode,
            setting_sources=[] if routing_active else ["user", "project", "local"],
        )
        if routing_active:
            settings = self._isolated_settings_for(provider)
            if settings:
                kw["settings"] = settings
        if allow_bypass or eff_mode == "bypassPermissions":
            kw["extra_args"] = {"dangerously-skip-permissions": None}
        # 路由接管时：env = 保留的功能开关 + 选定 provider 的端点项
        if routing_active:
            kw["env"] = {**self.feature_env, **self.router.env_for(provider)}
        else:
            kw["env"] = {}
        # fast mode：CLI 默认开启，关闭时注入 disable env（重连生效）
        if not self.fast_mode:
            kw["env"]["CLAUDE_CODE_DISABLE_FAST_MODE"] = "1"
        if not kw["env"]:
            kw.pop("env")
        if c.get("model"):
            kw["model"] = c["model"]
        # 会话级模型档位优先于 config 默认（重连/重启后经 options.model 复原）。
        # CLI 认档位别名（opus/sonnet/haiku）与具体模型串，直接透传。
        if model:
            kw["model"] = model
        if c.get("effort"):
            kw["effort"] = c["effort"]
        # Claude thinking/reasoning toggle (默认开启, 16000 token budget)
        if getattr(self, "thinking_enabled", True):
            kw["thinking"] = {"type": "enabled", "budget_tokens": 16000}
        if c.get("add_dirs"):
            kw["add_dirs"] = c["add_dirs"]
        return kw

    def _hooks_for(self, w: WorkerSession) -> dict | None:
        """为该 worker 造共享记忆 hook（PreToolUse 预警 + PostToolUse 记录）。"""
        if self.shared_mem is None:
            return None
        from phantom_llm.sharedmem import build_hooks
        return build_hooks(self.shared_mem, w.name, turn_getter=lambda: w.turns)

    def _worker_options(self, w: WorkerSession) -> ClaudeAgentOptions:
        kw = self._base_kwargs(w.mode, provider=w.provider, model=w.model)
        # 会话级 cwd 覆盖（接管外部 Claude Code 会话时指向其原始项目目录）
        if w.cwd:
            kw["cwd"] = w.cwd
        kw["can_use_tool"] = w._can_use_tool
        if (h := self._hooks_for(w)):
            kw["hooks"] = h
        # worker 能发文件（file），并能与同伴 worker 对等通信（peer）。两者经 tool search 发现。
        from phantom_llm.tools.file import build_file_server
        from phantom_llm.tools.peer import build_peer_server
        kw["mcp_servers"] = {"file": build_file_server(self),
                             "peer": build_peer_server(self, w.name)}
        # Claude Code 原生 subagent：让 worker 能在自己上下文内 fan-out 临时子代理（Task）。
        # 与「班组同伴 worker」语义不同——见 WORKER_TEAMING_PROMPT。
        # 不同 worker 可挂不同 subagent 集合：MAGI 挂三脑，其余默认。
        if w.agents_set == "magi":
            kw["agents"] = dict(MAGI_AGENTS)
        else:
            kw["agents"] = dict(DEFAULT_SUBAGENTS)
        # 系统提示：班组协作说明 + 专职角色（如 relay）追加。两段都挂在 claude_code 预设上。
        append = WORKER_TEAMING_PROMPT
        if w.system_append:
            append = w.system_append + "\n\n" + WORKER_TEAMING_PROMPT
        kw["system_prompt"] = {"type": "preset", "preset": "claude_code", "append": append}
        # 进程重启/重连后恢复对话：带上 resume，用一次即清
        if w.resume_session_id:
            kw["resume"] = w.resume_session_id
            w.resume_session_id = None
        return ClaudeAgentOptions(**kw)

    def _orchestrator_options(self, w: WorkerSession) -> ClaudeAgentOptions:
        from phantom_llm.tools.orchestrator import build_orchestrator_server, ORCH_SYSTEM_PROMPT
        # Bot 内嵌模式允许主对话带 --dangerously-skip-permissions 启动，
        # 因此可在运行时 /mode bypassPermissions 切到全自动免审批。
        # 独立 daemon 可通过 cfg["_allow_orchestrator_bypass"] 显式关闭该能力。
        # orchestrator 用全局活跃 provider（w.provider，默认 None=活跃）。
        allow_bypass = bool(self.cfg.get("_allow_orchestrator_bypass", True))
        kw = self._base_kwargs(allow_bypass=allow_bypass, provider=w.provider, model=w.model)
        kw["can_use_tool"] = w._can_use_tool
        if (h := self._hooks_for(w)):
            kw["hooks"] = h
        if w.resume_session_id:
            kw["resume"] = w.resume_session_id
            w.resume_session_id = None
        server = build_orchestrator_server(self)
        from phantom_llm.tools.file import build_file_server
        kw["mcp_servers"] = {"team": server, "file": build_file_server(self)}
        kw["system_prompt"] = {"type": "preset", "preset": "claude_code", "append": ORCH_SYSTEM_PROMPT}
        return ClaudeAgentOptions(**kw)

    # ---- 生命周期 ----
    async def start_orchestrator(self, resume_session_id: str | None = None,
                                 model: str | None = None):
        backend_config = self._backend_config_for(None)
        if not self._persist_session_id(backend_config):
            resume_session_id = None
        w = WorkerSession(
            self.ORCH,
            self._orchestrator_options,
            is_orchestrator=True,
            backend_name=self.active_backend,
            backend_config=backend_config,
        )
        w.permission_cb = self.permission_cb
        w.ask_question_cb = self.ask_question_cb
        w.auto_allow = set(self.auto_allow)
        w.resume_session_id = resume_session_id if self._persist_session_id(backend_config) else None
        w.model = model
        w.on_session_id = self.save_state  # 拿到 session_id 即存盘
        await w.start()
        self.orchestrator = w
        return w

    async def spawn_worker(self, name: str, mode: str | None = None,
                           provider: str | None = None,
                           model: str | None = None,
                           resume_session_id: str | None = None,
                           system_append: str | None = None,
                           agents_set: str | None = None,
                           cwd: str | None = None) -> WorkerSession:
        if name in self.workers or name == self.ORCH:
            raise ValueError(f"worker '{name}' already exists")
        if provider and self.router and not self.router.get(provider):
            raise ValueError(f"unknown provider '{provider}'")
        # 统一走 _worker_options：集中 hooks / resume / mode / provider，
        # factory 每次读 w 的最新字段，故重连/换 provider 后用最新配置。
        backend_config = self._backend_config_for(provider)
        # 显式 resume 必须优先保留。接管本地 Claude Code/Codex 会话时，
        # 语义是继续既有 transcript/context；不能因全局 persist 配置缺省而丢掉 session_id。
        if resume_session_id:
            if normalize_backend(self.active_backend) == "claude-code":
                backend_config["claude_persist_session"] = True
            elif normalize_backend(self.active_backend) == "codex":
                backend_config["codex_persist_session"] = True
        w = WorkerSession(
            name,
            self._worker_options,
            backend_name=self.active_backend,
            backend_config=backend_config,
        )
        w.permission_cb = self.permission_cb
        w.ask_question_cb = self.ask_question_cb
        w.auto_allow = set(self.auto_allow)
        w.provider = provider
        w.mode = mode
        w.model = model
        w.system_append = system_append
        w.agents_set = agents_set
        explicit_resume = bool(resume_session_id and self._persist_session_id(backend_config))
        w.resume_session_id = resume_session_id if explicit_resume else None
        if explicit_resume:
            w.session_id = resume_session_id
            w.strict_session_id = True
        w.cwd = cwd
        w.on_session_id = self.save_state  # 拿到 session_id 即存盘
        await w.start()
        self.workers[name] = w
        self.save_state()
        return w

    async def stop_worker(self, name: str) -> bool:
        w = self.workers.pop(name, None)
        if not w:
            return False
        await w.stop()
        self.save_state()
        return True

    def get(self, name: str) -> WorkerSession | None:
        if name == self.ORCH:
            return self.orchestrator
        return self.workers.get(name)

    def list_workers(self) -> list[dict]:
        out = []
        for w in self.workers.values():
            if self._is_internal_worker_name(w.name):
                continue
            out.append({
                "name": w.name,
                "busy": w.busy,
                "turns": w.turns,
                "session_id": (w.session_id or "")[:8],
                "idle_s": round(time.time() - w.last_active, 1),
                "last_output": w.last_output[-400:],
                "provider": w.provider or (self.router.active if self.router else None),
                "backend": w.backend_name,
            })
        return out

    # ---- worker 对等消息（peer messaging，非阻塞）----
    def _peer_sink(self, worker_name: str) -> TurnSink:
        """投递对等消息时驱动 worker 的 sink：把输出镜像到该 worker 的 TG 话题。"""
        async def on_start():
            if self.notify_cb:
                await self.notify_cb(worker_name, "start", "")
        async def on_text(t: str):
            if self.notify_cb:
                await self.notify_cb(worker_name, "text", t)
        async def on_event(t):
            if self.notify_cb:
                await self.notify_cb(worker_name, "event", t)
        async def on_done(msg):
            # 轮结束：通知 bot 层定格镜像气泡（带统计），否则心跳永不收尾
            if self.notify_cb:
                await self.notify_cb(worker_name, "done", msg)
        return TurnSink(on_text=on_text, on_event=on_event,
                        on_start=on_start, on_done=on_done)

    def _fmt_peer_msg(self, sender: str, message: str) -> str:
        return (f"📨 来自同伴 worker «{sender}» 的消息：\n{message}\n\n"
                f"（如需回复，用 message_worker(name=\"{sender}\", message=...) 发回。"
                f"这是班组内对等通信，不是主人发的指令。）")

    async def post_peer_message(self, sender: str, target: str, message: str) -> str:
        """把一条消息投进 target 的收件箱并尽快派发。非阻塞：发信方不等回执。
        target 空闲→立即起一轮（后台）；忙→入收件箱，待其 turn 结束抽干。

        target == 'main' / ORCH 走特殊路径：不直接 run orchestrator（它由 BotApp 驱动），
        改由 notify_main_cb 把消息排成主对话的下一轮（或忙时入队）。"""
        if target == self.ORCH or target.lower() == "main":
            if sender == self.ORCH or sender.lower() == "main":
                return "error: 不能给自己发消息"
            if self.notify_main_cb is None:
                return f"error: 主对话回送通道未就绪"
            try:
                msg = await self.notify_main_cb(sender, message)
                return msg or f"已投递给主对话（来自 {sender}）"
            except Exception as e:
                log.warning("notify_main_cb failed: %s", e)
                return f"error: 主对话投递失败: {e}"
        w = self.get(target)
        if w is None or w.is_orchestrator:
            return f"error: 没有同伴 worker '{target}'"
        if target == sender:
            return "error: 不能给自己发消息"
        self.peer_inbox.setdefault(target, []).append((sender, message))
        if not w.busy:
            asyncio.create_task(self._deliver_peer_inbox(target))
            return f"已投递给 «{target}»（空闲，立即处理）"
        return f"已投递给 «{target}»（忙，排在其当前 turn 之后）"

    async def _deliver_peer_inbox(self, target: str):
        """抽干 target 的对等收件箱：把暂存消息合并成一轮发给它，输出镜像到其话题。
        串行：一次只处理一轮，跑完再看是否又有新消息。"""
        w = self.get(target)
        if w is None or w.is_orchestrator or w.busy:
            return
        box = self.peer_inbox.get(target)
        if not box:
            return
        msgs = box[:]
        self.peer_inbox[target] = []
        if len(msgs) == 1:
            prompt = self._fmt_peer_msg(msgs[0][0], msgs[0][1])
        else:
            joined = "\n\n".join(self._fmt_peer_msg(s, m) for s, m in msgs)
            prompt = f"你收到 {len(msgs)} 条同伴消息：\n\n{joined}"
        try:
            await w.run(prompt, self._peer_sink(target))
        except Exception as e:
            log.warning("deliver peer inbox to '%s' failed: %s", target, e)
        finally:
            self.save_state()
            # 处理期间若又攒了新消息，继续抽
            if self.peer_inbox.get(target):
                asyncio.create_task(self._deliver_peer_inbox(target))

    async def drain_peer_inbox(self, target: str):
        """供 bot 在某 worker 的一轮结束后调用：若其收件箱有货，接着派发。"""
        if self.peer_inbox.get(target):
            await self._deliver_peer_inbox(target)

    def list_peers(self, exclude: str | None = None) -> list[dict]:
        """列出可作为对等通信对象的 worker（排除自己、排除 orchestrator）。"""
        out = []
        for w in self.workers.values():
            if exclude and w.name == exclude:
                continue
            out.append({"name": w.name, "busy": w.busy,
                        "idle_s": round(time.time() - w.last_active, 1)})
        return out

    # ---- 模型档位控制 ----
    def backend_state(self) -> dict:
        return backend_info(self.active_backend, self.backend_config)

    async def set_backend(self, name: str) -> str:
        normalized = normalize_backend(name)
        if normalized not in ("claude-code", "codex"):
            return f"未知后端 '{name}'；可选: claude-code, codex"
        if normalized == self.active_backend:
            return f"CLI backend 已是 {normalized}"
        self.active_backend = normalized
        self.backend_config["active"] = normalized
        sessions = [self.orchestrator] if self.orchestrator else []
        sessions.extend(self.workers.values())
        for w in sessions:
            if w is None:
                continue
            try:
                await w.stop()
            except Exception:
                pass
            w.backend_name = normalized
            w.backend_config = self._backend_config_for(w.provider)
            self._drop_transient_session_id(w)
            await w.start()
        self.save_state()
        return f"CLI backend → {normalized}"

    async def set_session_model(self, name: str, model: str | None) -> str:
        """切某会话（worker 或 main）的模型档位。SDK set_model 连接态即时生效，
        无需重连、不丢上下文。model=None 回该会话默认。持久化以便重启复原。"""
        w = self.get(name)
        if w is None:
            return f"没有会话 '{name}'"
        try:
            await w.set_model(model)
        except Exception as e:
            log.warning("set_model failed (%s): %s", name, e)
            return f"切模型失败: {e}"
        self.save_state()
        who = "主对话" if w.is_orchestrator else f"worker «{name}»"
        return f"{who} 模型 → {model or '默认'}（即时生效，上下文保留）"

    # ---- provider 路由控制 ----
    async def set_worker_provider(self, name: str, provider: str) -> str:
        """切某 worker 的 provider。env 在进程启动时定，故需重连。
        带 session_id resume 重连 → 保留该 worker 的对话上下文。"""
        if self.router is None:
            return "未启用 provider 路由（无 providers.toml）"
        if not self.router.get(provider):
            return f"未知 provider '{provider}'"
        w = self.get(name)
        if w is None:
            return f"没有 worker '{name}'"
        if w.is_orchestrator:
            return await self.set_active_provider(provider)
        w.provider = provider
        w.backend_config = self._backend_config_for(provider)
        # 重连使新 env 生效；带 resume 续上下文（worker 的 session_id 已即时落盘）
        w.resume_session_id = w.session_id if self._persist_session_id(w.backend_config) else None
        self._drop_transient_session_id(w)
        if w.client:
            try:
                await w.stop()
            except Exception:
                pass
        await w.start()
        self.save_state()  # 持久化新 provider，重启仍按此
        return f"worker '{name}' → provider '{provider}'（已重连，上下文保留）"

    async def set_active_provider(self, provider: str) -> str:
        """切全局活跃 provider。重连 orchestrator 使其立即生效；已有 worker 不动。"""
        if self.router is None:
            return "未启用 provider 路由（无 providers.toml）"
        if not self.router.set_active(provider):
            return f"未知 provider '{provider}'"
        self.save_state()  # 持久化活跃端点：重启后仍用这个 provider
        if self.orchestrator:
            try:
                await self.orchestrator.stop()
            except Exception:
                pass
            self.orchestrator.backend_config = self._backend_config_for(None)
            self._drop_transient_session_id(self.orchestrator)
            await self.orchestrator.start()
        return f"全局活跃 provider → '{provider}'（主对话已重连；新建 worker 默认沿用）"

    async def reload_providers_from_file(self) -> str:
        """从磁盘重读 providers.toml 并 reconnect orchestrator（无需 systemd 重启）。"""
        if self.router is None or not self.router.path:
            return "未启用 provider 路由（无 providers.toml）"
        from phantom_llm.router import Router

        path = self.router.path
        try:
            new_router = Router.from_file(path)
        except Exception as exc:
            return f"重载失败：{exc}"
        if new_router is None:
            return "重载失败：无法读取 providers.toml"
        old_active = self.router.active
        self.router = new_router
        if old_active and self.router.get(old_active):
            self.router.set_active(old_active)
        if self.orchestrator:
            try:
                await self.orchestrator.stop()
            except Exception:
                pass
            self.orchestrator.backend_config = self._backend_config_for(None)
            self._drop_transient_session_id(self.orchestrator)
            await self.orchestrator.start()
        return f"已重载 {len(self.router.providers)} 个 provider，活跃 → '{self.router.active}'"

    def add_provider_spec(self, *, name, base_url, auth_token="", priority=100,
                          make_default=False, models=None, timeout=None) -> str:
        """TG 配置入口：新增/覆盖一个 provider 并持久化到 providers.toml。"""
        if self.router is None:
            return "未启用 provider 路由"
        from phantom_llm.router import Provider
        p = Provider(name=name, base_url=base_url, auth_token=auth_token or "",
                     priority=int(priority), models=models or {}, timeout=timeout)
        self.router.add_provider(p, make_default=make_default)
        extra = "，已设为默认" if make_default else ""
        return f"已保存 provider '{name}'{extra}。切到它请用 /providers {name} 或 set_active_provider。"

    def remove_provider_spec(self, name: str) -> str:
        if self.router is None:
            return "未启用 provider 路由"
        ok = self.router.remove_provider(name)
        return f"已删除 provider '{name}'" if ok else f"没有 provider '{name}'"

    async def set_fast_mode(self, on: bool) -> str:
        """切 fast mode。靠 env 控制，须重连会话才生效（重连 orchestrator）。"""
        self.fast_mode = bool(on)
        self.save_state()  # 持久化：重启后仍按此设置
        if self.orchestrator:
            try:
                await self.orchestrator.stop()
            except Exception:
                pass
            self._drop_transient_session_id(self.orchestrator)
            await self.orchestrator.start()
        state = "开启 ⚡" if self.fast_mode else "关闭"
        return f"fast mode → {state}（主对话已重连生效；新建 worker 默认沿用）"

    async def shutdown(self):
        for w in list(self.workers.values()):
            await w.stop()
        if self.orchestrator:
            await self.orchestrator.stop()
