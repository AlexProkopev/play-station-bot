import html
import json
import os
from datetime import datetime, timedelta

from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService
from app.utils import get_now_msk

COUPONS_FILE = "storage/coupons_db.json"
COUPONS_DOC_KEY = "coupons_db"
ZERO_PURCHASE_COUPON_DAYS = 7


class CouponService:
    @staticmethod
    def _normalize_code(code: str | None) -> str:
        cleaned = (code or "").strip().upper()
        return "".join(cleaned.split())

    @staticmethod
    def _load_db() -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(COUPONS_DOC_KEY, {"coupons": {}})
            if not isinstance(data, dict):
                return {"coupons": {}}
            if not isinstance(data.get("coupons"), dict):
                data["coupons"] = {}
            return data

        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)

        if not os.path.exists(COUPONS_FILE):
            return {"coupons": {}}

        try:
            with open(COUPONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return {"coupons": {}}
                if not isinstance(data.get("coupons"), dict):
                    data["coupons"] = {}
                return data
        except Exception:
            return {"coupons": {}}

    @staticmethod
    def _save_db(data: dict) -> None:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(COUPONS_DOC_KEY, data, force=True)
            return
        try:
            with open(COUPONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as err:
            print(f"Error saving coupons db: {err}")

    @staticmethod
    def create_coupon(code: str, amount: float, created_by: int | None = None) -> tuple[bool, str]:
        normalized = CouponService._normalize_code(code)
        if not normalized:
            return False, "Код купона не может быть пустым."

        try:
            amount_value = float(amount)
        except (TypeError, ValueError):
            return False, "Сумма купона должна быть числом."

        if amount_value <= 0:
            return False, "Сумма купона должна быть больше 0."

        data = CouponService._load_db()
        coupons = data.get("coupons", {})

        existing = coupons.get(normalized)
        if isinstance(existing, dict) and existing.get("is_active", True):
            return False, "Купон с таким кодом уже существует и активен."

        coupons[normalized] = {
            "code": normalized,
            "amount": amount_value,
            "is_active": True,
            "created_at": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
            "created_by": created_by,
            "redeemed_at": None,
            "redeemed_by": None,
        }
        data["coupons"] = coupons
        CouponService._save_db(data)
        return True, normalized

    @staticmethod
    def _parse_dt(value) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        if not value:
            return None
        try:
            return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_dt(value: datetime | None) -> str:
        if not value:
            return "-"
        return value.strftime("%d.%m.%Y %H:%M") + " МСК"

    @staticmethod
    def last_redeem_at(user_id: int, coupons: dict | None = None) -> datetime | None:
        if coupons is None:
            coupons = CouponService._load_db().get("coupons", {})
        if not isinstance(coupons, dict):
            return None
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        latest = None
        for item in coupons.values():
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("redeemed_by") or 0) != uid:
                    continue
            except (TypeError, ValueError):
                continue
            stamp = CouponService._parse_dt(item.get("redeemed_at"))
            if stamp and (latest is None or stamp > latest):
                latest = stamp
        return latest

    @staticmethod
    def count_redeemed_by_user(user_id: int, coupons: dict | None = None) -> int:
        if coupons is None:
            coupons = CouponService._load_db().get("coupons", {})
        if not isinstance(coupons, dict):
            return 0
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return 0
        count = 0
        for item in coupons.values():
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("redeemed_by") or 0) == uid:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    @staticmethod
    def get_activation_status(user_id: int) -> dict:
        user = UserService.get_user(user_id) or {}
        try:
            purchases = max(0, int(user.get("purchases_count") or 0))
        except (TypeError, ValueError):
            purchases = 0
        try:
            stored = max(0, int(user.get("coupons_activated_count") or 0))
        except (TypeError, ValueError):
            stored = 0
        from_db = CouponService.count_redeemed_by_user(user_id)
        used = max(stored, from_db)
        last_at = CouponService._parse_dt(user.get("last_coupon_at"))
        coupon_last = CouponService.last_redeem_at(user_id)
        if coupon_last and (last_at is None or coupon_last > last_at):
            last_at = coupon_last

        if purchases >= 1:
            return {
                "purchases": purchases,
                "used": used,
                "unlimited": True,
                "can_redeem": True,
                "last_coupon_at": last_at.strftime("%Y-%m-%d %H:%M:%S") if last_at else None,
                "next_at": None,
            }

        next_at = (last_at + timedelta(days=ZERO_PURCHASE_COUPON_DAYS)) if last_at else None
        now = get_now_msk()
        can_redeem = next_at is None or now >= next_at
        return {
            "purchases": purchases,
            "used": used,
            "unlimited": False,
            "can_redeem": can_redeem,
            "last_coupon_at": last_at.strftime("%Y-%m-%d %H:%M:%S") if last_at else None,
            "next_at": next_at.strftime("%Y-%m-%d %H:%M:%S") if next_at else None,
        }

    @staticmethod
    def build_limit_message(status: dict | None = None) -> str:
        data = status or {}
        next_at = CouponService._parse_dt(data.get("next_at"))
        when = CouponService._format_dt(next_at)
        return (
            "🔒 <b>Промокод Store пока закрыт</b>\n\n"
            "До первого заказа действует лимит: <b>только 1 купон в неделю</b>.\n"
            "Подряд с пустой историей коды не принимаются.\n\n"
            "📚 Заказов в аккаунте: <b>0</b>\n"
            f"⏳ Следующее окно: <b>{when}</b>\n\n"
            "После <b>1 покупки</b> промокоды идут <b>без ограничений</b>."
        )

    @staticmethod
    def build_prompt_message(status: dict | None = None) -> str:
        data = status or {}
        try:
            purchases = max(0, int(data.get("purchases") or 0))
        except (TypeError, ValueError):
            purchases = 0
        if purchases >= 1:
            extra = (
                f"📚 Заказов в аккаунте: <b>{purchases}</b>\n"
                "✨ Промокоды Store <b>без ограничений</b>.\n\n"
            )
        else:
            extra = (
                "📚 Заказов в аккаунте: <b>0</b>\n"
                "⏳ До первого заказа - <b>1 купон в неделю</b>.\n"
                "После первой оплаты лимит снимается, дальше без ограничений.\n\n"
            )
        return (
            "🎟 <b>Промокод PlayStation™Store</b>\n\n"
            f"{extra}"
            "Пришлите код, который выдал куратор Store:"
        )

    @staticmethod
    def build_success_message(code: str, amount: float, status: dict | None = None) -> str:
        data = status or {}
        unlimited = bool(data.get("unlimited"))
        if unlimited:
            extra = "✨ Промокоды без ограничений: следующий код можно ввести сразу."
        else:
            next_at = CouponService._parse_dt(data.get("next_at"))
            extra = (
                "⏳ До первого заказа следующий промокод откроется <b>через 7 дней</b>"
                f" ({CouponService._format_dt(next_at)}).\n"
                "После 1 покупки это ограничение исчезнет."
            )
        return (
            "🟢 <b>Промокод зачислен в кошелек</b>\n\n"
            f"Код: <code>{html.escape(str(code))}</code>\n"
            f"На счет: <b>{amount:.2f} руб.</b>\n\n"
            f"{extra}"
        )

    @staticmethod
    def _format_amount(amount: float) -> str:
        try:
            number = float(amount or 0)
        except (TypeError, ValueError):
            number = 0.0
        if abs(number - round(number)) < 0.001:
            return f"{number:.0f}"
        return f"{number:.2f}"

    @staticmethod
    def build_admin_redeem_message(
        user_id: int,
        username: str | None,
        code: str,
        amount: float,
        purchases: int | None = None,
    ) -> str:
        if purchases is None:
            user = UserService.get_user(user_id) or {}
            try:
                purchases = max(0, int(user.get("purchases_count") or 0))
            except (TypeError, ValueError):
                purchases = 0
            if not username:
                username = user.get("username")
        else:
            try:
                purchases = max(0, int(purchases or 0))
            except (TypeError, ValueError):
                purchases = 0
        if username:
            who = f"@{html.escape(str(username).lstrip('@'))}"
        else:
            who = "без username"
        return (
            "🎟 <b>Промокод активирован в Store</b>\n\n"
            f"Пользователь: {who}\n"
            f"ID: <code>{int(user_id)}</code>\n"
            f"Сумма: <b>{CouponService._format_amount(amount)} ₽</b>\n"
            f"Покупок: <b>{purchases}</b>\n"
            f"Код: <code>{html.escape(str(code))}</code>"
        )

    @staticmethod
    def redeem_coupon(code: str, user_id: int, username: str | None = None) -> tuple[bool, float, str]:
        normalized = CouponService._normalize_code(code)
        if not normalized:
            return False, 0.0, "Пришлите код промокода."

        data = CouponService._load_db()
        coupons = data.get("coupons", {})
        coupon = coupons.get(normalized)
        if not isinstance(coupon, dict):
            return False, 0.0, "Такой промокод в Store не найден."

        if not coupon.get("is_active", True):
            return False, 0.0, "Этот промокод уже использован или выключен."

        amount = float(coupon.get("amount", 0.0) or 0.0)
        if amount <= 0:
            return False, 0.0, "Сумма промокода некорректна."

        UserService.add_user(user_id, username)
        status = CouponService.get_activation_status(user_id)
        if not status.get("can_redeem"):
            return False, 0.0, CouponService.build_limit_message(status)

        reserved, status = UserService.reserve_coupon_activation(
            user_id,
            CouponService.count_redeemed_by_user(user_id, coupons),
            last_known_at=status.get("last_coupon_at"),
        )
        if not reserved:
            return False, 0.0, CouponService.build_limit_message(status)

        credit = UserService.credit_balance(user_id, amount, source="bonus")
        if not credit.get("ok") or float(credit.get("credited") or 0) <= 0:
            UserService.release_coupon_activation(user_id)
            if credit.get("capped") or UserService.is_at_bonus_cap(UserService.get_user(user_id)):
                return False, 0.0, (
                    "В кошельке уже 1500 ₽ бонусами. Новая сумма не ляжет, "
                    "пока приглашенный друг не оплатит заказ."
                )
            return False, 0.0, "Не удалось зачислить сумму по промокоду."
        amount = float(credit.get("credited") or amount)

        coupon["is_active"] = False
        coupon["redeemed_by"] = int(user_id)
        coupon["redeemed_at"] = get_now_msk().strftime("%Y-%m-%d %H:%M:%S")
        coupons[normalized] = coupon
        data["coupons"] = coupons
        CouponService._save_db(data)
        return True, amount, normalized

    @staticmethod
    def consume_as_discount(code: str, user_id: int, username: str | None = None) -> tuple[bool, float, str]:
        """Списать купон в счёт заказа, не зачисляя сумму на баланс."""
        normalized = CouponService._normalize_code(code)
        if not normalized:
            return False, 0.0, "Пришлите код промокода."

        data = CouponService._load_db()
        coupons = data.get("coupons", {})
        coupon = coupons.get(normalized)
        if not isinstance(coupon, dict):
            return False, 0.0, "Такого промокода в Store нет."

        if not coupon.get("is_active", True):
            return False, 0.0, "Этот промокод уже погашен."

        amount = float(coupon.get("amount", 0.0) or 0.0)
        if amount <= 0:
            return False, 0.0, "Сумма промокода некорректна."

        UserService.add_user(user_id, username)
        status = CouponService.get_activation_status(user_id)
        if not status.get("can_redeem"):
            return False, 0.0, CouponService.build_limit_message(status)

        reserved, status = UserService.reserve_coupon_activation(
            user_id,
            CouponService.count_redeemed_by_user(user_id, coupons),
            last_known_at=status.get("last_coupon_at"),
        )
        if not reserved:
            return False, 0.0, CouponService.build_limit_message(status)

        coupon["is_active"] = False
        coupon["redeemed_by"] = int(user_id)
        coupon["redeemed_at"] = get_now_msk().strftime("%Y-%m-%d %H:%M:%S")
        coupon["redeemed_as"] = "checkout_discount"
        coupons[normalized] = coupon
        data["coupons"] = coupons
        CouponService._save_db(data)
        return True, amount, normalized