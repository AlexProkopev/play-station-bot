import html
import json
import os
import random
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from app.services.postgres_store import PostgresDocumentStore

SETTINGS_FILE = "storage/settings.json"
SETTINGS_DOC_KEY = "settings"
DEFAULT_SETTINGS = {
    "redirect_manual_to_auto": False,
    "ltc_rate_rub": None,
    "ltc_rate_updated_at": None,
    "btc_rate_rub": None,
    "crypto_rates_updated_at": None,
    "btc_wallet": None,
    "ltc_wallet": None,
    "btc_wallets": [],
    "ltc_wallets": [],
    "manual_payment_template": None,
    "manual_commission": 600,
    "rf_card_commission": 300,
    "yoomoney_commission": 0,
    "support_contact": "@egor_lavlav66",
    "contacts_text": None,
    "home_screen_text": None,
    "home_links": [],
    "featured_product": {
        "title": "",
        "description": "",
        "photo_file_id": None,
        "enabled": True,
    },
    "featured_products": {
        "enabled": True,
        "items": [],
    },
    "rf_cards": [],
    "yoomoney_wallets": [],
    "exchange_bot_url": None,
    # Кастомные названия способов оплаты: {"original_name": "custom_name"}
    "payment_method_aliases": {},
    # Альтернативные способы при отсутствии реквизитов: {"method_key": ["alt_key1", "alt_key2"]}
    "payment_fallback_methods": {},
    "auto_broadcast": {
        "enabled": False,
        "text": None,
        "media": None,
        "interval_minutes": 60,
        "last_sent_at": None,
        "next_send_at": None,
        "sending_slot": None,
        "slot_claims": {},
    },
    "auto_broadcasts": [],
    "purchase_streak": {
        "enabled": True,
        "required": 5,
        "percent": 100,
        "uses": 1,
    },
    "dispute_replies": [],
    "weekly_missions": {
        "enabled": True,
        "reward": 1000,
        "tasks": [
            {
                "id": "buy_once",
                "title": "Оформить заказ в Store",
                "kind": "purchases",
                "count": 1,
                "enabled": True,
                "reward": 0,
            },
            {
                "id": "invite",
                "title": "Привести друга в Store",
                "kind": "referrals",
                "count": 1,
                "enabled": True,
                "reward": 0,
            },
            {
                "id": "repeat",
                "title": "Повторить заказ в Store",
                "kind": "purchases",
                "count": 2,
                "enabled": True,
                "reward": 0,
            },
            {
                "id": "buy_three",
                "title": "Закрыть 3 заказа в Store",
                "kind": "purchases",
                "count": 3,
                "enabled": True,
                "reward": 0,
            },
        ],
    },
}

MISSION_KINDS = ("purchases", "referrals")
DEFAULT_PURCHASE_STREAK = deepcopy(DEFAULT_SETTINGS["purchase_streak"])
DEFAULT_WEEKLY_MISSIONS = deepcopy(DEFAULT_SETTINGS["weekly_missions"])

