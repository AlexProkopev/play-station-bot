import unittest

from app.services.disputes import (
    TELEGRAM_MESSAGE_LIMIT,
    build_admin_ticket_text,
    build_dispute_need_purchase_text,
    build_location_preview_text,
    build_shop_rules_text,
    can_open_dispute,
)


class DisputeAccessTests(unittest.TestCase):
    def test_zero_purchases_cannot_open_dispute(self) -> None:
        self.assertFalse(can_open_dispute(0))
        self.assertFalse(can_open_dispute(None))
        self.assertFalse(can_open_dispute("0"))

    def test_one_or_more_purchases_can_open_dispute(self) -> None:
        self.assertTrue(can_open_dispute(1))
        self.assertTrue(can_open_dispute(3))
        self.assertTrue(can_open_dispute("2"))


class DisputeCopyTests(unittest.TestCase):
    def test_rules_fit_in_one_telegram_message(self) -> None:
        text = build_shop_rules_text("@support")
        self.assertLessEqual(len(text), TELEGRAM_MESSAGE_LIMIT)
        self.assertIn("PlayStation™Store", text)
        self.assertIn("@support", text)

    def test_need_purchase_text_explains_support(self) -> None:
        text = build_dispute_need_purchase_text()
        self.assertIn("1 покупка", text)
        self.assertIn("поддержку", text.lower())

    def test_admin_ticket_includes_answers_and_photos(self) -> None:
        text = build_admin_ticket_text(
            user_id=42,
            username="buyer",
            kind="lost",
            answers=[{"title": "Время прибытия", "text": "18:00"}],
            photos=[{"title": "📷 Дальний ракурс"}],
            comment="ищу уже час",
        )
        self.assertIn("42", text)
        self.assertIn("18:00", text)
        self.assertIn("ищу уже час", text)
        self.assertIn("Вынести решение", text)

    def test_location_preview_builds_qa(self) -> None:
        text = build_location_preview_text(
            "empty",
            answers=[{"title": "Процесс поиска", "text": "смотрел 20 минут"}],
            photos=[{"title": "📷 ближний"}],
        )
        self.assertIn("смотрел 20 минут", text)
        self.assertIn("ближний", text)


if __name__ == "__main__":
    unittest.main()
