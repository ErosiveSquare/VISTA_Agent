"""上下文层测试：不变式 I1 / I2 / I3 与 Anchor Compression。

用标准库 unittest 编写，因此 `python -m unittest` 即可运行，不需要安装 pytest。
"""

from __future__ import annotations

import unittest

from vista.config import load_config
from vista.context.compactor import compact, merge_anchors
from vista.context.history import History
from vista.types import Anchor, Call, ToolResult


def _cfg(tmp="/tmp"):
    cfg = load_config(tmp)
    cfg.context.recent_keep = 2
    cfg.context.min_span = 2
    return cfg


def _busy_history(n_reads: int = 5) -> History:
    h = History()
    h.append_task("修复 auth.py 的时区 bug")
    for i in range(n_reads):
        call = Call.new("read_file", {"path": f"src/f{i}.py"})
        h.append_assistant(f"读取 f{i}", [call])
        h.append_tool_result(
            call,
            ToolResult(
                ok=True, tool="read_file", content="x" * 900, reclaimable=True,
                anchors=[Anchor("file", f"src/f{i}.py", f"sha{i}", (1, 40), f"定义 F{i}")],
            ),
        )
    call = Call.new("bash", {"cmd": "pytest -q"})
    h.append_assistant("跑测试", [call])
    h.append_tool_result(
        call,
        ToolResult(ok=False, tool="bash", code="NONZERO_EXIT",
                   content="E TypeError: can't compare naive and aware datetimes"),
    )
    h.append_todo("[x] 定位\n[~] 修复")
    return h


class TestHistoryInvariants(unittest.TestCase):
    def test_i1_append_only(self):
        """I1：压缩不删除任何事件，只插入标记。"""
        h = _busy_history()
        before = len(h.events)
        stats = compact(_cfg(), h, llm=None, goal="修复时区 bug")
        self.assertIsNotNone(stats)
        self.assertEqual(len(h.events), before + 1)
        # 被覆盖的事件仍然在列表里，只是打了 superseded_by
        superseded = [e for e in h.events if e.superseded_by is not None]
        self.assertGreater(len(superseded), 0)
        for e in superseded:
            self.assertIn(e, h.events)

    def test_i2_tool_call_pairing(self):
        """I2：视图中每个 tool_call 恰有一个 tool_result，且顺序严格配对。"""
        h = _busy_history()
        compact(_cfg(), h, llm=None, goal="g")
        view = h.view()
        call_ids = [tc["id"] for m in view if m["role"] == "assistant"
                    for tc in m.get("tool_calls", [])]
        result_ids = [m["tool_call_id"] for m in view if m["role"] == "tool"]
        self.assertEqual(call_ids, result_ids)

    def test_i2_orphan_repair(self):
        """安全网：人为制造孤儿 tool_result 时，视图仍然是合法的。"""
        h = History()
        call = Call.new("read_file", {"path": "a.py"})
        h.append_assistant("读", [call])
        h.append_tool_result(call, ToolResult(ok=True, tool="read_file", content="x"))
        h.events[0].superseded_by = 99  # 只抹掉 assistant，留下孤儿结果
        view = h.view()
        self.assertFalse(any(m["role"] == "tool" for m in view))
        self.assertTrue(any("工具" in (m.get("content") or "") for m in view))

    def test_i3_pinned_survives(self):
        """I3：pinned 事件（任务与任务清单）压缩后仍在视图中。"""
        h = _busy_history()
        compact(_cfg(), h, llm=None, goal="g")
        text = "\n".join(m.get("content") or "" for m in h.view())
        self.assertIn("修复 auth.py 的时区 bug", text)
        self.assertIn("任务清单", text)

    def test_compaction_marker_position(self):
        """压缩标记应出现在它所替代的位置，而不是被追加到最新事件之后。"""
        h = _busy_history()
        compact(_cfg(), h, llm=None, goal="g")
        view = h.view()
        marker_idx = next(i for i, m in enumerate(view)
                          if (m.get("content") or "").startswith("[上下文压缩"))
        last_assistant = max(i for i, m in enumerate(view) if m["role"] == "assistant")
        self.assertLess(marker_idx, last_assistant)

    def test_todo_supersedes_previous(self):
        h = History()
        h.append_todo("[ ] 第一版")
        h.append_todo("[x] 第二版")
        text = "\n".join(m.get("content") or "" for m in h.view())
        self.assertIn("第二版", text)
        self.assertNotIn("第一版", text)


