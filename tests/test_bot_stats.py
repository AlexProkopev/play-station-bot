import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from app.services import bot_stats as stats_module
from app.services.bot_stats import BotStatsService, build_stats_text


class BotStatsTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        stats_file = os.path.join(temp_dir, "bot_stats.json")
        return (
            patch.object(stats_module, "STATS_FILE", stats_file),
            patch.object(stats_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_start_captcha_block_and_periods(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                now = datetime(2026, 9, 18, 12, 0, 0)
                with patch("app.services.bot_stats.get_now_msk", return_value=now):
                    BotStatsService.note_start(11)
                    BotStatsService.note_start(11)
                    BotStatsService.note_start(22)
                    BotStatsService.note_captcha(11, is_new=True)
                    BotStatsService.note_captcha_fail(22)
                    BotStatsService.note_city_search(11, found=True)
                    BotStatsService.note_city_search(22, found=False)
                    BotStatsService.note_block(22)
                    BotStatsService.note_purchase(11, 2500)
                    BotStatsService.note_referral(11)
                periods = BotStatsService.period_bundle(now)
                today = periods["today"]
                self.assertEqual(today["starts"], 3)
                self.assertEqual(today["starts_unique"], 2)
                self.assertEqual(today["captcha_unique"], 1)
                self.assertEqual(today["captcha_new"], 1)
                self.assertEqual(today["no_captcha"], 1)
                self.assertEqual(today["captcha_fail"], 1)
                self.assertEqual(today["city_unique"], 2)
                self.assertEqual(today["city"], 2)
                self.assertEqual(today["city_found_unique"], 1)
                self.assertEqual(today["city_miss"], 1)
                self.assertEqual(today["blocked_unique"], 1)
                self.assertEqual(today["purchases"], 1)
                self.assertEqual(today["buyers"], 1)
                self.assertEqual(today["revenue"], 2500.0)
                self.assertEqual(today["referrals"], 1)
                self.assertEqual(periods["week"]["starts_unique"], 2)
                self.assertEqual(periods["month"]["buyers"], 1)

    def test_backfill_and_report_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                BotStatsService.backfill_from_users({
                    "101": {"joined": "2026-09-18 10:00:00", "active": True, "purchases_count": 1},
                    "102": {"joined": "2026-09-17 10:00:00", "active": False},
                })
                now = datetime(2026, 9, 18, 15, 0, 0)
                periods = BotStatsService.period_bundle(now)
                self.assertEqual(periods["today"]["captcha_new"], 1)
                self.assertEqual(periods["week"]["captcha_unique"], 2)
                text = build_stats_text(
                    {
                        "total": 10,
                        "active": 8,
                        "blocked": 2,
                        "buyers": 3,
                        "with_balance": 1,
                        "referred": 4,
                        "by_bot": {"860": 7, "—": 3},
                    },
                    periods,
                    now,
                )
                self.assertIn("В базе (все боты)", text)
                self.assertIn("Заблокировали бота", text)
                self.assertIn("Ввели капчу", text)
                self.assertIn("Искали город", text)
                self.assertIn("Воронка входа", text)
                self.assertIn("Сегодня", text)
                self.assertIn("7 дней", text)
                self.assertIn("30 дней", text)
                self.assertIn("По ботам", text)
                self.assertIn("Воронка", text)


if __name__ == "__main__":
    unittest.main()
