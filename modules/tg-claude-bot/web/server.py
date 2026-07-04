"""Compatibility shim for the split phantom-console project."""

from project_paths import ensure_split_projects

ensure_split_projects()

from phantom_console.server import *  # noqa: F401,F403
