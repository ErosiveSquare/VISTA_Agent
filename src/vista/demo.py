"""离线演示。

`vista demo` 不需要 API key、不需要网络：它用 MockProvider 回放一段预录的
模型响应脚本，在一个临时生成的 fixture 项目上跑通完整的 Agent Loop——
包括 TODO 拆解、RepoMap、grep 定位、指纹守卫拦截、Anchor Compression、
Verify-Gate 基线对比、技能卡蒸馏，最后产出 HTML 报告。

这个命令有三个用途：
    1. 任何人拿到仓库都能立刻看到系统在做什么，不必先配置模型
    2. 演示视频可以用它录制，结果完全确定、可复现
    3. 它本身就是一个端到端的冒烟测试
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from .config import Config, load_config

# ---------------------------------------------------------------------------
# fixture：一个带时区 bug 的极小项目
# ---------------------------------------------------------------------------
FIXTURE: dict[str, str] = {
    "src/__init__.py": "",
    "src/auth.py": '''"""极简的令牌签发与校验。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from .config import SECRET


class TokenManager:
    """签发与校验带过期时间的令牌。"""

    def __init__(self, secret: str = SECRET):
        self.secret = secret

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")

    def issue(self, user_id: str, ttl: int = 3600) -> str:
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
        # BUG: datetime.utcnow() 是 naive 的，与 aware 的 exp 无法比较
        if datetime.utcnow() > exp:
            return None
        return body["sub"]


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
''',
    "src/config.py": 'SECRET = "demo-secret-not-a-real-credential"\nTOKEN_TTL = 3600\n',
    "src/users.py": '''"""用户仓储（演示用的内存实现）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    user_id: str
    name: str
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
''',
    "src/service.py": '''"""把认证与用户仓储组合起来。"""

from __future__ import annotations

from .auth import TokenManager
from .users import User, UserRepo


class AuthService:
    def __init__(self) -> None:
        self.tokens = TokenManager()
        self.repo = UserRepo()

    def register(self, user_id: str, name: str) -> str:
        self.repo.add(User(user_id=user_id, name=name))
        return self.tokens.issue(user_id)

    def whoami(self, token: str) -> User | None:
        sub = self.tokens.verify(token)
        return self.repo.find(sub) if sub else None
''',
    "tests/__init__.py": "",
    "tests/test_auth.py": '''import unittest

from src.auth import TokenManager


class TestAuth(unittest.TestCase):
    def test_issue_and_verify(self):
        tm = TokenManager()
        token = tm.issue("alice", ttl=60)
        self.assertEqual(tm.verify(token), "alice")

    def test_tampered_token_rejected(self):
        tm = TokenManager()
        token = tm.issue("bob", ttl=60)
        self.assertIsNone(tm.verify(token[:-2] + "xx"))

    def test_expiry(self):
        tm = TokenManager()
        token = tm.issue("carol", ttl=-1)
        self.assertIsNone(tm.verify(token))
''',
    "tests/test_service.py": '''import unittest

from src.service import AuthService


class TestService(unittest.TestCase):
    def test_register_and_whoami(self):
        svc = AuthService()
        token = svc.register("dave", "Dave")
        user = svc.whoami(token)
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "Dave")
''',
    "tests/test_legacy.py": '''import unittest


class TestLegacy(unittest.TestCase):
    def test_legacy_login(self):
        # 这是项目里"原本就坏"的测试，用来演示 Verify-Gate 的基线对比：
        # VISTA 只要求不引入新的失败，不要求把历史遗留问题也一起修好。
        self.fail("legacy login flow not implemented yet")
''',
    "README.md": "# demo-auth\n\n一个用于演示 VISTA 的极小项目。`verify()` 里有一个时区相关的 bug。\n",
}

DEMO_TASK = "tests/test_auth.py::test_expiry 失败了，请定位并修复 src/auth.py 中的问题。"


def materialize_fixture(root: Path) -> None:
    for rel, body in FIXTURE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _script_path() -> Path:
    here = Path(__file__).resolve()
    for base in (here.parent, here.parent.parent, here.parent.parent.parent):
        cand = base / "examples" / "demo_pyauth.json"
        if cand.is_file():
            return cand
    raise FileNotFoundError("找不到 examples/demo_pyauth.json")


def run_demo(cfg: Config, keep: bool = False) -> int:
    from .cli.render import Console, TerminalUI
    from .llm.client import LLM, MockProvider
    from .loop import Agent
    from .report import write_report
    from . import __version__

    console = Console(color=cfg.color)
    workdir = Path(tempfile.mkdtemp(prefix="vista-demo-"))
    try:
        materialize_fixture(workdir)

        demo_cfg = load_config(workdir, {})
        demo_cfg.model.provider = "mock"
        demo_cfg.model.main = "demo-main"
        demo_cfg.model.weak = "demo-weak"
        demo_cfg.model.price_in = 0.15      # 仅用于展示成本核算，数字是示意
        demo_cfg.model.price_out = 0.60
        demo_cfg.interactive = False
        demo_cfg.permission.mode = "allow"
        demo_cfg.color = cfg.color
        # 演示项目很小，把索引与压缩的阈值调低，让机制在几步之内就能被看见
        demo_cfg.repomap.min_files = 3
        demo_cfg.context.recent_keep = 3
        demo_cfg.context.min_span = 3
        demo_cfg.context.budget = 3000
        demo_cfg.context.theta = 0.6
        demo_cfg.skills.min_steps = 3
        demo_cfg.limits.max_steps = 20
        # 演示项目只用标准库，验收命令显式指定为 unittest，
        # 这样 vista demo 在任何装了 Python 的机器上都能完整跑通。
        demo_cfg.verify.command = "python3 -m unittest discover -s tests -t . -q"

        console.banner(__version__, f"离线演示（mock provider，无需 API key） · {workdir}")
        console.write(console.style(
            "  演示项目：一个令牌校验模块，verify() 里混用了 naive 与 aware 的 datetime。", "grey"))
        console.write(console.style(
            "  tests/test_legacy.py 是故意留下的既有失败，用来展示 Verify-Gate 的基线对比。", "grey"))
        console.write()
        console.task(DEMO_TASK)
        console.write()

        script = json.loads(_script_path().read_text(encoding="utf-8"))
        llm = LLM(demo_cfg, provider=MockProvider(script=script))
        ui = TerminalUI(console, interactive=False)
        agent = Agent(demo_cfg, llm=llm, ui=ui, on_event=console.event)
        res = agent.run(DEMO_TASK)
        console.result(res)

        report = write_report(agent.session_dir)
        console.write()
        console.rule("演示产物")
        console.kv("HTML 报告", str(report))
        console.kv("会话轨迹", str(agent.session_dir / "trajectory.jsonl"))
        console.kv("项目记忆 L2", str(demo_cfg.project_file))
        console.kv("技能库 L3", str(demo_cfg.skills_dir))
        console.kv("上下文压缩", f"{agent.history.n_compactions} 次")
        console.kv("指纹拦截", f"{agent.ctx.stats.stale_blocked} 次")
        console.rule()

        if keep:
            console.ok(f"演示工作区已保留：{workdir}")
        else:
            keepdir = Path(cfg.cwd) / ".vista" / "demo"
            if keepdir.exists():
                shutil.rmtree(keepdir, ignore_errors=True)
            keepdir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workdir, keepdir)
            console.ok(f"演示产物已复制到：{keepdir}")
            console.write(console.style(f"  打开报告：{keepdir / '.vista' / 'sessions'}", "grey"))
        return 0 if res.ok else 1
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
