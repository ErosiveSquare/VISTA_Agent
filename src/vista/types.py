"""VISTA 核心数据模型。

这是整个项目的契约层：所有模块之间传递的结构都定义在这里。
本模块不依赖任何其它 vista 模块（架构分层 L1 底层）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

# --------------------------------------------------------------------------
# 事件种类
# --------------------------------------------------------------------------
EventKind = Literal[
    "task",  # 用户任务（pinned）
    "todo",  # TODO 列表快照（pinned，同类只保留最新一条）
    "assistant",  # 模型输出（可携带 tool calls）
    "tool_result",  # 工具执行结果
    "compaction",  # 压缩标记（覆盖一段历史）
    "verify",  # Verify-Gate 记录
    "note",  # scaffold 注入的系统提示
]

Role = Literal["system", "user", "assistant", "tool"]


# --------------------------------------------------------------------------
# Anchor —— 证据锚点
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Anchor:
    """指向"可重新获取"的证据来源。

    压缩时，可重取内容的正文被整段丢弃，只保留 Anchor。
    digest 由工具层生成（不经过 LLM），因此不会产生幻觉。
    """

    kind: Literal["file", "grep", "cmd", "map"]
    ref: str
    sha: str | None = None
    span: tuple[int, int] | None = None
    digest: str = ""
    tokens_saved: int = 0
    changed: bool = False  # 期间内容发生过变化

    def render(self) -> str:
        mark = "!" if self.changed else ""
        if self.kind == "file":
            span = f'{self.span[0]}-{self.span[1]}' if self.span else "all"
            sha = self.sha or "?"
            return f'<anchor{mark} file="{self.ref}" lines="{span}" sha="{sha}">{self.digest}</anchor>'
        return f'<anchor{mark} kind="{self.kind}" ref="{self.ref}">{self.digest}</anchor>'

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("span") is not None:
            d["span"] = list(d["span"])
        return d

    @staticmethod
    def from_dict(d: dict) -> "Anchor":
        span = d.get("span")
        return Anchor(
            kind=d.get("kind", "file"),
            ref=d.get("ref", ""),
            sha=d.get("sha"),
            span=tuple(span) if span else None,
            digest=d.get("digest", ""),
            tokens_saved=int(d.get("tokens_saved", 0) or 0),
            changed=bool(d.get("changed", False)),
        )


# --------------------------------------------------------------------------
# Call —— 一次工具调用
# --------------------------------------------------------------------------
@dataclass
class Call:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new(name: str, arguments: dict | None = None) -> "Call":
        return Call(id="call_" + uuid.uuid4().hex[:12], name=name, arguments=arguments or {})

    def signature(self) -> str:
        """用于无进展检测的稳定签名。"""
        import json

        try:
            body = json.dumps(self.arguments, sort_keys=True, ensure_ascii=False)
        except Exception:
            body = str(self.arguments)
        return f"{self.name}::{body}"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


# --------------------------------------------------------------------------
# ToolResult —— 统一的工具结果信封
# --------------------------------------------------------------------------
@dataclass
class ToolResult:
    ok: bool
    tool: str
    code: str = "OK"
    content: str = ""
    anchors: list[Anchor] = field(default_factory=list)
    reclaimable: bool = False
    truncated: dict | None = None
    hint: str | None = None
    mutated: list[str] = field(default_factory=list)
    cost_ms: int = 0

    def render(self) -> str:
        """渲染成给模型看的文本。"""
        parts: list[str] = []
        if not self.ok:
            parts.append(f"[{self.code}] {self.content}".rstrip())
            if self.hint:
                parts.append(f"建议：{self.hint}")
        else:
            if self.content:
                parts.append(self.content)
            if self.hint:
                parts.append(f"提示：{self.hint}")
        if self.truncated:
            t = self.truncated
            parts.append(
                f"[输出已截断：原始 {t.get('orig_bytes', 0)} 字节，保留 {t.get('kept_bytes', 0)} 字节]"
            )
        return "\n".join(p for p in parts if p) or "(无输出)"

    @staticmethod
    def err(tool: str, code: str, content: str, hint: str | None = None) -> "ToolResult":
        return ToolResult(ok=False, tool=tool, code=code, content=content, hint=hint)


# --------------------------------------------------------------------------
# Event —— 历史的原子单位（只追加，永不删除）
# --------------------------------------------------------------------------
@dataclass
class Event:
    idx: int
    kind: EventKind
    role: Role
    content: str
    ts: float = field(default_factory=time.time)
    tokens: int = 0
    pinned: bool = False
    reclaimable: bool = False
    anchors: list[Anchor] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    tool_name: str | None = None
    tool_call_id: str | None = None
    code: str | None = None
    superseded_by: int | None = None
    order: float = -1.0   # 视图排序键；压缩标记取被覆盖区间的起点
    meta: dict = field(default_factory=dict)

    @property
    def live(self) -> bool:
        """是否出现在发给模型的视图中。pinned 事件永远存活（不变式 I3）。"""
        return self.pinned or self.superseded_by is None

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "kind": self.kind,
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "tokens": self.tokens,
            "pinned": self.pinned,
            "reclaimable": self.reclaimable,
            "anchors": [a.to_dict() for a in self.anchors],
            "calls": [c.to_dict() for c in self.calls],
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "code": self.code,
            "superseded_by": self.superseded_by,
            "order": self.order,
            "meta": self.meta,
        }


# --------------------------------------------------------------------------
# KeyInfo —— 不可重取内容的结构化压缩产物（固定 schema）
# --------------------------------------------------------------------------
@dataclass
class KeyInfo:
    goal: str = ""
    verified_facts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    touched_files: list[dict] = field(default_factory=list)
    next_step: str = ""

    @staticmethod
    def from_dict(d: dict) -> "KeyInfo":
        def _slist(key: str, cap: int) -> list[str]:
            v = d.get(key) or []
            if isinstance(v, str):
                v = [v]
            return [str(x).strip() for x in v if str(x).strip()][:cap]

        files: list[dict] = []
        for item in (d.get("touched_files") or [])[:12]:
            if isinstance(item, dict):
                files.append(
                    {
                        "path": str(item.get("path", "")),
                        "change": str(item.get("change", "")),
                        "verified": bool(item.get("verified", False)),
                    }
                )
            elif isinstance(item, str):
                files.append({"path": item, "change": "", "verified": False})
        return KeyInfo(
            goal=str(d.get("goal", ""))[:300],
            verified_facts=_slist("verified_facts", 8),
            rejected=_slist("rejected", 5),
            open_questions=_slist("open_questions", 3),
            touched_files=files,
            next_step=str(d.get("next_step", ""))[:200],
        )

    def is_empty(self) -> bool:
        return not any(
            [self.verified_facts, self.rejected, self.open_questions, self.touched_files, self.next_step]
        )

    def render(self) -> str:
        lines: list[str] = []
        if self.goal:
            lines.append(f"任务目标：{self.goal}")
        if self.verified_facts:
            lines.append("已确立的事实：")
            lines += [f"  - {x}" for x in self.verified_facts]
        if self.rejected:
            lines.append("已排除的方向（不要重复尝试）：")
            lines += [f"  - {x}" for x in self.rejected]
        if self.open_questions:
            lines.append("待解决：")
            lines += [f"  - {x}" for x in self.open_questions]
        if self.touched_files:
            lines.append("已改动文件：")
            for f in self.touched_files:
                flag = "已验证" if f.get("verified") else "未验证"
                change = f" —— {f['change']}" if f.get("change") else ""
                lines.append(f"  - {f['path']}（{flag}）{change}")
        if self.next_step:
            lines.append(f"下一步：{self.next_step}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# TODO
# --------------------------------------------------------------------------
TodoStatus = Literal["pending", "doing", "done"]


@dataclass
class TodoItem:
    text: str
    status: TodoStatus = "pending"

    def render(self) -> str:
        mark = {"pending": "[ ]", "doing": "[~]", "done": "[x]"}.get(self.status, "[ ]")
        return f"{mark} {self.text}"


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
@dataclass
class Usage:
    in_tokens: int = 0
    out_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.in_tokens + other.in_tokens, self.out_tokens + other.out_tokens)


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[Call] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    latency_ms: int = 0
    raw: dict | None = None


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
@dataclass
class VerifyReport:
    passed: bool
    mode: str  # "test" | "lint" | "syntax" | "manual" | "skipped"
    command: str = ""
    exit_code: int = 0
    new_failures: list[str] = field(default_factory=list)
    known_failures: list[str] = field(default_factory=list)
    unfixed_targets: list[str] = field(default_factory=list)
    unfixed_targets: list[str] = field(default_factory=list)
    output: str = ""
    verified: bool = True  # False 表示"通过但未经真实验证"（降级模式）
    duration_ms: int = 0

    def render(self) -> str:
        head = "通过" if self.passed else "未通过"
        lines = [f"[VERIFY-GATE] {head}（模式：{self.mode}）"]
        if self.command:
            lines.append(f"命令：{self.command}")
            lines.append(f"退出码：{self.exit_code}")
        if self.new_failures:
            lines.append("新增失败（基线中没有的）：")
            lines += [f"  - {x}" for x in self.new_failures]
        if self.unfixed_targets:
            lines.append("本次任务应当修好、但仍然失败的用例：")
            lines += [f"  - {x}" for x in self.unfixed_targets]
        if self.unfixed_targets:
            lines.append("与本次改动相关、但仍未修复的既有失败：")
            lines += [f"  - {x}" for x in self.unfixed_targets]
        if self.known_failures:
            lines.append(f"既有失败（基线已有，不计入）：{', '.join(self.known_failures[:6])}")
        if self.output:
            lines.append("输出片段：")
            lines.append(self.output)
        if self.passed and not self.verified:
            lines.append("注意：本次未能进行真实验证，结果标记为 verified=false。")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# 运行结果
# --------------------------------------------------------------------------
RunStatus = Literal[
    "success",
    "answered",
    "steps_exhausted",
    "budget_exhausted",
    "stuck",
    "verify_exhausted",
    "parse_failure",
    "api_failure",
    "interrupted",
    "error",
]

TERMINAL_REASON = {
    "success": "T1b 模型声明完成且 Verify-Gate 通过",
    "answered": "T1a 模型返回纯文本回答，无工具调用",
    "steps_exhausted": "T2 步数预算耗尽",
    "budget_exhausted": "T3 成本预算耗尽",
    "stuck": "T4 连续无进展",
    "verify_exhausted": "Verify-Gate 连续失败达上限",
    "parse_failure": "模型输出连续无法解析",
    "api_failure": "模型接口重试耗尽",
    "interrupted": "T5 用户中断",
    "error": "内部错误",
}


@dataclass
class RunResult:
    status: RunStatus
    summary: str = ""
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    wall_ms: int = 0
    session_id: str = ""
    session_dir: str = ""
    verified: bool = False
    verify: VerifyReport | None = None
    mutated: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "answered")

    @property
    def reason(self) -> str:
        return TERMINAL_REASON.get(self.status, self.status)


# --------------------------------------------------------------------------
# 技能卡（L3）
# --------------------------------------------------------------------------
@dataclass
class SkillCard:
    name: str
    title: str = ""
    triggers: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    session_id: str = ""
    distilled_at: float = 0.0
    source_steps: int = 0
    usage_count: int = 0
    success_count: int = 0
    fail_streak: int = 0
    enabled: bool = True
    path: str = ""

    def render(self) -> str:
        lines = [f"## 技能卡：{self.title or self.name}"]
        if self.preconditions:
            lines.append("适用前提：")
            lines += [f"  - {x}" for x in self.preconditions]
        if self.steps:
            lines.append("参考步骤：")
            lines += [f"  {i}. {s}" for i, s in enumerate(self.steps, 1)]
        if self.pitfalls:
            lines.append("已知的坑：")
            lines += [f"  - {x}" for x in self.pitfalls]
        lines.append(
            f"（来自历史任务的经验，使用 {self.usage_count} 次 / 成功 {self.success_count} 次；"
            f"若与当前项目实际情况冲突，以实际情况为准）"
        )
        return "\n".join(lines)

    def to_yaml_dict(self) -> dict:
        return {
            "schema_version": 1,
            "name": self.name,
            "title": self.title,
            "triggers": list(self.triggers),
            "scope": {"languages": list(self.languages), "frameworks": list(self.frameworks)},
            "preconditions": list(self.preconditions),
            "steps": list(self.steps),
            "pitfalls": list(self.pitfalls),
            "provenance": {
                "session_id": self.session_id,
                "distilled_at": self.distilled_at,
                "source_steps": self.source_steps,
            },
            "stats": {
                "usage_count": self.usage_count,
                "success_count": self.success_count,
                "fail_streak": self.fail_streak,
                "enabled": self.enabled,
            },
        }

    @staticmethod
    def from_yaml_dict(d: dict, path: str = "") -> "SkillCard":
        scope = d.get("scope") or {}
        prov = d.get("provenance") or {}
        stats = d.get("stats") or {}

        def _l(v) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v]

        return SkillCard(
            name=str(d.get("name", "unnamed")),
            title=str(d.get("title", "")),
            triggers=_l(d.get("triggers")),
            languages=_l(scope.get("languages")),
            frameworks=_l(scope.get("frameworks")),
            preconditions=_l(d.get("preconditions")),
            steps=_l(d.get("steps")),
            pitfalls=_l(d.get("pitfalls")),
            session_id=str(prov.get("session_id", "")),
            distilled_at=float(prov.get("distilled_at", 0) or 0),
            source_steps=int(prov.get("source_steps", 0) or 0),
            usage_count=int(stats.get("usage_count", 0) or 0),
            success_count=int(stats.get("success_count", 0) or 0),
            fail_streak=int(stats.get("fail_streak", 0) or 0),
            enabled=bool(stats.get("enabled", True)),
            path=path,
        )
