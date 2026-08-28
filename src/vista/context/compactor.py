"""Anchor Compression —— VISTA 名字里的 "A"。

要解决的是三个已被文献识别的失败模式：

  1. 响应式压缩太晚：等到接近 token 上限才压缩，此时上下文已经被陈旧内容
     污染了很多步。对策：阈值设在 0.6 而不是 0.9。
  2. 周期式压缩太钝：每 k 轮固定压缩，不看内容，经常在一个子目标进行到
     一半时把还需要的信息抹掉。对策：只在 TODO 项边界压缩（带逃生阀）。
  3. 压缩会静默抹掉安全约束。对策：Constraint Pinning，pinned 事件不参与压缩。

核心做法是按【内容的可重取性】分三类差异处理：

  PINNED       原样保留，位置不变                              保留 100%
  RECLAIMABLE  正文整段丢弃，只保留证据锚点                     保留约 2%
  DERIVED      交给 weak 模型压缩成固定 schema 的 KeyInfo       保留约 5-10%

关键洞察：编程域有一个通用域没有的杠杆——文件是可重取的。一个 500 行文件的
正文占几千 token，它的锚点只占 20 个 token，而重读的代价只是一次工具调用。
所以对可重取内容做有损摘要是没必要的信息损失，直接丢弃留指针即可。
真正需要 LLM 压缩的只有推理产物，那部分丢了确实找不回来。

另一个关键设计：reclaimable 是由工具注册表【静态】决定的（见 types.py 的说明表），
不由 LLM 判断。因此压缩决策是确定性的、可复现的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..llm import tokens as T
from ..prompts import COMPACT_PROMPT, COMPACT_SYSTEM, PROBE_PROMPT
from ..types import Anchor, Event, KeyInfo
from ..util.text import one_line
from .history import History


@dataclass
class CompactionStats:
    span: tuple[int, int] = (0, 0)
    before_tokens: int = 0
    after_tokens: int = 0
    n_reclaimable: int = 0
    n_derived: int = 0
    n_pinned_kept: int = 0
    n_anchors: int = 0
    llm_used: bool = False
    probe_ok: bool | None = None
    forced: bool = False

    @property
    def saved(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    def to_dict(self) -> dict:
        return {
            "span": list(self.span),
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "saved": self.saved,
            "n_reclaimable": self.n_reclaimable,
            "n_derived": self.n_derived,
            "n_pinned_kept": self.n_pinned_kept,
            "n_anchors": self.n_anchors,
            "llm_used": self.llm_used,
            "probe_ok": self.probe_ok,
            "forced": self.forced,
        }


# ---------------------------------------------------------------------------
# 锚点合并
# ---------------------------------------------------------------------------
def merge_anchors(anchors: list[Anchor], cap: int = 30) -> list[Anchor]:
    """合并同源锚点。

    规则：
      - 同一文件、区间相邻或重叠 → 合并成更大的区间，digest 取并集
      - 同一文件但 sha 不同（期间被改动过）→ 保留最新的 sha，并标记 changed，
        因为"这个文件在你读过之后变了"对模型是重要信息
      - 其它 kind 按 ref 去重，保留最新的 digest
    """
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[Anchor]] = {}
    for a in anchors:
        key = (a.kind, a.ref)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(a)

    merged: list[Anchor] = []
    for key in order:
        items = groups[key]
        kind, ref = key
        if kind != "file":
            last = items[-1]
            merged.append(Anchor(kind=kind, ref=ref, sha=last.sha, span=None,
                                 digest=last.digest, changed=len({i.sha for i in items}) > 1))
            continue

        latest_sha = items[-1].sha
        changed = len({i.sha for i in items if i.sha}) > 1
        same = [i for i in items if i.sha == latest_sha and i.span]
        if not same:
            merged.append(Anchor(kind="file", ref=ref, sha=latest_sha,
                                 digest=items[-1].digest, changed=changed))
            continue

        spans = sorted((i.span for i in same if i.span), key=lambda s: s[0])
        out_spans: list[list[int]] = []
        for lo, hi in spans:
            if out_spans and lo <= out_spans[-1][1] + 1:
                out_spans[-1][1] = max(out_spans[-1][1], hi)
            else:
                out_spans.append([lo, hi])

        digests: list[str] = []
        for i in same:
            if i.digest and i.digest not in digests:
                digests.append(i.digest)
        digest = one_line(" ; ".join(digests), 90)

        for lo, hi in out_spans:
            merged.append(
                Anchor(kind="file", ref=ref, sha=latest_sha, span=(lo, hi),
                       digest=digest, changed=changed)
            )

    if len(merged) > cap:
        merged = merged[-cap:]
    return merged


# ---------------------------------------------------------------------------
# KeyInfo
# ---------------------------------------------------------------------------
def _render_transcript(events: list[Event], model: str, max_tokens: int = 12_000) -> str:
    """把 DERIVED 事件渲染成给 weak 模型的输入，从后往前保留（近期更重要）。"""
    chunks: list[str] = []
    used = 0
    for e in reversed(events):
        label = {
            "assistant": "智能体",
            "tool_result": f"工具 {e.tool_name or ''} 结果" + ("" if e.meta.get("ok", True) else "（失败）"),
            "verify": "验收",
            "note": "系统提示",
            "compaction": "更早的压缩摘要",
        }.get(e.kind, e.kind)
        body = e.content if len(e.content) <= 2400 else e.content[:1200] + "\n…\n" + e.content[-1200:]
        piece = f"[{label}]\n{body}"
        n = T.count_tokens(piece, model)
        if used + n > max_tokens:
            break
        chunks.append(piece)
        used += n
    return "\n\n".join(reversed(chunks))


def _heuristic_keyinfo(goal: str, events: list[Event], anchors: list[Anchor]) -> KeyInfo:
    """weak 模型不可用或调用失败时的确定性降级方案。

    不依赖任何模型：从验收记录、工具错误、变更文件里抽取结构化事实。
    质量不如 LLM 摘要，但保证压缩永远不会因为模型故障而失败。
    """
    facts: list[str] = []
    rejected: list[str] = []
    touched: list[dict] = []
    seen_paths: set[str] = set()

    for e in events:
        if e.kind == "verify":
            passed = e.meta.get("passed")
            facts.append(f"验收{'通过' if passed else '未通过'}：{one_line(e.content, 60)}")
        elif e.kind == "tool_result" and not e.meta.get("ok", True):
            rejected.append(f"{e.tool_name} 失败（{e.code}）：{one_line(e.content, 50)}")
        for m in e.meta.get("mutated", []) or []:
            if m and m != "<workspace>" and m not in seen_paths:
                seen_paths.add(m)
                touched.append({"path": m, "change": "本次会话中被修改", "verified": False})

    last_assistant = next((e for e in reversed(events) if e.kind == "assistant" and e.content.strip()), None)
    return KeyInfo(
        goal=one_line(goal, 100),
        verified_facts=facts[-8:],
        rejected=rejected[-5:],
        open_questions=[],
        touched_files=touched[:12],
        next_step=one_line(last_assistant.content, 60) if last_assistant else "",
    )


def _llm_keyinfo(llm, goal: str, transcript: str) -> KeyInfo | None:
    if llm is None or not transcript.strip():
        return None
    data = llm.call_structured(
        COMPACT_PROMPT.format(goal=one_line(goal, 200), transcript=transcript),
        role="weak",
        system=COMPACT_SYSTEM,
    )
    if not isinstance(data, dict):
        return None
    ki = KeyInfo.from_dict(data)
    return None if ki.is_empty() and not ki.goal else ki


def _probe(llm, summary: str, ki: KeyInfo) -> bool | None:
    """压缩验证探针（可选）：确认关键信息在压缩后仍然可被读出。"""
    if llm is None:
        return None
    data = llm.call_structured(PROBE_PROMPT.format(summary=summary), role="weak")
    if not isinstance(data, dict):
        return None
    got_goal = str(data.get("goal", ""))
    got_next = str(data.get("next_step", ""))

    def overlap(a: str, b: str) -> float:
        from ..util.text import tokenize

        ta, tb = tokenize(a), tokenize(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / min(len(ta), len(tb))

    score = max(overlap(got_goal, ki.goal), overlap(got_next, ki.next_step))
    return score >= 0.3


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_compaction(index: int, span: tuple[int, int], anchors: list[Anchor],
                      keyinfo: KeyInfo, before: int, after_hint: int) -> str:
    lines = [
        f"[上下文压缩 #{index} · 覆盖第 {span[0]}–{span[1] - 1} 条事件 · "
        f"{before / 1000:.1f}k tokens 已压缩]"
    ]
    if anchors:
        lines.append("")
        lines.append("已获取过的证据（正文已释放；需要具体内容时请重新读取）：")
        lines += [a.render() for a in anchors]
    body = keyinfo.render()
    if body:
        lines.append("")
        lines.append(body)
    lines.append("")
    lines.append("（以上是对更早工作的压缩。文件正文可重新读取；已确立的事实与已排除的方向请信任。）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def compact(
    cfg: Config,
    history: History,
    llm=None,
    goal: str = "",
    force: bool = False,
) -> CompactionStats | None:
    """执行一次压缩。返回统计信息；不值得压缩时返回 None。"""
    recent_keep = 2 if force else cfg.context.recent_keep
    min_span = 2 if force else cfg.context.min_span

    rest = history.compactable(recent_keep, min_span)
    if not rest:
        return None
    a, b = rest[0].idx, rest[-1].idx + 1
    pinned: list[Event] = []

    reclaimable = [e for e in rest if e.reclaimable]
    derived = [e for e in rest if not e.reclaimable]

    before = sum(e.tokens for e in rest)

    # ① RECLAIMABLE —— 零 LLM 成本，纯结构化
    raw_anchors: list[Anchor] = []
    for e in rest:
        raw_anchors.extend(e.anchors)
    anchors = merge_anchors(raw_anchors, cfg.context.anchor_cap)
    for anc in anchors:
        object.__setattr__(anc, "tokens_saved", 0)

    # ② DERIVED —— 一次 weak 模型调用，固定 schema；失败则确定性降级
    transcript = _render_transcript(derived, cfg.model.main)
    keyinfo = _llm_keyinfo(llm, goal, transcript)
    llm_used = keyinfo is not None
    if keyinfo is None:
        keyinfo = _heuristic_keyinfo(goal, rest, anchors)
    if not keyinfo.goal:
        keyinfo.goal = one_line(goal, 100)

    text = render_compaction(history.n_compactions + 1, (a, b), anchors, keyinfo, before, 0)

    probe_ok: bool | None = None
    if cfg.context.probe and llm_used:
        probe_ok = _probe(llm, text, keyinfo)
        if probe_ok is False:
            # 探针未通过 → 保守重压：把 transcript 放宽，保留更多细节
            keyinfo2 = _llm_keyinfo(llm, goal, _render_transcript(derived, cfg.model.main, 20_000))
            if keyinfo2 is not None:
                keyinfo = keyinfo2
                text = render_compaction(history.n_compactions + 1, (a, b), anchors, keyinfo, before, 0)

    # ③ 写回：插入标记，不删除原事件（不变式 I1）
    stats = CompactionStats(
        span=(a, b),
        before_tokens=before,
        n_reclaimable=len(reclaimable),
        n_derived=len(derived),
        n_pinned_kept=len(pinned),
        n_anchors=len(anchors),
        llm_used=llm_used,
        probe_ok=probe_ok,
        forced=force,
    )
    mark = history.append_compaction(text, (a, b), stats.to_dict(), order=rest[0].order - 0.5)
    stats.after_tokens = mark.tokens
    mark.meta.update(stats.to_dict())
    history.mark_superseded(rest, mark.idx)
    return stats
