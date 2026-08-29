import unittest

from src.service import AuthService


class TestService(unittest.TestCase):
    def test_register_and_whoami(self):
        svc = AuthService()
        user = svc.whoami(svc.register("dave", "Dave"))
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "Dave")

    def test_deactivate(self):
        svc = AuthService()
        svc.register("erin", "Erin")
        self.assertTrue(svc.deactivate("erin"))
        self.assertEqual(svc.repo.active_users(), [])
