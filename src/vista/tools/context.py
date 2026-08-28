"""ToolContext —— 工具层需要的依赖的窄接口。

架构上，tools 属于 L2 执行层，不允许 import L3 能力层（context/memory）或
L4 编排层（loop）。因此这里定义一个由 loop 组装、向下传递的窄接口对象，
避免出现反向依赖。memory 相关字段用鸭子类型持有，仅调用其公开方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..config import Config
from ..safety.permission import PermissionPolicy
from ..safety.snapshot import SnapshotStore
from ..types import TodoItem


class UI(Protocol):
    """工具需要与用户交互时使用的最小界面。"""

    def confirm(self, title: str, detail: str = "") -> tuple[bool, bool]:
        """返回 (是否允许, 是否始终允许)。"""

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        """向用户提问，返回回答；非交互环境返回 None。"""

    def notify(self, text: str, kind: str = "info") -> None: ...


@dataclass
class ToolStats:
    stale_blocked: int = 0
    no_match: int = 0
    ambiguous: int = 0
    permission_denied: int = 0
    blocked_command: int = 0
    timeouts: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)

    def bump(self, tool: str) -> None:
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1

    def to_dict(self) -> dict:
        return {
            "stale_blocked": self.stale_blocked,
            "no_match": self.no_match,
            "ambiguous": self.ambiguous,
            "permission_denied": self.permission_denied,
            "blocked_command": self.blocked_command,
            "timeouts": self.timeouts,
            "by_tool": dict(self.by_tool),
        }


@dataclass
class ToolContext:
    cfg: Config
    root: Path
    ledger: Any                      # tools.files.FileLedger
    permission: PermissionPolicy
    ui: UI
    snapshots: SnapshotStore | None = None
    repomap: Any = None              # memory.repomap.RepoMap
    project: Any = None              # memory.project.ProjectMemory
    skills: Any = None               # memory.skills.SkillIndex
    stats: ToolStats = field(default_factory=ToolStats)
    step: int = 0
    todos: list[TodoItem] = field(default_factory=list)
    finish_summary: str | None = None
    on_todo_change: Callable[[list[TodoItem]], None] | None = None
    mutated_files: set[str] = field(default_factory=set)


class NullUI:
    """非交互环境下的默认界面：一律拒绝需要确认的操作之外的交互。

    注意 confirm 返回 True——因为非交互模式下的权限判定已经在
    PermissionPolicy 里完成了，走到 confirm 说明策略已允许。
    """

    def confirm(self, title: str, detail: str = "") -> tuple[bool, bool]:
        return True, False

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        return None

    def notify(self, text: str, kind: str = "info") -> None:
        pass
