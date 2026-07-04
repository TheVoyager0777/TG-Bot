"""commands_llm —— LLM 相关的 Telegram 命令，集中管理。

涵盖：会话控制（/stop）、权限模式（/mode）、加速（/fast）、
上下文统计（/context）、历史压缩（/compact）、端点路由（/providers /provider）、
端点可用性测试（/testllm）。

与 bot.py 解耦：这里只定义 plain handler，不在模块加载期 import bot，
由 bot.py 在注册时用 _owner_only 包一层。目标会话一律走 BotApp.target_for(update)，
保证论坛模式下命令作用在「消息所在话题对应的 worker」，而非 attached。
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger("tgclaude")

VALID_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}

# 模型档位别名 → 给用户看的说明。值 None=回会话默认。CLI 认这些别名与具体模型串。
MODEL_TIERS = {
    "default": None,
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
}


def _b(ctx):
    return ctx.application.bot_data["botapp"]


# ── /stop：中断当前话题对应会话 ──────────────────────────────────────────
async def _interrupt(b, name: str) -> bool:
    if getattr(b, "llm_frontend_external_chat", False):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{b.llm_frontend_url}/interrupt",
                    json={"session": name},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            log.warning("frontend interrupt %s failed: %s", name, e)
            return False
    w = b.mgr.get(name)
    if not (w and w.busy and w.client):
        return False
    try:
        await w.client.interrupt()
        live = b._active_live.get(name)
        if live:
            live.set_status(live.ST_INT)
        return True
    except Exception as e:
        log.warning("interrupt %s failed: %s", name, e)
        return False


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    w, name, _tid = b.target_for(update)
    stopped: list[str] = []
    # 1) 先停「当前会话」本身（forum 下 = 该话题对应 worker；DM 下 = attached）
    if await _interrupt(b, name):
        stopped.append(name)
    # 2) DM 模式补救：若当前会话是主对话（或它空闲），把正在忙的 worker 也停掉。
    #    原因：DM 是单条线性对话，orchestrator 派活后 worker 的输出内联镜像给主人看，
    #    但主人没 /attach 到该 worker，/stop 只会打到主对话——表现为"透传到主对话"。
    #    这里让 /stop 把实际在跑的 worker 一并中断，不再是空打。forum 模式各 worker
    #    有独立话题，去对应话题 /stop 即可，故不在 forum 下做这种级联（避免误停旁的 worker）。
    if not b.forum and (w is None or w.is_orchestrator):
        for ws in b.mgr.list_workers():
            if ws["busy"] and await _interrupt(b, ws["name"]):
                stopped.append(ws["name"])
    if stopped:
        uniq = ", ".join(f"«{s}»" for s in dict.fromkeys(stopped))
        await update.message.reply_text(f"✋ 已中断 {uniq}")
    else:
        await update.message.reply_text(f"«{name}» 没在跑")


# ── /mode：切权限模式（作用于当前话题会话 + 设为新 worker 默认）──────────
async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if not ctx.args or ctx.args[0] not in VALID_MODES:
        await update.message.reply_text(
            f"当前默认: {b.mgr.default_mode}\n用法: /mode <{'|'.join(VALID_MODES)}>")
        return
    new = ctx.args[0]
    w, name, _tid = b.target_for(update)
    if getattr(b, "llm_frontend_external_chat", False):
        b.mgr.default_mode = new
        b.save_state()
        await update.message.reply_text(f"默认 mode → {new}（LLM_Frontend 后端下轮生效）")
        return
    if not (w and w.client):
        await update.message.reply_text(f"«{name}» 会话未就绪")
        return
    try:
        await w.client.set_permission_mode(new)
        w.mode = new
        b.mgr.default_mode = new
        b.save_state()
        note = ""
        if new == "bypassPermissions":
            note = ("\n⚠️ 全自动免审批：工具调用不再弹按钮，新 worker 也继承此模式。"
                    "仅信任环境用，/mode default 可切回。")
        await update.message.reply_text(f"«{name}» mode → {new}（新 worker 默认沿用）{note}")
    except Exception as e:
        await update.message.reply_text(f"切换失败: {e}")


# ── /model：切模型档位（作用于当前话题会话，SDK set_model 即时生效）────────
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    w, name, _tid = b.target_for(update)
    if getattr(b, "llm_frontend_external_chat", False):
        if not ctx.args:
            await update.message.reply_text("LLM_Frontend 模式下模型由 /backend 与配置控制。")
            return
        model = MODEL_TIERS[ctx.args[0]] if ctx.args[0] in MODEL_TIERS else ctx.args[0].strip()
        if w is not None:
            w.model = model
        b.save_state()
        await update.message.reply_text(f"«{name}» 模型 → {model or '默认'}（下轮生效）")
        return
    if not (w and w.client):
        await update.message.reply_text(f"«{name}» 会话未就绪")
        return
    if not ctx.args:
        cur = w.model or "默认"
        await update.message.reply_text(
            f"«{name}» 当前模型: {cur}\n"
            f"用法: /model <default|opus|sonnet|haiku|具体模型串>")
        return
    arg = ctx.args[0].strip()
    # 档位别名映射；非别名则当作具体模型串直接透传
    model = MODEL_TIERS[arg] if arg in MODEL_TIERS else arg
    msg = await b.mgr.set_session_model(name, model)
    await update.message.reply_text(msg)



async def cmd_fast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    cur = b.mgr.fast_mode
    if not ctx.args:
        state = "开启 ⚡" if cur else "关闭"
        await update.message.reply_text(
            f"fast mode 当前: {state}\n用法: /fast on | off | toggle")
        return
    arg = ctx.args[0].lower()
    if arg in ("on", "1", "true"):
        target = True
    elif arg in ("off", "0", "false"):
        target = False
    elif arg in ("toggle", "t"):
        target = not cur
    else:
        await update.message.reply_text("用法: /fast on | off | toggle")
        return
    if target == cur:
        await update.message.reply_text(f"fast mode 已是 {'开启' if cur else '关闭'}，无需改动")
        return
    msg = await b.mgr.set_fast_mode(target)
    await update.message.reply_text(msg)


# ── /context：上下文窗口用量分解 ─────────────────────────────────────────
def _bar(pct: float, width: int = 14) -> str:
    fill = max(0, min(width, round(pct / 100 * width)))
    return "█" * fill + "░" * (width - fill)


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


async def cmd_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    w, name, _tid = b.target_for(update)
    if not (w and w.client):
        await update.message.reply_text(f"«{name}» 会话未就绪")
        return
    u = await w.context_usage()
    if not u:
        await update.message.reply_text("拿不到上下文统计（端点可能不支持）")
        return
    total = u.get("totalTokens", 0)
    mx = u.get("maxTokens", 0) or 1
    pct = u.get("percentage", 0.0)
    lines = [
        f"🧮 *上下文用量* «{name}»",
        f"`{_bar(pct)}` {pct:.1f}%",
        f"{_fmt_tokens(total)} / {_fmt_tokens(mx)} tokens · {u.get('model','?')}",
    ]
    cats = [c for c in (u.get("categories") or []) if c.get("tokens", 0) > 0]
    if cats:
        lines.append("")
        for c in sorted(cats, key=lambda x: -x.get("tokens", 0))[:7]:
            lines.append(f"· {c['name']}: {_fmt_tokens(c['tokens'])}")
    if u.get("isAutoCompactEnabled"):
        thr = u.get("autoCompactThreshold")
        lines.append("")
        lines.append(f"自动压缩: 开" + (f"（阈值 {_fmt_tokens(thr)}）" if thr else ""))
    lines.append("\n/compact 可手动压缩历史")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /compact：压缩对话历史 ───────────────────────────────────────────────
async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    w, name, _tid = b.target_for(update)
    if not (w and w.client):
        await update.message.reply_text(f"«{name}» 会话未就绪")
        return
    if w.busy:
        await update.message.reply_text(f"«{name}» 正忙，先 /stop 再压缩")
        return
    note = await update.message.reply_text(f"🗜 正在压缩 «{name}» 的历史…")
    before = await w.context_usage()
    msg = await w.compact()
    after = await w.context_usage()
    tail = ""
    if before and after:
        b0, a0 = before.get("totalTokens", 0), after.get("totalTokens", 0)
        if b0 and a0 < b0:
            tail = f"\n{_fmt_tokens(b0)} → {_fmt_tokens(a0)} tokens"
    b.save_state()
    try:
        await note.edit_text(f"{msg}{tail}")
    except Exception:
        await update.message.reply_text(f"{msg}{tail}")


# ── /providers：列出/切换活跃端点 ────────────────────────────────────────
async def cmd_providers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    r = b.mgr.router
    if r is None:
        await update.message.reply_text("未启用 provider 路由（无 providers.toml）")
        return
    if not ctx.args:
        # 显示列表 + 当前会话用的 provider
        _w, name, _tid = b.target_for(update)
        cur = ""
        w = b.mgr.get(name)
        if w is not None and not w.is_orchestrator:
            cur = f"\n\n当前会话 «{name}» 用: {w.provider or '(全局默认)'}"
        import html as _html
        await update.message.reply_text(
            "<b>🌐 LLM Providers</b>\n<pre>" + _html.escape(r.summary()) + "</pre>"
            + _html.escape(cur) +
            "\n\n<i>/providers &lt;name&gt; 切端点（在 worker 话题里=切该 worker，"
            "General/私聊=切全局）</i>",
            parse_mode=ParseMode.HTML)
        return
    # 按所在话题定位：worker 话题 → 切该 worker；主对话/General/私聊 → 切全局
    w, name, _tid = b.target_for(update)
    prov = ctx.args[0]
    if w is not None and not w.is_orchestrator:
        msg = await b.mgr.set_worker_provider(name, prov)
    else:
        msg = await b.mgr.set_active_provider(prov)
    await update.message.reply_text(msg)


async def cmd_backend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show or switch the active CLI backend in LLM_Frontend."""
    b = _b(ctx)
    base = getattr(b, "llm_frontend_url", "http://127.0.0.1:8799")
    try:
        async with aiohttp.ClientSession() as session:
            if ctx.args:
                async with session.post(
                    f"{base}/backend",
                    json={"name": ctx.args[0]},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    data = await resp.json(content_type=None)
            else:
                async with session.get(
                    f"{base}/backend",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
    except Exception as e:
        await update.message.reply_text(f"LLM_Frontend 不可用: {e}")
        return
    if ctx.args and not data.get("ok", True):
        await update.message.reply_text(str(data.get("error") or data.get("message") or data))
        return
    backend = data.get("backend") or data
    active = backend.get("active") or "unknown"
    available = backend.get("available") or {}
    lines = [f"CLI backend: `{active}`", "", "可用后端:"]
    for name, info in available.items():
        mark = "✓" if info.get("available") else "×"
        mode = info.get("mode") or "-"
        lines.append(f"{mark} `{name}` · {mode}")
    lines.append("")
    lines.append("切换: `/backend claude-code` 或 `/backend codex`")
    if data.get("message"):
        lines.insert(0, data["message"])
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


_PROVIDER_USAGE = (
    "🛠 *配置 LLM Provider*\n"
    "新增/覆盖（key=value，空格分隔）:\n"
    "`/provider add name=backup url=https://x.com token=sk-xxx`\n"
    "可选: `priority=20` `default=1` `opus=claude-opus-4-8` `sonnet=…` `haiku=…` `timeout=60`\n\n"
    "删除: `/provider rm <name>`\n"
    "列表: `/provider list`\n\n"
    "⚠️ 含 token 的那条消息发出后会被*自动删除*以防泄露。"
)


def _parse_kv(args: list[str]) -> dict:
    out = {}
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


async def cmd_provider(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    if b.mgr.router is None:
        await update.message.reply_text("未启用 provider 路由")
        return
    if not ctx.args:
        await update.message.reply_text(_PROVIDER_USAGE, parse_mode=ParseMode.MARKDOWN)
        return
    sub = ctx.args[0].lower()

    if sub == "list":
        await update.message.reply_text("🌐 *Providers*\n" + b.mgr.router.summary(),
                                        parse_mode=ParseMode.MARKDOWN)
        return

    if sub == "rm":
        if len(ctx.args) < 2:
            await update.message.reply_text("用法: /provider rm <name>")
            return
        await update.message.reply_text(b.mgr.remove_provider_spec(ctx.args[1]))
        return

    if sub == "add":
        kv = _parse_kv(ctx.args[1:])
        name = kv.get("name")
        url = kv.get("url") or kv.get("base_url")
        token = kv.get("token") or kv.get("auth_token") or ""
        had_token = bool(token)
        # 含 token：立刻删掉用户原始消息，防止留痕。
        # 注意：Telegram 不允许 bot 删除「私聊里用户发的消息」，只有群里(且有
        # can_delete_messages 权限)能删。删失败必须显式警告，不能谎报已删。
        deleted = False
        if had_token:
            try:
                deleted = await ctx.bot.delete_message(
                    update.effective_chat.id, update.message.message_id)
            except Exception as e:
                log.warning("delete token message failed: %s", e)
                deleted = False
        if not name or not url:
            await ctx.bot.send_message(update.effective_chat.id,
                                       "❌ 至少需要 name= 和 url=。/provider 看用法",
                                       message_thread_id=update.message.message_thread_id)
            return
        models = {k: kv[k] for k in ("opus", "sonnet", "haiku") if kv.get(k)}
        msg = b.mgr.add_provider_spec(
            name=name, base_url=url, auth_token=token,
            priority=int(kv.get("priority", 100)) if kv.get("priority", "").isdigit() else 100,
            make_default=kv.get("default") in ("1", "true", "yes"),
            models=models,
            timeout=int(kv["timeout"]) if kv.get("timeout", "").isdigit() else None,
        )
        if not had_token:
            tokinfo = "（未给 token，将继承环境）"
        elif deleted:
            tokinfo = "（token 已保存，含 token 的原消息已删除）"
        else:
            tokinfo = ("⚠️ *token 已保存，但删不掉你那条原消息*（私聊里 bot 无权删）。\n"
                       "请手动删除上面那条 /provider add 消息；若担心泄露，去服务商吊销并换一个 token。")
        await ctx.bot.send_message(update.effective_chat.id, f"✅ {msg}\n{tokinfo}",
                                   message_thread_id=update.message.message_thread_id,
                                   parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text(_PROVIDER_USAGE, parse_mode=ParseMode.MARKDOWN)


# ── /model_refresh：强制刷新当前活跃 provider 的可用模型清单 ────────────
async def cmd_model_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    b = _b(ctx)
    r = b.mgr.router
    if r is None or not r.providers:
        await update.message.reply_text("未启用 provider 路由")
        return
    cache = getattr(b, "model_cache", None)
    if cache is None:
        await update.message.reply_text("model_cache 未初始化")
        return
    # 决定要刷的 provider：当前会话 worker 用 w.provider；否则全局 active
    w, _name, _tid = b.target_for(update)
    prov_name = (w.provider if (w and not w.is_orchestrator) else None) or r.active
    prov = r.get(prov_name)
    if prov is None:
        await update.message.reply_text(f"未知 provider '{prov_name}'")
        return
    note = await update.message.reply_text(f"🔄 刷新 provider «{prov_name}» 模型清单…")
    entry = await cache.refresh(prov)
    if entry.get("error"):
        msg = f"⚠️ 刷新失败: {entry['error'][:120]}\n回退到静态映射。"
    else:
        models = entry.get("models", [])
        msg = (f"✅ {prov_name} 共 {len(models)} 个可用模型：\n"
               + "\n".join(f"· {m}" for m in models[:30]))
        if len(models) > 30:
            msg += f"\n… 还有 {len(models)-30} 个"
    try:
        await note.edit_text(msg)
    except Exception:
        await update.message.reply_text(msg)



async def _probe_provider(prov, timeout: float = 15.0) -> dict:
    """向单个 provider 发最小 messages 请求，测连通+延迟。"""
    url = prov.base_url.rstrip("/") + "/v1/messages"
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if prov.auth_token:
        headers["x-api-key"] = prov.auth_token
    model = (prov.models or {}).get("haiku") or (prov.models or {}).get("sonnet") or "claude-haiku-4-5-20251001"
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    t0 = time.time()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                latency = time.time() - t0
                body = await resp.text()
                if resp.status == 200:
                    return {"name": prov.name, "ok": True, "ms": int(latency * 1000), "model": model}
                else:
                    short = body[:120].replace("\n", " ")
                    return {"name": prov.name, "ok": False, "ms": int(latency * 1000),
                            "err": f"HTTP {resp.status}: {short}"}
    except asyncio.TimeoutError:
        return {"name": prov.name, "ok": False, "ms": int((time.time() - t0) * 1000), "err": "timeout"}
    except Exception as e:
        return {"name": prov.name, "ok": False, "ms": int((time.time() - t0) * 1000), "err": str(e)[:100]}


async def cmd_testllm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """测试所有 LLM provider 端点可用性（并发 ping）。"""
    b = _b(ctx)
    r = b.mgr.router
    if r is None or not r.providers:
        await update.message.reply_text("未配置 provider（/provider add 添加）")
        return
    note = await update.message.reply_text(f"🔍 正在测试 {len(r.providers)} 个端点…")
    tasks = [_probe_provider(p) for p in r.providers]
    results = await asyncio.gather(*tasks)
    lines = ["🏥 *LLM 端点可用性*\n"]
    all_ok = True
    for res in results:
        if res["ok"]:
            mark = "✅"
            detail = f"{res['ms']}ms · {res.get('model','')}"
        else:
            mark = "❌"
            detail = f"{res['ms']}ms · {res.get('err','')}"
            all_ok = False
        active = " 👈" if res["name"] == r.active else ""
        lines.append(f"{mark} *{res['name']}*{active}\n    {detail}")
    summary = "全部正常 🎉" if all_ok else "存在异常端点 ⚠️"
    lines.append(f"\n{summary}")
    try:
        await note.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# 注册表：(命令名, handler)。bot.py 用 _owner_only 包一层再 add_handler。
LLM_COMMANDS = [
    ("stop", cmd_stop),
    ("mode", cmd_mode),
    ("model", cmd_model),
    ("model_refresh", cmd_model_refresh),
    ("fast", cmd_fast),
    ("context", cmd_context),
    ("compact", cmd_compact),
    ("providers", cmd_providers),
    ("backend", cmd_backend),
    ("provider", cmd_provider),
    ("testllm", cmd_testllm),
]
