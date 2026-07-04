"""monitor.py — 主机 / 构建状态采集，供 TG 命令调用。无外部依赖除 psutil。"""
from __future__ import annotations
import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

import psutil


def _human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024.0:
            return f"{n:3.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}E"


def host_status() -> str:
    """CPU / 内存 / 负载 / 磁盘 一屏摘要。"""
    cpu = psutil.cpu_percent(interval=0.4)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    try:
        load = os.getloadavg()
        load_s = f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"
    except OSError:
        load_s = "n/a"
    boot = time.time() - psutil.boot_time()
    up_h = int(boot // 3600)
    up_m = int((boot % 3600) // 60)
    lines = [
        "🖥 *Host*",
        f"  CPU   : {cpu:.0f}%  ({psutil.cpu_count()} cores)",
        f"  Load  : {load_s}",
        f"  Mem   : {_human(vm.used)}/{_human(vm.total)} ({vm.percent:.0f}%)",
        f"  Swap  : {_human(sw.used)}/{_human(sw.total)} ({sw.percent:.0f}%)",
        f"  Uptime: {up_h}h{up_m:02d}m",
    ]
    return "\n".join(lines)


def disk_status(paths: list[str]) -> str:
    lines = ["💾 *Disk*"]
    seen = set()
    for p in paths:
        try:
            rp = os.path.realpath(p)
            st = os.stat(rp)
            key = st.st_dev
            if key in seen:
                continue
            seen.add(key)
            du = shutil.disk_usage(rp)
            lines.append(
                f"  {p}\n    {_human(du.used)}/{_human(du.total)} "
                f"({du.used / du.total * 100:.0f}% used, {_human(du.free)} free)"
            )
        except OSError as e:
            lines.append(f"  {p}: {e}")
    return "\n".join(lines)


def dir_size(path: str) -> str:
    """du -sh 单目录占用（异步友好的话用 async_run 包）。"""
    rp = os.path.realpath(path)
    if not os.path.isdir(rp):
        return f"{path}: not a dir"
    total = 0
    for root, _dirs, files in os.walk(rp, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return f"{path}: {_human(total)}"


def top_procs(n: int = 8) -> str:
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # prime cpu_percent then sort
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.4)
    rows = []
    for p in procs:
        try:
            info = p.info
            cpu = p.cpu_percent(None)
            rss = info["memory_info"].rss if info["memory_info"] else 0
            rows.append((cpu, rss, info["pid"], (info["name"] or "?")[:18]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    rows.sort(reverse=True)
    lines = ["📊 *Top procs (cpu)*", "```"]
    lines.append(f"{'PID':>7} {'CPU%':>5} {'RSS':>7}  NAME")
    for cpu, rss, pid, name in rows[:n]:
        lines.append(f"{pid:>7} {cpu:>5.0f} {_human(rss):>7}  {name}")
    lines.append("```")
    return "\n".join(lines)


@dataclass
class CmdResult:
    rc: int
    out: str


async def run_cmd(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> CmdResult:
    """跑一条命令，捕获合并输出，带超时。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return CmdResult(proc.returncode or 0, out.decode("utf-8", "replace"))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CmdResult(124, f"(timeout after {timeout}s)")


async def ph_status(repo: str, project: str) -> str:
    """ph status + ph report 摘要。"""
    ph = os.path.join(repo, "build", "ph")
    if not os.path.exists(ph):
        return f"ph not found at {ph}"
    env_proj = {**os.environ}
    r1 = await run_cmd([ph, "status", "--project", project], cwd=repo, timeout=30)
    text = f"🔨 *ph status* (project={project})\n```\n{r1.out.strip()[:1500]}\n```"
    return text


async def ph_report(repo: str, project: str) -> str:
    ph = os.path.join(repo, "build", "ph")
    r = await run_cmd([ph, "report", "--project", project], cwd=repo, timeout=30)
    body = r.out.strip() or "(no report data)"
    return f"📈 *ph report* (project={project})\n```\n{body[:2500]}\n```"


def adb_devices() -> str:
    adb = shutil.which("adb")
    if not adb:
        return "📱 adb not installed"
    try:
        out = subprocess.run(
            [adb, "devices", "-l"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        return f"📱 *adb devices*\n```\n{out[:1200]}\n```"
    except Exception as e:
        return f"📱 adb error: {e}"