class TestCompaction(unittest.TestCase):
    def test_reclaimable_body_dropped_anchor_kept(self):
        """可重取内容的正文被丢弃，锚点被保留。"""
        h = _busy_history()
        before = h.total_tokens()
        stats = compact(_cfg(), h, llm=None, goal="修复时区 bug")
        after = h.total_tokens()
        self.assertLess(after, before / 3)
        self.assertEqual(stats.n_reclaimable, 5)
        marker = h.events[-1].content
        self.assertNotIn("xxxxxxxxxx", marker)          # 正文没了
        for i in range(5):
            self.assertIn(f"src/f{i}.py", marker)       # 锚点还在

    def test_derived_error_survives(self):
        """不可重取内容（错误信息）不能从视图中消失。

        它要么还在保护窗口里原样保留，要么已经被写进压缩摘要——
        两者都可以，但绝不能两头都没有。
        """
        h = _busy_history()
        # 再追加几步，把 bash 失败挤出 recent_keep 保护窗口
        for i in range(3):
            call = Call.new("read_file", {"path": f"src/z{i}.py"})
            h.append_assistant(f"看 z{i}", [call])
            h.append_tool_result(call, ToolResult(
                ok=True, tool="read_file", content="z" * 400, reclaimable=True,
                anchors=[Anchor("file", f"src/z{i}.py", f"z{i}", (1, 10), f"Z{i}")]))
        compact(_cfg(), h, llm=None, goal="修复时区 bug")
        view_text = "\n".join(m.get("content") or "" for m in h.view())
        self.assertIn("TypeError", view_text)

    def test_too_short_not_compacted(self):
        h = History()
        h.append_task("t")
        h.append_assistant("hi")
        self.assertIsNone(compact(_cfg(), h, llm=None, goal="t"))

    def test_hierarchical_compaction(self):
        """二次压缩可以把上一次的压缩标记一并吸收。"""
        cfg = _cfg()
        h = _busy_history(3)
        self.assertIsNotNone(compact(cfg, h, llm=None, goal="g"))
        for i in range(3):
            call = Call.new("read_file", {"path": f"src/g{i}.py"})
            h.append_assistant(f"看 g{i}", [call])
            h.append_tool_result(call, ToolResult(
                ok=True, tool="read_file", content="y" * 900, reclaimable=True,
                anchors=[Anchor("file", f"src/g{i}.py", f"s{i}", (1, 30), f"G{i}")]))
        self.assertIsNotNone(compact(cfg, h, llm=None, goal="g"))
        markers = [m for m in h.view() if (m.get("content") or "").startswith("[上下文压缩")]
        self.assertEqual(len(markers), 1)


class TestAnchorMerge(unittest.TestCase):
    def test_adjacent_spans_merged(self):
        out = merge_anchors([
            Anchor("file", "a.py", "s1", (1, 40), "定义 A"),
            Anchor("file", "a.py", "s1", (41, 80), "定义 B"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].span, (1, 80))
        self.assertIn("定义 A", out[0].digest)
        self.assertIn("定义 B", out[0].digest)

    def test_disjoint_spans_kept(self):
        out = merge_anchors([
            Anchor("file", "a.py", "s1", (1, 10), "A"),
            Anchor("file", "a.py", "s1", (500, 520), "B"),
        ])
        self.assertEqual(len(out), 2)

    def test_sha_change_flagged(self):
        """同一文件出现不同 sha，说明期间被改过，必须标记出来。"""
        out = merge_anchors([
            Anchor("file", "a.py", "old", (1, 40), "旧版"),
            Anchor("file", "a.py", "new", (1, 40), "新版"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sha, "new")
        self.assertTrue(out[0].changed)
        self.assertIn("!", out[0].render())

    def test_cap_respected(self):
        anchors = [Anchor("file", f"f{i}.py", "s", (1, 5), f"D{i}") for i in range(50)]
        self.assertEqual(len(merge_anchors(anchors, cap=30)), 30)


if __name__ == "__main__":
    unittest.main()
