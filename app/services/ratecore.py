from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any

import httpx

from app import config
from app.http_ssl import windows_ssl_context

logger = logging.getLogger("app.services.ratecore")

DEFAULT_TIMEOUT = 30.0


class RatecoreError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.retry_after_seconds = retry_after_seconds

    @property
    def reason_code(self) -> str:
        if isinstance(self.payload, dict):
            return str(self.payload.get("reason_code") or self.payload.get("error_code") or "")
        return ""


def encode_json(payload: dict | None) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_canonical(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    idempotency_key: str,
    raw_body: bytes,
) -> str:
    body_hash = hashlib.sha256(raw_body).hexdigest()
    return "\n".join(
        [
            method.upper(),
            path,
            timestamp,
            nonce,
            idempotency_key or "",
            body_hash,
        ]
    )


def sign_canonical(canonical: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_callback_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _api_prefix() -> str:
    return "/api/sandbox/v1" if config.is_ratecore_sandbox() else "/api/v1"


class RatecoreClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        sandbox: bool | None = None,
    ):
        self.base_url = (base_url or config.RATECORE_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.RATECORE_API_KEY
        self.api_secret = api_secret if api_secret is not None else config.RATECORE_API_SECRET
        self.sandbox = config.is_ratecore_sandbox() if sandbox is None else sandbox

    @property
    def prefix(self) -> str:
        return "/api/sandbox/v1" if self.sandbox else "/api/v1"

    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _signed_headers(self, method: str, path: str, raw_body: bytes, idempotency_key: str = "") -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        canonical = build_canonical(method, path, timestamp, nonce, idempotency_key, raw_body)
        signature = sign_canonical(canonical, self.api_secret)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Signature-Version": "3",
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        idempotency_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:
        if not self.configured():
            raise RatecoreError("Ratecore API keys are not configured")
        raw_body = encode_json(payload)
        headers = self._signed_headers(method, path, raw_body, idempotency_key)
        url = self.base_url + path
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=windows_ssl_context()) as client:
                response = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    content=raw_body if raw_body else None,
                )
        except httpx.HTTPError as exc:
            logger.exception("Ratecore transport error %s %s", method, path)
            raise RatecoreError(f"Ratecore transport error: {exc}") from exc

        parsed: Any = None
        if response.content:
            try:
                parsed = response.json()
            except ValueError:
                parsed = {"raw": response.text[:1000]}

        if response.status_code >= 400:
            message = "Ratecore request failed"
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or parsed.get("error") or message)
            logger.warning(
                "Ratecore %s %s -> %s %s",
                method,
                path,
                response.status_code,
                message,
            )
            raise RatecoreError(
                message,
                status_code=response.status_code,
                payload=parsed,
                retry_after_seconds=_retry_after_seconds(response, parsed),
            )
        return parsed

    async def list_methods(self) -> list[dict]:
        data = await self.request("GET", f"{self.prefix}/methods")
        if isinstance(data, dict):
            items = data.get("methods") or data.get("data") or []
        else:
            items = data
        if not isinstance(items, list):
            return []
        methods = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            children = _method_children(item)
            methods.append(_copy_method(item, submethods=children or None))
        return group_payment_methods(methods)

    async def list_crypto_assets(self) -> list[dict]:
        try:
            data = await self.request("GET", f"{self.prefix}/crypto/assets")
        except RatecoreError as exc:
            if exc.status_code in {404, 503}:
                logger.warning("Ratecore crypto assets unavailable: %s", exc)
                return []
            raise
        items = []
        if isinstance(data, dict):
            items = data.get("assets") or data.get("data") or data.get("methods") or []
        elif isinstance(data, list):
            items = data
        assets = []
        for item in items:
            if isinstance(item, str):
                assets.append({"key": item, "name": item})
                continue
            if not isinstance(item, dict):
                continue
            key = str(item.get("alias") or item.get("key") or item.get("asset") or "").strip()
            if not key:
                continue
            assets.append({"key": key, "name": str(item.get("name") or key)})
        return assets

    async def create_payment(
        self,
        *,
        order_id: str,
        amount,
        method: str,
        callback_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "order_id": order_id,
            "amount": _json_amount(amount),
            "method": method,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        data = await self.request(
            "POST",
            f"{self.prefix}/payments",
            payload=payload,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
        )
        return _unwrap(data)

    async def get_payment(self, payment_id: str) -> dict:
        data = await self.request("GET", f"{self.prefix}/payments/{payment_id}")
        return _unwrap(data)

    async def wait_payment(self, payment_id: str, *, timeout: float = 35.0) -> dict:
        data = await self.request(
            "GET",
            f"{self.prefix}/payments/{payment_id}/wait",
            timeout=timeout,
        )
        return _unwrap(data)

    async def create_crypto_payment(
        self,
        *,
        order_id: str,
        amount,
        asset: str,
        currency: str = "RUB",
        callback_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "order_id": order_id,
            "amount": _json_amount(amount),
            "currency": currency,
            "asset": asset,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        data = await self.request(
            "POST",
            f"{self.prefix}/crypto/payments",
            payload=payload,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
        )
        return _unwrap(data)

    async def get_crypto_payment(self, payment_id: str) -> dict:
        data = await self.request("GET", f"{self.prefix}/crypto/payments/{payment_id}")
        return _unwrap(data)

    async def wait_crypto_payment(self, payment_id: str, *, timeout: float = 35.0) -> dict:
        data = await self.request(
            "GET",
            f"{self.prefix}/crypto/payments/{payment_id}/wait",
            timeout=timeout,
        )
        return _unwrap(data)

    async def upload_receipt(
        self,
        payment_id: str,
        file_bytes: bytes,
        filename: str = "receipt.jpg",
        content_type: str = "image/jpeg",
    ) -> dict:
        """Upload receipt/check for a payment that requires PDF confirmation."""
        if not self.configured():
            raise RatecoreError("Ratecore API keys are not configured")
        
        path = f"{self.prefix}/payments/{payment_id}/receipt"
        url = self.base_url + path
        
        # For multipart uploads, we use different signing
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        # Sign with empty body for multipart
        canonical = build_canonical("POST", path, timestamp, nonce, "", b"")
        signature = sign_canonical(canonical, self.api_secret)
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "X-Signature-Version": "3",
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, verify=windows_ssl_context()) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    files={"file": (filename, file_bytes, content_type)},
                )
        except httpx.HTTPError as exc:
            logger.exception("Ratecore receipt upload transport error")
            raise RatecoreError(f"Ratecore transport error: {exc}") from exc
        
        parsed: Any = None
        if response.content:
            try:
                parsed = response.json()
            except ValueError:
                parsed = {"raw": response.text[:1000]}
        
        if response.status_code >= 400:
            message = "Receipt upload failed"
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or parsed.get("error") or message)
            logger.warning(
                "Ratecore receipt upload %s -> %s %s",
                payment_id,
                response.status_code,
                message,
            )
            raise RatecoreError(
                message,
                status_code=response.status_code,
                payload=parsed,
            )
        
        logger.info("Receipt uploaded for payment %s: %s", payment_id, parsed)
        return _unwrap(parsed) if parsed else {}

    async def sandbox_complete(self, payment_id: str) -> dict:
        if not self.sandbox:
            raise RatecoreError("Sandbox complete is available only in sandbox mode")
        data = await self.request(
            "POST",
            f"{self.prefix}/testing/payments/{payment_id}/complete",
            payload={},
            idempotency_key=uuid.uuid4().hex,
        )
        return _unwrap(data)

    async def configure_sandbox_webhook(self, callback_url: str) -> dict:
        if not self.sandbox:
            raise RatecoreError("Sandbox webhook settings are available only in sandbox mode")
        data = await self.request(
            "POST",
            f"{self.prefix}/webhook-settings",
            payload={
                "callback_url": callback_url,
                "is_enabled": True,
                "auto_webhook_on_status_change": True,
            },
            idempotency_key=uuid.uuid4().hex,
        )
        return _unwrap(data)

    async def reveal_sandbox_callback_secret(self) -> str:
        if not self.sandbox:
            raise RatecoreError("Sandbox secret reveal is available only in sandbox mode")
        data = await self.request(
            "POST",
            f"{self.prefix}/webhook-settings/reveal-secret",
            payload={},
            idempotency_key=uuid.uuid4().hex,
        )
        payload = _unwrap(data)
        secret = ""
        if isinstance(payload, dict):
            secret = str(
                payload.get("callback_secret")
                or (payload.get("data") or {}).get("callback_secret")
                or ""
            )
        return secret.strip()


