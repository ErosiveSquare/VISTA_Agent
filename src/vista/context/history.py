"""对话历史。

三条不变式在这里落地：

    I1  history 只追加，永不删除。压缩通过插入一条 compaction 事件并给被覆盖的
        事件打上 superseded_by 标记来实现，原始事件永远保留。
        好处是：发给模型的视图、L4 归档、--resume 恢复、事后回放，
        四件事共用同一份数据，不需要两套逻辑。

    I2  每个 tool_call 有且仅有一个对应的 tool_result。
        view() 末尾有一个校验修复过程作为安全网。

    I3  pinned 事件永远出现在视图中，且保持原始相对位置。
        这就是 Constraint Pinning ——压缩不会静默抹掉任务目标与任务清单。
"""

from __future__ import annotations

import json
from typing import Iterable

from ..llm import tokens as T
from ..types import Anchor, Call, Event, EventKind, Role, ToolResult

# 这些事件独立成一条消息，可以安全地作为压缩区间的边界
TURN_STARTS = {"assistant", "compaction", "note", "verify", "todo", "task"}


class History:
    def __init__(self, model: str = "") -> None:
        self.events: list[Event] = []
        self.model = model
        self.n_compactions = 0

    # ------------------------------------------------------------------
    # 追加
    # ------------------------------------------------------------------
    def _append(self, kind: EventKind, role: Role, content: str, order: float | None = None, **kw) -> Event:
        ev = Event(idx=len(self.events), kind=kind, role=role, content=content, **kw)
        ev.order = float(len(self.events)) if order is None else float(order)
        ev.tokens = T.count_tokens(content, self.model) + 8 * len(ev.calls)
        self.events.append(ev)
        return ev

    def append_task(self, text: str) -> Event:
        return self._append("task", "user", text, pinned=True)

    def append_todo(self, rendered: str) -> Event:
        """任务清单是 pinned 的，但同类只保留最新一条。"""
        for e in self.events:
            if e.kind == "todo" and e.superseded_by is None:
                e.superseded_by = len(self.events)
                e.pinned = False  # 旧清单让位给新清单
        return self._append("todo", "user", rendered, pinned=True)

    def append_assistant(self, text: str, calls: list[Call] | None = None) -> Event:
        return self._append("assistant", "assistant", text or "", calls=list(calls or []))

    def append_tool_result(self, call: Call, result: ToolResult) -> Event:
        return self._append(
            "tool_result", "tool", result.render(),
            tool_name=call.name, tool_call_id=call.id,
            reclaimable=bool(result.reclaimable and result.ok),
            anchors=list(result.anchors),
            code=result.code,
            meta={"ok": result.ok, "mutated": list(result.mutated), "cost_ms": result.cost_ms},
        )

    def append_note(self, text: str) -> Event:
        return self._append("note", "user", text)

    def append_verify(self, text: str, passed: bool) -> Event:
        return self._append("verify", "user", text, meta={"passed": passed})

    def append_compaction(self, text: str, span: tuple[int, int], stats: dict,
                          order: float | None = None) -> Event:
        ev = self._append("compaction", "user", text, order=order,
                          meta={"span": list(span), **stats})
        self.n_compactions += 1
        return ev

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def live_events(self) -> list[Event]:
        return [e for e in self.events if e.live]

    def ordered(self) -> list[Event]:
        """按逻辑顺序返回存活事件。

        压缩标记虽然是追加在末尾的（不变式 I1：只追加），但它在逻辑上
        代表被它覆盖的那一段历史，因此排序键取"区间起点 - 0.5"，
        让摘要出现在它所替代的位置，而不是跑到最新事件后面。
        """
        return sorted(self.live_events(), key=lambda e: (e.order, e.idx))

    def total_tokens(self) -> int:
        return sum(e.tokens for e in self.live_events())

    def last_compaction_pos(self) -> int:
        for i in range(len(self.events) - 1, -1, -1):
            if self.events[i].kind == "compaction":
                return i
        return -1

    def steps_since_compaction(self) -> int:
        pos = self.last_compaction_pos()
        return sum(1 for e in self.events[pos + 1 :] if e.kind == "assistant")

    def anchors(self) -> list[Anchor]:
        out: list[Anchor] = []
        for e in self.live_events():
            out.extend(e.anchors)
        return out

    def mutated_files(self) -> list[str]:
        out: list[str] = []
        for e in self.events:
            for m in e.meta.get("mutated", []) or []:
                if m not in out and m != "<workspace>":
                    out.append(m)
        return out

    # ------------------------------------------------------------------
    # 压缩区间（对齐到回合边界，保证 I2 不被破坏）
    # ------------------------------------------------------------------
    def compactable(self, recent_keep: int, min_span: int) -> list[Event] | None:
        """返回本次应当被压缩的事件列表；不值得压缩时返回 None。

        截断点会向前对齐到最近的"回合起点"，保证不会把一个 assistant 的
        tool_calls 和它的 tool_result 切开（不变式 I2）。
        """
        seq = [e for e in self.ordered() if e.kind != "task"]
        if len(seq) <= recent_keep + min_span:
            return None

        cut = len(seq) - recent_keep
        while cut > 0 and seq[cut].kind not in TURN_STARTS:
            cut -= 1
        if cut <= 0:
            return None

        candidates = [e for e in seq[:cut] if not e.pinned]
        if len(candidates) < min_span:
            return None
        return candidates

    def mark_superseded(self, events: Iterable[Event], mark_idx: int) -> int:
        n = 0
        for e in events:
            if e.pinned or e.superseded_by is not None:
                continue
            e.superseded_by = mark_idx
            n += 1
        return n

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def view(self) -> list[dict]:
        msgs = [_to_message(e) for e in self.ordered()]
        return _repair_pairing(msgs)


