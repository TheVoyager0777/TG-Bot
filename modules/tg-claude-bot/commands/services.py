"""commands_services — manage split phantom submodule services from Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes


VALID_ACTIONS = {"status", "start", "stop", "restart"}
VALID_TARGETS = {"console", "llm", "infiniproxy", "network", "all"}
UNIT_NAMES = {
    "console": "phantom-console.service",
    "llm": "llm-frontend.service",
    "infiniproxy": "phantom-infiniproxy.service",
    "network": "phantom-network.service",
}
LEGACY_UNIT_NAMES = {
    "llm": "phantom-llm.service",
}


def _cfg_path(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    cfg = ctx.application.bot_data.get("cfg") or {}
    return cfg.get("_config_path") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml")


def _module_cmd(target: str, action: str, cfg_path: str) -> list[str]:
    if target == "console":
        cmd = [sys.executable, "-m", "phantom_console.cli", action]
        if action in ("start", "serve"):
            cmd += ["--config", cfg_path]
        return cmd
    if target == "llm":
        cmd = [sys.executable, "-m", "phantom_llm.daemon", action]
        if action in ("start", "serve"):
            cmd += ["--config", cfg_path]
        return cmd
    if target == "infiniproxy":
        return [sys.executable, "-m", "phantom_infiniproxy.cli", action]
    if target == "network":
        cmd = [sys.executable, "-m", "phantom_network.cli", action]
        if action in ("start", "serve"):
            cmd += ["--config", cfg_path]
        return cmd
    raise ValueError(f"unknown target: {target}")


def _version_cmd(target: str) -> list[str]:
    if target == "console":
        return [sys.executable, "-m", "phantom_console.cli", "version"]
    if target == "llm":
        return [sys.executable, "-m", "phantom_llm.daemon", "version"]
    if target == "infiniproxy":
        return [sys.executable, "-m", "phantom_infiniproxy.cli", "version"]
    if target == "network":
        return [sys.executable, "-m", "phantom_network.cli", "version"]
    raise ValueError(f"unknown target: {target}")


async def _run_cmd(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    env = os.environ.copy()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace = os.path.dirname(root)
    repo = os.path.dirname(workspace)
    paths = [
        os.path.join(repo, "LLM_Frontend"),
        os.path.join(repo, "LLM_Backend"),
        os.path.join(workspace, "phantom-console"),
        os.path.join(workspace, "phantom-network"),
        os.path.join(workspace, "infiniproxy"),
        root,
    ]
    env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, "timeout"
    text = out.decode("utf-8", errors="replace").strip()
    return int(proc.returncode or 0), text


async def _unit_load_state(unit: str) -> str:
    rc, out = await _run_cmd(
        ["systemctl", "--user", "show", "-P", "LoadState", unit],
        timeout=5.0,
    )
    return out.strip() if rc == 0 else "not-found"


async def _systemd_unit_for(target: str) -> str | None:
    unit = UNIT_NAMES.get(target)
    if unit and await _unit_load_state(unit) == "loaded":
        return unit
    legacy = LEGACY_UNIT_NAMES.get(target)
    if legacy and await _unit_load_state(legacy) == "loaded":
        return legacy
    return unit if unit else None


async def _systemd_unit_loaded(target: str) -> bool:
    unit = await _systemd_unit_for(target)
    return bool(unit and await _unit_load_state(unit) == "loaded")


async def _systemd_unit_state(target: str) -> str:
    unit = await _systemd_unit_for(target)
    if not unit:
        return "unmanaged"
    if await _unit_load_state(unit) != "loaded":
        return "unloaded"
    rc, out = await _run_cmd(["systemctl", "--user", "is-active", unit], timeout=5.0)
    if rc == 0 and out:
        return out.strip()
    return (out or "inactive").strip()


async def _run_cli_action(target: str, action: str, cfg_path: str) -> str:
    if action == "restart":
        rc1, out1 = await _run_cmd(_module_cmd(target, "stop", cfg_path))
        rc2, out2 = await _run_cmd(_module_cmd(target, "start", cfg_path))
        mark = "OK" if rc2 == 0 else f"FAIL rc={rc2}"
        return f"{target}: {mark}\nstop: {out1 or rc1}\nstart: {out2 or rc2}"
    rc, out = await _run_cmd(_module_cmd(target, action, cfg_path))
    if action == "status":
        # status returns non-zero when stopped; that is not a command failure.
        mark = "OK" if rc in (0, 3) else f"FAIL rc={rc}"
    else:
        mark = "OK" if rc == 0 else f"FAIL rc={rc}"
    return f"{target}: {mark}\n{out or '(no output)'}"


async def _run_action(target: str, action: str, cfg_path: str) -> str:
    if not await _systemd_unit_loaded(target):
        return await _run_cli_action(target, action, cfg_path)

    unit = await _systemd_unit_for(target)
    if not unit:
        return await _run_cli_action(target, action, cfg_path)
    if action == "status":
        unit_state = await _systemd_unit_state(target)
        rc_cli, out_cli = await _run_cmd(_module_cmd(target, "status", cfg_path))
        mark = "OK" if rc_cli in (0, 3) else f"FAIL rc={rc_cli}"
        return f"{target}: {mark}\nsystemd: {unit_state}\ncli: {out_cli or rc_cli}"

    rc, out = await _run_cmd(["systemctl", "--user", action, unit], timeout=30.0)
    rc_cli, out_cli = await _run_cmd(_module_cmd(target, "status", cfg_path))
    if action in ("start", "restart"):
        ok = rc == 0 and rc_cli == 0
    else:
        ok = rc == 0 and rc_cli == 3
    mark = "OK" if ok else f"FAIL rc={rc}, status_rc={rc_cli}"
    return f"{target}: {mark}\nsystemd: {out or action}\nstatus: {out_cli or rc_cli}"


async def _module_status_block(target: str, cfg_path: str) -> str:
    rc_status, out_status = await _run_cmd(_module_cmd(target, "status", cfg_path))
    rc_ver, out_ver = await _run_cmd(_version_cmd(target))
    unit = await _systemd_unit_for(target)
    unit_state = await _systemd_unit_state(target)
    unit_label = f"{unit_state} ({unit})" if unit else unit_state
    running = "running" if rc_status == 0 else "stopped" if rc_status == 3 else f"unknown(rc={rc_status})"
    raw_status = (out_status or "").replace("`", "'")[:160]
    version_line = out_ver if rc_ver == 0 else "unknown"
    extra = ""
    if target == "llm":
        extra = await _llm_backend_line()
    return (
        f"*{target}*\n"
        f"  state: `{running}`\n"
        f"  systemd: `{unit_label}`\n"
        f"  version: `{version_line}`\n"
        f"{extra}"
        f"  raw: `{raw_status}`"
    )


async def _llm_backend_line() -> str:
    def _read() -> str:
        try:
            with urlopen("http://127.0.0.1:8799/backend", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except (OSError, URLError, json.JSONDecodeError):
            return ""
        active = data.get("active")
        version = (data.get("version") or {}).get("version")
        if not active:
            return ""
        if version:
            return f"  backend: `{active}` (`LLM_Backend v{version}`)\n"
        return f"  backend: `{active}`\n"

    return await asyncio.to_thread(_read)


async def cmd_svc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/svc status|start|stop|restart [console|llm|infiniproxy|all]."""
    args = ctx.args or []
    if not args or args[0] not in VALID_ACTIONS:
        await update.message.reply_text(
            "用法: `/svc status|start|stop|restart [console|llm|infiniproxy|network|all]`",
            parse_mode=ParseMode.MARKDOWN)
        return
    action = args[0]
    target = args[1] if len(args) > 1 else "all"
    if target not in VALID_TARGETS:
        await update.message.reply_text("target 须为 console|llm|infiniproxy|network|all")
        return
    targets = ["network", "console", "llm", "infiniproxy"] if target == "all" else [target]
    note = await update.message.reply_text(f"执行子模块服务命令: `{action} {target}`",
                                           parse_mode=ParseMode.MARKDOWN)
    cfg_path = _cfg_path(ctx)
    parts = []
    for item in targets:
        parts.append(await _run_action(item, action, cfg_path))
    text = "```\n" + "\n\n".join(parts)[-3800:] + "\n```"
    try:
        await note.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


SERVICE_COMMANDS = {
    "svc": cmd_svc,
}


async def build_services_status_text(cfg_path: str) -> str:
    from infra import version as bot_version

    snap = bot_version.snapshot()
    lines = [
        "*Phantom services*",
        "",
        "*bot*",
        f"  state: `running`",
        f"  version: `tg-claude-bot v{snap['version']} · {snap['description']} · {snap['build_time']} · {snap['fingerprint']}`",
    ]
    for target in ("network", "console", "llm", "infiniproxy"):
        lines.append("")
        lines.append(await _module_status_block(target, cfg_path))
    return "\n".join(lines)


async def cmd_svcstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/svcstatus — show bot and submodule service status/version."""
    cfg_path = _cfg_path(ctx)
    await update.message.reply_text(
        await build_services_status_text(cfg_path),
        parse_mode=ParseMode.MARKDOWN,
    )


SERVICE_COMMANDS["svcstatus"] = cmd_svcstatus
