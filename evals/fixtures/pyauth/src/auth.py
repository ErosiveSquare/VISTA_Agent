"""令牌签发与校验。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from .config import DEFAULT_TTL, SECRET


class TokenManager:
    def __init__(self, secret: str = SECRET):
        self.secret = secret

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")

    def issue(self, user_id: str, ttl: int = DEFAULT_TTL) -> str:
        exp = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        body = json.dumps({"sub": user_id, "exp": exp.isoformat()}, separators=(",", ":"))
        blob = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
        return f"{blob}.{self._sign(blob)}"

    def verify(self, token: str) -> str | None:
        try:
            blob, sig = token.rsplit(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sign(blob)):
            return None
        body = json.loads(_b64d(blob))
        exp = datetime.fromisoformat(body["exp"])
        # BUG: utcnow() 返回 naive datetime，与 aware 的 exp 无法比较
        if datetime.utcnow() > exp:
            return None
        return body["sub"]


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
