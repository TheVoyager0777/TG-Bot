"""Command-line lifecycle for phantom-console."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from .server import Console
from . import version


DEFAULT_PIDFILE = Path.home() / ".config" / "phantom-console" / "phantom-console.pid"


def _load_toml(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _cfg_value(cfg: dict, section: str, key: str, default=None):
    return (cfg.get(section) or {}).get(key, default)


def _settings(args) -> dict:
    cfg = _load_toml(args.config)
    return {
        "bot_token": args.bot_token
        or os.environ.get("PHANTOM_CONSOLE_BOT_TOKEN")
        or _cfg_value(cfg, "telegram", "token", ""),
        "owner_id": int(
            args.owner_id
            or os.environ.get("PHANTOM_CONSOLE_OWNER_ID")
            or _cfg_value(cfg, "telegram", "owner_id", 0)
            or 0
        ),
        "port": int(
            args.port
            or os.environ.get("PHANTOM_CONSOLE_PORT")
            or _cfg_value(cfg, "webapp", "port", 8765)
        ),
        "static_port": int(
            args.static_port
            or os.environ.get("PHANTOM_CONSOLE_STATIC_PORT")
            or _cfg_value(cfg, "webapp", "static_port", 8766)
        ),
        "webctl": {
            "enabled": bool(_cfg_value(cfg, "webctl", "enabled", True)),
            "autostart": bool(_cfg_value(cfg, "webctl", "autostart", True)),
            "path": args.webctl_path
            or os.environ.get("PHANTOM_CONSOLE_WEBCTL_PATH")
            or _cfg_value(cfg, "webctl", "path",
                          "/home/voyager/桌面/Workspace/Platform_Phantom/WebCTL"),
            "host": args.webctl_host
            or os.environ.get("PHANTOM_CONSOLE_WEBCTL_HOST")
            or _cfg_value(cfg, "webctl", "host", "127.0.0.1"),
            "port": int(
                args.webctl_port
                or os.environ.get("PHANTOM_CONSOLE_WEBCTL_PORT")
                or _cfg_value(cfg, "webctl", "port", 8080)
            ),
            "url": args.webctl_url
            or os.environ.get("WEBCTL_URL")
            or _cfg_value(cfg, "webctl", "url", ""),
        },
    }


def _pidfile(args) -> Path:
    return Path(args.pidfile).expanduser() if args.pidfile else DEFAULT_PIDFILE


def _read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().strip())
    except Exception:
        return None


def _is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_pidfile_for_current_process(pidfile: Path) -> bool:
    pid = _read_pid(pidfile)
    current = os.getpid()
    if _is_running(pid) and pid != current:
        print(f"phantom-console already running pid={pid}", file=sys.stderr)
        return False
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(current))
    return True


def _remove_pidfile_for_current_process(pidfile: Path) -> None:
    if _read_pid(pidfile) == os.getpid():
        try:
            pidfile.unlink()
        except OSError:
            pass


async def _serve_async(args) -> int:
    st = _settings(args)
    if not st["bot_token"] or not st["owner_id"]:
        print("missing bot token or owner id; pass --config, --bot-token/--owner-id, or env", file=sys.stderr)
        return 2
    console = Console(
        st["bot_token"],
        st["owner_id"],
        port=st["port"],
        static_port=st["static_port"],
        webctl=st["webctl"],
    )
    url = await console.start()
    print(f"phantom-console serving: {url} (api=http://127.0.0.1:{st['port']})", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        await console.stop()
    return 0


def cmd_serve(args) -> int:
    pidfile = _pidfile(args)
    if not _write_pidfile_for_current_process(pidfile):
        return 1
    try:
        return asyncio.run(_serve_async(args))
    finally:
        _remove_pidfile_for_current_process(pidfile)


def cmd_start(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    if _is_running(pid):
        print(f"phantom-console already running pid={pid}")
        return 0
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "phantom_console.cli", "serve"]
    if args.config:
        cmd += ["--config", args.config]
    if args.bot_token:
        cmd += ["--bot-token", args.bot_token]
    if args.owner_id:
        cmd += ["--owner-id", str(args.owner_id)]
    if args.port:
        cmd += ["--port", str(args.port)]
    if args.static_port:
        cmd += ["--static-port", str(args.static_port)]
    if args.webctl_path:
        cmd += ["--webctl-path", args.webctl_path]
    if args.webctl_host:
        cmd += ["--webctl-host", args.webctl_host]
    if args.webctl_port:
        cmd += ["--webctl-port", str(args.webctl_port)]
    if args.webctl_url:
        cmd += ["--webctl-url", args.webctl_url]
    log_path = str(pidfile.with_suffix(".log"))
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pidfile.write_text(str(proc.pid))
    print(f"phantom-console started pid={proc.pid} log={log_path}")
    return 0


def cmd_stop(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    if not _is_running(pid):
        print("phantom-console not running")
        try:
            pidfile.unlink()
        except OSError:
            pass
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _is_running(pid):
            break
        time.sleep(0.2)
    if _is_running(pid):
        os.kill(pid, signal.SIGKILL)
    try:
        pidfile.unlink()
    except OSError:
        pass
    print(f"phantom-console stopped pid={pid}")
    return 0


def cmd_status(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    running = _is_running(pid)
    print(f"phantom-console {'running' if running else 'stopped'}" + (f" pid={pid}" if pid else ""))
    return 0 if running else 3


def cmd_version(_args) -> int:
    print(version.line())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phantom-console")
    parser.add_argument("--pidfile", default=str(DEFAULT_PIDFILE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--config", help="TOML config with [telegram] and optional [webapp]")
        p.add_argument("--bot-token", help="Telegram bot token used for key/initData validation")
        p.add_argument("--owner-id", type=int, help="Telegram owner id")
        p.add_argument("--port", type=int, help="API port")
        p.add_argument("--static-port", type=int, help="static/PWA port")
        p.add_argument("--webctl-path", help="Path to Platform_Phantom/WebCTL")
        p.add_argument("--webctl-host", help="WebCTL host")
        p.add_argument("--webctl-port", type=int, help="WebCTL port")
        p.add_argument("--webctl-url", help="WebCTL base URL")

    serve = sub.add_parser("serve", help="run foreground server")
    add_common(serve)
    serve.set_defaults(func=cmd_serve)

    start = sub.add_parser("start", help="start background server")
    add_common(start)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop background server")
    stop.add_argument("--timeout", type=float, default=5.0)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="show background server status")
    status.set_defaults(func=cmd_status)

    ver = sub.add_parser("version", help="show module version")
    ver.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
