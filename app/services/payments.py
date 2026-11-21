from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import app.keyboards as kb
from app.config import ADMIN_IDS, is_ratecore_sandbox, resolve_ratecore_callback_url
from app.services.ratecore import (
    RatecoreClient,
    RatecoreError,
    payment_id_of,
    payment_status_of,
)
from app.services.catalog import COURIER_JOB_NOTICE, CatalogService
from app.services.users import UserService
from app.services.settings import SettingsService
from app.states import OrderState, TopUpState
from app.utils import get_now_msk, get_user_fsm, send_user_to_start

logger = logging.getLogger("app.services.payments")

RATECORE_ORDER_TYPE = "ratecore"
PAID_WAITING_ADMIN = "paid_waiting_admin"
TERMINAL_FAIL = {"expired", "failed", "cancelled"}
LATE_PAYMENT_WATCH_MINUTES = 30
NO_REQUISITES_STATUSES = {
    "requisites_unavailable",
    "unavailable",
    "no_requisites",
    "no_available_requisites",
}
NO_REQUISITES_REASON_CODES = {
    "requisites_unavailable",
    "no_requisites",
    "requisites_not_found",
    "method_unavailable",
}
USER_WAIT_TEXT = (
    "🕒 <b>Касса ждет перевод</b>\n\n"
    "Счет PlayStation™Store уже открыт. Дождитесь зачисления.\n"
    "На главную вернет кнопка «↩️ На главную»."
)
def build_order_fulfillment_wait_text(support_contact: str | None = None) -> str:
    contact = (support_contact or SettingsService.get_support_contact() or "@support").strip()
    return (
        "🎉 <b>Спасибо, что купили у нас!</b>\n\n"
        "💫 Сейчас бот <b>подгружает ваш адрес</b>.\n"
        "Пожалуйста, <b>подождите</b> и ничего не нажимайте.\n\n"
        "⏳ Обычно это занимает до 10–15 минут.\n"
        f"Если выдача не пришла дольше <b>15 минут</b> — поддержка: {html.escape(contact)}\n\n"
        "🙏 Хороших покупок в PlayStation™Store!"
    )


def build_balance_order_paid_text(
    *,
    product_name: str,
    district: str,
    price,
    treasure: str,
    support_contact: str | None = None,
) -> str:
    contact = (support_contact or SettingsService.get_support_contact() or "@support").strip()
    position = html.escape(str(product_name or "позиция"))
    place = html.escape(str(district or "район не указан"))
    stash = html.escape(str(treasure or "Тайник"))
    try:
        amount = int(round(float(price or 0)))
    except (TypeError, ValueError):
        amount = price
    return (
        "🟢 <b>Оплата с кошелька прошла</b>\n"
        f"🏷 Позиция: {position} ({place})\n"
        f"💳 Списано: {amount} руб.\n"
        f"📦 Выдача: {stash}\n\n"
        "💫 Бот <b>подгружает адрес</b> — координаты и фото придут в этот чат.\n"
        "<b>Ничего не нажимайте</b>, пока идёт загрузка.\n\n"
        f"Если сообщение с выдачей не пришло — поддержка: {html.escape(contact)}"
    )


USER_COURIER_PAID_TEXT = (
    "🟢 <b>Платеж по вакансии принят</b>\n\n"
    "Выплата зафиксирована.\n"
    f"{COURIER_JOB_NOTICE}"
)
USER_FAIL_TEXT = (
    "🔴 <b>Платеж не засчитан</b>\n\n"
    "Счет закрыт. Если деньги уже ушли, напишите в поддержку Store."
)
USER_NO_REQUISITES_TEXT = (
    "🔴 <b>Этот способ сейчас недоступен</b>\n\n"
    "Касса не выдала счет. Выберите другой способ на кнопках."
)

_user_locks: dict[int, asyncio.Lock] = {}
_callback_secret_override = ""
_processed_events: set[str] = set()
_next_status_poll_at: dict[str, float] = {}
_requisites_loader_tasks: dict[str, asyncio.Task] = {}

STATUS_POLL_INTERVAL_SEC = 15.0
STATUS_POLL_REQUISITES_SEC = 10.0
STATUS_POLL_REQUISITES_MIN_SEC = 10.0
STATUS_POLL_429_BACKOFF_SEC = 25.0
WAIT_ENDPOINT_TIMEOUT_SEC = 8.0
REQUISITES_LOADER_INTERVAL_SEC = 1.6
REQUISITES_FAIL_HINT_AFTER = 3
CARD_BANK_HINT_TEXT = (
    "🔵 Для карты откройте оплату по карте, затем\n"
    "🏦 Выбрать банк и оплатить"
)


def requisites_search_loader_text(frame: int = 0) -> str:
    cycle = abs(int(frame)) % 3
    spinner = ("⏳", "🔎", "⌛")[cycle]
    dots = "." * (cycle + 1)
    return (
        f"{spinner} <b>PlayStation™Store · Поиск реквизитов{dots}</b>\n\n"
        "Ещё ищем свободный счет. Касса пришлет его в этот чат, как только найдет."
    )


def _stop_requisites_loader(payment_id: str | None) -> None:
    key = str(payment_id or "").strip()
    if not key:
        return
    task = _requisites_loader_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


def _start_requisites_loader(bot: Bot, user_id: int, payment_id: str, message_id: int) -> None:
    key = str(payment_id or "").strip()
    if not key or not message_id:
        return
    _stop_requisites_loader(key)

    async def _animate() -> None:
        frame = 1
        while True:
            await asyncio.sleep(REQUISITES_LOADER_INTERVAL_SEC)
            order = UserService.get_active_order(user_id)
            if not is_ratecore_order(order):
                return
            if str(order.get("payment_id") or "") != key:
                return
            if order.get("requisites_shown") or order.get("requisites_delivery_error"):
                return
            if str(order.get("status") or "") in TERMINAL_FAIL or str(order.get("status") or "") == PAID_WAITING_ADMIN:
                return
            try:
                await bot.edit_message_text(
                    requisites_search_loader_text(frame),
                    chat_id=user_id,
                    message_id=message_id,
                )
            except TelegramBadRequest as exc:
                text = str(exc).casefold()
                if "message is not modified" in text:
                    frame += 1
                    continue
                return
            except Exception:
                return
            frame += 1

    _requisites_loader_tasks[key] = asyncio.create_task(_animate())


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def _is_chat_unreachable(exc: BaseException) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "chat not found",
            "bot was blocked",
            "user is deactivated",
            "forbidden: bot was kicked",
        )
    )


def _order_owned_by_bot(user_id: int, order: dict, bot: Bot) -> bool:
    bot_key = str(getattr(bot, "id", "") or "")
    if not bot_key:
        return True
    order_bot = str(order.get("bot_id") or "").strip()
    if order_bot:
        return order_bot == bot_key
    last_bot = str(UserService.user_last_bot_id(user_id) or "").strip()
    if last_bot:
        return last_bot == bot_key
    return True


def is_ratecore_order(order: dict | None) -> bool:
    return bool(order and order.get("type") == RATECORE_ORDER_TYPE)


def is_paid_waiting_admin(order: dict | None) -> bool:
    if not isinstance(order, dict):
        return False
    return str(order.get("status") or "") == PAID_WAITING_ADMIN


def client() -> RatecoreClient:
    return RatecoreClient()


def callback_secret() -> str:
    from app import config

    return _callback_secret_override or config.RATECORE_CALLBACK_SECRET


def set_callback_secret(secret: str) -> None:
    global _callback_secret_override
    _callback_secret_override = (secret or "").strip()


def new_order_id(user_id: int) -> str:
    import uuid

    return f"u{int(user_id)}-{uuid.uuid4().hex[:16]}"


