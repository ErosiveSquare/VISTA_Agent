"""计价。"""

from __future__ import annotations

from .models import OrderLine


class Pricing:
    def __init__(self, tax_rate: float = 0.0):
        self.tax_rate = tax_rate

    def subtotal(self, lines: list[OrderLine], catalog) -> int:
        total = 0
        for line in lines:
            item = catalog.get(line.sku)
            if item is None:
                raise KeyError(line.sku)
            total += item.price_cents * line.qty
        return total

    def total(self, lines: list[OrderLine], catalog) -> int:
        sub = self.subtotal(lines, catalog)
        return sub + round(sub * self.tax_rate)