def _retry_after_seconds(response: httpx.Response, parsed: Any) -> float | None:
    candidates: list[Any] = [response.headers.get("Retry-After")]
    if isinstance(parsed, dict):
        candidates.append(parsed.get("retry_after") or parsed.get("retryAfter"))
        details = parsed.get("details")
        if isinstance(details, dict):
            candidates.append(details.get("retry_after") or details.get("retryAfter"))
    for raw in candidates:
        if raw is None or raw == "":
            continue
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _unwrap(data: Any) -> dict:
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict) and (inner.get("id") or inner.get("public_id") or inner.get("status")):
        outer_status = str(data.get("status") or "")
        if "id" not in data and "public_id" not in data:
            return inner
        if outer_status.isdigit():
            return inner
    payment = data.get("payment")
    if isinstance(payment, dict) and (payment.get("id") or payment.get("public_id") or payment.get("status")):
        return payment
    return data


def _json_amount(amount) -> int | float | str:
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if number.is_integer():
        return int(number)
    return round(number, 2)


CLIENT_FEE_SHARE = 0.70
DEFAULT_METHOD_FEE_PERCENT = 23.0


def _as_fee_percent(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).replace("%", "").replace(",", ".").strip()
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number <= 1:
        return round(number * 100.0, 4)
    if number <= 80:
        return number
    return None


