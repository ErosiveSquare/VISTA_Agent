"""文件工具 + 指纹守卫。

FileLedger 记录每次 read_file 时文件全文的 sha256。任何 edit_file / write_file
之前都要重算并比对（不变式 I6）：

    - 未读过就编辑        → NOT_READ
    - 指纹不一致          → STALE_CONTEXT，拒绝执行并要求重读

这个机制不产生新能力，它消除一整类静默错误：agent 基于陈旧内容做串替换，
old_str 恰好仍能匹配，于是"改成功了但改错了地方"。这种错误不会报错，
要到跑测试甚至上线才暴露。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PermissionDenied, hint_for
from ..types import Anchor, ToolResult
from ..util.paths import is_binary, read_text, rel_to, resolve_safe, sha_of, write_text
from ..util.text import find_near_matches, locate_all, number_lines, render_diff, strip_line_numbers
from .context import ToolContext
from .registry import tool


# ===========================================================================
# FileLedger
# ===========================================================================
@dataclass
class LedgerRecord:
    path: str
    sha: str
    ranges: list[tuple[int, int]] = field(default_factory=list)
    read_at: float = 0.0
    written_at: float = 0.0
    total_lines: int = 0

    def covers_full(self) -> bool:
        return any(lo <= 1 and hi >= self.total_lines for lo, hi in self.ranges)


class FileLedger:
    """文件指纹账本。"""

    def __init__(self) -> None:
        self._records: dict[str, LedgerRecord] = {}

    def record_read(self, rel: str, sha: str, span: tuple[int, int], total_lines: int) -> None:
        rec = self._records.get(rel)
        if rec is None or rec.sha != sha:
            rec = LedgerRecord(path=rel, sha=sha, total_lines=total_lines)
            self._records[rel] = rec
        rec.total_lines = total_lines
        rec.read_at = time.time()
        if span not in rec.ranges:
            rec.ranges.append(span)
            rec.ranges.sort()

    def record_write(self, rel: str, sha: str, total_lines: int) -> None:
        rec = LedgerRecord(path=rel, sha=sha, total_lines=total_lines)
        rec.read_at = time.time()
        rec.written_at = time.time()
        rec.ranges = [(1, total_lines)]
        self._records[rel] = rec

    def get(self, rel: str) -> LedgerRecord | None:
        return self._records.get(rel)

    def invalidate(self, rel: str) -> None:
        self._records.pop(rel, None)

    def known(self) -> list[str]:
        return sorted(self._records)

    def to_dict(self) -> dict:
        return {
            k: {"sha": v.sha, "ranges": [list(r) for r in v.ranges], "lines": v.total_lines}
            for k, v in self._records.items()
        }


# ===========================================================================
# 工具实现
# ===========================================================================
def _digest_of(rel: str, lines: list[str], lo: int, hi: int) -> str:
    """由工具层生成的一行摘要 —— 压缩后唯一存活的语义信息。

    刻意不经过 LLM：确定性、零成本、不会产生幻觉。
    """
    from ..memory.symbols import quick_symbols

    names = quick_symbols(rel, lines[lo - 1 : hi])
    if names:
        return "定义 " + " / ".join(names[:6]) + ("…" if len(names) > 6 else "")
    head = next((ln.strip() for ln in lines[lo - 1 : hi] if ln.strip()), "")
    return (head[:60] + "…") if len(head) > 60 else (head or f"{hi - lo + 1} 行文本")


def _guard(ctx: ToolContext, path: str, tool_name: str) -> tuple[Path, str, str, list[str]] | ToolResult:
    """写操作前的统一校验：返回 (绝对路径, 相对路径, 当前 sha, 当前行) 或错误结果。"""
    p = resolve_safe(path, ctx.root)
    rel = rel_to(p, ctx.root)
    if not p.exists():
        return ToolResult.err(tool_name, "FILE_NOT_FOUND",
                              f"{rel} 不存在。", hint_for("FILE_NOT_FOUND"))
    if not p.is_file():
        return ToolResult.err(tool_name, "FILE_NOT_FOUND", f"{rel} 不是一个普通文件。")
    text = read_text(p)
    cur_sha = sha_of(text)

    rec = ctx.ledger.get(rel)
    if rec is None:
        return ToolResult.err(
            tool_name, "NOT_READ",
            f"{rel} 在本次会话中尚未被读取过。",
            f"请先调用 read_file('{rel}') 获取当前内容。",
        )
    if rec.sha != cur_sha:
        ctx.stats.stale_blocked += 1
        ctx.ledger.invalidate(rel)
        return ToolResult.err(
            tool_name, "STALE_CONTEXT",
            f"{rel} 自你上次读取（sha={rec.sha}）之后已经发生变化（当前 sha={cur_sha}）。"
            f"可能的原因：你自己执行的 bash 命令、代码格式化工具，或工作区被外部修改。",
            f"请重新调用 read_file('{rel}')，基于最新内容重新构造 old_str。",
        )
    return p, rel, cur_sha, text.split("\n")


def _snapshot(ctx: ToolContext, rel: str, label: str) -> None:
    if ctx.snapshots is not None:
        ctx.snapshots.take([rel], ctx.step, label)


def _ask_permission(ctx: ToolContext, tool_name: str, args: dict) -> None:
    from ..safety.permission import describe_call

    verdict = ctx.permission.check(tool_name, args)
    if verdict.decision == "allow":
        return
    if verdict.decision == "deny":
        raise PermissionDenied(f"操作被拒绝：{verdict.reason}")
    ok, always = ctx.ui.confirm(describe_call(tool_name, args), verdict.reason)
    if always:
        ctx.permission.remember_allow(ctx.permission.key_for(tool_name, args))
    if not ok:
        raise PermissionDenied("用户拒绝了该操作。")


# ---------------------------------------------------------------------------
@tool(category="file", reclaimable=True)
def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 400) -> ToolResult:
    """读取工作区内某个文件的内容，返回带行号的文本。

    编辑任何文件之前，都必须先用本工具读取它。

    Args:
        path: 相对于工作区根目录的文件路径
        offset: 起始行号，从 1 开始
        limit: 最多读取的行数
    """
    p = resolve_safe(path, ctx.root)
    rel = rel_to(p, ctx.root)
    if not p.exists():
        return ToolResult.err("read_file", "FILE_NOT_FOUND", f"{rel} 不存在。", hint_for("FILE_NOT_FOUND"))
    if p.is_dir():
        try:
            entries = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())[:200]
        except OSError as e:
            return ToolResult.err("read_file", "TOOL_ERROR", str(e))
        return ToolResult(
            ok=True, tool="read_file", content=f"{rel} 是一个目录，包含：\n" + "\n".join(entries),
            hint="请指定具体的文件路径。", reclaimable=True,
        )
    if is_binary(p):
        return ToolResult.err("read_file", "BINARY_FILE", f"{rel} 是二进制文件。", hint_for("BINARY_FILE"))
    size = p.stat().st_size
    if size > ctx.cfg.tools.max_file_bytes:
        return ToolResult.err(
            "read_file", "FILE_TOO_LARGE",
            f"{rel} 有 {size} 字节，超过单次读取上限 {ctx.cfg.tools.max_file_bytes}。",
            hint_for("FILE_TOO_LARGE"),
        )

    text = read_text(p)
    sha = sha_of(text)
    lines = text.split("\n")
    total = len(lines)
    offset = max(1, min(offset, total))
    limit = max(1, min(limit, ctx.cfg.tools.read_limit * 8))
    end = min(offset - 1 + limit, total)

    body = number_lines(lines[offset - 1 : end], offset)
    tail = ""
    if end < total:
        tail = f"\n\n[本文件共 {total} 行，当前显示第 {offset}-{end} 行。继续读取请设置 offset={end + 1}]"
    elif offset > 1:
        tail = f"\n\n[本文件共 {total} 行，当前显示第 {offset}-{end} 行（已到末尾）]"

    ctx.ledger.record_read(rel, sha, (offset, end), total)
    digest = _digest_of(rel, lines, offset, end)

    return ToolResult(
        ok=True, tool="read_file", content=f"{rel}（sha={sha}）\n{body}{tail}",
        reclaimable=True,
        anchors=[Anchor(kind="file", ref=rel, sha=sha, span=(offset, end), digest=digest)],
    )


# ---------------------------------------------------------------------------
@tool(category="file", mutating=True)
def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    """把内容整体写入一个文件；文件已存在时会被覆盖，父目录会自动创建。

    覆盖已存在的文件之前，必须先用 read_file 读取过它。
    如果只是修改文件的一小部分，请优先使用 edit_file。

    Args:
        path: 相对于工作区根目录的文件路径
        content: 要写入的完整文件内容
    """
    p = resolve_safe(path, ctx.root)
    rel = rel_to(p, ctx.root)
    existed = p.is_file()

    if existed:
        guard = _guard(ctx, path, "write_file")
        if isinstance(guard, ToolResult):
            return guard
        old_text = read_text(p)
    else:
        old_text = ""

    _ask_permission(ctx, "write_file", {"path": rel})
    _snapshot(ctx, rel, f"write_file {rel}")

    try:
        write_text(p, content)
    except OSError as e:
        return ToolResult.err("write_file", "WRITE_FAILED", str(e), hint_for("WRITE_FAILED"))

    new_lines = content.split("\n")
    ctx.ledger.record_write(rel, sha_of(content), len(new_lines))

    if existed:
        detail = render_diff(old_text, content, rel)
        verb = "已覆盖"
    else:
        detail = f"（新建文件，共 {len(new_lines)} 行）"
        verb = "已创建"
    return ToolResult(
        ok=True, tool="write_file",
        content=f"{verb} {rel}\n{detail}",
        mutated=[rel],
    )


# ---------------------------------------------------------------------------
@tool(category="file", mutating=True)
def edit_file(ctx: ToolContext, path: str, old_str: str, new_str: str,
              replace_all: bool = False) -> ToolResult:
    """把文件中的一段精确文本替换为新文本。这是修改代码的首选方式。

    old_str 必须与文件中的内容逐字符一致（包括缩进），并且默认必须唯一匹配。
    注意：read_file 返回的行号前缀（形如 "   42│"）只是显示用的，
    构造 old_str 时不要包含它们。

    Args:
        path: 相对于工作区根目录的文件路径
        old_str: 要被替换掉的原文本，必须在文件中唯一出现
        new_str: 替换后的新文本；传空字符串表示删除这段内容
        replace_all: 为 true 时替换所有匹配处，而不是要求唯一匹配
    """
    guard = _guard(ctx, path, "edit_file")
    if isinstance(guard, ToolResult):
        return guard
    p, rel, _cur_sha, _lines = guard
    text = read_text(p)

    if not old_str:
        return ToolResult.err("edit_file", "BAD_ARGS", "old_str 不能为空。",
                              "如果要创建新文件或整体重写，请使用 write_file。")

    needle = old_str
    n = text.count(needle)
    fixed_note = ""
    if n == 0:  # 容错：模型可能把行号前缀抄了进来
        cleaned = strip_line_numbers(old_str)
        if cleaned != old_str and text.count(cleaned) > 0:
            needle = cleaned
            n = text.count(needle)
            fixed_note = "（已自动去除 old_str 中误抄入的行号前缀）"

    if n == 0:
        ctx.stats.no_match += 1
        near = find_near_matches(text, old_str)
        return ToolResult.err(
            "edit_file", "NO_MATCH",
            f"在 {rel} 中没有找到 old_str。\n最接近的位置：\n{near}",
            hint_for("NO_MATCH"),
        )
    if n > 1 and not replace_all:
        ctx.stats.ambiguous += 1
        locs = locate_all(text, needle)
        return ToolResult.err(
            "edit_file", "AMBIGUOUS",
            f"old_str 在 {rel} 中匹配到 {n} 处（第 {', '.join(map(str, locs))} 行附近）。",
            hint_for("AMBIGUOUS"),
        )

    _ask_permission(ctx, "edit_file", {"path": rel})
    _snapshot(ctx, rel, f"edit_file {rel}")

    new_text = text.replace(needle, new_str, -1 if replace_all else 1)
    if new_text == text:
        return ToolResult(ok=True, tool="edit_file", content=f"{rel} 内容未发生变化（old_str 与 new_str 相同）。")

    try:
        write_text(p, new_text)
    except OSError as e:
        return ToolResult.err("edit_file", "WRITE_FAILED", str(e), hint_for("WRITE_FAILED"))

    new_lines = new_text.split("\n")
    ctx.ledger.record_write(rel, sha_of(new_text), len(new_lines))
    diff = render_diff(text, new_text, rel)
    head = f"已修改 {rel}（替换 {n if replace_all else 1} 处）{fixed_note}"
    return ToolResult(ok=True, tool="edit_file", content=f"{head}\n{diff}", mutated=[rel])
