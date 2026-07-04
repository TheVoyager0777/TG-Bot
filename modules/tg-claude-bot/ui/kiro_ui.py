"""kiro_ui —— 把 Kiro IDE 聊天界面的工具呈现语言移植到 Telegram。

逆向自 Kiro 0.12.x 的 kiro-ui-agent-chat bundle：
- 工具调用用「进行时动作标签 + 目标」呈现（Reading foo.py / Running: npm test）
- 每个工具一行原地变状态：运行中 ◐ → 完成 ✓ / 失败 ✗ / 拒绝 ⊘
- 完成后切换成过去式标签（Read foo.py / Ran npm test）
- 思考块折叠成单行 💭

设计取值：Kiro 暗色主题 + 紫色强调 #8041e6（TG 纯文本无法上色，用 emoji 还原状态语义）。
"""
from __future__ import annotations

import html as _html
import re as _re

# ── 状态图标（对应 Kiro 的 agent*Icon 状态机）──────────────────────────────
PHASE_RUN = "running"
PHASE_DONE = "completed"
PHASE_ERROR = "error"
PHASE_REJECT = "rejected"

# Kiro 状态机：inProgress / completed / error / rejected / pending
ICON = {
    PHASE_RUN:    "◐",   # in-progress（旋转态，TG 静态用半圆）
    PHASE_DONE:   "✓",   # accepted/completed
    PHASE_ERROR:  "✗",   # error/failed
    PHASE_REJECT: "⊘",   # rejected（用户拒绝授权）
    "pending":    "○",   # 等待授权
}


def _basename(p: str) -> str:
    if not p:
        return ""
    p = p.rstrip("/")
    return p.rsplit("/", 1)[-1] or p


def _trunc(s: str, n: int) -> str:
    s = " ".join(str(s).split())  # 折叠空白
    return s if len(s) <= n else s[: n - 1] + "…"


# ── 工具名 → (进行时标签, 过去式标签) ──────────────────────────────────────
# 左：Claude Code / SDK 的工具名；值：取 input 造标签的函数。
# 逆向自 Kiro switch：read_file→"Reading {f}"，fs_write→"Writing {f}" 等。

def _label_read(d, past):
    f = _basename(d.get("file_path") or d.get("path") or "")
    verb = "Read" if past else "Reading"
    return f"{verb} {f}" if f else f"{verb} file"


def _label_write(d, past):
    f = _basename(d.get("file_path") or d.get("path") or "")
    verb = "Wrote" if past else "Writing"
    return f"{verb} {f}" if f else f"{verb} file"


def _label_edit(d, past):
    f = _basename(d.get("file_path") or d.get("path") or "")
    verb = "Edited" if past else "Editing"
    return f"{verb} {f}" if f else f"{verb} file"


def _label_delete(d, past):
    f = _basename(d.get("file_path") or d.get("path") or "")
    verb = "Deleted" if past else "Deleting"
    return f"{verb} {f}" if f else f"{verb} file"


def _label_bash(d, past):
    cmd = _trunc(d.get("command") or "", 60)
    verb = "Ran" if past else "Running"
    if not cmd:
        return "Ran command" if past else "Running command"
    return f"{verb}: {cmd}"


def _label_grep(d, past):
    pat = _trunc(d.get("pattern") or d.get("query") or "", 40)
    verb = "Searched" if past else "Searching"
    return f'{verb} for "{pat}"' if pat else f"{verb} content"


def _label_glob(d, past):
    pat = _trunc(d.get("pattern") or d.get("query") or "", 40)
    verb = "Searched" if past else "Searching"
    return f'{verb} files "{pat}"' if pat else f"{verb} files"


def _label_ls(d, past):
    f = _basename(d.get("path") or "")
    verb = "Listed" if past else "Listing"
    return f"{verb} {f}" if f else f"{verb} directory"


