"""下单。"""

from __future__ import annotations

from .inventory import Inventory
from .models import OrderLine
from .pricing import Pricing


class OrderService:
    def __init__(self, inventory: Inventory, pricing: Pricing | None = None):
        self.inventory = inventory
        self.pricing = pricing or Pricing()
        self.orders: list[dict] = []

    def place(self, lines: list[OrderLine]) -> dict:
        for line in lines:
            self.inventory.reserve(line.sku, line.qty)
        order = {
            "id": len(self.orders) + 1,
            "lines": lines,
            "total": self.pricing.total(lines, self.inventory),
        }
        self.orders.append(order)
        return order

    def find(self, order_id: int) -> dict | None:
        return next((o for o in self.orders if o["id"] == order_id), None)
