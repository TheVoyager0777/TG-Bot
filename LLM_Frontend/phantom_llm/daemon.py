"""Standalone lifecycle and local API for phantom-llm."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aiohttp import web

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from .manager import SessionManager
from .session import TurnSink
from . import version
try:
    from llm_backend import version as backend_version
except Exception:  # pragma: no cover
    backend_version = None


DEFAULT_PIDFILE = Path.home() / ".config" / "phantom-llm" / "phantom-llm.pid"
VALID_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}
log = logging.getLogger("phantom_llm.daemon")


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_providers_path"] = os.path.join(os.path.dirname(os.path.abspath(path)), "providers.toml")
    return cfg


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
        print(f"phantom-llm already running pid={pid}", file=sys.stderr)
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


class LlmDaemon:
    def __init__(self, cfg: dict, *, allow_bypass: bool = False):
        cfg = dict(cfg)
        cfg["claude"] = dict(cfg.get("claude") or {})
        cfg["_allow_orchestrator_bypass"] = bool(allow_bypass)
        if not allow_bypass and cfg["claude"].get("permission_mode") == "bypassPermissions":
            cfg["claude"]["permission_mode"] = "default"
        self.cfg = cfg
        self.mgr = SessionManager(cfg)
        self._outputs: dict[str, list[str]] = {}
        self._interactive_queues: dict[str, asyncio.Queue[dict]] = {}
        self._pending_permissions: dict[str, asyncio.Future[str]] = {}
        self._pending_questions: dict[str, asyncio.Future[dict | None]] = {}
        self.shutdown_event: asyncio.Event | None = None
        self.mgr.permission_cb = self._interactive_permission
        self.mgr.ask_question_cb = self._interactive_question

    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(
                text=json.dumps({"ok": False, "error": "invalid json"}),
                content_type="application/json",
            )
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"ok": False, "error": "json object required"}),
                content_type="application/json",
            )
        return body

    @staticmethod
    def _error(error: str, status: int = 400, **extra) -> web.Response:
        return web.json_response({"ok": False, "error": error, **extra}, status=status)

    async def _interactive_permission(self, worker: str, tool: str, tool_input: dict) -> bool:
        queue = self._interactive_queues.get(worker)
        if queue is None:
            print(f"phantom-llm: denied tool request worker={worker} tool={tool}", file=sys.stderr, flush=True)
            return False
        token = uuid.uuid4().hex[:12]
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_permissions[token] = fut
        await queue.put({
            "type": "perm",
            "token": token,
            "tool": tool,
            "input": tool_input or {},
            "preview": json.dumps(tool_input or {}, ensure_ascii=False)[:400],
        })
        try:
            decision = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            await queue.put({"type": "perm_done", "token": token, "decision": "timeout"})
            return False
        finally:
            self._pending_permissions.pop(token, None)
        await queue.put({"type": "perm_done", "token": token, "decision": decision})
        if decision == "always":
            w = self.mgr.get(worker)
            if w:
                w.auto_allow.add(tool)
            return True
        return decision == "allow"

    async def _interactive_question(self, worker: str, tool_input: dict) -> dict | None:
        queue = self._interactive_queues.get(worker)
        if queue is None:
            return None
        token = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict | None] = asyncio.get_running_loop().create_future()
        self._pending_questions[token] = fut
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        if not isinstance(questions, list):
            questions = [tool_input] if isinstance(tool_input, dict) and tool_input.get("question") else []
        await queue.put({
            "type": "ask_question",
            "token": token,
            "phase": "start",
            "questions": questions,
            "input": tool_input or {},
        })
        try:
            result = await asyncio.wait_for(fut, timeout=900)
        except asyncio.TimeoutError:
            await queue.put({"type": "ask_question", "token": token, "phase": "timeout"})
            return None
        finally:
            self._pending_questions.pop(token, None)
        phase = "answered" if result and result.get("answers") else "cancelled"
        await queue.put({
            "type": "ask_question",
            "token": token,
            "phase": phase,
            "answers": (result or {}).get("answers", {}) if isinstance(result, dict) else {},
        })
        return result

    async def start(self) -> None:
        state = self.mgr.load_state()
        self.mgr.restore_settings()
        await self.mgr.start_orchestrator(
            resume_session_id=state.get("orchestrator_session_id"),
            model=state.get("orchestrator_model"),
        )
        await self.mgr.restore_workers(state)

    async def stop(self) -> None:
        self.mgr.save_state()
        await self.mgr.shutdown()

    def _session_info(self, name: str) -> dict | None:
        w = self.mgr.get(name)
        if w is None:
            return None
        return {
            "name": w.name,
            "orchestrator": bool(w.is_orchestrator),
            "busy": w.busy,
            "turns": w.turns,
            "session_id": ((w.session_id or w.resume_session_id) or "")[:8],
            "session_id_full": (w.session_id or w.resume_session_id) or "",
            "provider": w.provider or (self.mgr.router.active if self.mgr.router else None),
            "model": w.model,
            "mode": w.mode or self.mgr.default_mode,
            "backend": w.backend_name,
            "idle_s": round(time.time() - w.last_active, 1),
            "cwd": w.cwd,
            "last_output": w.last_output[-400:],
        }

    def _sessions(self) -> list[dict]:
        names = [self.mgr.ORCH] if self.mgr.orchestrator else []
        names.extend(self.mgr.workers.keys())
        return [s for name in names if (s := self._session_info(name)) is not None]

    def state(self) -> dict:
        return {
            "ok": True,
            "default_mode": self.mgr.default_mode,
            "active_backend": self.mgr.active_backend,
            "backend": self.mgr.backend_state(),
            "active_provider": self.mgr.router.active if self.mgr.router else None,
            "fast_mode": self.mgr.fast_mode,
            "sessions": self._sessions(),
        }

    def _providers(self) -> dict:
        router = self.mgr.router
        if router is None:
            return {"ok": True, "enabled": False, "active": None, "providers": []}
        providers = []
        for p in router.providers:
            providers.append({
                "name": p.name,
                "base_url": p.base_url,
                "priority": p.priority,
                "default": p.default,
                "active": p.name == router.active,
                "models": dict(p.models or {}),
                "timeout": p.timeout,
                "has_auth_token": bool(p.auth_token),
                "headers": sorted((p.headers or {}).keys()),
                "betas": list(p.betas or []),
            })
        return {
            "ok": True,
            "enabled": True,
            "active": router.active,
            "providers": providers,
        }

    def _sink(self, name: str, events: list[dict] | None = None) -> TurnSink:
        self._outputs.setdefault(name, [])

        async def on_text(text: str):
            self._outputs[name].append(text)
            if events is not None:
                events.append({"type": "text", "text": text})

        async def on_event(event):
            if events is not None:
                if isinstance(event, dict):
                    events.append(dict(event))
                else:
                    events.append({"type": "note", "text": str(event)})

        async def on_start():
            self._outputs[name] = []
            if events is not None:
                events.append({"type": "turn_start"})

        return TurnSink(on_text=on_text, on_event=on_event, on_start=on_start)

    def _stream_sink(self, name: str, queue: asyncio.Queue[dict]) -> TurnSink:
        self._outputs.setdefault(name, [])

        async def on_text(text: str):
            self._outputs[name].append(text)
            await queue.put({"type": "text", "text": text})

        async def on_event(event):
            if isinstance(event, dict):
                await queue.put(dict(event))
            else:
                await queue.put({"type": "note", "text": str(event)})

        async def on_start():
            self._outputs[name] = []
            await queue.put({"type": "turn_start"})

        return TurnSink(on_text=on_text, on_event=on_event, on_start=on_start)

    def _apply_request_overrides(self, w, body: dict) -> None:
        provider = str(body.get("provider") or "").strip()
        if provider:
            if self.mgr.router and not self.mgr.router.get(provider):
                raise web.HTTPBadRequest(text=f"unknown provider: {provider}")
            w.provider = provider
            w.backend_config = self.mgr._backend_config_for(provider)
        if "model" in body:
            model = str(body.get("model") or "").strip()
            w.model = model or None
        cwd = str(body.get("cwd") or "").strip()
        if cwd:
            w.cwd = cwd
        effort = str(body.get("effort") or body.get("reasoning_effort") or "").strip()
        if effort:
            w.backend_config["codex_reasoning_effort"] = effort
            w.backend_config["model_reasoning_effort"] = effort

    async def run_prompt(self, session: str, text: str, *, autospawn: bool = False,
                         overrides: dict | None = None,
                         collect_events: bool = False) -> str | tuple[str, list[dict]]:
        w = self.mgr.get(session or self.mgr.ORCH)
        if w is None:
            if autospawn and session and session != self.mgr.ORCH:
                w = await self.mgr.spawn_worker(
                    session,
                    provider=(overrides or {}).get("provider"),
                    model=(overrides or {}).get("model"),
                    cwd=(overrides or {}).get("cwd"),
                )
            else:
                raise web.HTTPNotFound(text=f"unknown session: {session}")
        if w is None:
            raise web.HTTPNotFound(text=f"unknown session: {session}")
        if overrides:
            self._apply_request_overrides(w, overrides)
        if w.busy:
            raise web.HTTPConflict(text=f"session busy: {session}")
        events: list[dict] | None = [] if collect_events else None
        result = await w.run(text, self._sink(w.name, events))
        self.mgr.save_state()
        out = result or "".join(self._outputs.get(w.name, []))
        if collect_events:
            return out, (events or [])
        return out

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_state(self, _request: web.Request) -> web.Response:
        return web.json_response(self.state())

    async def handle_backend(self, _request: web.Request) -> web.Response:
        data = self.mgr.backend_state()
        data["ok"] = True
        if backend_version is not None:
            data["version"] = backend_version.as_dict()
        return web.json_response(data)

    async def handle_set_backend(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        name = (body.get("name") or body.get("backend") or "").strip()
        if not name:
            return self._error("name required")
        msg = await self.mgr.set_backend(name)
        ok = not msg.startswith("未知")
        return web.json_response({"ok": ok, "message": msg, "backend": self.mgr.backend_state()},
                                 status=200 if ok else 400)

    async def handle_run(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session = (body.get("session") or self.mgr.ORCH).strip()
        text = (body.get("text") or "").strip()
        if not text:
            return self._error("text required")
        try:
            collect_events = bool(body.get("events") or body.get("collect_events"))
            result = await self.run_prompt(
                session,
                text,
                autospawn=bool(body.get("autospawn")),
                overrides=body,
                collect_events=collect_events,
            )
        except web.HTTPNotFound as e:
            return self._error(e.text or "session not found", status=404)
        except web.HTTPConflict as e:
            return self._error(e.text or "session busy", status=409)
        except Exception as e:
            log.exception("run failed for session=%s", session)
            return self._error(str(e) or "run failed", status=500)
        if collect_events:
            out, events = result
            return web.json_response({
                "ok": True,
                "session": session,
                "text": out,
                "events": events,
            })
        return web.json_response({"ok": True, "session": session, "text": result})

    async def handle_run_stream(self, request: web.Request) -> web.StreamResponse:
        body = await self._json_body(request)
        session = (body.get("session") or self.mgr.ORCH).strip()
        text = (body.get("text") or "").strip()
        if not text:
            return self._error("text required")
        w = self.mgr.get(session or self.mgr.ORCH)
        if w is None:
            if body.get("autospawn") and session and session != self.mgr.ORCH:
                w = await self.mgr.spawn_worker(
                    session,
                    provider=body.get("provider"),
                    model=body.get("model"),
                    cwd=body.get("cwd"),
                )
            else:
                return self._error(f"unknown session: {session}", status=404)
        if w is None:
            return self._error(f"unknown session: {session}", status=404)
        try:
            self._apply_request_overrides(w, body)
        except web.HTTPBadRequest as e:
            return self._error(e.text or "bad request", status=400)
        if w.busy:
            return self._error(f"session busy: {session}", status=409)

        queue: asyncio.Queue[dict] = asyncio.Queue()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        async def emit(event: dict):
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            await response.write(line.encode("utf-8"))

        async def run_worker():
            try:
                result = await w.run(text, self._stream_sink(w.name, queue))
                self.mgr.save_state()
                out = result or "".join(self._outputs.get(w.name, []))
                await queue.put({"type": "final", "text": out})
            except Exception as e:
                log.exception("stream run failed for session=%s", session)
                await queue.put({"type": "error", "error": str(e) or "run failed"})
            finally:
                await queue.put({"type": "_eof"})

        task = asyncio.create_task(run_worker())
        self._interactive_queues[w.name] = queue
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "_eof":
                    break
                await emit(event)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            task.cancel()
            raise
        finally:
            if self._interactive_queues.get(w.name) is queue:
                self._interactive_queues.pop(w.name, None)
            if not task.done():
                task.cancel()
            try:
                await response.write_eof()
            except Exception:
                pass
        return response

    async def handle_spawn(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        name = (body.get("name") or "").strip()
        if not name:
            return self._error("name required")
        try:
            w = await self.mgr.spawn_worker(
                name,
                mode=body.get("mode"),
                provider=body.get("provider"),
                model=body.get("model"),
                resume_session_id=body.get("resume_session_id") or body.get("session_id"),
                system_append=body.get("system_append"),
                agents_set=body.get("agents_set"),
                cwd=body.get("cwd"),
            )
        except ValueError as e:
            return self._error(str(e), status=400)
        return web.json_response({"ok": True, "name": w.name, "session": self._session_info(w.name)})

    async def handle_stop_worker(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        ok = await self.mgr.stop_worker(name)
        return web.json_response({"ok": ok, **({} if ok else {"error": "session not found"})})

    async def handle_interrupt(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        name = (body.get("session") or self.mgr.ORCH).strip()
        w = self.mgr.get(name)
        if w is None:
            return self._error("session not ready", status=404)
        if not w.busy:
            return web.json_response({"ok": False, "error": "session not busy"})
        ok = await w.interrupt()
        return web.json_response({"ok": ok})

    async def handle_permission(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        token = (body.get("token") or "").strip()
        decision = (body.get("decision") or "").strip()
        if decision not in {"allow", "always", "deny"}:
            return self._error("decision must be one of: allow, always, deny")
        fut = self._pending_permissions.get(token)
        if fut is None or fut.done():
            return self._error("permission request not found", status=404)
        fut.set_result(decision)
        return web.json_response({"ok": True})

    async def handle_answer_question(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        token = (body.get("token") or "").strip()
        updated = body.get("updated_input")
        if updated is None and not body.get("cancelled"):
            answers = body.get("answers") or {}
            updated = {"answers": answers} if isinstance(answers, dict) else None
        if updated is not None and not isinstance(updated, dict):
            return self._error("updated_input must be an object")
        fut = self._pending_questions.get(token)
        if fut is None or fut.done():
            return self._error("question request not found", status=404)
        fut.set_result(updated)
        return web.json_response({"ok": True})

    async def handle_sessions(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "sessions": self._sessions()})

    async def handle_session(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        session = self._session_info(name)
        if session is None:
            return self._error("session not found", status=404)
        return web.json_response({"ok": True, "session": session})

    async def handle_set_session_model(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        body = await self._json_body(request)
        model = body.get("model")
        if isinstance(model, str):
            model = model.strip()
            if model in {"", "default", "none", "null"}:
                model = None
        elif model is not None:
            return self._error("model must be a string or null")
        msg = await self.mgr.set_session_model(name, model)
        ok = not (msg.startswith("没有") or msg.startswith("切模型失败"))
        status = 200 if ok else 404 if msg.startswith("没有") else 400
        return web.json_response({
            "ok": ok,
            "message": msg,
            "session": self._session_info(name),
        }, status=status)

    async def handle_set_mode(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        mode = (body.get("mode") or "").strip()
        if mode not in VALID_MODES:
            return self._error(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
        name = (body.get("session") or "").strip()
        target = self.mgr.get(name) if name else None
        if name and target is None:
            return self._error("session not found", status=404)
        if target and target.client:
            try:
                await target.client.set_permission_mode(mode)
            except Exception as e:
                return self._error(f"mode switch failed: {e}")
        if target:
            target.mode = mode
        self.mgr.default_mode = mode
        self.mgr.save_state()
        return web.json_response({
            "ok": True,
            "mode": mode,
            "default_mode": self.mgr.default_mode,
            "session": self._session_info(name) if name else None,
        })

    async def handle_compact_session(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        w = self.mgr.get(name or self.mgr.ORCH)
        if w is None:
            return self._error("session not found", status=404)
        msg = await w.compact()
        ok = not (msg.startswith("压缩失败") or msg.startswith("当前后端"))
        status = 200 if ok else 400
        self.mgr.save_state()
        return web.json_response({
            "ok": ok,
            "message": msg,
            "session": self._session_info(name),
        }, status=status)

    async def handle_providers(self, _request: web.Request) -> web.Response:
        return web.json_response(self._providers())

    async def handle_set_active_provider(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        provider = (body.get("name") or body.get("provider") or "").strip()
        if not provider:
            return self._error("provider required")
        msg = await self.mgr.set_active_provider(provider)
        ok = not (msg.startswith("未启用") or msg.startswith("未知"))
        return web.json_response({
            "ok": ok,
            "message": msg,
            "providers": self._providers(),
        }, status=200 if ok else 400)

    async def handle_reload_providers(self, _request: web.Request) -> web.Response:
        msg = await self.mgr.reload_providers_from_file()
        ok = not (msg.startswith("未启用") or msg.startswith("重载失败"))
        return web.json_response({
            "ok": ok,
            "message": msg,
            "providers": self._providers(),
        }, status=200 if ok else 400)

    async def handle_shutdown(self, _request: web.Request) -> web.Response:
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        return web.json_response({"ok": True})

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/state", self.handle_state)
        app.router.add_get("/backend", self.handle_backend)
        app.router.add_post("/backend", self.handle_set_backend)
        app.router.add_post("/run", self.handle_run)
        app.router.add_post("/run/stream", self.handle_run_stream)
        app.router.add_post("/worker", self.handle_spawn)
        app.router.add_delete("/worker/{name}", self.handle_stop_worker)
        app.router.add_post("/interrupt", self.handle_interrupt)
        app.router.add_post("/permission", self.handle_permission)
        app.router.add_post("/ask", self.handle_answer_question)
        app.router.add_get("/sessions", self.handle_sessions)
        app.router.add_get("/sessions/{name}", self.handle_session)
        app.router.add_post("/sessions", self.handle_spawn)
        app.router.add_delete("/sessions/{name}", self.handle_stop_worker)
        app.router.add_post("/sessions/{name}/model", self.handle_set_session_model)
        app.router.add_post("/sessions/{name}/compact", self.handle_compact_session)
        app.router.add_post("/mode", self.handle_set_mode)
        app.router.add_get("/providers", self.handle_providers)
        app.router.add_post("/providers/active", self.handle_set_active_provider)
        app.router.add_post("/providers/reload", self.handle_reload_providers)
        app.router.add_post("/shutdown", self.handle_shutdown)
        return app


async def _serve_async(args) -> int:
    cfg = _load_config(args.config)
    daemon = LlmDaemon(cfg, allow_bypass=args.allow_bypass)
    await daemon.start()
    runner = web.AppRunner(daemon.make_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    print(f"phantom-llm serving: http://{args.host}:{args.port}", flush=True)

    stop = asyncio.Event()
    daemon.shutdown_event = stop
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        await daemon.stop()
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
        print(f"phantom-llm already running pid={pid}")
        return 0
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "phantom_llm.daemon",
        "serve",
        "--config",
        args.config,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.allow_bypass:
        cmd.append("--allow-bypass")
    log_path = str(pidfile.with_suffix(".log"))
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pidfile.write_text(str(proc.pid))
    print(f"phantom-llm started pid={proc.pid} log={log_path}")
    return 0


def cmd_stop(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    if not _is_running(pid):
        print("phantom-llm not running")
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
    print(f"phantom-llm stopped pid={pid}")
    return 0


def cmd_status(args) -> int:
    pidfile = _pidfile(args)
    pid = _read_pid(pidfile)
    running = _is_running(pid)
    print(f"phantom-llm {'running' if running else 'stopped'}" + (f" pid={pid}" if pid else ""))
    return 0 if running else 3


def cmd_version(_args) -> int:
    print(version.line())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phantom-llm")
    parser.add_argument("--pidfile", default=str(DEFAULT_PIDFILE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--config", required=True, help="bot-style TOML config path")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8799)
        p.add_argument("--allow-bypass", action="store_true",
                       help="allow bypassPermissions in standalone daemon")

    serve = sub.add_parser("serve", help="run foreground LLM daemon")
    add_common(serve)
    serve.set_defaults(func=cmd_serve)

    start = sub.add_parser("start", help="start background LLM daemon")
    add_common(start)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop background LLM daemon")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="show background daemon status")
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
