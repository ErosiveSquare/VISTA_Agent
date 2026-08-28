"""模型输出解析。

题目明确要求"模型输出的解析"须自行编写。本模块实现四级降级：

    级别 1：原生 tool_calls 字段（标准 OpenAI 兼容接口）
    级别 2：<tool_call>{...}</tool_call> XML 标签（部分开源模型的习惯）
    级别 3：```json 围栏代码块中的调用对象
    级别 4：判定是否为"最终回答"（无工具调用是合法的，不是解析失败）

任何一级的 JSON 都可能被截断或使用了单引号，因此附带一个启发式的
repair_json：补右括号、单引号转双引号、删除尾随逗号、转义裸控制字符。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..types import Call, LLMResponse


class _ParseFailed:
    """哨兵：无法从模型输出中提取任何可执行结构。"""

    def __repr__(self) -> str:  # pragma: no cover
        return "<PARSE_FAILED>"

    def __bool__(self) -> bool:
        return False


PARSE_FAILED = _ParseFailed()

_XML_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FENCE_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.S)
_BARE_OBJ_RE = re.compile(r"(\{[^{}]*\"(?:name|tool)\"\s*:\s*\"[A-Za-z_]\w*\".*?\})", re.S)

# 出现这些迹象时，认为模型是在给最终回答而不是在调用工具
_ANSWER_HINTS = (
    "已完成", "完成了", "总结", "综上", "以下是", "结论",
    "done", "summary", "in conclusion", "here is", "here's",
)


# ---------------------------------------------------------------------------
# JSON 修复
# ---------------------------------------------------------------------------
def repair_json(raw: str) -> Any | None:
    """尽力把不合法的 JSON 修成可解析的对象；失败返回 None。"""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return {}

    def _try(cands: list[str]) -> Any | None:
        seen: set[str] = set()
        for c in cands:
            c = c.strip()
            if not c or c in seen:
                continue
            seen.add(c)
            try:
                return json.loads(c)
            except Exception:
                continue
        return None

    # 每个阶段都在"上一阶段的全部候选"之上再生成新候选，
    # 这样"单引号 + 尾随逗号 + 截断"这类组合缺陷也能被修好。
    stages: list[Callable[[str], str | None]] = [
        _unfence,
        lambda x: x.replace("'", '"') if ("'" in x and '"' not in x) else None,
        _escape_bare_controls,
        lambda x: re.sub(r",\s*([}\]])", r"\1", x),
        _balance,
        lambda x: re.sub(r",\s*([}\]])", r"\1", x),
    ]

    pool: list[str] = [s]
    got = _try(pool)
    if got is not None:
        return got

    for transform in stages:
        grown: list[str] = []
        for base in pool:
            try:
                new = transform(base)
            except Exception:
                new = None
            if new and new != base:
                grown.append(new)
        if not grown:
            continue
        pool = pool + grown
        got = _try(grown)
        if got is not None:
            return got
    return None


def _unfence(s: str) -> str | None:
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s.strip(), re.S)
    return m.group(1) if m else None


def _balance(s: str) -> str:
    """按栈补全右括号，并闭合未结束的字符串。"""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
                stack.pop()
    out = s
    if in_str:
        out += '"'
    # 移除结尾可能残留的逗号
    out = re.sub(r",\s*$", "", out)
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def _escape_bare_controls(s: str) -> str:
    out: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _normalize_call(obj: Any) -> Call | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
        inner = name
        name = inner.get("name")
        args = inner.get("arguments")
    else:
        args = obj.get("arguments")
        if args is None:
            args = obj.get("args")
        if args is None:
            args = obj.get("parameters")
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(args, str):
        parsed = repair_json(args)
        args = parsed if isinstance(parsed, dict) else {"_raw": args}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"value": args}
    call_id = obj.get("id") if isinstance(obj.get("id"), str) else None
    call = Call.new(name.strip(), args)
    if call_id:
        call.id = call_id
    return call


def looks_like_final_answer(text: str) -> bool:
    """没有工具调用时，判断这是不是一个正常的最终回答。"""
    t = (text or "").strip()
    if len(t) < 8:
        return False
    if "<tool_call" in t or '"name"' in t and '"arguments"' in t:
        return False
    if len(t) >= 60:
        return True
    low = t.lower()
    return any(h in low for h in _ANSWER_HINTS)


def parse_tool_calls(resp: LLMResponse) -> list[Call] | _ParseFailed:
    """从模型响应中提取工具调用。

    返回：
        list[Call]  —— 提取到的调用（空列表表示"这是最终回答"）
        PARSE_FAILED —— 无法解析，需要注入格式提示后重试
    """
    # ---- 级别 1：原生 tool_calls ----
    if resp.tool_calls:
        good = [c for c in resp.tool_calls if c.name]
        if good:
            return good
        return PARSE_FAILED

    text = resp.text or ""
    if not text.strip():
        return PARSE_FAILED

    # ---- 级别 2：XML 标签 ----
    calls: list[Call] = []
    for blob in _XML_RE.findall(text):
        obj = repair_json(blob)
        call = _normalize_call(obj)
        if call:
            calls.append(call)
    if calls:
        return calls

    # ---- 级别 3：围栏代码块 ----
    for blob in _FENCE_RE.findall(text):
        obj = repair_json(blob)
        if isinstance(obj, list):
            for item in obj:
                call = _normalize_call(item)
                if call:
                    calls.append(call)
        else:
            call = _normalize_call(obj)
            if call:
                calls.append(call)
    if calls:
        return calls

    # ---- 级别 3.5：裸对象 ----
    for blob in _BARE_OBJ_RE.findall(text):
        obj = repair_json(blob)
        call = _normalize_call(obj)
        if call:
            calls.append(call)
    if calls:
        return calls

    # ---- 级别 4：最终回答 ----
    if looks_like_final_answer(text):
        return []
    return PARSE_FAILED


def parse_structured(text: str, *, expect: str = "object") -> Any | None:
    """从可能夹带解释文字的模型输出中提取 JSON 对象/数组。"""
    if text is None:
        return None
    direct = repair_json(text)
    if isinstance(direct, (dict, list)):
        return direct

    for m in _FENCE_RE.findall(text):
        obj = repair_json(m)
        if isinstance(obj, (dict, list)):
            return obj

    opener, closer = ("{", "}") if expect == "object" else ("[", "]")
    start = text.find(opener)
    end = text.rfind(closer)
    if start >= 0:
        chunk = text[start : end + 1] if end > start else text[start:]
        obj = repair_json(chunk)
        if isinstance(obj, (dict, list)):
            return obj
    return None
