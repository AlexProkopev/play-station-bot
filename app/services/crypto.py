import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import BTC_WALLET_ADDRESS, LTC_WALLET_ADDRESS
from app.http_ssl import windows_ssl_context
from app.services.settings import SettingsService

logger = logging.getLogger("app.services.crypto")

COINGECKO_SIMPLE_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,litecoin&vs_currencies=rub"
)
RATE_TTL = timedelta(hours=24)
CRYPTO_ORDER_TTL = timedelta(hours=1)
CRYPTO_ORDER_TYPES = {"btc", "ltc", "crypto"}
TX_HASH_RE = re.compile(r"(?:0x)?[0-9a-fA-F]{32,128}")
COIN_META = {
    "btc": {"symbol": "BTC", "title": "Bitcoin", "gecko_id": "bitcoin", "decimals": 8},
    "ltc": {"symbol": "LTC", "title": "Litecoin", "gecko_id": "litecoin", "decimals": 8},
}


class CryptoService:
    @classmethod
    def get_wallet(cls, coin: str) -> str:
        return cls.pick_wallet(coin)

    @classmethod
    def pick_wallet(cls, coin: str) -> str:
        coin = cls._norm_coin(coin)
        if coin == "btc":
            return SettingsService.pick_btc_wallet() or BTC_WALLET_ADDRESS
        return SettingsService.pick_ltc_wallet() or LTC_WALLET_ADDRESS

    @classmethod
    def get_ltc_wallet(cls) -> str:
        return cls.get_wallet("ltc")

    @classmethod
    def is_rate_fresh(cls, updated_at: str | None, now: datetime | None = None) -> bool:
        parsed = cls._parse_iso(updated_at)
        if parsed is None:
            return False
        current = now or datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return current - parsed < RATE_TTL

    @classmethod
    def parse_coingecko_payload(cls, payload: dict) -> tuple[float | None, float | None]:
        if not isinstance(payload, dict):
            return None, None
        btc = cls._positive_float((payload.get("bitcoin") or {}).get("rub"))
        ltc = cls._positive_float((payload.get("litecoin") or {}).get("rub"))
        return btc, ltc

    @classmethod
    async def refresh_rates(cls, force: bool = False) -> bool:
        _btc, _ltc, updated_at = SettingsService.get_crypto_rates()
        if not force and cls.is_rate_fresh(updated_at):
            return True

        try:
            async with httpx.AsyncClient(timeout=15.0, verify=windows_ssl_context()) as client:
                response = await client.get(COINGECKO_SIMPLE_PRICE_URL)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            logger.exception("CoinGecko rate refresh failed")
            return bool(_btc or _ltc)

        btc, ltc = cls.parse_coingecko_payload(payload)
        if not btc or not ltc:
            logger.warning("CoinGecko payload missing BTC/LTC RUB rates: %s", payload)
            return bool(_btc or _ltc)

        SettingsService.set_crypto_rates(btc, ltc)
        logger.info("Crypto rates updated from CoinGecko: BTC=%s LTC=%s", btc, ltc)
        return True

    @classmethod
    async def get_rate(cls, coin: str) -> Optional[float]:
        coin = cls._norm_coin(coin)
        await cls.refresh_rates(force=False)
        btc, ltc, _updated_at = SettingsService.get_crypto_rates()
        rate = btc if coin == "btc" else ltc
        if not rate or rate <= 0:
            logger.warning("%s/RUB rate is not available", coin.upper())
            return None
        return float(rate)

    @classmethod
    async def get_ltc_rub_rate(cls) -> Optional[float]:
        return await cls.get_rate("ltc")

    @classmethod
    def convert_rub(cls, rub_amount: float, rate: float) -> float:
        if rate <= 0:
            raise ValueError("Rate must be positive")
        return float(rub_amount) / float(rate)

    @classmethod
    def format_ltc_amount(cls, rub_amount: float, rate: float) -> float:
        return cls.convert_rub(rub_amount, rate)

    @classmethod
    def format_amount(cls, coin_amount: float, coin: str) -> str:
        decimals = COIN_META[cls._norm_coin(coin)]["decimals"]
        text = f"{float(coin_amount):.{decimals}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @classmethod
    def symbol(cls, coin: str) -> str:
        return COIN_META[cls._norm_coin(coin)]["symbol"]

    @classmethod
    def title(cls, coin: str) -> str:
        return COIN_META[cls._norm_coin(coin)]["title"]

    @classmethod
    def extract_tx_hash(cls, text: str | None) -> str | None:
        raw = " ".join((text or "").strip().split())
        if not raw:
            return None
        match = TX_HASH_RE.search(raw.replace("`", ""))
        if not match:
            return None
        value = match.group(0)
        if value.lower().startswith("0x"):
            value = value[2:]
        return value

    @classmethod
    def is_crypto_order(cls, order: dict | None) -> bool:
        return bool(order and order.get("type") in CRYPTO_ORDER_TYPES)

    @classmethod
    def is_balance_topup(cls, order: dict | None) -> bool:
        return bool(order and order.get("purpose") == "balance_topup")

    @classmethod
    def is_chest_boost(cls, order: dict | None) -> bool:
        return bool(order and order.get("purpose") == "chest_boost")

    @classmethod
    def _norm_coin(cls, coin: str) -> str:
        value = (coin or "").strip().lower()
        if value not in COIN_META:
            raise ValueError(f"Unsupported coin: {coin}")
        return value

    @staticmethod
    def _positive_float(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        return number

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
