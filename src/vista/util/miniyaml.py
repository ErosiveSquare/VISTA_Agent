"""YAML 子集的读写。

技能卡（L3）用 YAML 存储，因为它需要人可读、可手动编辑、可提交进 git。
为了让 VISTA 保持"零必需依赖"，这里实现了一个覆盖本项目所需语法的 YAML
子集读写器；如果环境里装了 PyYAML，则自动优先使用它。

支持的语法：
  - 顶层映射
  - 嵌套映射（缩进 2 空格）
  - 标量列表（- item）
  - 标量：字符串（可加引号）、整数、浮点、true/false、null
  - # 行注释

不支持（也不需要）：锚点、多文档、流式集合、块标量、复杂键。
"""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - 取决于环境
    import yaml as _pyyaml
except Exception:  # pragma: no cover
    _pyyaml = None


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_./\u4e00-\u9fff][^:#\n]*$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def _emit_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    s = str(v)
    if (
        s.lower() in _RESERVED
        or not _PLAIN_SAFE.match(s)
        or s != s.strip()
        or re.fullmatch(r"-?\d+(\.\d+)?", s)
    ):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


def dumps(data: dict, indent: int = 0) -> str:
    """把嵌套 dict 序列化为 YAML 文本。"""
    lines: list[str] = []
    pad = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}:")
                lines.append(dumps(value, indent + 2).rstrip("\n"))
        elif isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        body = dumps(item, indent + 4).rstrip("\n").split("\n")
                        first = body[0].lstrip()
                        lines.append(f"{pad}  - {first}")
                        lines.extend(body[1:])
                    else:
                        lines.append(f"{pad}  - {_emit_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_emit_scalar(value)}")
    return "\n".join(lines) + ("\n" if indent == 0 else "\n")


# ---------------------------------------------------------------------------
# 反序列化
# ---------------------------------------------------------------------------
def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "" or s in ("null", "~"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s.startswith("[") and s.endswith("]"):
        return _parse_flow_seq(s[1:-1])
    if s.startswith("{") and s.endswith("}"):
        return _parse_flow_map(s[1:-1])
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        body = s[1:-1]
        if s[0] == '"':
            body = body.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return body
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d*([eE][+-]?\d+)?", s):
        return float(s)
    return s


def _split_flow(body: str) -> list[str]:
    """按逗号切分行内序列，忽略引号内与嵌套括号里的逗号。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_flow_seq(body: str) -> list:
    return [_parse_scalar(item) for item in _split_flow(body)]


def _parse_flow_map(body: str) -> dict:
    out: dict = {}
    for item in _split_flow(body):
        k, sep, v = item.partition(":")
        if sep:
            out[k.strip().strip("\"'")] = _parse_scalar(v)
    return out


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _tokenize(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in (text or "").splitlines():
        line = _strip_comment(raw.replace("\t", "  ")).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip()))
    return rows


def _parse_block(rows: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    if pos >= len(rows):
        return None, pos
    if rows[pos][1].startswith("- "):
        return _parse_list(rows, pos, indent)
    return _parse_map(rows, pos, indent)


def _parse_list(rows: list[tuple[int, str]], pos: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    while pos < len(rows):
        cur_indent, text = rows[pos]
        if cur_indent < indent or not text.startswith("- "):
            break
        body = text[2:].strip()
        if ":" in body and not body.startswith(("\"", "'")):
            # 列表元素是一个映射：把它当作一个从当前位置开始的小 block
            key, _, rest = body.partition(":")
            sub: dict[str, Any] = {}
            if rest.strip():
                sub[key.strip()] = _parse_scalar(rest)
                pos += 1
            else:
                pos += 1
                if pos < len(rows) and rows[pos][0] > cur_indent + 2:
                    sub[key.strip()], pos = _parse_block(rows, pos, rows[pos][0])
                else:
                    sub[key.strip()] = None
            # 同一个元素下的后续键（缩进大于 '- ' 的列宽）
            while pos < len(rows) and rows[pos][0] == cur_indent + 2 and not rows[pos][1].startswith("- "):
                k2, _, v2 = rows[pos][1].partition(":")
                if v2.strip():
                    sub[k2.strip()] = _parse_scalar(v2)
                    pos += 1
                else:
                    pos += 1
                    if pos < len(rows) and rows[pos][0] > cur_indent + 2:
                        sub[k2.strip()], pos = _parse_block(rows, pos, rows[pos][0])
                    else:
                        sub[k2.strip()] = None
            items.append(sub)
        else:
            items.append(_parse_scalar(body))
            pos += 1
    return items, pos


def _parse_map(rows: list[tuple[int, str]], pos: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while pos < len(rows):
        cur_indent, text = rows[pos]
        if cur_indent < indent:
            break
        if cur_indent > indent:  # 结构异常，跳过以保证健壮
            pos += 1
            continue
        if text.startswith("- "):
            break
        key, sep, rest = text.partition(":")
        if not sep:
            pos += 1
            continue
        key = key.strip().strip("\"'")
        if rest.strip():
            out[key] = _parse_scalar(rest)
            pos += 1
        else:
            pos += 1
            if pos < len(rows) and rows[pos][0] > indent:
                out[key], pos = _parse_block(rows, pos, rows[pos][0])
            elif pos < len(rows) and rows[pos][0] == indent and rows[pos][1].startswith("- "):
                out[key], pos = _parse_list(rows, pos, indent)
            else:
                out[key] = None
    return out, pos


def loads(text: str) -> Any:
    """解析 YAML 子集。优先使用 PyYAML（若可用）。"""
    if _pyyaml is not None:
        try:
            return _pyyaml.safe_load(text)
        except Exception:
            pass  # 落到自实现解析器
    rows = _tokenize(text)
    if not rows:
        return {}
    value, _ = _parse_block(rows, 0, rows[0][0])
    return value


def dump_text(data: dict) -> str:
    """序列化。始终使用自实现的 emitter，保证输出格式稳定、可 diff。"""
    return dumps(data, 0)
