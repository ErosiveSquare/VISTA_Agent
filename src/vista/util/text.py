"""文本处理工具。"""

from __future__ import annotations

import difflib
import re

# read_file 的行号分隔符。刻意不用 ':'，避免模型把行号当成 old_str 的一部分。
LINE_SEP = "│"


def number_lines(lines: list[str], start: int) -> str:
    return "\n".join(f"{start + i:>5}{LINE_SEP}{line}" for i, line in enumerate(lines))


def strip_line_numbers(text: str) -> str:
    """把模型误抄进来的行号前缀去掉，用于 old_str 的容错。"""
    out: list[str] = []
    changed = False
    for line in text.split("\n"):
        m = re.match(r"^\s*\d+" + re.escape(LINE_SEP) + r"(.*)$", line)
        if m:
            out.append(m.group(1))
            changed = True
        else:
            out.append(line)
    return "\n".join(out) if changed else text


def render_diff(old: str, new: str, path: str, context: int = 2, max_lines: int = 80) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}",
            lineterm="", n=context,
        )
    )
    if not diff:
        return "(文件内容未发生变化)"
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"… [diff 还有 {len(diff) - max_lines} 行未显示]"]
    return "\n".join(diff)


def locate_all(text: str, needle: str, cap: int = 6) -> list[int]:
    """返回 needle 每次出现处的行号（1-based）。"""
    lines: list[int] = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            break
        lines.append(text.count("\n", 0, i) + 1)
        start = i + max(len(needle), 1)
        if len(lines) >= cap:
            break
    return lines


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def find_near_matches(text: str, needle: str, k: int = 2, window: int = 3) -> str:
    """old_str 未命中时，找出最接近的几处，帮助模型自我纠正。"""
    lines = text.split("\n")
    n_lines = [ln for ln in needle.split("\n") if ln.strip()]
    if not n_lines:
        return "(old_str 为空)"
    probe = _norm_ws(n_lines[0])
    if not probe:
        return "(old_str 首行为空白)"

    scored: list[tuple[float, int]] = []
    for i, line in enumerate(lines):
        ratio = difflib.SequenceMatcher(None, _norm_ws(line), probe).ratio()
        if ratio > 0.55:
            scored.append((ratio, i))
    scored.sort(reverse=True)
    if not scored:
        return "(在文件中没有找到相似的行；请确认你读取的是同一个文件)"

    blocks: list[str] = []
    for ratio, i in scored[:k]:
        lo, hi = max(0, i - window), min(len(lines), i + window + 1)
        body = number_lines(lines[lo:hi], lo + 1)
        blocks.append(f"— 相似度 {ratio:.2f}，第 {i + 1} 行附近：\n{body}")
    return "\n".join(blocks)


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

CJK_STOP = {"的", "了", "和", "与", "是", "在", "我", "你", "他", "这", "那", "一个", "请", "帮"}


def tokenize(text: str) -> set[str]:
    """技能卡检索用的轻量分词：英文按词，中文按二元切分。"""
    text = text or ""
    toks: set[str] = {w.lower() for w in _WORD_RE.findall(text) if len(w) > 1}
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in cjk_runs:
        for ch in run:
            if ch not in CJK_STOP:
                toks.add(ch)
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in CJK_STOP:
                toks.add(bigram)
    return toks


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def one_line(text: str, cap: int = 80) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def slug(text: str, cap: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "skill")[:cap]
