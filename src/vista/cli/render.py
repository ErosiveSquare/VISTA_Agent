"""终端渲染。

刻意没有引入 rich：VISTA 承诺零必需依赖，而这里需要的能力（颜色、加粗、
流式增量输出、简单框线）用几十行 ANSI 就够了。代价是没有自动换行的表格排版，
收益是安装 VISTA 只需要一个 Python。
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import TextIO

# ---------------------------------------------------------------------------
RESET = "\033[0m"
CODES = {
    "dim": "\033[2m", "bold": "\033[1m", "italic": "\033[3m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m", "brightred": "\033[91m", "brightgreen": "\033[92m",
    "brightyellow": "\033[93m", "brightcyan": "\033[96m", "white": "\033[97m",
}

KIND_STYLE = {
    "step": ("grey", "·"),
    "tool": ("cyan", "▸"),
    "tool_error": ("brightred", "✗"),
    "compaction": ("yellow", "⟐"),
    "verify": ("blue", "⊙"),
    "verify_pass": ("brightgreen", "✓"),
    "verify_fail": ("brightred", "✗"),
    "baseline": ("grey", "·"),
    "baseline_done": ("grey", "·"),
    "warn": ("yellow", "!"),
    "memory": ("magenta", "◆"),
    "skill": ("magenta", "◆"),
    "end": ("bold", "■"),
    "info": ("grey", "·"),
}


def supports_color(stream: TextIO = sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def term_width(default: int = 100) -> int:
    try:
        return max(50, min(shutil.get_terminal_size().columns, 140))
    except OSError:
        return default


class Console:
    def __init__(self, color: bool | None = None, stream: TextIO | None = None,
                 quiet: bool = False):
        self.out = stream or sys.stdout
        self.color = supports_color(self.out) if color is None else color
        self.quiet = quiet
        self._streaming = False

    # ------------------------------------------------------------------
    def style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        prefix = "".join(CODES.get(s, "") for s in styles)
        return f"{prefix}{text}{RESET}" if prefix else text

    def write(self, text: str = "") -> None:
        if self.quiet:
            return
        self._end_stream()
        self.out.write(text + "\n")
        self.out.flush()

    def raw(self, text: str) -> None:
        if self.quiet:
            return
        self.out.write(text)
        self.out.flush()

    # ------------------------------------------------------------------
    def rule(self, title: str = "", char: str = "─") -> None:
        w = term_width()
        if title:
            head = f"{char * 2} {title} "
            line = head + char * max(0, w - len(head) - 1)
        else:
            line = char * (w - 1)
        self.write(self.style(line, "grey"))

    def banner(self, version: str, cfg_line: str) -> None:
        self.write()
        self.write(self.style("  VISTA", "bold", "brightcyan")
                   + self.style(f"  v{version}", "grey"))
        self.write(self.style("  Verified · Indexed · Self-evolving · Tiered-memory · Anchored-context",
                              "grey"))
        self.write(self.style(f"  {cfg_line}", "grey"))
        self.write()

    def task(self, text: str) -> None:
        self.write(self.style("任务  ", "bold") + text)

    # ------------------------------------------------------------------
    def event(self, kind: str, text: str, extra: dict | None = None) -> None:
        if self.quiet:
            return
        color, glyph = KIND_STYLE.get(kind, ("grey", "·"))
        extra = extra or {}
        if kind == "step":
            tok = extra.get("tokens")
            suffix = self.style(f"   [{tok:,} tok]", "grey") if tok else ""
            self.write(self.style(f"{glyph} {text}", "grey") + suffix)
            return
        self.write(self.style(f"  {glyph} ", color) + self.style(text, color))

    def stream(self, delta: str) -> None:
        """模型流式输出的增量回调。"""
        if self.quiet:
            return
        if not self._streaming:
            self.out.write(self.style("  ", "dim"))
            self._streaming = True
        self.out.write(delta)
        self.out.flush()

    def _end_stream(self) -> None:
        if self._streaming:
            self.out.write((RESET if self.color else "") + "\n")
            self.out.flush()
            self._streaming = False

    # ------------------------------------------------------------------
    def kv(self, key: str, value: str, width: int = 14) -> None:
        self.write(self.style(key.ljust(width), "grey") + value)

    def error(self, text: str) -> None:
        self._end_stream()
        self.out.write(self.style(f"错误：{text}", "brightred") + "\n")
        self.out.flush()

    def warn(self, text: str) -> None:
        self.write(self.style(f"注意：{text}", "yellow"))

    def ok(self, text: str) -> None:
        self.write(self.style(text, "brightgreen"))

    # ------------------------------------------------------------------
    def result(self, res, show_path: bool = True) -> None:
        """打印一次运行的最终结果。"""
        self._end_stream()
        self.write()
        self.rule()
        good = res.status in ("success", "answered")
        head = {
            "success": "任务完成，Verify-Gate 通过",
            "answered": "已回答",
        }.get(res.status, f"未完成（{res.status}）")
        if res.status == "success" and not res.verified:
            head = "任务完成，但本次未能进行真实验证（verified=false）"
            self.write(self.style(f"◐ {head}", "yellow"))
        else:
            self.write(self.style(("✓ " if good else "✗ ") + head,
                                  "brightgreen" if good else "brightred"))
        self.write(self.style(f"  终止条件：{res.reason}", "grey"))
        if res.summary:
            self.write()
            self.write(res.summary.strip()[:2000])
        self.write()
        self.kv("步数", str(res.steps))
        self.kv("token", f"输入 {res.usage.in_tokens:,} / 输出 {res.usage.out_tokens:,}")
        self.kv("成本", f"${res.cost:.4f}")
        self.kv("耗时", f"{res.wall_ms / 1000:.1f}s")
        if res.mutated:
            self.kv("改动文件", ", ".join(res.mutated[:8]))
        if show_path and res.session_dir:
            self.kv("会话轨迹", res.session_dir)
        self.rule()


# ---------------------------------------------------------------------------
class TerminalUI:
    """实现 ToolContext 需要的 UI 协议。"""

    def __init__(self, console: Console, interactive: bool = True):
        self.console = console
        self.interactive = interactive

    def confirm(self, title: str, detail: str = "") -> tuple[bool, bool]:
        if not self.interactive:
            return True, False
        c = self.console
        c._end_stream()
        c.write()
        c.write(c.style("  需要确认  ", "bold", "yellow") + title)
        if detail:
            c.write(c.style(f"            {detail}", "grey"))
        prompt = c.style("  [y] 允许  [a] 始终允许  [n] 拒绝 > ", "yellow")
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            c.write()
            return False, False
        if ans in ("a", "always"):
            return True, True
        return (ans in ("", "y", "yes")), False

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        if not self.interactive:
            return None
        c = self.console
        c._end_stream()
        c.write()
        c.write(c.style("  智能体提问  ", "bold", "magenta") + question)
        if options:
            for i, opt in enumerate(options, 1):
                c.write(c.style(f"    {i}. ", "grey") + opt)
        try:
            ans = input(c.style("  你的回答 > ", "magenta")).strip()
        except (EOFError, KeyboardInterrupt):
            c.write()
            return None
        if options and ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        return ans or None

    def notify(self, text: str, kind: str = "info") -> None:
        self.console.event(kind, text)

    def stream(self, delta: str) -> None:
        self.console.stream(delta)
