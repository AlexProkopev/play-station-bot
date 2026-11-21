import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.handlers.user_flow import (
    _PENDING_CANCEL_TEXTS,
    _is_paid_pending_lock,
    _is_pending_payment_lock,
)
from app.services.payments import PAID_WAITING_ADMIN
import app.keyboards as kb
from app.services import settings as settings_module
from app.services.settings import SettingsService
from app.states import OrderState, TopUpState


class AutoBroadcastIntervalTests(unittest.TestCase):
    def test_parse_minutes_and_hours(self) -> None:
        self.assertEqual(SettingsService.parse_interval_minutes("30"), 30)
        self.assertEqual(SettingsService.parse_interval_minutes("30м"), 30)
        self.assertEqual(SettingsService.parse_interval_minutes("2ч"), 120)
        self.assertEqual(SettingsService.parse_interval_minutes("2h"), 120)
        self.assertIsNone(SettingsService.parse_interval_minutes("abc"))
        self.assertIsNone(SettingsService.parse_interval_minutes("0"))

    def test_format_interval(self) -> None:
        self.assertEqual(SettingsService.format_interval_minutes(30), "30 мин")
        self.assertEqual(SettingsService.format_interval_minutes(120), "2 ч")
        self.assertEqual(SettingsService.format_interval_minutes(90), "1 ч 30 мин")

    def test_next_slot_keeps_cadence_after_late_send(self) -> None:
        from datetime import datetime, timedelta

        due = datetime(2026, 8, 20, 14, 0, 0)
        now = datetime(2026, 8, 20, 14, 10, 0)
        nxt = SettingsService.next_broadcast_slot(due, timedelta(hours=2), now)
        self.assertEqual(nxt, datetime(2026, 8, 20, 16, 0, 0))

    def test_next_slot_skips_missed_windows(self) -> None:
        from datetime import datetime, timedelta

        due = datetime(2026, 8, 20, 14, 0, 0)
        now = datetime(2026, 8, 20, 17, 0, 0)
        nxt = SettingsService.next_broadcast_slot(due, timedelta(hours=2), now)
        self.assertEqual(nxt, datetime(2026, 8, 20, 18, 0, 0))

    def test_resolve_next_uses_saved_future_slot(self) -> None:
        from datetime import datetime, timedelta

        now = datetime(2026, 8, 20, 15, 0, 0)
        saved = datetime(2026, 8, 20, 16, 0, 0)
        nxt = SettingsService.resolve_next_send_at(
            now=now,
            interval=timedelta(hours=2),
            last_sent_at=datetime(2026, 8, 20, 12, 0, 0),
            next_send_at=saved,
        )
        self.assertEqual(nxt, saved)


