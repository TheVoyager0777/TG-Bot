"""Compatibility shim for the split phantom-llm project."""

from project_paths import ensure_split_projects

ensure_split_projects()

from phantom_llm.prompt_pool import *  # noqa: F401,F403
