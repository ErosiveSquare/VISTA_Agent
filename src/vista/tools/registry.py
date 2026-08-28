"""工具注册表。

职责：
    1. @tool 装饰器完成注册与元数据标注
    2. 从 Python 类型标注 + docstring 自动生成 OpenAI function schema
       （避免签名与 schema 两处维护、两处漂移）
    3. 统一异常兜底 —— 不变式 I4：工具内部的任何异常都转成
       ToolResult(ok=False)，绝不向上抛到主循环
"""

from __future__ import annotations

import inspect
import re
import types as _pytypes
import time
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, get_args, get_origin

from ..errors import BlockedCommand, PathEscape, PermissionDenied, hint_for
from ..types import Call, ToolResult
from ..util.paths import truncate_head_tail
from .context import ToolContext

_UnionType = getattr(_pytypes, "UnionType", None)

_ARG_RE = re.compile(r"^(\s+)(\w+)\s*:\s*(.+)$")


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., ToolResult]
    description: str
    parameters: dict
    mutating: bool = False
    reclaimable: bool = False
    control: bool = False       # 控制信号（finish / todo_write），不算"原子工具"
    category: str = "misc"
    required: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, ToolSpec] = {}


# ---------------------------------------------------------------------------
# 类型 → JSON Schema
# ---------------------------------------------------------------------------
def _json_type(ann: Any) -> dict:
    if ann is inspect.Parameter.empty or ann is Any:
        return {"type": "string"}
    origin = get_origin(ann)
    if origin is typing.Union or origin is _UnionType:
        args = [a for a in get_args(ann) if a is not type(None)]
        return _json_type(args[0]) if args else {"type": "string"}
    if origin in (list, typing.List) or ann is list:
        args = get_args(ann)
        return {"type": "array", "items": _json_type(args[0]) if args else {"type": "string"}}
    if origin in (dict, typing.Dict) or ann is dict:
        return {"type": "object"}
    if ann is str:
        return {"type": "string"}
    if ann is bool:
        return {"type": "boolean"}
    if ann is int:
        return {"type": "integer"}
    if ann is float:
        return {"type": "number"}
    if getattr(ann, "__name__", "") == "Literal" or origin is typing.Literal:
        vals = list(get_args(ann))
        return {"type": "string", "enum": [str(v) for v in vals]}
    return {"type": "string"}


def _parse_doc(doc: str | None) -> tuple[str, dict[str, str]]:
    """从 Google 风格 docstring 中解析描述与参数说明。"""
    if not doc:
        return "", {}
    lines = doc.expandtabs(4).split("\n")
    desc_lines: list[str] = []
    args: dict[str, str] = {}
    args_indent = -1
    last_arg: str | None = None
    for raw in lines:
        stripped = raw.rstrip()
        indent = len(stripped) - len(stripped.lstrip(" "))
        bare = stripped.strip()
        if args_indent < 0:
            if re.match(r"^(Args|Arguments|参数)\s*:\s*$", bare):
                args_indent = indent
                continue
            desc_lines.append(bare)
            continue
        if not bare:
            continue
        if indent <= args_indent:  # Args 段落结束
            args_indent = -1
            last_arg = None
            desc_lines.append(bare)
            continue
        m = _ARG_RE.match(stripped)
        if m and m.group(2) not in ("Returns", "Raises"):
            last_arg = m.group(2)
            args[last_arg] = m.group(3).strip()
        elif last_arg:  # 参数说明的续行
            args[last_arg] = (args[last_arg] + " " + bare).strip()
    return "\n".join(desc_lines).strip(), args