def _label_task(d, past):
    desc = _trunc(d.get("description") or d.get("subagent_type") or "", 40)
    verb = "Ran sub-agent" if past else "Running sub-agent"
    return f"{verb}: {desc}" if desc else verb


def _label_webfetch(d, past):
    url = _trunc(d.get("url") or "", 50)
    verb = "Fetched" if past else "Fetching"
    return f"{verb} {url}" if url else f"{verb} URL"


def _label_websearch(d, past):
    q = _trunc(d.get("query") or "", 40)
    verb = "Searched web" if past else "Searching web"
    return f'{verb} "{q}"' if q else verb


def _label_todo(d, past):
    return "Updated task list" if past else "Updating task list"


# Claude Code 工具名映射（含常见别名）
_TOOL_LABEL = {
    "Read": _label_read, "read_file": _label_read,
    "Write": _label_write, "fs_write": _label_write, "create": _label_write,
    "Edit": _label_edit, "MultiEdit": _label_edit, "str_replace": _label_edit,
    "NotebookEdit": _label_edit,
    "Bash": _label_bash, "execute_bash": _label_bash, "run_command": _label_bash,
    "BashOutput": _label_bash,
    "Grep": _label_grep, "grep_search": _label_grep,
    "Glob": _label_glob, "file_search": _label_glob,
    "LS": _label_ls, "list_directory": _label_ls,
    "Task": _label_task, "invoke_sub_agent": _label_task,
    "WebFetch": _label_webfetch,
    "WebSearch": _label_websearch,
    "TodoWrite": _label_todo,
}


def tool_label(tool: str, tool_input: dict | None, past: bool = False) -> str:
    """Kiro 风格的工具标签。past=True 用过去式（完成态）。"""
    d = tool_input or {}
    fn = _TOOL_LABEL.get(tool)
    if fn:
        return fn(d, past)
    # 未知工具（含 MCP mcp__xxx__yyy）：净化名字
    name = tool.split("__")[-1] if tool.startswith("mcp__") else tool
    return name


def tool_line(tool: str, tool_input: dict | None, phase: str = PHASE_RUN) -> str:
    """渲染一行工具卡片：图标 + 标签。完成/失败用过去式。"""
    past = phase in (PHASE_DONE, PHASE_ERROR)
    label = tool_label(tool, tool_input, past=past)
    icon = ICON.get(phase, ICON[PHASE_RUN])
    if phase == PHASE_ERROR:
        label += " — failed"
    elif phase == PHASE_REJECT:
        label += " — rejected"
    return f"{icon} {label}"


def thinking_line(text: str, limit: int = 140) -> str:
    """思考块 → 单行 💭（Kiro 把 reasoning 折叠成一行摘要）。"""
    t = " ".join((text or "").split())
    if not t:
        return ""
    return f"💭 {t[:limit]}" + ("…" if len(t) > limit else "")


# ── markdown → Telegram HTML（完成态富文本，对应 Kiro 的 markdown 渲染）────
# 保守子集：``` 围栏→<pre>、`code`→<code>、**bold**→<b>、# 标题行→<b>、
# - / * 列表点→•。所有内容先 HTML 转义再包标签，保证实体平衡、解析不炸。

_HEADING_RE = _re.compile(r"^#{1,6}\s+(.+)$")
_BOLD_RE = _re.compile(r"\*\*(.+?)\*\*")
_BULLET_RE = _re.compile(r"^(\s*)[-*]\s+")


def _inline_html(ln: str) -> str:
    """单行（非围栏内）的内联格式化：先按反引号切分隔离 inline code。"""
    parts = ln.split("`")
    if len(parts) % 2 == 0:  # 反引号不配对：放弃 code 解析，整行只做 bold
        esc = _html.escape(ln)
        return _BOLD_RE.sub(r"<b>\1</b>", esc)
    chunks = []
    for i, p in enumerate(parts):
        esc = _html.escape(p)
        if i % 2:
            chunks.append(f"<code>{esc}</code>")
        else:
            chunks.append(_BOLD_RE.sub(r"<b>\1</b>", esc))
    return "".join(chunks)


