import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import chest as chest_module
from app.services import coupons as coupons_module
from app.services import users as users_module
from app.services.chest import FREE_PRIZE, ChestService
from app.services.coupons import CouponService
from app.services.referrals import complete_product_purchase
from app.services.users import BONUS_BALANCE_CAP, BONUS_CAP_NOTICE, UserService


class _AlwaysWin:
    def random(self) -> float:
        return 0.0


class BonusBalanceCapTests(unittest.TestCase):
    def _user_patches(self, temp_dir: str):
        return (
            patch.object(users_module, "USERS_FILE", os.path.join(temp_dir, "users_db.json")),
            patch.object(users_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_bonuses_stop_at_1500_without_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._user_patches(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(11, "farmer")
                self.assertTrue(UserService.add_balance(11, 900))
                self.assertTrue(UserService.add_balance(11, 700))
                self.assertEqual(UserService.get_balance(11), BONUS_BALANCE_CAP)
                self.assertFalse(UserService.add_balance(11, 200))
                self.assertEqual(UserService.get_balance(11), BONUS_BALANCE_CAP)
                notice = UserService.consume_bonus_cap_notice(11)
                self.assertEqual(notice, BONUS_CAP_NOTICE)
                self.assertIn("реферальн", notice.lower())
                self.assertIsNone(UserService.consume_bonus_cap_notice(11))

    def test_topup_and_referral_bonus_are_not_capped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._user_patches(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(21, "wallet")
                self.assertTrue(UserService.add_balance(21, 2000, source="topup"))
                self.assertEqual(UserService.get_balance(21), 2000.0)
                self.assertFalse(UserService.add_balance(21, 100))
                self.assertEqual(UserService.get_balance(21), 2000.0)
                self.assertTrue(UserService.add_balance(21, 250, source="admin"))
                self.assertEqual(UserService.get_balance(21), 2250.0)

    def test_own_purchase_lifts_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._user_patches(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(31, "buyer")
                UserService.add_balance(31, 1500)
                self.assertFalse(UserService.add_balance(31, 100))
                complete_product_purchase(31, 2000)
                self.assertTrue(UserService.add_balance(31, 400))
                self.assertEqual(UserService.get_balance(31), 1900.0)

    def test_referral_who_bought_lifts_cap_and_credits_over_1500(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._user_patches(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(41, "inviter")
                UserService.add_user(42, "friend", referrer_id="41")
                UserService.add_balance(41, 1500)
                self.assertFalse(UserService.add_balance(41, 50))
                payload = complete_product_purchase(42, 4000)
                self.assertEqual(payload["bonus"], 200.0)
                self.assertEqual(UserService.get_balance(41), 1700.0)
                self.assertEqual(UserService.get_user(41)["paid_referral_count"], 1)
                self.assertTrue(UserService.add_balance(41, 100))
                self.assertEqual(UserService.get_balance(41), 1800.0)

    def test_historical_paid_referral_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._user_patches(temp_dir)
            with patches[0], patches[1]:
                UserService.reset_runtime_cache()
                UserService.add_user(51, "old_inviter")
                UserService.add_user(52, "old_friend", referrer_id="51")
                UserService.increment_purchase(52)
                UserService.add_balance(51, 1500)
                self.assertTrue(UserService.add_balance(51, 80))
                self.assertEqual(UserService.get_balance(51), 1580.0)

    def test_coupon_is_kept_when_cap_blocks_credit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = os.path.join(temp_dir, "users_db.json")
            coupons_file = os.path.join(temp_dir, "coupons_db.json")
            with patch.object(users_module, "USERS_FILE", users_file), patch.object(
                users_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(coupons_module, "COUPONS_FILE", coupons_file), patch.object(
                coupons_module.PostgresDocumentStore, "is_enabled", return_value=False
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(61, "coupon")
                UserService.add_balance(61, 1500)
                created, code = CouponService.create_coupon("CAP1500", 400, created_by=1)
                self.assertTrue(created)
                ok, amount, payload = CouponService.redeem_coupon(code, 61, "coupon")
                self.assertFalse(ok)
                self.assertEqual(amount, 0.0)
                self.assertIn("1500", payload)
                self.assertEqual(UserService.get_balance(61), 1500.0)
                coupons = CouponService._load_db().get("coupons") or {}
                self.assertTrue((coupons.get(code) or {}).get("is_active", True))
                UserService.increment_purchase(61)
                again, again_amount, _ = CouponService.redeem_coupon(code, 61, "coupon")
                self.assertTrue(again)
                self.assertEqual(again_amount, 400.0)
                self.assertEqual(UserService.get_balance(61), 1900.0)

    def test_chest_does_not_spend_daily_slot_at_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = os.path.join(temp_dir, "users_db.json")
            chest_file = os.path.join(temp_dir, "chest_config.json")
            with patch.object(users_module, "USERS_FILE", users_file), patch.object(
                users_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "CHEST_FILE", chest_file), patch.object(
                chest_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "_today", return_value="2026-09-23"):
                UserService.reset_runtime_cache()
                UserService.add_user(71, "chest")
                UserService.add_balance(71, 1500)
                result = ChestService.open(71, rng=_AlwaysWin())
                self.assertFalse(result.get("ok"))
                self.assertEqual(result.get("error"), "bonus_cap")
                self.assertFalse(ChestService.snapshot(71).get("free_used"))
                UserService.increment_purchase(71)
                won = ChestService.open(71, rng=_AlwaysWin())
                self.assertTrue(won.get("ok"))
                self.assertTrue(won.get("win"))
                self.assertGreaterEqual(float(won.get("prize") or 0), 10)
                self.assertLessEqual(float(won.get("prize") or 0), 500)
                self.assertEqual(UserService.get_balance(71), 1500.0 + float(won.get("prize") or 0))


if __name__ == "__main__":
    unittest.main()