def _extract_fee_percent(item: dict | None) -> float | None:
    if not isinstance(item, dict):
        return None
    for key in (
        "fee_percent",
        "your_percent",
        "yours_percent",
        "your_fee_percent",
        "your_fee",
        "merchant_percent",
        "merchant_fee",
        "fee_percent_merchant",
        "commission_percent",
        "percent",
        "fee",
        "commission",
        "market_percent",
        "market_fee",
    ):
        parsed = _as_fee_percent(item.get(key))
        if parsed is not None:
            return parsed
    for nested_key in ("fees", "tariff", "pricing", "commission_info"):
        nested = item.get(nested_key)
        if isinstance(nested, dict):
            parsed = _extract_fee_percent(nested)
            if parsed is not None:
                return parsed
    return None


def default_fee_percent_for_key(key: str | None) -> float:
    lowered = str(key or "").casefold()
    if "simcard" in lowered:
        return 30.0
    if lowered.endswith("_pdf") or "_pdf" in lowered:
        return 28.0
    return DEFAULT_METHOD_FEE_PERCENT


def method_fee_percent(item: dict | None, *, key: str | None = None) -> float:
    parsed = _extract_fee_percent(item)
    if parsed is not None:
        return parsed
    return default_fee_percent_for_key(key or (item or {}).get("key"))


def estimate_payer_amount(
    base_amount,
    fee_percent: float | None = None,
    *,
    key: str | None = None,
    item: dict | None = None,
) -> int:
    try:
        base = float(base_amount or 0)
    except (TypeError, ValueError):
        return 0
    if base <= 0:
        return 0
    percent = fee_percent if fee_percent is not None else method_fee_percent(item, key=key)
    extra = base * (max(0.0, float(percent)) / 100.0) * CLIENT_FEE_SHARE
    return int(base + extra + 0.5)


def estimate_payer_amount_range(items: list[dict] | None, base_amount) -> tuple[int, int] | None:
    values: list[int] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        children = item.get("submethods") if isinstance(item.get("submethods"), list) else []
        if children:
            nested = estimate_payer_amount_range(children, base_amount)
            if nested:
                values.extend(nested)
            continue
        value = estimate_payer_amount(base_amount, item=item, key=str(item.get("key") or ""))
        if value > 0:
            values.append(value)
    if not values:
        return None
    return min(values), max(values)


