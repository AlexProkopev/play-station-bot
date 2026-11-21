import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import roulette as roulette_module
from app.services import users as users_module
from app.services.roulette import RouletteService
from app.services.users import UserService


class RouletteServiceTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        return (
            patch.object(roulette_module, "ROULETTE_FILE", os.path.join(temp_dir, "roulette.json")),
            patch.object(roulette_module.PostgresDocumentStore, "is_enabled", return_value=False),
            patch.object(users_module, "USERS_FILE", os.path.join(temp_dir, "users.json")),
            patch.object(users_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_parse_money_and_item_prizes(self) -> None:
        money = RouletteService.parse_prize_line("5000 руб")
        self.assertEqual(money["kind"], "money")
        self.assertEqual(money["amount"], 5000.0)
        item = RouletteService.parse_prize_line("Наушники")
        self.assertEqual(item["kind"], "item")
        self.assertEqual(item["label"], "Наушники")
        prizes, error = RouletteService.parse_prize_lines("5000\n3000\nНаушники", 3)
        self.assertEqual(error, "")
        self.assertEqual(len(prizes), 3)
        self.assertEqual(prizes[0]["kind"], "money")
        self.assertEqual(prizes[2]["kind"], "item")

    def test_join_and_secret_place_winners_pay_money(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                UserService.reset_runtime_cache()
                UserService.add_user(11, "one")
                UserService.add_user(22, "two")
                UserService.add_user(33, "three")
                prizes, error = RouletteService.parse_prize_lines("1000\nНаушники", 2)
                self.assertEqual(error, "")
                game = RouletteService.create_game(
                    created_by=1,
                    title="Тестовая игра",
                    max_participants=3,
                    conditions_text="Описание розыгрыша",
                    description="Описание розыгрыша",
                    winners_count=2,
                    prizes=prizes,
                )
                game_id = int(game["id"])
                ok, _ = RouletteService.add_participant(game_id, 11, "one", "One", bot_id=111)
                self.assertTrue(ok)
                ok, _ = RouletteService.add_participant(game_id, 22, "two", "Two", bot_id=222)
                self.assertTrue(ok)
                ok, _ = RouletteService.add_participant(game_id, 33, "three", "Three", bot_id=111)
                self.assertTrue(ok)
                again, status = RouletteService.add_participant(game_id, 11, "one", "One")
                self.assertFalse(again)
                self.assertIn("уже", status.lower())

                public = RouletteService.format_public_game_text(RouletteService.get_game(game_id))
                self.assertIn("1000 руб", public)
                self.assertIn("Наушники", public)
                self.assertNotIn("11", public)

                ok, _ = RouletteService.assign_place_winner(game_id, 1, 22)
                self.assertTrue(ok)
                dup, _ = RouletteService.assign_place_winner(game_id, 2, 22)
                self.assertFalse(dup)
                ok, _ = RouletteService.assign_place_winner(game_id, 2, 11)
                self.assertTrue(ok)
                game = RouletteService.get_game(game_id)
                self.assertTrue(RouletteService.all_places_assigned(game))
                self.assertNotIn("победител", RouletteService.format_public_results_text(game).lower())

                RouletteService.close_game(game_id)
                payout_ok, _, info = RouletteService.apply_finish_payouts(game_id)
                self.assertTrue(payout_ok)
                self.assertEqual(info["credited"], 1)
                self.assertEqual(UserService.get_balance(22), 1000.0)
                self.assertEqual(UserService.get_balance(11), 0.0)
                RouletteService.mark_result_announced(game_id)
                results = RouletteService.format_public_results_text(RouletteService.get_game(game_id))
                self.assertIn("@two", results)
                self.assertIn("Наушники", results)
                self.assertIn("1000 руб", results)

                pending = RouletteService.get_pending_results_for_user(33)
                self.assertEqual(len(pending), 1)
                participant = next(
                    item for item in RouletteService.get_game(game_id)["participants"] if int(item["user_id"]) == 22
                )
                self.assertTrue(RouletteService.should_deliver_on_bot(participant, 222))
                self.assertFalse(RouletteService.should_deliver_on_bot(participant, 111))
