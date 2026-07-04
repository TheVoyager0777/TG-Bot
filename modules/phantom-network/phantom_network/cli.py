"""Command line entrypoint for phantom-network."""

from __future__ import annotations

import argparse

from . import version
from .addressbook import DEFAULT_STATE_FILE
from .daemon import (
    DEFAULT_HOST,
    DEFAULT_PIDFILE,
    DEFAULT_PORT,
    cmd_contacts,
    cmd_register,
    cmd_resolve,
    cmd_serve,
    cmd_start,
    cmd_status,
    cmd_stop,
)


def cmd_version(_args) -> int:
    print(version.line())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phantom-network")
    parser.add_argument("--pidfile", default=str(DEFAULT_PIDFILE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_runtime(p):
        p.add_argument("--config", help="TOML config; [webapp] and [network] are used")
        p.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
        p.add_argument("--host", default=DEFAULT_HOST)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--netns", default="direct")
        p.add_argument("--no-netns-autostart", action="store_true")

    serve = sub.add_parser("serve", help="run foreground network daemon")
    add_runtime(serve)
    serve.set_defaults(func=cmd_serve)

    start = sub.add_parser("start", help="start background network daemon")
    add_runtime(start)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop background network daemon")
    stop.add_argument("--timeout", type=float, default=8.0)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="show daemon status")
    status.add_argument("--host", default=DEFAULT_HOST)
    status.add_argument("--port", type=int, default=DEFAULT_PORT)
    status.set_defaults(func=cmd_status)

    register = sub.add_parser("register", help="register a stable contact")
    register.add_argument("name")
    register.add_argument("local_url")
    register.add_argument("--module", default="")
    register.add_argument("--role", default="api")
    register.add_argument("--lanes", type=int, default=1)
    register.add_argument("--private", action="store_true")
    register.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    register.set_defaults(func=cmd_register)

    resolve = sub.add_parser("resolve", help="resolve a stable contact")
    resolve.add_argument("name")
    resolve.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    resolve.set_defaults(func=cmd_resolve)

    contacts = sub.add_parser("contacts", help="dump contact book")
    contacts.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    contacts.set_defaults(func=cmd_contacts)

    ver = sub.add_parser("version", help="print version")
    ver.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
