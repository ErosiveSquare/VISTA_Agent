"""Token 计数。

若环境中装有 tiktoken 则使用精确计数，否则使用中英混合的启发式估算。
预算判据本身留有约 40% 余量，因此 ±15% 的估算偏差是可以接受的。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

_TIKTOKEN_AVAILABLE: bool | None = None

# 每条消息的固定开销（role / 分隔符等），经验值
MESSAGE_OVERHEAD = 4
TOOLCALL_OVERHEAD = 12


@lru_cache(maxsize=8)
def _encoder(model: str):
    global _TIKTOKEN_AVAILABLE
    try:
        import tiktoken  # type: ignore
    except Exception:
        _TIKTOKEN_AVAILABLE = False
        return None
    _TIKTOKEN_AVAILABLE = True
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def using_tiktoken() -> bool:
    return bool(_TIKTOKEN_AVAILABLE)


def estimate(text: str) -> int:
    """启发式估算：中文约 1.5 字符/token，其它约 3.8 字符/token。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
            cjk += 1
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 3.8) + 1


def count_tokens(text: str, model: str = "") -> int:
    if not text:
        return 0
    enc = _encoder(model or "gpt-4o-mini")
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return estimate(text)


def count_message(msg: dict[str, Any], model: str = "") -> int:
    total = MESSAGE_OVERHEAD
    content = msg.get("content")
    if isinstance(content, str):
        total += count_tokens(content, model)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                total += count_tokens(part["text"], model)
    for tc in msg.get("tool_calls") or []:
        total += TOOLCALL_OVERHEAD
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        total += count_tokens(str(fn.get("name", "")), model)
        args = fn.get("arguments", "")
        total += count_tokens(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False), model)
    if msg.get("name"):
        total += count_tokens(str(msg["name"]), model)
    return total


def count_messages(messages: list[dict], model: str = "") -> int:
    return sum(count_message(m, model) for m in messages) + 3


def count_tools_schema(schemas: list[dict], model: str = "") -> int:
    if not schemas:
        return 0
    return count_tokens(json.dumps(schemas, ensure_ascii=False), model)
