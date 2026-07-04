"""
router.py — LLM provider 路由层。

底层是 Claude Code CLI（只说 Anthropic 协议），所以路由 = 给每个 session 注入不同的
ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / 模型名 env。本模块只负责"选哪个 provider
+ 给出对应 env 字典 + fallback 链"，不碰协议本身。

- 全局默认 provider（providers.toml 里 default=true 的那个，或 priority 最小的）。
- 每个 worker 可覆盖（spawn 时带 provider=，或运行时 set）。
- fallback：按 priority 升序的候选链；调用方在连接/请求失败时依次尝试下一个。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

log = logging.getLogger("tgclaude.router")


@dataclass
class Provider:
    name: str
    base_url: str
    auth_token: str = ""
    auth_token_env: str = ""
    auth_token_file: str = ""
    auth_token_private: bool = False
    priority: int = 100
    default: bool = False
    models: dict = field(default_factory=dict)
    timeout: int | None = None
    # 该端点要附加的自定义请求头（如 {"anthropic-beta": "context-1m-2025-08-07"}）。
    # 走 CLI 的 ANTHROPIC_CUSTOM_HEADERS（每行 "Name: value"）。注意：实测该变量
    # 对 anthropic-beta 不可靠（会被 CLI 自带 beta 列表覆盖）；开启 1M 上下文请用
    # 下面的 betas 字段（→ ANTHROPIC_BETAS，CLI 会正确并入 anthropic-beta 头）。
    headers: dict = field(default_factory=dict)
    # 该端点要追加的 anthropic-beta 标识列表（如 ["context-1m-2025-08-07"]）。
    # 不同代理"开启 1M 上下文"约定不同：有的用模型名后缀 [1M]，AnyRouter 要 beta。
    betas: list = field(default_factory=list)

    # 兜底 socket 超时（秒）：provider 没显式配 timeout 时用。代理端半挂/慢响应
    # 不会无上限挂死 worker turn —— SDK CLI 的 ANTHROPIC_REQUEST_TIMEOUT 控这个。
    DEFAULT_TIMEOUT = 120

    def env(self) -> dict[str, str]:
        """该 provider 对应的 ANTHROPIC_* env 注入字典。"""
        e: dict[str, str] = {"ANTHROPIC_BASE_URL": self.base_url, "PHANTOM_PROVIDER": self.name}
        if self.auth_token:
            e["ANTHROPIC_AUTH_TOKEN"] = self.auth_token
            e["ANTHROPIC_API_KEY"] = self.auth_token  # 两个都给，CLI 认任一
        m = self.models or {}
        if m.get("opus"):
            e["ANTHROPIC_DEFAULT_OPUS_MODEL"] = m["opus"]
        if m.get("sonnet"):
            e["ANTHROPIC_DEFAULT_SONNET_MODEL"] = m["sonnet"]
        if m.get("haiku"):
            e["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = m["haiku"]
        e["ANTHROPIC_REQUEST_TIMEOUT"] = str(self.timeout or self.DEFAULT_TIMEOUT)
        if self.headers:
            # CLI 读 ANTHROPIC_CUSTOM_HEADERS：多个头用换行分隔的 "Name: value"
            e["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(
                f"{k}: {v}" for k, v in self.headers.items())
        if self.betas:
            # CLI 把 ANTHROPIC_BETAS（逗号分隔）并入 anthropic-beta 请求头
            e["ANTHROPIC_BETAS"] = ",".join(self.betas)
        return e


class Router:
    def __init__(self, providers: list[Provider], path: str | None = None):
        self.path = path
        self._rebuild(providers)

    def _rebuild(self, providers: list[Provider]):
        # 按优先级排序（fallback 顺序）
        self.providers = sorted(providers, key=lambda p: p.priority)
        self.by_name = {p.name: p for p in self.providers}
        if not self.providers:
            self.active = None
            return
        # 全局活跃 provider：保持原 active（若仍在），否则 default=true，否则 priority 最小
        cur = getattr(self, "active", None)
        if cur and cur in self.by_name:
            self.active = cur
        else:
            d = next((p for p in self.providers if p.default), self.providers[0])
            self.active = d.name

    @classmethod
    def from_file(cls, path: str) -> "Router | None":
        if not os.path.exists(path):
            # 文件不存在也返回一个空 router（带 path），以便 TG 里现配现存
            return cls([], path=path)
        with open(path, "rb") as f:
            data = tomllib.load(f)
        secrets = _load_private_secrets(path)
        provs = []
        for row in data.get("provider", []):
            token, token_source = _row_token(row, secrets)
            provs.append(Provider(
                name=row["name"],
                base_url=row["base_url"],
                auth_token=token,
                auth_token_env=row.get("auth_token_env", "") or "",
                auth_token_file=row.get("auth_token_file", "") or "",
                auth_token_private=token_source == "private",
                priority=int(row.get("priority", 100)),
                default=bool(row.get("default", False)),
                models=row.get("models", {}) or {},
                timeout=row.get("timeout"),
                headers=row.get("headers", {}) or {},
                betas=row.get("betas", []) or [],
            ))
        return cls(provs, path=path)

    def get(self, name: str) -> Provider | None:
        return self.by_name.get(name)

    def env_for(self, name: str | None) -> dict[str, str]:
        """给定 provider 名（None=用全局活跃）的 env 注入字典；未知名回退活跃。空表返回 {}。"""
        if not self.providers:
            return {}
        p = self.by_name.get(name or self.active) or self.by_name.get(self.active)
        return p.env() if p else {}

    def set_active(self, name: str) -> bool:
        if name not in self.by_name:
            return False
        self.active = name
        return True

    def fallback_chain(self, start: str | None = None) -> list[str]:
        """从 start（默认全局活跃）开始，按优先级排出 fallback 候选名列表。"""
        start = start or self.active
        names = [p.name for p in self.providers]
        if start in names:  # 把 start 放最前，其余按 priority 顺延
            rest = [n for n in names if n != start]
            return [start] + rest
        return names

    # ---- 运行时增删改 + 持久化（TG 配置用）----
    def add_provider(self, p: Provider, make_default: bool = False):
        """新增或覆盖同名 provider。make_default=True 时清掉别人的 default 并设它为活跃。"""
        if make_default:
            for q in self.providers:
                q.default = False
            p.default = True
        provs = [q for q in self.providers if q.name != p.name] + [p]
        self._rebuild(provs)
        if make_default:
            self.active = p.name
        self.save()

    def remove_provider(self, name: str) -> bool:
        if name not in self.by_name:
            return False
        removed_default = self.by_name[name].default
        provs = [q for q in self.providers if q.name != name]
        # 若删的是 active，_rebuild 会重新挑一个
        if self.active == name:
            self.active = None
        self._rebuild(provs)
        # 若删的是 default 且还有剩余 provider，把新 active 提为 default，避免文件里 default 孤儿
        if removed_default and self.providers and self.active:
            for q in self.providers:
                q.default = (q.name == self.active)
        self.save()
        return True

    def save(self):
        """写回 providers.toml（原子写：临时文件 + rename，权限 600）。"""
        if not self.path:
            return
        import tomli_w
        rows = []
        for p in self.providers:
            row = {"name": p.name, "base_url": p.base_url,
                   "priority": p.priority, "default": p.default}
            if p.auth_token:
                if p.auth_token_private:
                    pass
                elif p.auth_token_env:
                    row["auth_token_env"] = p.auth_token_env
                elif p.auth_token_file:
                    row["auth_token_file"] = p.auth_token_file
                else:
                    row["auth_token"] = p.auth_token
            elif p.auth_token_env:
                row["auth_token_env"] = p.auth_token_env
            elif p.auth_token_file:
                row["auth_token_file"] = p.auth_token_file
            if p.timeout:
                row["timeout"] = p.timeout
            if p.models:
                row["models"] = p.models
            if p.headers:
                row["headers"] = p.headers
            if p.betas:
                row["betas"] = p.betas
            rows.append(row)
        data = {"provider": rows}
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(b"# providers.toml -- managed by bot (/provider commands). Edit live via Telegram.\n")
            tomli_w.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def summary(self) -> str:
        if not self.providers:
            return "（暂无 provider，用 /provider add 添加）"
        lines = []
        for p in self.providers:
            mark = "👉" if p.name == self.active else "  "
            tok = "（专用token）" if p.auth_token else "（继承环境token）"
            dflt = " [default]" if p.default else ""
            lines.append(f"{mark} {p.name} · prio={p.priority} · {p.base_url} {tok}{dflt}")
        return "\n".join(lines)


def _private_secret_path(path: str) -> Path | None:
    env_path = os.environ.get("PHANTOM_PROVIDER_SECRETS") or os.environ.get("AUTO_REVIEW_PROVIDER_SECRETS")
    if env_path:
        return Path(env_path).expanduser()
    try:
        p = Path(path).resolve()
        if len(p.parents) >= 3:
            return p.parents[2] / "data" / "auto-review" / "provider-secrets.json"
    except OSError:
        return None
    return None


def _load_private_secrets(path: str) -> dict:
    secret_path = _private_secret_path(path)
    if not secret_path or not secret_path.exists():
        return {}
    try:
        data = json.loads(secret_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    providers = data.get("providers") if isinstance(data, dict) else {}
    return providers if isinstance(providers, dict) else {}


def _row_token(row: dict, secrets: dict) -> tuple[str, str]:
    raw = str(row.get("auth_token") or "")
    if raw:
        return raw, "inline"
    env_name = str(row.get("auth_token_env") or "").strip()
    if env_name and os.environ.get(env_name):
        return os.environ.get(env_name, ""), "env"
    file_name = str(row.get("auth_token_file") or "").strip()
    if file_name:
        try:
            value = Path(file_name).expanduser().read_text(encoding="utf-8").strip()
            if value:
                return value, "file"
        except OSError:
            pass
    private = str(secrets.get(row.get("name") or "") or "")
    if private:
        return private, "private"
    return "", ""