def format_approx_amount_suffix(base_amount, item: dict | None = None) -> str:
    try:
        base = float(base_amount or 0)
    except (TypeError, ValueError):
        return ""
    if base <= 0:
        return ""
    children = item.get("submethods") if isinstance(item, dict) and isinstance(item.get("submethods"), list) else []
    if children:
        span = estimate_payer_amount_range(children, base)
        if not span:
            return ""
        low, high = span
        if low == high:
            return f" · ~{low} ₽"
        return f" · ~{low}-{high} ₽"
    value = estimate_payer_amount(base, item=item, key=str((item or {}).get("key") or ""))
    if value <= 0:
        return ""
    return f" · ~{value} ₽"


def _method_children(item: dict) -> list[dict]:
    for key in ("submethods", "sub_methods", "subMethods", "bank_codes", "banks", "children", "methods"):
        raw = item.get(key)
        if not isinstance(raw, list) or not raw:
            continue
        children = []
        for child in raw:
            if not isinstance(child, dict):
                continue
            child_key = str(child.get("key") or child.get("code") or "").strip()
            if not child_key:
                continue
            payload = dict(child)
            payload["key"] = child_key
            children.append(_copy_method(payload))
        if children:
            return children
    return []


METHOD_DISPLAY_NAMES = {
    "card": "Оплата картой",
    "card_pdf": "Карта - PDF",
    "nspk": "НСПК",
    "simcard": "Баланс телефона",
    "sbp": "Перевод по СБП",
    "sbp_pdf": "СБП - PDF",
    "sbp_sng": "СБП СНГ",
}

CARD_GROUP_KEYS = ("card", "card_pdf", "nspk")
SBP_GROUP_KEYS = ("sbp", "sbp_pdf", "sbp_sng")
PHONE_METHOD_KEY = "simcard"
MAIN_CRYPTO_ASSET_KEYS = {"btc", "ltc", "bitcoin", "litecoin"}

SUBMETHOD_DISPLAY_NAMES = {
    "sber": "Сбербанк",
    "qr_sber": "Сбербанк",
    "alfa": "Альфа-Банк",
    "qr_alfa": "Альфа-Банк",
    "vtb": "ВТБ",
    "qr_vtb": "ВТБ",
    "tbank": "Т-Банк",
    "tinkoff": "Т-Банк",
    "ozon": "Озон Банк",
    "qr_ozon": "Озон Банк",
    "gasprom": "Газпромбанк",
    "gazprom": "Газпромбанк",
    "qr_gasprom": "Газпромбанк",
    "psb": "ПСБ",
    "qr_smart": "Любой банк",
    "sngc_abh": "СНГ - перевод на карту",
}


def _copy_method(item: dict, *, name: str | None = None, submethods: list[dict] | None = None) -> dict:
    key = str(item.get("key") or item.get("code") or "").strip()
    display = name or METHOD_DISPLAY_NAMES.get(key) or SUBMETHOD_DISPLAY_NAMES.get(key) or str(item.get("name") or key)
    copied = {"key": key, "name": display}
    if not key.startswith("_group_"):
        copied["fee_percent"] = method_fee_percent(item, key=key)
    if submethods:
        copied["submethods"] = [
            _copy_method(child)
            for child in submethods
            if str(child.get("key") or child.get("code") or "").strip()
        ]
    return copied


def _methods_for_keys(by_key: dict[str, dict], keys: tuple[str, ...]) -> list[dict]:
    items = []
    for key in keys:
        item = by_key.get(key)
        if item:
            items.append(_copy_method(item))
    return items


def _as_group_or_leaf(group_key: str, group_name: str, items: list[dict]) -> dict | None:
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return {"key": group_key, "name": group_name, "submethods": items}


