"""L1/L2/L3 记忆层与 Verify-Gate 的测试。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from vista.config import RepoMapCfg, SkillsCfg, load_config
from vista.memory.project import ProjectMemory, detect_commands, merge_detected
from vista.memory.repomap import RepoMap, pagerank
from vista.memory.skills import SkillIndex
from vista.memory.symbols import extract_tags
from vista.types import SkillCard
from vista.util import miniyaml
from vista.verify import VerifyGate, extract_failure_detail, parse_failures


# ===========================================================================
class TestPageRank(unittest.TestCase):
    def test_uniform_cycle(self):
        """三节点环、均匀个性化 → 三者得分相等。"""
        r = pagerank(3, [{1: 1.0}, {2: 1.0}, {0: 1.0}], [1, 1, 1])
        for x in r:
            self.assertAlmostEqual(x, 1 / 3, places=4)
        self.assertAlmostEqual(sum(r), 1.0, places=6)

    def test_hub_scores_highest(self):
        """B、C 都指向 A → A 得分最高。"""
        r = pagerank(3, [{}, {0: 1.0}, {0: 1.0}], [1, 1, 1])
        self.assertGreater(r[0], r[1])
        self.assertGreater(r[0], r[2])

    def test_personalization_shifts_mass(self):
        base = pagerank(3, [{1: 1.0}, {2: 1.0}, {0: 1.0}], [1, 1, 1])
        focused = pagerank(3, [{1: 1.0}, {2: 1.0}, {0: 1.0}], [1, 1, 20])
        self.assertGreater(focused[2], base[2])
        self.assertEqual(max(focused), focused[2])

    def test_dangling_node_conserves_mass(self):
        r = pagerank(3, [{1: 1.0}, {}, {}], [1, 1, 1])
        self.assertAlmostEqual(sum(r), 1.0, places=6)

    def test_empty_graph(self):
        self.assertEqual(pagerank(0, [], []), [])


class TestSymbols(unittest.TestCase):
    def test_python(self):
        tags = extract_tags("a.py", "class Foo:\n    def bar(self):\n        return baz()\n")
        names = {d.name for d in tags.defs}
        self.assertIn("Foo", names)
        self.assertIn("bar", names)
        self.assertIn("baz", tags.refs)

    def test_javascript(self):
        tags = extract_tags("a.js", "export class Widget {}\nfunction render(){ return mount(); }\n")
        names = {d.name for d in tags.defs}
        self.assertIn("Widget", names)
        self.assertIn("render", names)

    def test_go(self):
        tags = extract_tags("a.go", "type Server struct {}\nfunc Serve() error { return nil }\n")
        self.assertIn("Server", {d.name for d in tags.defs})
        self.assertIn("Serve", {d.name for d in tags.defs})

    def test_unknown_extension(self):
        self.assertEqual(extract_tags("a.xyz", "whatever").defs, [])


class TestRepoMap(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vista-rm-"))
        for i in range(20):
            (self.root / f"m{i}.py").write_text(
                f"from core import Engine\n\n\nclass M{i}:\n    def run(self):\n        return Engine().start()\n",
                encoding="utf-8")
        (self.root / "core.py").write_text(
            "class Engine:\n    def start(self):\n        return 1\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_budget_respected(self):
        rm = RepoMap(self.root, RepoMapCfg(min_files=3))
        rm.build()
        for budget in (120, 400, 1024):
            text, used = rm.render([], budget)
            self.assertLessEqual(used, budget, f"budget={budget} used={used}")
            self.assertTrue(text.strip())

    def test_hub_file_ranked_first(self):
        """被 20 个文件引用的 core.py 应该排在最前面。"""
        rm = RepoMap(self.root, RepoMapCfg(min_files=3))
        rm.build()
        top = rm.top_files(3)
        self.assertEqual(top[0][0], "core.py")

    def test_small_repo_auto_disabled(self):
        tiny = Path(tempfile.mkdtemp(prefix="vista-tiny-"))
        try:
            (tiny / "a.py").write_text("x = 1\n", encoding="utf-8")
            rm = RepoMap(tiny, RepoMapCfg(min_files=15))
            stats = rm.build()
            self.assertFalse(stats.enabled)
            self.assertFalse(rm.available)
            self.assertIn("阈值", stats.reason)
        finally:
            shutil.rmtree(tiny, ignore_errors=True)

    def test_per_file_cap(self):
        """单个文件的琐碎方法不应霸占整张索引。"""
        rm = RepoMap(self.root, RepoMapCfg(min_files=3, per_file_cap=2))
        rm.build()
        text, _ = rm.render([], 2000)
        for line in text.split("\n"):
            pass
        blocks = {}
        current = None
        for line in text.split("\n")[1:]:
            if line and not line.startswith("  "):
                current = line
                blocks[current] = 0
            elif current:
                blocks[current] += 1
        for f, n in blocks.items():
            self.assertLessEqual(n, 2, f)


# ===========================================================================
class TestProjectMemory(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vista-pm-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_roundtrip(self):
        path = self.root / "project.md"
        pm = ProjectMemory.load(path)
        pm.add("verify", "测试命令：pytest -q")
        pm.add("conventions", "路由集中在 src/api/routes/")
        self.assertTrue(pm.save())
        again = ProjectMemory.load(path)
        self.assertIn("测试命令：pytest -q", again.get("verify"))
        self.assertIn("路由集中在 src/api/routes/", again.get("conventions"))

    def test_no_duplicates(self):
        pm = ProjectMemory.load(self.root / "p.md")
        pm.add("verify", "pytest -q")
        pm.add("verify", "pytest -q")
        self.assertEqual(len(pm.get("verify")), 1)

    def test_budget_truncation(self):
        pm = ProjectMemory.load(self.root / "p.md")
        for i in range(200):
            pm.add("notes", f"这是第 {i} 条相当长的项目记忆条目，用来测试预算裁剪逻辑是否生效")
        rendered = pm.render(budget=200)
        from vista.llm.tokens import count_tokens

        self.assertLess(count_tokens(rendered), 320)

    def test_detect_python(self):
        (self.root / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        det = detect_commands(self.root)
        self.assertEqual(det["language"], "python")
        # 探测到 pytest 配置时用 pytest；当前环境没装 pytest 则降级到标准库 unittest。
        # 两条路径都是正确行为，关键是"必须探测出一个可执行的验收命令"。
        self.assertTrue(
            det["test"].startswith("pytest") or "unittest" in det["test"],
            det["test"],
        )

    def test_detect_node(self):
        (self.root / "package.json").write_text(
            '{"scripts":{"test":"jest","build":"vite build"},"dependencies":{"react":"18"}}',
            encoding="utf-8")
        det = detect_commands(self.root)
        self.assertIn("test", det["test"])
        self.assertIn("react", det["framework"])

    def test_detect_go(self):
        (self.root / "go.mod").write_text("module x\n", encoding="utf-8")
        det = detect_commands(self.root)
        self.assertEqual(det["test"], "go test ./...")

    def test_merge_detected(self):
        pm = ProjectMemory.load(self.root / "p.md")
        merge_detected(pm, {"test": "pytest -q", "lint": "ruff check .",
                            "build": "", "language": "python", "framework": ["fastapi"]})
        text = pm.render(4000)
        self.assertIn("pytest -q", text)
        self.assertIn("ruff check .", text)
        self.assertIn("fastapi", text)


# ===========================================================================
class TestSkills(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vista-sk-"))
        self.cfg = SkillsCfg(min_score=0.1)
        self.idx = SkillIndex(self.dir, self.cfg)
        for name, title, triggers, langs in [
            ("add-rest-endpoint", "新增 REST 接口",
             ["新增接口", "add endpoint", "REST", "路由"], ["python"]),
            ("fix-timezone", "修复时区问题",
             ["时区", "timezone", "utcnow", "naive"], ["python"]),
            ("upgrade-deps", "升级依赖", ["升级依赖", "upgrade", "bump"], ["javascript"]),
        ]:
            card = SkillCard(name=name, title=title, triggers=triggers, languages=langs,
                             steps=["步骤一", "步骤二", "步骤三"], pitfalls=["坑"])
            self.idx.save_card(card)
            self.idx.cards.append(card)
        self.idx._build_idf()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_retrieval_ranks_correct_card(self):
        got = self.idx.retrieve("verify 报 utcnow 时区错误，请修复", k=1)
        self.assertEqual(got[0].name, "fix-timezone")

    def test_retrieval_respects_language_scope(self):
        py = self.idx.retrieve("升级依赖", k=1, scope={"language": "python", "framework": []})
        js = self.idx.retrieve("升级依赖", k=1, scope={"language": "javascript", "framework": []})
        self.assertEqual(js[0].name, "upgrade-deps")
        if py:
            self.assertLessEqual(
                self.idx.score(py[0], {"升级", "依赖", "upgrade"}, {"language": "python"}),
                self.idx.score(js[0], {"升级", "依赖", "upgrade"}, {"language": "javascript"}),
            )

    def test_no_match_returns_empty(self):
        self.assertEqual(self.idx.retrieve("完全无关的天气查询任务"), [])

    def test_fail_streak_disables(self):
        card = self.idx.cards[0]
        self.idx.record_outcome([card], success=False)
        self.assertTrue(card.enabled)
        self.idx.record_outcome([card], success=False)
        self.assertFalse(card.enabled)
        self.assertEqual(SkillIndex.load(self.dir, self.cfg).cards[0].enabled, False)

    def test_success_resets_streak(self):
        card = self.idx.cards[0]
        self.idx.record_outcome([card], success=False)
        self.idx.record_outcome([card], success=True)
        self.assertEqual(card.fail_streak, 0)
        self.assertEqual(card.success_count, 1)

    def test_disabled_not_retrieved(self):
        self.idx.set_enabled("fix-timezone", False)
        got = self.idx.retrieve("utcnow 时区问题", k=2)
        self.assertNotIn("fix-timezone", [c.name for c in got])

    def test_should_distill_conditions(self):
        ok, _ = self.idx.should_distill(success=True, steps=10, mutated=2, hit_existing=False)
        self.assertTrue(ok)
        for kwargs in (
            {"success": False, "steps": 10, "mutated": 2, "hit_existing": False},
            {"success": True, "steps": 2, "mutated": 2, "hit_existing": False},
            {"success": True, "steps": 10, "mutated": 0, "hit_existing": False},
            {"success": True, "steps": 10, "mutated": 2, "hit_existing": True},
        ):
            self.assertFalse(self.idx.should_distill(**kwargs)[0], kwargs)

    def test_distill_skip(self):
        class FakeLLM:
            def call_text(self, *a, **k):
                return "SKIP"

        self.assertIsNone(self.idx.distill(FakeLLM(), "摘要", "sid", 8))

    def test_distill_creates_card(self):
        class FakeLLM:
            def call_text(self, *a, **k):
                return ("name: handle-pagination\ntitle: 分页处理\n"
                        "triggers:\n  - 分页\n  - pagination\n"
                        "scope:\n  languages:\n    - python\n  frameworks: []\n"
                        "steps:\n  - 定位列表接口\n  - 加 page 参数\n  - 补测试\n"
                        "pitfalls:\n  - 忘记边界值\n")

        card = self.idx.distill(FakeLLM(), "摘要", "sid-1", 9)
        self.assertIsNotNone(card)
        self.assertEqual(card.name, "handle-pagination")
        self.assertEqual(card.source_steps, 9)
        self.assertTrue(Path(card.path).is_file())

    def test_yaml_roundtrip(self):
        card = self.idx.cards[1]
        text = miniyaml.dump_text(card.to_yaml_dict())
        back = SkillCard.from_yaml_dict(miniyaml.loads(text))
        self.assertEqual(back.name, card.name)
        self.assertEqual(back.triggers, card.triggers)
        self.assertEqual(back.steps, card.steps)


# ===========================================================================
class TestVerify(unittest.TestCase):
    PYTEST_OUT = (
        "FAILED tests/test_auth.py::test_expiry - TypeError: bad\n"
        "FAILED tests/test_legacy.py::test_login - AssertionError\n"
        "2 failed, 22 passed\n"
    )
    UNITTEST_OUT_311 = (
        "FAIL: test_expiry (tests.test_auth.TestAuth.test_expiry)\n"
        "AssertionError: boom\n"
    )
    UNITTEST_OUT_310 = "FAIL: test_expiry (tests.test_auth.TestAuth)\nAssertionError: boom\n"

    def test_parse_pytest(self):
        failures, fw = parse_failures(self.PYTEST_OUT)
        self.assertEqual(fw, "pytest")
        self.assertIn("tests/test_auth.py::test_expiry", failures)
        self.assertEqual(len(failures), 2)

    def test_parse_unittest_both_formats(self):
        for out in (self.UNITTEST_OUT_311, self.UNITTEST_OUT_310):
            failures, fw = parse_failures(out)
            self.assertEqual(fw, "unittest")
            self.assertEqual(failures, {"tests.test_auth.TestAuth.test_expiry"})

    def test_parse_go_and_cargo(self):
        self.assertEqual(parse_failures("--- FAIL: TestServe (0.01s)\n")[1], "go")
        self.assertEqual(parse_failures("test tokens::expiry ... FAILED\n")[1], "cargo")

    def test_extract_detail(self):
        out = (
            "FAILED tests/test_auth.py::test_expiry - TypeError: bad\n"
            "___ test_expiry ___\n"
            "E   TypeError: can't compare offset-naive and offset-aware datetimes\n"
        )
        detail = extract_failure_detail(out, "tests/test_auth.py::test_expiry")
        self.assertTrue(detail.strip())

    def _gate(self, root: Path, baseline: set[str]) -> VerifyGate:
        cfg = load_config(root)
        gate = VerifyGate(cfg, detected={"test": "echo noop", "lint": ""})
        gate.make_plan()
        gate.baseline_failures = set(baseline)
        gate.baseline_done = True
        return gate

    def test_baseline_known_failure_not_counted(self):
        """既有失败不算失败，新增失败才算 —— 这是 Verify-Gate 的核心判据。"""
        root = Path(tempfile.mkdtemp(prefix="vista-vg-"))
        try:
            gate = self._gate(root, {"tests/test_legacy.py::test_login"})
            failures, _ = parse_failures(self.PYTEST_OUT)
            new = sorted(failures - gate.baseline_failures)
            known = sorted(failures & gate.baseline_failures)
            self.assertEqual(new, ["tests/test_auth.py::test_expiry"])
            self.assertEqual(known, ["tests/test_legacy.py::test_login"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_syntax_fallback_marks_unverified(self):
        """无测试项目降级为语法检查时，必须诚实标注 verified=false。"""
        root = Path(tempfile.mkdtemp(prefix="vista-vg2-"))
        try:
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            cfg = load_config(root)
            gate = VerifyGate(cfg, detected={})
            plan = gate.make_plan()
            self.assertEqual(plan.mode, "syntax")
            rep = gate.run(["a.py"])
            self.assertTrue(rep.passed)
            self.assertFalse(rep.verified)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_syntax_catches_broken_file(self):
        root = Path(tempfile.mkdtemp(prefix="vista-vg3-"))
        try:
            (root / "bad.py").write_text("def f(:\n", encoding="utf-8")
            gate = VerifyGate(load_config(root), detected={})
            gate.make_plan()
            self.assertFalse(gate.run(["bad.py"]).passed)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_disabled_gate_is_unverified(self):
        root = Path(tempfile.mkdtemp(prefix="vista-vg4-"))
        try:
            cfg = load_config(root)
            cfg.verify.enabled = False
            gate = VerifyGate(cfg, detected={"test": "pytest -q"})
            self.assertEqual(gate.make_plan().mode, "skipped")
            rep = gate.run([])
            self.assertTrue(rep.passed)
            self.assertFalse(rep.verified)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
