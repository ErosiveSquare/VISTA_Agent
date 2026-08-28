"""L1 RepoMap —— VISTA 名字里的 "I"（Indexed）。

五阶段流水线：
    ① 文件枚举（优先 git ls-files，天然遵守 .gitignore）
    ② 符号抽取（tree-sitter 可选，正则降级）
    ③ 建引用图：文件 u 引用符号 s、文件 v 定义 s → 加边 u → v
    ④ 个性化 PageRank（自实现幂迭代，未引入图库）
    ⑤ 按分数排序符号，二分查找填满 token 预算

边权设计：

    w_uv = Σ_{s ∈ R_u ∩ D_v}  c_u(s) / |D(s)|

其中 c_u(s) 是 u 中引用 s 的次数，|D(s)| 是全仓库中定义 s 的文件数。
除以 |D(s)| 是为了压制 get / run / handle 这类烂大街的名字——
一个只在两个文件里出现的名字，比一个到处都是的名字更有定位价值。

PageRank：
    r ← (1-d)·p + d·M·r,   d = 0.85
p 是个性化向量：焦点文件权重 ×20，其余均匀。悬挂节点（无出边）的质量
按 p 重新分配。收敛判据是 L1 范数小于 1e-6，最多迭代 100 次。

为什么不用 networkx：幂迭代只要二十行，自己写既减少依赖，
也让这个算法在答辩时可以手推。
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..config import RepoMapCfg
from ..llm import tokens as T
from ..util.paths import iter_source_files, read_text, rel_to
from .symbols import SOURCE_EXTS, Definition, FileTags, extract_tags, using_tree_sitter


# ---------------------------------------------------------------------------
# PageRank
# ---------------------------------------------------------------------------
def pagerank(
    n: int,
    out_edges: list[dict[int, float]],
    personalization: list[float],
    damping: float = 0.85,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> list[float]:
    """个性化 PageRank 的幂迭代实现。"""
    if n == 0:
        return []
    total_p = sum(personalization) or 1.0
    p = [x / total_p for x in personalization]
    r = list(p)

    out_sum = [sum(e.values()) for e in out_edges]

    for _ in range(max_iter):
        nxt = [(1.0 - damping) * p[i] for i in range(n)]
        dangling = 0.0
        for i in range(n):
            if out_sum[i] <= 0:
                dangling += r[i]
                continue
            share = damping * r[i] / out_sum[i]
            for j, w in out_edges[i].items():
                nxt[j] += share * w
        if dangling:
            extra = damping * dangling
            for i in range(n):
                nxt[i] += extra * p[i]
        delta = sum(abs(nxt[i] - r[i]) for i in range(n))
        r = nxt
        if delta < tol:
            break
    return r


# ---------------------------------------------------------------------------
@dataclass
class RankedSymbol:
    file: str
    name: str
    kind: str
    line: int
    signature: str
    score: float


@dataclass
class RepoMapStats:
    n_files: int = 0
    n_defs: int = 0
    n_edges: int = 0
    build_ms: int = 0
    tree_sitter: bool = False
    cached: bool = False
    enabled: bool = True
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "n_files": self.n_files, "n_defs": self.n_defs, "n_edges": self.n_edges,
            "build_ms": self.build_ms, "tree_sitter": self.tree_sitter,
            "enabled": self.enabled, "reason": self.reason,
        }


class RepoMap:
    def __init__(self, root: Path, cfg: RepoMapCfg, model: str = ""):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.model = model
        self.stats = RepoMapStats()
        self._tags: dict[str, FileTags] = {}
        self._sig: dict[str, tuple[int, int]] = {}     # rel -> (mtime_ns, size)
        self._built = False
        self._rank_cache: dict[tuple[str, ...], list[RankedSymbol]] = {}

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.cfg.enabled and self.stats.enabled and bool(self._tags)

    def invalidate(self, paths: list[str] | None = None) -> None:
        self._rank_cache.clear()
        if paths is None:
            self._tags.clear()
            self._sig.clear()
            self._built = False
            return
        for p in paths:
            self._tags.pop(p, None)
            self._sig.pop(p, None)

    # ------------------------------------------------------------------
    def build(self, force: bool = False) -> RepoMapStats:
        """（增量）解析仓库中的源文件。按 (mtime, size) 判断缓存是否有效。"""
        t0 = time.time()
        if not self.cfg.enabled:
            self.stats = RepoMapStats(enabled=False, reason="配置中已禁用")
            return self.stats

        files = iter_source_files(self.root, SOURCE_EXTS, max_files=self.cfg.max_files)
        if len(files) < self.cfg.min_files:
            self.stats = RepoMapStats(
                n_files=len(files), enabled=False,
                reason=f"仓库只有 {len(files)} 个源文件（阈值 {self.cfg.min_files}），"
                       f"索引的固定成本超过收益，已自动关闭",
            )
            self._tags.clear()
            return self.stats

        alive: set[str] = set()
        n_cached = 0
        for p in files:
            rel = rel_to(p, self.root)
            alive.add(rel)
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_size)
            if not force and self._sig.get(rel) == sig and rel in self._tags:
                n_cached += 1
                continue
            try:
                source = read_text(p)
            except OSError:
                continue
            self._tags[rel] = extract_tags(rel, source)
            self._sig[rel] = sig

        for gone in set(self._tags) - alive:
            self._tags.pop(gone, None)
            self._sig.pop(gone, None)

        self._built = True
        self._rank_cache.clear()
        self.stats = RepoMapStats(
            n_files=len(self._tags),
            n_defs=sum(len(t.defs) for t in self._tags.values()),
            build_ms=int((time.time() - t0) * 1000),
            tree_sitter=using_tree_sitter(),
            cached=n_cached == len(files),
            enabled=True,
        )
        return self.stats

    # ------------------------------------------------------------------
    def _rank(self, focus: list[str]) -> list[RankedSymbol]:
        key = tuple(sorted(focus))
        if key in self._rank_cache:
            return self._rank_cache[key]

        rels = sorted(self._tags)
        index = {rel: i for i, rel in enumerate(rels)}
        n = len(rels)
        if n == 0:
            return []

        # 符号 → 定义它的文件
        defined_in: dict[str, list[str]] = defaultdict(list)
        for rel, tags in self._tags.items():
            for d in tags.defs:
                defined_in[d.name].append(rel)

        # 建边
        out_edges: list[dict[int, float]] = [dict() for _ in range(n)]
        n_edges = 0
        for rel, tags in self._tags.items():
            u = index[rel]
            for name, count in tags.refs.items():
                targets = defined_in.get(name)
                if not targets:
                    continue
                weight = count / len(targets)
                for t in targets:
                    if t == rel:
                        continue
                    v = index[t]
                    out_edges[u][v] = out_edges[u].get(v, 0.0) + weight
                    n_edges += 1
        self.stats.n_edges = n_edges

        # 个性化向量
        focus_set = {f for f in focus if f in index}
        for f in focus:
            if f in index:
                continue
            focus_set |= {rel for rel in rels if rel.endswith(f) or f.endswith(rel)}
        p = [1.0] * n
        for rel in focus_set:
            if rel in index:
                p[index[rel]] = self.cfg.focus_weight

        ranks = pagerank(n, out_edges, p, damping=self.cfg.damping)

        # 全仓库的引用计数（决定同一文件内哪些符号更重要）
        global_refs: Counter = Counter()
        for tags in self._tags.values():
            global_refs.update(tags.refs)

        import math

        out: list[RankedSymbol] = []
        for rel, tags in self._tags.items():
            base = ranks[index[rel]]
            for d in tags.defs:
                ref_n = global_refs.get(d.name, 0)
                bonus = 1.0 + math.log1p(ref_n)
                kind_w = {"class": 1.4, "type": 1.3, "struct": 1.3, "trait": 1.3,
                          "interface": 1.3, "module": 1.2}.get(d.kind, 1.0)
                out.append(
                    RankedSymbol(rel, d.name, d.kind, d.line, d.signature, base * bonus * kind_w)
                )

        out.sort(key=lambda s: (-s.score, s.file, s.line))

        # 每个文件最多保留 per_file_cap 个符号。否则一个高分文件里的
        # to_dict / render 这类琐碎方法会把整张索引占满，
        # 而索引的价值恰恰在于让模型看见"仓库里有哪些地方"。
        capped: list[RankedSymbol] = []
        per_file: Counter = Counter()
        for s in out:
            if per_file[s.file] >= self.cfg.per_file_cap:
                continue
            per_file[s.file] += 1
            capped.append(s)

        self._rank_cache[key] = capped
        return capped

    # ------------------------------------------------------------------
    def render(self, focus: list[str] | None = None, budget: int = 1024) -> tuple[str, int]:
        """返回 (索引文本, 实际 token 数)。"""
        focus = [f for f in (focus or []) if f]
        if not self._built:
            self.build()
        if not self.available:
            return "", 0
        ranked = self._rank(focus)
        if not ranked:
            return "", 0

        # 二分查找最大的 K，使渲染结果不超预算
        lo, hi, best, best_text = 1, len(ranked), 0, ""
        while lo <= hi:
            mid = (lo + hi) // 2
            text = self._render_n(ranked, mid, focus)
            n = T.count_tokens(text, self.model)
            if n <= budget:
                best, best_text = mid, text
                lo = mid + 1
            else:
                hi = mid - 1
        if best == 0:
            best_text = self._render_n(ranked, 1, focus)
        return best_text, T.count_tokens(best_text, self.model)

    def _render_n(self, ranked: list[RankedSymbol], k: int, focus: list[str]) -> str:
        chosen = ranked[:k]
        by_file: dict[str, list[RankedSymbol]] = defaultdict(list)
        order: list[str] = []
        for s in chosen:
            if s.file not in by_file:
                order.append(s.file)
            by_file[s.file].append(s)

        head = f"仓库共 {self.stats.n_files} 个源文件、{self.stats.n_defs} 个符号；"
        head += f"以下是按结构中心性排序的前 {len(chosen)} 个"
        head += f"（聚焦：{', '.join(focus[:3])}）" if focus else ""
        lines = [head]
        for rel in order:
            lines.append(f"{rel}")
            for s in sorted(by_file[rel], key=lambda x: x.line):
                lines.append(f"  {s.signature}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def top_files(self, k: int = 10, focus: list[str] | None = None) -> list[tuple[str, float]]:
        ranked = self._rank([f for f in (focus or []) if f])
        agg: dict[str, float] = defaultdict(float)
        for s in ranked:
            agg[s.file] += s.score
        return sorted(agg.items(), key=lambda kv: -kv[1])[:k]
