"""审计日志。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AuditEntry:
    action: str
    user_id: str
    ts: float = field(default_factory=time.time)


class AuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, action: str, user_id: str) -> AuditEntry:
        entry = AuditEntry(action=action, user_id=user_id)
        self.entries.append(entry)
        return entry

    def for_user(self, user_id: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.user_id == user_id]