def filter_main_crypto_assets(assets: list[dict] | None) -> list[dict]:
    by_kind = {
        "btc": {"key": "btc", "name": "Bitcoin"},
        "ltc": {"key": "ltc", "name": "Litecoin"},
    }
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        key = str(asset.get("key") or "").strip()
        if not key:
            continue
        lowered = key.casefold()
        name = str(asset.get("name") or key)
        if lowered in {"btc", "bitcoin"} or "bitcoin" in name.casefold():
            by_kind["btc"] = {"key": key, "name": name}
        elif lowered in {"ltc", "litecoin"} or "litecoin" in name.casefold():
            by_kind["ltc"] = {"key": key, "name": name}
    return [by_kind["btc"], by_kind["ltc"]]


def group_payment_methods(methods: list[dict] | None) -> list[dict]:
    items = [item for item in (methods or []) if isinstance(item, dict) and str(item.get("key") or "").strip()]
    if any(str(item.get("key") or "").startswith("_group_") for item in items):
        return items

    by_key: dict[str, dict] = {}
    for item in items:
        key = str(item.get("key") or "").strip()
        children = item.get("submethods") if isinstance(item.get("submethods"), list) else _method_children(item)
        by_key[key] = _copy_method(item, submethods=children or None)

    reserved = set(CARD_GROUP_KEYS + SBP_GROUP_KEYS + (PHONE_METHOD_KEY,))
    used: set[str] = {key for key in reserved if key in by_key}
    sng_card_items: list[dict] = []
    qr_items: list[dict] = []
    intra_items: list[dict] = []
    for key, item in by_key.items():
        if key.startswith("sngc"):
            sng_card_items.append(_copy_method(item, name="СНГ - перевод на карту"))
            used.add(key)

    for key, item in by_key.items():
        if key in used:
            continue
        if key.startswith("qr_"):
            qr_items.append(_copy_method(item))
            used.add(key)
            continue
        children = item.get("submethods") or []
        if children:
            for child in children:
                child_key = str(child.get("key") or "").strip()
                if not child_key or child_key in used or child_key in reserved:
                    continue
                intra_items.append(_copy_method(child))
                used.add(child_key)
            used.add(key)
            continue
        intra_items.append(_copy_method(item))
        used.add(key)

    other_items: list[dict] = []
    if qr_items:
        other_items.append({"key": "_group_qr", "name": "QR-код", "submethods": qr_items})
    if intra_items:
        other_items.append({"key": "_group_intra", "name": "Внутрибанк", "submethods": intra_items})

    card_items = _methods_for_keys(by_key, CARD_GROUP_KEYS) + sng_card_items
    if intra_items:
        card_items.append({
            "key": "_group_card_banks",
            "name": "Выбрать банк и оплатить",
            "submethods": intra_items,
        })

    grouped: list[dict] = []
    card_group = _as_group_or_leaf("_group_card", "Карта", card_items)
    if card_group:
        grouped.append(card_group)
    sbp_group = _as_group_or_leaf("_group_sbp", "СБП", _methods_for_keys(by_key, SBP_GROUP_KEYS))
    if sbp_group:
        grouped.append(sbp_group)
    if PHONE_METHOD_KEY in by_key:
        grouped.append(_copy_method(by_key[PHONE_METHOD_KEY]))
    if other_items:
        grouped.append({"key": "_group_other", "name": "Другие способы", "submethods": other_items})

    return grouped or items


def iter_payment_method_nodes(methods: list[dict] | None):
    for item in methods or []:
        if not isinstance(item, dict):
            continue
        yield item
        for child in item.get("submethods") or []:
            if not isinstance(child, dict):
                continue
            yield child
            for grand in child.get("submethods") or []:
                if isinstance(grand, dict):
                    yield grand


def find_nested_group(grouped: list[dict] | None, nest_key: str) -> tuple[int | None, dict | None]:
    wanted = str(nest_key or "").strip()
    if not wanted:
        return None, None
    for index, item in enumerate(grouped or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("key") or "") == wanted:
            return index, item
        for child in item.get("submethods") or []:
            if isinstance(child, dict) and str(child.get("key") or "") == wanted:
                return index, child
    return None, None


def payment_id_of(payload: dict | None) -> str:
    data = payload or {}
    return str(data.get("id") or data.get("public_id") or "").strip()


def payment_status_of(payload: dict | None) -> str:
    data = payload or {}
    return str(data.get("status") or "").strip().lower()
