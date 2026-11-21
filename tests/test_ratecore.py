import hashlib
import hmac
import json
import unittest

from app.keyboards import get_ratecore_methods_keyboard, get_ratecore_submethods_keyboard
from app.services.payments import (
    STATUS_POLL_429_BACKOFF_SEC,
    STATUS_POLL_INTERVAL_SEC,
    STATUS_POLL_REQUISITES_MIN_SEC,
    STATUS_POLL_REQUISITES_SEC,
    _clear_status_poll,
    _fallback_requisites_text,
    _has_payment_requisites,
    _poll_delay_from_payload,
    _requisites_markup,
    _schedule_status_poll,
    _status_poll_is_due,
    admin_action_keyboard,
    admin_created_keyboard,
    admin_created_text,
    admin_paid_text,
    admin_requisites_failed_text,
    admin_requisites_ready_text,
    admin_user_cancelled_text,
    incoming_payment_status,
    is_ratecore_order,
    payment_retry_summary,
    requisites_search_loader_text,
    CARD_BANK_HINT_TEXT,
    should_show_card_bank_hint,
)
from app.services.ratecore import (
    build_canonical,
    encode_json,
    estimate_payer_amount,
    format_approx_amount_suffix,
    group_payment_methods,
    method_fee_percent,
    sign_canonical,
    verify_callback_signature,
    _unwrap,
)
from app.utils import order_ttl
from datetime import timedelta


