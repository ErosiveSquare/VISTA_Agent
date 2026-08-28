"""快照与回滚（不变式 I5：任何写操作之前必有快照）。

刻意没有使用"影子 git 仓库"（Cline 的做法）：用户的工作区可能有未提交的改动，
VISTA 不应该去碰用户的版本控制状态。这里做的是文件级的轻量快照——
只复制"即将被修改的文件"，代价小且完全不干扰用户的 git。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from ..util.paths import rel_to


@dataclass
class Snapshot:
    id: str
    step: int
    files: list[dict] = field(default_factory=list)  # {"path": rel, "existed": bool}
    ts: float = field(default_factory=time.time)
    label: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "step": self.step, "files": self.files, "ts": self.ts, "label": self.label}


class SnapshotStore:
    def __init__(self, root: Path, session_dir: Path):
        self.root = Path(root).resolve()
        self.dir = Path(session_dir) / "snapshots"
        self.snapshots: list[Snapshot] = []
        self._seq = 0

    # ------------------------------------------------------------------
    def take(self, paths: list[str], step: int, label: str = "") -> Snapshot | None:
        """为一组即将被修改的文件建立快照。paths 是工作区相对路径。"""
        uniq: list[str] = []
        for p in paths:
            rel = rel_to(self.root / p, self.root) if not str(p).startswith("/") else rel_to(Path(p), self.root)
            if rel and rel not in uniq:
                uniq.append(rel)
        if not uniq:
            return None

        self._seq += 1
        snap = Snapshot(id=f"snap-{self._seq:03d}", step=step, label=label)
        target = self.dir / snap.id
        target.mkdir(parents=True, exist_ok=True)

        for rel in uniq:
            src = self.root / rel
            existed = src.is_file()
            snap.files.append({"path": rel, "existed": existed})
            if existed:
                dst = target / quote(rel, safe="")
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    snap.files[-1]["existed"] = False

        (target / "manifest.json").write_text(
            json.dumps(snap.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.snapshots.append(snap)
        return snap

    # ------------------------------------------------------------------
    def restore(self, snapshot_id: str | None = None) -> tuple[bool, str]:
        """回滚到某个快照。默认回滚最近一次。"""
        if not self.snapshots:
            return False, "没有可回滚的快照。"
        snap = self.snapshots[-1]
        if snapshot_id:
            found = [s for s in self.snapshots if s.id == snapshot_id]
            if not found:
                return False, f"找不到快照 {snapshot_id}。"
            snap = found[0]

        target = self.dir / snap.id
        restored, removed, failed = [], [], []
        for item in snap.files:
            rel, existed = item["path"], item["existed"]
            dst = self.root / rel
            if existed:
                src = target / quote(rel, safe="")
                if not src.is_file():
                    failed.append(rel)
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    restored.append(rel)
                except OSError:
                    failed.append(rel)
            else:
                # 快照时文件不存在 —— 说明是新建的，回滚就是删除
                try:
                    if dst.is_file():
                        dst.unlink()
                        removed.append(rel)
                except OSError:
                    failed.append(rel)

        idx = self.snapshots.index(snap)
        self.snapshots = self.snapshots[:idx]

        parts = [f"已回滚到 {snap.id}（第 {snap.step} 步）"]
        if restored:
            parts.append(f"恢复 {len(restored)} 个文件：{', '.join(restored[:5])}")
        if removed:
            parts.append(f"删除 {len(removed)} 个新建文件：{', '.join(removed[:5])}")
        if failed:
            parts.append(f"失败 {len(failed)} 个：{', '.join(failed[:5])}")
        return not failed, "；".join(parts)

    # ------------------------------------------------------------------
    def list(self) -> list[Snapshot]:
        return list(self.snapshots)

    @property
    def count(self) -> int:
        return len(self.snapshots)
