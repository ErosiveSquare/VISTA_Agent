#!/usr/bin/env python3
"""生成评测用的 fixture 项目。

三个项目都刻意只用标准库：
  - 评测机器不需要装任何东西就能跑，结果可复现
  - 验收命令统一用 `python3 -m unittest`，不受 pytest 版本影响
  - 每个项目都留有既有失败（legacy），用来检验 Verify-Gate 的基线对比

运行：python evals/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures"


# ===========================================================================
# fixture 1：pyauth —— 令牌服务，带一个时区 bug
# ===========================================================================
PYAUTH: dict[str, str] = {
    "src/__init__.py": "",
    "src/config.py": 'SECRET = "fixture-secret-not-a-real-credential"\nDEFAULT_TTL = 3600\n',
    "src/auth.py": '''"""令牌签发与校验。"""

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
''',
    "src/users.py": '''"""用户仓储（内存实现）。"""

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
''',
    "src/service.py": '''"""认证服务。"""

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
''',
    "src/audit.py": '''"""审计日志。"""

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
''',
    "tests/__init__.py": "",
    "tests/test_auth.py": '''import unittest

from src.auth import TokenManager


class TestAuth(unittest.TestCase):
    def test_issue_and_verify(self):
        tm = TokenManager()
        self.assertEqual(tm.verify(tm.issue("alice", ttl=60)), "alice")

    def test_tampered_rejected(self):
        tm = TokenManager()
        token = tm.issue("bob", ttl=60)
        self.assertIsNone(tm.verify(token[:-2] + "xx"))

    def test_expiry(self):
        tm = TokenManager()
        self.assertIsNone(tm.verify(tm.issue("carol", ttl=-1)))
''',
    "tests/test_service.py": '''import unittest

from src.service import AuthService


class TestService(unittest.TestCase):
    def test_register_and_whoami(self):
        svc = AuthService()
        user = svc.whoami(svc.register("dave", "Dave"))
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "Dave")

    def test_deactivate(self):
        svc = AuthService()
        svc.register("erin", "Erin")
        self.assertTrue(svc.deactivate("erin"))
        self.assertEqual(svc.repo.active_users(), [])
''',
    "tests/test_legacy.py": '''import unittest


class TestLegacy(unittest.TestCase):
    def test_legacy_login(self):
        # 项目里原本就失败的用例。Verify-Gate 的基线对比应当把它识别为
        # "既有失败"，不计入本次任务的责任范围。
        self.fail("legacy login flow not implemented yet")
''',
    "README.md": "# pyauth fixture\n\n令牌签发与校验服务。`verify()` 中有一个时区相关的 bug。\n",
}


# ===========================================================================
# fixture 2：pystore —— 库存服务，多文件改动
# ===========================================================================
PYSTORE: dict[str, str] = {
    "store/__init__.py": "",
    "store/models.py": '''"""领域模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    price_cents: int
    stock: int = 0

    @property
    def in_stock(self) -> bool:
        return self.stock > 0


@dataclass
class OrderLine:
    sku: str
    qty: int
''',
    "store/inventory.py": '''"""库存。"""

from __future__ import annotations

from .models import Item


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def put(self, item: Item) -> Item:
        self._items[item.sku] = item
        return item

    def get(self, sku: str) -> Item | None:
        return self._items.get(sku)

    def reserve(self, sku: str, qty: int) -> None:
        item = self._items.get(sku)
        if item is None:
            raise KeyError(sku)
        if item.stock < qty:
            raise OutOfStock(f"{sku}: 库存 {item.stock}，需要 {qty}")
        item.stock -= qty

    def restock(self, sku: str, qty: int) -> int:
        item = self._items[sku]
        item.stock += qty
        return item.stock

    def list_items(self) -> list[Item]:
        return list(self._items.values())
''',
    "store/pricing.py": '''"""计价。"""

from __future__ import annotations

from .models import OrderLine


class Pricing:
    def __init__(self, tax_rate: float = 0.0):
        self.tax_rate = tax_rate

    def subtotal(self, lines: list[OrderLine], catalog) -> int:
        total = 0
        for line in lines:
            item = catalog.get(line.sku)
            if item is None:
                raise KeyError(line.sku)
            total += item.price_cents * line.qty
        return total

    def total(self, lines: list[OrderLine], catalog) -> int:
        sub = self.subtotal(lines, catalog)
        return sub + round(sub * self.tax_rate)
''',
    "store/orders.py": '''"""下单。"""

from __future__ import annotations

from .inventory import Inventory
from .models import OrderLine
from .pricing import Pricing


