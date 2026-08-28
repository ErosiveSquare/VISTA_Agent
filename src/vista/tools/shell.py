"""bash 工具。

要点：
    - 独立进程组启动，超时时杀掉整个进程组（否则子进程会变成孤儿继续跑）
    - 输出按"头 40% + 尾 60%"截断：报错信息通常在末尾，尾部权重更高
    - 只读命令白名单决定 reclaimable：只有幂等无副作用的命令，
      其结果才允许在上下文压缩时被丢弃并留锚点
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

from ..errors import BlockedCommand, PermissionDenied, hint_for
from ..safety.permission import check_blocked, describe_call, is_readonly_command
from ..types import Anchor, ToolResult
from ..util.paths import git_dirty_files, resolve_safe, truncate_head_tail
from ..util.text import one_line
from .context import ToolContext
from .registry import tool


@tool(category="execute", mutating=True)
def bash(ctx: ToolContext, cmd: str, timeout: int = 120, cwd: str = "") -> ToolResult:
    """在工作区中执行一条 shell 命令，返回合并后的标准输出与错误输出。

    用它来运行测试、安装依赖、查看 git 状态、执行构建等。
    注意：每次调用都是一个独立的 shell，cd 不会在多次调用之间保持。
    需要切换目录时请使用 cwd 参数，或在一条命令里用 && 串联。

    Args:
        cmd: 要执行的 shell 命令
        timeout: 超时秒数，超时后整个进程组会被终止
        cwd: 执行目录，相对于工作区根目录；留空表示工作区根目录
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return ToolResult.err("bash", "BAD_ARGS", "cmd 不能为空。")

    blocked, why = check_blocked(cmd)
    if blocked:
        ctx.stats.blocked_command += 1
        raise BlockedCommand(cmd, why)

    workdir = resolve_safe(cwd, ctx.root) if cwd else ctx.root
    if not workdir.is_dir():
        return ToolResult.err("bash", "FILE_NOT_FOUND", f"目录不存在：{cwd}")

    readonly = is_readonly_command(cmd)
    timeout = max(1, min(int(timeout or ctx.cfg.tools.bash_timeout), 1800))

    # ---- 权限 ----
    verdict = ctx.permission.check("bash", {"cmd": cmd})
    if verdict.decision == "deny":
        ctx.stats.permission_denied += 1
        raise PermissionDenied(f"命令被拒绝：{verdict.reason}")
    if verdict.decision == "ask":
        ok, always = ctx.ui.confirm(describe_call("bash", {"cmd": cmd}), verdict.reason)
        if always:
            ctx.permission.remember_allow(ctx.permission.key_for("bash", {"cmd": cmd}))
        if not ok:
            ctx.stats.permission_denied += 1
            raise PermissionDenied("用户拒绝执行该命令。")

    # ---- 快照（非只读命令）----
    if not readonly and ctx.snapshots is not None:
        dirty = git_dirty_files(ctx.root)
        if dirty:
            ctx.snapshots.take(dirty, ctx.step, f"bash: {one_line(cmd, 40)}")

    # ---- 执行 ----
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    env.setdefault("GIT_PAGER", "cat")
    env.setdefault("PAGER", "cat")

    popen_kwargs: dict = {}
    popen_cmd: str | list[str] = cmd
    use_shell = True

    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    elif os.name == "nt":
        # 优先 PowerShell 7，然后 Windows PowerShell。
        ps = shutil.which("pwsh") or shutil.which("powershell")

        # 某些 Windows 没把 powershell.exe 加入 PATH，
        # 再检查系统标准安装位置。
        if not ps:
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            candidate = Path(
                system_root,
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
            if candidate.is_file():
                ps = str(candidate)

        if ps:
            popen_cmd = [
                ps,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                cmd,
            ]
            use_shell = False
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    t0 = time.time()
    timed_out = False

    try:
        proc = subprocess.Popen(
            popen_cmd,
            shell=use_shell,
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            **popen_kwargs,
        )

    except OSError as e:
        return ToolResult.err("bash", "TOOL_ERROR", f"无法启动命令：{e}")

    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        ctx.stats.timeouts += 1
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        code = -1

    elapsed = int((time.time() - t0) * 1000)
    kept, meta = truncate_head_tail(out or "", ctx.cfg.tools.bash_output_bytes)

    if timed_out:
        return ToolResult(
            ok=False, tool="bash", code="TIMEOUT",
            content=f"$ {cmd}\n[命令超过 {timeout} 秒被终止]\n{kept}",
            hint=hint_for("TIMEOUT"), truncated=meta, cost_ms=elapsed,
        )

    header = f"$ {cmd}\n[exit={code}, {elapsed}ms]"
    body = f"{header}\n{kept}" if kept.strip() else f"{header}\n(无输出)"
    anchors = []
    if readonly:
        anchors = [Anchor(kind="cmd", ref=cmd, digest=f"退出码 {code}，{one_line(kept, 50)}")]

    return ToolResult(
        ok=(code == 0),
        tool="bash",
        code="OK" if code == 0 else "NONZERO_EXIT",
        content=body,
        reclaimable=readonly,
        anchors=anchors,
        truncated=meta,
        mutated=[] if readonly else ["<workspace>"],
        cost_ms=elapsed,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            # Windows：终止 shell 以及它启动的所有子进程。
            try:
                subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(proc.pid),
                        "/T",
                        "/F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            except Exception:
                proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except Exception:
            pass

