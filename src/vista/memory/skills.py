"""L3 SOP / 技能卡 —— VISTA 名字里的 "S"（Self-evolving）。

蒸馏触发条件（"条件支路"，必须全部满足）：
    1. Verify-Gate 通过 —— 只有经过真实验证的轨迹才有资格成为经验
    2. 步数 ≥ min_steps —— 太简单的任务不值得蒸馏
    3. 本次至少产生了一次文件修改 —— 纯问答不蒸馏
    4. 未命中已有技能卡 —— 命中则只更新统计，不新建

检索用关键词 + IDF 加权，刻意不用 embedding：
    - 关键词匹配的误触发是可解释、可调试的；embedding 的失配无法向用户解释
    - 技能卡数量在几十量级，精确率比召回率重要
    - 不引入额外的模型调用与向量存储

记忆污染防护三层：
    1. 来源可信：只蒸馏验证通过的轨迹
    2. 失败降权：连续失败 max_fail_streak 次自动停用
    3. 人可控：纯 YAML，用户随时能打开看见并删掉 —— 这是最实在的一层
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import SkillsCfg
from ..llm import tokens as T
from ..prompts import DISTILL_PROMPT, DISTILL_SYSTEM
from ..types import SkillCard
from ..util import miniyaml
from ..util.paths import read_text, write_text
from ..util.text import slug, tokenize


@dataclass
class Retrieval:
    card: SkillCard
    score: float


class SkillIndex:
    def __init__(self, directory: Path, cfg: SkillsCfg, model: str = ""):
        self.dir = Path(directory)
        self.cfg = cfg
        self.model = model
        self.cards: list[SkillCard] = []
        self.last_retrieved: list[SkillCard] = []
        self._idf: dict[str, float] = {}

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, directory: Path, cfg: SkillsCfg, model: str = "") -> "SkillIndex":
        idx = cls(directory, cfg, model)
        idx.reload()
        return idx

    def reload(self) -> None:
        self.cards = []
        if not self.dir.is_dir():
            self._build_idf()
            return
        for p in sorted(self.dir.glob("*.yaml")) + sorted(self.dir.glob("*.yml")):
            try:
                data = miniyaml.loads(read_text(p))
            except Exception:
                continue
            if not isinstance(data, dict) or not data.get("name"):
                continue
            self.cards.append(SkillCard.from_yaml_dict(data, str(p)))
        self._build_idf()

    def _build_idf(self) -> None:
        n = len(self.cards)
        df: dict[str, int] = {}
        for c in self.cards:
            terms = set()
            for t in c.triggers:
                terms |= tokenize(t)
            terms |= tokenize(c.title)
            for t in terms:
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log((n + 1) / (k + 1)) + 1.0 for t, k in df.items()}

    # ------------------------------------------------------------------
    def score(self, card: SkillCard, query_terms: set[str], scope: dict | None) -> float:
        terms: set[str] = set()
        for t in card.triggers:
            terms |= tokenize(t)
        terms |= tokenize(card.title)
        if not terms:
            return 0.0
        hit = terms & query_terms
        if not hit:
            return 0.0
        raw = sum(self._idf.get(t, 1.0) for t in hit) / math.sqrt(len(terms))

        # 语言 / 框架匹配
        lam_scope = 1.0
        if scope:
            lang = (scope.get("language") or "").lower()
            fws = {f.lower() for f in (scope.get("framework") or [])}
            if card.languages:
                lam_scope *= 1.0 if (not lang or lang in {x.lower() for x in card.languages}) else 0.3
            if card.frameworks and fws:
                lam_scope *= 1.0 if fws & {x.lower() for x in card.frameworks} else 0.5

        # 历史成功率加权
        lam_stats = (1.0 + card.success_count) / (1.0 + card.usage_count)
        return raw * lam_scope * lam_stats

    def retrieve(self, task: str, k: int | None = None, scope: dict | None = None,
                 force: bool = False) -> list[SkillCard]:
        if not self.cfg.enabled and not force:
            return []
        if not self.cards:
            return []
        k = k or self.cfg.top_k
        qterms = tokenize(task)
        if not qterms:
            return []
        scored = [
            Retrieval(c, self.score(c, qterms, scope))
            for c in self.cards
            if c.enabled or force
        ]
        scored = [r for r in scored if r.score >= (0.0 if force else self.cfg.min_score)]
        scored.sort(key=lambda r: -r.score)
        out = [r.card for r in scored[:k]]
        if not force:
            self.last_retrieved = out
        return out

    def render(self, cards: list[SkillCard] | None = None, budget: int = 500) -> str:
        cards = cards if cards is not None else self.last_retrieved
        if not cards:
            return ""
        blocks: list[str] = []
        used = 0
        for c in cards:
            block = c.render()
            n = T.count_tokens(block, self.model)
            if used + n > budget and blocks:
                break
            blocks.append(block)
            used += n
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    def record_outcome(self, cards: list[SkillCard], success: bool) -> None:
        """技能卡被注入后，记录本次任务的结果，用于失败降权与自动停用。"""
        for c in cards:
            c.usage_count += 1
            if success:
                c.success_count += 1
                c.fail_streak = 0
            else:
                c.fail_streak += 1
                if c.fail_streak >= self.cfg.max_fail_streak:
                    c.enabled = False
            self.save_card(c)

    def save_card(self, card: SkillCard) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = Path(card.path) if card.path else self.dir / f"{card.name}.yaml"
        write_text(path, miniyaml.dump_text(card.to_yaml_dict()))
        card.path = str(path)
        return path

    def remove(self, name: str) -> bool:
        for c in list(self.cards):
            if c.name == name:
                try:
                    if c.path:
                        Path(c.path).unlink(missing_ok=True)
                except OSError:
                    return False
                self.cards.remove(c)
                self._build_idf()
                return True
        return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        for c in self.cards:
            if c.name == name:
                c.enabled = enabled
                if enabled:
                    c.fail_streak = 0
                self.save_card(c)
                return True
        return False

    # ------------------------------------------------------------------
    # 蒸馏
    # ------------------------------------------------------------------
    def should_distill(self, *, success: bool, steps: int, mutated: int, hit_existing: bool) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, "技能库已关闭"
        if not success:
            return False, "任务未通过验收"
        if steps < self.cfg.min_steps:
            return False, f"步数 {steps} 少于阈值 {self.cfg.min_steps}"
        if mutated <= 0:
            return False, "本次没有文件改动"
        if hit_existing:
            return False, "命中了已有技能卡，只更新统计"
        return True, ""

    def distill(self, llm, summary: str, session_id: str, steps: int,
                scope: dict | None = None) -> SkillCard | None:
        """用 weak 模型把一次成功轨迹蒸馏成技能卡。返回 None 表示放弃蒸馏。"""
        if llm is None:
            return None
        text = llm.call_text(DISTILL_PROMPT.format(summary=summary),
                             role="weak", system=DISTILL_SYSTEM)
        if not text:
            return None
        body = text.strip()
        if body.upper().startswith("SKIP") or "\nSKIP" in body.upper()[:200]:
            return None
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body).strip()
        try:
            data = miniyaml.loads(body)
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("name"):
            return None

        card = SkillCard.from_yaml_dict(data)
        if not card.steps or not card.triggers:
            return None
        card.name = slug(card.name)
        if any(c.name == card.name for c in self.cards):
            card.name = f"{card.name}-{int(time.time()) % 10000}"
        card.session_id = session_id
        card.distilled_at = time.time()
        card.source_steps = steps
        if scope:
            card.languages = card.languages or ([scope["language"]] if scope.get("language") else [])
            card.frameworks = card.frameworks or list(scope.get("framework") or [])

        self.save_card(card)
        self.cards.append(card)
        self._build_idf()
        return card

    # ------------------------------------------------------------------
    def summary(self) -> list[dict]:
        return [
            {
                "name": c.name, "title": c.title, "enabled": c.enabled,
                "usage": c.usage_count, "success": c.success_count,
                "fail_streak": c.fail_streak, "triggers": c.triggers[:6],
                "path": c.path,
            }
            for c in sorted(self.cards, key=lambda x: (-x.success_count, x.name))
        ]
