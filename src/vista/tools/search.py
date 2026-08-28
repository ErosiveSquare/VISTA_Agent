"""检索工具：grep 与 repo_map。"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

from ..errors import hint_for
from ..types import Anchor, ToolResult
from ..util.paths import is_binary, read_text, rel_to, resolve_safe
from ..util.text import one_line
from .context import ToolContext
from .registry import tool

_SKIP_DIRS = {
    ".git", ".vista", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", ".idea",
    ".next", "coverage", ".tox", "site-packages",
}


def _has_rg() -> bool:
    return shutil.which("rg") is not None


def _rg(pattern: str, root: Path, path: str, file_glob: str | None,
        context: int, max_results: int) -> tuple[list[str], bool] | None:
    cmd = [
        "rg", "--line-number", "--with-filename", "--no-heading", "--color", "never",
        "--max-count", str(max_results), "--max-filesize", "512K",
    ]
    if context > 0:
        cmd += ["--context", str(context)]
    if file_glob:
        cmd += ["--glob", file_glob]
    for d in sorted(_SKIP_DIRS):
        cmd += ["--glob", f"!{d}/**"]
    cmd += ["--", pattern, path or "."]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                              errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return lines, len(lines) >= max_results


def _py_grep(pattern: str, root: Path, path: str, file_glob: str | None,
             context: int, max_results: int) -> tuple[list[str], bool]:
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))

    base = resolve_safe(path or ".", root)
    out: list[str] = []
    hits = 0
    targets: list[Path] = [base] if base.is_file() else []
    if base.is_dir():
        for dirpath, dirnames, filenames in Path(base).walk() if hasattr(Path, "walk") else _walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in sorted(filenames):
                targets.append(Path(dirpath) / fn)

    for f in targets:
        if hits >= max_results:
            break
        if file_glob and not fnmatch.fnmatch(f.name, file_glob) and not fnmatch.fnmatch(
            rel_to(f, root), file_glob
        ):
            continue
        try:
            if f.stat().st_size > 512 * 1024 or is_binary(f):
                continue
            lines = read_text(f).split("\n")
        except OSError:
            continue
        rel = rel_to(f, root)
        for i, line in enumerate(lines):
            if hits >= max_results:
                break
            if rx.search(line):
                hits += 1
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                for j in range(lo, hi):
                    sep = ":" if j == i else "-"
                    out.append(f"{rel}{sep}{j + 1}{sep}{lines[j][:300]}")
    return out, hits >= max_results


def _walk(base: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(base):
        yield Path(dirpath), dirnames, filenames


@tool(category="search", reclaimable=True)
def grep(ctx: ToolContext, pattern: str, path: str = ".", file_glob: str = "",
         context: int = 2, max_results: int = 40) -> ToolResult:
    """在工作区中按正则搜索文本，返回命中的文件、行号与上下文。

    这是定位代码的主要手段。优先使用它而不是逐个 read_file。

    Args:
        pattern: 正则表达式
        path: 搜索的起始目录或文件，默认整个工作区
        file_glob: 只搜索匹配该通配符的文件，例如 *.py
        context: 每条命中前后附带的上下文行数
        max_results: 最多返回的命中条数
    """
    resolve_safe(path or ".", ctx.root)  # 越界检查
    max_results = max(1, min(max_results, ctx.cfg.tools.grep_max_results * 4))
    context = max(0, min(context, 10))
    glob = file_glob or None

    res = _rg(pattern, ctx.root, path or ".", glob, context, max_results) if _has_rg() else None
    if res is None:
        lines, capped = _py_grep(pattern, ctx.root, path or ".", glob, context, max_results)
        engine = "python"
    else:
        lines, capped = res
        engine = "ripgrep"

    if not lines:
        return ToolResult(
            ok=True, tool="grep", code="NO_RESULTS",
            content=f"在 {path or '.'} 中没有匹配 /{pattern}/ 的内容。",
            hint=hint_for("NO_RESULTS"), reclaimable=True,
            anchors=[Anchor(kind="grep", ref=f"{pattern}@{path or '.'}", digest="无命中")],
        )

    files = sorted({ln.split(":")[0].split("-")[0] for ln in lines if ln})
    body = "\n".join(lines)
    tail = f"\n[命中数已达上限 {max_results}，可能还有更多；请缩小范围]" if capped else ""
    digest = f"{len(lines)} 行命中，涉及 {len(files)} 个文件：" + one_line(", ".join(files[:6]), 60)

    return ToolResult(
        ok=True, tool="grep",
        content=f"[{engine}] /{pattern}/ 在 {path or '.'}\n{body}{tail}",
        reclaimable=True,
        anchors=[Anchor(kind="grep", ref=f"{pattern}@{path or '.'}", digest=digest)],
    )


@tool(category="search", reclaimable=True)
def repo_map(ctx: ToolContext, focus: list[str] | None = None, budget: int = 0) -> ToolResult:
    """生成当前仓库的结构索引：按重要性排序的符号列表（类、函数、类型）。

    在开始一个不熟悉的任务时先调用它，可以避免大量盲目的文件浏览。
    传入 focus 可以让索引偏向与这些文件相关的部分。

    Args:
        focus: 你正在关注的文件路径列表，索引会向它们的依赖倾斜
        budget: 索引的 token 预算，0 表示使用配置中的默认值
    """
    if ctx.repomap is None or not ctx.cfg.repomap.enabled:
        return ToolResult(
            ok=True, tool="repo_map", code="NO_RESULTS",
            content="仓库索引未启用（可能是仓库文件数太少，或通过 --no-repomap 关闭了）。"
                    "请改用 grep 定位代码。",
            reclaimable=True,
        )
    focus_list = [f for f in (focus or []) if isinstance(f, str) and f.strip()]
    text, used = ctx.repomap.render(focus_list, budget or ctx.cfg.repomap.budget)
    if not text.strip():
        return ToolResult(ok=True, tool="repo_map", code="NO_RESULTS",
                          content="没有解析到任何符号。", reclaimable=True)
    return ToolResult(
        ok=True, tool="repo_map", content=text, reclaimable=True,
        anchors=[Anchor(kind="map", ref="repo_map",
                        digest=f"仓库结构索引，{used} tokens" + (f"，聚焦 {', '.join(focus_list[:3])}" if focus_list else ""))],
    )
