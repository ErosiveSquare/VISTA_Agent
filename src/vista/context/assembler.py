"""上下文组装（Agent Loop 第 ① 步）。

每一轮都重新组装，而不是维护一个不断追加的 messages 列表。原因：
    - L1 RepoMap 会随焦点文件变化而变化，必须每轮重算注入位置
    - 系统提示中的安全约束因此天然"每轮重建"，压缩永远碰不到它（Constraint Pinning）
    - 组装结果是纯函数式的，便于 /context 命令展示构成、便于单元测试

组装顺序（也是 token 预算的分配顺序）：
    system = 系统提示 + 安全约束 + 验收方式 + L2 项目记忆 + L1 RepoMap + L3 技能卡
    然后是 history.view()：任务（pinned）→ 压缩标记 → 近期事件 → 任务清单（pinned）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..llm import tokens as T
from ..prompts import BASELINE_SYSTEM, build_system_prompt
from .history import History, strip_internal


@dataclass
class ContextBreakdown:
    """/context 命令展示的上下文构成。"""

    system: int = 0
    repo_map: int = 0
    project: int = 0
    skills: int = 0
    task: int = 0
    todo: int = 0
    compaction: int = 0
    recent: int = 0
    tools_schema: int = 0
    total: int = 0
    n_messages: int = 0
    parts: list[tuple[str, int]] = field(default_factory=list)

    def render(self, budget: int) -> str:
        pct = (self.total / budget * 100) if budget else 0
        lines = [f"上下文构成（共 {self.total:,} / {budget:,} tokens，{pct:.0f}%）"]
        width = max((len(n) for n, _ in self.parts), default=8)
        for i, (name, n) in enumerate(self.parts):
            branch = "└" if i == len(self.parts) - 1 else "├"
            lines.append(f"  {branch} {name.ljust(width)} {n:>7,}")
        return "\n".join(lines)


@dataclass
class Assembled:
    messages: list[dict]
    system: str
    breakdown: ContextBreakdown


def assemble(
    cfg: Config,
    history: History,
    *,
    repo_map_text: str = "",
    project_text: str = "",
    skills_text: str = "",
    verify_hint: str = "",
    tool_schemas: list[dict] | None = None,
) -> Assembled:
    model = cfg.model.main

    if cfg.baseline_mode:
        system = BASELINE_SYSTEM
        repo_map_text = project_text = skills_text = ""
    else:
        system = build_system_prompt(
            project_memory=project_text,
            repo_map=repo_map_text,
            skills=skills_text,
            verify_hint=verify_hint,
        )

    raw_view = history.view()
    messages = [{"role": "system", "content": system}] + strip_internal(raw_view)

    bd = ContextBreakdown()
    bd.repo_map = T.count_tokens(repo_map_text, model)
    bd.project = T.count_tokens(project_text, model)
    bd.skills = T.count_tokens(skills_text, model)
    bd.system = T.count_tokens(system, model) - bd.repo_map - bd.project - bd.skills
    bd.tools_schema = T.count_tools_schema(tool_schemas or [], model)

    for e in history.events:
        if not e.live:
            continue
        if e.kind == "task":
            bd.task += e.tokens
        elif e.kind == "todo":
            bd.todo += e.tokens
        elif e.kind == "compaction":
            bd.compaction += e.tokens
        else:
            bd.recent += e.tokens

    bd.n_messages = len(messages)
    bd.total = T.count_messages(messages, model) + bd.tools_schema

    bd.parts = [
        ("系统提示与安全约束", bd.system),
        ("L2 项目记忆", bd.project),
        ("L1 仓库索引", bd.repo_map),
        ("L3 技能卡", bd.skills),
        ("工具 schema", bd.tools_schema),
        ("任务（pinned）", bd.task),
        ("任务清单（pinned）", bd.todo),
        (f"压缩标记 x{history.n_compactions}", bd.compaction),
        ("近期事件", bd.recent),
    ]
    bd.parts = [(n, v) for n, v in bd.parts if v > 0]

    return Assembled(messages=messages, system=system, breakdown=bd)