class OrderService:
    def __init__(self, inventory: Inventory, pricing: Pricing | None = None):
        self.inventory = inventory
        self.pricing = pricing or Pricing()
        self.orders: list[dict] = []

    def place(self, lines: list[OrderLine]) -> dict:
        for line in lines:
            self.inventory.reserve(line.sku, line.qty)
        order = {
            "id": len(self.orders) + 1,
            "lines": lines,
            "total": self.pricing.total(lines, self.inventory),
        }
        self.orders.append(order)
        return order

    def find(self, order_id: int) -> dict | None:
        return next((o for o in self.orders if o["id"] == order_id), None)
''',
    "tests/__init__.py": "",
    "tests/test_inventory.py": '''import unittest

from store.inventory import Inventory, OutOfStock
from store.models import Item


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        self.inv.put(Item(sku="A1", name="Widget", price_cents=1000, stock=5))

    def test_reserve(self):
        self.inv.reserve("A1", 3)
        self.assertEqual(self.inv.get("A1").stock, 2)

    def test_out_of_stock(self):
        with self.assertRaises(OutOfStock):
            self.inv.reserve("A1", 99)

    def test_restock(self):
        self.assertEqual(self.inv.restock("A1", 5), 10)
''',
    "tests/test_orders.py": '''import unittest

from store.inventory import Inventory
from store.models import Item, OrderLine
from store.orders import OrderService
from store.pricing import Pricing


class TestOrders(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        self.inv.put(Item(sku="A1", name="Widget", price_cents=1000, stock=10))
        self.inv.put(Item(sku="B2", name="Gadget", price_cents=250, stock=10))
        self.svc = OrderService(self.inv, Pricing(tax_rate=0.1))

    def test_place_order(self):
        order = self.svc.place([OrderLine("A1", 2), OrderLine("B2", 4)])
        self.assertEqual(order["total"], round(3000 * 1.1))
        self.assertEqual(self.inv.get("A1").stock, 8)

    def test_find(self):
        order = self.svc.place([OrderLine("A1", 1)])
        self.assertEqual(self.svc.find(order["id"]), order)
''',
    "README.md": "# pystore fixture\n\n一个小型库存与下单服务。\n",
}


# ===========================================================================
# fixture 3：jsutil —— 纯 Node，验证跨语言通用性
# ===========================================================================
JSUTIL: dict[str, str] = {
    "package.json": '''{
  "name": "jsutil-fixture",
  "version": "1.0.0",
  "private": true,
  "type": "commonjs",
  "scripts": {
    "test": "node --test test/"
  }
}
''',
    "src/slug.js": '''"use strict";

/** 把标题转成 URL slug。 */
function slugify(input) {
  if (typeof input !== "string") return "";
  return input
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

module.exports = { slugify };
''',
    "src/paginate.js": '''"use strict";

/** 对数组做分页。 */
function paginate(items, page, perPage) {
  const p = Number(page) || 1;
  const size = Number(perPage) || 20;
  const start = (p - 1) * size;
  return {
    items: items.slice(start, start + size),
    page: p,
    perPage: size,
    total: items.length,
    totalPages: Math.ceil(items.length / size),
  };
}

module.exports = { paginate };
''',
    "src/index.js": '''"use strict";

const { slugify } = require("./slug");
const { paginate } = require("./paginate");

module.exports = { slugify, paginate };
''',
    "test/slug.test.js": '''"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { slugify } = require("../src/slug");

test("basic slug", () => {
  assert.strictEqual(slugify("Hello World"), "hello-world");
});

test("strips punctuation", () => {
  assert.strictEqual(slugify("  A, B & C!  "), "a-b-c");
});

test("non-string input", () => {
  assert.strictEqual(slugify(null), "");
});
''',
    "test/paginate.test.js": '''"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { paginate } = require("../src/paginate");

const items = Array.from({ length: 45 }, (_, i) => i + 1);

test("first page", () => {
  const r = paginate(items, 1, 20);
  assert.strictEqual(r.items.length, 20);
  assert.strictEqual(r.totalPages, 3);
});

test("last page", () => {
  const r = paginate(items, 3, 20);
  assert.strictEqual(r.items.length, 5);
});
''',
    "README.md": "# jsutil fixture\n\n纯 Node 的小工具库，用来验证 VISTA 的跨语言通用性。\n",
}


FIXTURES = {"pyauth": PYAUTH, "pystore": PYSTORE, "jsutil": JSUTIL}


def main() -> int:
    for name, files in FIXTURES.items():
        base = ROOT / name
        for rel, body in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        print(f"生成 fixture：{base}（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