def inline_html(line: str) -> str:
    """公开版单行内联格式化（详情页/待办等单行富文本用）。"""
    return _inline_html(line)


def md_to_html(text: str) -> str:
    """把模型 markdown 输出转成 Telegram HTML。逐行处理，围栏内容原样进 <pre>。"""
    out: list[str] = []
    code: list[str] | None = None
    for ln in (text or "").split("\n"):
        if ln.strip().startswith("```"):
            if code is None:
                code = []
            else:
                out.append("<pre>" + _html.escape("\n".join(code)) + "</pre>")
                code = None
            continue
        if code is not None:
            code.append(ln)
            continue
        m = _HEADING_RE.match(ln.strip())
        if m:
            # 标题行整行加粗；行内去掉 ** 防嵌套 <b>
            out.append("<b>" + _inline_html(m.group(1).replace("**", "")) + "</b>")
            continue
        out.append(_inline_html(_BULLET_RE.sub(r"\1• ", ln)))
    if code is not None:  # 未闭合围栏：仍按代码块收尾
        out.append("<pre>" + _html.escape("\n".join(code)) + "</pre>")
    return "\n".join(out)


# ── 回合统计行（对应 Kiro 完成态底部的 usage 摘要）─────────────────────────

def _agg_usage(usage: dict, model_usage: dict, field: str) -> int:
    if field in usage:
        try:
            return int(usage[field] or 0)
        except (TypeError, ValueError):
            return 0
    total = 0
    for mu in model_usage.values():
        if isinstance(mu, dict) and field in mu:
            try:
                total += int(mu[field] or 0)
            except (TypeError, ValueError):
                pass
    return total


def _kfmt(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_result_stats(msg) -> str:
    """把 ResultMessage 压成一行统计：耗时/轮次/token/缓存/请求数/成本。"""
    bits: list[str] = []
    dur = getattr(msg, "duration_ms", None)
    api_dur = getattr(msg, "duration_api_ms", None)
    turns = getattr(msg, "num_turns", None)
    cost = getattr(msg, "total_cost_usd", None)
    usage = getattr(msg, "usage", None) or {}
    model_usage = getattr(msg, "model_usage", None) or {}
    if dur:
        bits.append(f"⏱ {dur / 1000:.1f}s" + (f" (api {api_dur / 1000:.1f}s)" if api_dur else ""))
    if turns:
        bits.append(f"🔁 {turns}轮")
    in_tok = _agg_usage(usage, model_usage, "input_tokens")
    out_tok = _agg_usage(usage, model_usage, "output_tokens")
    cache_create = _agg_usage(usage, model_usage, "cache_creation_input_tokens")
    cache_read = _agg_usage(usage, model_usage, "cache_read_input_tokens")
    if in_tok or out_tok:
        bits.append(f"🪙 {_kfmt(in_tok)}↑/{_kfmt(out_tok)}↓")
    if cache_create or cache_read:
        bits.append(f"💾 {_kfmt(cache_read)}命中/{_kfmt(cache_create)}写入")
    n_req = 0
    iters = usage.get("iterations") if isinstance(usage, dict) else None
    if isinstance(iters, list):
        n_req = len(iters)
    else:
        for mu in model_usage.values():
            if isinstance(mu, dict):
                mui = mu.get("iterations")
                if isinstance(mui, list):
                    n_req += len(mui)
                elif isinstance(mu.get("requests"), int):
                    n_req += mu["requests"]
    if n_req:
        bits.append(f"📡 {n_req}请求")
    if cost:
        bits.append(f"💵 ${cost:.4f}")
    if getattr(msg, "is_error", False):
        bits.append("⚠️ 错误")
    return " · ".join(bits)
