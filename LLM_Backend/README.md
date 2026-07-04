# LLM_Backend

Independent CLI backend adapters for Phantom LLM.

- `claude-code`: non-interactive Claude Code CLI backend using `claude --print`.
- `codex`: non-interactive Codex CLI backend using `codex exec`.

The frontend (`LLM_Frontend`) selects the active backend from `[llm_backend]`
in the bot config.

```toml
[llm_backend]
active = "claude-code" # or "codex"
codex_bin = "codex"
claude_bin = "claude"
codex_sandbox = "workspace-write"
codex_approval = "never"
```

`backend_info()` exposes a `capabilities` object for each backend with these
keys: `stream`, `interrupt`, `resume_session`, `tools`, `permission_mode`,
`model`, `cwd`, and `attachments`.

Codex resume support is intentionally narrow and follows the local CLI help:
when `BackendRequest.session_id` is present the adapter runs
`codex exec resume <session_id> -`. New Codex runs still use `codex exec -`.
The resume subcommand does not advertise `--cd`, `--sandbox`,
`--ask-for-approval`, or `--profile`, so those exec-only flags are not passed
while resuming.
