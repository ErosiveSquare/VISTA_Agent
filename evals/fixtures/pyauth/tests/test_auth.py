import unittest

from src.auth import TokenManager


class TestAuth(unittest.TestCase):
    def test_issue_and_verify(self):
        tm = TokenManager()
        self.assertEqual(tm.verify(tm.issue("alice", ttl=60)), "alice")

    def test_tampered_rejected(self):
        tm = TokenManager()
        token = tm.issue("bob", ttl=60)
        self.assertIsNone(tm.verify(token[:-2] + "xx"))

    def test_expiry(self):
        tm = TokenManager()
        self.assertIsNone(tm.verify(tm.issue("carol", ttl=-1)))
