"""认证服务。"""

from __future__ import annotations

from .auth import TokenManager
from .users import User, UserRepo


class AuthService:
    def __init__(self) -> None:
        self.tokens = TokenManager()
        self.repo = UserRepo()

    def register(self, user_id: str, name: str, role: str = "member") -> str:
        self.repo.add(User(user_id=user_id, name=name, role=role))
        return self.tokens.issue(user_id)

    def whoami(self, token: str) -> User | None:
        sub = self.tokens.verify(token)
        return self.repo.find(sub) if sub else None

    def deactivate(self, user_id: str) -> bool:
        user = self.repo.find(user_id)
        if user is None:
            return False
        user.active = False
        return True