def _to_message(e: Event) -> dict:
    if e.kind == "assistant":
        m: dict = {"role": "assistant", "content": e.content or ""}
        if e.calls:
            m["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.arguments, ensure_ascii=False),
                    },
                }
                for c in e.calls
            ]
        m["_ev"] = e.idx
        return m
    if e.kind == "tool_result":
        return {
            "role": "tool",
            "tool_call_id": e.tool_call_id or "",
            "name": e.tool_name or "",
            "content": e.content,
            "_ev": e.idx,
        }
    if e.kind == "todo":
        return {"role": "user", "content": "[当前任务清单]\n" + e.content, "_ev": e.idx}
    if e.kind == "compaction":
        return {"role": "user", "content": e.content, "_ev": e.idx}
    return {"role": e.role, "content": e.content, "_ev": e.idx}


def _repair_pairing(msgs: list[dict]) -> list[dict]:
    """安全网：保证 assistant.tool_calls 与后续 tool 消息严格配对（不变式 I2）。

    压缩已经对齐到回合边界，正常情况下这里不会做任何修改；
    但历史被外部工具改写、或未来新增事件类型时，这一层能防止请求被网关拒绝。
    """
    # 第一遍：收集每个 assistant 之后紧跟的 tool 消息 id
    out: list[dict] = []
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            j = i + 1
            found: dict[str, dict] = {}
            while j < n and msgs[j]["role"] == "tool":
                tid = msgs[j].get("tool_call_id")
                if tid in ids and tid not in found:
                    found[tid] = msgs[j]
                j += 1
            if len(found) == len(ids):
                out.append(m)
                for tid in ids:
                    out.append(found[tid])
            else:
                # 有孤儿：把工具调用降级成文本，避免请求非法
                names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
                text = (m.get("content") or "").strip()
                out.append(
                    {
                        "role": "assistant",
                        "content": (text + f"\n[已发起工具调用：{names}（结果已被上下文压缩）]").strip(),
                        "_ev": m.get("_ev"),
                    }
                )
                for tid, tm in found.items():
                    out.append(
                        {
                            "role": "user",
                            "content": f"[工具 {tm.get('name', '')} 的结果]\n{tm['content']}",
                            "_ev": tm.get("_ev"),
                        }
                    )
            i = j
            continue
        if m["role"] == "tool":
            # 前面没有对应的 assistant.tool_calls —— 转成普通消息
            out.append(
                {
                    "role": "user",
                    "content": f"[工具 {m.get('name', '')} 的结果]\n{m['content']}",
                    "_ev": m.get("_ev"),
                }
            )
            i += 1
            continue
        out.append(m)
        i += 1
    return out


def strip_internal(msgs: list[dict]) -> list[dict]:
    """去掉内部调试字段，得到可以直接发给模型的消息列表。"""
    clean: list[dict] = []
    for m in msgs:
        c = {k: v for k, v in m.items() if not k.startswith("_")}
        if c.get("role") == "tool" and not c.get("tool_call_id"):
            c["role"] = "user"
            c.pop("tool_call_id", None)
            c.pop("name", None)
        clean.append(c)
    return clean