class AutoBroadcastClaimTests(unittest.TestCase):
    def test_each_bot_claims_the_same_slot_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_file = os.path.join(temp_dir, "settings.json")
            with patch.object(settings_module, "SETTINGS_FILE", settings_file), patch.object(
                settings_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                SettingsService.save_auto_broadcast(
                    {
                        "enabled": True,
                        "text": "привет",
                        "interval_minutes": 60,
                        "next_send_at": "2026-08-21 12:00:00",
                        "sending_slot": None,
                        "slot_claims": {},
                    }
                )
                now = datetime(2026, 8, 21, 12, 1, 0)
                self.assertEqual(SettingsService.claim_auto_broadcast_send(11, now), ("привет", True))
                self.assertEqual(SettingsService.claim_auto_broadcast_send(11, now), (None, False))
                self.assertEqual(SettingsService.claim_auto_broadcast_send(22, now), ("привет", False))
                cfg = SettingsService.get_auto_broadcast()
                self.assertEqual(cfg["next_send_at"], "2026-08-21 13:00:00")
                self.assertEqual(cfg["sending_slot"], "2026-08-21 12:00:00")

    def test_create_and_delete_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_file = os.path.join(temp_dir, "settings.json")
            with patch.object(settings_module, "SETTINGS_FILE", settings_file), patch.object(
                settings_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ), patch(
                "app.services.broadcast_media.PostgresDocumentStore.is_enabled",
                return_value=False,
            ):
                created = SettingsService.create_auto_broadcast()
                self.assertTrue(created["id"])
                SettingsService.save_auto_broadcast({"text": "второй", "enabled": True}, worker_id=created["id"])
                workers = SettingsService.list_auto_broadcasts()
                self.assertTrue(any(item["id"] == created["id"] for item in workers))
                self.assertTrue(SettingsService.delete_auto_broadcast(created["id"]))
                left = SettingsService.list_auto_broadcasts()
                self.assertFalse(any(item["id"] == created["id"] for item in left))

    def test_claim_allows_media_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_file = os.path.join(temp_dir, "settings.json")
            with patch.object(settings_module, "SETTINGS_FILE", settings_file), patch.object(
                settings_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                SettingsService.save_auto_broadcast(
                    {
                        "enabled": True,
                        "text": None,
                        "media": {"kind": "photo", "filename": "promo.jpg", "count": 1},
                        "interval_minutes": 60,
                        "next_send_at": "2026-08-21 12:00:00",
                        "sending_slot": None,
                        "slot_claims": {},
                    }
                )
                now = datetime(2026, 8, 21, 12, 1, 0)
                token, first = SettingsService.claim_auto_broadcast_send(11, now)
                self.assertEqual(token, SettingsService.MEDIA_ONLY_CLAIM)
                self.assertTrue(first)
                cfg = SettingsService.get_auto_broadcast()
                self.assertEqual(cfg["media"]["kind"], "photo")
                self.assertTrue(SettingsService.worker_has_payload(cfg))


class BroadcastMediaStoreTests(unittest.TestCase):
    def test_local_roundtrip_and_delete(self) -> None:
        from app.services import broadcast_media as media_module
        from app.services.broadcast_media import BroadcastMediaStore

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(media_module, "STORAGE_DIR", temp_dir), patch.object(
                media_module.PostgresDocumentStore,
                "is_enabled",
                return_value=False,
            ):
                BroadcastMediaStore.save(
                    "w1",
                    {
                        "caption_html": "<b>hi</b>",
                        "items": [
                            {
                                "type": "photo",
                                "filename": "a.jpg",
                                "data": b"\xff\xd8fake",
                            }
                        ],
                    },
                )
                decoded = BroadcastMediaStore.load_decoded("w1")
                self.assertIsNotNone(decoded)
                self.assertEqual(decoded["caption_html"], "<b>hi</b>")
                self.assertEqual(decoded["items"][0]["data"], b"\xff\xd8fake")
                BroadcastMediaStore.delete("w1")
                self.assertIsNone(BroadcastMediaStore.load("w1"))


class BroadcastHelperTests(unittest.TestCase):
    def test_media_kind_from_simple_objects(self) -> None:
        from types import SimpleNamespace

        from app.services.broadcast import media_kind_label, message_media_kind

        photo_msg = SimpleNamespace(
            photo=[SimpleNamespace(file_id="x")],
            animation=None,
            video=None,
            video_note=None,
            voice=None,
            audio=None,
            sticker=None,
            document=None,
            text=None,
        )
        self.assertEqual(message_media_kind(photo_msg), "photo")
        self.assertEqual(media_kind_label("document"), "📎 файл")
        text_msg = SimpleNamespace(
            photo=None,
            animation=None,
            video=None,
            video_note=None,
            voice=None,
            audio=None,
            sticker=None,
            document=None,
            text="hello",
        )
        self.assertEqual(message_media_kind(text_msg), "text")


class YooMoneyWalletTests(unittest.TestCase):
    def test_normalize_wallet_number_and_email(self) -> None:
        self.assertEqual(
            SettingsService.normalize_yoomoney_wallet("4100 1160 5020 257"),
            "410011605020257",
        )
        self.assertEqual(
            SettingsService.normalize_yoomoney_wallet("wallet@example.com"),
            "wallet@example.com",
        )
        self.assertIsNone(SettingsService.normalize_yoomoney_wallet("12345"))
        self.assertIsNone(SettingsService.normalize_yoomoney_wallet("not-an-email"))

    def test_commission_roundtrip(self) -> None:
        stored = {"yoomoney_commission": 0}

        def fake_load():
            return stored

        def fake_save(settings):
            stored.update(settings)

        with patch.object(SettingsService, "_load_settings", side_effect=fake_load), patch.object(
            SettingsService, "_save_settings", side_effect=fake_save
        ):
            self.assertEqual(SettingsService.get_yoomoney_commission(), 0)
            SettingsService.set_yoomoney_commission(250)
            self.assertEqual(SettingsService.get_yoomoney_commission(), 250)
            SettingsService.set_yoomoney_commission(-10)
            self.assertEqual(SettingsService.get_yoomoney_commission(), 0)


class YooMoneyPaymentTextTests(unittest.TestCase):
    def test_payment_text_includes_commission(self) -> None:
        from app.handlers.user_flow import _build_yoomoney_payment_text

        text = _build_yoomoney_payment_text(
            2300,
            "410011605020257",
            "abc123",
            "12:00",
            commission=300,
        )
        self.assertIn("2 300", text)
        self.assertIn("КОМИССИЯ", text)
        self.assertIn("300", text)

    def test_payment_text_hides_zero_commission(self) -> None:
        from app.handlers.user_flow import _build_yoomoney_payment_text

        text = _build_yoomoney_payment_text(2000, "410011605020257", "abc123", "12:00", commission=0)
        self.assertNotIn("КОМИССИЯ", text)


class RfCardPaymentTextTests(unittest.TestCase):
    def test_ticket_matches_operator_format_with_commission(self) -> None:
        from app.handlers.user_flow import _build_card_rf_payment_text

        text = _build_card_rf_payment_text(
            4900,
            "2200 3961 1916 4800",
            "4c2705dce0594bc19044c3c399d21994",
            "20:11",
            400,
            product_name="Товар 1.0г",
            user_id=1097037804,
            username="tonnu1982",
            city="Челябинск",
            district="Ленинский район",
            treasure_type="Магнит надежный - городской клад",
        )
        self.assertIn("Заявка на оплату картой РФ", text)
        self.assertIn("1097037804 (@tonnu1982)", text)
        self.assertIn("Челябинск", text)
        self.assertIn("Ленинский район", text)
        self.assertIn("4900 руб. (4500+400 комиссия)", text)
        self.assertIn("2200 3961 1916 4800", text)
        self.assertIn("20:11 (МСК)", text)
        self.assertNotIn("ВРЕМЯ НА ОПЛАТУ", text)

    def test_user_ticket_includes_payment_instructions(self) -> None:
        from app.handlers.user_flow import _build_card_rf_payment_text

        text = _build_card_rf_payment_text(
            4900,
            "2200 3961 1916 4800",
            "abc",
            "20:11",
            400,
            product_name="Товар 1.0г",
            user_id=1,
            username="user",
            city="Город",
            district="Район",
            treasure_type="Тип",
            include_instructions=True,
        )
        self.assertIn("После оплаты отправьте подтверждение перевода", text)
        self.assertIn("ВРЕМЯ НА ОПЛАТУ: 12 минут", text)
        self.assertIn("заявка будет закрыта автоматически", text)

    def test_ticket_hides_commission_when_zero(self) -> None:
        from app.handlers.user_flow import _build_card_rf_payment_text

        text = _build_card_rf_payment_text(
            4500,
            "2200 3961 1916 4800",
            "abc",
            "20:11",
            0,
            product_name="Item",
            user_id=1,
            username="user",
            city="Город",
            district="Район",
            treasure_type="Тип",
        )
        self.assertIn("4500 руб.", text)
        self.assertNotIn("комиссия", text)


class TopupCardTemplateTests(unittest.TestCase):
    def test_default_template_includes_card_and_amount(self) -> None:
        from unittest.mock import patch
        from app.handlers.user_flow import build_topup_card_payment_text

        with patch("app.handlers.user_flow.SettingsService.get_manual_payment_template", return_value=None):
            text = build_topup_card_payment_text(
                "2200 3961 1916 4800",
                {"amount": 32223, "expires_at": "08:20"},
            )
        self.assertIn("2200 3961 1916 4800", text)
        self.assertIn("32223", text)
        self.assertIn("08:20", text)
        self.assertIn("Реквизиты для пополнения", text)

    def test_admin_keyboard_has_card_buttons(self) -> None:
        from app.keyboards import get_admin_topup_request_keyboard

        markup = get_admin_topup_request_keyboard(8270697334, ["2200 3961 1916 4800", "4276 0000 0000 0001"])
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("admin_topup_send_8270697334_0", callbacks)
        self.assertIn("admin_topup_send_8270697334_1", callbacks)
        self.assertIn("admin_chat_8270697334", callbacks)
        self.assertIn("admin_cancel_topup_8270697334", callbacks)
        self.assertIn("admin_confirm_topup_8270697334", callbacks)


class PendingOrderLockTests(unittest.TestCase):
    def test_locks_waiting_for_receipt_and_ratecore_orders(self) -> None:
        self.assertTrue(_is_pending_payment_lock(None, OrderState.waiting_for_receipt.state))
        self.assertTrue(_is_pending_payment_lock(None, TopUpState.waiting_for_receipt.state))
        self.assertTrue(_is_pending_payment_lock({"type": "ratecore"}, None))
        self.assertFalse(_is_pending_payment_lock({"type": "balance"}, None))
        self.assertTrue(
            _is_pending_payment_lock({"type": "balance", "status": PAID_WAITING_ADMIN}, None)
        )
        self.assertFalse(_is_pending_payment_lock({"type": "card_rf"}, None))
        self.assertFalse(_is_pending_payment_lock(None, OrderState.waiting_for_city.state))

    def test_unpaid_ratecore_can_cancel_to_start(self) -> None:
        self.assertFalse(_is_paid_pending_lock({"type": "ratecore", "status": "awaiting_payment"}))
        self.assertTrue(_is_paid_pending_lock({"type": "ratecore", "status": PAID_WAITING_ADMIN}))
        self.assertTrue(_is_paid_pending_lock({"type": "balance", "status": PAID_WAITING_ADMIN}))
        self.assertIn(kb.BTN_CANCEL, _PENDING_CANCEL_TEXTS)


if __name__ == "__main__":
    unittest.main()
