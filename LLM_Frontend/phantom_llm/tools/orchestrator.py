"""
orchestrator_tools.py — 主对话(orchestrator)用的 in-process MCP 工具。

主对话靠这些工具充当"中间人/调度者"：它能 spawn 新 worker、把任务派给 worker、
收集 worker 的输出、列出/停止 worker。每个工具就是一个跑在本进程内的 async 函数。

设计要点：
- send_to_worker 是阻塞式：等该 worker 把这轮 prompt 跑完，把完整输出当工具结果
  返回给主对话，于是主对话能基于结果继续推理（真正的 agent team 协作）。
- spawn_worker 只建会话不派活（或带 initial prompt 顺手派一轮）。
- worker 的流式过程也会实时镜像到 TG（由 manager.notify_cb 推送），用户能旁观。
"""
from __future__ import annotations

import asyncio
import logging
from claude_agent_sdk import create_sdk_mcp_server, tool
from phantom_llm.session import TurnSink

log = logging.getLogger("tgclaude.orch")

# send_to_worker / spawn_worker 内 w.run() 上限。超过仍没返回 = 上游半挂或
# CLI 卡死，强行 cancel 让 orchestrator 拿到错误响应继续推理，不无限阻塞。
WORKER_RUN_TIMEOUT = 600

ORCH_SYSTEM_PROMPT = """\
你是一个 worker 班组的协调者（orchestrator），通过 Telegram 跟唯一的主人对话。

⚠️ 先分清两套「团队 / agent team」概念，全程别混用：
1) 「班组 worker team」——本系统的 worker：由你用下面的 `team` MCP 工具创建的
   *独立常驻 Claude 会话*，每个有自己的上下文，（论坛模式下）有自己的 Telegram 话题，
   主人能单独旁观/直连。worker 之间还能用 peer 工具互发消息协作。这是你调度的对象。
2) 「Claude Code 原生 subagent / Task」——SDK 内置的临时子代理：从某个会话自身上下文
   派生、干完一件活即弃、产出回灌那一轮。每个 worker 内部可用 Task 自行 fan-out，
   但那是 worker 自己的事，不归你用 team 工具管。
简言之：你管的是「班组 worker」（长期、独立会话）；原生 subagent 是 worker 内部的零活并行。

你手里的 `team` MCP 工具，用来创建并调度多个独立的 worker 会话来并行干活：

- spawn_worker(name, task): 新建一个常驻 worker。给了 task 就让它立刻开干并返回首轮结果。
- send_to_worker(name, message): 把消息发给已存在的 worker，等它跑完，拿回完整输出（阻塞）。
- dispatch_worker(name, message): 派活但【不等】它跑完（非阻塞）——让 worker 后台独立跑、
  你立即回到跟主人对话。要真正并行多个 worker、或派完继续聊就用它，稍后 peek_worker 取结果。
- list_workers(): 看当前所有 worker 的状态（忙/闲、轮次、最近输出片段）。
- peek_worker(name): 看某 worker 最近一次的完整输出。
- stop_worker(name): 关掉一个 worker。

worker 之间的对等协作（你不必事事居中转发）：
- 每个 worker 都有 message_worker / list_peers / peek_peer（peer 工具）能直接给同伴发消息。
- 所以可以让 A worker 把中间结果直接丢给 B worker，不用绕回你。投递是非阻塞的。

provider 路由（如果配了多个 LLM 端点）：
- list_providers(): 看有哪些 LLM 端点、当前活跃哪个。
- set_active_provider(provider): 切全局默认端点（主对话重连）。
- set_worker_provider(name, provider): 把某 worker 切到指定端点（该 worker 重连、上下文重置）。
- spawn_worker 可带 provider= 让新 worker 用指定端点（如重活走强端点、杂活走便宜端点）。

共享记忆（跨 worker 文件改动台账，防止并行踩同一处）：
- check_file_history(path): 派活前查这文件是否别的 worker 改过，避免覆盖彼此工作。
- recent_edits(limit, worker, path_prefix): 看最近改动全景，可按 worker 或路径前缀过滤。
- 注意：每个 worker 改文件时系统会自动记录；它要改的文件若近期被他人动过，会自动收到预警。
  你派并行任务前，建议先 recent_edits / check_file_history 看看，把可能冲突的任务串行化或分区。

文件发送（给主人发文件到 Telegram）：
- send_file(path): 把服务器上的文件发送给主人（≤50MB）。适用于构建产物、日志、截图等需要主人接收的文件。
- send_file 还有可选 target 参数：默认发给主人；填某个「文件转发群」的别名则把文件单向转发到那个群。
- send_notification(text, target?, pin?): 发文本通知（构建完成/失败、公告等）。target 选转发群别名=发到那个群，pin=true 顺手置顶。
- pin_message(message_id, target?, unpin?): 置顶/取消置顶某条已发消息（message_id 来自 send_notification 返回）。
- list_forward_targets(): 看当前登记了哪些文件转发群（别名）。转发群只收文件/通知、群成员不参与对话，全由主人控制。
- 这个能力你和每个 worker 都有（独立的 `file` MCP 工具，经 tool search 可发现）。
  对外播报/发布有专职的 `relay` worker（自带话题）：需要对外通知时可 send_to_worker("relay", ...) 让它去发。
  复核与裁决有专职的 `MAGI` worker（自带话题）：内部持 MELCHIOR-1 / BALTHASAR-2 / CASPER-3
  三脑做并行独立判断+多数决。需要严肃复核（代码/方案是否能上、风险评估、跨视角争议）时
  send_to_worker("MAGI", "议题：...") 由它做三脑表决并给出报告。
  所以派活时若产物需要回传给主人，可以直接让 worker 自己 send_file，不必把文件路径绕回你再发。

工作方式：
- 你是中间人。简单的问题自己答；需要动手干活、或要并行/隔离上下文时，派给班组 worker。
- 给 worker 起有意义的名字（如 build、kernel-audit、device-test）。
- 把大任务拆成子任务分给不同 worker，再汇总他们的结果回报给主人。可让 worker 之间用
  message_worker 直接交换中间结果，不必事事绕回你。
- 单个 worker 内部要并行处理一堆零活（搜一片文件、读多个模块）时，提醒它用 Claude Code
  原生 Task 子代理自行 fan-out，而不是为这种零活再开一个班组 worker。
- worker 跑长任务时，你可以先把任务派出去，然后继续跟主人对话，稍后用 peek/list 查进度。
- 回报时用中文，简洁。worker 的原始输出已经实时镜像给主人看了，你只需要给结论和下一步。
"""


