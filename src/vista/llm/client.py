"""LLM 客户端。

VISTA 不依赖任何 agent 框架。为了同时满足"零必需依赖"与"允许使用厂商 SDK"，
这里提供三种 provider：

    http    —— 仅用标准库 urllib 直连 OpenAI 兼容的 /chat/completions（默认）
    openai  —— 使用官方 openai SDK（若已安装）
    mock    —— 离线脚本回放，用于单元测试与无网络演示

三者返回统一的 LLMResponse，上层（loop/compactor/skills）完全不感知差异。

多模型路由（role-based）：
    main —— 主循环推理与决策
    weak —— 上下文压缩的 KeyInfo 生成、技能卡蒸馏。只做"信息重组"，不做决策，
            因此可以用更便宜的模型。
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import Config
from ..errors import ContextOverflow, FatalLLMError, RetryableError
from ..types import Call, LLMResponse, Usage
from . import parser as P
from . import tokens as T

DeltaHook = Callable[[str], None]

_CONTEXT_MARKERS = (
    "context length", "context_length", "maximum context", "too many tokens",
    "reduce the length", "input is too long", "context window",
)


# ===========================================================================
# Provider 抽象
# ===========================================================================
class Provider:
    name = "base"

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        tools: list[dict] | None,
        temperature: float,
        max_tokens: int,
        timeout: int,
        stream: bool,
        on_delta: DeltaHook | None,
    ) -> LLMResponse:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1) 标准库 HTTP provider
# ---------------------------------------------------------------------------
class HttpProvider(Provider):
    name = "http"

    def __init__(self, base_url: str, api_key: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def chat(self, messages, *, model, tools, temperature, max_tokens, timeout, stream, on_delta):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if "api.deepseek.com" in self.base_url:
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "vista-agent/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(self._endpoint(), data=body, headers=headers, method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if stream:
                    out = _consume_sse(resp, on_delta)
                else:
                    out = _parse_completion(json.loads(resp.read().decode("utf-8", "replace")))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:1200]
            except Exception:
                pass
            _raise_http(e.code, detail)
        except urllib.error.URLError as e:
            raise RetryableError(f"网络错误：{e.reason}")
        except TimeoutError:
            raise RetryableError(f"请求超时（{timeout}s）")
        except json.JSONDecodeError as e:
            raise RetryableError(f"响应不是合法 JSON：{e}")

        out.model = model
        out.latency_ms = int((time.time() - t0) * 1000)
        return out


def _raise_http(code: int, detail: str) -> None:
    low = (detail or "").lower()
    if any(m in low for m in _CONTEXT_MARKERS):
        raise ContextOverflow(f"上下文超过模型窗口：{detail[:300]}", status=code)
    if code in (408, 409, 425, 429, 500, 502, 503, 504, 529):
        raise RetryableError(f"HTTP {code}：{detail[:300]}", status=code)
    raise FatalLLMError(f"HTTP {code}：{detail[:500]}", status=code)


def _parse_completion(data: dict) -> LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        raise RetryableError("响应中没有 choices 字段")
    msg = choices[0].get("message") or {}
    calls: list[Call] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        parsed = P.repair_json(args) if isinstance(args, str) else args
        call = Call.new(fn.get("name") or "", parsed if isinstance(parsed, dict) else {})
        if isinstance(tc.get("id"), str) and tc["id"]:
            call.id = tc["id"]
        if call.name:
            calls.append(call)
    usage_raw = data.get("usage") or {}
    return LLMResponse(
        text=msg.get("content") or "",
        tool_calls=calls,
        usage=Usage(
            int(usage_raw.get("prompt_tokens") or 0),
            int(usage_raw.get("completion_tokens") or 0),
        ),
        finish_reason=choices[0].get("finish_reason") or "",
        raw=data,
    )


def _consume_sse(resp: Iterable[bytes], on_delta: DeltaHook | None) -> LLMResponse:
    """解析 SSE 流。工具调用的 arguments 是增量拼接的，需要按 index 累积。"""
    text_parts: list[str] = []
    tool_acc: dict[int, dict[str, Any]] = {}
    usage = Usage()
    finish = ""

    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if chunk.get("usage"):
            u = chunk["usage"]
            usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))

        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                text_parts.append(piece)
                if on_delta:
                    on_delta(piece)
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                slot = tool_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    calls: list[Call] = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot["name"]:
            continue
        parsed = P.repair_json(slot["arguments"] or "{}")
        call = Call.new(slot["name"], parsed if isinstance(parsed, dict) else {})
        if slot["id"]:
            call.id = slot["id"]
        calls.append(call)

    return LLMResponse(text="".join(text_parts), tool_calls=calls, usage=usage, finish_reason=finish)


# ---------------------------------------------------------------------------
# 2) 官方 SDK provider
# ---------------------------------------------------------------------------
class OpenAISDKProvider(Provider):
    name = "openai"

    def __init__(self, base_url: str, api_key: str):
        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:  # pragma: no cover
            raise FatalLLMError(
                f"provider=openai 需要安装 openai 库：pip install openai（{e}）。"
                f"你也可以改用 provider=\"http\"，它只依赖标准库。"
            )
        self._client = OpenAI(base_url=base_url or None, api_key=api_key or "sk-noop")

    def chat(self, messages, *, model, tools, temperature, max_tokens, timeout, stream, on_delta):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        t0 = time.time()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # SDK 的异常类型随版本变化，统一按文本分类
            msg = str(e)
            low = msg.lower()
            if any(m in low for m in _CONTEXT_MARKERS):
                raise ContextOverflow(msg)
            if any(k in low for k in ("rate limit", "429", "timeout", "503", "502", "overloaded")):
                raise RetryableError(msg)
            raise FatalLLMError(msg)
        data = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
        out = _parse_completion(data)
        if on_delta and out.text:
            on_delta(out.text)
        out.model = model
        out.latency_ms = int((time.time() - t0) * 1000)
        return out


# ---------------------------------------------------------------------------
# 3) Mock provider（离线脚本回放）
# ---------------------------------------------------------------------------
class MockProvider(Provider):
    """按脚本回放模型响应。

    脚本是一个 JSON 数组，每个元素形如：
        {"text": "...", "tool_calls": [{"name": "read_file",
                                        "arguments": {"path": "a.py"}}]}
    也可以只写 {"text": "..."} 表示纯文本回答。
    脚本用尽后自动返回一次 finish，保证循环一定能收敛。

    还支持按角色分流：{"main": [...], "weak": [...]}。
    """

    name = "mock"

    def __init__(self, script: Any = None, path: str | Path | None = None):
        data: Any = script
        if data is None and path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data is None:
            data = []
        if isinstance(data, dict):
            self._scripts = {k: list(v) for k, v in data.items()}
        else:
            self._scripts = {"main": list(data)}
        self._cursor: dict[str, int] = {}
        self.calls_log: list[dict] = []

    def _next(self, role: str) -> dict | None:
        seq = self._scripts.get(role) or []
        i = self._cursor.get(role, 0)
        if i >= len(seq):
            return None
        self._cursor[role] = i + 1
        item = seq[i]
        return item if isinstance(item, dict) else {"text": str(item)}

    def chat(self, messages, *, model, tools, temperature, max_tokens, timeout, stream, on_delta):
        role = "weak" if model.startswith("__weak__") else "main"
        self.calls_log.append({"role": role, "messages": messages, "n_tools": len(tools or [])})
        item = self._next(role)
        if item is None:
            if role == "weak":
                item = {"text": "{}"}
            else:
                item = {
                    "text": "脚本已用尽，自动结束。",
                    "tool_calls": [{"name": "finish", "arguments": {"summary": "脚本已用尽"}}],
                }
        text = str(item.get("text") or "")
        calls: list[Call] = []
        for tc in item.get("tool_calls") or []:
            calls.append(Call.new(tc.get("name", ""), tc.get("arguments") or {}))
        if on_delta and text:
            on_delta(text)
        prompt_tokens = T.count_messages(messages, "")
        out_tokens = T.count_tokens(text, "") + 20 * len(calls)
        return LLMResponse(
            text=text, tool_calls=calls,
            usage=Usage(prompt_tokens, out_tokens),
            model=model, finish_reason="tool_calls" if calls else "stop", latency_ms=1,
        )


# ===========================================================================
# LLM 门面
# ===========================================================================
class LLM:
    def __init__(self, cfg: Config, provider: Provider | None = None):
        self.cfg = cfg
        self.mcfg = cfg.model
        self.provider = provider or self._build_provider()
        self.total = Usage()
        self.cost = 0.0
        self.n_calls = 0
        self.by_role: dict[str, dict] = {}

    # ---------------- 构建 ----------------
    def _build_provider(self) -> Provider:
        kind = (self.mcfg.provider or "http").lower()
        if kind == "mock":
            import os

            path = os.environ.get("VISTA_MOCK_SCRIPT")
            return MockProvider(path=path) if path else MockProvider(script=[])
        if kind == "openai":
            return OpenAISDKProvider(self.mcfg.base_url, self.cfg.api_key)
        return HttpProvider(self.mcfg.base_url, self.cfg.api_key)

    def model_for(self, role: str) -> str:
        if role == "weak":
            name = self.mcfg.weak_model
            if isinstance(self.provider, MockProvider):
                return "__weak__" + name
            return name
        return self.mcfg.main

    # ---------------- 主调用 ----------------
    def call(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        role: str = "main",
        on_delta: DeltaHook | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """带指数退避的一次模型调用。

        不可重试的错误（鉴权失败等）直接抛 FatalLLMError；
        ContextOverflow 原样抛给上层，由主循环触发强制压缩后重试。
        """
        model = self.model_for(role)
        last: Exception | None = None
        for attempt in range(self.mcfg.max_retries + 1):
            try:
                resp = self.provider.chat(
                    messages,
                    model=model,
                    tools=tools,
                    temperature=self.mcfg.temperature if temperature is None else temperature,
                    max_tokens=max_tokens or self.mcfg.max_tokens,
                    timeout=self.mcfg.timeout,
                    stream=self.mcfg.stream and role == "main" and on_delta is not None,
                    on_delta=on_delta,
                )
                self._account(resp, role, messages, tools)
                return resp
            except ContextOverflow:
                raise
            except RetryableError as e:
                last = e
                if attempt >= self.mcfg.max_retries:
                    break
                delay = min(2 ** attempt + random.uniform(0, 0.6), 20.0)
                time.sleep(delay)
            except FatalLLMError:
                raise
        raise RetryableError(f"重试 {self.mcfg.max_retries} 次后仍失败：{last}")

    def call_structured(
        self, prompt: str, role: str = "weak", system: str | None = None, expect: str = "object"
    ) -> Any | None:
        """要求模型只输出 JSON，并解析。失败返回 None（由调用方降级处理）。"""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = self.call(messages, tools=None, role=role, temperature=0.0)
        except Exception:
            return None
        return P.parse_structured(resp.text, expect=expect)

    def call_text(self, prompt: str, role: str = "weak", system: str | None = None) -> str | None:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            return self.call(messages, tools=None, role=role, temperature=0.0).text
        except Exception:
            return None

    # ---------------- 记账 ----------------
    def _account(self, resp: LLMResponse, role: str, messages: list[dict], tools: list[dict] | None) -> None:
        u = resp.usage
        if u.in_tokens == 0:  # 部分网关不回 usage，则本地估算
            u = Usage(
                T.count_messages(messages, resp.model) + T.count_tools_schema(tools or [], resp.model),
                T.count_tokens(resp.text, resp.model) + 20 * len(resp.tool_calls),
            )
            resp.usage = u
        self.total = self.total + u
        self.n_calls += 1
        cost = (u.in_tokens * self.mcfg.price_in + u.out_tokens * self.mcfg.price_out) / 1_000_000
        self.cost += cost
        slot = self.by_role.setdefault(role, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
        slot["calls"] += 1
        slot["in"] += u.in_tokens
        slot["out"] += u.out_tokens
        slot["cost"] += cost

    def stats(self) -> dict:
        return {
            "calls": self.n_calls,
            "in_tokens": self.total.in_tokens,
            "out_tokens": self.total.out_tokens,
            "cost": round(self.cost, 6),
            "by_role": {k: {**v, "cost": round(v["cost"], 6)} for k, v in self.by_role.items()},
            "provider": self.provider.name,
            "model_main": self.mcfg.main,
            "model_weak": self.mcfg.weak_model,
        }
