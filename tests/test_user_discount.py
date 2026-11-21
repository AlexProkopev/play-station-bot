import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import users as users_module
from app.services.users import UserService


class PersonalDiscountTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        users_file = os.path.join(temp_dir, "users_db.json")
        return (
            patch.object(users_module, "USERS_FILE", users_file),
            patch.object(users_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_price_and_burn_after_purchases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(42, "buyer")
                UserService.set_personal_discount(42, 10, 2)
                self.assertEqual(UserService.price_with_discount(42, 1000), 900.0)
                info = UserService.get_personal_discount(42)
                self.assertTrue(info["active"])
                self.assertEqual(info["left"], 2)

                UserService.increment_purchase(42)
                self.assertEqual(UserService.get_personal_discount(42)["left"], 1)
                self.assertEqual(UserService.price_with_discount(42, 1000), 900.0)

                UserService.increment_purchase(42)
                gone = UserService.get_personal_discount(42)
                self.assertFalse(gone["active"])
                self.assertEqual(gone["percent"], 0)
                self.assertEqual(UserService.price_with_discount(42, 1000), 1000.0)

    def test_clear_discount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(7, "x")
                UserService.set_personal_discount(7, 20, 5)
                UserService.clear_personal_discount(7)
                self.assertFalse(UserService.get_personal_discount(7)["active"])
                self.assertEqual(UserService.price_with_discount(7, 500), 500.0)
