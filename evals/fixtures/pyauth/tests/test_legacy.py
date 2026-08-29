import unittest


class TestLegacy(unittest.TestCase):
    def test_legacy_login(self):
        # 项目里原本就失败的用例。Verify-Gate 的基线对比应当把它识别为
        # "既有失败"，不计入本次任务的责任范围。
        self.fail("legacy login flow not implemented yet")
