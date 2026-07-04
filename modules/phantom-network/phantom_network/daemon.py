"""Cloudflared domain-pool daemon and local contact-book API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aiohttp import web

from .addressbook import AddressBook, DEFAULT_STATE_FILE


DEFAULT_PIDFILE = Path.home() / ".config" / "phantom-network" / "phantom-network.pid"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8890
DOMAIN_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


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
        print(f"phantom-network already running pid={pid}", file=sys.stderr)
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


def _load_toml(path: str | None) -> dict:
    if not path:
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


def _cfg_value(cfg: dict, section: str, key: str, default=None):
    return (cfg.get(section) or {}).get(key, default)


def _console_key_from_config(cfg: dict) -> str:
    token = str(_cfg_value(cfg, "telegram", "token", "") or "")
    if not token:
        return ""
    return hmac.new(token.encode(), b"phantom-console-v1", hashlib.sha256).hexdigest()[:32]


def _lan_host_for_netns() -> str:
    return os.environ.get("CF_HOST_VETH_IP") or "10.200.0.1"


def _default_contacts(cfg: dict) -> list[dict]:
    webapp = cfg.get("webapp") or {}
    infiniproxy = cfg.get("infiniproxy") or {}
    webctl = cfg.get("webctl") or {}
    llm = cfg.get("llm_frontend") or {}
    api_port = int(webapp.get("port") or os.environ.get("PHANTOM_CONSOLE_PORT") or 8875)
    static_port = int(webapp.get("static_port") or os.environ.get("PHANTOM_CONSOLE_STATIC_PORT") or 8876)
    infiniproxy_port = int(infiniproxy.get("port") or os.environ.get("PROXY_PORT") or 8010)
    llm_port = int(llm.get("port") or os.environ.get("PHANTOM_LLM_PORT") or 8799)
    webctl_port = int(webctl.get("port") or os.environ.get("PHANTOM_CONSOLE_WEBCTL_PORT") or 8080)
    lanes = int(_cfg_value(cfg, "network", "lanes", os.environ.get("PHANTOM_NETWORK_LANES", 2)) or 2)
    host = os.environ.get("PHANTOM_NETWORK_LOCAL_HOST") or "127.0.0.1"
    private_lanes = int(_cfg_value(cfg, "network", "private_lanes", 1) or 1)
    return [
        {
            "name": "console.api",
            "module": "phantom-console",
            "role": "api",
            "local_url": f"http://{host}:{api_port}",
            "public": True,
            "lanes": lanes,
        },
        {
            "name": "console.static",
            "module": "phantom-console",
            "role": "static",
            "local_url": f"http://{host}:{static_port}",
            "public": True,
            "lanes": lanes,
        },
        {
            "name": "infiniproxy.api",
            "module": "infiniproxy",
            "role": "api",
            "local_url": f"http://{host}:{infiniproxy_port}",
            "public": bool(_cfg_value(cfg, "network", "public_infiniproxy", False)),
            "lanes": private_lanes,
        },
        {
            "name": "llm.frontend",
            "module": "LLM_Frontend",
            "role": "api",
            "local_url": f"http://{host}:{llm_port}",
            "public": bool(_cfg_value(cfg, "network", "public_llm", False)),
            "lanes": private_lanes,
        },
        {
            "name": "webctl.ui",
            "module": "Platform_Phantom/WebCTL",
            "role": "ui",
            "local_url": f"http://{host}:{webctl_port}",
            "public": bool(_cfg_value(cfg, "network", "public_webctl", False)),
            "lanes": private_lanes,
        },
    ]


class NamedTunnelLane:
    """A persistent named Cloudflare tunnel (cloudflared tunnel run --token)."""

    def __init__(self, daemon: "NetworkDaemon", contact: dict):
        self.daemon = daemon
        self.contact = contact
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.token = contact.get("metadata", {}).get("tunnel_token", "")

    async def run_forever(self) -> None:
        while not self.daemon.stopping:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                name = self.contact.get("name", "named-tunnel")
                print(f"[{name}] named tunnel error: {exc}", flush=True)
            await asyncio.sleep(5)

    async def _run_once(self) -> None:
        cloudflared = shutil.which("cloudflared")
        if not cloudflared:
            name = self.contact.get("name", "named-tunnel")
            self.daemon.book.set_domain(name, "missing-cloudflared://named", status="offline",
                                        error="cloudflared not installed")
            await asyncio.sleep(30)
            return
        if not self.token:
            name = self.contact.get("name", "named-tunnel")
            self.daemon.book.set_domain(name, "missing-token://named", status="offline",
                                        error="no tunnel token configured")
            await asyncio.sleep(60)
            return
        name = self.contact.get("name", "named-tunnel")
        cmd = [cloudflared, "tunnel", "run", "--token", self.token]
        cmd = self.daemon.wrap_netns(cmd)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"[{name}] cloudflared named-tunnel pid={self.proc.pid}", flush=True)
        self.daemon.book.mark_domain(name, "cf-named://connected", "online")
        assert self.proc.stdout is not None
        while not self.daemon.stopping:
            raw = await self.proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                print(f"[{name}] {line}", flush=True)
            if re.search(r"Registered tunnel connection", line):
                m = re.search(r"connection=([a-f0-9-]+)", line)
                loc = re.search(r"location=(\S+)", line)
                loc_str = loc.group(1) if loc else "?"
                conn_id = m.group(1)[:8] if m else "?"
                self.daemon.book.mark_domain(name, f"cf-named://{loc_str}#{conn_id}", "online")
            if re.search(r"Unauthorized|invalid token|Tunnel not found|Tunnel.*deleted", line):
                self.daemon.book.mark_domain(name, f"cf-named://error", "offline", error=line[:180])
                break
        await self.stop()

    async def stop(self) -> None:
        name = self.contact.get("name", "named-tunnel")
        proc = self.proc
        self.proc = None
        if proc and proc.returncode is None:
            try:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        proc.kill()
                except ProcessLookupError:
                    pass
        self.daemon.book.mark_domain(name, "", "offline")


class TunnelLane:
    def __init__(self, daemon: "NetworkDaemon", contact: dict, lane: int):
        self.daemon = daemon
        self.contact = contact
        self.lane = lane
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.domain = ""

    async def run_forever(self) -> None:
        while not self.daemon.stopping:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[{self.contact['name']}#{self.lane}] tunnel error: {exc}", flush=True)
            await asyncio.sleep(2)

    async def _run_once(self) -> None:
        cloudflared = shutil.which("cloudflared")
        if not cloudflared:
            self.daemon.book.set_domain(
                self.contact["name"],
                f"missing-cloudflared://lane-{self.lane}",
                lane=self.lane,
                status="offline",
                error="cloudflared not installed",
            )
            await asyncio.sleep(30)
            return

        url = self._target_url()
        cmd = [
            cloudflared,
            "--config",
            os.devnull,
            "tunnel",
            "--protocol",
            "http2",
            "--no-autoupdate",
            "--http-host-header",
            "localhost",
            "--url",
            url,
        ]
        cmd = self.daemon.wrap_netns(cmd)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"[{self.contact['name']}#{self.lane}] cloudflared pid={self.proc.pid} target={url}", flush=True)
        assert self.proc.stdout is not None
        while not self.daemon.stopping:
            raw = await self.proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                print(f"[{self.contact['name']}#{self.lane}] {line}", flush=True)
            match = DOMAIN_RE.search(line)
            if match:
                self.domain = match.group(0).rstrip("/")
                self.daemon.book.set_domain(
                    self.contact["name"],
                    self.domain,
                    lane=self.lane,
                    kind="cloudflared",
                    status="unknown",
                    error="waiting for role health probe",
                )
            if re.search(r"Unauthorized: Tunnel not found|Tunnel.*deleted|connection.*refused.*trycloudflare", line):
                break
        await self.stop()

    def _target_url(self) -> str:
        local_url = self.contact["local_url"].rstrip("/")
        if self.daemon.netns_ready:
            parsed = re.match(r"^http://(?:127\.0\.0\.1|localhost)(:\d+)(/.*)?$", local_url)
            if parsed:
                return f"http://{_lan_host_for_netns()}{parsed.group(1)}{parsed.group(2) or ''}"
        return local_url

    async def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc and proc.returncode is None:
            try:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        proc.kill()
                except ProcessLookupError:
                    pass


class NetworkDaemon:
    def __init__(self, config: str | None, state_file: str, host: str, port: int,
                 netns: str = "direct", autostart_netns: bool = True):
        self.config = config
        self.cfg = _load_toml(config) if config else {}
        self.book = AddressBook(state_file)
        self.console_key = _console_key_from_config(self.cfg)
        self.host = host
        self.port = int(port)
        self.netns = netns
        self.autostart_netns = autostart_netns
        self.netns_ready = False
        self.stopping = False
        self.lanes: list[TunnelLane] = []
        self.runner: web.AppRunner | None = None
        self.health_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.netns_ready = self._ensure_netns()
        self._register_defaults()
        contacts = self.book.snapshot().get("contacts", {})
        for contact in contacts.values():
            if not contact.get("public", True):
                continue
            token = (contact.get("metadata") or {}).get("tunnel_token", "")
            if token:
                # Named tunnel — one persistent connection
                lane = NamedTunnelLane(self, contact)
                lane.task = asyncio.create_task(lane.run_forever())
                self.lanes.append(lane)
            else:
                # Quick tunnels
                lanes = max(1, int(contact.get("lanes") or 1))
                for lane_no in range(lanes):
                    lane = TunnelLane(self, contact, lane_no)
                    lane.task = asyncio.create_task(lane.run_forever())
                    self.lanes.append(lane)
        self.health_task = asyncio.create_task(self._health_loop())
        await self._start_api()
        self.book.write_compat()

    async def stop(self) -> None:
        self.stopping = True
        for lane in self.lanes:
            if lane.task:
                lane.task.cancel()
        if self.health_task:
            self.health_task.cancel()
        await asyncio.gather(*(lane.stop() for lane in self.lanes), return_exceptions=True)
        await asyncio.gather(*(lane.task for lane in self.lanes if lane.task), return_exceptions=True)
        if self.health_task:
            await asyncio.gather(self.health_task, return_exceptions=True)
            self.health_task = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    def wrap_netns(self, cmd: list[str]) -> list[str]:
        if not self.netns_ready:
            return cmd
        return ["sudo", "-n", "ip", "netns", "exec", self.netns] + cmd

    def _register_defaults(self) -> None:
        for contact in _default_contacts(self.cfg):
            self.book.register(**contact)

    def _ensure_netns(self) -> bool:
        if not self.netns or self.netns.lower() in {"none", "off", "disabled"}:
            return False
        try:
            out = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True, timeout=3)
            if re.search(rf"\b{re.escape(self.netns)}\b", out.stdout):
                return True
        except Exception:
            pass
        if not self.autostart_netns:
            return False
        script = Path(__file__).resolve().parents[1] / "scripts" / "netns-direct.sh"
        if script.exists():
            try:
                subprocess.run(["sudo", "-n", str(script), "up"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20, check=True)
                return True
            except Exception:
                return False
        return False

    async def _health_loop(self) -> None:
        while not self.stopping:
            try:
                await self._check_domains_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[health] check failed: {exc}", flush=True)
            await asyncio.sleep(20)

    async def _check_domains_once(self) -> None:
        snap = self.book.snapshot()
        tasks = []
        for name, contact in snap.get("contacts", {}).items():
            for item in contact.get("domains", []) or []:
                url = item.get("url") or ""
                if not url.startswith("http"):
                    continue
                tasks.append(asyncio.to_thread(self._probe_domain, contact, url))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _probe_domain(self, contact: dict, url: str) -> None:
        name = str(contact.get("name") or "")
        role = str(contact.get("role") or "")
        start = time.monotonic()
        headers = {"User-Agent": "phantom-network-health"}
        if name == "console.api":
            path = "/api/state"
            method = "GET"
            if self.console_key:
                headers["X-Console-Key"] = self.console_key
        elif name == "console.static":
            path = "/connect"
            method = "HEAD"
        else:
            path = "/"
            method = "HEAD"
        try:
            req = Request(url.rstrip("/") + path, headers=headers, method=method)
            with urlopen(req, timeout=6) as resp:
                status = resp.status
                body = resp.read(4096) if name == "console.api" else b""
            elapsed = (time.monotonic() - start) * 1000
            if name == "console.api" and 200 <= status < 300:
                try:
                    payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict) and "sessions" in payload:
                    self.book.mark_domain(name, url, "online", latency_ms=round(elapsed, 1))
                else:
                    self.book.mark_domain(
                        name, url, "degraded", latency_ms=round(elapsed, 1),
                        error="api identity probe returned unexpected json",
                    )
            elif name == "console.api" and status == 403:
                self.book.mark_domain(name, url, "online", latency_ms=round(elapsed, 1))
            elif name == "console.api":
                self.book.mark_domain(
                    name, url, "offline" if status == 404 else "degraded",
                    latency_ms=round(elapsed, 1),
                    error=f"api identity probe http {status}",
                )
            elif 200 <= status < 500:
                self.book.mark_domain(name, url, "online", latency_ms=round(elapsed, 1))
            else:
                self.book.mark_domain(name, url, "degraded", latency_ms=round(elapsed, 1),
                                      error=f"http {status}")
        except HTTPError as exc:
            elapsed = (time.monotonic() - start) * 1000
            if name == "console.api" and exc.code == 403:
                self.book.mark_domain(name, url, "online", latency_ms=round(elapsed, 1))
            elif name == "console.api":
                self.book.mark_domain(
                    name, url, "offline" if exc.code == 404 else "degraded",
                    latency_ms=round(elapsed, 1),
                    error=f"api identity probe http {exc.code}",
                )
            elif 200 <= exc.code < 500:
                self.book.mark_domain(name, url, "online", latency_ms=round(elapsed, 1))
            else:
                self.book.mark_domain(
                    name, url, "offline", latency_ms=round(elapsed, 1),
                    error=f"http {exc.code}",
                )
        except Exception as exc:
            self.book.mark_domain(name, url, "offline", error=str(exc)[:180])

    async def _start_api(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self.api_health)
        app.router.add_get("/contacts", self.api_contacts)
        app.router.add_get("/resolve/{name}", self.api_resolve)
        app.router.add_post("/register", self.api_register)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        print(f"phantom-network serving: http://{self.host}:{self.port}", flush=True)

    async def api_health(self, _request: web.Request) -> web.Response:
        snap = self.book.snapshot()
        return web.json_response({
            "ok": True,
            "contacts": len(snap.get("contacts", {})),
            "lanes": len(self.lanes),
            "netns": self.netns if self.netns_ready else "",
            "updated_at": snap.get("updated_at", 0),
        })

    async def api_contacts(self, _request: web.Request) -> web.Response:
        return web.json_response(self.book.snapshot())

    async def api_resolve(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        item = self.book.resolve(name)
        if not item:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item)

    async def api_register(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        try:
            item = self.book.register(
                name=str(body["name"]),
                local_url=str(body["local_url"]),
                role=str(body.get("role") or "api"),
                module=str(body.get("module") or ""),
                lanes=int(body.get("lanes") or 1),
                public=bool(body.get("public", True)),
                metadata=body.get("metadata") or {},
            )
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True, "contact": item})


async def serve_forever(args) -> int:
    daemon = NetworkDaemon(
        config=args.config,
        state_file=args.state_file,
        host=args.host,
        port=args.port,
        netns=args.netns,
        autostart_netns=not args.no_netns_autostart,
    )
    await daemon.start()
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
        await daemon.stop()
    return 0


def cmd_serve(args) -> int:
    pidfile = Path(args.pidfile).expanduser()
    if not _write_pidfile_for_current_process(pidfile):
        return 1
    try:
        return asyncio.run(serve_forever(args))
    finally:
        _remove_pidfile_for_current_process(pidfile)


def cmd_start(args) -> int:
    pidfile = Path(args.pidfile).expanduser()
    pid = _read_pid(pidfile)
    if _is_running(pid):
        print(f"phantom-network already running pid={pid}")
        return 0
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "phantom_network.cli",
        "--pidfile",
        str(pidfile),
        "serve",
        "--state-file",
        args.state_file,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--netns",
        args.netns,
    ]
    if args.config:
        cmd += ["--config", args.config]
    if args.no_netns_autostart:
        cmd += ["--no-netns-autostart"]
    log_path = str(pidfile.with_suffix(".log"))
    log = open(log_path, "ab")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pidfile.write_text(str(proc.pid))
    print(f"phantom-network started pid={proc.pid} log={log_path}")
    return 0


def cmd_stop(args) -> int:
    pidfile = Path(args.pidfile).expanduser()
    pid = _read_pid(pidfile)
    if not _is_running(pid):
        print("phantom-network not running")
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
    print(f"phantom-network stopped pid={pid}")
    return 0


def _health_url(args) -> str:
    return f"http://{args.host}:{args.port}/health"


def cmd_status(args) -> int:
    pid = _read_pid(Path(args.pidfile).expanduser())
    running = _is_running(pid)
    health = "unknown"
    if running:
        try:
            with urlopen(_health_url(args), timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            health = f"ok contacts={data.get('contacts', 0)} lanes={data.get('lanes', 0)}"
        except (OSError, URLError, json.JSONDecodeError):
            health = "unreachable"
    print(f"phantom-network {'running' if running else 'stopped'}" +
          (f" pid={pid}" if pid else "") + f" health={health}")
    return 0 if running else 3


def cmd_register(args) -> int:
    book = AddressBook(args.state_file)
    item = book.register(
        name=args.name,
        local_url=args.local_url,
        role=args.role,
        module=args.module,
        lanes=args.lanes,
        public=not args.private,
    )
    print(json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_resolve(args) -> int:
    book = AddressBook(args.state_file)
    item = book.resolve(args.name)
    if not item:
        print("not found", file=sys.stderr)
        return 1
    print(json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_contacts(args) -> int:
    book = AddressBook(args.state_file)
    print(json.dumps(book.snapshot(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0
