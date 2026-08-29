"""库存。"""

from __future__ import annotations

from .models import Item


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def put(self, item: Item) -> Item:
        self._items[item.sku] = item
        return item

    def get(self, sku: str) -> Item | None:
        return self._items.get(sku)

    def reserve(self, sku: str, qty: int) -> None:
        item = self._items.get(sku)
        if item is None:
            raise KeyError(sku)
        if item.stock < qty:
            raise OutOfStock(f"{sku}: 库存 {item.stock}，需要 {qty}")
        item.stock -= qty

    def restock(self, sku: str, qty: int) -> int:
        item = self._items[sku]
        item.stock += qty
        return item.stock

    def list_items(self) -> list[Item]:
        return list(self._items.values())
