"""用户仓储（内存实现）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    user_id: str
    name: str
    role: str = "member"
    active: bool = True


class UserRepo:
    def __init__(self) -> None:
        self._rows: dict[str, User] = {}

    def add(self, user: User) -> User:
        self._rows[user.user_id] = user
        return user

    def find(self, user_id: str) -> User | None:
        return self._rows.get(user_id)

    def active_users(self) -> list[User]:
        return [u for u in self._rows.values() if u.active]

    def all_users(self) -> list[User]:
        return list(self._rows.values())
