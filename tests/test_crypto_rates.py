import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.crypto import CryptoService
from app.utils import order_ttl


class CryptoRateTests(unittest.TestCase):
    def test_convert_rub_to_coin(self) -> None:
        self.assertAlmostEqual(CryptoService.convert_rub(8_500_000, 8_500_000), 1.0)
        self.assertAlmostEqual(CryptoService.convert_rub(4250, 8500), 0.5)

    def test_format_amount_strips_zeros(self) -> None:
        self.assertEqual(CryptoService.format_amount(0.00123000, "btc"), "0.00123")
        self.assertEqual(CryptoService.format_amount(1, "ltc"), "1")

    def test_parse_coingecko_payload(self) -> None:
        btc, ltc = CryptoService.parse_coingecko_payload(
            {"bitcoin": {"rub": 8500000.12}, "litecoin": {"rub": 9200.5}}
        )
        self.assertEqual(btc, 8500000.12)
        self.assertEqual(ltc, 9200.5)

    def test_rate_freshness(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        fresh = (now - timedelta(hours=2)).isoformat()
        stale = (now - timedelta(hours=25)).isoformat()
        self.assertTrue(CryptoService.is_rate_fresh(fresh, now=now))
        self.assertFalse(CryptoService.is_rate_fresh(stale, now=now))
        self.assertFalse(CryptoService.is_rate_fresh(None, now=now))

    def test_extract_tx_hash(self) -> None:
        txid = "a" * 64
        self.assertEqual(CryptoService.extract_tx_hash(txid), txid)
        self.assertEqual(CryptoService.extract_tx_hash(f"вот хеш {txid} проверьте"), txid)
        self.assertEqual(CryptoService.extract_tx_hash(f"0x{txid}"), txid)
        self.assertIsNone(CryptoService.extract_tx_hash("привет"))
        self.assertIsNone(CryptoService.extract_tx_hash("abc123"))

    def test_crypto_order_ttl_is_one_hour(self) -> None:
        self.assertEqual(order_ttl({"type": "btc"}), timedelta(hours=1))
        self.assertEqual(order_ttl({"type": "ltc"}), timedelta(hours=1))
        self.assertEqual(order_ttl({"type": "card_rf"}), timedelta(minutes=20))
        self.assertEqual(order_ttl({"type": "yoomoney"}), timedelta(minutes=25))
        self.assertEqual(order_ttl({"type": "yoomoney", "ttl_minutes": 25}), timedelta(minutes=25))

    def test_balance_topup_flag(self) -> None:
        self.assertTrue(CryptoService.is_balance_topup({"purpose": "balance_topup", "type": "btc"}))
        self.assertFalse(CryptoService.is_balance_topup({"type": "btc"}))
        self.assertFalse(CryptoService.is_balance_topup(None))

    def test_get_wallet_prefers_settings(self) -> None:
        with patch("app.services.crypto.SettingsService.pick_btc_wallet", return_value="bc1qfromdb"), patch(
            "app.services.crypto.SettingsService.pick_ltc_wallet", return_value="LTfromdb"
        ):
            self.assertEqual(CryptoService.get_wallet("btc"), "bc1qfromdb")
            self.assertEqual(CryptoService.get_wallet("ltc"), "LTfromdb")

    def test_topup_payment_text(self) -> None:
        from app.handlers.user_flow import _build_crypto_payment_text

        text = _build_crypto_payment_text(
            "btc",
            "Пополнение баланса",
            5000,
            "0.001",
            5_000_000,
            "bc1qwallet",
            "28.08 12:00",
            is_topup=True,
        )
        self.assertIn("ПОПОЛНЕНИЕ БАЛАНСА", text)
        self.assertIn("bc1qwallet", text)
        self.assertIn("0.001 BTC", text)
        self.assertNotIn("ВЫ ПОКУПАЕТЕ", text)


class CryptoRateRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_coingecko_when_fresh(self) -> None:
        now = datetime.now(timezone.utc)
        with patch(
            "app.services.crypto.SettingsService.get_crypto_rates",
            return_value=(100.0, 10.0, now.isoformat()),
        ), patch("app.services.crypto.httpx.AsyncClient") as client_cls:
            ok = await CryptoService.refresh_rates(force=False)
            self.assertTrue(ok)
            client_cls.assert_not_called()

    async def test_saves_rates_from_coingecko(self) -> None:
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = lambda: {"bitcoin": {"rub": 111.0}, "litecoin": {"rub": 22.0}}
        client = AsyncMock()
        client.get = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with patch(
            "app.services.crypto.SettingsService.get_crypto_rates",
            return_value=(None, None, None),
        ), patch(
            "app.services.crypto.SettingsService.set_crypto_rates"
        ) as set_rates, patch(
            "app.services.crypto.httpx.AsyncClient", return_value=client
        ):
            ok = await CryptoService.refresh_rates(force=True)
            self.assertTrue(ok)
            set_rates.assert_called_once_with(111.0, 22.0)


if __name__ == "__main__":
    unittest.main()