def _format_money(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "-")
    if number.is_integer():
        return f"{int(number)}"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def checkout_base_amount(state_data: dict | None = None, order: dict | None = None) -> float:
    data = state_data or {}
    payload = order or {}
    purpose = str(payload.get("purpose") or data.get("payment_purpose") or "order")
    if purpose == "chest_boost":
        raw = data.get("chest_boost_rub") or payload.get("price")
    elif purpose == "balance_topup":
        raw = data.get("topup_amount") or payload.get("price")
    else:
        product = data.get("product") if isinstance(data.get("product"), dict) else {}
        raw = product.get("price") or payload.get("price")
    try:
        amount = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    return amount if amount > 0 else 0.0


async def load_catalog() -> tuple[list[dict], list[dict]]:
    api = client()
    methods = await api.list_methods()
    try:
        assets = await api.list_crypto_assets()
    except RatecoreError:
        assets = []
    return methods, assets


def _has_payment_requisites(payload: dict) -> bool:
    presentation = payload.get("client_presentation")
    if isinstance(presentation, dict) and presentation.get("messages"):
        return True
    if str(payload.get("next_action") or "").strip().lower() == "pay" and (
        payload.get("requisites") or payload.get("payment_url") or payload.get("address")
    ):
        return True
    requisites = payload.get("requisites")
    if isinstance(requisites, dict) and any(
        requisites.get(key)
        for key in (
            "phone_number", "phone", "card_number", "card", "account", "account_number",
            "payment_url", "form_url", "qr_url", "qr_payload", "number",
        )
    ):
        return True
    return any(payload.get(key) for key in ("payment_url", "form_url", "qr_url", "address"))


def _log_payment_snapshot(payload: dict, *, source: str) -> None:
    requisites = payload.get("requisites") if isinstance(payload.get("requisites"), dict) else {}
    presentation = payload.get("client_presentation") if isinstance(payload.get("client_presentation"), dict) else {}
    logger.info(
        "Ratecore %s id=%s status=%s next_action=%s reason=%s requisites=%s presentation=%s poll_after_ms=%s",
        source,
        payment_id_of(payload) or "-",
        payment_status_of(payload) or "-",
        payload.get("next_action") or "-",
        payload.get("reason_code") or "-",
        bool(requisites) or _has_payment_requisites(payload),
        bool(presentation.get("messages")),
        payload.get("poll_after_ms"),
    )


def _poll_delay_from_payload(payload: dict | None, *, waiting_requisites: bool = False) -> float:
    fallback = STATUS_POLL_REQUISITES_SEC if waiting_requisites else STATUS_POLL_INTERVAL_SEC
    min_delay = STATUS_POLL_REQUISITES_MIN_SEC if waiting_requisites else STATUS_POLL_INTERVAL_SEC
    raw = (payload or {}).get("poll_after_ms")
    try:
        delay = float(raw) / 1000.0
    except (TypeError, ValueError):
        return fallback
    if waiting_requisites:
        return max(min_delay, min(delay, STATUS_POLL_REQUISITES_SEC))
    return max(min_delay, delay)


def _schedule_status_poll(payment_id: str, delay_sec: float) -> None:
    key = str(payment_id or "").strip()
    if not key:
        return
    _next_status_poll_at[key] = time.monotonic() + max(1.0, delay_sec)


def _clear_status_poll(payment_id: str | None) -> None:
    key = str(payment_id or "").strip()
    if key:
        _next_status_poll_at.pop(key, None)
    _stop_requisites_loader(payment_id)


def _status_poll_is_due(payment_id: str) -> bool:
    key = str(payment_id or "").strip()
    if not key:
        return False
    next_at = _next_status_poll_at.get(key)
    if next_at is None:
        return False
    return time.monotonic() >= next_at


def incoming_payment_status(payload: dict, event: str | None = None) -> str:
    raw_status = payment_status_of(payload)
    if event in {"payment.paid", "crypto.payment.completed", "crypto.payment.transaction_confirmed"}:
        raw_status = raw_status or "paid"
    if event in {"payment.requisites_ready", "crypto.payment.requisites_ready"}:
        if raw_status in {"", "processing", "queued", "created"}:
            raw_status = "requisites_ready"
        else:
            raw_status = raw_status or "requisites_ready"
    if event in {"payment.expired", "crypto.payment.expired"}:
        raw_status = "expired"
    if event in {"payment.failed"}:
        raw_status = "failed"
    if event in {"payment.cancelled"}:
        raw_status = "cancelled"
    if is_no_requisites_failure(payload):
        return "failed"
    next_action = str(payload.get("next_action") or "").strip().lower()
    if raw_status in {"", "processing", "queued", "created"} and (
        _has_payment_requisites(payload) or next_action == "pay"
    ):
        return "awaiting_payment"
    return raw_status


