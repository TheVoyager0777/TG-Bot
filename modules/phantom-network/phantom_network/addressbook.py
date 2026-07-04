"""Persistent contact book for Phantom module network endpoints."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


DEFAULT_STATE_FILE = Path.home() / ".config" / "phantom-network" / "addressbook.json"
DEFAULT_COMPAT_FILE = Path.home() / ".config" / "tg-cf-tunnels.json"


def now() -> int:
    return int(time.time())


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": 0,
        "contacts": {},
        "pools": {},
    }


class AddressBook:
    def __init__(self, state_file: str | os.PathLike[str] = DEFAULT_STATE_FILE,
                 compat_file: str | os.PathLike[str] = DEFAULT_COMPAT_FILE):
        self.state_file = Path(state_file).expanduser()
        self.compat_file = Path(compat_file).expanduser()

    def load(self) -> dict[str, Any]:
        try:
            with self.state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = default_state()
        data.setdefault("schema_version", 1)
        data.setdefault("updated_at", 0)
        data.setdefault("contacts", {})
        data.setdefault("pools", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = now()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.state_file.name + ".",
            suffix=".tmp",
            dir=str(self.state_file.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.state_file)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def register(self, name: str, local_url: str, role: str = "api",
                 module: str = "", lanes: int = 1,
                 public: bool = True, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.load()
        contacts = data["contacts"]
        contact = contacts.get(name) or {}
        created_at = contact.get("created_at") or now()
        contact.update({
            "name": name,
            "module": module or contact.get("module") or name.split(".", 1)[0],
            "role": role,
            "local_url": local_url.rstrip("/"),
            "public": bool(public),
            "lanes": max(1, int(lanes or 1)),
            "created_at": created_at,
            "updated_at": now(),
            "metadata": metadata or contact.get("metadata") or {},
        })
        contact.setdefault("domains", [])
        contacts[name] = contact
        data["pools"][name] = self._pool_from_contact(contact)
        self.save(data)
        self.write_compat(data)
        return contact

    def set_domain(self, name: str, url: str, lane: int = 0, kind: str = "cloudflared",
                   status: str = "unknown", latency_ms: float | None = None,
                   error: str = "") -> dict[str, Any]:
        data = self.load()
        contact = data["contacts"].setdefault(name, {
            "name": name,
            "module": name.split(".", 1)[0],
            "role": "api",
            "local_url": "",
            "public": True,
            "lanes": 1,
            "created_at": now(),
            "metadata": {},
            "domains": [],
        })
        domains = contact.setdefault("domains", [])
        url = url.rstrip("/")
        for old in domains:
            if old.get("lane") == int(lane) and old.get("url") != url and old.get("status") == "online":
                old["status"] = "superseded"
                old["last_checked"] = now()
        item = next((d for d in domains if d.get("url") == url), None)
        if item is None:
            item = {"url": url, "first_seen": now()}
            domains.append(item)
        item.update({
            "url": url,
            "lane": int(lane),
            "kind": kind,
            "status": status,
            "latency_ms": latency_ms,
            "error": error,
            "last_seen": now(),
        })
        contact["updated_at"] = now()
        data["pools"][name] = self._pool_from_contact(contact)
        self.save(data)
        self.write_compat(data)
        return item

    def mark_domain(self, name: str, url: str, status: str,
                    latency_ms: float | None = None, error: str = "") -> None:
        data = self.load()
        contact = data.get("contacts", {}).get(name)
        if not contact:
            return
        for item in contact.get("domains", []):
            if item.get("url") == url.rstrip("/"):
                item["status"] = status
                item["latency_ms"] = latency_ms
                item["error"] = error
                item["last_checked"] = now()
                if status == "online":
                    item["last_ok"] = now()
                break
        data["pools"][name] = self._pool_from_contact(contact)
        self.save(data)
        self.write_compat(data)

    def resolve(self, name: str) -> dict[str, Any] | None:
        data = self.load()
        contact = data.get("contacts", {}).get(name)
        if not contact:
            return None
        pool = self._pool_from_contact(contact)
        return {
            "name": name,
            "module": contact.get("module"),
            "role": contact.get("role"),
            "local_url": contact.get("local_url", ""),
            "primary": pool.get("primary", ""),
            "domains": pool.get("domains", []),
            "all_domains": contact.get("domains", []),
            "updated_at": contact.get("updated_at", 0),
        }

    def snapshot(self) -> dict[str, Any]:
        data = self.load()
        data["pools"] = {
            name: self._pool_from_contact(contact)
            for name, contact in data.get("contacts", {}).items()
        }
        return data

    def write_compat(self, data: dict[str, Any] | None = None) -> None:
        data = data or self.load()
        static = self._pool_from_contact(data.get("contacts", {}).get("console.static", {})).get("primary", "")
        api = self._pool_from_contact(data.get("contacts", {}).get("console.api", {})).get("primary", "")
        compat = {
            "schema_version": 2,
            "source": "phantom-network",
            "updated_at": data.get("updated_at", now()),
            "static_url": static,
            "api_url": api,
        }
        self.compat_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.compat_file.name + ".",
            suffix=".tmp",
            dir=str(self.compat_file.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(compat, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.compat_file)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _pool_from_contact(contact: dict[str, Any]) -> dict[str, Any]:
        domains = list(contact.get("domains") or [])
        domains.sort(key=lambda d: (
            0 if d.get("status") == "online" else 1,
            float(d.get("latency_ms") if d.get("latency_ms") is not None else 999999),
            -int(d.get("last_ok") or d.get("last_seen") or 0),
        ))
        public_domains = [d for d in domains if d.get("url")]
        online_domains = [d for d in public_domains if d.get("status") == "online"]
        return {
            "primary": online_domains[0]["url"] if online_domains else "",
            "domains": [d["url"] for d in public_domains],
            "online": [d["url"] for d in online_domains],
            "lanes": int(contact.get("lanes") or 1),
            "local_url": contact.get("local_url", ""),
            "updated_at": contact.get("updated_at", 0),
        }
