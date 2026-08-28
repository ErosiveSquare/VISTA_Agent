"""记忆工具与人机交互工具。"""

from __future__ import annotations

from ..errors import PermissionDenied, hint_for
from ..types import Anchor, ToolResult
from ..util.text import one_line
from .context import ToolContext
from .registry import tool


@tool(category="memory", reclaimable=True)
def memory_read(ctx: ToolContext, scope: str = "project", query: str = "") -> ToolResult:
    """读取 VISTA 的长期记忆。

    scope="project" 返回本项目的约定（构建命令、测试命令、目录职责等）；
    scope="skill" 返回与 query 相关的技能卡（来自过去已通过验证的任务）。

    Args:
        scope: project 或 skill
        query: scope=skill 时用于检索的关键词
    """
    scope = (scope or "project").strip().lower()
    if scope.startswith("proj"):
        if ctx.project is None:
            return ToolResult.err("memory_read", "MEMORY_NOT_FOUND", "项目记忆不可用。")
        text = ctx.project.render(ctx.cfg.memory.project_budget)
        if not text.strip():
            return ToolResult(
                ok=True, tool="memory_read", code="MEMORY_NOT_FOUND",
                content="本项目还没有记录任何长期记忆。",
                hint="如果你在本次任务中发现了值得长期保留的项目约定（例如测试命令），"
                     "可以用 memory_write 记录下来。",
                reclaimable=True,
            )
        return ToolResult(ok=True, tool="memory_read", content=text, reclaimable=True,
                          anchors=[Anchor(kind="map", ref="L2:project", digest="项目记忆")])

    if scope.startswith("skill"):
        if ctx.skills is None:
            return ToolResult.err("memory_read", "MEMORY_NOT_FOUND", "技能库不可用。")
        cards = ctx.skills.retrieve(query or "", k=3, force=True)
        if not cards:
            return ToolResult(ok=True, tool="memory_read", code="MEMORY_NOT_FOUND",
                              content=f"没有与「{one_line(query, 40)}」相关的技能卡。", reclaimable=True)
        body = "\n\n".join(c.render() for c in cards)
        return ToolResult(ok=True, tool="memory_read", content=body, reclaimable=True,
                          anchors=[Anchor(kind="map", ref="L3:skills",
                                          digest=f"{len(cards)} 张技能卡")])

    return ToolResult.err("memory_read", "BAD_ARGS", f"未知的 scope：{scope}",
                          "scope 只能是 project 或 skill。")


@tool(category="memory", mutating=True)
def memory_write(ctx: ToolContext, scope: str = "project", key: str = "",
                 content: str = "") -> ToolResult:
    """把一条值得跨会话保留的项目知识写入长期记忆。

    适合写入的内容：构建/测试命令、代码风格约定、目录职责、依赖管理方式。
    不适合写入的内容：本次任务的临时结论、具体的代码片段。

    Args:
        scope: 目前只支持 project
        key: 记忆分类，例如 build、verify、conventions、layout
        content: 记忆正文，一到三句话
    """
    scope = (scope or "project").strip().lower()
    if not scope.startswith("proj"):
        return ToolResult.err("memory_write", "BAD_ARGS",
                              "目前只支持写入 project 记忆；技能卡由系统在任务成功后自动蒸馏。")
    if ctx.project is None:
        return ToolResult.err("memory_write", "MEMORY_NOT_FOUND", "项目记忆不可用。")
    key = (key or "notes").strip()
    content = (content or "").strip()
    if not content:
        return ToolResult.err("memory_write", "BAD_ARGS", "content 不能为空。")

    verdict = ctx.permission.check("memory_write", {"scope": scope, "key": key})
    if verdict.decision == "deny":
        raise PermissionDenied("写入记忆被拒绝。")
    if verdict.decision == "ask":
        ok, always = ctx.ui.confirm(f"写入项目记忆 [{key}]：{one_line(content, 60)}", verdict.reason)
        if always:
            ctx.permission.remember_allow(ctx.permission.key_for("memory_write", {}))
        if not ok:
            raise PermissionDenied("用户拒绝写入记忆。")

    ctx.project.add(key, content)
    return ToolResult(ok=True, tool="memory_write",
                      content=f"已记入项目记忆 [{key}]：{one_line(content, 80)}\n"
                              f"（任务成功后才会落盘到 .vista/project.md）")


@tool(category="interact")
def ask_user(ctx: ToolContext, question: str, options: list[str] | None = None) -> ToolResult:
    """在无法自行判断时向用户提问。

    只在真正需要用户决策时使用（例如存在多个都合理但不可兼得的方案）。
    不要用它来确认你自己能查证的事实——那些应该用 grep / read_file / bash 去查。

    Args:
        question: 要问用户的问题
        options: 可选项列表；留空表示开放式提问
    """
    question = (question or "").strip()
    if not question:
        return ToolResult.err("ask_user", "BAD_ARGS", "question 不能为空。")
    if not ctx.cfg.interactive:
        return ToolResult(
            ok=False, tool="ask_user", code="NO_INTERACTIVE",
            content=f"当前是非交互模式，没有用户可以回答：{question}",
            hint=hint_for("NO_INTERACTIVE"),
        )
    answer = ctx.ui.ask(question, list(options) if options else None)
    if answer is None:
        return ToolResult(ok=False, tool="ask_user", code="NO_INTERACTIVE",
                          content="用户没有回答。", hint=hint_for("NO_INTERACTIVE"))
    return ToolResult(ok=True, tool="ask_user", content=f"用户回答：{answer}")
