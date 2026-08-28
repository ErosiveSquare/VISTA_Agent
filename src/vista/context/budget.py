"""上下文与成本预算。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..types import Usage


@dataclass
class Budget:
    max_cost: float = 1.0
    context_budget: int = 100_000
    theta: float = 0.6
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    context_series: list[dict] = field(default_factory=list)

    # ---------------- 成本 ----------------
    def add(self, usage: Usage, cost: float) -> None:
        self.usage = self.usage + usage
        self.cost += cost

    def exceeded(self) -> bool:
        return self.max_cost > 0 and self.cost >= self.max_cost

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_cost - self.cost)

    # ---------------- 上下文 ----------------
    @property
    def threshold(self) -> int:
        return int(self.context_budget * self.theta)

    def should_compact(self, current_tokens: int) -> bool:
        return current_tokens > self.threshold

    def record_context(self, step: int, tokens: int, event: str = "") -> None:
        self.context_series.append({"step": step, "tokens": tokens, "event": event})

    def to_dict(self) -> dict:
        return {
            "in_tokens": self.usage.in_tokens,
            "out_tokens": self.usage.out_tokens,
            "cost": round(self.cost, 6),
            "max_cost": self.max_cost,
            "context_budget": self.context_budget,
            "threshold": self.threshold,
            "series": self.context_series,
        }