def build_orchestrator_server(manager):
    """用闭包把 SessionManager 绑进各工具。返回 McpSdkServerConfig。"""

    async def _mirror(worker_name: str, kind: str, text: str):
        if manager.notify_cb:
            await manager.notify_cb(worker_name, kind, text)

    def _make_sink(worker_name: str) -> TurnSink:
        async def on_start():
            await _mirror(worker_name, "start", "")
        async def on_text(t: str):
            await _mirror(worker_name, "text", t)
        async def on_event(t: str):
            await _mirror(worker_name, "event", t)
        async def on_done(msg):
            # 轮结束：定格该 worker 的镜像气泡（否则心跳无限期续编辑）
            await _mirror(worker_name, "done", msg)
        return TurnSink(on_text=on_text, on_event=on_event,
                        on_start=on_start, on_done=on_done)

    @tool("spawn_worker", "新建一个 worker 会话。可选 task：给了就立刻开干并返回首轮结果。可选 provider：指定该 worker 用哪个 LLM 端点。",
          {"name": str, "task": str, "provider": str})
    async def spawn_worker(args):
        name = (args.get("name") or "").strip()
        task = (args.get("task") or "").strip()
        provider = (args.get("provider") or "").strip() or None
        if not name:
            return {"content": [{"type": "text", "text": "error: name 必填"}]}
        try:
            w = await manager.spawn_worker(name, provider=provider)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}]}
        ptxt = f"（provider={provider}）" if provider else ""
        await _mirror(name, "spawn", f"worker '{name}' 已创建{ptxt}")
        if not task:
            return {"content": [{"type": "text", "text": f"worker '{name}' 已创建（空闲）{ptxt}"}]}
        try:
            out = await asyncio.wait_for(w.run(task, _make_sink(name)),
                                         timeout=WORKER_RUN_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("spawn_worker first turn timeout: %s", name)
            return {"content": [{"type": "text",
                "text": f"error: worker '{name}' 首轮 {WORKER_RUN_TIMEOUT}s 内未结束（上游可能挂）；用 stop_worker 清理或换 provider"}]}
        except Exception as e:
            log.exception("spawn_worker first turn failed: %s", name)
            return {"content": [{"type": "text",
                "text": f"worker '{name}' 已创建但首轮失败: {e}\n用 send_to_worker 重发或 stop_worker 清理"}]}
        manager.save_state()  # 首轮后 session_id 已生成，存盘可 resume
        await manager.drain_peer_inbox(name)  # 首轮间若有同伴消息进来，接着派
        return {"content": [{"type": "text", "text": f"[{name}] 首轮输出:\n{out[:4000]}"}]}

    @tool("send_to_worker", "把消息发给已存在的 worker，等它跑完，返回完整输出。",
          {"name": str, "message": str})
    async def send_to_worker(args):
        name = (args.get("name") or "").strip()
        message = args.get("message") or ""
        w = manager.get(name)
        if not w or w.is_orchestrator:
            return {"content": [{"type": "text", "text": f"error: 没有 worker '{name}'"}]}
        if w.busy:
            return {"content": [{"type": "text", "text": f"worker '{name}' 正忙，稍后用 peek_worker 查结果"}]}
        try:
            out = await asyncio.wait_for(w.run(message, _make_sink(name)),
                                         timeout=WORKER_RUN_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("send_to_worker timeout: %s", name)
            return {"content": [{"type": "text",
                "text": f"error: worker '{name}' {WORKER_RUN_TIMEOUT}s 内未结束（上游可能挂）；用 peek_worker 看片段或 stop_worker 重建"}]}
        except Exception as e:
            log.exception("send_to_worker run failed: %s", name)
            return {"content": [{"type": "text",
                "text": f"error: worker '{name}' 跑挂了: {e}\n（CLI 可能 SIGTERM/上游超时；用 stop_worker+spawn 重建或换 provider）"}]}
        manager.save_state()
        await manager.drain_peer_inbox(name)  # 这轮间若有同伴消息进来，接着派
        return {"content": [{"type": "text", "text": f"[{name}] 输出:\n{out[:4000]}"}]}

    @tool("dispatch_worker",
          "把任务派给 worker 但【不等它跑完】（非阻塞，fire-and-forget）：立即返回，"
          "worker 在后台独立跑、输出实时镜像到它自己的话题。适合让多个 worker 真正并行、"
          "或你想派完活继续跟主人对话。稍后用 peek_worker/list_workers 看进度与结果。",
          {"name": str, "message": str})
    async def dispatch_worker(args):
        name = (args.get("name") or "").strip()
        message = args.get("message") or ""
        w = manager.get(name)
        if not w or w.is_orchestrator:
            return {"content": [{"type": "text", "text": f"error: 没有 worker '{name}'"}]}
        if w.busy:
            return {"content": [{"type": "text",
                "text": f"worker '{name}' 正忙；这条会被丢弃。等它闲了或换个 worker。"}]}

        async def _bg():
            try:
                await asyncio.wait_for(w.run(message, _make_sink(name)),
                                       timeout=WORKER_RUN_TIMEOUT)
            except Exception as e:
                log.warning("dispatch_worker bg run failed (%s): %s", name, e)
            finally:
                manager.save_state()
                await manager.drain_peer_inbox(name)

        asyncio.create_task(_bg())
        return {"content": [{"type": "text",
            "text": f"已把任务派给 «{name}»（后台独立跑，不阻塞你）。"
                    f"稍后 peek_worker('{name}') 看结果。"}]}

    @tool("list_workers", "列出所有 worker 及其状态。", {})
    async def list_workers(args):
        ws = manager.list_workers()
        if not ws:
            return {"content": [{"type": "text", "text": "当前没有 worker"}]}
        lines = []
        for w in ws:
            st = "忙" if w["busy"] else "闲"
            lines.append(f"- {w['name']} [{st}] turns={w['turns']} idle={w['idle_s']}s sid={w['session_id']}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool("peek_worker", "查看某 worker 最近一次的完整输出。", {"name": str})
    async def peek_worker(args):
        name = (args.get("name") or "").strip()
        w = manager.get(name)
        if not w or w.is_orchestrator:
            return {"content": [{"type": "text", "text": f"error: 没有 worker '{name}'"}]}
        body = w.last_output[-4000:] or "(还没有输出)"
        st = "忙" if w.busy else "闲"
        return {"content": [{"type": "text", "text": f"[{name}] ({st}) 最近输出:\n{body}"}]}

    @tool("stop_worker", "关闭一个 worker 会话。", {"name": str})
    async def stop_worker(args):
        name = (args.get("name") or "").strip()
        ok = await manager.stop_worker(name)
        await _mirror(name, "event", f"worker '{name}' 已关闭" if ok else f"没有 worker '{name}'")
        if ok:
            await _mirror(name, "close", "")  # 论坛模式下关闭对应话题
        return {"content": [{"type": "text", "text": f"{'已关闭' if ok else '不存在'}: {name}"}]}

    @tool("list_providers", "列出可用的 LLM provider（端点）及当前活跃项。", {})
    async def list_providers(args):
        if manager.router is None:
            return {"content": [{"type": "text", "text": "未启用 provider 路由（无 providers.toml）"}]}
        return {"content": [{"type": "text", "text": manager.router.summary()}]}

    @tool("set_active_provider", "切换全局活跃 provider（主对话重连生效；新建 worker 默认沿用）。",
          {"provider": str})
    async def set_active_provider(args):
        msg = await manager.set_active_provider((args.get("provider") or "").strip())
        return {"content": [{"type": "text", "text": msg}]}

    @tool("set_worker_provider", "把某个 worker 切到指定 provider（该 worker 重连，上下文重置）。",
          {"name": str, "provider": str})
    async def set_worker_provider(args):
        msg = await manager.set_worker_provider(
            (args.get("name") or "").strip(), (args.get("provider") or "").strip())
        return {"content": [{"type": "text", "text": msg}]}

    # ---- 共享记忆：跨 worker 文件改动台账 ----
    def _fmt_recs(recs, now):
        import time as _t
        if now is None:
            now = _t.time()
        lines = []
        for r in recs:
            age = now - r.ts
            a = (f"{int(age)}s" if age < 60 else f"{int(age/60)}m" if age < 3600
                 else f"{int(age/3600)}h" if age < 86400 else f"{int(age/86400)}d")
            lines.append(f"  {r.path} ← {r.worker} ({r.op}, {a}前)")
        return "\n".join(lines)

    @tool("check_file_history",
          "查询某文件是否被其他 worker 改过（避免并行踩同一处）。返回改动历史。",
          {"path": str})
    async def check_file_history(args):
        if manager.shared_mem is None:
            return {"content": [{"type": "text", "text": "未启用共享记忆"}]}
        import time as _t
        path = (args.get("path") or "").strip()
        recs = manager.shared_mem.who_touched(path)
        if not recs:
            return {"content": [{"type": "text", "text": f"{path}：无任何 worker 改动记录"}]}
        return {"content": [{"type": "text",
                "text": f"{path} 改动历史（{len(recs)} 次）：\n" + _fmt_recs(recs, _t.time())}]}

    @tool("recent_edits",
          "查看最近的文件改动（所有 worker）。可选 worker 名过滤、path 前缀过滤。",
          {"limit": int, "worker": str, "path_prefix": str})
    async def recent_edits(args):
        if manager.shared_mem is None:
            return {"content": [{"type": "text", "text": "未启用共享记忆"}]}
        import time as _t
        lim = int(args.get("limit") or 20)
        pref = (args.get("path_prefix") or "").strip()
        wk = (args.get("worker") or "").strip()
        if pref:
            recs = manager.shared_mem.search(pref, limit=lim)
        else:
            recs = manager.shared_mem.recent(limit=lim, worker=wk or None)
        if not recs:
            return {"content": [{"type": "text", "text": "暂无改动记录"}]}
        return {"content": [{"type": "text",
                "text": f"最近 {len(recs)} 条改动：\n" + _fmt_recs(recs, _t.time())}]}

    # ---- 文件发送：见 file_tools.py 的独立 `file` server ----
    # send_file 已抽到 file_tools.build_file_server，同时注入 orchestrator 和 worker，
    # 这样主对话和所有 worker 都能（经 tool search 发现并）发文件给主人，不再 team 专属。

    @tool("list_forward_targets",
          "列出已登记的「文件转发群」别名（send_file 的 target 可填这些；转发群只收文件）。", {})
    async def list_forward_targets(args):
        fn = getattr(manager, "list_forward_targets", None)
        targets = fn() if fn else {}
        if not targets:
            return {"content": [{"type": "text",
                "text": "未登记任何文件转发群（send_file 不带 target=只发给主人）。"
                        "主人可用 /forward here 在目标群登记。"}]}
        lines = [f"- {alias}  (chat_id={cid})" for cid, alias in targets.items()]
        return {"content": [{"type": "text",
                "text": "已登记的文件转发群（send_file 的 target 用别名）：\n" + "\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="team",
        version="1.0.0",
        tools=[spawn_worker, send_to_worker, dispatch_worker, list_workers, peek_worker,
               stop_worker, list_providers, set_active_provider, set_worker_provider,
               check_file_history, recent_edits, list_forward_targets],
    )
