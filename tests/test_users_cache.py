import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import users as users_module
from app.services.users import UserService


class UserCacheTests(unittest.TestCase):
    def test_reads_stay_in_memory_after_file_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(11, "cached_user")
                UserService.set_active_order(11, {"timestamp": "2026-08-19 12:00:00", "type": "card_rf"})
                os.remove(temp_users_file)

                user = UserService.get_user(11)
                orders = UserService.get_all_active_orders()

                self.assertEqual(user["username"], "cached_user")
                self.assertEqual(len(orders), 1)
                self.assertEqual(orders[0]["user_id"], "11")

    def test_active_orders_do_not_scan_users_without_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                for uid in range(1, 21):
                    UserService.add_user(uid, f"user{uid}")
                UserService.set_active_order(7, {"timestamp": "2026-08-19 12:00:00"})

                orders = UserService.get_all_active_orders()
                self.assertEqual([item["user_id"] for item in orders], ["7"])
                self.assertEqual(len(UserService.get_all_users()), 20)

    def test_purchases_and_profile_fields_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(99, "referrer")
                UserService.add_user(42, "buyer", referrer_id="99")
                UserService.increment_purchase(42)
                UserService.increment_purchase(42)
                UserService.add_balance(42, 100)

                user = UserService.get_user(42)
                referrer = UserService.get_user(99)

                self.assertEqual(user["purchases_count"], 2)
                self.assertEqual(user["balance"], 100.0)
                self.assertEqual(referrer["referral_count"], 1)


if __name__ == "__main__":
    unittest.main()
