"""控制信号：todo_write 与 finish。

这两个不是"原子工具"（它们不与外部环境交互），但走同一条分发路径，
以便统一记账、统一异常兜底、统一进入历史。

finish 的语义是本项目的核心设计之一：它只是"请求结束"，
真正是否结束由 Verify-Gate 裁定（见 verify.py）。
"""

from __future__ import annotations

from ..types import TodoItem, ToolResult
from .context import ToolContext
from .registry import tool

_VALID_STATUS = {"pending", "doing", "done"}


@tool(category="control", control=True)
def todo_write(ctx: ToolContext, items: list[dict] | None = None) -> ToolResult:
    """写入或更新任务清单。清单会常驻在上下文中，且不会被压缩掉。

    面对需要多步完成的任务时，先用它把任务拆成 3-7 个可验证的小步；
    每完成一步就再调用一次把该项标记为 done。

    Args:
        items: 清单项数组，每项形如 {"text": "…", "status": "pending|doing|done"}
    """
    items = items or []
    if not isinstance(items, list) or not items:
        return ToolResult.err("todo_write", "BAD_ARGS",
                              "items 不能为空。", '格式：[{"text": "…", "status": "pending"}]')

    todos: list[TodoItem] = []
    for raw in items[:20]:
        if isinstance(raw, str):
            todos.append(TodoItem(text=raw.strip()))
            continue
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or raw.get("title") or "").strip()
        if not text:
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in _VALID_STATUS:
            status = "pending"
        todos.append(TodoItem(text=text[:160], status=status))  # type: ignore[arg-type]

    if not todos:
        return ToolResult.err("todo_write", "BAD_ARGS", "没有解析到任何有效的清单项。")

    ctx.todos = todos
    if ctx.on_todo_change:
        ctx.on_todo_change(todos)

    done = sum(1 for t in todos if t.status == "done")
    body = "\n".join(t.render() for t in todos)
    return ToolResult(ok=True, tool="todo_write",
                      content=f"任务清单已更新（{done}/{len(todos)} 完成）：\n{body}")


@tool(category="control", control=True)
def finish(ctx: ToolContext, summary: str = "") -> ToolResult:
    """声明你认为任务已经完成。

    重要：调用它并不会立刻结束任务。系统会自动运行本项目的测试与静态检查，
    并与任务开始时的基线对比——只有不引入新的失败，任务才算成功；
    否则失败信息会被反馈给你，你需要继续修复。
    因此在调用 finish 之前，建议你先自己跑一次测试。

    如果本次任务本身就会让某些测试变红（例如"请修改测试用例"），
    请在 summary 中明确说明。

    Args:
        summary: 你做了什么、改了哪些文件、为什么这样改
    """
    ctx.finish_summary = (summary or "").strip()
    return ToolResult(ok=True, tool="finish",
                      content="已收到完成声明，正在执行 Verify-Gate 验收…")