def tool(
    *,
    mutating: bool = False,
    reclaimable: bool = False,
    control: bool = False,
    category: str = "misc",
    name: str | None = None,
):
    """把一个普通函数注册为工具。

    被装饰的函数签名形如 fn(ctx: ToolContext, **params) -> ToolResult；
    ctx 不出现在给模型的 schema 中。
    """

    def deco(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        tool_name = name or fn.__name__
        sig = inspect.signature(fn)
        try:
            hints = typing.get_type_hints(fn)
        except Exception:
            hints = {}
        description, arg_docs = _parse_doc(fn.__doc__)

        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            if pname == "ctx":
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            schema = _json_type(hints.get(pname, param.annotation))
            if pname in arg_docs:
                schema["description"] = arg_docs[pname]
            if param.default is not inspect.Parameter.empty and param.default is not None:
                schema["default"] = param.default
            props[pname] = schema
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        spec = ToolSpec(
            name=tool_name,
            fn=fn,
            description=description,
            parameters={"type": "object", "properties": props, "required": required},
            mutating=mutating,
            reclaimable=reclaimable,
            control=control,
            category=category,
            required=required,
        )
        REGISTRY[tool_name] = spec
        fn.__vista_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return deco


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
def get(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)


def schemas(names: list[str] | None = None) -> list[dict]:
    if names is None:
        names = list(REGISTRY)
    return [REGISTRY[n].schema() for n in names if n in REGISTRY]


def tool_names(baseline: bool = False) -> list[str]:
    """返回本次运行暴露给模型的工具名。

    baseline=True 是消融开关：只保留 bash + finish，对标 mini-swe-agent
    那种"只有一个 shell 工具"的极简 scaffold。
    """
    if baseline:
        return [n for n in ("bash", "finish") if n in REGISTRY]
    order = [
        "read_file", "write_file", "edit_file",
        "grep", "repo_map",
        "bash",
        "memory_read", "memory_write",
        "ask_user",
        "todo_write", "finish",
    ]
    known = [n for n in order if n in REGISTRY]
    extra = [n for n in REGISTRY if n not in known]
    return known + extra


def _coerce(spec: ToolSpec, args: dict) -> tuple[dict, str | None]:
    """按 schema 做一次宽松的参数纠正，容忍模型的常见小错误。"""
    props = spec.parameters.get("properties", {})
    out: dict[str, Any] = {}
    unknown: list[str] = []
    for k, v in (args or {}).items():
        if k not in props:
            unknown.append(k)
            continue
        want = props[k].get("type")
        try:
            if want == "integer" and not isinstance(v, bool):
                out[k] = int(v)
            elif want == "number":
                out[k] = float(v)
            elif want == "boolean":
                out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "y")
            elif want == "array":
                out[k] = v if isinstance(v, list) else ([v] if v not in (None, "") else [])
            elif want == "string":
                out[k] = v if isinstance(v, str) else ("" if v is None else str(v))
            else:
                out[k] = v
        except (TypeError, ValueError):
            return {}, f"参数 {k} 的值 {v!r} 无法转换为 {want}。"

    missing = [r for r in spec.required if r not in out or out[r] in (None, "")]
    if missing:
        return {}, f"缺少必填参数：{', '.join(missing)}。"
    note = f"（忽略了未知参数：{', '.join(unknown)}）" if unknown else None
    return out, None if not unknown else note


def dispatch(call: Call, ctx: ToolContext) -> ToolResult:
    """执行一次工具调用。绝不抛出异常（不变式 I4）。"""
    t0 = time.time()
    spec = REGISTRY.get(call.name)
    if spec is None:
        available = ", ".join(tool_names(ctx.cfg.baseline_mode))
        return ToolResult.err(
            call.name, "UNKNOWN_TOOL",
            f"不存在名为 {call.name} 的工具。可用工具：{available}",
            hint_for("UNKNOWN_TOOL"),
        )

    args, err = _coerce(spec, call.arguments)
    if err and not args:
        return ToolResult.err(call.name, "BAD_ARGS", err, hint_for("BAD_ARGS"))

    ctx.stats.bump(call.name)
    try:
        result = spec.fn(ctx, **args)
    except PathEscape as e:
        result = ToolResult.err(call.name, "PATH_ESCAPE", str(e), hint_for("PATH_ESCAPE"))
    except PermissionDenied as e:
        ctx.stats.permission_denied += 1
        result = ToolResult.err(call.name, "PERMISSION_DENIED", str(e), hint_for("PERMISSION_DENIED"))
    except BlockedCommand as e:
        ctx.stats.blocked_command += 1
        result = ToolResult.err(call.name, "BLOCKED_COMMAND", str(e), hint_for("BLOCKED_COMMAND"))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # 兜底：任何异常都变成模型能看到的反馈
        result = ToolResult.err(
            call.name, "TOOL_ERROR",
            f"{type(e).__name__}: {e}",
            hint_for("TOOL_ERROR"),
        )

    if not isinstance(result, ToolResult):  # 防御性
        result = ToolResult(ok=True, tool=call.name, content=str(result))

    result.tool = call.name
    result.cost_ms = int((time.time() - t0) * 1000)
    if err:  # 未知参数提示
        result.hint = f"{result.hint or ''} {err}".strip()

    # 统一的结果体积上限，防止单个工具结果撑爆上下文
    cap = ctx.cfg.tools.tool_result_bytes
    if len(result.content) > cap:
        result.content, meta = truncate_head_tail(result.content, cap)
        result.truncated = meta

    if result.mutated:
        # "<workspace>" 是 bash 用来触发快照的哨兵，不是真实文件路径，
        # 不能让它流进 mutated_files —— 那会污染 Verify-Gate 的目标判定与最终报告。
        for path in result.mutated:
            if path and path != "<workspace>":
                ctx.mutated_files.add(path)
    return result
