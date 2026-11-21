import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.handlers.user_flow import _race_profile_block
from app.services import referral_race as race_module
from app.services import users as users_module
from app.services.referral_race import PRIZES, ReferralRaceService
from app.services.referrals import complete_product_purchase
from app.services.users import UserService


class ReferralRaceTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        race_file = os.path.join(temp_dir, "referral_race.json")
        users_file = os.path.join(temp_dir, "users_db.json")
        return (
            patch.object(race_module, "RACE_FILE", race_file),
            patch.object(race_module.PostgresDocumentStore, "is_enabled", return_value=False),
            patch.object(users_module, "USERS_FILE", users_file),
            patch.object(users_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_start_keeps_fake_mass_and_grows_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                started = datetime(2026, 9, 3, 12, 0, 0)
                race = ReferralRaceService.start(now=started, rng=random.Random(7))
                fake_ids = [item["id"] for item in race["fakes"]]
                self.assertGreaterEqual(len(fake_ids), 16)
                self.assertLessEqual(len(fake_ids), 24)
                self.assertEqual(len(set(fake_ids)), len(fake_ids))

                start_board = ReferralRaceService.board(now=started)
                noon_next = ReferralRaceService.board(now=started + timedelta(hours=30))
                finish = ReferralRaceService.board(now=started + timedelta(hours=47))
                self.assertEqual(len(start_board["rows"]), len(noon_next["rows"]))
                self.assertEqual(len(start_board["rows"]), len(finish["rows"]))
                self.assertGreaterEqual(len(start_board["rows"]), 16)

                by_id_start = {row["id"]: row for row in start_board["rows"]}
                by_id_mid = {row["id"]: row for row in noon_next["rows"]}
                by_id_end = {row["id"]: row for row in finish["rows"]}
                grew = False
                for fake_id in fake_ids:
                    a = by_id_start[fake_id]
                    b = by_id_mid[fake_id]
                    c = by_id_end[fake_id]
                    self.assertGreaterEqual(b["gray"] + b["combat"], a["gray"] + a["combat"])
                    self.assertGreaterEqual(c["gray"] + c["combat"], b["gray"] + b["combat"])
                    if c["gray"] + c["combat"] > a["gray"] + a["combat"]:
                        grew = True
                self.assertTrue(grew)
                self.assertEqual([item["id"] for item in ReferralRaceService.get_race()["fakes"]], fake_ids)

    def test_gray_and_first_purchase_combat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                now = datetime(2026, 9, 3, 10, 0, 0)
                with patch.object(race_module, "get_now_msk", return_value=now + timedelta(hours=1)):
                    ReferralRaceService.start(now=now, rng=random.Random(3))
                    UserService.add_user(100, "inviter")
                    UserService.add_user(201, "gray", referrer_id="100")
                    UserService.add_user(202, "buyer", referrer_id="100")

                    ReferralRaceService.note_gray(100, 201)
                    ReferralRaceService.note_gray(100, 201)
                    ReferralRaceService.note_gray(100, 202)
                    complete_product_purchase(202, 2000)
                    complete_product_purchase(202, 2000)

                board = ReferralRaceService.board(100, now=now + timedelta(hours=1))
                self.assertEqual(board["self"]["gray"], 2)
                self.assertEqual(board["self"]["combat"], 1)
                real = ReferralRaceService.real_leaderboard(now + timedelta(hours=1))
                self.assertEqual(len(real), 1)
                self.assertEqual(real[0]["id"], "100")
                for row in board["rows"]:
                    self.assertFalse(str(row["id"]).startswith("@"))

    def test_award_first_place_is_5000(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                ReferralRaceService.start(now=datetime(2026, 9, 3, 10, 0, 0), rng=random.Random(1))
                result = ReferralRaceService.award(1, 555)
                self.assertTrue(result["ok"])
                self.assertEqual(result["prize"], 5000)
                self.assertEqual(PRIZES[1], 5000)
                self.assertEqual(PRIZES[2], 2500)
                self.assertEqual(PRIZES[3], 1000)
                again = ReferralRaceService.award(1, 556)
                self.assertFalse(again["ok"])
                self.assertEqual(again["reason"], "place")
                other = ReferralRaceService.award(2, 555)
                self.assertFalse(other["ok"])
                self.assertEqual(other["reason"], "already")

    def test_profile_block_has_places_without_nicks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                ReferralRaceService.start(now=datetime(2026, 9, 3, 10, 0, 0), rng=random.Random(9))
                text = _race_profile_block(42)
                self.assertIn("Ваше место в топе", text)
                self.assertIn("#1", text)
                self.assertIn("боевых", text)
                self.assertIn("серых", text)
                self.assertIn("5000", text)
                self.assertNotIn("@", text)
                self.assertNotIn("m1", text)

    def test_profile_hides_race_when_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                self.assertEqual(_race_profile_block(42), "")
                ReferralRaceService.start(now=datetime(2026, 9, 3, 10, 0, 0), rng=random.Random(9))
                ReferralRaceService.stop()
                self.assertEqual(_race_profile_block(42), "")
