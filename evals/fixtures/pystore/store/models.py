"""领域模型。"""

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
