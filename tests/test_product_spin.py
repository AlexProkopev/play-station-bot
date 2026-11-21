import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import roulette as roulette_module
from app.services import users as users_module
from app.services.product_spin import MIN_STAKE, ProductSpinService
from app.services.roulette import RouletteService
from app.services.users import UserService


class _AlwaysWin:
    def random(self) -> float:
        return 0.0


class _AlwaysLose:
    def random(self) -> float:
        return 0.999


class ProductSpinTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        return (
            patch.object(users_module, "USERS_FILE", os.path.join(temp_dir, "users.json")),
            patch.object(users_module.PostgresDocumentStore, "is_enabled", return_value=False),
            patch.object(roulette_module, "ROULETTE_FILE", os.path.join(temp_dir, "roulette.json")),
            patch.object(roulette_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_rejects_cheap_product_and_empty_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(7, "spin")
                ok, _ = ProductSpinService.can_play({"price": 150})
                self.assertFalse(ok)
                result = ProductSpinService.play(7, MIN_STAKE, rng=_AlwaysLose())
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "balance")

    def test_lose_and_win_update_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(8, "lucky")
                UserService.add_balance(8, 1000)
                lose = ProductSpinService.play(8, MIN_STAKE, rng=_AlwaysLose())
                self.assertTrue(lose["ok"])
                self.assertFalse(lose["win"])
                self.assertEqual(UserService.get_balance(8), 800)
                played, won = UserService.get_spin_stats(8)
                self.assertEqual(played, 1)
                self.assertEqual(won, 0)

                UserService.add_balance(8, 200)
                win = ProductSpinService.play(8, MIN_STAKE, rng=_AlwaysWin())
                self.assertTrue(win["ok"])
                self.assertTrue(win["win"])
                played, won = UserService.get_spin_stats(8)
                self.assertEqual(played, 2)
                self.assertEqual(won, MIN_STAKE)

    def test_profile_adds_raffle_and_spin(self) -> None:
        from app.handlers.user_flow import _game_profile_stats

        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(9, "mix")
                UserService.add_balance(9, 500)
                prizes, _ = RouletteService.parse_prize_lines("300", 1)
                game = RouletteService.create_game(
                    created_by=1,
                    title="R",
                    max_participants=5,
                    conditions_text="d",
                    winners_count=1,
                    prizes=prizes,
                )
                gid = int(game["id"])
                RouletteService.add_participant(gid, 9, "mix", "Mix")
                RouletteService.assign_place_winner(gid, 1, 9)
                RouletteService.apply_finish_payouts(gid)
                ProductSpinService.play(9, MIN_STAKE, rng=_AlwaysWin())
                played, won = _game_profile_stats(9)
                self.assertEqual(played, 2)
                self.assertEqual(won, 500.0)
