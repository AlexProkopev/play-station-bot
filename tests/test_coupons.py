import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.services import coupons as coupons_module
from app.services import users as users_module
from app.services.coupons import CouponService
from app.services.users import UserService


class CouponServiceTests(unittest.TestCase):
    def test_create_and_redeem_coupon_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            temp_coupons_file = os.path.join(temp_dir, "coupons_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                coupons_module,
                "COUPONS_FILE",
                temp_coupons_file,
            ), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(
                coupons_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(101, "buyer")

                created, normalized = CouponService.create_coupon(" sale500 ", 500, created_by=999)
                self.assertTrue(created)
                self.assertEqual(normalized, "SALE500")

                activated, amount, payload = CouponService.redeem_coupon("sale500", 101, "buyer")
                self.assertTrue(activated)
                self.assertEqual(amount, 500.0)
                self.assertEqual(payload, "SALE500")
                self.assertEqual(UserService.get_balance(101), 500.0)

                second_try, _, message = CouponService.redeem_coupon("SALE500", 101, "buyer")
                self.assertFalse(second_try)
                self.assertIn("уже использован", message)

    def test_second_coupon_blocked_same_week_without_purchases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            temp_coupons_file = os.path.join(temp_dir, "coupons_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                coupons_module,
                "COUPONS_FILE",
                temp_coupons_file,
            ), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(
                coupons_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(101, "buyer")
                CouponService.create_coupon("ONE", 100, created_by=1)
                CouponService.create_coupon("TWO", 200, created_by=1)

                first_ok, _, _ = CouponService.redeem_coupon("ONE", 101, "buyer")
                self.assertTrue(first_ok)

                status = CouponService.get_activation_status(101)
                self.assertFalse(status["can_redeem"])
                self.assertEqual(status["purchases"], 0)
                self.assertEqual(status["used"], 1)

                second_ok, _, message = CouponService.redeem_coupon("TWO", 101, "buyer")
                self.assertFalse(second_ok)
                self.assertIn("1 купон в неделю", message)
                self.assertEqual(UserService.get_balance(101), 100.0)

    def test_zero_purchase_second_coupon_after_week(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            temp_coupons_file = os.path.join(temp_dir, "coupons_db.json")
            start = datetime(2026, 8, 1, 12, 0, 0)
            later = start + timedelta(days=7, minutes=1)

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                coupons_module,
                "COUPONS_FILE",
                temp_coupons_file,
            ), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(
                coupons_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(users_module, "get_now_msk", return_value=start), patch.object(
                coupons_module,
                "get_now_msk",
                return_value=start,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(101, "buyer")
                CouponService.create_coupon("ONE", 100, created_by=1)
                CouponService.create_coupon("TWO", 200, created_by=1)
                self.assertTrue(CouponService.redeem_coupon("ONE", 101, "buyer")[0])

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                coupons_module,
                "COUPONS_FILE",
                temp_coupons_file,
            ), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(
                coupons_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(users_module, "get_now_msk", return_value=later), patch.object(
                coupons_module,
                "get_now_msk",
                return_value=later,
            ):
                UserService.reset_runtime_cache()
                status = CouponService.get_activation_status(101)
                self.assertTrue(status["can_redeem"])
                self.assertTrue(CouponService.redeem_coupon("TWO", 101, "buyer")[0])
                self.assertEqual(UserService.get_balance(101), 300.0)

    def test_purchases_make_coupons_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            temp_coupons_file = os.path.join(temp_dir, "coupons_db.json")

            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                coupons_module,
                "COUPONS_FILE",
                temp_coupons_file,
            ), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch.object(
                coupons_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(202, "buyer")
                UserService.increment_purchase(202)
                CouponService.create_coupon("A", 50, created_by=1)
                CouponService.create_coupon("B", 50, created_by=1)
                CouponService.create_coupon("C", 50, created_by=1)

                self.assertTrue(CouponService.redeem_coupon("A", 202, "buyer")[0])
                self.assertTrue(CouponService.redeem_coupon("B", 202, "buyer")[0])
                self.assertTrue(CouponService.redeem_coupon("C", 202, "buyer")[0])
                self.assertEqual(UserService.get_balance(202), 150.0)
                self.assertTrue(CouponService.get_activation_status(202)["unlimited"])

    def test_prompt_explains_weekly_limit(self) -> None:
        text = CouponService.build_prompt_message({"purchases": 0, "can_redeem": True})
        self.assertIn("1 купон в неделю", text)
        unlimited = CouponService.build_prompt_message({"purchases": 2, "unlimited": True})
        self.assertIn("без ограничений", unlimited)

    def test_admin_redeem_message_includes_user_amount_and_purchases(self) -> None:
        text = CouponService.build_admin_redeem_message(101, "buyer", "SALE500", 500, purchases=3)
        self.assertIn("@buyer", text)
        self.assertIn("101", text)
        self.assertIn("500 ₽", text)
        self.assertIn("Покупок: <b>3</b>", text)
        self.assertIn("SALE500", text)

    def test_add_balance_creates_missing_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_users_file = os.path.join(temp_dir, "users_db.json")
            with patch.object(users_module, "USERS_FILE", temp_users_file), patch.object(
                users_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                UserService.reset_runtime_cache()
                self.assertTrue(UserService.add_balance(777, 250))
                self.assertEqual(UserService.get_balance(777), 250.0)
                UserService.add_balance(777, 50)
                self.assertEqual(UserService.get_balance(777), 300.0)

    def test_duplicate_active_coupon_code_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_coupons_file = os.path.join(temp_dir, "coupons_db.json")
            with patch.object(coupons_module, "COUPONS_FILE", temp_coupons_file), patch.object(
                coupons_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                first_ok, _ = CouponService.create_coupon("promo", 300, created_by=1)
                self.assertTrue(first_ok)

                second_ok, second_msg = CouponService.create_coupon("PROMO", 500, created_by=2)
                self.assertFalse(second_ok)
                self.assertIn("уже существует", second_msg)


if __name__ == "__main__":
    unittest.main()