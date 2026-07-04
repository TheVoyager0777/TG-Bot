#!/usr/bin/env python3
"""tg_send_cli.py — 独立 CLI 工具，直接发送文件/通知到 TG bot 的转发群。

用于 phantom build 等外部流程调用，不依赖 bot 运行时上下文。
从 config.toml 读 token + forward_groups 持久化状态。

用法:
  tg_send_cli.py file <target> <path> [caption]
  tg_send_cli.py notify <target> <text> [--pin]
  tg_send_cli.py list

示例:
  tg_send_cli.py file PH_DEV /path/to/build.zip "WAIPIO NoKSU 20260614"
  tg_send_cli.py notify PH_DEV "构建完成 ✅" --pin
  tg_send_cli.py list
"""
import asyncio
import json
import os
import sys
from pathlib import Path

try:
    import tomli as tomllib
except ImportError:
    import tomllib

from telegram import Bot


# ─── 配置路径 ───────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "config.toml"
STATE_FILE = BOT_DIR / "tg_forward_state.json"  # 新增: forward_groups 持久化


# ─── 加载配置 ───────────────────────────────────────────────────────────────
def load_config():
    """从 config.toml 读 bot token + owner/group"""
    if not CONFIG_FILE.exists():
        print(f"错误: 找不到 {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_FILE, "rb") as f:
        cfg = tomllib.load(f)
    # config.toml 格式: [telegram] 段
    tg_cfg = cfg.get("telegram", {})
    token = tg_cfg.get("token", "")
    owner_id = tg_cfg.get("owner_id", 0)
    group_id = tg_cfg.get("group_chat_id", 0)
    if not token:
        print("错误: config.toml 缺少 [telegram] token", file=sys.stderr)
        sys.exit(1)
    return token, owner_id, group_id


def load_forward_groups():
    """从持久化文件读 forward_groups (别名 -> chat_id)"""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        # 格式: {"forward_groups": {"PH_DEV": "-1001234567890", ...}}
        return data.get("forward_groups", {})
    except Exception as e:
        print(f"警告: 读取 {STATE_FILE} 失败: {e}", file=sys.stderr)
        return {}


def save_forward_groups(groups: dict):
    """持久化 forward_groups 到文件"""
    data = {"forward_groups": groups}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def resolve_target(alias: str, forward_groups: dict) -> int:
    """别名 -> chat_id，不存在返回 0"""
    if not alias:
        return 0
    # 大小写不敏感匹配
    for name, cid_str in forward_groups.items():
        if name.lower() == alias.lower():
            return int(cid_str)
    # 尝试直接解析为 chat_id
    try:
        cid = int(alias)
        if str(cid) in forward_groups.values():
            return cid
    except ValueError:
        pass
    return 0


# ─── 发送逻辑 ───────────────────────────────────────────────────────────────
async def send_file(token: str, chat_id: int, file_path: str, caption: str = ""):
    """发送文件到指定 chat_id"""
    if not os.path.isfile(file_path):
        print(f"错误: 文件不存在: {file_path}", file=sys.stderr)
        sys.exit(1)

    size = os.path.getsize(file_path)
    if size > 50 * 1024 * 1024:
        print(f"错误: 文件 {size} 字节超过 TG 50MB 限制", file=sys.stderr)
        sys.exit(1)

    bot = Bot(token=token)

    # 动态超时: 默认按 ~85KB/s 的慢上行估算 (实测慢链路约此速率), 90-1800s 范围。
    # TG_SEND_TIMEOUT (秒) 可显式覆盖 write/read 超时, 用于极慢上行的大文件。
    write_timeout = max(90.0, min(1800.0, size / (85 * 1024)))
    env_to = os.environ.get("TG_SEND_TIMEOUT")
    if env_to:
        try:
            write_timeout = max(write_timeout, float(env_to))
        except ValueError:
            pass
    conn_timeout = max(60.0, min(300.0, size / (1024 * 1024)))

    cap = f"{os.path.basename(file_path)}\n{caption}" if caption else os.path.basename(file_path)

    print(f"发送文件: {file_path} ({size / 1024 / 1024:.1f} MB)")
    print(f"目标 chat_id: {chat_id}")

    with open(file_path, "rb") as fh:
        msg = await bot.send_document(
            chat_id,
            document=fh,
            filename=os.path.basename(file_path),
            caption=cap[:1024],
            write_timeout=write_timeout,
            read_timeout=write_timeout,
            connect_timeout=conn_timeout,
            pool_timeout=conn_timeout,
        )

    print(f"✅ 已发送 (message_id: {msg.message_id})")
    return msg.message_id


async def send_notification(token: str, chat_id: int, text: str, pin: bool = False):
    """发送文本通知到指定 chat_id"""
    bot = Bot(token=token)

    print(f"发送通知到 chat_id: {chat_id}")
    print(f"内容: {text[:100]}...")

    msg = await bot.send_message(chat_id, text[:4096])

    if pin:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        print(f"✅ 已发送并置顶 (message_id: {msg.message_id})")
    else:
        print(f"✅ 已发送 (message_id: {msg.message_id})")

    return msg.message_id


# ─── CLI 入口 ───────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    token, owner_id, group_id = load_config()
    forward_groups = load_forward_groups()

    if cmd == "list":
        if not forward_groups:
            print("无已注册转发群")
            print(f"提示: 用 'tg_send_cli.py register <别名> <chat_id>' 注册")
        else:
            print("已注册转发群:")
            for alias, cid in forward_groups.items():
                print(f"  {alias} → {cid}")
        sys.exit(0)

    if cmd == "register":
        if len(sys.argv) < 4:
            print("用法: tg_send_cli.py register <别名> <chat_id>")
            sys.exit(1)
        alias = sys.argv[2]
        chat_id = sys.argv[3]
        forward_groups[alias] = chat_id
        save_forward_groups(forward_groups)
        print(f"✅ 已注册: {alias} → {chat_id}")
        sys.exit(0)

    if cmd == "file":
        if len(sys.argv) < 4:
            print("用法: tg_send_cli.py file <target> <path> [caption]")
            sys.exit(1)
        target = sys.argv[2]
        file_path = sys.argv[3]
        caption = sys.argv[4] if len(sys.argv) > 4 else ""

        chat_id = resolve_target(target, forward_groups)
        if chat_id == 0:
            print(f"错误: 未找到转发群 '{target}'", file=sys.stderr)
            print(f"已注册: {', '.join(forward_groups.keys()) or '(无)'}", file=sys.stderr)
            sys.exit(1)

        asyncio.run(send_file(token, chat_id, file_path, caption))

    elif cmd == "notify":
        if len(sys.argv) < 4:
            print("用法: tg_send_cli.py notify <target> <text> [--pin]")
            sys.exit(1)
        target = sys.argv[2]
        text = sys.argv[3]
        pin = "--pin" in sys.argv

        chat_id = resolve_target(target, forward_groups)
        if chat_id == 0:
            print(f"错误: 未找到转发群 '{target}'", file=sys.stderr)
            print(f"已注册: {', '.join(forward_groups.keys()) or '(无)'}", file=sys.stderr)
            sys.exit(1)

        asyncio.run(send_notification(token, chat_id, text, pin))

    else:
        print(f"错误: 未知命令 '{cmd}'", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
