"""
file_tools.py — 文件传输能力的 in-process MCP server。

把 send_file 从 orchestrator 专属的 team server 里独立出来，做成一个轻量 `file`
server，同时注入给 orchestrator 和每个 worker —— 这样主对话和所有 worker 都能
（在 ENABLE_TOOL_SEARCH 下经 tool search 发现并）调用「发文件给主人」的能力。

工具走 manager.send_file_cb 回调桥接到 BotApp → Telegram。
"""
from __future__ import annotations

import logging
import os

from claude_agent_sdk import create_sdk_mcp_server, tool

try:
    from telegram.error import TimedOut
except Exception:  # telegram 未装时不至于 import 崩
    class TimedOut(Exception):
        pass

log = logging.getLogger("tgclaude.filetools")

MAX_TG_BYTES = 50 * 1024 * 1024  # Telegram bot 上传上限 50MB


def build_file_server(manager):
    """返回一个只含 send_file 的 MCP server。manager 提供 send_file_cb 桥接。"""

    @tool(
        "send_file",
        "把服务器上的文件发送给主人的 Telegram（≤50MB）。用于发送构建产物、"
        "日志、截图、报告等需要主人直接接收的文件。传绝对路径最稳妥。"
        "可选 target：默认发给主人（主控）；填某个已注册转发群的别名/ID 则把文件"
        "单向转发到那个群（其他群只接收文件，不参与对话）。"
        "重要：本工具一次返回即为终态——返回文本以「已发送」开头即成功，以"
        "「error:」开头即参数错误（改正后才重试）。大文件上传可能在底层超时但"
        "其实已送达，此时返回「已提交」开头的提示，切勿自动重发同一文件。",
        {"path": str, "caption": str, "target": str},
    )
    async def send_file(args):
        path = (args.get("path") or "").strip()
        caption = (args.get("caption") or "").strip()
        target = (args.get("target") or "").strip() or None
        if not path:
            return {"content": [{"type": "text", "text": "error: path 必填，请勿重试"}]}
        if not os.path.isfile(path):
            return {"content": [{"type": "text",
                "text": f"error: 文件不存在: {path}（请勿重试，先确认路径）"}]}
        size = os.path.getsize(path)
        if size > MAX_TG_BYTES:
            return {"content": [{"type": "text",
                "text": f"error: 文件 {size} 字节超过 TG 50MB 限制（请勿重试）"}]}
        if size == 0:
            return {"content": [{"type": "text", "text": "error: 文件为空（请勿重试）"}]}
        cb = manager.send_file_cb
        if cb is None:
            return {"content": [{"type": "text",
                "text": "error: send_file 回调未注册（bot 层未注入，请勿重试）"}]}
        dest = f"转发群「{target}」" if target else "主人"
        try:
            await cb(path, caption, target)
            return {"content": [{"type": "text",
                "text": f"已发送: {os.path.basename(path)} ({size} 字节) → {dest}。"
                        "已送达，无需重发。"}]}
        except ValueError as e:
            # 目标不存在等参数问题：明确告知不要重试同一目标
            return {"content": [{"type": "text",
                "text": f"error: {e}（请勿重试同一目标）"}]}
        except TimedOut as e:
            # 大文件上传的超时多半是「已送达但响应慢」——Telegram 服务端通常已收下。
            # 明确告诉调用方这不是失败，不要自动重发（重发只会让对端收到重复文件）。
            log.warning("send_file timed out (likely delivered): %s", e)
            return {"content": [{"type": "text",
                "text": f"已提交: {os.path.basename(path)} ({size} 字节) → {dest}。"
                        "上传超时但文件很可能已送达，请勿自动重发；"
                        "如主人确认未收到，再由主人指示重试。"}]}
        except Exception as e:
            log.warning("send_file failed: %s", e)
            return {"content": [{"type": "text",
                "text": f"error: 发送失败: {e}。这是一次性发送结果，"
                        "请勿连续自动重试；如需重发请等主人指示。"}]}

    @tool(
        "send_notification",
        "发一条文本通知到 Telegram。用于构建完成/失败、发布公告、状态播报等。"
        "可选 target：默认发给主人（主控）；填某个已登记转发群的别名/ID 则单向发到那个群。"
        "可选 pin=true：发完顺手把这条消息置顶到目标群（bot 需有置顶权限）。"
        "返回里含 message_id，可用 pin_message 工具后续置顶/取消置顶。"
        "重要：本工具一次返回即为终态，「已发送」开头即成功，请勿自动重发。",
        {"text": str, "target": str, "pin": bool},
    )
    async def send_notification(args):
        text = (args.get("text") or "").strip()
        target = (args.get("target") or "").strip() or None
        pin = bool(args.get("pin"))
        if not text:
            return {"content": [{"type": "text", "text": "error: text 必填，请勿重试"}]}
        cb = manager.send_notification_cb
        if cb is None:
            return {"content": [{"type": "text",
                "text": "error: 通知回调未注册（bot 层未注入，请勿重试）"}]}
        dest = f"转发群「{target}」" if target else "主人"
        try:
            mid = await cb(text, target)
        except ValueError as e:
            return {"content": [{"type": "text",
                "text": f"error: {e}（请勿重试同一目标）"}]}
        except Exception as e:
            log.warning("send_notification failed: %s", e)
            return {"content": [{"type": "text",
                "text": f"error: 通知发送失败: {e}。请勿连续自动重试。"}]}
        extra = ""
        if pin and manager.pin_cb is not None:
            try:
                extra = "；" + await manager.pin_cb(target, mid, False)
            except Exception as e:
                extra = f"；置顶失败: {e}"
        return {"content": [{"type": "text",
            "text": f"已发送通知 → {dest} (message_id={mid}){extra}。已送达，无需重发。"}]}

    @tool(
        "pin_message",
        "把已发出的某条消息置顶（或取消置顶）。message_id 来自 send_notification 的返回。"
        "可选 target：与发该消息时一致（默认主控；填转发群别名/ID 则操作那个群）。"
        "可选 unpin=true：取消置顶而非置顶。bot 需在目标群有置顶权限。",
        {"message_id": int, "target": str, "unpin": bool},
    )
    async def pin_message(args):
        mid = args.get("message_id")
        target = (args.get("target") or "").strip() or None
        unpin = bool(args.get("unpin"))
        if not mid:
            return {"content": [{"type": "text", "text": "error: message_id 必填，请勿重试"}]}
        cb = manager.pin_cb
        if cb is None:
            return {"content": [{"type": "text",
                "text": "error: 置顶回调未注册（bot 层未注入，请勿重试）"}]}
        try:
            res = await cb(target, int(mid), unpin)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"error: {e}（请勿重试同一目标）"}]}
        except Exception as e:
            log.warning("pin_message failed: %s", e)
            return {"content": [{"type": "text", "text": f"error: 操作失败: {e}"}]}
        return {"content": [{"type": "text", "text": res}]}

    return create_sdk_mcp_server(name="file", version="1.0.0",
                                 tools=[send_file, send_notification, pin_message])
