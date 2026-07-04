"""
peer_tools.py — worker 之间的「对等通信」MCP 工具（每个 worker 各持一份）。

语义区分（重要，避免与 Claude Code 原生概念混淆）：
- 这里的「班组同伴 / peer worker」= 本 bot 在 Telegram 侧 spawn 的另一个常驻 Claude
  会话（独立上下文、独立话题）。worker 之间靠 message_worker 互发消息协作。
- Claude Code 原生的「subagent / Task」= 由 SDK 的 agents= 提供、worker 内部自己
  fan-out 的临时子代理（共享本 worker 的上下文派生），用 Task 工具触发，不在这里。

投递是「非阻塞」的：message_worker 把消息丢进对方收件箱即返回，绝不等对方跑完
（否则 A→B、B→A 互等会死锁）。对方空闲就立刻处理，忙就排在它当前 turn 之后。
若需要拿对方的产出，过一会用 peek_peer 看，或让对方处理完用 message_worker 回你。
"""
from __future__ import annotations

import logging
from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger("tgclaude.peer")


def build_peer_server(manager, owner: str):
    """给名为 owner 的 worker 造一份对等通信工具集。闭包绑定 manager + 自身名字。"""

    @tool("message_worker",
          "给班组里另一个同伴 worker 发一条消息（非阻塞：丢进对方收件箱即返回，不等回执）。"
          "对方空闲会立即处理、忙则排到其当前 turn 之后。需要对方产出就稍后用 peek_peer 看，"
          "或请对方处理完 message_worker 回你。\n"
          "特殊：name='main' 表示发给主对话(orchestrator)——主人在主对话里会把你的消息"
          "作为下一轮 prompt 收到（或主对话忙时入队）。要主动告知主人重要事项就用这个。\n"
          "注意：这是 worker 之间的对等通信，不是 Claude Code 原生 subagent（那是你内部用 Task fan-out 的临时子代理）。",
          {"name": str, "message": str})
    async def message_worker(args):
        target = (args.get("name") or "").strip()
        message = args.get("message") or ""
        if not target:
            return {"content": [{"type": "text", "text": "error: name 必填（同伴 worker 名 或 'main'）"}]}
        if not message.strip():
            return {"content": [{"type": "text", "text": "error: message 不能为空"}]}
        msg = await manager.post_peer_message(owner, target, message)
        return {"content": [{"type": "text", "text": msg}]}

    @tool("list_peers",
          "列出班组里其他同伴 worker（可作为 message_worker 的对象）及其忙闲状态。"
          "也会列出『main』（主对话 orchestrator）作为可投递目标——给 main 发就是在跟主人说话。"
          "不含你自己。", {})
    async def list_peers(args):
        peers = manager.list_peers(exclude=owner)
        lines = []
        # 把主对话作为可达目标列在最前面（除非 owner 自己就是 orchestrator）
        if owner != manager.ORCH:
            orch = manager.orchestrator
            if orch is not None:
                st = "忙" if orch.busy else "闲"
                lines.append(f"- main [{st}]  ← 主对话(orchestrator)，发给它就是跟主人说话")
        for p in peers:
            lines.append(f"- {p['name']} [{'忙' if p['busy'] else '闲'}] idle={p['idle_s']}s")
        if not lines:
            return {"content": [{"type": "text", "text": "目前没有其他可达目标。"}]}
        return {"content": [{"type": "text", "text": "可达目标：\n" + "\n".join(lines)}]}

    @tool("peek_peer",
          "查看某个同伴 worker（或主对话 'main'）最近一次的完整输出。",
          {"name": str})
    async def peek_peer(args):
        target = (args.get("name") or "").strip()
        if target == "main" or target == manager.ORCH:
            w = manager.orchestrator
            if w is None or owner == manager.ORCH:
                return {"content": [{"type": "text", "text": "error: 主对话未就绪或你就是主对话"}]}
            body = w.last_output[-4000:] or "(还没有输出)"
            st = "忙" if w.busy else "闲"
            return {"content": [{"type": "text",
                    "text": f"[main] ({st}) 主对话最近输出:\n{body}"}]}
        w = manager.get(target)
        if w is None or w.is_orchestrator or target == owner:
            return {"content": [{"type": "text", "text": f"error: 没有同伴 worker '{target}'"}]}
        body = w.last_output[-4000:] or "(还没有输出)"
        st = "忙" if w.busy else "闲"
        return {"content": [{"type": "text",
                "text": f"[{target}] ({st}) 最近输出:\n{body}"}]}

    return create_sdk_mcp_server(
        name="peer",
        version="1.0.0",
        tools=[message_worker, list_peers, peek_peer],
    )
