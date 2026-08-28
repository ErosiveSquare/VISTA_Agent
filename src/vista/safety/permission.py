"""权限与危险命令拦截。

三态策略：allow / ask / deny，最严格的规则获胜（deny > ask > allow）。

VISTA 是交互式本地工具，安全边界是"用户在场"，因此没有引入容器隔离
（这与 Aider 的选择一致）。防护由三层构成：
    1. 危险模式表 —— 直接 deny，模型看到的是一个错误结果而不是崩溃
    2. 默认拒绝的权限策略 —— 写操作与非白名单 bash 需要确认
    3. 写操作前快照 —— 任何改动都可回滚
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

Decision = Literal["allow", "ask", "deny"]

# ---------------------------------------------------------------------------
# 危险命令模式
#
# 注意 git push --force / git rebase 这两条：本项目的作业要求明确禁止改写
# 已推送的提交历史，这里把这条外部约束内化成了 agent 的系统约束。
# ---------------------------------------------------------------------------
BLOCKED_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f[a-zA-Z]*\s+(?:/|~|\$HOME|/\*|\*)\s*(?:$|;|&)",
     "递归强制删除根目录或家目录"),
    (r"\brm\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*f[a-zA-Z]*[rR][a-zA-Z]*\s+(?:/|~|\$HOME|/\*|\*)\s*(?:$|;|&)",
     "递归强制删除根目录或家目录"),
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:?\s*&?\s*\}\s*;?\s*:", "fork bomb"),
    (r"\bdd\b[^\n]*\bof\s*=\s*/dev/(?:sd|nvme|disk|hd)", "直接写裸设备"),
    (r"\bmkfs(?:\.\w+)?\b", "格式化文件系统"),
    (r">\s*/dev/(?:sd|nvme|disk|hd)", "重定向到裸设备"),
    (r"\b(?:curl|wget)\b[^|;\n]*\|\s*(?:sudo\s+)?(?:ba|z|k|)sh\b", "管道执行远程脚本"),
    (r"\bchmod\s+(?:-R\s+)?0?777\s+/(?:\s|$)", "对根目录放开全部权限"),
    (r"\bchown\s+-R\s+[^\s]+\s+/(?:\s|$)", "递归修改根目录属主"),
    (r"\bgit\s+push\b[^\n]*(?:--force\b(?!-with-lease)|(?:^|\s)-f(?:\s|$))", "强制推送会改写远端历史"),
    (r"\bgit\s+(?:rebase|filter-branch|filter-repo)\b", "改写提交历史"),
    (r"\bgit\s+reset\s+--hard\s+origin/", "丢弃本地历史"),
    (r"\bhistory\s+-c\b", "清空 shell 历史"),
    (r"\b(?:shutdown|reboot|halt|poweroff)\b", "关机或重启"),
    (r"\bsudo\s+(?:rm|dd|mkfs|chown|chmod)\b", "以 root 执行破坏性命令"),
    (r"\b:\s*>\s*/dev/(?:sda|nvme0n1)", "清空磁盘"),
]

_COMPILED = [(re.compile(p, re.I), why) for p, why in BLOCKED_PATTERNS]

# 只读命令白名单：命中则 (a) 无需确认，(b) 结果标记为可重取
READONLY_HEADS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "which", "whereis", "file", "stat",
    "find", "tree", "du", "df", "env", "printenv", "date", "echo", "uname",
    "grep", "rg", "ag", "sed", "awk", "sort", "uniq", "diff", "basename", "dirname",
    "python", "python3", "node", "true", "false", "type", "command",
}
READONLY_GIT = {
    "status",
    "diff",
    "log",
    "show",
    "ls-files",
    "rev-parse",
    "describe",
    "blame",
    "shortlog",
}

# sed/awk/python 这些既能只读也能改文件，需要额外检查
_AMBIGUOUS = {"sed", "awk", "python", "python3", "node", "echo", "find"}

_REDIRECT_RE = re.compile(r"(?<![0-9<>])>{1,2}(?!&)")
_MUTATING_HINT = re.compile(
    r"\b(rm|mv|cp|mkdir|touch|install|pip|npm|yarn|pnpm|cargo|go|make|"
    r"git\s+(?:add|commit|checkout|switch|merge|apply|restore|stash|clean|init|mv|rm)|"
    r"tee|truncate|chmod|chown|ln)\b",
    re.I,
)


@dataclass
class Verdict:
    decision: Decision
    reason: str = ""
    readonly: bool = False


def _split_segments(cmd: str) -> list[str]:
    """按 ; && || | 拆成子命令，逐段判定。"""
    parts = re.split(r"(?:\|\||&&|;|\||\n)", cmd)
    return [p.strip() for p in parts if p.strip()]


def check_blocked(cmd: str) -> tuple[bool, str]:
    for rx, why in _COMPILED:
        if rx.search(cmd):
            return True, why
    return False, ""


def is_readonly_command(cmd: str) -> bool:
    """判断一条 bash 命令是否只读（幂等、无副作用）。

    这个判定同时决定该次工具结果的 reclaimable 标记，因此必须保守：
    不确定时一律返回 False（代价只是压缩效率略低，不会丢信息）。
    """
    if not cmd or not cmd.strip():
        return False
    if _REDIRECT_RE.search(cmd):
        return False
    if _MUTATING_HINT.search(cmd):
        return False
    for seg in _split_segments(cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            return False
        if not toks:
            return False
        head = toks[0].rsplit("/", 1)[-1]
        if head == "git":
            if len(toks) < 2 or toks[1] not in READONLY_GIT:
                return False
            continue
        if head not in READONLY_HEADS:
            return False
        if head in _AMBIGUOUS:
            rest = " ".join(toks[1:])
            if head in ("sed", "awk") and re.search(r"(^|\s)-i\b", rest):
                return False
            if head in ("python", "python3", "node"):
                # python -c / node -e 可以执行任意代码，绝不能视为只读
                if re.search(r"(^|\s)(?:-c|-e)(\s|$)", rest):
                    return False

                # 只有查询版本号才可以确定是只读
                if not re.search(r"(^|\s)(?:--version|-V)(\s|$)", rest):
                    return False
            if head == "find" and re.search(r"-(delete|exec|execdir|ok)\b", rest):
                return False
    return True


class PermissionPolicy:
    """把 (工具, 参数) 映射到 allow / ask / deny。"""

    # 无副作用工具
    READ_TOOLS = {"read_file", "grep", "repo_map", "memory_read", "ask_user", "todo_write", "finish"}
    WRITE_TOOLS = {"write_file", "edit_file", "memory_write"}

    def __init__(self, mode: str = "ask", interactive: bool = False,
                 allow_bash_in_run_mode: bool = True):
        self.mode = mode
        self.interactive = interactive
        self.allow_bash_in_run_mode = allow_bash_in_run_mode
        self.always_allow: set[str] = set()

    def remember_allow(self, key: str) -> None:
        self.always_allow.add(key)

    def key_for(self, tool: str, args: dict) -> str:
        if tool == "bash":
            cmd = str(args.get("cmd", "")).strip()
            head = cmd.split()[0] if cmd.split() else ""
            return f"bash:{head}"
        return f"tool:{tool}"

    def check(self, tool: str, args: dict) -> Verdict:
        if tool == "bash":
            cmd = str(args.get("cmd", ""))
            blocked, why = check_blocked(cmd)
            if blocked:
                return Verdict("deny", f"命中危险命令规则（{why}）")
            ro = is_readonly_command(cmd)
            if self.mode == "allow":
                return Verdict("allow", readonly=ro)
            if self.mode == "deny":
                return Verdict("deny", "权限策略为 deny")
            if ro:
                return Verdict("allow", "只读命令", readonly=True)
            if self.key_for(tool, args) in self.always_allow:
                return Verdict("allow", "用户已选择始终允许")
            if not self.interactive:
                return Verdict(
                    "allow" if self.allow_bash_in_run_mode else "deny",
                    "非交互模式下的 bash 策略",
                )
            return Verdict("ask", "该命令可能修改环境")

        if tool in self.READ_TOOLS:
            return Verdict("allow", readonly=True)

        if tool in self.WRITE_TOOLS:
            if self.mode == "allow":
                return Verdict("allow")
            if self.mode == "deny":
                return Verdict("deny", "权限策略为 deny")
            if self.key_for(tool, args) in self.always_allow:
                return Verdict("allow", "用户已选择始终允许")
            if not self.interactive:
                return Verdict("allow", "非交互模式下允许写工作区")
            return Verdict("ask", "该操作会修改文件")

        return Verdict("allow" if self.mode == "allow" else "ask", "未知工具")


def describe_call(tool: str, args: dict) -> str:
    """给用户看的一行操作描述。"""
    if tool == "bash":
        return f"执行命令：{args.get('cmd', '')}"
    if tool in ("write_file", "read_file"):
        return f"{'写入' if tool == 'write_file' else '读取'}文件：{args.get('path', '')}"
    if tool == "edit_file":
        return f"编辑文件：{args.get('path', '')}"
    if tool == "memory_write":
        return f"写入记忆（{args.get('scope', '')}）：{args.get('key', '')}"
    return f"{tool}({', '.join(f'{k}={v!r}'[:40] for k, v in list(args.items())[:3])})"
