import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from app.services import settings as settings_module
from app.services import users as users_module
from app.services.loyalty import LoyaltyService
from app.services.referrals import complete_product_purchase
from app.services.settings import SettingsService
from app.services.users import UserService


class LoyaltyTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        users_file = os.path.join(temp_dir, "users_db.json")
        settings_file = os.path.join(temp_dir, "settings.json")
        return (
            patch.object(users_module, "USERS_FILE", users_file),
            patch.object(users_module.PostgresDocumentStore, "is_enabled", return_value=False),
            patch.object(settings_module, "SETTINGS_FILE", settings_file),
            patch.object(settings_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_five_plus_one_starts_then_grants_free_bonus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(42, "buyer")

                first = complete_product_purchase(42, 1000)["loyalty"]
                self.assertIn("Акция 5+1", first["message"])
                self.assertIn("бонусом бесплатно", first["message"])
                self.assertNotIn("Серия", first["message"] or "")
                self.assertFalse(UserService.get_personal_discount(42)["active"])

                for expected in range(2, 5):
                    payload = complete_product_purchase(42, 1000)["loyalty"]
                    self.assertEqual(payload["streak"], expected)
                    self.assertIn(f"<b>{expected}</b> из <b>5</b>", payload["message"])

                fifth = complete_product_purchase(42, 1000)["loyalty"]
                self.assertTrue(fifth["granted"])
                self.assertIn("бонусом бесплатно", fifth["message"])
                discount = UserService.get_personal_discount(42)
                self.assertTrue(discount["active"])
                self.assertEqual(discount["percent"], 100)
                self.assertEqual(discount["left"], 1)
                self.assertEqual(UserService.price_with_discount(42, 2000), 0.0)

                sixth = complete_product_purchase(42, 2000)["loyalty"]
                self.assertTrue(sixth["used_coupon"])
                self.assertIsNone(sixth["message"])
                self.assertFalse(UserService.get_personal_discount(42)["active"])
                self.assertEqual(UserService.price_with_discount(42, 2000), 2000.0)

                seventh = complete_product_purchase(42, 1000)["loyalty"]
                self.assertIn("Акция 5+1", seventh["message"])

    def test_weekly_missions_claim_and_admin_reward(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(10, "inviter")
                UserService.add_user(11, "buyer", referrer_id="10")

                SettingsService.update_weekly_missions(reward=1500)
                self.assertEqual(SettingsService.get_weekly_missions()["reward"], 1500)

                view = LoyaltyService.get_view(10)
                self.assertTrue(view["enabled"])
                self.assertFalse(view["can_claim"])
                invite_done = next(task for task in view["tasks"] if task["id"] == "invite")
                self.assertTrue(invite_done["done"])

                complete_product_purchase(10, 500)
                complete_product_purchase(10, 500)
                complete_product_purchase(10, 500)
                view = LoyaltyService.get_view(10)
                self.assertTrue(view["all_done"])
                self.assertTrue(view["can_claim"])
                self.assertEqual(view["reward"], 1500)

                claimed = LoyaltyService.claim_weekly_reward(10)
                self.assertTrue(claimed["ok"])
                self.assertEqual(claimed["amount"], 1500)
                self.assertEqual(UserService.get_balance(10), 1500.0)

                again = LoyaltyService.claim_weekly_reward(10)
                self.assertFalse(again["ok"])
                self.assertEqual(again["error"], "claimed")

    def test_week_reset_allows_new_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(7, "solo")
                UserService.add_user(8, "friend", referrer_id="7")
                week1 = datetime(2026, 9, 14, 12, 0, 0)
                week2 = datetime(2026, 9, 21, 12, 0, 0)
                with patch("app.services.loyalty.get_now_msk", return_value=week1):
                    complete_product_purchase(7, 100)
                    complete_product_purchase(7, 100)
                    complete_product_purchase(7, 100)
                    self.assertTrue(LoyaltyService.claim_weekly_reward(7)["ok"])
                    self.assertTrue(LoyaltyService.get_view(7)["claimed"])
                with patch("app.services.loyalty.get_now_msk", return_value=week2):
                    view = LoyaltyService.get_view(7)
                    self.assertFalse(view["claimed"])
                    self.assertEqual(view["purchases"], 0)
                    self.assertFalse(view["can_claim"])

    def test_admin_can_edit_mission_task_reward(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                task = SettingsService.get_mission_task("buy_once")
                self.assertIsNotNone(task)
                task["reward"] = 250
                saved = SettingsService.upsert_mission_task(task)
                self.assertEqual(saved["reward"], 250)
                self.assertEqual(LoyaltyService.claim_total(), 1250)


if __name__ == "__main__":
    unittest.main()