class SettingsService:
    _lock = threading.RLock()

    @staticmethod
    def _load_settings():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(SETTINGS_DOC_KEY, DEFAULT_SETTINGS)
            if not isinstance(data, dict):
                data = DEFAULT_SETTINGS.copy()
            for key, value in DEFAULT_SETTINGS.items():
                if key not in data:
                    data[key] = deepcopy(value)
            return data
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        
        if not os.path.exists(SETTINGS_FILE):
            return DEFAULT_SETTINGS.copy()
        
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key, value in DEFAULT_SETTINGS.items():
                    if key not in data:
                        data[key] = deepcopy(value)
                return data
        except Exception:
            return DEFAULT_SETTINGS.copy()

    @staticmethod
    def _save_settings(settings: dict):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(SETTINGS_DOC_KEY, settings)
            return
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")

    @staticmethod
    def is_redirect_active() -> bool:
        settings = SettingsService._load_settings()
        return settings.get("redirect_manual_to_auto", False)

    @staticmethod
    def set_redirect_active(state: bool):
        settings = SettingsService._load_settings()
        settings["redirect_manual_to_auto"] = state
        SettingsService._save_settings(settings)

    @staticmethod
    def get_ltc_rate() -> tuple[float | None, str | None]:
        btc, ltc, updated_at = SettingsService.get_crypto_rates()
        return ltc, updated_at

    @staticmethod
    def set_ltc_rate(rate: float | None):
        btc, _ltc, _updated_at = SettingsService.get_crypto_rates()
        SettingsService.set_crypto_rates(btc, rate)

    @staticmethod
    def get_crypto_rates() -> tuple[float | None, float | None, str | None]:
        settings = SettingsService._load_settings()
        updated_at = settings.get("crypto_rates_updated_at") or settings.get("ltc_rate_updated_at")
        return (
            SettingsService._positive_rate(settings.get("btc_rate_rub")),
            SettingsService._positive_rate(settings.get("ltc_rate_rub")),
            updated_at,
        )

    @staticmethod
    def set_crypto_rates(btc_rate: float | None, ltc_rate: float | None):
        settings = SettingsService._load_settings()
        settings["btc_rate_rub"] = SettingsService._positive_rate(btc_rate)
        settings["ltc_rate_rub"] = SettingsService._positive_rate(ltc_rate)
        if settings["btc_rate_rub"] or settings["ltc_rate_rub"]:
            stamp = datetime.now(timezone.utc).isoformat()
            settings["crypto_rates_updated_at"] = stamp
            settings["ltc_rate_updated_at"] = stamp
        else:
            settings["crypto_rates_updated_at"] = None
            settings["ltc_rate_updated_at"] = None
        SettingsService._save_settings(settings)

    @staticmethod
    def get_btc_wallets() -> list[str]:
        settings = SettingsService._load_settings()
        wallets = settings.get("btc_wallets")
        if not isinstance(wallets, list):
            wallets = []
        cleaned = [str(item).strip() for item in wallets if str(item).strip()]
        legacy = settings.get("btc_wallet")
        if legacy and str(legacy).strip() and str(legacy).strip() not in cleaned:
            cleaned.append(str(legacy).strip())
        return cleaned

    @staticmethod
    def get_ltc_wallets() -> list[str]:
        settings = SettingsService._load_settings()
        wallets = settings.get("ltc_wallets")
        if not isinstance(wallets, list):
            wallets = []
        cleaned = [str(item).strip() for item in wallets if str(item).strip()]
        legacy = settings.get("ltc_wallet")
        if legacy and str(legacy).strip() and str(legacy).strip() not in cleaned:
            cleaned.append(str(legacy).strip())
        return cleaned

    @staticmethod
    def set_btc_wallets(wallets: list[str]) -> None:
        settings = SettingsService._load_settings()
        cleaned = [str(item).strip() for item in wallets if str(item).strip()]
        settings["btc_wallets"] = cleaned
        settings["btc_wallet"] = cleaned[0] if len(cleaned) == 1 else None
        SettingsService._save_settings(settings)

    @staticmethod
    def set_ltc_wallets(wallets: list[str]) -> None:
        settings = SettingsService._load_settings()
        cleaned = [str(item).strip() for item in wallets if str(item).strip()]
        settings["ltc_wallets"] = cleaned
        settings["ltc_wallet"] = cleaned[0] if len(cleaned) == 1 else None
        SettingsService._save_settings(settings)

    @staticmethod
    def add_btc_wallet(wallet: str) -> bool:
        value = str(wallet or "").strip()
        if not value:
            return False
        wallets = SettingsService.get_btc_wallets()
        if value in wallets:
            return True
        wallets.append(value)
        SettingsService.set_btc_wallets(wallets)
        return True

    @staticmethod
    def add_ltc_wallet(wallet: str) -> bool:
        value = str(wallet or "").strip()
        if not value:
            return False
        wallets = SettingsService.get_ltc_wallets()
        if value in wallets:
            return True
        wallets.append(value)
        SettingsService.set_ltc_wallets(wallets)
        return True

    @staticmethod
    def remove_btc_wallet_at(index: int) -> str | None:
        wallets = SettingsService.get_btc_wallets()
        if index < 0 or index >= len(wallets):
            return None
        removed = wallets.pop(index)
        SettingsService.set_btc_wallets(wallets)
        return removed

    @staticmethod
    def remove_ltc_wallet_at(index: int) -> str | None:
        wallets = SettingsService.get_ltc_wallets()
        if index < 0 or index >= len(wallets):
            return None
        removed = wallets.pop(index)
        SettingsService.set_ltc_wallets(wallets)
        return removed

    @staticmethod
    def pick_btc_wallet() -> str | None:
        wallets = SettingsService.get_btc_wallets()
        if not wallets:
            return None
        if len(wallets) == 1:
            return wallets[0]
        return wallets[random.randint(0, len(wallets) - 1)]

    @staticmethod
    def pick_ltc_wallet() -> str | None:
        wallets = SettingsService.get_ltc_wallets()
        if not wallets:
            return None
        if len(wallets) == 1:
            return wallets[0]
        return wallets[random.randint(0, len(wallets) - 1)]

    @staticmethod
    def get_btc_wallet() -> str:
        return SettingsService.pick_btc_wallet() or ""

    @staticmethod
    def set_btc_wallet(wallet: str | None):
        cleaned = str(wallet).strip() if wallet else ""
        if cleaned:
            SettingsService.set_btc_wallets([cleaned])
        else:
            SettingsService.set_btc_wallets([])

    @staticmethod
    def get_ltc_wallet() -> str:
        return SettingsService.pick_ltc_wallet() or ""

    @staticmethod
    def set_ltc_wallet(wallet: str | None):
        cleaned = str(wallet).strip() if wallet else ""
        if cleaned:
            SettingsService.set_ltc_wallets([cleaned])
        else:
            SettingsService.set_ltc_wallets([])

    @staticmethod
    def _positive_rate(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        return number

    @staticmethod
    def get_manual_payment_template() -> str | None:
        settings = SettingsService._load_settings()
        template = settings.get("manual_payment_template")
        if isinstance(template, str) and template.strip():
            return template
        return None

    @staticmethod
    def set_manual_payment_template(template: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(template, str):
            cleaned = template.strip() or None
        settings["manual_payment_template"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_manual_commission() -> int:
        settings = SettingsService._load_settings()
        val = settings.get("manual_commission", 600)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 600

    @staticmethod
    def set_manual_commission(value: int):
        settings = SettingsService._load_settings()
        settings["manual_commission"] = int(value)
        SettingsService._save_settings(settings)

    @staticmethod
    def get_rf_card_commission() -> int:
        settings = SettingsService._load_settings()
        val = settings.get("rf_card_commission", 300)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 300

    @staticmethod
    def set_rf_card_commission(value: int):
        settings = SettingsService._load_settings()
        settings["rf_card_commission"] = int(value)
        SettingsService._save_settings(settings)

    @staticmethod
    def get_yoomoney_commission() -> int:
        settings = SettingsService._load_settings()
        val = settings.get("yoomoney_commission", 0)
        try:
            return max(0, int(val))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def set_yoomoney_commission(value: int):
        settings = SettingsService._load_settings()
        settings["yoomoney_commission"] = max(0, int(value))
        SettingsService._save_settings(settings)

    @staticmethod
    def get_diamond_site_url() -> str:
        settings = SettingsService._load_settings()
        value = settings.get("diamond_site_url")
        return str(value).strip() if isinstance(value, str) else ""

    @staticmethod
    def set_diamond_site_url(url: str) -> None:
        settings = SettingsService._load_settings()
        settings["diamond_site_url"] = str(url or "").strip()
        SettingsService._save_settings(settings)

    @staticmethod
    def get_support_contact() -> str:
        settings = SettingsService._load_settings()
        val = settings.get("support_contact", "@egor_lavlav66")
        return str(val).strip() if val else "@egor_lavlav66"

    @staticmethod
    def set_support_contact(contact: str):
        settings = SettingsService._load_settings()
        settings["support_contact"] = contact.strip()
        SettingsService._save_settings(settings)

    @staticmethod
    def get_home_screen_text() -> str:
        settings = SettingsService._load_settings()
        value = settings.get("home_screen_text")
        if isinstance(value, str) and value.strip():
            return value.strip()
        from app.config import SHOP_NAME
        return (
            f"🔵 <b>{html.escape(SHOP_NAME)}</b>\n"
            "Витрина открыта.\n\n"
            "🛒 «Купить» — город и позиции.\n"
            "💳 Кошелек и 📚 история заказов - кнопки внизу.\n"
            "🔵 «На главную» возвращает на этот экран."
        )

    @staticmethod
    def set_home_screen_text(text: str) -> None:
        settings = SettingsService._load_settings()
        settings["home_screen_text"] = str(text or "").strip()
        SettingsService._save_settings(settings)

    @staticmethod
    def _clean_home_url(value: str) -> str:
        url = " ".join(str(value or "").strip().split())
        if url.startswith("@"):
            url = f"https://t.me/{url[1:]}"
        elif url.lower().startswith("t.me/"):
            url = "https://" + url
        if not url.startswith(("http://", "https://", "tg://")):
            return ""
        return url

    @staticmethod
    def get_home_links() -> list[dict]:
        settings = SettingsService._load_settings()
        raw = settings.get("home_links")
        if not isinstance(raw, list):
            return []
        links = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())
            url = SettingsService._clean_home_url(str(item.get("url") or ""))
            link_id = str(item.get("id") or "").strip()
            if not title or not url or not link_id:
                continue
            links.append({"id": link_id, "title": title[:60], "url": url})
        return links

    @staticmethod
    def _write_home_links(links: list[dict]) -> None:
        settings = SettingsService._load_settings()
        settings["home_links"] = links
        SettingsService._save_settings(settings)

    @staticmethod
    def add_home_link(title: str, url: str) -> tuple[bool, str]:
        clean_title = " ".join(str(title or "").split())
        clean_url = SettingsService._clean_home_url(url)
        if not clean_title:
            return False, "Имя кнопки пустое."
        if not clean_url:
            return False, "Нужна ссылка: https://..., t.me/... или @username."
        links = SettingsService.get_home_links()
        links.append({"id": uuid.uuid4().hex[:8], "title": clean_title[:60], "url": clean_url})
        SettingsService._write_home_links(links)
        return True, "Кнопка сохранена."

    @staticmethod
    def update_home_link(link_id: str, *, title: str | None = None, url: str | None = None) -> tuple[bool, str]:
        links = SettingsService.get_home_links()
        for item in links:
            if item["id"] != str(link_id):
                continue
            if title is not None:
                clean_title = " ".join(str(title).split())
                if not clean_title:
                    return False, "Имя кнопки пустое."
                item["title"] = clean_title[:60]
            if url is not None:
                clean_url = SettingsService._clean_home_url(url)
                if not clean_url:
                    return False, "Нужна ссылка: https://..., t.me/... или @username."
                item["url"] = clean_url
            SettingsService._write_home_links(links)
            return True, "Кнопка обновлена."
        return False, "Кнопка не найдена."

    @staticmethod
    def delete_home_link(link_id: str) -> bool:
        links = SettingsService.get_home_links()
        kept = [item for item in links if item["id"] != str(link_id)]
        if len(kept) == len(links):
            return False
        SettingsService._write_home_links(kept)
        return True

    @staticmethod
    def get_contacts_text() -> str:
        settings = SettingsService._load_settings()
        value = settings.get("contacts_text")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return (
            "📡 <b>Связь со Store</b>\n\n"
            f"Вопросы по заказам: {SettingsService.get_support_contact()}"
        )

    @staticmethod
    def normalize_featured_item(raw, item_id: str | None = None) -> dict:
        data = raw if isinstance(raw, dict) else {}
        uid = str(data.get("id") or item_id or uuid.uuid4().hex[:8])
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        photo = str(data.get("photo_file_id") or "").strip() or None
        return {
            "id": uid,
            "title": title,
            "description": description,
            "photo_file_id": photo,
        }

    @staticmethod
    def _legacy_featured_item(raw) -> dict | None:
        data = raw if isinstance(raw, dict) else {}
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        photo = str(data.get("photo_file_id") or "").strip() or None
        if not (title or description or photo):
            return None
        return SettingsService.normalize_featured_item(
            {
                "id": "legacy",
                "title": title,
                "description": description,
                "photo_file_id": photo,
            }
        )

    @staticmethod
    def get_featured_catalog() -> dict:
        settings = SettingsService._load_settings()
        raw = settings.get("featured_products")
        catalog = raw if isinstance(raw, dict) else {}
        items_raw = catalog.get("items")
        items = []
        if isinstance(items_raw, list):
            for item in items_raw:
                if isinstance(item, dict):
                    items.append(SettingsService.normalize_featured_item(item))
        if not items:
            legacy = SettingsService._legacy_featured_item(settings.get("featured_product"))
            if legacy:
                items = [legacy]
                enabled = bool((settings.get("featured_product") or {}).get("enabled", True))
                settings["featured_products"] = {"enabled": enabled, "items": items}
                SettingsService._save_settings(settings)
                return {"enabled": enabled, "items": items}
        enabled = bool(catalog.get("enabled", True))
        return {"enabled": enabled, "items": items}

    @staticmethod
    def save_featured_catalog(*, enabled: bool | None = None, items: list | None = None) -> dict:
        current = SettingsService.get_featured_catalog()
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if items is not None:
            current["items"] = [
                SettingsService.normalize_featured_item(item) for item in items if isinstance(item, dict)
            ]
        settings = SettingsService._load_settings()
        settings["featured_products"] = current
        SettingsService._save_settings(settings)
        return current

    @staticmethod
    def list_featured_products() -> list[dict]:
        return list(SettingsService.get_featured_catalog().get("items") or [])

    @staticmethod
    def get_featured_item(item_id: str) -> dict | None:
        target = str(item_id or "")
        for item in SettingsService.list_featured_products():
            if str(item.get("id")) == target:
                return item
        return None

    @staticmethod
    def add_featured_item(title: str, description: str = "", photo_file_id: str | None = None) -> dict:
        item = SettingsService.normalize_featured_item(
            {
                "title": title,
                "description": description,
                "photo_file_id": photo_file_id,
            }
        )
        items = SettingsService.list_featured_products()
        items.append(item)
        SettingsService.save_featured_catalog(items=items)
        return item

    @staticmethod
    def update_featured_item(item_id: str, **changes) -> dict | None:
        items = SettingsService.list_featured_products()
        updated = None
        for item in items:
            if str(item.get("id")) != str(item_id):
                continue
            if "title" in changes:
                item["title"] = str(changes.get("title") or "").strip()
            if "description" in changes:
                item["description"] = str(changes.get("description") or "").strip()
            if "photo_file_id" in changes:
                photo = changes.get("photo_file_id")
                item["photo_file_id"] = str(photo).strip() if photo else None
            updated = item
            break
        if updated is None:
            return None
        SettingsService.save_featured_catalog(items=items)
        return updated

    @staticmethod
    def delete_featured_item(item_id: str) -> bool:
        items = SettingsService.list_featured_products()
        kept = [item for item in items if str(item.get("id")) != str(item_id)]
        if len(kept) == len(items):
            return False
        SettingsService.save_featured_catalog(items=kept)
        return True

    @staticmethod
    def is_price_menu_visible(catalog: dict | None = None) -> bool:
        data = catalog or SettingsService.get_featured_catalog()
        if not data.get("enabled", True):
            return False
        return bool(data.get("items"))

    @staticmethod
    def featured_item_text(item: dict | None) -> str:
        data = item if isinstance(item, dict) else {}
        title = html.escape(str(data.get("title") or "").strip() or "Товар")
        description = html.escape(str(data.get("description") or "").strip())
        lines = [f"🎮 <b>{title}</b>"]
        if description:
            lines.append(description)
        return "\n".join(lines)

    @staticmethod
    def set_contacts_text(text: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(text, str):
            cleaned = text.strip() or None
        settings["contacts_text"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_preorder_text() -> str:
        settings = SettingsService._load_settings()
        value = settings.get("preorder_text")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return (
            "📦 <b>Отдельный заказ Store</b>\n\n"
            "Напишите в поддержку позицию, объем и город доставки.\n"
            "Куратор согласует срок и сумму."
        )

    @staticmethod
    def set_preorder_text(text: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(text, str):
            cleaned = text.strip() or None
        settings["preorder_text"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_rf_cards() -> list[str]:
        settings = SettingsService._load_settings()
        cards = settings.get("rf_cards", [])
        return [str(item).strip() for item in cards if str(item).strip()]

    @staticmethod
    def set_rf_cards(cards: list[str]):
        settings = SettingsService._load_settings()
        cleaned = [str(item).strip() for item in cards if str(item).strip()]
        settings["rf_cards"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def remove_rf_card_at(index: int) -> str | None:
        cards = SettingsService.get_rf_cards()
        if index < 0 or index >= len(cards):
            return None
        removed = cards.pop(index)
        SettingsService.set_rf_cards(cards)
        return removed

    @staticmethod
    def get_yoomoney_wallets() -> list[str]:
        settings = SettingsService._load_settings()
        wallets = settings.get("yoomoney_wallets", [])
        return [str(item).strip() for item in wallets if str(item).strip()]

    @staticmethod
    def set_yoomoney_wallets(wallets: list[str]):
        settings = SettingsService._load_settings()
        cleaned = [str(item).strip() for item in wallets if str(item).strip()]
        settings["yoomoney_wallets"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def normalize_yoomoney_wallet(value: str) -> str | None:
        text = (value or "").strip()
        if not text:
            return None
        if "@" in text:
            email = text.lower()
            if re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email):
                return email
            return None
        digits = re.sub(r"\D", "", text)
        if 11 <= len(digits) <= 16:
            return digits
        return None

    @staticmethod
    def remove_yoomoney_wallet_at(index: int) -> str | None:
        wallets = SettingsService.get_yoomoney_wallets()
        if index < 0 or index >= len(wallets):
            return None
        removed = wallets.pop(index)
        SettingsService.set_yoomoney_wallets(wallets)
        return removed

    @staticmethod
    def get_exchange_bot_url() -> str | None:
        settings = SettingsService._load_settings()
        value = settings.get("exchange_bot_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def set_exchange_bot_url(url: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(url, str):
            cleaned = url.strip() or None
        settings["exchange_bot_url"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_random_rf_card() -> str | None:
        cards = SettingsService.get_rf_cards()
        if not cards:
            return None
        return cards[0] if len(cards) == 1 else cards[random.randint(0, len(cards) - 1)]

    @staticmethod
    def parse_interval_minutes(raw: str) -> int | None:
        text = (raw or "").strip().lower().replace(" ", "")
        match = re.fullmatch(r"(\d+)(м|мин|минута|минуты|минут|min|m|ч|час|часа|часов|h|hour|hours)?", text)
        if not match:
            return None
        value = int(match.group(1))
        unit = match.group(2) or "м"
        if unit in {"ч", "час", "часа", "часов", "h", "hour", "hours"}:
            minutes = value * 60
        else:
            minutes = value
        if minutes < 1 or minutes > 7 * 24 * 60:
            return None
        return minutes

    @staticmethod
    def format_interval_minutes(minutes: int) -> str:
        hours, rest = divmod(int(minutes), 60)
        if hours and rest:
            return f"{hours} ч {rest} мин"
        if hours:
            return f"{hours} ч"
        return f"{rest} мин"

    BROADCAST_TIME_FMT = "%Y-%m-%d %H:%M:%S"

    @staticmethod
    def parse_broadcast_time(raw) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.strptime(str(raw), SettingsService.BROADCAST_TIME_FMT)
        except ValueError:
            return None

    @staticmethod
    def format_broadcast_time(value: datetime) -> str:
        return value.replace(microsecond=0).strftime(SettingsService.BROADCAST_TIME_FMT)

    @staticmethod
    def interval_timedelta(minutes) -> timedelta:
        try:
            value = int(minutes or 60)
        except (TypeError, ValueError):
            value = 60
        return timedelta(minutes=max(1, value))

    @staticmethod
    def next_broadcast_slot(due_at: datetime, interval: timedelta, now: datetime) -> datetime:
        """Keep the original cadence after a late/restarted send."""
        seconds = max(60, int(interval.total_seconds()))
        step = timedelta(seconds=seconds)
        nxt = due_at + step
        if nxt > now:
            return nxt
        elapsed = (now - due_at).total_seconds()
        steps = int(elapsed // seconds) + 1
        nxt = due_at + timedelta(seconds=steps * seconds)
        if nxt <= now:
            nxt += step
        return nxt

    @staticmethod
    def resolve_next_send_at(
        *,
        now: datetime,
        interval: timedelta,
        last_sent_at: datetime | None = None,
        next_send_at: datetime | None = None,
    ) -> datetime:
        if next_send_at and next_send_at > now:
            return next_send_at
        if last_sent_at:
            nxt = last_sent_at + interval
            if nxt > now:
                return nxt
            return SettingsService.next_broadcast_slot(last_sent_at, interval, now)
        return now + interval

    MAX_AUTO_BROADCASTS = 8
    _WORKER_DEFAULTS = {
        "id": "default",
        "enabled": False,
        "text": None,
        "media": None,
        "interval_minutes": 60,
        "last_sent_at": None,
        "next_send_at": None,
        "sending_slot": None,
        "slot_claims": {},
    }
    MEDIA_ONLY_CLAIM = "*"

    @classmethod
    def _mutate_settings(cls, mutator):
        with cls._lock:
            if PostgresDocumentStore.is_enabled():
                return PostgresDocumentStore.mutate_document(
                    SETTINGS_DOC_KEY,
                    mutator,
                    default=DEFAULT_SETTINGS,
                )
            settings = cls._load_settings()
            mutator(settings)
            cls._save_settings(settings)
            return settings

    @classmethod
    def normalize_broadcast_worker(cls, data, default_id: str = "default") -> dict:
        merged = deepcopy(cls._WORKER_DEFAULTS)
        if isinstance(data, dict):
            merged.update(data)
        merged["id"] = str(merged.get("id") or default_id)
        try:
            merged["interval_minutes"] = int(merged.get("interval_minutes") or 60)
        except (TypeError, ValueError):
            merged["interval_minutes"] = 60
        merged["enabled"] = bool(merged.get("enabled"))
        text = merged.get("text")
        merged["text"] = text.strip() if isinstance(text, str) and text.strip() else None
        merged["media"] = cls.normalize_broadcast_media(merged.get("media"))
        if not isinstance(merged.get("slot_claims"), dict):
            merged["slot_claims"] = {}
        return merged

    @staticmethod
    def normalize_broadcast_media(media) -> dict | None:
        if not isinstance(media, dict):
            return None
        kind = str(media.get("kind") or "").strip().lower()
        if not kind:
            return None
        try:
            count = int(media.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        filename = media.get("filename")
        types = media.get("types")
        payload = {
            "kind": kind,
            "filename": str(filename)[:200] if filename else None,
            "count": max(1, count),
        }
        if isinstance(types, list):
            payload["types"] = [str(item) for item in types if item][:12]
        return payload

    @classmethod
    def worker_has_payload(cls, ab: dict) -> bool:
        if not isinstance(ab, dict):
            return False
        text = ab.get("text")
        if isinstance(text, str) and text.strip():
            return True
        media = ab.get("media")
        return isinstance(media, dict) and bool(media.get("kind"))

    @classmethod
    def _workers_from_settings(cls, settings: dict) -> list[dict]:
        items = settings.get("auto_broadcasts") if isinstance(settings, dict) else None
        workers = []
        if isinstance(items, list):
            workers = [
                cls.normalize_broadcast_worker(item, str(item.get("id") or "default"))
                for item in items
                if isinstance(item, dict)
            ]
        if not workers:
            legacy = settings.get("auto_broadcast") if isinstance(settings, dict) else None
            if isinstance(legacy, dict) and (
                legacy.get("text")
                or legacy.get("media")
                or legacy.get("enabled")
                or legacy.get("next_send_at")
                or legacy.get("last_sent_at")
            ):
                workers = [cls.normalize_broadcast_worker(legacy, "default")]
        if isinstance(settings, dict):
            settings["auto_broadcasts"] = workers
        return workers

    @classmethod
    def list_auto_broadcasts(cls) -> list[dict]:
        settings = cls._load_settings()
        return deepcopy(cls._workers_from_settings(settings))

    @classmethod
    def get_auto_broadcast(cls, worker_id: str | None = None) -> dict:
        workers = cls.list_auto_broadcasts()
        if worker_id:
            for item in workers:
                if item.get("id") == str(worker_id):
                    return item
            return cls.normalize_broadcast_worker({}, str(worker_id))
        for item in workers:
            if item.get("id") == "default":
                return item
        if workers:
            return workers[0]
        return cls.normalize_broadcast_worker({}, "default")

    @classmethod
    def save_auto_broadcast(cls, payload: dict, worker_id: str | None = None) -> dict:
        wid = str(worker_id or (payload or {}).get("id") or "default")
        box = {"worker": None}

        def mutate(settings: dict):
            workers = cls._workers_from_settings(settings)
            updated = None
            for index, item in enumerate(workers):
                if item.get("id") == wid:
                    item.update(payload or {})
                    workers[index] = cls.normalize_broadcast_worker(item, wid)
                    updated = workers[index]
                    break
            if updated is None:
                created = cls.normalize_broadcast_worker(payload or {}, wid)
                created["id"] = wid
                workers.append(created)
                updated = created
            settings["auto_broadcasts"] = workers
            settings["auto_broadcast"] = next(
                (item for item in workers if item.get("id") == "default"),
                workers[0] if workers else cls.normalize_broadcast_worker({}, "default"),
            )
            box["worker"] = deepcopy(updated)

        cls._mutate_settings(mutate)
        if payload and "media" in payload and not payload.get("media"):
            from app.services.broadcast_media import BroadcastMediaStore

            BroadcastMediaStore.delete(wid)
        return box["worker"] or cls.normalize_broadcast_worker(payload or {}, wid)

    @classmethod
    def create_auto_broadcast(cls) -> dict:
        worker_id = uuid.uuid4().hex[:8]
        if len(cls.list_auto_broadcasts()) >= cls.MAX_AUTO_BROADCASTS:
            raise ValueError("too many auto broadcasts")
        return cls.save_auto_broadcast({"id": worker_id, "enabled": False}, worker_id=worker_id)

    @classmethod
    def delete_auto_broadcast(cls, worker_id: str) -> bool:
        wid = str(worker_id)
        box = {"deleted": False}

        def mutate(settings: dict):
            workers = cls._workers_from_settings(settings)
            kept = [item for item in workers if item.get("id") != wid]
            box["deleted"] = len(kept) != len(workers)
            settings["auto_broadcasts"] = kept
            settings["auto_broadcast"] = next(
                (item for item in kept if item.get("id") == "default"),
                kept[0] if kept else deepcopy(cls._WORKER_DEFAULTS),
            )

        cls._mutate_settings(mutate)
        if box["deleted"]:
            from app.services.broadcast_media import BroadcastMediaStore

            BroadcastMediaStore.delete(wid)
        return box["deleted"]

    @classmethod
    def _apply_broadcast_claim(cls, ab: dict, bot_key: str, now: datetime, *, open_now: bool) -> tuple[str | None, bool]:
        if not ab.get("enabled") and not open_now:
            sending = cls.parse_broadcast_time(ab.get("sending_slot"))
            if sending is None:
                return None, False
        if not cls.worker_has_payload(ab):
            return None, False
        interval = cls.interval_timedelta(ab.get("interval_minutes"))
        next_at = cls.parse_broadcast_time(ab.get("next_send_at"))
        sending = cls.parse_broadcast_time(ab.get("sending_slot"))

        if open_now:
            sending = now.replace(microsecond=0)
            ab["sending_slot"] = cls.format_broadcast_time(sending)
            ab["next_send_at"] = cls.format_broadcast_time(now + interval)
            ab["last_sent_at"] = cls.format_broadcast_time(now)
            claims = ab.get("slot_claims") if isinstance(ab.get("slot_claims"), dict) else {}
            claims[cls.format_broadcast_time(sending)] = [bot_key]
            ab["slot_claims"] = cls._prune_slot_claims(claims)
            return cls._claim_token(ab), True

        if ab.get("enabled"):
            if next_at is None:
                next_at = now.replace(microsecond=0)
                ab["next_send_at"] = cls.format_broadcast_time(next_at)

            if sending is not None and next_at is not None and now >= next_at:
                sending = next_at.replace(microsecond=0)
                ab["sending_slot"] = cls.format_broadcast_time(sending)
                ab["next_send_at"] = cls.format_broadcast_time(
                    cls.next_broadcast_slot(sending, interval, now)
                )
                ab["last_sent_at"] = cls.format_broadcast_time(now)
            elif sending is None and now >= next_at:
                sending = next_at.replace(microsecond=0)
                ab["sending_slot"] = cls.format_broadcast_time(sending)
                ab["next_send_at"] = cls.format_broadcast_time(
                    cls.next_broadcast_slot(sending, interval, now)
                )
                ab["last_sent_at"] = cls.format_broadcast_time(now)

        if sending is None:
            return None, False

        slot_key = cls.format_broadcast_time(sending)
        claims = ab.get("slot_claims") if isinstance(ab.get("slot_claims"), dict) else {}
        claimed = [str(item) for item in (claims.get(slot_key) or [])]
        if bot_key in claimed:
            return None, False
        is_first = not claimed
        claimed.append(bot_key)
        claims[slot_key] = claimed
        ab["slot_claims"] = cls._prune_slot_claims(claims)
        return cls._claim_token(ab), is_first

    @classmethod
    def _claim_token(cls, ab: dict) -> str:
        text = ab.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return cls.MEDIA_ONLY_CLAIM

    @classmethod
    def claim_auto_broadcast_send(
        cls,
        bot_id,
        now: datetime,
        *,
        open_now: bool = False,
        worker_id: str | None = None,
    ) -> tuple[str | None, bool]:
        """Let this bot send once for the current slot. Other bots keep the same slot."""
        box = {"text": None, "first": False}
        bot_key = str(bot_id)
        target_id = str(worker_id or "default")

        def mutate(settings: dict):
            if not isinstance(settings, dict):
                return
            workers = cls._workers_from_settings(settings)
            worker = None
            for item in workers:
                if item.get("id") == target_id:
                    worker = item
                    break
            if worker is None and target_id == "default":
                worker = cls.normalize_broadcast_worker(settings.get("auto_broadcast"), "default")
                workers.append(worker)
            if worker is None:
                return
            text, first = cls._apply_broadcast_claim(worker, bot_key, now, open_now=open_now)
            settings["auto_broadcasts"] = workers
            settings["auto_broadcast"] = next(
                (item for item in workers if item.get("id") == "default"),
                workers[0],
            )
            box["text"] = text
            box["first"] = first

        cls._mutate_settings(mutate)
        return box["text"], box["first"]

    @classmethod
    def claim_due_auto_broadcasts(cls, bot_id, now: datetime) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        bot_key = str(bot_id)

        def mutate(settings: dict):
            if not isinstance(settings, dict):
                return
            workers = cls._workers_from_settings(settings)
            for worker in workers:
                text, first = cls._apply_broadcast_claim(worker, bot_key, now, open_now=False)
                if text:
                    results.append((text, first, str(worker.get("id") or "default")))
            settings["auto_broadcasts"] = workers
            if workers:
                settings["auto_broadcast"] = next(
                    (item for item in workers if item.get("id") == "default"),
                    workers[0],
                )

        cls._mutate_settings(mutate)
        return results

    @staticmethod
    def _prune_slot_claims(claims: dict) -> dict:
        if len(claims) <= 12:
            return claims
        keys = sorted(str(key) for key in claims)
        keep = set(keys[-8:])
        return {key: claims[key] for key in claims if str(key) in keep}

    # ─── Кастомные названия способов оплаты ───

    @classmethod
    def get_payment_method_aliases(cls) -> dict[str, str]:
        """Возвращает словарь переименований: {"original": "custom_name"}"""
        settings = cls._load_settings()
        aliases = settings.get("payment_method_aliases")
        return aliases if isinstance(aliases, dict) else {}

    @classmethod
    def set_payment_method_alias(cls, original_name: str, custom_name: str) -> None:
        """Задать кастомное название для способа оплаты."""
        def mutate(settings: dict):
            aliases = settings.get("payment_method_aliases")
            if not isinstance(aliases, dict):
                aliases = {}
            aliases[str(original_name).strip()] = str(custom_name).strip()
            settings["payment_method_aliases"] = aliases
        cls._mutate_settings(mutate)

    @classmethod
    def remove_payment_method_alias(cls, original_name: str) -> None:
        """Удалить кастомное название."""
        def mutate(settings: dict):
            aliases = settings.get("payment_method_aliases")
            if isinstance(aliases, dict) and original_name in aliases:
                del aliases[original_name]
                settings["payment_method_aliases"] = aliases
        cls._mutate_settings(mutate)

    @classmethod
    def get_aliased_method_name(cls, original_name: str) -> str:
        """Получить отображаемое имя (кастомное или оригинальное)."""
        aliases = cls.get_payment_method_aliases()
        return aliases.get(str(original_name).strip(), original_name)

    # ─── Альтернативные способы при отсутствии реквизитов ───

    @classmethod
    def get_payment_fallback_methods(cls) -> dict[str, list[str]]:
        """Возвращает словарь: {"method_key": ["alt1", "alt2"]}"""
        settings = cls._load_settings()
        fallbacks = settings.get("payment_fallback_methods")
        return fallbacks if isinstance(fallbacks, dict) else {}

    @classmethod
    def set_payment_fallback_methods(cls, method_key: str, alternatives: list[str]) -> None:
        """Задать альтернативные способы для метода."""
        def mutate(settings: dict):
            fallbacks = settings.get("payment_fallback_methods")
            if not isinstance(fallbacks, dict):
                fallbacks = {}
            fallbacks[str(method_key).strip()] = [str(alt).strip() for alt in alternatives]
            settings["payment_fallback_methods"] = fallbacks
        cls._mutate_settings(mutate)

    @classmethod
    def get_fallback_for_method(cls, method_key: str) -> list[str]:
        """Получить список альтернативных ключей для метода."""
        fallbacks = cls.get_payment_fallback_methods()
        return fallbacks.get(str(method_key).strip(), [])

    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @classmethod
    def normalize_purchase_streak(cls, raw) -> dict:
        data = deepcopy(DEFAULT_PURCHASE_STREAK)
        if isinstance(raw, dict):
            data["enabled"] = bool(raw.get("enabled", True))
            data["required"] = max(2, min(20, cls._as_int(raw.get("required"), 5)))
            data["percent"] = max(1, min(100, cls._as_int(raw.get("percent"), 100)))
            data["uses"] = max(1, min(10, cls._as_int(raw.get("uses"), 1)))
        return data

    @classmethod
    def get_purchase_streak(cls) -> dict:
        settings = cls._load_settings()
        return cls.normalize_purchase_streak(settings.get("purchase_streak"))

    @classmethod
    def save_purchase_streak(cls, payload: dict) -> dict:
        normalized = cls.normalize_purchase_streak(payload)

        def mutate(settings: dict):
            settings["purchase_streak"] = normalized

        cls._mutate_settings(mutate)
        return normalized

    @classmethod
    def update_purchase_streak(cls, **changes) -> dict:
        current = cls.get_purchase_streak()
        current.update(changes)
        return cls.save_purchase_streak(current)

    @classmethod
    def normalize_mission_task(cls, raw, fallback_id: str | None = None) -> dict | None:
        if not isinstance(raw, dict):
            return None
        task_id = str(raw.get("id") or fallback_id or "").strip()
        title = str(raw.get("title") or "").strip()
        if not task_id or not title:
            return None
        kind = str(raw.get("kind") or "purchases").strip().lower()
        if kind not in MISSION_KINDS:
            kind = "purchases"
        enabled = raw.get("enabled")
        return {
            "id": task_id[:32],
            "title": title[:80],
            "kind": kind,
            "count": max(1, min(50, cls._as_int(raw.get("count"), 1))),
            "enabled": True if enabled is None else bool(enabled),
            "reward": max(0, min(1_000_000, cls._as_int(raw.get("reward"), 0))),
        }

    @classmethod
    def normalize_weekly_missions(cls, raw) -> dict:
        data = deepcopy(DEFAULT_WEEKLY_MISSIONS)
        if not isinstance(raw, dict):
            return data
        data["enabled"] = bool(raw.get("enabled", True))
        data["reward"] = max(0, min(1_000_000, cls._as_int(raw.get("reward"), 1000)))
        tasks = raw.get("tasks")
        if isinstance(tasks, list):
            normalized = []
            seen = set()
            for index, item in enumerate(tasks):
                task = cls.normalize_mission_task(item, fallback_id=f"task{index + 1}")
                if not task or task["id"] in seen:
                    continue
                seen.add(task["id"])
                normalized.append(task)
            data["tasks"] = normalized
        return data

    @classmethod
    def get_weekly_missions(cls) -> dict:
        settings = cls._load_settings()
        return cls.normalize_weekly_missions(settings.get("weekly_missions"))

    @classmethod
    def save_weekly_missions(cls, payload: dict) -> dict:
        normalized = cls.normalize_weekly_missions(payload)

        def mutate(settings: dict):
            settings["weekly_missions"] = normalized

        cls._mutate_settings(mutate)
        return normalized

    @classmethod
    def update_weekly_missions(cls, **changes) -> dict:
        current = cls.get_weekly_missions()
        current.update(changes)
        return cls.save_weekly_missions(current)

    @classmethod
    def get_mission_task(cls, task_id: str) -> dict | None:
        wanted = str(task_id or "").strip()
        for task in cls.get_weekly_missions().get("tasks") or []:
            if task.get("id") == wanted:
                return deepcopy(task)
        return None

    @classmethod
    def upsert_mission_task(cls, payload: dict) -> dict | None:
        task = cls.normalize_mission_task(payload)
        if not task:
            return None
        current = cls.get_weekly_missions()
        tasks = list(current.get("tasks") or [])
        for index, item in enumerate(tasks):
            if item.get("id") == task["id"]:
                tasks[index] = task
                break
        else:
            if len(tasks) >= 12:
                return None
            tasks.append(task)
        current["tasks"] = tasks
        cls.save_weekly_missions(current)
        return task

    @classmethod
    def delete_mission_task(cls, task_id: str) -> bool:
        wanted = str(task_id or "").strip()
        current = cls.get_weekly_missions()
        tasks = [task for task in (current.get("tasks") or []) if task.get("id") != wanted]
        if len(tasks) == len(current.get("tasks") or []):
            return False
        current["tasks"] = tasks
        cls.save_weekly_missions(current)
        return True

    DISPUTE_REPLY_LIMIT = 8

    @staticmethod
    def normalize_dispute_reply(raw) -> dict | None:
        if not isinstance(raw, dict):
            return None
        try:
            percent = int(raw.get("percent"))
        except (TypeError, ValueError):
            return None
        if percent < 1 or percent > 90:
            return None
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        if len(text) > 3500:
            text = text[:3500]
        return {"percent": percent, "text": text, "label": f"{percent}%"}

    @classmethod
    def _clean_dispute_replies(cls, raw) -> list[dict]:
        if not isinstance(raw, list):
            return []
        items = []
        for row in raw:
            item = cls.normalize_dispute_reply(row)
            if not item:
                continue
            items.append({"percent": item["percent"], "text": item["text"]})
            if len(items) >= cls.DISPUTE_REPLY_LIMIT:
                break
        return items

    @classmethod
    def get_dispute_replies(cls) -> list[dict]:
        settings = cls._load_settings()
        stored = cls._clean_dispute_replies(settings.get("dispute_replies"))
        return [
            {"percent": item["percent"], "text": item["text"], "label": f"{item['percent']}%"}
            for item in stored
        ]

    @classmethod
    def add_dispute_reply(cls, percent: int, text: str) -> dict | None:
        item = cls.normalize_dispute_reply({"percent": percent, "text": text})
        if not item:
            return None
        saved = {"item": None}

        def mutate(settings: dict):
            rows = cls._clean_dispute_replies(settings.get("dispute_replies"))
            if len(rows) >= cls.DISPUTE_REPLY_LIMIT:
                return
            rows.append({"percent": item["percent"], "text": item["text"]})
            settings["dispute_replies"] = rows
            saved["item"] = {"percent": item["percent"], "text": item["text"], "label": item["label"]}

        cls._mutate_settings(mutate)
        return saved["item"]

    @classmethod
    def update_dispute_reply_text(cls, index: int, text: str) -> dict | None:
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        if len(cleaned) > 3500:
            cleaned = cleaned[:3500]
        saved = {"item": None}

        def mutate(settings: dict):
            rows = cls._clean_dispute_replies(settings.get("dispute_replies"))
            if index < 0 or index >= len(rows):
                return
            rows[index]["text"] = cleaned
            settings["dispute_replies"] = rows
            row = rows[index]
            saved["item"] = {"percent": row["percent"], "text": row["text"], "label": f"{row['percent']}%"}

        cls._mutate_settings(mutate)
        return saved["item"]

    @classmethod
    def delete_dispute_reply(cls, index: int) -> bool:
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        saved = {"ok": False}

        def mutate(settings: dict):
            rows = cls._clean_dispute_replies(settings.get("dispute_replies"))
            if index < 0 or index >= len(rows):
                return
            rows.pop(index)
            settings["dispute_replies"] = rows
            saved["ok"] = True

        cls._mutate_settings(mutate)
        return saved["ok"]
