import unittest

from store.inventory import Inventory, OutOfStock
from store.models import Item


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        self.inv.put(Item(sku="A1", name="Widget", price_cents=1000, stock=5))

    def test_reserve(self):
        self.inv.reserve("A1", 3)
        self.assertEqual(self.inv.get("A1").stock, 2)

    def test_out_of_stock(self):
        with self.assertRaises(OutOfStock):
            self.inv.reserve("A1", 99)

    def test_restock(self):
        self.assertEqual(self.inv.restock("A1", 5), 10)
