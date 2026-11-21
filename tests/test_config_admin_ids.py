import unittest

from app.config import is_admin, parse_admin_ids


class ConfigAdminIdsTests(unittest.TestCase):
    def test_parse_admin_ids_supports_multiple_separators(self) -> None:
        self.assertEqual(
            parse_admin_ids("111, 222;333\n444"),
            ("111", "222", "333", "444"),
        )

    def test_parse_admin_ids_keeps_order_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_admin_ids("111,222,111,333"),
            ("111", "222", "333"),
        )

    def test_is_admin_checks_against_effective_list(self) -> None:
        self.assertTrue(is_admin(222, ("111", "222")))
        self.assertFalse(is_admin("999", ("111", "222")))


if __name__ == "__main__":
    unittest.main()