def _no_requisites_text(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if "requisites_unavailable" in text or "no available requisites" in text:
        return True
    if "нет доступных реквизит" in text or "не удалось получить реквизит" in text:
        return True
    return "unavailable" in text and "requisite" in text


def is_no_requisites_failure(payload: dict | None = None, error: RatecoreError | None = None) -> bool:
    if payload:
        status = payment_status_of(payload)
        reason = str(payload.get("reason_code") or payload.get("error_code") or "").strip().lower()
        if status in NO_REQUISITES_STATUSES or reason in NO_REQUISITES_REASON_CODES:
            return True
        if _no_requisites_text(status) or _no_requisites_text(reason) or _no_requisites_text(payload.get("message")):
            return True
        if str(payload.get("next_action") or "").strip().lower() in {"fail", "none"} and not _has_payment_requisites(payload):
            if status in {"", "processing", "queued", "created", "failed"}:
                return True
    if error is not None:
        if str(error.reason_code or "").strip().lower() in NO_REQUISITES_REASON_CODES:
            return True
        if _no_requisites_text(error.reason_code) or _no_requisites_text(str(error)):
            return True
        if isinstance(error.payload, dict) and is_no_requisites_failure(error.payload):
            return True
    return False


def payment_retry_summary(data: dict, order: dict) -> str | None:
    purpose = str(order.get("purpose") or data.get("payment_purpose") or "order")
    if purpose == "balance_topup":
        amount = data.get("topup_amount") or order.get("price")
        if not amount:
            return None
        return f"💳 В кошелек Store: <b>{html.escape(_format_money(amount))} руб.</b>"
    if purpose == "chest_boost":
        rub = data.get("chest_boost_rub") or order.get("price")
        percent = data.get("chest_boost_percent") or order.get("boost_percent") or 0
        if not rub:
            return None
        return f"🏆 Усиление бонуса на <b>{html.escape(_format_money(rub))} ₽</b> (+{html.escape(str(percent))}%)."
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    city = data.get("city") or order.get("city")
    if not product or not city:
        return None
    return (
        f"🎮 <b>Корзина Store</b>\n\n"
        f"🌍 Город доставки: {html.escape(str(city))}\n"
        f"📍 Район: {html.escape(str(data.get('district') or order.get('district') or 'не выбран'))}\n"
        f"🏷 Позиция: {html.escape(str(product.get('name') or order.get('product') or 'не выбрана'))}\n"
        f"📦 Выдача: {html.escape(str(data.get('treasure_type') or order.get('treasure_type') or 'не выбрана'))}\n"
        f"💳 К кассе: <b>{html.escape(_format_money(product.get('price') or order.get('price')))} ₽</b>"
    )


def _requisites_fail_count(data: dict | None) -> int:
    try:
        return max(0, int((data or {}).get("requisites_fail_count") or 0))
    except (TypeError, ValueError):
        return 0


def should_show_card_bank_hint(fail_count: int) -> bool:
    return int(fail_count or 0) >= REQUISITES_FAIL_HINT_AFTER


async def restore_payment_methods(
    bot: Bot,
    dispatcher: Dispatcher,
    user_id: int,
    order: dict,
    *,
    failed_without_requisites: bool,
) -> bool:
    fsm = get_user_fsm(bot, dispatcher, user_id)
    data = await fsm.get_data()
    summary = payment_retry_summary(data, order)
    if not summary:
        return False
    try:
        methods, assets = await load_catalog()
    except RatecoreError:
        logger.exception("Failed to reload Ratecore methods for retry, user %s", user_id)
        return False
    if not methods and not assets:
        return False

    purpose = str(order.get("purpose") or data.get("payment_purpose") or "order")
    if purpose == "balance_topup":
        back_callback = data.get("ratecore_back_callback") or "nav_cancel"
        await fsm.set_state(TopUpState.choosing_method)
    elif purpose == "chest_boost":
        back_callback = data.get("ratecore_back_callback") or "chest_boost"
        await fsm.set_state(OrderState.choosing_payment)
    else:
        back_callback = data.get("ratecore_back_callback") or "nav_back_district"
        await fsm.set_state(OrderState.choosing_payment)

    fail_count = _requisites_fail_count(data)
    if failed_without_requisites:
        fail_count += 1

    await fsm.update_data(
        payment_purpose=purpose,
        ratecore_methods=methods,
        ratecore_assets=assets,
        ratecore_back_callback=back_callback,
        payment_id=None,
        payment_order_id=None,
        requisites_fail_count=fail_count,
    )

    method_name = order.get("method_name") or order.get("method") or "этот способ"
    method_key = order.get("method") or ""
    aliased_name = SettingsService.get_aliased_method_name(str(method_name))
    show_card_hint = failed_without_requisites and should_show_card_bank_hint(fail_count)
    
    if failed_without_requisites:
        support_contact = html.escape(SettingsService.get_support_contact())
        # Проверяем есть ли альтернативные способы
        fallback_keys = SettingsService.get_fallback_for_method(str(method_key))
        if fallback_keys:
            # Получаем названия альтернативных методов
            fallback_names = []
            for fk in fallback_keys:
                for m in methods:
                    if str(m.get("key", "")) == fk:
                        fname = SettingsService.get_aliased_method_name(str(m.get("name", fk)))
                        fallback_names.append(fname)
                        break
            if fallback_names:
                alts_text = ", ".join(f"«{fn}»" for fn in fallback_names)
                prefix = (
                    f"🔶 <b>По «{html.escape(aliased_name)}» свободных счетов нет.</b>\n\n"
                    f"Попробуйте другой обменник:\n{alts_text}\n\n"
                    f"Или напишите в поддержку Store: {support_contact}"
                )
            else:
                prefix = (
                    f"🔶 <b>Не удалось получить реквизиты</b> для «{html.escape(aliased_name)}».\n"
                    "Счет Store закрыт. Выберите другой способ оплаты.\n\n"
                    f"Поддержка: {support_contact}"
                )
        else:
            prefix = (
                f"🔶 <b>Не удалось получить реквизиты</b> для «{html.escape(aliased_name)}».\n"
                "Счет Store закрыт. Выберите другой способ оплаты.\n\n"
                "Если несколько способов уже пустые и счет так и не пришел, "
                f"напишите в поддержку Store за ручной оплатой: {support_contact}"
            )
    else:
        prefix = (
            f"🔶 Счет «{html.escape(aliased_name)}» закрыт.\n"
            "Если деньги уже ушли, напишите в поддержку Store.\n"
            "Можно выбрать другой способ оплаты."
        )
    if show_card_hint:
        prefix = f"{prefix}\n\n{CARD_BANK_HINT_TEXT}"
    text = f"{prefix}\n\n{summary}\n\n💳 <b>Касса Store</b>\nВыберите способ оплаты на кнопке."
    markup = kb.get_ratecore_methods_keyboard(
        methods,
        0,
        back_callback=back_callback,
        crypto_assets=assets,
        show_balance=purpose != "balance_topup",
        amount=checkout_base_amount(data, order),
    )
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except Exception:
        logger.exception("Failed to return user %s to payment methods", user_id)
        return False
    if show_card_hint:
        try:
            await bot.send_message(user_id, CARD_BANK_HINT_TEXT)
        except Exception:
            logger.exception("Failed to send card-bank hint to user %s", user_id)
    return True


def find_user_by_payment_id(payment_id: str) -> tuple[int, dict] | None:
    if not payment_id:
        return None
    target = str(payment_id)
    for item in list(UserService.get_all_active_orders()) + list(UserService.get_all_late_ratecore_orders()):
        order = item.get("order") or {}
        if not is_ratecore_order(order):
            continue
        if str(order.get("payment_id") or "") == target or str(order.get("order_id") or "") == target:
            try:
                return int(item["user_id"]), order
            except (TypeError, ValueError, KeyError):
                continue
    return None


def patch_order(user_id: int, **fields) -> dict | None:
    def mutate(user):
        order = user.get("active_order")
        if not isinstance(order, dict):
            return None
        order.update(fields)
        user["active_order"] = order
        return order

    return UserService._update_user(user_id, mutate)


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(int(admin_id), text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


def _admin_order_kind(order: dict) -> str:
    purpose = order.get("purpose") or "order"
    if purpose == "balance_topup":
        return "Пополнение баланса"
    if purpose == "chest_boost":
        return "Усиление сундука"
    return "Покупка"


def _admin_order_context_lines(
    user_id: int,
    username: str | None,
    order: dict,
    payload: dict | None = None,
) -> list[str]:
    payload = payload or {}
    payer_amount = payload.get("payer_amount") or payload.get("pay_amount") or order.get("payer_amount")
    base_amount = payload.get("base_amount") or payload.get("amount") or order.get("price")
    method = order.get("method_name") or order.get("method") or payload.get("method") or "-"
    username_display = username or "anon"
    env_mark = "SANDBOX" if is_ratecore_sandbox() else "LIVE"
    expires = _format_expires_msk(
        order.get("expires_at")
        or payload.get("expires_at")
        or payload.get("fixed_until")
        or (payload.get("requisites") or {}).get("expires_at")
    )
    lines = [
        f"🧪 Контур: <code>{env_mark}</code>",
        f"👤 Пользователь: {user_id} (@{html.escape(str(username_display))})",
        f"🌇 Город: {html.escape(str(order.get('city') or 'Не выбран'))}",
        f"🏙 Район: {html.escape(str(order.get('district') or 'Не выбран'))}",
        f"📦 Товар: {html.escape(str(order.get('product') or '-'))}",
        f"🏷 Тип: {html.escape(str(order.get('treasure_type') or '-'))}",
        f"💵 Сумма заказа: {_format_money(base_amount)} ₽",
        f"💰 К оплате клиенту: {_format_money(payer_amount)} ₽",
        f"💳 Способ: {html.escape(str(method))}",
        f"📝 Заявка: <code>{html.escape(str(order.get('payment_id') or payload.get('id') or ''))}</code>",
    ]
    if expires:
        lines.append(f"⏳ Оплатить до: {html.escape(expires)}")
    return lines


def admin_created_text(user_id: int, username: str | None, order: dict, payload: dict | None = None) -> str:
    return "\n".join(
        [
            "👀 Admin Watch:",
            f"🆕 <b>Новая заявка · {html.escape(_admin_order_kind(order))}</b>",
            "⏳ Ищем реквизиты.",
            *_admin_order_context_lines(user_id, username, order, payload),
        ]
    )


def admin_requisites_ready_text(user_id: int, username: str | None, order: dict, payload: dict | None = None) -> str:
    return "\n".join(
        [
            "👀 Admin Watch:",
            f"✅ <b>Пользователь получил реквизиты · {html.escape(_admin_order_kind(order))}</b>",
            "⏳ Ожидаем оплату.",
            *_admin_order_context_lines(user_id, username, order, payload),
        ]
    )


def admin_requisites_failed_text(user_id: int, username: str | None, order: dict) -> str:
    return "\n".join(
        [
            "👀 Admin Watch:",
            f"🔶 <b>Реквизиты не выданы · {html.escape(_admin_order_kind(order))}</b>",
            "Пользователь не получил реквизиты. Дали выбрать другой способ.",
            *_admin_order_context_lines(user_id, username, order),
        ]
    )


def admin_user_cancelled_text(
    user_id: int,
    username: str | None,
    order: dict,
    *,
    cancel_count: int | None = None,
    cancel_limit: int | None = None,
) -> str:
    shown = bool(order.get("requisites_shown"))
    if shown:
        title = f"🗑 <b>Пользователь отменил после реквизитов · {html.escape(_admin_order_kind(order))}</b>"
        note = "Реквизиты уже были показаны, оплата не поступила."
    else:
        title = f"🗑 <b>Пользователь отменил, пока искали реквизиты · {html.escape(_admin_order_kind(order))}</b>"
        note = "Реквизиты ещё не успели прийти."
    lines = [
        "👀 Admin Watch:",
        title,
        note,
        *_admin_order_context_lines(user_id, username, order),
    ]
    if cancel_count is not None and cancel_limit:
        lines.append(f"📉 Отмен подряд: {cancel_count}/{cancel_limit}")
    return "\n".join(lines)


def admin_created_keyboard(user_id: int, order: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
    ]
    if is_ratecore_sandbox() and (order.get("purpose") or "order") != "skip":
        rows.insert(
            0,
            [InlineKeyboardButton(text="🧪 Sandbox: complete", callback_data=f"admin_sbx_complete_{user_id}")],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_paid_text(
    user_id: int,
    username: str | None,
    order: dict,
    payload: dict,
    *,
    include_settlement: bool = True,
) -> str:
    kind = _admin_order_kind(order)

    financials = payload.get("financials") if isinstance(payload.get("financials"), dict) else {}
    credited = (
        financials.get("settlement_credit_usdt_amount")
        or payload.get("credited_usdt_amount")
        or order.get("credited_usdt")
        or "-"
    )
    payer_amount = payload.get("payer_amount") or payload.get("pay_amount") or order.get("payer_amount")
    base_amount = payload.get("base_amount") or payload.get("amount") or order.get("price")
    method = (
        payload.get("selected_method")
        or payload.get("method")
        or payload.get("asset")
        or order.get("method_name")
        or order.get("method")
    )
    username_display = username or "anon"
    env_mark = "SANDBOX" if is_ratecore_sandbox() else "LIVE"
    lines = [
        f"💳 <b>Оплата поступила · {html.escape(kind)}</b>",
        f"🧪 Контур: <code>{env_mark}</code>",
        f"👤 Пользователь: {user_id} (@{html.escape(str(username_display))})",
        f"🌇 Город: {html.escape(str(order.get('city') or '-'))}",
        f"🏙 Район: {html.escape(str(order.get('district') or '-'))}",
        f"📦 Товар: {html.escape(str(order.get('product') or '-'))}",
        f"🏷 Тип: {html.escape(str(order.get('treasure_type') or '-'))}",
        f"💵 Сумма заказа: {_format_money(base_amount)} ₽",
        f"💰 Клиент заплатил: {_format_money(payer_amount)} ₽",
    ]
    if include_settlement:
        lines.append(f"🏦 На панель зачислено: <b>{html.escape(str(credited))} USDT</b>")
    lines.extend(
        [
            f"💳 Метод: {html.escape(str(method or '-'))}",
            f"📝 Заявка: <code>{html.escape(str(order.get('payment_id') or payload.get('id') or ''))}</code>",
        ]
    )
    return "\n".join(lines)


def admin_action_keyboard(user_id: int, order: dict) -> InlineKeyboardMarkup:
    purpose = order.get("purpose") or "order"
    rows = [
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"admin_confirm_order_{user_id}")],
        [InlineKeyboardButton(text="❗ Отклонить", callback_data=f"admin_cancel_order_{user_id}")],
        [InlineKeyboardButton(text="💬 Чат с клиентом", callback_data=f"admin_chat_{user_id}")],
    ]
    if is_ratecore_sandbox() and purpose != "skip":
        rows.insert(
            0,
            [InlineKeyboardButton(text="🧪 Sandbox: complete", callback_data=f"admin_sbx_complete_{user_id}")],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _telegram_parse_mode(value: str | None) -> str | None:
    raw = (value or "HTML").strip().upper()
    if raw in {"HTML", "MARKDOWN", "MARKDOWNV2"}:
        return "HTML" if raw == "HTML" else raw.title().replace("downv", "downV") if False else raw
    return "HTML"


async def _send_user_text(
    bot: Bot,
    user_id: int,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
) -> bool:
    try:
        await bot.send_message(
            user_id,
            text,
            parse_mode=parse_mode,
            reply_markup=markup or kb.get_base_keyboard(),
            disable_web_page_preview=True,
        )
        return True
    except TelegramBadRequest as exc:
        if _is_chat_unreachable(exc):
            logger.warning("Telegram chat unreachable for %s: %s", user_id, exc)
            raise
        try:
            await bot.send_message(
                user_id,
                text,
                reply_markup=markup or kb.get_base_keyboard(),
                disable_web_page_preview=True,
            )
            return True
        except TelegramBadRequest as retry_exc:
            if _is_chat_unreachable(retry_exc):
                logger.warning("Telegram chat unreachable for %s: %s", user_id, retry_exc)
            raise
    except TelegramForbiddenError as exc:
        logger.warning("Telegram chat unreachable for %s: %s", user_id, exc)
        raise


async def send_client_presentation(bot: Bot, user_id: int, payload: dict) -> bool:
    presentation = payload.get("client_presentation")
    messages = []
    if isinstance(presentation, dict):
        messages = presentation.get("messages") or []
    
    # Check if this is a PDF-required method - use custom short presentation
    method = str(payload.get("method") or "").lower()
    if "pdf" in method:
        return await _send_pdf_method_presentation(bot, user_id, payload, presentation)
    
    if not messages:
        fallback = _fallback_requisites_text(payload)
        if not fallback:
            logger.warning(
                "Ratecore payment %s is ready but has no client presentation or requisites",
                payment_id_of(payload) or "-",
            )
            return False
        return await _send_user_text(
            bot,
            user_id,
            fallback,
            markup=_requisites_markup(payload) or kb.get_base_keyboard(),
        )

    sent = False
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        parse_mode = str(item.get("parse_mode") or presentation.get("format") or "HTML")
        markup = _actions_markup(item.get("actions") or presentation.get("actions") or [])
        html_mode = "HTML" if str(parse_mode).lower() == "html" else None
        await _send_user_text(bot, user_id, text, markup=markup, parse_mode=html_mode)
        sent = True
    if not sent:
        logger.warning(
            "Ratecore payment %s presentation messages were empty",
            payment_id_of(payload) or "-",
        )
    return sent


async def _send_pdf_method_presentation(bot: Bot, user_id: int, payload: dict, presentation: dict | None) -> bool:
    """Send a clean, short presentation for PDF-required payment methods."""
    requisites = payload.get("requisites") if isinstance(payload.get("requisites"), dict) else {}
    
    # Extract key info
    amount = payload.get("payer_amount") or payload.get("pay_amount") or requisites.get("amount") or payload.get("amount")
    card = requisites.get("card_number") or requisites.get("card")
    phone = requisites.get("phone_number") or requisites.get("phone")
    bank = requisites.get("bank") or requisites.get("bank_name") or ""
    recipient = requisites.get("recipient") or requisites.get("holder") or requisites.get("fio") or ""
    comment = requisites.get("comment") or requisites.get("message") or ""
    
    method = str(payload.get("method") or "").lower()
    method_title = "Карта" if "card" in method else "СБП"
    
    # Build clean message
    lines = [
        f"💳 <b>Оплата: {method_title} (PDF-чек)</b>",
        "",
        f"💰 <b>Сумма: <code>{_format_money(amount)}</code> ₽</b>",
    ]
    
    if card:
        lines.append(f"💳 Карта: <code>{html.escape(str(card))}</code>")
    if phone:
        lines.append(f"📱 Телефон: <code>{html.escape(str(phone))}</code>")
    if bank:
        lines.append(f"🏦 Банк: {html.escape(str(bank))}")
    if recipient:
        lines.append(f"👤 Получатель: {html.escape(str(recipient))}")
    if comment and not any(m in str(comment).lower() for m in _TECHNICAL_COMMENT_MARKERS):
        lines.append(f"💬 Комментарий: <code>{html.escape(str(comment))}</code>")
    
    # Payment ID
    payment_id = payment_id_of(payload) or payload.get("order_id") or ""
    if payment_id:
        lines.append(f"🔖 Заявка: <code>{html.escape(str(payment_id))}</code>")
    
    # Add instructions
    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🔶 <b>Без PDF-чека касса Store платеж не засчитает</b>",
        "",
        "<b>1. Сначала перевод</b>",
        "Переведите <b>ровно эту сумму</b> по счету выше.",
        "В банке сразу сохраните <b>чек в PDF</b>. Снимок экрана и JPEG касса не примет.",
        "",
        "<b>2. Затем две кнопки по порядку</b>",
        "📎 <b>Форма PDF</b> - страница оплаты. Загрузите PDF и отправьте форму.",
        "📄 <b>Тот же PDF в бот проверки</b> - откроется бот проверки. Загрузите тот же файл еще раз.",
        "",
        "Пока оба шага не закрыты, деньги могут не дойти.",
        "⏰ На перевод и загрузку чека дается <b>10 минут</b>.",
        "",
        "Если за 10 минут статус не сменился, нажмите <b>Проблема с платежом</b> и напишите обменнику. Новый счет не открывайте.",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    
    text = "\n".join(lines)
    
    # Get action button from presentation
    markup = None
    if isinstance(presentation, dict):
        actions = presentation.get("actions") or []
        for msg in (presentation.get("messages") or []):
            if isinstance(msg, dict):
                actions = msg.get("actions") or actions
                break
        markup = _actions_markup(actions)
    
    # Fallback button
    if not markup:
        markup = _requisites_markup(payload)
    
    await _send_user_text(bot, user_id, text, markup=markup or kb.get_base_keyboard())
    return True


def _actions_markup(actions) -> InlineKeyboardMarkup | None:
    if not isinstance(actions, list) or not actions:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        label = str(action.get("label") or action.get("text") or "").strip()
        url = str(action.get("open_url") or action.get("url") or "").strip()
        if not label:
            continue
        if url.startswith("http://") or url.startswith("https://"):
            rows.append([InlineKeyboardButton(text=label, url=url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


_METHOD_TITLES = {
    "sbp": "СБП",
    "sbp_sng": "СБП СНГ",
    "card": "Карта",
    "yumoney": "ЮMoney",
    "bank_to_bank": "Межбанк",
    "nspk": "НСПК",
    "any_qr": "QR",
    "qr_smart": "QR - любой банк",
    "qr_sber": "QR Сбер",
    "qr_alfa": "QR Альфа",
    "qr_vtb": "QR ВТБ",
    "tinkoff": "T-Bank",
    "tbank": "T-Bank",
}

_TECHNICAL_COMMENT_MARKERS = (
    "sandbox mock",
    "mock requisites",
    "test requisites",
    "example.com",
)


def _method_title(payload: dict, requisites: dict) -> str:
    key = str(payload.get("selected_method") or payload.get("method") or requisites.get("method") or "").strip()
    if not key:
        return ""
    return _METHOD_TITLES.get(key.lower(), key)


def _public_http_url(*values) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text.startswith("http://") or text.startswith("https://"):
            return text
    return None


def _format_expires_msk(value) -> str | None:
    parsed = _parse_iso(value)
    if parsed is None:
        text = str(value or "").strip()
        return text or None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    msk = parsed.astimezone(timezone(timedelta(hours=3)))
    return msk.strftime("%d.%m %H:%M") + " (МСК)"


def _is_technical_comment(value) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    return any(marker in text for marker in _TECHNICAL_COMMENT_MARKERS)


def _requisites_markup(payload: dict) -> InlineKeyboardMarkup | None:
    url = _extract_payment_url(payload)
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть оплату", url=url)])
    rows.append([InlineKeyboardButton(text=kb.BTN_CANCEL, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _extract_payment_url(payload: dict) -> str | None:
    """Extract the Ratecore payment page URL from payload / presentation actions."""
    if not isinstance(payload, dict):
        return None
    requisites = payload.get("requisites") if isinstance(payload.get("requisites"), dict) else {}
    url = _public_http_url(
        payload.get("payment_url"),
        requisites.get("payment_url"),
        requisites.get("form_url"),
        requisites.get("qr_url"),
    )
    if url:
        return url
    presentation = payload.get("client_presentation") if isinstance(payload.get("client_presentation"), dict) else {}
    candidates: list[dict] = []
    if isinstance(presentation.get("actions"), list):
        candidates.extend(a for a in presentation["actions"] if isinstance(a, dict))
    for msg in presentation.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("actions"), list):
            candidates.extend(a for a in msg["actions"] if isinstance(a, dict))
    for action in candidates:
        candidate = _public_http_url(action.get("open_url"), action.get("url"))
        if candidate:
            return candidate
    return None


def pdf_redirect_markup(payment_url: str | None) -> InlineKeyboardMarkup | None:
    if not payment_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть страницу чека", url=payment_url)],
        ]
    )


def pdf_redirect_text(payment_id: str | None) -> str:
    pid = (payment_id or "").strip()
    lines = [
        "🔶 <b>PDF в этот чат слать не нужно.</b>",
        "",
        "Для способов с PDF чек уходит на страницу оплаты. Кнопка ниже.",
        "",
        "1️⃣ Откройте страницу по кнопке",
        "2️⃣ Впишите номер счета" + (f": <code>{html.escape(pid)}</code>" if pid else ""),
        "3️⃣ Приложите PDF и отправьте форму",
        "",
        "🛟 Файл из чата мы все равно передали куратору Store.",
    ]
    return "\n".join(lines)


def _fallback_requisites_text(payload: dict) -> str | None:
    requisites = payload.get("requisites") if isinstance(payload.get("requisites"), dict) else {}
    card = requisites.get("card_number") or payload.get("address")
    phone = requisites.get("phone_number") or requisites.get("phone")
    account = requisites.get("account") or requisites.get("account_number")
    pay_url = _public_http_url(
        payload.get("payment_url"),
        requisites.get("payment_url"),
        requisites.get("form_url"),
    )
    amount = payload.get("payer_amount") or payload.get("pay_amount") or requisites.get("amount") or payload.get("amount")
    if not any([card, phone, account, pay_url, amount]):
        return None

    lines = ["💳 <b>Счет PlayStation™Store</b>"]
    if amount:
        currency = payload.get("currency") or requisites.get("currency") or "₽"
        currency_text = "₽" if str(currency).upper() in {"RUB", "RUR", "₽"} else str(currency)
        lines.append(f"💰 Сумма счета: <code>{html.escape(_format_money(amount))} {html.escape(currency_text)}</code>")

    method_name = _method_title(payload, requisites)
    if method_name:
        lines.append(f"💳 Способ: {html.escape(method_name)}")

    if phone:
        lines.append(f"📱 Телефон СБП: <code>{html.escape(str(phone))}</code>")
    if card:
        lines.append(f"💳 Карта: <code>{html.escape(str(card))}</code>")
    if account:
        lines.append(f"🏦 Счёт: <code>{html.escape(str(account))}</code>")

    holder = requisites.get("card_holder") or requisites.get("holder") or requisites.get("recipient")
    if holder:
        lines.append(f"👤 Получатель: {html.escape(str(holder))}")
    bank = requisites.get("bank_name") or requisites.get("bank")
    if bank:
        lines.append(f"🏦 Банк: {html.escape(str(bank))}")

    comment = requisites.get("comment")
    if comment and not _is_technical_comment(comment):
        lines.append(f"📝 Комментарий к платежу: <code>{html.escape(str(comment))}</code>")

    memo = payload.get("memo") or payload.get("memo_tag")
    if memo:
        lines.append(f"🔖 Memo: <code>{html.escape(str(memo))}</code>")

    if pay_url:
        lines.append(f"🔗 Ссылка: {html.escape(pay_url)}")
    qr_payload = str(requisites.get("qr_payload") or "").strip()
    if qr_payload and qr_payload not in {str(pay_url or ""), str(phone or "")}:
        if qr_payload.startswith("http://") or qr_payload.startswith("https://"):
            lines.append(f"📷 QR: {html.escape(qr_payload)}")

    if not any([phone, card, account, pay_url]):
        lines.append("🔶 В этом ответе нет счета для перевода, только сумма и срок.")

    expires = _format_expires_msk(payload.get("expires_at") or payload.get("fixed_until") or requisites.get("expires_at"))
    if expires:
        lines.append(f"⏳ Оплатить до: <b>{html.escape(expires)}</b>")
    return "\n".join(lines)


async def create_and_start(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    user_id: int,
    username: str | None,
    state_data: dict,
    method_key: str,
    method_name: str,
    channel: str,
    message: Message | None = None,
) -> None:
    purpose = str(state_data.get("payment_purpose") or "order")
    if purpose == "chest_boost":
        try:
            amount = float(state_data.get("chest_boost_rub") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        percent = int(state_data.get("chest_boost_percent") or 0)
        product_name = f"Усиление сундука +{percent}%"
        city = None
        district = None
        treasure = "-"
    elif purpose == "balance_topup":
        try:
            amount = float(state_data.get("topup_amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        product_name = "Пополнение баланса"
        city = None
        district = None
        treasure = "-"
    else:
        product = state_data.get("product") or {}
        try:
            amount = float(product.get("price") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        product_name = product.get("name") or "Товар"
        city = state_data.get("city")
        district = state_data.get("district")
        if CatalogService.is_courier_job(product) or state_data.get("courier_job"):
            treasure = "Работа"
            district = district or "-"
        else:
            treasure = state_data.get("treasure_type") or "Тайник"

    if amount <= 0:
        raise RatecoreError("Сумма заказа не задана")

    order_id = new_order_id(user_id)
    callback_url = resolve_ratecore_callback_url() or None
    api = client()
    if channel == "crypto":
        payload = await api.create_crypto_payment(
            order_id=order_id,
            amount=amount,
            asset=method_key,
        )
    else:
        payload = await api.create_payment(
            order_id=order_id,
            amount=amount,
            method=method_key,
            callback_url=callback_url,
        )

    payment_id = payment_id_of(payload)
    status = payment_status_of(payload) or ("awaiting_payment" if _has_payment_requisites(payload) else "processing")
    expires_at = payload.get("expires_at") or payload.get("fixed_until")
    ttl_minutes = _ttl_minutes_from_expires(expires_at)
    # Extract receipt info from Ratecore response
    receipt_info = payload.get("receipt") or {}
    receipt_required = bool(receipt_info.get("required"))
    receipt_allows_upload = bool(
        receipt_info.get("capability", {}).get("allows_internal_collection")
    )
    
    order_data = {
        "type": RATECORE_ORDER_TYPE,
        "channel": channel,
        "purpose": purpose,
        "product": product_name,
        "price": amount,
        "payer_amount": payload.get("payer_amount") or payload.get("pay_amount") or amount,
        "method": method_key,
        "method_name": method_name,
        "treasure_type": treasure,
        "city": city,
        "district": district,
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_minutes": ttl_minutes,
        "expires_at": expires_at,
        "order_id": payload.get("order_id") or order_id,
        "payment_id": payment_id,
        "status": status,
        "state_version": payload.get("state_version"),
        "boost_percent": int(state_data.get("chest_boost_percent") or 0) if purpose == "chest_boost" else 0,
        "admin_paid_notified": False,
        "requisites_shown": False,
        "bot_id": getattr(bot, "id", None),
        "message_id": getattr(message, "message_id", None),
        "receipt_required": receipt_required,
        "receipt_allows_upload": receipt_allows_upload,
        "receipt_uploaded": False,
        "payment_url": _extract_payment_url(payload),
        "kind": (
            "courier_job"
            if CatalogService.is_courier_job(state_data.get("product")) or state_data.get("courier_job")
            else None
        ),
    }
    if payment_id:
        _schedule_status_poll(payment_id, STATUS_POLL_REQUISITES_MIN_SEC)
    UserService.set_active_order(user_id, order_data)
    fsm = get_user_fsm(bot, dispatcher, user_id)
    if purpose == "balance_topup":
        await fsm.set_state(TopUpState.waiting_for_receipt)
    else:
        await fsm.set_state(OrderState.waiting_for_receipt)
    await fsm.update_data(
        payment_id=payment_id,
        payment_order_id=order_data["order_id"],
        payment_purpose=purpose,
    )

    _log_payment_snapshot(payload, source="create")
    immediate_status = incoming_payment_status(payload)
    if immediate_status in TERMINAL_FAIL:
        await apply_status_payload(bot, dispatcher, user_id, payload, username=username)
        return

    try:
        await notify_admins(
            bot,
            admin_created_text(user_id, username, order_data, payload),
            reply_markup=admin_created_keyboard(user_id, order_data),
        )
    except Exception:
        logger.exception("Failed to notify admins about created Ratecore order for %s", user_id)

    if immediate_status in {"requisites_ready", "awaiting_payment", "action_required"}:
        await apply_status_payload(bot, dispatcher, user_id, payload, username=username)
        return

    loader_text = requisites_search_loader_text(0)
    loader_message_id = getattr(message, "message_id", None)
    if message is not None:
        try:
            await message.edit_text(loader_text)
        except Exception:
            sent = await bot.send_message(user_id, loader_text, reply_markup=kb.get_base_keyboard())
            loader_message_id = sent.message_id
    else:
        sent = await bot.send_message(user_id, loader_text, reply_markup=kb.get_base_keyboard())
        loader_message_id = sent.message_id
    if payment_id and loader_message_id:
        patch_order(user_id, message_id=loader_message_id)
        _start_requisites_loader(bot, user_id, payment_id, int(loader_message_id))

    await apply_status_payload(bot, dispatcher, user_id, payload, username=username)
    if channel != "crypto":
        return
    order = UserService.get_active_order(user_id)
    if (
        payment_id
        and is_ratecore_order(order)
        and not order.get("requisites_shown")
        and str(order.get("status") or "") not in TERMINAL_FAIL
        and str(order.get("status") or "") != PAID_WAITING_ADMIN
    ):
        try:
            payload = await api.wait_crypto_payment(payment_id, timeout=WAIT_ENDPOINT_TIMEOUT_SEC)
            _log_payment_snapshot(payload, source="wait")
            still_waiting = not bool((UserService.get_active_order(user_id) or {}).get("requisites_shown"))
            _schedule_status_poll(payment_id, _poll_delay_from_payload(payload, waiting_requisites=still_waiting))
            await apply_status_payload(bot, dispatcher, user_id, payload, username=username)
        except RatecoreError as exc:
            if exc.status_code == 429:
                delay = exc.retry_after_seconds or STATUS_POLL_429_BACKOFF_SEC
                _schedule_status_poll(payment_id, max(delay, STATUS_POLL_429_BACKOFF_SEC))
            elif exc.status_code not in {404, 405}:
                logger.warning("Ratecore wait failed for %s: %s", payment_id, exc)


def _ttl_minutes_from_expires(expires_at) -> int:
    parsed = _parse_iso(expires_at)
    if parsed is None:
        return 20
    now = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = parsed - now
    minutes = int(delta.total_seconds() // 60)
    return max(5, min(minutes + 2, 180))


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _late_watch_until_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=LATE_PAYMENT_WATCH_MINUTES)).isoformat()


def is_late_watch_fresh(order: dict | None) -> bool:
    if not isinstance(order, dict):
        return False
    parsed = _parse_iso(order.get("late_watch_until"))
    if parsed is None:
        return True
    now = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= now


def _matching_late_order(user_id: int, incoming_id: str | None, payload: dict) -> dict | None:
    late = UserService.get_late_ratecore_order(user_id)
    if not is_ratecore_order(late) or not is_late_watch_fresh(late):
        return None
    candidates = {
        str(late.get("payment_id") or ""),
        str(late.get("order_id") or ""),
    }
    incoming = str(incoming_id or "")
    order_id = str(payload.get("order_id") or payload.get("external_reference") or "")
    if incoming and incoming in candidates:
        return late
    if order_id and order_id in candidates:
        return late
    if not incoming and not order_id:
        return late
    return None


def _status_rank(status: str) -> int:
    order = {
        "processing": 1,
        "queued": 1,
        "created": 1,
        "action_required": 2,
        "awaiting_payment": 3,
        "requisites_ready": 3,
        PAID_WAITING_ADMIN: 5,
        "paid": 5,
        "completed": 5,
        "expired": 6,
        "failed": 6,
        "cancelled": 6,
    }
    return order.get(status, 0)


async def apply_status_payload(
    bot: Bot,
    dispatcher: Dispatcher,
    user_id: int,
    payload: dict,
    *,
    username: str | None = None,
    event: str | None = None,
    event_id: str | None = None,
) -> None:
    async with _lock_for(user_id):
        await _apply_status_locked(
            bot,
            dispatcher,
            user_id,
            payload,
            username=username,
            event=event,
            event_id=event_id,
        )


async def _apply_status_locked(
    bot: Bot,
    dispatcher: Dispatcher,
    user_id: int,
    payload: dict,
    *,
    username: str | None = None,
    event: str | None = None,
    event_id: str | None = None,
) -> None:
    if event_id:
        if event_id in _processed_events:
            return
        _processed_events.add(event_id)
        if len(_processed_events) > 5000:
            _processed_events.clear()

    order = UserService.get_active_order(user_id)
    incoming_id = payment_id_of(payload)
    raw_status = incoming_payment_status(payload, event)
    if not raw_status:
        return

    if is_ratecore_order(order):
        if incoming_id and order.get("payment_id") and str(incoming_id) != str(order.get("payment_id")):
            return
    else:
        late = _matching_late_order(user_id, incoming_id, payload)
        if raw_status not in {"paid", "completed"} or not late:
            return
        if order:
            logger.warning(
                "Late Ratecore payment %s arrived but user %s already has another order",
                incoming_id or late.get("payment_id"),
                user_id,
            )
            try:
                text = admin_paid_text(
                    user_id,
                    username or UserService.get_username(user_id),
                    late,
                    payload,
                )
                await notify_admins(
                    bot,
                    text + "\n🔶 У пользователя уже есть другая активная заявка. Проверьте вручную.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="💬 Чат с клиентом", callback_data=f"admin_chat_{user_id}")],
                        ]
                    ),
                )
            except Exception:
                logger.exception("Failed to notify admins about conflicting late payment for %s", user_id)
            UserService.clear_late_ratecore_order(user_id)
            _clear_status_poll(incoming_id or late.get("payment_id"))
            return
        order = dict(late)
        UserService.clear_late_ratecore_order(user_id)
        UserService.set_active_order(user_id, order)

    current = str(order.get("status") or "")
    financials = payload.get("financials") if isinstance(payload.get("financials"), dict) else {}
    credited = financials.get("settlement_credit_usdt_amount") or payload.get("credited_usdt_amount")
    if current == PAID_WAITING_ADMIN and raw_status in {"paid", "completed", "processing"}:
        if credited and not order.get("credited_usdt"):
            patch_order(user_id, credited_usdt=credited)
            await notify_admins(
                bot,
                f"🏦 Уточнение по заявке <code>{html.escape(str(order.get('payment_id') or ''))}</code>\n"
                f"На панель зачислено: <b>{html.escape(str(credited))} USDT</b>",
            )
        return
    if _status_rank(raw_status) < _status_rank(current) and current == PAID_WAITING_ADMIN:
        return

    patch = {
        "status": current,
        "payer_amount": payload.get("payer_amount") or payload.get("pay_amount") or order.get("payer_amount"),
        "expires_at": payload.get("expires_at") or payload.get("fixed_until") or order.get("expires_at"),
        "state_version": payload.get("state_version") or order.get("state_version"),
    }
    if credited:
        patch["credited_usdt"] = credited
    if incoming_id:
        patch["payment_id"] = incoming_id
    payment_url = _extract_payment_url(payload)
    if payment_url and not order.get("payment_url"):
        patch["payment_url"] = payment_url

    if raw_status in {"requisites_ready", "awaiting_payment", "action_required"}:
        patch["status"] = raw_status
        ttl = _ttl_minutes_from_expires(patch["expires_at"])
        patch["ttl_minutes"] = ttl
        UserService.set_active_order(user_id, {**order, **patch})
        if order.get("requisites_shown") or order.get("requisites_delivery_error"):
            UserService.set_active_order(user_id, {**order, **patch})
            return
        _stop_requisites_loader(order.get("payment_id") or incoming_id)
        loader_mid = order.get("message_id")
        # Удаляем сообщение "Ищем реквизиты" - оно больше не нужно
        if loader_mid:
            try:
                await bot.delete_message(chat_id=user_id, message_id=int(loader_mid))
            except Exception:
                pass
        try:
            shown = await send_client_presentation(bot, user_id, payload)
        except Exception as exc:
            if _is_chat_unreachable(exc):
                logger.warning("Cannot deliver Ratecore presentation to %s: %s", user_id, exc)
                patch_order(user_id, requisites_delivery_error="chat_unreachable")
                return
            logger.exception("Failed to send Ratecore presentation to %s", user_id)
            shown = False
        if not shown:
            pid = str(order.get("payment_id") or incoming_id or "")
            mid = order.get("message_id")
            if pid and mid:
                _start_requisites_loader(bot, user_id, pid, int(mid))
            return
        patch_order(user_id, requisites_shown=True)
        # Don't show wait message for PDF methods - user needs to upload receipt
        method_key = str(payload.get("method") or "").lower()
        if "pdf" not in method_key:
            try:
                await bot.send_message(user_id, USER_WAIT_TEXT, reply_markup=kb.get_base_keyboard())
            except Exception as exc:
                if _is_chat_unreachable(exc):
                    logger.warning("Telegram chat unreachable for wait hint %s: %s", user_id, exc)
                else:
                    logger.exception("Failed to send wait hint to %s", user_id)
        try:
            ready_order = UserService.get_active_order(user_id) or {**order, **patch, "requisites_shown": True}
            await notify_admins(
                bot,
                admin_requisites_ready_text(
                    user_id,
                    username or UserService.get_username(user_id),
                    ready_order,
                    payload,
                ),
                reply_markup=admin_created_keyboard(user_id, ready_order),
            )
        except Exception:
            logger.exception("Failed to notify admins that requisites were shown to %s", user_id)
        return

    if raw_status in {"paid", "completed"}:
        patch["status"] = PAID_WAITING_ADMIN
        _clear_status_poll(incoming_id or order.get("payment_id"))
        updated = {**order, **patch, "admin_paid_notified": True}
        UserService.set_active_order(user_id, updated)
        if not order.get("admin_paid_notified"):
            try:
                paid_text = (
                    USER_COURIER_PAID_TEXT
                    if updated.get("kind") == "courier_job"
                    or CatalogService.is_courier_job(updated.get("product"))
                    else build_order_fulfillment_wait_text()
                )
                await bot.send_message(user_id, paid_text, reply_markup=kb.get_base_keyboard())
            except Exception:
                logger.exception("Failed to tell user %s that payment arrived", user_id)
            text = admin_paid_text(user_id, username or UserService.get_username(user_id), updated, payload)
            await notify_admins(bot, text, reply_markup=admin_action_keyboard(user_id, updated))
        return

    if raw_status in TERMINAL_FAIL:
        shown = bool(order.get("requisites_shown"))
        _stop_requisites_loader(order.get("payment_id") or incoming_id)
        # Удаляем сообщение "Ищем реквизиты" если оно было
        loader_mid = order.get("message_id")
        if loader_mid and not shown:
            try:
                await bot.delete_message(chat_id=user_id, message_id=int(loader_mid))
            except Exception:
                pass
        if shown and raw_status == "expired":
            payment_id = str(incoming_id or order.get("payment_id") or "")
            UserService.set_late_ratecore_order(
                user_id,
                {
                    **order,
                    "status": "expired",
                    "late_watch_until": _late_watch_until_iso(),
                },
            )
            UserService.remove_active_order(user_id)
            if payment_id:
                _schedule_status_poll(payment_id, STATUS_POLL_INTERVAL_SEC)
            await send_user_to_start(
                bot,
                dispatcher,
                user_id,
                "⏳ <b>Счет Store истек</b>\n"
                "Перевод не пришел, поэтому заказ закрыт.\n"
                "👇 Вы снова на главной.",
            )
            return
        _clear_status_poll(incoming_id or order.get("payment_id"))
        UserService.remove_active_order(user_id)
        if not shown:
            try:
                await notify_admins(
                    bot,
                    admin_requisites_failed_text(
                        user_id,
                        username or UserService.get_username(user_id),
                        order,
                    ),
                    reply_markup=admin_created_keyboard(user_id, order),
                )
            except Exception:
                logger.exception("Failed to notify admins that requisites failed for %s", user_id)
        restored = await restore_payment_methods(
            bot,
            dispatcher,
            user_id,
            order,
            failed_without_requisites=not shown,
        )
        if not restored:
            await send_user_to_start(bot, dispatcher, user_id, USER_FAIL_TEXT)
        return

    if raw_status in {"processing", "queued", "created"}:
        patch["status"] = "processing"
        UserService.set_active_order(user_id, {**order, **patch})
        logger.info(
            "Ratecore payment %s still %s, waiting for requisites",
            incoming_id or order.get("payment_id"),
            raw_status,
        )
        return

    logger.warning(
        "Ratecore payment %s has unhandled status %s next_action=%s",
        incoming_id or order.get("payment_id"),
        raw_status,
        payload.get("next_action"),
    )


async def refresh_order(bot: Bot, dispatcher: Dispatcher, user_id: int, order: dict) -> None:
    payment_id = str(order.get("payment_id") or "").strip()
    if not payment_id or str(order.get("status") or "") == PAID_WAITING_ADMIN:
        return
    if not _status_poll_is_due(payment_id):
        return
    waiting_requisites = not bool(order.get("requisites_shown"))
    poll_fallback = STATUS_POLL_REQUISITES_SEC if waiting_requisites else STATUS_POLL_INTERVAL_SEC
    _schedule_status_poll(payment_id, poll_fallback)
    api = client()
    try:
        if order.get("channel") == "crypto":
            payload = await api.get_crypto_payment(payment_id)
        else:
            payload = await api.get_payment(payment_id)
    except RatecoreError as exc:
        if exc.status_code == 429:
            delay = exc.retry_after_seconds or STATUS_POLL_429_BACKOFF_SEC
            _schedule_status_poll(payment_id, max(delay, STATUS_POLL_429_BACKOFF_SEC))
            logger.warning(
                "Ratecore status poll throttled for %s, next try in %.0fs",
                payment_id,
                max(delay, STATUS_POLL_429_BACKOFF_SEC),
            )
        else:
            logger.warning("Ratecore status poll failed for %s: %s", payment_id, exc)
        return
    _log_payment_snapshot(payload, source="poll")
    _schedule_status_poll(payment_id, _poll_delay_from_payload(payload, waiting_requisites=waiting_requisites))
    username = None
    try:
        username = UserService.get_username(user_id)
    except Exception:
        username = None
    await apply_status_payload(bot, dispatcher, user_id, payload, username=username)


async def poll_active_payments(bot: Bot, dispatcher: Dispatcher) -> None:
    watched = list(UserService.get_all_active_orders()) + list(UserService.get_all_late_ratecore_orders())
    for item in watched:
        order = item.get("order") or {}
        if not is_ratecore_order(order):
            continue
        if str(order.get("status") or "") == PAID_WAITING_ADMIN:
            continue
        try:
            user_id = int(item["user_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if not _order_owned_by_bot(user_id, order, bot):
            continue
        payment_id = str(order.get("payment_id") or "").strip()
        if order.get("late_watch_until") and not is_late_watch_fresh(order):
            UserService.clear_late_ratecore_order(user_id)
            _clear_status_poll(payment_id)
            continue
        if payment_id and payment_id not in _next_status_poll_at:
            _schedule_status_poll(payment_id, 0)
        try:
            await refresh_order(bot, dispatcher, user_id, order)
        except Exception:
            logger.exception("Ratecore poll failed for user %s", user_id)


async def handle_callback_event(bot: Bot, dispatcher: Dispatcher, envelope: dict) -> None:
    event = str(envelope.get("event") or "")
    event_id = str(envelope.get("event_id") or "")
    payload = envelope.get("payment") if isinstance(envelope.get("payment"), dict) else {}
    payment_id = payment_id_of(payload) or str(payload.get("public_id") or "")
    order_id = str(payload.get("order_id") or payload.get("external_reference") or "")
    found = find_user_by_payment_id(payment_id) or find_user_by_payment_id(order_id)
    if not found:
        logger.warning("Ratecore callback for unknown payment %s / %s", payment_id, order_id)
        return
    user_id, _order = found
    username = None
    try:
        username = UserService.get_username(user_id)
    except Exception:
        username = None
    await apply_status_payload(
        bot,
        dispatcher,
        user_id,
        payload,
        username=username,
        event=event,
        event_id=event_id or None,
    )


async def sandbox_complete_for_user(bot: Bot, dispatcher: Dispatcher, user_id: int) -> str:
    if not is_ratecore_sandbox():
        raise RatecoreError("Sandbox complete недоступен в production")
    order = UserService.get_active_order(user_id)
    if not is_ratecore_order(order):
        raise RatecoreError("Нет активной заявки Ratecore")
    payment_id = str(order.get("payment_id") or "")
    if not payment_id:
        raise RatecoreError("Нет payment_id")
    payload = await client().sandbox_complete(payment_id)
    await apply_status_payload(bot, dispatcher, user_id, payload)
    return payment_id
