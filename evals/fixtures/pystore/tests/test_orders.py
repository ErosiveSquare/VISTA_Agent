import unittest

from store.inventory import Inventory
from store.models import Item, OrderLine
from store.orders import OrderService
from store.pricing import Pricing


class TestOrders(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        self.inv.put(Item(sku="A1", name="Widget", price_cents=1000, stock=10))
        self.inv.put(Item(sku="B2", name="Gadget", price_cents=250, stock=10))
        self.svc = OrderService(self.inv, Pricing(tax_rate=0.1))

    def test_place_order(self):
        order = self.svc.place([OrderLine("A1", 2), OrderLine("B2", 4)])
        self.assertEqual(order["total"], round(3000 * 1.1))
        self.assertEqual(self.inv.get("A1").stock, 8)

    def test_find(self):
        order = self.svc.place([OrderLine("A1", 1)])
        self.assertEqual(self.svc.find(order["id"]), order)
