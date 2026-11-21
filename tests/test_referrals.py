import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import users as users_module
from app.services.referrals import (
    REFERRAL_PERCENT,
    apply_purchase_referral,
    build_referrer_bonus_text,
    calculate_referral_bonus,
    complete_product_purchase,
)
from app.services.users import UserService


class ReferralBonusTests(unittest.TestCase):
    def test_five_percent_rounding(self) -> None:
        self.assertEqual(REFERRAL_PERCENT, 5.0)
        self.assertEqual(calculate_referral_bonus(2000), 100.0)
        self.assertEqual(calculate_referral_bonus(99), 4.95)
        self.assertEqual(calculate_referral_bonus(0), 0.0)
        self.assertEqual(calculate_referral_bonus(None), 0.0)

    def test_credits_referrer_and_builds_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(99, "inviter")
                UserService.add_user(42, "buyer", referrer_id="99")

                payload = complete_product_purchase(42, 4500)
                self.assertIsNotNone(payload)
                self.assertEqual(payload["referrer_id"], 99)
                self.assertEqual(payload["bonus"], 225.0)
                self.assertEqual(UserService.get_balance(99), 225.0)
                self.assertEqual(UserService.get_user(42)["purchases_count"], 1)

                text = build_referrer_bonus_text(payload)
                self.assertIn("4500", text)
                self.assertIn("5%", text)
                self.assertIn("225", text)
                self.assertIn("@buyer", text)
                self.assertIn("Начислено на баланс", text)

    def test_no_bonus_without_referrer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(77, "solo")
                self.assertIsNone(apply_purchase_referral(77, 1000))
                self.assertEqual(UserService.get_balance(77), 0.0)

    def test_self_referral_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(15, "loop", referrer_id="15")
                self.assertIsNone(apply_purchase_referral(15, 500))


if __name__ == "__main__":
    unittest.main()