class RatecoreSigningTests(unittest.TestCase):
    def test_hmac_v3_canonical_and_signature(self) -> None:
        payload = {"order_id": "order-1001", "amount": 5000, "method": "card"}
        body = encode_json(payload)
        canonical = build_canonical(
            "POST",
            "/api/sandbox/v1/payments",
            "1784358000",
            "2d6d7b64fdd84e42a1e5d21756f6c395",
            "transport-retry-1001",
            body,
        )
        self.assertEqual(canonical.split("\n")[0], "POST")
        self.assertEqual(canonical.split("\n")[1], "/api/sandbox/v1/payments")
        self.assertEqual(canonical.split("\n")[4], "transport-retry-1001")
        self.assertEqual(canonical.split("\n")[5], hashlib.sha256(body).hexdigest())
        signature = sign_canonical(canonical, "test-secret")
        self.assertEqual(len(signature), 64)
        self.assertEqual(
            signature,
            hmac.new(b"test-secret", canonical.encode(), hashlib.sha256).hexdigest(),
        )

    def test_callback_signature_uses_raw_body(self) -> None:
        raw = b'{"event":"payment.paid","event_id":"E1"}'
        secret = "cb-secret"
        good = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_callback_signature(raw, good, secret))
        self.assertTrue(verify_callback_signature(raw, good.upper(), secret))
        self.assertFalse(verify_callback_signature(raw, "00" * 32, secret))
        rebuilt = json.dumps(json.loads(raw), separators=(", ", ": ")).encode()
        self.assertFalse(verify_callback_signature(rebuilt, good, secret))

    def test_unwrap_nested_payment_envelope(self) -> None:
        self.assertEqual(
            _unwrap({"status": 200, "data": {"id": "PG-1", "status": "processing"}})["id"],
            "PG-1",
        )
        self.assertEqual(
            _unwrap({"payment": {"id": "PG-2", "status": "requisites_ready"}})["status"],
            "requisites_ready",
        )
        self.assertEqual(_unwrap({"id": "PG-3", "status": "queued"})["id"], "PG-3")

    def test_group_payment_methods_uses_bank_codes_and_qr(self) -> None:
        grouped = group_payment_methods(
            [
                {"key": "card", "name": "Карта"},
                {"key": "simcard", "name": "SIMCARD"},
                {"key": "sbp", "name": "СБП"},
                {"key": "qr_sber", "name": "QR Sber"},
                {"key": "qr_smart", "name": "QR — любой банк"},
                {
                    "key": "bank_to_bank",
                    "name": "Межбанк",
                    "bank_codes": [
                        {"key": "alfa", "name": "ALFA BANK"},
                        {"key": "sber", "name": "SBER"},
                    ],
                },
                {"key": "tbank", "name": "TBank"},
                {"key": "sngc_abh", "name": "SNGC ABH"},
                {"key": "nspk", "name": "NSPK"},
            ]
        )
        keys = [item["key"] for item in grouped]
        self.assertEqual(keys[0], "_group_card")
        self.assertIn("simcard", keys)
        self.assertIn("sbp", keys)
        self.assertIn("_group_other", keys)
        card = next(item for item in grouped if item["key"] == "_group_card")
        self.assertEqual(
            {child["key"] for child in card["submethods"]},
            {"card", "nspk", "sngc_abh", "_group_card_banks"},
        )
        nspk = next(child for child in card["submethods"] if child["key"] == "nspk")
        self.assertEqual(nspk["name"], "НСПК")
        banks = next(child for child in card["submethods"] if child["key"] == "_group_card_banks")
        self.assertEqual(banks["name"], "Выбрать банк и оплатить")
        self.assertEqual({child["key"] for child in banks["submethods"]}, {"alfa", "sber", "tbank"})
        other = next(item for item in grouped if item["key"] == "_group_other")
        self.assertEqual({child["key"] for child in other["submethods"]}, {"_group_qr", "_group_intra"})
        qr = next(child for child in other["submethods"] if child["key"] == "_group_qr")
        intra = next(child for child in other["submethods"] if child["key"] == "_group_intra")
        self.assertEqual({child["key"] for child in qr["submethods"]}, {"qr_sber", "qr_smart"})
        self.assertEqual({child["key"] for child in intra["submethods"]}, {"alfa", "sber", "tbank"})
        self.assertNotIn("nspk", {child["key"] for child in intra["submethods"]})

    def test_group_card_and_sbp_variants(self) -> None:
        grouped = group_payment_methods(
            [
                {"key": "card", "name": "Карта"},
                {"key": "card_pdf", "name": "Карта PDF"},
                {"key": "sngc_abh", "name": "SNGC ABH"},
                {"key": "nspk", "name": "NSPK"},
                {"key": "sbp", "name": "СБП"},
                {"key": "sbp_pdf", "name": "СБП PDF"},
                {"key": "sbp_sng", "name": "СБП СНГ"},
                {"key": "simcard", "name": "SIM"},
                {"key": "qr_sber", "name": "QR"},
            ]
        )
        by_key = {item["key"]: item for item in grouped}
        self.assertEqual(
            {child["key"] for child in by_key["_group_card"]["submethods"]},
            {"card", "card_pdf", "nspk", "sngc_abh"},
        )
        self.assertEqual({child["key"] for child in by_key["_group_sbp"]["submethods"]}, {"sbp", "sbp_pdf", "sbp_sng"})
        self.assertIn("simcard", by_key)
        self.assertEqual({child["key"] for child in by_key["_group_other"]["submethods"]}, {"_group_qr"})
        markup = get_ratecore_methods_keyboard(
            [
                {"key": "card", "name": "Карта"},
                {"key": "card_pdf", "name": "Карта PDF"},
                {"key": "sngc_abh", "name": "SNGC ABH"},
                {"key": "sbp", "name": "СБП"},
                {"key": "sbp_pdf", "name": "СБП PDF"},
                {"key": "sbp_sng", "name": "СБП СНГ"},
                {"key": "simcard", "name": "SIM"},
            ],
            crypto_assets=[{"key": "btc", "name": "Bitcoin"}, {"key": "ltc", "name": "Litecoin"}, {"key": "usdt", "name": "USDT"}],
            show_balance=True,
        )
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("Карта · 3 варианта" in text for text in labels))
        self.assertTrue(any("СБП · 3 варианта" in text for text in labels))
        self.assertTrue(any("Баланс телефона" in text for text in labels))
        self.assertTrue(any("С баланса" in text for text in labels))
        # Crypto buttons должны быть убраны из выбора способа оплаты
        self.assertFalse(any(cb and cb.startswith("rcc_") for cb in callbacks))
        self.assertFalse(any("Bitcoin" in text for text in labels))
        self.assertFalse(any("Litecoin" in text for text in labels))

    def test_estimate_payer_amount_uses_client_share(self) -> None:
        self.assertEqual(estimate_payer_amount(2500, 23), 2903)
        self.assertEqual(estimate_payer_amount(2500, item={"key": "sbp"}), 2903)
        self.assertEqual(estimate_payer_amount(2500, item={"key": "sbp_pdf"}), 2990)
        self.assertEqual(estimate_payer_amount(2500, item={"key": "simcard"}), 3025)
        self.assertEqual(method_fee_percent({"your_percent": "20%"}), 20.0)
        self.assertEqual(format_approx_amount_suffix(2500, {"key": "sbp"}), " · ~2903 ₽")
        self.assertEqual(
            format_approx_amount_suffix(
                2500,
                {"submethods": [{"key": "sbp"}, {"key": "sbp_pdf"}]},
            ),
            " · ~2903-2990 ₽",
        )

    def test_methods_keyboard_shows_approx_amounts(self) -> None:
        markup = get_ratecore_methods_keyboard(
            [
                {"key": "sbp", "name": "СБП", "your_percent": 23},
                {"key": "simcard", "name": "SIMCARD", "your_percent": 30},
            ],
            amount=2500,
            show_balance=True,
        )
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("~2903 ₽" in text for text in labels))
        self.assertTrue(any("~3025 ₽" in text for text in labels))
        self.assertTrue(any("С баланса · 2500 ₽" in text for text in labels))
        self.assertFalse(any("70%" in text for text in labels))
        self.assertFalse(any("комис" in text.casefold() for text in labels))


