import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import chest as chest_module
from app.services import users as users_module
from app.services.chest import (
    FREE_PRIZE,
    ChestService,
    boost_percent_for_rub,
    spin_amount,
    win_chance,
    SPIN_HIGH_MAX,
    SPIN_LOW_MAX,
    SPIN_LOW_MIN,
)
from app.services.users import UserService


class _FixedDice:
    def __init__(self, roll: float, prize: int = 500):
        self._roll = roll
        self._prize = prize

    def random(self) -> float:
        return self._roll

    def randrange(self, start, stop=None, step=1):
        return self._prize


class ChestServiceTests(unittest.TestCase):
    def test_spin_amount_low_and_high_tiers(self) -> None:
        low = spin_amount(_FixedDice(0.5, prize=55))
        self.assertGreaterEqual(low, SPIN_LOW_MIN)
        self.assertLessEqual(low, SPIN_LOW_MAX)
        high = spin_amount(_FixedDice(0.01, prize=450))
        self.assertGreater(high, SPIN_LOW_MAX)
        self.assertLessEqual(high, SPIN_HIGH_MAX)

    def test_boost_percent_and_chance(self) -> None:
        self.assertEqual(boost_percent_for_rub(100), 10)
        self.assertEqual(boost_percent_for_rub(500), 50)
        self.assertEqual(boost_percent_for_rub(600), 50)
        self.assertEqual(boost_percent_for_rub(50), 0)
        self.assertEqual(win_chance(0), 0)
        self.assertEqual(win_chance(10), 10)
        self.assertEqual(win_chance(50), 50)

    def test_free_win_credits_500_and_blocks_second_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = os.path.join(temp_dir, "users_db.json")
            chest_file = os.path.join(temp_dir, "chest_config.json")
            with patch.object(users_module, "USERS_FILE", users_file), patch.object(
                users_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "CHEST_FILE", chest_file), patch.object(
                chest_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "_today", return_value="2026-09-03"):
                UserService.reset_runtime_cache()
                UserService.add_user(11, "chest_user")
                result = ChestService.open(11, rng=_FixedDice(0.0))
                self.assertTrue(result["ok"])
                self.assertTrue(result["win"])
                self.assertFalse(result["boosted"])
                self.assertEqual(result["prize"], FREE_PRIZE)
                self.assertEqual(UserService.get_balance(11), 500.0)
                again = ChestService.open(11, rng=_FixedDice(0.0))
                self.assertFalse(again.get("ok"))
                self.assertEqual(again.get("error"), "already")
                self.assertEqual(UserService.get_balance(11), 500.0)

    def test_free_miss_does_not_credit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = os.path.join(temp_dir, "users_db.json")
            chest_file = os.path.join(temp_dir, "chest_config.json")
            with patch.object(users_module, "USERS_FILE", users_file), patch.object(
                users_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "CHEST_FILE", chest_file), patch.object(
                chest_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "_today", return_value="2026-09-03"):
                UserService.reset_runtime_cache()
                UserService.add_user(12, "miss")
                result = ChestService.open(12, rng=_FixedDice(0.99, prize=65))
                self.assertTrue(result["ok"])
                self.assertTrue(result["win"])
                self.assertGreaterEqual(result["prize"], SPIN_LOW_MIN)
                self.assertLessEqual(result["prize"], SPIN_LOW_MAX)
                self.assertEqual(UserService.get_balance(12), float(result["prize"]))

    def test_boost_does_not_change_daily_free_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = os.path.join(temp_dir, "users_db.json")
            chest_file = os.path.join(temp_dir, "chest_config.json")
            with patch.object(users_module, "USERS_FILE", users_file), patch.object(
                users_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "CHEST_FILE", chest_file), patch.object(
                chest_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "_today", return_value="2026-09-03"):
                UserService.reset_runtime_cache()
                UserService.add_user(13, "boost")
                ChestService.grant_boost(13, 300)
                miss = ChestService.open(13, rng=_FixedDice(0.99, prize=40))
                self.assertTrue(miss["ok"])
                self.assertTrue(miss["win"])
                self.assertFalse(miss["boosted"])
                self.assertGreaterEqual(miss["prize"], SPIN_LOW_MIN)
                self.assertLessEqual(miss["prize"], SPIN_LOW_MAX)
                self.assertEqual(UserService.get_balance(13), float(miss["prize"]))
                again = ChestService.open(13, rng=_FixedDice(0.0))
                self.assertFalse(again.get("ok"))
                self.assertEqual(again.get("error"), "already")

    def test_day_rollover_resets_free_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = os.path.join(temp_dir, "users_db.json")
            chest_file = os.path.join(temp_dir, "chest_config.json")
            with patch.object(users_module, "USERS_FILE", users_file), patch.object(
                users_module.PostgresDocumentStore, "is_enabled", return_value=False
            ), patch.object(chest_module, "CHEST_FILE", chest_file), patch.object(
                chest_module.PostgresDocumentStore, "is_enabled", return_value=False
            ):
                UserService.reset_runtime_cache()
                UserService.add_user(14, "next_day")
                with patch.object(chest_module, "_today", return_value="2026-09-03"):
                    ChestService.open(14, rng=_FixedDice(0.99))
                    self.assertEqual(ChestService.can_open(14)[1], "already")
                with patch.object(chest_module, "_today", return_value="2026-09-04"):
                    allowed, reason = ChestService.can_open(14)
                    self.assertTrue(allowed)
                    self.assertEqual(reason, "free")
