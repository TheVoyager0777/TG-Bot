"""Command-line lifecycle for phantom-infiniproxy."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from . import version


MODULE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PIDFILE = Path.home() / ".config" / "phantom-infiniproxy" / "phantom-infiniproxy.pid"
DEFAULT_ENV_FILE = MODULE_DIR / ".env"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010


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
        print(f"phantom-infiniproxy already running pid={pid}", file=sys.stderr)
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


def _env(args) -> dict:
    env = os.environ.copy()
    env.setdefault("PROXY_HOST", args.host or DEFAULT_HOST)
    env.setdefault("PROXY_PORT", str(args.port or DEFAULT_PORT))
    if args.env_file:
        env["PHANTOM_INFINIPROXY_ENV_FILE"] = args.env_file
    return env


def _host_port(args) -> tuple[str, int]:
    return (args.host or os.environ.get("PROXY_HOST") or DEFAULT_HOST,
            int(args.port or os.environ.get("PROXY_PORT") or DEFAULT_PORT))


def _health_url(args) -> str:
    host, port = _host_port(args)
    return f"http://{host}:{port}/health"


def _healthy(args, timeout: float = 2.0) -> bool:
    try:
        with urlopen(_health_url(args), timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (OSError, URLError):
        return False


def cmd_serve(args) -> int:
    pidfile = _pidfile(args)
    if not _write_pidfile_for_current_process(pidfile):
        return 1
    try:
        os.chdir(MODULE_DIR)
        env_file = Path(args.env_file).expanduser() if args.env_file else DEFAULT_ENV_FILE
        if env_file.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env_file, override=False)
            except Exception as exc:
                print(f"warning: failed to load env file {env_file}: {exc}", file=sys.stderr)
        os.environ.setdefault("PROXY_HOST", args.host or DEFAULT_HOST)
        os.environ.setdefault("PROXY_PORT", str(args.port or DEFAULT_PORT))
        from proxy_server import app
        from config import ProxyConfig
        import uvicorn

        cfg = ProxyConfig.from_env()
        print(
            f"phantom-infiniproxy serving: http://{cfg.proxy_host}:{cfg.proxy_port}",
            flush=True,
        )
        uvicorn.run(app, host=cfg.proxy_host, port=cfg.proxy_port, log_level="info")
        return 0
    finally:
        _remove_pidfile_for_current_process(pidfile)


def cmd_start(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    if _is_running(pid):
        print(f"phantom-infiniproxy already running pid={pid}")
        return 0
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "phantom_infiniproxy.cli",
        "--pidfile",
        str(pidfile),
        "serve",
    ]
    if args.env_file:
        cmd += ["--env-file", args.env_file]
    if args.host:
        cmd += ["--host", args.host]
    if args.port:
        cmd += ["--port", str(args.port)]
    log_path = str(pidfile.with_suffix(".log"))
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(MODULE_DIR),
        env=_env(args),
        start_new_session=True,
    )
    pidfile.write_text(str(proc.pid))
    print(f"phantom-infiniproxy started pid={proc.pid} log={log_path}")
    return 0


def cmd_stop(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    if not _is_running(pid):
        print("phantom-infiniproxy not running")
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
    print(f"phantom-infiniproxy stopped pid={pid}")
    return 0


def cmd_status(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    running = _is_running(pid)
    health = _healthy(args) if running else False
    state = "running" if running else "stopped"
    suffix = f" pid={pid}" if pid else ""
    print(f"phantom-infiniproxy {state}{suffix} health={'ok' if health else 'unknown'}")
    return 0 if running else 3


def cmd_version(_args) -> int:
    print(version.line())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phantom-infiniproxy")
    parser.add_argument("--pidfile", default=str(DEFAULT_PIDFILE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
        p.add_argument("--host", default=DEFAULT_HOST)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)

    serve = sub.add_parser("serve", help="run foreground proxy")
    add_common(serve)
    serve.set_defaults(func=cmd_serve)

    start = sub.add_parser("start", help="start background proxy")
    add_common(start)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop background proxy")
    stop.add_argument("--timeout", type=float, default=5.0)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="show background proxy status")
    add_common(status)
    status.set_defaults(func=cmd_status)

    ver = sub.add_parser("version", help="show module version")
    ver.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