class RatecoreOrderHelpersTests(unittest.TestCase):
    def test_ratecore_order_detection(self) -> None:
        self.assertTrue(is_ratecore_order({"type": "ratecore"}))
        self.assertFalse(is_ratecore_order({"type": "card_rf"}))
        self.assertFalse(is_ratecore_order(None))

    def test_paid_waiting_admin_ttl_is_long(self) -> None:
        self.assertEqual(
            order_ttl({"type": "ratecore", "status": "paid_waiting_admin"}),
            timedelta(days=2),
        )
        self.assertEqual(order_ttl({"type": "ratecore", "ttl_minutes": 12}), timedelta(minutes=12))

    def test_admin_paid_text_includes_settlement(self) -> None:
        text = admin_paid_text(
            42,
            "buyer",
            {
                "purpose": "order",
                "city": "Город",
                "district": "Район",
                "product": "Товар",
                "treasure_type": "Тайник",
                "payment_id": "SPAY-1",
                "method": "sbp",
            },
            {
                "payer_amount": "6150",
                "base_amount": "5000",
                "financials": {"settlement_credit_usdt_amount": "12.34"},
            },
        )
        self.assertIn("12.34 USDT", text)
        self.assertIn("5000", text)
        self.assertIn("6150", text)
        self.assertIn("SPAY-1", text)
        paid_kb = admin_action_keyboard(42, {"purpose": "order"})
        paid_callbacks = [btn.callback_data for row in paid_kb.inline_keyboard for btn in row]
        self.assertIn("admin_confirm_order_42", paid_callbacks)
        self.assertIn("admin_cancel_order_42", paid_callbacks)
        self.assertIn("admin_chat_42", paid_callbacks)

    def test_admin_paid_text_without_settlement_keeps_city(self) -> None:
        text = admin_paid_text(
            42,
            "buyer",
            {
                "purpose": "order",
                "city": "Белгород",
                "district": "Центр",
                "product": "Товар",
                "treasure_type": "Тайник",
                "method_name": "Оплата с баланса",
                "price": 2500,
            },
            {"payer_amount": 2500, "base_amount": 2500},
            include_settlement=False,
        )
        self.assertIn("Белгород", text)
        self.assertIn("Центр", text)
        self.assertIn("Тайник", text)
        self.assertIn("Оплата с баланса", text)
        self.assertNotIn("USDT", text)

    def test_admin_created_text_looks_like_old_watch(self) -> None:
        text = admin_created_text(
            42,
            "buyer",
            {
                "purpose": "order",
                "city": "Белгород",
                "district": "Центр",
                "product": "Товар",
                "treasure_type": "Тайник",
                "payment_id": "SPAY-NEW",
                "method": "sbp",
                "method_name": "СБП",
                "price": 2500,
                "payer_amount": 2901,
                "expires_at": "2026-09-04T09:06:49+00:00",
            },
        )
        self.assertIn("Новая заявка", text)
        self.assertIn("Ищем реквизиты", text)
        self.assertIn("Admin Watch", text)
        self.assertIn("Белгород", text)
        self.assertIn("Центр", text)
        self.assertIn("2500", text)
        self.assertIn("2901", text)
        self.assertIn("СБП", text)
        self.assertIn("SPAY-NEW", text)
        markup = admin_created_keyboard(42, {"purpose": "order"})
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("admin_chat_42", callbacks)
        self.assertNotIn("admin_confirm_order_42", callbacks)
        self.assertNotIn("admin_cancel_order_42", callbacks)

    def test_admin_requisites_ready_text_waits_for_payment(self) -> None:
        text = admin_requisites_ready_text(
            42,
            "buyer",
            {
                "purpose": "order",
                "city": "Белгород",
                "district": "Центр",
                "product": "Товар",
                "treasure_type": "Тайник",
                "payment_id": "SPAY-READY",
                "method_name": "СБП",
                "price": 2500,
                "payer_amount": 2901,
            },
        )
        self.assertIn("получил реквизиты", text)
        self.assertIn("Ожидаем оплату", text)
        self.assertIn("SPAY-READY", text)
        self.assertIn("Белгород", text)

    def test_admin_requisites_failed_text_offers_retry(self) -> None:
        text = admin_requisites_failed_text(
            42,
            "buyer",
            {
                "purpose": "order",
                "city": "Белгород",
                "product": "Товар",
                "method_name": "СБП",
                "payment_id": "SPAY-FAIL",
            },
        )
        self.assertIn("Реквизиты не выданы", text)
        self.assertIn("другой способ", text)
        self.assertIn("SPAY-FAIL", text)

    def test_admin_user_cancelled_text_depends_on_requisites(self) -> None:
        waiting = admin_user_cancelled_text(
            42,
            "buyer",
            {"purpose": "order", "payment_id": "SPAY-WAIT", "requisites_shown": False},
            cancel_count=1,
            cancel_limit=3,
        )
        after = admin_user_cancelled_text(
            42,
            "buyer",
            {"purpose": "order", "payment_id": "SPAY-SHOWN", "requisites_shown": True},
            cancel_count=2,
            cancel_limit=3,
        )
        self.assertIn("пока искали реквизиты", waiting)
        self.assertIn("1/3", waiting)
        self.assertIn("после реквизитов", after)
        self.assertIn("2/3", after)

    def test_methods_keyboard_groups_instead_of_pages(self) -> None:
        methods = [{"key": f"m{i}", "name": f"Method {i}"} for i in range(10)]
        markup = get_ratecore_methods_keyboard(methods, page=0)
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any(cb.startswith("rcg_") for cb in callbacks))
        self.assertNotIn("rcp_1", callbacks)
        self.assertTrue(any("Другие способы" in text for text in labels))
        self.assertTrue(any("Закрыть" in text for text in labels))

    def test_nested_methods_open_submethods_screen(self) -> None:
        methods = [
            {
                "key": "card",
                "name": "Карта",
            },
            {
                "key": "card_pdf",
                "name": "Карта PDF",
            },
            {
                "key": "qr_sber",
                "name": "QR Sber",
            },
            {
                "key": "qr_alfa",
                "name": "QR Alfa",
            },
            {
                "key": "sbp",
                "name": "СБП",
            },
            {
                "key": "sbp_pdf",
                "name": "СБП PDF",
            },
        ]
        markup = get_ratecore_methods_keyboard(methods)
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("Карта · 2 варианта" in text for text in labels))
        self.assertTrue(any("СБП · 2 варианта" in text for text in labels))
        self.assertTrue(any("Другие способы" in text for text in labels))
        self.assertTrue(any(cb.startswith("rcg_") for cb in callbacks))
        self.assertNotIn("rcp_1", callbacks)

        other_index = next(
            int(cb.split("_", 1)[1])
            for cb, label in zip(callbacks, labels)
            if cb.startswith("rcg_") and "Другие" in label
        )
        other_screen = get_ratecore_submethods_keyboard(methods, other_index)
        other_callbacks = [button.callback_data for row in other_screen.inline_keyboard for button in row]
        self.assertIn("rcn__group_qr", other_callbacks)

        qr_screen = get_ratecore_submethods_keyboard(methods, other_index, nest_key="_group_qr")
        qr_callbacks = [button.callback_data for row in qr_screen.inline_keyboard for button in row]
        self.assertIn("rcm_qr_sber", qr_callbacks)
        self.assertIn("rcm_qr_alfa", qr_callbacks)

    def test_requisites_are_detected_without_status(self) -> None:
        self.assertTrue(
            _has_payment_requisites(
                {
                    "method": "sbp",
                    "requisites": {"phone_number": "+79990000000", "amount": "2901"},
                }
            )
        )
        self.assertFalse(_has_payment_requisites({"method": "sbp", "order_id": "order-1"}))

    def test_sandbox_sbp_link_is_shown_without_mock_comment(self) -> None:
        payload = {
            "method": "sbp",
            "payer_amount": "2901",
            "currency": "RUB",
            "requisites": {
                "amount": "2901",
                "comment": "Sandbox mock requisites",
                "currency": "RUB",
                "expires_at": "2026-09-04T09:06:49+00:00",
                "method": "sbp",
                "payment_url": "https://sandbox.example/pay/spay-test",
                "qr_payload": "sandbox://mock-qr/spay-test",
                "type": "payment_link",
            },
        }
        text = _fallback_requisites_text(payload)
        self.assertIn("2901", text)
        self.assertIn("СБП", text)
        self.assertIn("https://sandbox.example/pay/spay-test", text)
        self.assertIn("04.09 12:06 (МСК)", text)
        self.assertNotIn("Sandbox mock requisites", text)
        self.assertNotIn("2026-09-04T09:06:49", text)
        markup = _requisites_markup(payload)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://sandbox.example/pay/spay-test")
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "nav_cancel")

    def test_card_requisites_show_number_and_holder(self) -> None:
        text = _fallback_requisites_text(
            {
                "method": "card",
                "payer_amount": "1161",
                "requisites": {
                    "card_number": "2200700123456789",
                    "card_holder": "IVAN IVANOV",
                    "bank_name": "Sber",
                    "comment": "заказ 15",
                    "expires_at": "2026-09-04T09:06:49+00:00",
                },
            }
        )
        self.assertIn("2200700123456789", text)
        self.assertIn("IVAN IVANOV", text)
        self.assertIn("Sber", text)
        self.assertIn("заказ 15", text)

    def test_payment_retry_summary_keeps_order_context(self) -> None:
        text = payment_retry_summary(
            {
                "city": "Белгород",
                "district": "Центр",
                "treasure_type": "Тайник",
                "product": {"name": "Товар", "price": 2500},
            },
            {"purpose": "order"},
        )
        self.assertIn("Белгород", text)
        self.assertIn("Товар", text)
        self.assertIn("2500", text)
        self.assertIsNone(payment_retry_summary({}, {"purpose": "order"}))

    def test_processing_payload_with_requisites_is_ready(self) -> None:
        self.assertEqual(
            incoming_payment_status(
                {
                    "status": "processing",
                    "requisites": {"card_number": "2200700123456789"},
                }
            ),
            "awaiting_payment",
        )
        self.assertEqual(incoming_payment_status({"status": "processing"}), "processing")
        self.assertEqual(
            incoming_payment_status(
                {"status": "queued"},
                event="payment.requisites_ready",
            ),
            "requisites_ready",
        )
        self.assertTrue(
            _has_payment_requisites(
                {
                    "status": "processing",
                    "next_action": "pay",
                    "requisites": {"qr_payload": "https://pay.example/qr"},
                }
            )
        )
        self.assertEqual(
            incoming_payment_status(
                {
                    "status": "failed",
                    "reason_code": "requisites_unavailable",
                    "message": "Payment requisites are temporarily unavailable.",
                }
            ),
            "failed",
        )
        self.assertEqual(
            incoming_payment_status(
                {
                    "status": "processing",
                    "message": "Сейчас по выбранному способу нет доступных реквизитов.",
                }
            ),
            "failed",
        )

    def test_status_poll_respects_backoff(self) -> None:
        payment_id = "PG-TEST-POLL"
        _clear_status_poll(payment_id)
        self.assertFalse(_status_poll_is_due(payment_id))
        _schedule_status_poll(payment_id, STATUS_POLL_INTERVAL_SEC)
        self.assertFalse(_status_poll_is_due(payment_id))
        _schedule_status_poll(payment_id, STATUS_POLL_429_BACKOFF_SEC)
        self.assertFalse(_status_poll_is_due(payment_id))
        _clear_status_poll(payment_id)
        self.assertFalse(_status_poll_is_due(payment_id))

    def test_requisites_search_polls_faster_than_paid_watch(self) -> None:
        self.assertLess(STATUS_POLL_REQUISITES_SEC, STATUS_POLL_INTERVAL_SEC)
        self.assertEqual(
            _poll_delay_from_payload({"poll_after_ms": 1000}, waiting_requisites=True),
            STATUS_POLL_REQUISITES_MIN_SEC,
        )
        self.assertEqual(
            _poll_delay_from_payload({"poll_after_ms": 25000}, waiting_requisites=True),
            STATUS_POLL_REQUISITES_SEC,
        )
        self.assertEqual(
            _poll_delay_from_payload({"poll_after_ms": 1000}, waiting_requisites=False),
            STATUS_POLL_INTERVAL_SEC,
        )

    def test_requisites_loader_text_animates(self) -> None:
        first = requisites_search_loader_text(0)
        second = requisites_search_loader_text(1)
        third = requisites_search_loader_text(2)
        self.assertIn("Поиск реквизитов", first)
        self.assertIn("Ещё ищем", first)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertEqual(first, requisites_search_loader_text(3))

    def test_card_bank_hint_after_third_fail(self) -> None:
        self.assertFalse(should_show_card_bank_hint(0))
        self.assertFalse(should_show_card_bank_hint(2))
        self.assertTrue(should_show_card_bank_hint(3))
        self.assertTrue(should_show_card_bank_hint(4))
        self.assertIn("оплату по карте", CARD_BANK_HINT_TEXT)
        self.assertIn("Выбрать банк и оплатить", CARD_BANK_HINT_TEXT)
