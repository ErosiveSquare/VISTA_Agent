"""L4 会话归档。

一份 JSONL 轨迹同时服务四个用途：
    1. 发给模型的视图（由 history 计算得出，同源）
    2. HTML 报告的唯一数据源
    3. SOP 蒸馏的输入
    4. --resume 恢复的依据

每一步立即 flush（不缓冲），保证 Ctrl-C 或崩溃之后轨迹仍然完整。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class Archive:
    def __init__(self, session_dir: Path, session_id: str):
        self.dir = Path(session_dir)
        self.session_id = session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "trajectory.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")
        self._closed = False
        self._written = 0

    # ------------------------------------------------------------------
    def write(self, kind: str, /, **payload: Any) -> None:
        if self._closed:
            return
        row = {"t": kind, "ts": time.time(), **payload}
        try:
            self._fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
            self._written += 1
        except (OSError, ValueError):
            pass

    def meta(self, **payload: Any) -> None:
        self.write("meta", schema_version=SCHEMA_VERSION, session_id=self.session_id, **payload)

    def event(self, ev) -> None:
        d = ev.to_dict()
        # 归档保留完整正文；正文很大时截断以控制文件体积
        if len(d.get("content", "")) > 20000:
            d["content"] = d["content"][:10000] + "\n…[归档截断]…\n" + d["content"][-8000:]
        self.write("event", **d)

    def llm(self, role: str, model: str, usage, cost: float, latency_ms: int, step: int) -> None:
        self.write("llm", role=role, model=model, step=step,
                   in_tokens=usage.in_tokens, out_tokens=usage.out_tokens,
                   cost=round(cost, 6), latency_ms=latency_ms)

    def compaction(self, stats: dict, step: int) -> None:
        self.write("compaction", step=step, **stats)

    def snapshot(self, snap) -> None:
        if snap is not None:
            self.write("snapshot", **snap.to_dict())

    def verify(self, report, attempt: int, step: int) -> None:
        d = report.to_dict()
        d["output"] = (d.get("output") or "")[:4000]
        self.write("verify", attempt=attempt, step=step, **d)

    def context(self, step: int, tokens: int, breakdown: dict) -> None:
        self.write("context", step=step, tokens=tokens, breakdown=breakdown)

    def end(self, **payload: Any) -> None:
        self.write("end", **payload)
        self.close()

    def close(self) -> None:
        if not self._closed:
            try:
                self._fh.close()
            except OSError:
                pass
            self._closed = True

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def read_rows(path: Path) -> Iterator[dict]:
    p = Path(path)
    if not p.is_file():
        return
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_session(session_dir: Path) -> dict:
    """把一次会话的 JSONL 读成结构化数据，供报告与 resume 使用。"""
    d = Path(session_dir)
    rows = list(read_rows(d / "trajectory.jsonl"))
    out: dict = {
        "session_id": d.name, "meta": {}, "events": [], "llm": [], "compactions": [],
        "snapshots": [], "verifies": [], "context": [], "end": {}, "dir": str(d),
    }
    bucket = {
        "event": "events", "llm": "llm", "compaction": "compactions",
        "snapshot": "snapshots", "verify": "verifies", "context": "context",
    }
    for r in rows:
        kind = r.get("t")
        if kind == "meta":
            out["meta"] = r
        elif kind == "end":
            out["end"] = r
        elif kind in bucket:
            out[bucket[kind]].append(r)
    return out


def list_sessions(sessions_dir: Path, limit: int = 50) -> list[dict]:
    base = Path(sessions_dir)
    if not base.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir() or not (d / "trajectory.jsonl").is_file():
            continue
        meta: dict = {}
        end: dict = {}
        for r in read_rows(d / "trajectory.jsonl"):
            if r.get("t") == "meta":
                meta = r
            elif r.get("t") == "end":
                end = r
        out.append(
            {
                "session_id": d.name,
                "dir": str(d),
                "task": (meta.get("task") or "")[:100],
                "model": meta.get("model", ""),
                "status": end.get("status", "unknown"),
                "steps": end.get("steps", 0),
                "cost": end.get("cost", 0.0),
                "ts": meta.get("ts", 0),
            }
        )
        if len(out) >= limit:
            break
    return out


def resume_context(session_dir: Path, recent_keep: int = 6) -> dict:
    """从归档中构造恢复上下文。

    复用的正是压缩机制本身：取最后一条压缩摘要 + 最近若干条事件，
    而不是重放全部历史。因此 --resume 不需要另写一套逻辑。
    """
    data = load_session(session_dir)
    events = data["events"]

    last_compaction_text = ""
    for e in events:
        if e.get("kind") == "compaction" and e.get("superseded_by") is None:
            last_compaction_text = e.get("content", "")

    task = ""
    todo = ""
    for e in events:
        if e.get("kind") == "task" and not task:
            task = e.get("content", "")
        if e.get("kind") == "todo" and e.get("superseded_by") is None:
            todo = e.get("content", "")

    live = [
        e for e in events
        if e.get("superseded_by") is None and e.get("kind") in ("assistant", "tool_result", "verify", "note")
    ]
    recent = live[-recent_keep:]

    steps = sum(1 for e in events if e.get("kind") == "assistant")
    return {
        "task": task,
        "todo": todo,
        "summary": last_compaction_text,
        "recent": recent,
        "steps": steps,
        "meta": data["meta"],
        "cost": data.get("end", {}).get("cost", 0.0),
    }
