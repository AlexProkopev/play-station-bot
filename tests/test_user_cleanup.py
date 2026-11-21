import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import users as users_module
from app.services.users import UserService


class UserCleanupTests(unittest.TestCase):
    def test_clear_users_db_keeps_users_with_balance_or_purchases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_users_file = users_module.USERS_FILE
            temp_users_file = os.path.join(temp_dir, "users_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(1, "empty_user")
                UserService.add_user(2, "balance_user")
                UserService.add_user(3, "buyer_user")

                UserService.add_balance(2, 150.0)
                UserService.increment_purchase(3)

                removed = UserService.clear_users_db()
                data = UserService._load_db()

            self.assertEqual(removed, 1)
            self.assertEqual(set(data["users"].keys()), {"2", "3"})
            self.assertEqual(data["users"]["2"]["balance"], 150.0)
            self.assertEqual(data["users"]["3"]["purchases_count"], 1)

            users_module.USERS_FILE = original_users_file


if __name__ == "__main__":
    unittest.main()