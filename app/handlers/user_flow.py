from typing import Any, Awaitable, Callable, Dict

from aiogram import Router, F, Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, BufferedInputFile, TelegramObject
from aiogram.filters import Command, ChatMemberUpdatedFilter, KICKED, MEMBER, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
import asyncio # Добавляем импорт asyncio
import html
import logging
from io import BytesIO
import os
import random
import re
import uuid
from urllib.parse import quote

from app.states import OrderState, ReviewState, TopUpState, CouponState, DisputeState
from app.services.geo import GeoService, MIN_CITY_POPULATION
from app.services.catalog import COURIER_JOB_NOTICE, CatalogService
from app.services.blacklist import BlacklistService
from app.services.users import UserService, CANCEL_BAN_LIMIT, CANCEL_CRITICAL_COUNT, CANCEL_BAN_HOURS
from app.services.reviews import ReviewsService
from app.services.crypto import CryptoService
from app.services.payments import (
    PAID_WAITING_ADMIN,
    USER_WAIT_TEXT,
    create_and_start as start_ratecore_payment,
    is_no_requisites_failure,
    is_ratecore_order,
    load_catalog,
    notify_admins,
    admin_action_keyboard,
    admin_created_keyboard,
    admin_paid_text,
    admin_requisites_failed_text,
    admin_user_cancelled_text,
    build_balance_order_paid_text,
    checkout_base_amount,
    pdf_redirect_markup,
    pdf_redirect_text,
    refresh_order,
    restore_payment_methods,
)
from app.services.ratecore import (
    RatecoreError,
    find_nested_group,
    group_payment_methods,
    iter_payment_method_nodes,
)
from app.services.settings import SettingsService
from app.services.analytics import ProductInterestService
from app.services.chest import ChestService, boost_percent_for_rub
from app.services.roulette import RouletteService
from app.services.referral_race import ReferralRaceService, PRIZES as RACE_PRIZES
from app.services.coupons import CouponService
from app.services.disputes import build_shop_rules_text
from app.services.referrals import complete_product_purchase, notify_bonus_cap, notify_referrer_bonus
from app.services.loyalty import LoyaltyService, notify_purchase_loyalty
from app.services.bot_stats import BotStatsService
from app.services.captcha import answers_match, create_challenge
import app.keyboards as kb
from app.config import ADMIN_ID, ADMIN_IDS, is_admin
from datetime import datetime, timedelta
from app.utils import get_now_msk, send_user_to_start, send_home_to_chat

router = Router()


@router.message.middleware()
@router.callback_query.middleware()
async def _remember_valley_user(handler, event, data):
    user = getattr(event, "from_user", None)
    if user is not None:
        kb.remember_menu_user(user.id)
    return await handler(event, data)
logger = logging.getLogger(__name__)
REVIEWS_ALL_CITIES_PAGE_SIZE = 2

_PENDING_CANCEL_TEXTS = set(kb.CANCEL_TEXT_ALIASES)
_PENDING_MENU_TEXTS = {
    kb.BTN_BUY,
    kb.BTN_PRICE,
    kb.BTN_BUY_LEGACY,
    kb.BTN_PROFILE,
    kb.BTN_PROFILE_LEGACY,
    kb.BTN_CONTACTS,
    kb.BTN_CHEST,
    kb.BTN_ROULETTE,
    kb.BTN_COUPON,
    kb.BTN_ALL_CITIES_REVIEWS,
    kb.BTN_SUPPORT,
    kb.BTN_SUPPORT_CHAT,
    kb.BTN_RULES,
    kb.BTN_DISPUTE,
    kb.BTN_MISSIONS,
    kb.BTN_GUIDE,
    kb.BTN_BACK,
    kb.BTN_ORDER_BACK,
    kb.BTN_DISMISS,
    kb.BTN_OTHER_CITY,
    "Купить",
    "🛒 Купить",
    "👤 Профиль",
    "Профиль",
    "⭐️ Отзывы",
    "В магазин",
    "КАБИНЕТ",
    "СВЯЗЬ",
    "БОНУС",
    "КУПОН",
    "СИГНАЛЫ",
    "СПОР",
    "УСЛОВИЯ ПОЛЬЗОВАНИЯ",
    "вызвать оператора",
    "мануал",
    "назад",
    "к оплате",
    "другой город",
    "закрыть канал",
    "Поддержка и правила",
    "❓ Ненаходы и поддержка",
    "Ненаходы и поддержка",
    "📜 Правила",
    "⚖️ Создать Диспут",
    "Создать Диспут",
    "Создать диспут",
    "❓ Поддержка",
    "Миссии",
    "🎯 Миссии",
}
_PENDING_ALLOWED_CALLBACKS = {
    "order_back_to_pay",
    "nav_cancel",
    "nav_back_products",
    "nav_back_city",
    "menu_buy",
    "menu_price",
    "ignore",
}
_PENDING_CATALOG_PREFIXES = ("prod_", "mini_prod_", "price_item_")
_PAID_LOCK_STATUSES = {PAID_WAITING_ADMIN, "paid", "completed"}


def _is_pending_payment_lock(order: dict | None, state_name: str | None) -> bool:
    if state_name in {OrderState.waiting_for_receipt.state, TopUpState.waiting_for_receipt.state}:
        return True
    if _is_paid_pending_lock(order):
        return True
    return is_ratecore_order(order)


def _is_paid_pending_lock(order: dict | None) -> bool:
    if not isinstance(order, dict):
        return False
    return str(order.get("status") or "") in _PAID_LOCK_STATUSES


def _is_pending_catalog_callback(callback_data: str) -> bool:
    if callback_data in _PENDING_ALLOWED_CALLBACKS:
        return True
    return callback_data.startswith(_PENDING_CATALOG_PREFIXES)


def _is_pending_cancel_intent(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        return (event.text or "").strip() in _PENDING_CANCEL_TEXTS
    if isinstance(event, CallbackQuery):
        return (event.data or "") == "nav_cancel"
    return False


PENDING_ORDER_NOTICE = (
    "🕒 <b>Заказ уже в обработке</b>\n\n"
    "Новый заказ откроется после завершения текущего.\n"
    "Вернуться на главную можно кнопкой <b>↩️ На главную</b>."
)
PAID_ORDER_NOTICE = (
    "🟢 <b>Платеж уже зачислен</b>\n\n"
    "Этот заказ закрыт для отмены."
)


async def _send_pending_order_notice(target: Message, order: dict | None = None) -> None:
    if _is_paid_pending_lock(order):
        await target.answer(PAID_ORDER_NOTICE, reply_markup=kb.get_base_keyboard())
        return
    await target.answer(PENDING_ORDER_NOTICE, reply_markup=kb.get_pending_order_warning_inline_keyboard())


class PendingOrderLockMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or is_admin(user.id):
            return await handler(event, data)

        state: FSMContext | None = data.get("state")
        state_name = await state.get_state() if state else None
        order = UserService.get_active_order(user.id)
        if not _is_pending_payment_lock(order, state_name):
            return await handler(event, data)

        if _is_pending_cancel_intent(event) and not _is_paid_pending_lock(order):
            return await handler(event, data)

        if isinstance(event, Message):
            text = (event.text or "").strip()
            if text in {kb.BTN_CLOSE_CHAT, "закрыть канал"}:
                return await handler(event, data)
            # Allow photos/documents through if user is waiting for receipt
            if state_name in {OrderState.waiting_for_receipt.state, TopUpState.waiting_for_receipt.state}:
                if event.photo or event.document:
                    return await handler(event, data)
            if event.photo or event.document or text:
                try:
                    await event.delete()
                except Exception:
                    pass
                await _send_pending_order_notice(event, order)
                return None
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            if _is_pending_catalog_callback(callback_data) and not _is_paid_pending_lock(order):
                return await handler(event, data)
            if callback_data == "order_back_to_pay":
                return await handler(event, data)
            try:
                await event.answer(
                    "Заявка уже обрабатывается." if _is_paid_pending_lock(order) else "Заявка уже создана. Можно отменить кнопкой внизу.",
                    show_alert=True,
                )
            except TelegramBadRequest:
                pass
            if event.message:
                await _send_pending_order_notice(event.message, order)
            return None

        return await handler(event, data)


class LastBotMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        bot = data.get("bot")
        if user is not None and bot is not None:
            try:
                UserService.touch_last_bot(user.id, bot.id)
            except Exception:
                logger.exception("failed to remember last bot for %s", getattr(user, "id", None))
        return await handler(event, data)


router.message.middleware(PendingOrderLockMiddleware())
router.callback_query.middleware(PendingOrderLockMiddleware())
router.message.middleware(LastBotMiddleware())
router.callback_query.middleware(LastBotMiddleware())


def _reply_panel_action(text: str | None) -> str | None:
    raw = " ".join(str(text or "").strip().split())
    if not raw:
        return None
    head = raw.split("·", 1)[0].strip().casefold()
    balance_heads = {
        kb.BTN_BALANCE.casefold(),
        kb.BTN_BALANCE_LEGACY.casefold(),
        "кошелек",
        "баланс",
    }
    purchase_heads = {
        kb.BTN_PURCHASES.casefold(),
        kb.BTN_PURCHASES_LEGACY.casefold(),
        "история заказов",
        "покупки",
    }
    if head in balance_heads or raw.casefold().startswith(kb.BTN_BALANCE.casefold()):
        return "balance"
    if head in purchase_heads or raw.casefold().startswith(kb.BTN_PURCHASES.casefold()):
        return "purchases"
    return None


@router.message(F.text.func(lambda text: _reply_panel_action(text) == "balance"))
async def valley_balance(message: Message):
    await _show_balance_card(message, message.from_user.id)


@router.message(F.text.func(lambda text: _reply_panel_action(text) == "purchases"))
async def valley_purchases(message: Message):
    await _show_purchases_card(message, message.from_user.id)


class _SafeTemplateDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _render_manual_payment_template(template: str | None, context: dict) -> str | None:
    if not template:
        return None
    try:
        return template.format_map(_SafeTemplateDict(context))
    except Exception:
        return template


_DEFAULT_TOPUP_CARD_TEMPLATE = (
    "💳 <b>Счет кошелька PlayStation™Store</b>\n\n"
    "💵 К зачислению: <b>{amount} руб.</b>\n"
    "📝 Карта: <code>{card}</code>\n\n"
    "Переведите ровно эту сумму на карту.\n\n"
    "━━━━━━━━━━━━━━\n"
    "📩 После перевода пришлите PDF или снимок экрана\n"
    "━━━━━━━━━━━━━━\n\n"
    "⏳ Счет открыт до <b>{expires_at}</b> (МСК)."
)


def build_topup_card_payment_text(card: str, context: dict | None = None) -> str:
    data = dict(context or {})
    card_value = str(card or "").strip()
    try:
        amount_raw = data.get("amount")
        amount_display = f"{float(amount_raw):.0f}" if amount_raw not in (None, "") else ""
    except (TypeError, ValueError):
        amount_display = str(data.get("amount") or "")
    payload = {
        "user_id": data.get("user_id", ""),
        "amount": amount_display,
        "expires_at": data.get("expires_at") or "-",
        "city": data.get("city") or "",
        "district": data.get("district") or "",
        "product": data.get("product") or "",
        "card": html.escape(card_value),
    }
    template = SettingsService.get_manual_payment_template() or _DEFAULT_TOPUP_CARD_TEMPLATE
    rendered = _render_manual_payment_template(template, payload) or _DEFAULT_TOPUP_CARD_TEMPLATE.format_map(
        _SafeTemplateDict(payload)
    )
    if "{card}" not in str(template) and card_value and card_value not in rendered:
        rendered = f"{rendered}\n\n📝 Карта: <code>{html.escape(card_value)}</code>"
    return rendered


def _format_rub_amount(value: float | int | str) -> str:
    try:
        amount = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    return f"{amount:,}".replace(",", " ")


def _format_order_sum_line(total, commission: float | int = 0) -> str:
    try:
        total_value = int(round(float(total)))
    except (TypeError, ValueError):
        total_value = total
    try:
        commission_value = int(round(float(commission or 0)))
    except (TypeError, ValueError):
        commission_value = 0
    if commission_value > 0:
        try:
            base_value = int(round(float(total) - commission_value))
        except (TypeError, ValueError):
            base_value = total
        return f"{total_value} руб. ({base_value}+{commission_value} комиссия)"
    return f"{total_value} руб."


_RF_PAYMENT_USER_INSTRUCTIONS = (
    "\n\n━━━━━━━━━━━━━━\n"
    "📩 После перевода пришлите подтверждение\n"
    "Подойдет PDF или снимок экрана\n"
    "━━━━━━━━━━━━━━\n\n"
    "⚡️ Чек лучше отправить сразу: касса Store смотрит его быстро\n\n"
    "⏳ <b>На перевод есть 12 минут</b>\n"
    "Если деньги не придут вовремя, счет закроется сам."
)


def _build_card_rf_payment_text(
    amount,
    card_number: str,
    payment_request_id: str,
    expires_at: str,
    commission: float | int = 0,
    product_name: str | None = None,
    *,
    user_id: int | str | None = None,
    username: str | None = None,
    city: str | None = None,
    district: str | None = None,
    treasure_type: str | None = None,
    include_instructions: bool = False,
) -> str:
    username_display = username or "anon"
    city_display = city or "Не выбран"
    district_display = district or "Не выбран"
    product_display = product_name or "Неизвестно"
    type_display = treasure_type or "Не выбран"
    text = (
        "💳 <b>Заявка на оплату картой РФ</b>\n"
        f"👤 Пользователь: {user_id} (@{html.escape(str(username_display))})\n"
        f"🌇 Город: {html.escape(str(city_display))}\n"
        f"🏙 Район: {html.escape(str(district_display))}\n"
        f"📦 Товар: {html.escape(str(product_display))}\n"
        f"🏷 Тип: {html.escape(str(type_display))}\n"
        f"💵 Сумма: {_format_order_sum_line(amount, commission)}\n"
        f"📝 Номер заявки: <code>{html.escape(str(payment_request_id))}</code>\n"
        f"📝 Карта: <code>{html.escape(str(card_number))}</code>\n"
        f"⏳ Действительно до: {expires_at} (МСК)"
    )
    if include_instructions:
        return text + _RF_PAYMENT_USER_INSTRUCTIONS
    return text


YOOMONEY_TTL_MINUTES = 25


def _is_yoomoney_order(order: dict | None) -> bool:
    return bool(order and order.get("type") == "yoomoney")


def _pick_rotating_item(items: list[str], last: str | None) -> str | None:
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    candidates = [item for item in items if item != last]
    if not candidates:
        candidates = list(items)
    return random.choice(candidates)


def _build_yoomoney_payment_text(
    amount,
    wallet: str,
    payment_request_id: str,
    expires_at: str,
    product_name: str | None = None,
    commission: float | int = 0,
) -> str:
    amount_display = _format_rub_amount(amount)
    commission_line = ""
    try:
        commission_value = float(commission)
    except (TypeError, ValueError):
        commission_value = 0
    if commission_value > 0:
        commission_line = f"💜 <b>КОМИССИЯ: {_format_rub_amount(commission_value)} ₽</b>\n\n"
    product_line = ""
    if product_name:
        product_line = f"🛍 <b>ВЫ ПОКУПАЕТЕ:</b> {html.escape(product_name)}\n\n"
    return (
        f"💳 <b>КАССА STORE: {amount_display} ₽</b>\n\n"
        f"{commission_line}"
        f"{product_line}"
        f"⏳ <b>На перевод есть {YOOMONEY_TTL_MINUTES} минут</b>\n"
        "Если деньги не придут вовремя, счет закроется сам.\n\n"
        "💜 <b>КОШЕЛЕК ЮMoney:</b>\n\n"
        f"<code>{html.escape(wallet)}</code>\n\n"
        "🔶 Перевод только <b>ЮMoney на ЮMoney</b>. С карты и СБП этот кошелек Store не примет.\n\n"
        "🆔 <b>НОМЕР ЗАЯВКИ:</b>\n"
        f"<code>{payment_request_id}</code>\n\n"
        f"⏰ <b>ЗАЯВКА АКТИВНА ДО: {expires_at} (МСК)</b>\n\n"
        "━━━━━━━━━━━━━━\n"
        "📩 После перевода пришлите подтверждение\n"
        "Подойдет PDF или снимок экрана\n"
        "━━━━━━━━━━━━━━\n\n"
        "⚡️ Чек проверяется быстро, поэтому лучше отправить его сразу после оплаты"
    )

def _normalize_product_base_name(name: str) -> str:
    if not name:
        return ""
    cleaned = name
    cleaned = re.sub(r"\s*(\d+(?:[\.,]\d+)?)\s*г\.?", "", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()

def _extract_base_weight_grams(name: str) -> float:
    if not name:
        return 1.0
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*г", name)
    if not match:
        return 1.0
    raw = match.group(1).replace(",", ".")
    try:
        value = float(raw)
        return value if value > 0 else 1.0
    except ValueError:
        return 1.0


def _extract_product_weight_label(name: str) -> str:
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*[🅶г]", name or "", flags=re.IGNORECASE)
    if not match:
        return "не указан"
    return f"{match.group(1).replace(',', '.')} г"


def _extract_product_piece_label(name: str) -> str | None:
    match = re.search(r"(\d+)\s*шт", name or "", flags=re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1)} шт"


def _parse_index_command(text: str | None, *commands: str) -> int | None:
    raw = " ".join((text or "").strip().split())
    if not raw:
        return None
    for command in commands:
        match = re.fullmatch(rf"/?{re.escape(command)}\s*(\d+)", raw, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def _matches_command(text: str | None, *commands: str) -> bool:
    raw = " ".join((text or "").strip().split()).lower()
    if not raw:
        return False
    return any(raw == command.lower() or raw == f"/{command.lower()}" for command in commands)


def _parse_payment_command(text: str | None) -> str | None:
    raw = " ".join((text or "").strip().split()).lower()
    if not raw:
        return None
    match = re.fullmatch(r"/?(?:оплата|pay)\s+(.+)", raw)
    if not match:
        return None
    mapping = {
        "баланс": "balance",
        "balance": "balance",
        "карта": "card_rf",
        "card": "card_rf",
        "юмани": "yoomoney",
        "юmoney": "yoomoney",
        "yoomoney": "yoomoney",
        "yumoney": "yoomoney",
        "зарубеж": "foreign",
        "зарубежная": "foreign",
        "foreign": "foreign",
        "btc": "btc",
        "bitcoin": "btc",
        "биткоин": "btc",
        "ltc": "ltc",
        "litecoin": "ltc",
        "лайткоин": "ltc",
    }
    return mapping.get(match.group(1).strip())


async def _remember_menu_message(state: FSMContext, message: Message | None):
    if not message:
        return
    await state.update_data(active_menu_chat_id=message.chat.id, active_menu_message_id=message.message_id)


async def _delete_previous_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    prev_chat_id = data.get("active_menu_chat_id")
    prev_message_id = data.get("active_menu_message_id")
    if not prev_chat_id or not prev_message_id:
        return
    if prev_chat_id != message.chat.id:
        return
    try:
        await message.bot.delete_message(prev_chat_id, prev_message_id)
    except TelegramBadRequest:
        pass
    await state.update_data(active_menu_chat_id=None, active_menu_message_id=None)


async def _replace_menu_message(message: Message, state: FSMContext, text: str, reply_markup=None) -> Message:
    await _delete_previous_menu(message, state)
    sent = await message.answer(text, reply_markup=reply_markup)
    await _remember_menu_message(state, sent)
    return sent


async def _send_long_html_chunks(
    message: Message,
    text: str,
    reply_markup=None,
    max_len: int = 3900,
    state: FSMContext | None = None,
) -> None:
    """Send long HTML text in multiple Telegram-safe chunks.

    Telegram sendMessage has a hard limit (~4096 chars). We split by paragraphs
    first to keep formatting readable and avoid truncation errors.
    """
    if state is not None:
        await _delete_previous_menu(message, state)

    if len(text) <= max_len:
        sent = await message.answer(text, reply_markup=reply_markup)
        if state is not None:
            await _remember_menu_message(state, sent)
        return

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_len:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # Fallback for a very long paragraph without separators.
        while len(paragraph) > max_len:
            chunks.append(paragraph[:max_len])
            paragraph = paragraph[max_len:]
        current = paragraph

    if current:
        chunks.append(current)

    last_sent: Message | None = None
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        last_sent = await message.answer(chunk, reply_markup=reply_markup if is_last else None)

    if state is not None and last_sent is not None:
        await _remember_menu_message(state, last_sent)


def _city_has_markup(city_name: str, region_name: str | None = None) -> bool:
    return CatalogService.city_has_markup(city_name, region_name)


def _build_miniopt_header(city_name: str, region_name: str | None = None) -> str:
    kind = "Крупный заказ, скидка 15%" if _city_has_markup(city_name, region_name) else "Крупный заказ"
    return kind


def _build_catalog_markup(
    city_name: str,
    population: int = 0,
    scope: str = "regular",
    region_name: str | None = None,
) -> InlineKeyboardMarkup:
    has_markup = _city_has_markup(city_name, region_name)
    return kb.get_products_keyboard(
        city_name,
        population,
        region_name=region_name,
        show_miniopt_discount_badge=has_markup,
    )


async def _build_catalog_text(
    state: FSMContext,
    city_name: str,
    population: int = 0,
    header: str | None = None,
    scope: str = "regular",
) -> str:
    data = await state.get_data()
    region_name = data.get("region")
    if scope == "miniopt":
        products = CatalogService.get_miniopt_products_for_city(
            city_name, population, region_name=region_name
        )
        kind = _build_miniopt_header(city_name, region_name)
        catalog_scope = "miniopt"
    else:
        products = CatalogService.get_products_for_city(city_name, population, region_name=region_name)
        kind = "Витрина города"
        catalog_scope = "regular"
    product_ids = [item.get("id") for item in products if isinstance(item, dict) and item.get("id")]
    await state.update_data(catalog_product_ids=product_ids, catalog_scope=catalog_scope)
    return kb.format_catalog_board(city_name, products, kind=kind)


def _build_user_help_text(city_name: str | None = None) -> str:
    lines = [
        f"📘 <b>Гайд {html.escape(kb.SHOP_NAME)}</b>",
        f"<b>4 шага</b>:",
        f"1. {html.escape(kb.BTN_BUY)} → напишите город.",
        "2. Кнопка с <b>названием товара и ценой</b> — выбор позиции.",
        "3. Кнопки <b>R1</b>, <b>R2</b>… — район.",
        "4. Кнопки <b>S1</b>, <b>S2</b>… — способ, потом оплата.",
        "Промокод — до выбора способа оплаты.",
        "↩️ «На главную» — выход на старт.",
    ]

    if city_name:
        lines.extend([
            "",
            f"Город в этой сессии: <b>{html.escape(city_name)}</b>",
        ])

    lines.extend([
        "",
        "⭐ Оценки листаются кнопками «Раньше» и «Позже».",
        "В каталог возвращает кнопка «🎮 Вернуться в каталог».",
    ])
    return "\n".join(lines)


def _visible_roulette_games(user_id: int) -> list[dict]:
    return RouletteService.get_active_games()


def _roulette_markup(game: dict, user_id: int, *, list_visible: bool = False):
    participants = game.get("participants") or []
    return kb.get_roulette_game_keyboard(
        int(game.get("id", 0) or 0),
        is_active=game.get("status") == "active",
        joined=RouletteService.is_participant(game, user_id),
        participants_count=len(participants),
        max_participants=int(game.get("max_participants", 0) or 0),
        list_visible=list_visible,
        result_announced=bool(game.get("result_announced")),
    )


async def _send_roulette_games(message: Message) -> None:
    games = _visible_roulette_games(message.from_user.id)
    if not games:
        await message.answer("🎲 <b>Розыгрыши Store</b>\n\nСейчас нет открытых игр. Загляните позже.")
        return
    for game in games:
        await message.answer(
            RouletteService.format_public_game_text(game),
            reply_markup=_roulette_markup(game, message.from_user.id),
        )


@router.message(Command("roulette"))
@router.message(F.text == kb.BTN_ROULETTE)
async def roulette_open_from_message(message: Message):
    await _send_roulette_games(message)


@router.callback_query(F.data == "menu_roulette")
async def menu_roulette(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await _send_roulette_games(callback.message)


@router.callback_query(F.data == "roulette_open")
async def roulette_open_from_callback(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await _send_roulette_games(callback.message)


@router.callback_query(F.data.startswith("roul_join_"))
async def roulette_join_game(callback: CallbackQuery):
    game_id = int((callback.data or "").rsplit("_", 1)[-1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    ok, status = RouletteService.add_participant(
        game_id,
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name,
        bot_id=getattr(callback.bot, "id", None),
    )
    if not ok:
        await callback.answer(status, show_alert=True)
        return
    await callback.answer("Вы записаны")
    await callback.message.edit_text(
        RouletteService.format_public_game_text(RouletteService.get_game(game_id) or game),
        reply_markup=_roulette_markup(RouletteService.get_game(game_id) or game, callback.from_user.id),
    )


@router.callback_query(F.data.startswith("roul_list_"))
async def roulette_show_participants(callback: CallbackQuery):
    game_id = int((callback.data or "").rsplit("_", 1)[-1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    if not RouletteService.is_participant(game, callback.from_user.id):
        await callback.answer("Список доступен после записи в игру.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        RouletteService.format_public_participants_text(game),
        reply_markup=_roulette_markup(game, callback.from_user.id, list_visible=True),
    )


@router.callback_query(F.data.startswith("roul_hide_"))
async def roulette_hide_participants(callback: CallbackQuery):
    game_id = int((callback.data or "").rsplit("_", 1)[-1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        RouletteService.format_public_game_text(game),
        reply_markup=_roulette_markup(game, callback.from_user.id),
    )


@router.callback_query(F.data.startswith("roul_result_"))
async def roulette_show_results(callback: CallbackQuery):
    game_id = int((callback.data or "").rsplit("_", 1)[-1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        RouletteService.format_public_results_text(game),
        reply_markup=kb.get_roulette_results_keyboard(),
    )


async def deliver_pending_roulette_results_for_bot(
    bot: Bot,
    game_id: int | None = None,
) -> tuple[int, int, list[str]]:
    games = RouletteService.get_games_pending_delivery()
    if game_id is not None:
        games = [game for game in games if int(game.get("id", 0) or 0) == int(game_id)]

    sent = 0
    failed = 0
    notes: list[str] = []
    bot_id = getattr(bot, "id", None)
    for game in games:
        text = RouletteService.format_public_results_text(game)
        delivered = {int(value) for value in game.get("result_delivered_user_ids") or []}
        game_id_value = int(game.get("id", 0) or 0)
        for participant in game.get("participants") or []:
            user_id = int(participant.get("user_id", 0) or 0)
            if user_id <= 0 or user_id in delivered:
                continue
            if not RouletteService.should_deliver_on_bot(participant, bot_id):
                continue
            try:
                await bot.send_message(
                    user_id,
                    text,
                    reply_markup=kb.get_roulette_results_keyboard(),
                )
                RouletteService.mark_result_delivered(game_id_value, user_id)
                delivered.add(user_id)
                sent += 1
            except Exception as exc:
                failed += 1
                notes.append(f"{user_id}: {exc.__class__.__name__}")
    return sent, failed, notes


async def _check_channel_membership(bot: Bot, user_id: int, channel_ref: str | None) -> tuple[bool, str | None]:
    if not channel_ref:
        return True, None

    chat_id: int | str
    normalized = str(channel_ref).strip()
    if normalized.startswith("-100") and normalized[1:].isdigit():
        chat_id = int(normalized)
    else:
        chat_id = normalized

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        return False, "Не удалось проверить подписку. Убедитесь, что бот админ в канале."

    status = getattr(member, "status", "")
    if status in {"left", "kicked"}:
        return False, f"Для участия нужна подписка на канал {html.escape(normalized)}."

    return True, None


def _build_city_review_dump_chunks(city_name: str, reviews: list[dict], chunk_limit: int = 3800) -> list[str]:
    rating = ReviewsService.rating_stats_from_reviews(reviews)
    header = ReviewsService.format_reviews_header(
        rating=rating,
        city=city_name,
    )
    chunks: list[str] = []
    current = header

    for index, review in enumerate(reviews, start=1):
        entry = "\n" + ReviewsService.format_public_review_card(review, index=index)
        if len(current) + len(entry) > chunk_limit:
            chunks.append(current)
            current = header + entry
        else:
            current += entry

    if current:
        chunks.append(current)
    return chunks


async def _send_all_city_reviews(message: Message, state: FSMContext, city_name: str) -> None:
    data = await state.get_data()
    region_name = data.get("region") if city_name == data.get("city") else None
    await state.update_data(
        rev_return_city=data.get("rev_search_city"),
        rev_return_product=data.get("rev_search_product"),
        rev_return_product_id=data.get("rev_search_product_id"),
        rev_return_index=int(data.get("rev_current_index", 0) or 0),
        rev_all_city_mode=True,
        rev_search_city=city_name,
        rev_search_region=region_name,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
    )
    await _show_review_page(
        lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
        0,
        state,
    )


async def _restore_after_all_city_reviews(send_method, state: FSMContext):
    data = await state.get_data()

    await state.update_data(
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
    )

    city_name = data.get("city")
    population = data.get("population", 0)
    await state.update_data(rev_search_city=None, rev_search_region=None, rev_search_product=None, rev_search_product_id=None)
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        population,
        header=f"💎 <b>В магазин · {html.escape(str(city_name or '-'))}</b>",
    )
    await send_method(
        catalog_text,
        reply_markup=_build_catalog_markup(city_name, population, region_name=data.get("region")),
    )
    await state.set_state(OrderState.choosing_product)


async def _show_districts_text(message: Message, state: FSMContext, city_name: str, districts: list[str]):
    text = kb.format_district_board(city_name, districts)
    await _replace_menu_message(message, state, text, reply_markup=kb.get_districts_keyboard(districts))
    await state.set_state(OrderState.choosing_district)


async def _show_treasure_types_text(message: Message, state: FSMContext, product_name: str, options: list[str]):
    text = kb.format_stash_board(product_name, options)
    await _replace_menu_message(message, state, text, reply_markup=kb.get_treasure_types_keyboard(options))
    await state.set_state(OrderState.choosing_treasure_type)


async def _show_payment_text(
    message: Message,
    state: FSMContext,
    summary_text: str | None = None,
    back_callback: str = "nav_back_district",
):
    await _show_ratecore_methods(
        message,
        state,
        purpose="order",
        summary_text=summary_text,
        back_callback=back_callback,
    )


def _checkout_coupon_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎟 Есть промокод", callback_data="pay_coupon_enter")],
            [InlineKeyboardButton(text="➡️ Оплатить без промокода", callback_data="pay_coupon_skip")],
            [InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")],
        ]
    )


def _apply_coupon_to_summary(summary: str, product: dict) -> str:
    discount = float(product.get("coupon_discount") or 0)
    if discount <= 0:
        return summary
    price = kb.format_price_label(product.get("price") or 0)
    note = f"🎟 Промокод уменьшил заказ на <b>{kb.format_price_label(discount)} ₽</b>\nОсталось оплатить <b>{price} ₽</b>"
    if summary:
        return summary + "\n" + note
    return note


async def _offer_checkout_coupon(
    message: Message,
    state: FSMContext,
    summary_text: str | None = None,
    back_callback: str = "nav_back_district",
) -> None:
    await state.update_data(checkout_summary=summary_text or "", checkout_back_callback=back_callback)
    await state.set_state(OrderState.waiting_for_checkout_coupon)
    body = (summary_text or "").strip()
    extra = (
        "🎟 <b>Промокод перед оплатой</b>\n"
        "<i>Код уменьшит сумму. Если кода нет, нажмите «Оплатить без промокода».</i>"
    )
    text = f"{body}\n\n{extra}" if body else extra
    await _replace_menu_message(message, state, text, reply_markup=_checkout_coupon_keyboard())


@router.callback_query(F.data == "pay_coupon_skip", OrderState.waiting_for_checkout_coupon)
@router.callback_query(F.data == "pay_coupon_skip", OrderState.waiting_for_checkout_coupon_code)
async def pay_coupon_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer()
    if callback.message:
        await _show_payment_text(
            callback.message,
            state,
            summary_text=data.get("checkout_summary"),
            back_callback=data.get("checkout_back_callback") or "nav_back_district",
        )


@router.callback_query(F.data == "pay_coupon_enter", OrderState.waiting_for_checkout_coupon)
async def pay_coupon_enter(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(OrderState.waiting_for_checkout_coupon_code)
    if callback.message:
        await callback.message.answer(
            "🎟 Пришлите промокод одним сообщением.\n"
            "Если кода нет, нажмите «Оплатить без промокода».",
            reply_markup=_checkout_coupon_keyboard(),
        )


def _is_valley_panel_text(text: str | None) -> bool:
    raw = " ".join(str(text or "").strip().split())
    return (
        not raw
        or raw in kb.CANCEL_TEXT_ALIASES
        or raw in {kb.BTN_BUY, kb.BTN_BUY_LEGACY, "В магазин"}
        or raw.startswith(kb.BTN_BALANCE)
        or raw.startswith(kb.BTN_BALANCE_LEGACY)
        or raw.startswith(kb.BTN_PURCHASES)
        or raw.startswith(kb.BTN_PURCHASES_LEGACY)
    )


@router.message(
    OrderState.waiting_for_checkout_coupon_code,
    F.text.func(lambda text: not _is_valley_panel_text(text)),
)
async def pay_coupon_code(message: Message, state: FSMContext):
    code = " ".join((message.text or "").strip().split())
    ok, amount, info = CouponService.consume_as_discount(
        code,
        message.from_user.id,
        message.from_user.username,
    )
    if not ok:
        await message.answer(f"💎 {html.escape(str(info))}", reply_markup=_checkout_coupon_keyboard())
        return
    data = await state.get_data()
    product = dict(data.get("product") or {})
    try:
        price = float(product.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    cut = min(price, float(amount or 0))
    leftover = max(0.0, float(amount or 0) - cut)
    product["price"] = max(0.0, price - cut)
    product["coupon_discount"] = float(product.get("coupon_discount") or 0) + cut
    product["coupon_code"] = info
    await state.update_data(product=product)
    if leftover > 0:
        UserService.credit_balance(message.from_user.id, leftover, source="bonus")
    summary = _apply_coupon_to_summary(str(data.get("checkout_summary") or ""), product)
    await state.update_data(checkout_summary=summary)
    note = f"🎟 Промокод принят. С корзины снято <b>{kb.format_price_label(cut)} ₽</b>."
    if leftover > 0:
        note += f"\nОстаток <b>{kb.format_price_label(leftover)} ₽</b> зачислен в кошелек."
    await message.answer(note)
    await _show_payment_text(
        message,
        state,
        summary_text=summary,
        back_callback=data.get("checkout_back_callback") or "nav_back_district",
    )


async def _show_ratecore_methods(
    target: Message | CallbackQuery,
    state: FSMContext,
    *,
    purpose: str,
    summary_text: str | None = None,
    back_callback: str,
    page: int = 0,
) -> None:
    message = target.message if isinstance(target, CallbackQuery) else target
    try:
        methods, assets = await load_catalog()
    except RatecoreError as exc:
        text = f"🔶 Оплата временно недоступна. Попробуйте позже.\n<code>{html.escape(str(exc))}</code>"
        if isinstance(target, CallbackQuery):
            await target.answer("Оплата временно недоступна", show_alert=True)
            try:
                await target.message.edit_text(text, reply_markup=kb.get_base_keyboard())
            except TelegramBadRequest:
                await message.answer(text)
        else:
            await message.answer(text)
        return

    await state.update_data(
        payment_purpose=purpose,
        ratecore_methods=methods,
        ratecore_assets=assets,
        ratecore_back_callback=back_callback,
    )
    data = await state.get_data()
    amount = checkout_base_amount(data)
    lines = []
    if summary_text:
        lines.extend([summary_text, ""])
    lines.append("💳 <b>Касса PlayStation™Store</b>\nВыберите способ оплаты на кнопке.")
    # Show balance button only for regular orders, not for top-up
    show_balance = purpose != "balance_topup"
    markup = kb.get_ratecore_methods_keyboard(
        methods,
        page,
        back_callback=back_callback,
        crypto_assets=assets,
        show_balance=show_balance,
        amount=amount,
    )
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(text, reply_markup=markup)
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
    else:
        await _replace_menu_message(message, state, text, reply_markup=markup)
    if purpose == "balance_topup":
        await state.set_state(TopUpState.choosing_method)
    else:
        await state.set_state(OrderState.choosing_payment)


def _resolve_product_id_by_index(data: dict, index: int) -> str | None:
    if index < 1:
        return None
    product_ids = data.get("catalog_product_ids") or []
    if index > len(product_ids):
        return None
    return product_ids[index - 1]


async def _send_access_captcha(message: Message, state: FSMContext) -> None:
    challenge = create_challenge()
    await state.update_data(captcha_correct=challenge.answer, captcha_kind=challenge.kind)
    await state.set_state(OrderState.waiting_for_captcha)

    photo = BufferedInputFile(challenge.image_png, filename="captcha.png")
    await message.answer_photo(photo, caption=challenge.caption)


async def _show_card_only_notice(callback: CallbackQuery) -> None:
    notice = (
        "🔶 <b>У этого способа сейчас нет счета.</b>\n\n"
        "Выберите <b>оплату картой</b> на кнопках кассы."
    )
    try:
        await callback.answer("Счета нет. Выберите оплату картой.", show_alert=True)
    except TelegramBadRequest:
        pass
    try:
        await callback.message.answer(notice)
    except TelegramBadRequest:
        pass


def _courier_job_summary(city_name: str, price) -> str:
    return (
        f"🚚 <b>Вакансия курьера · {html.escape(kb.SHOP_NAME)}</b>\n\n"
        f"Город выхода: {html.escape(str(city_name or 'не выбран'))}\n"
        f"Выплата: <b>{kb.format_price_label(price)} ₽</b>\n\n"
        f"{html.escape(COURIER_JOB_NOTICE)}"
    )


def _courier_job_paid_text(product_name: str, price) -> str:
    return (
        "🟢 <b>Вакансия оплачена</b>\n"
        f"📦 Позиция: {html.escape(str(product_name or 'Работа курьер'))}\n"
        f"💳 Списано с кошелька: {kb.format_price_label(price)} руб.\n\n"
        f"{COURIER_JOB_NOTICE}"
    )


async def _start_courier_job_checkout(user_id: int, message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("city"):
        await state.clear()
        await message.answer(
            "🔶 Данные о городе устарели. Пожалуйста, введите город заново.",
            reply_markup=kb.get_base_keyboard(),
        )
        await state.set_state(OrderState.waiting_for_city)
        return

    product = CatalogService.get_courier_job_product()
    await state.update_data(
        product=product,
        product_scope="regular",
        available_treasure_types=None,
        treasure_type="Работа",
        district="-",
        available_districts=[],
        district_stash_map={},
        requisites_fail_count=0,
        courier_job=True,
    )
    await _offer_checkout_coupon(
        message,
        state,
        summary_text=_courier_job_summary(data.get("city"), product.get("price")),
        back_callback="nav_back_products",
    )


async def _start_product_checkout(user_id: int, message: Message, state: FSMContext, product_id: str):
    await _start_scoped_product_checkout(user_id, message, state, product_id, "regular")


async def _start_scoped_product_checkout(
    user_id: int,
    message: Message,
    state: FSMContext,
    product_id: str,
    scope: str,
):
    active_order = UserService.get_active_order(user_id)
    if active_order:
        await message.answer(
            "🕒 <b>Сначала закройте текущий заказ.</b>\n"
            "Пока он открыт, второй оформить нельзя.\n"
            f"Нажмите <b>↩️ На главную</b>, затем снова <b>{html.escape(kb.BTN_BUY)}</b>.",
            reply_markup=kb.get_base_keyboard(),
        )
        return

    data_state = await state.get_data()
    city_name = data_state.get('city')
    if CatalogService.is_courier_job(product_id) and scope != "miniopt":
        await _start_courier_job_checkout(user_id, message, state)
        return
    if scope == "miniopt":
        product = CatalogService.get_miniopt_product_by_id(
            product_id,
            city_name=city_name,
            region_name=data_state.get("region"),
        )
    else:
        product = CatalogService.get_product_by_id(
            product_id,
            city_name=city_name,
            region_name=data_state.get("region"),
        )
    if not product:
        await message.answer("🔶 Товар недоступен.")
        return

    product = dict(product)
    original_price = product.get("price") or 0
    discounted = UserService.price_with_discount(user_id, original_price)
    product["original_price"] = original_price
    product["price"] = discounted
    discount_info = UserService.get_personal_discount(user_id)
    if discount_info.get("active"):
        product["applied_discount_percent"] = discount_info["percent"]

    await state.update_data(
        product=product,
        product_scope=scope,
        available_treasure_types=None,
        treasure_type=None,
        requisites_fail_count=0,
    )
    data = await state.get_data()
    if not data.get("city") or not data.get("districts"):
        await state.clear()
        await message.answer(
            "🔶 Данные о городе устарели. Пожалуйста, введите город заново.",
            reply_markup=kb.get_base_keyboard()
        )
        await state.set_state(OrderState.waiting_for_city)
        return

    city_name = data['city']
    all_districts = data['districts']
    if scope == "miniopt":
        available_districts = CatalogService.get_available_districts_for_miniopt_product(
            city_name,
            product['id'],
            all_districts,
        )
    else:
        available_districts = CatalogService.get_available_districts_for_product(
            city_name,
            product['id'],
            all_districts,
        )

    if not available_districts:
        await message.answer("🔶 Для этой позиции Store не нашел районы. Откройте другую позицию или другой город.")
        await state.set_state(OrderState.choosing_product)
        return

    if scope == "miniopt":
        district_stash_map = CatalogService.get_miniopt_district_stash_type_map(
            city_name,
            product['id'],
            available_districts,
        )
    else:
        district_stash_map = CatalogService.get_district_stash_type_map(
            city_name,
            product['id'],
            available_districts,
        )
    await state.update_data(available_districts=available_districts, district_stash_map=district_stash_map)
    await _show_districts_text(message, state, city_name, available_districts)


async def _open_product_reviews_message(message: Message, state: FSMContext, product_id: str):
    data = await state.get_data()
    city_name = data.get('city')
    if not city_name:
        await message.answer("🔶 Сессия города истекла. Введите город заново.")
        return

    product = CatalogService.get_product_by_id(
        product_id,
        city_name=city_name,
        region_name=data.get("region"),
    )
    if not product:
        await message.answer("🔶 Товар недоступен.")
        return

    loading_msg = await _replace_menu_message(message, state, "⭐ <b>Открываю оценки этой позиции...</b>")
    await state.update_data(
        rev_search_city=city_name,
        rev_search_region=data.get("region"),
        rev_search_product=product.get('name'),
        rev_search_product_id=product_id,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
    )
    await _show_review_page(lambda text, reply_markup=None: loading_msg.edit_text(text, reply_markup=reply_markup), 0, state)


async def _process_crypto_payment_message(message: Message, state: FSMContext, bot: Bot, coin: str = "ltc"):
    await _start_crypto_payment(message, state, bot, coin)


def _build_crypto_admin_card(
    user_id: int,
    username: str | None,
    order: dict,
    status: str,
    expires_at: str | None = None,
) -> str:
    coin = order.get("type") if order.get("type") != "crypto" else "ltc"
    try:
        symbol = CryptoService.symbol(coin)
        title = CryptoService.title(coin)
    except ValueError:
        symbol = str(coin or "CRYPTO").upper()
        title = symbol
    tx_hash = order.get("tx_hash")
    hash_line = f"🔗 <b>Хеш:</b> <code>{html.escape(str(tx_hash))}</code>" if tx_hash else "🔗 <b>Хеш:</b> ещё не прислан"
    expire_line = f"⏳ До: <b>{expires_at}</b> (МСК)" if expires_at else "⏳ Срок: 1 час"
    is_topup = CryptoService.is_balance_topup(order)
    is_chest = CryptoService.is_chest_boost(order)
    if is_chest:
        header = f"🎁 <b>Сундук: усиление {title} ({symbol})</b>\n"
        product_line = "📦 Назначение: <b>Усиление сундука</b>\n"
    elif is_topup:
        header = f"💎 <b>Пополнение баланса {title} ({symbol})</b>\n"
        product_line = "📦 Назначение: <b>Пополнение баланса</b>\n"
    else:
        header = f"💎 <b>Заявка {title} ({symbol})</b>\n"
        product_line = f"📦 Товар: {html.escape(str(order.get('product') or 'Неизвестно'))}\n"
    return (
        f"{header}"
        f"📌 Статус: <b>{html.escape(status)}</b>\n\n"
        f"👤 Пользователь: {user_id} (@{username or 'anon'})\n"
        f"🌇 Город: {order.get('city') or 'Не выбран'}\n"
        f"🏙 Район: {order.get('district') or 'Не выбран'}\n"
        f"{product_line}"
        f"🏷 Формат: {html.escape(str(order.get('treasure_type') or '-'))}\n"
        f"💵 {_format_rub_amount(order.get('price') or 0)} ₽ → "
        f"<code>{html.escape(str(order.get('crypto_amount') or '-'))} {symbol}</code>\n"
        f"🏦 Кошелёк: <code>{html.escape(str(order.get('wallet') or ''))}</code>\n"
        f"{hash_line}\n"
        f"📝 Номер заявки: <code>{html.escape(str(order.get('payment_request_id') or ''))}</code>\n"
        f"{expire_line}"
    )


async def _send_crypto_application_to_admins(
    bot: Bot,
    user_id: int,
    username: str | None,
    order: dict,
    status: str,
    expires_at: str | None = None,
) -> None:
    text = _build_crypto_admin_card(user_id, username, order, status, expires_at)
    await _send_message_to_admins(
        bot,
        text,
        reply_markup=kb.get_crypto_order_admin_keyboard(
            user_id,
            topup=CryptoService.is_balance_topup(order),
            chest=CryptoService.is_chest_boost(order),
        ),
    )


def _build_crypto_payment_text(
    coin: str,
    product_name: str | None,
    rub_amount: float,
    coin_amount: str,
    rate: float,
    wallet: str,
    expires_at: str,
    *,
    is_topup: bool = False,
    is_chest: bool = False,
) -> str:
    symbol = CryptoService.symbol(coin)
    title = CryptoService.title(coin)
    rate_display = f"{rate:,.2f}".replace(",", " ")
    rub_display = _format_rub_amount(rub_amount)
    shop = html.escape(kb.SHOP_NAME)
    if is_chest:
        order_line = "🎁 Усиление сундука"
        screen = f"💎 <b>Crypto-касса · {title}</b>"
    elif is_topup:
        order_line = "💰 Пополнение кошелька"
        screen = f"💎 <b>Crypto-касса · {title}</b>"
    else:
        screen = f"💎 <b>Crypto-касса · {title}</b>"
        if product_name:
            weight, pname = kb.catalog_name_parts(product_name)
            short = kb.short_catalog_title(pname)
            order_line = html.escape(short)
            if weight:
                order_line = f"{order_line} · {html.escape(weight)}"
        else:
            order_line = "Позиция в корзине"
    lines = [
        f"🔵 <b>{shop}</b>",
        screen,
        "",
        f"🎮 {order_line}",
        "",
        f"💵 Сумма: <b>{rub_display} ₽</b>",
        f"📊 Курс: 1 {symbol} ≈ <b>{rate_display} ₽</b>",
        f"🪙 Перевод: <code>{coin_amount} {symbol}</code>",
        "",
        "<b>Адрес для перевода</b>",
        f"<code>{html.escape(wallet)}</code>",
        "",
        "Переведите <b>точную</b> сумму на адрес.",
        f"⏳ Счёт активен 1 час · до <b>{expires_at}</b> (МСК).",
        "",
        "📝 После оплаты пришлите <b>хеш (txid)</b> одним сообщением.",
    ]
    return "\n".join(lines)


async def _start_crypto_payment(
    target: Message | CallbackQuery,
    state: FSMContext,
    bot: Bot,
    coin: str,
    *,
    purpose: str = "order",
) -> None:
    coin = coin.lower()
    is_topup = purpose == "topup"
    is_chest = purpose == "chest_boost"
    user_id = target.from_user.id
    message = target.message if isinstance(target, CallbackQuery) else target
    data_ctx = await state.get_data()
    if is_topup:
        fallback_markup = kb.get_ratecore_methods_keyboard(
            data_ctx.get("ratecore_methods") or [],
            0,
            back_callback=data_ctx.get("ratecore_back_callback") or "nav_cancel",
            crypto_assets=data_ctx.get("ratecore_assets") or [],
            show_balance=False,
            amount=checkout_base_amount(data_ctx),
        )
    else:
        fallback_markup = kb.get_ratecore_methods_keyboard(
            data_ctx.get("ratecore_methods") or [],
            0,
            back_callback=data_ctx.get("ratecore_back_callback") or "nav_back_district",
            crypto_assets=data_ctx.get("ratecore_assets") or [],
            show_balance=True,
            amount=checkout_base_amount(data_ctx),
        )

    if BlacklistService.is_banned(user_id):
        if isinstance(target, CallbackQuery):
            await target.answer("🔒 Вы заблокированы.", show_alert=True)
        else:
            await message.answer("🔒 Вы заблокированы.")
        return

    wallet = (CryptoService.pick_wallet(coin) or "").strip()
    if not wallet:
        text = f"🔶 Кошелёк {CryptoService.symbol(coin)} ещё не настроен. Выберите другой способ оплаты."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
            try:
                await target.message.edit_text(text, reply_markup=fallback_markup)
            except TelegramBadRequest:
                await message.answer(text, reply_markup=fallback_markup)
        else:
            await message.answer(text, reply_markup=fallback_markup)
        return

    data = await state.get_data()
    if is_chest:
        try:
            rub_amount = float(data.get("chest_boost_rub") or 0)
        except (TypeError, ValueError):
            rub_amount = 0.0
        percent = int(data.get("chest_boost_percent") or boost_percent_for_rub(rub_amount))
        product = {"name": f"Усиление сундука +{percent}%", "price": rub_amount}
    elif is_topup:
        try:
            rub_amount = float(data.get("topup_amount") or 0)
        except (TypeError, ValueError):
            rub_amount = 0.0
        product = {"name": "Пополнение баланса", "price": rub_amount}
    else:
        product = data.get("product") or {}
        try:
            rub_amount = float(product.get("price") or 0)
        except (TypeError, ValueError):
            rub_amount = 0.0

    if rub_amount <= 0:
        if is_topup:
            text = "🔶 Сумма не задана. Начните пополнение заново."
        elif is_chest:
            text = "🔶 Сумма усиления не задана. Выберите ступень ещё раз."
        else:
            text = "🔶 Сумма заказа не задана."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await message.answer(text)
        return

    rate = await CryptoService.get_rate(coin)
    if not rate:
        text = "🔶 Курс временно недоступен. Попробуйте через минуту или выберите другой способ."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await message.answer(text)
        return

    coin_amount_value = CryptoService.convert_rub(rub_amount, rate)
    coin_amount = CryptoService.format_amount(coin_amount_value, coin)
    payment_request_id = uuid.uuid4().hex
    expires_dt = get_now_msk() + timedelta(hours=1)
    expires_at = expires_dt.strftime("%d.%m %H:%M")

    if is_chest:
        order_purpose = "chest_boost"
    elif is_topup:
        order_purpose = "balance_topup"
    else:
        order_purpose = "order"
    order_data = {
        "type": coin,
        "purpose": order_purpose,
        "product": product.get("name", "Unknown"),
        "price": rub_amount,
        "crypto_amount": coin_amount,
        "crypto_rate": rate,
        "wallet": wallet,
        "treasure_type": "-" if (is_topup or is_chest) else data.get("treasure_type", "Тайник"),
        "city": None if (is_topup or is_chest) else data.get("city"),
        "district": None if (is_topup or is_chest) else data.get("district"),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_minutes": 60,
        "message_id": message.message_id,
        "payment_request_id": payment_request_id,
        "tx_hash": None,
        "boost_percent": int(data.get("chest_boost_percent") or 0) if is_chest else 0,
    }
    UserService.set_active_order(user_id, order_data)
    await state.update_data(
        payment_request_id=payment_request_id,
        payment_amount=rub_amount,
        payment_coin=coin,
        payment_crypto_amount=coin_amount,
        payment_wallet=wallet,
    )

    pay_text = _build_crypto_payment_text(
        coin,
        product.get("name"),
        rub_amount,
        coin_amount,
        rate,
        wallet,
        expires_at,
        is_topup=is_topup,
        is_chest=is_chest,
    )

    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(pay_text, reply_markup=kb.get_pending_order_inline_keyboard())
        except TelegramBadRequest:
            await message.answer(pay_text, reply_markup=kb.get_pending_order_inline_keyboard())
        await message.answer(
            "⌨️ Меню PlayStation™Store — на кнопках снизу.",
            reply_markup=kb.get_pending_order_keyboard(),
        )
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
    else:
        await message.answer(pay_text, reply_markup=kb.get_pending_order_keyboard())

    await _send_crypto_application_to_admins(
        bot,
        user_id=user_id,
        username=target.from_user.username,
        order=order_data,
        status="ожидает хеш транзакции",
        expires_at=expires_at,
    )
    await state.set_state(OrderState.waiting_for_receipt)


async def _complete_balance_purchase(
    bot: Bot,
    user_id: int,
    *,
    username: str | None,
    data: dict,
    product: dict,
    price,
) -> dict | None:
    district = data.get("district")
    treasure = data.get("treasure_type") or "Тайник"
    city = data.get("city")
    order_data = {
        "type": "balance",
        "purpose": "order",
        "product": product.get("name") if isinstance(product, dict) else product,
        "price": price,
        "payer_amount": price,
        "treasure_type": treasure,
        "city": city,
        "district": district,
        "method": "balance",
        "method_name": "Оплата с баланса",
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "purchase_counted": True,
        "status": "paid_waiting_admin",
        "bot_id": getattr(bot, "id", None),
        "kind": "courier_job" if CatalogService.is_courier_job(product) else None,
    }
    UserService.set_active_order(user_id, order_data)
    bonus_payload = complete_product_purchase(
        user_id,
        price,
        title=str(order_data.get("product") or ""),
        city=str(city or ""),
    )
    await notify_referrer_bonus(bot, bonus_payload)
    text = admin_paid_text(
        user_id,
        username,
        order_data,
        {"payer_amount": price, "base_amount": price},
        include_settlement=False,
    )
    await notify_admins(bot, text, reply_markup=admin_action_keyboard(user_id, order_data))
    return bonus_payload


async def _process_balance_payment_message(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        await message.answer("🔒 Вы заблокированы.")
        return

    data = await state.get_data()
    product = data.get('product')
    price = product['price'] if product else 0

    if UserService.deduct_balance(user_id, price):
        district = data.get('district')
        treasure = data.get('treasure_type', 'Тайник')
        bonus_payload = await _complete_balance_purchase(
            bot,
            user_id,
            username=message.from_user.username,
            data=data,
            product=product,
            price=price,
        )

        if CatalogService.is_courier_job(product):
            await message.answer(_courier_job_paid_text(product.get("name"), price))
        else:
            await message.answer(
                build_balance_order_paid_text(
                    product_name=product.get("name"),
                    district=district,
                    price=price,
                    treasure=treasure,
                )
            )
        await notify_purchase_loyalty(bot, user_id, bonus_payload)
        await state.clear()
    else:
        bal = UserService.get_balance(user_id)
        await message.answer(f"❗ В кошельке не хватает. Сейчас: {bal} руб. К кассе: {price} руб.")


async def _process_card_rf_payment_message(message: Message, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    user_id = message.from_user.id

    if BlacklistService.is_banned(user_id):
        await message.answer("🔒 Вы заблокированы.")
        return

    blocked_rf, until_rf = UserService.is_rf_card_blocked(user_id)
    if blocked_rf:
        await message.answer(f"🛑️ Оплата картой РФ временно недоступна до {until_rf}. Выберите оплату с баланса.")
        return

    if SettingsService.is_redirect_active():
        await message.answer("🔶 Оплата картой временно недоступна. Попробуйте позже или оплатите с баланса.")
        return

    blocked, until = UserService.is_manual_payment_blocked(user_id)
    if blocked:
        await message.answer(
            f"🛑️ Оплата картой временно недоступна до {until}. Попробуйте позже или оплатите с баланса."
        )
        return

    cards = SettingsService.get_rf_cards()
    last_card = UserService.get_last_rf_card(user_id)
    if not cards:
        card_number = None
    elif len(cards) == 1:
        card_number = cards[0]
    else:
        candidates = [item for item in cards if item != last_card]
        if not candidates:
            candidates = list(cards)
        card_number = random.choice(candidates)

    if not card_number:
        await message.answer("🔶 Способ оплаты картой РФ временно недоступен. Выберите оплату с баланса.")
        return

    UserService.set_last_rf_card(user_id, card_number)

    data = await state.get_data()
    product = data.get('product')
    rf_commission = SettingsService.get_rf_card_commission()
    base_price = product['price'] if product else 0
    total_price = base_price + rf_commission

    payment_request_id = uuid.uuid4().hex
    expires_at = (get_now_msk() + timedelta(minutes=12)).strftime("%H:%M")

    order_data = {
        "type": "card_rf",
        "product": product['name'] if product else "Unknown",
        "price": total_price,
        "treasure_type": data.get('treasure_type', 'Тайник'),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": message.message_id,
        "payment_request_id": payment_request_id,
        "card_number": card_number,
    }
    UserService.set_active_order(user_id, order_data)

    await state.update_data(
        payment_request_id=payment_request_id,
        payment_card=card_number,
        payment_amount=total_price,
    )

    admin_text = _build_card_rf_payment_text(
        total_price,
        card_number,
        payment_request_id,
        expires_at,
        rf_commission,
        product_name=product['name'] if product else None,
        user_id=user_id,
        username=message.from_user.username,
        city=data.get('city'),
        district=data.get('district'),
        treasure_type=data.get('treasure_type'),
    )
    pay_text = _build_card_rf_payment_text(
        total_price,
        card_number,
        payment_request_id,
        expires_at,
        rf_commission,
        product_name=product['name'] if product else None,
        user_id=user_id,
        username=message.from_user.username,
        city=data.get('city'),
        district=data.get('district'),
        treasure_type=data.get('treasure_type'),
        include_instructions=True,
    )
    await message.answer(
        pay_text,
        reply_markup=kb.get_pending_order_inline_keyboard(),
    )
    await message.answer(
        "📩 После оплаты пришлите скриншот или PDF чека.",
        reply_markup=kb.get_pending_order_keyboard(),
    )

    await notify_admin(bot, admin_text)
    await _send_message_to_admins(
        bot,
        "🔽 Нажмите для чата с пользователем",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
            [InlineKeyboardButton(text="❗ Отменить заказ", callback_data=f"admin_cancel_order_{user_id}")],
            [InlineKeyboardButton(text="✅ Подтвердить (+покупка)", callback_data=f"admin_confirm_order_{user_id}")]
        ])
    )

    await state.set_state(OrderState.waiting_for_receipt)
    asyncio.create_task(_schedule_rf_payment_reminders(bot, user_id, payment_request_id, dispatcher))


async def _start_yoomoney_payment(
    target: Message | CallbackQuery,
    state: FSMContext,
    bot: Bot,
    dispatcher: Dispatcher,
) -> None:
    user_id = target.from_user.id
    message = target.message if isinstance(target, CallbackQuery) else target

    async def _reject(text: str, *, alert: bool = True) -> None:
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=alert)
        else:
            await message.answer(text)

    if BlacklistService.is_banned(user_id):
        await _reject("🔒 Вы заблокированы.")
        return

    blocked_ym, until_ym = UserService.is_yoomoney_blocked(user_id)
    if blocked_ym:
        await _reject(f"🛑️ Оплата ЮMoney временно недоступна до {until_ym}. Выберите другой способ.")
        return

    if SettingsService.is_redirect_active():
        await _reject("🔶 Оплата ЮMoney временно недоступна. Попробуйте позже или оплатите с баланса.")
        return

    blocked, until = UserService.is_manual_payment_blocked(user_id)
    if blocked:
        await _reject(f"🛑️ Оплата ЮMoney временно недоступна до {until}. Попробуйте позже или оплатите с баланса.")
        return

    wallet = _pick_rotating_item(
        SettingsService.get_yoomoney_wallets(),
        UserService.get_last_yoomoney_wallet(user_id),
    )
    if not wallet:
        text = "🔶 Кошельки ЮMoney пока не заданы. Выберите другой способ оплаты."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
            try:
                await target.message.edit_text(text, reply_markup=kb.get_payment_keyboard())
            except TelegramBadRequest:
                await message.answer(text, reply_markup=kb.get_payment_keyboard())
        else:
            await message.answer(text, reply_markup=kb.get_payment_keyboard())
        return

    UserService.set_last_yoomoney_wallet(user_id, wallet)
    data = await state.get_data()
    product = data.get("product") or {}
    ym_commission = SettingsService.get_yoomoney_commission()
    try:
        base_price = float(product.get("price") or 0)
    except (TypeError, ValueError):
        base_price = 0
    total_price = base_price + ym_commission
    try:
        amount_display = f"{float(total_price):.0f}"
    except (TypeError, ValueError):
        amount_display = str(total_price)

    payment_request_id = uuid.uuid4().hex
    expires_at = (get_now_msk() + timedelta(minutes=YOOMONEY_TTL_MINUTES)).strftime("%H:%M")
    order_data = {
        "type": "yoomoney",
        "product": product.get("name", "Unknown"),
        "price": total_price,
        "commission": ym_commission,
        "treasure_type": data.get("treasure_type", "Тайник"),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_minutes": YOOMONEY_TTL_MINUTES,
        "message_id": message.message_id,
        "payment_request_id": payment_request_id,
        "yoomoney_wallet": wallet,
        "card_number": wallet,
    }
    UserService.set_active_order(user_id, order_data)
    await state.update_data(
        payment_request_id=payment_request_id,
        payment_card=wallet,
        payment_amount=total_price,
        payment_method="yoomoney",
    )

    pay_text = _build_yoomoney_payment_text(
        total_price,
        wallet,
        payment_request_id,
        expires_at,
        product_name=product.get("name"),
        commission=ym_commission,
    )
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(pay_text, reply_markup=kb.get_pending_order_inline_keyboard())
        except TelegramBadRequest:
            await message.answer(pay_text, reply_markup=kb.get_pending_order_inline_keyboard())
        await message.answer(
            "📩 После оплаты пришлите скриншот или PDF чека.",
            reply_markup=kb.get_pending_order_keyboard(),
        )
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
    else:
        await message.answer(pay_text, reply_markup=kb.get_pending_order_inline_keyboard())
        await message.answer(
            "📩 После оплаты пришлите скриншот или PDF чека.",
            reply_markup=kb.get_pending_order_keyboard(),
        )

    admin_commission_line = f"💜 Комиссия: {int(ym_commission)} руб.\n" if ym_commission else ""
    admin_text = (
        "💜 <b>Заявка на оплату ЮMoney</b>\n"
        f"👤 Пользователь: {user_id} (@{target.from_user.username or 'anon'})\n"
        f"🌇 Город: {data.get('city', 'Не выбран')}\n"
        f"🏙 Район: {data.get('district', 'Не выбран')}\n"
        f"📦 Товар: {product.get('name', 'Неизвестно')}\n"
        f"🏷 Тип: {data.get('treasure_type') or 'Не выбран'}\n"
        f"💵 Сумма: {amount_display} руб.\n"
        f"{admin_commission_line}"
        f"📝 Номер заявки: <code>{payment_request_id}</code>\n"
        f"📝 Кошелёк: <code>{html.escape(wallet)}</code>\n"
        f"⏳ Действительно до: {expires_at} (МСК), {YOOMONEY_TTL_MINUTES} мин"
    )
    await notify_admin(bot, admin_text)
    await _send_message_to_admins(
        bot,
        "🔽 Нажмите для чата с пользователем",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
            [InlineKeyboardButton(text="❗ Отменить заказ", callback_data=f"admin_cancel_order_{user_id}")],
            [InlineKeyboardButton(text="✅ Подтвердить (+покупка)", callback_data=f"admin_confirm_order_{user_id}")],
        ]),
    )
    await state.set_state(OrderState.waiting_for_receipt)
    asyncio.create_task(_schedule_yoomoney_payment_reminders(bot, user_id, payment_request_id, dispatcher))

async def notify_admin(bot: Bot, text: str):
    """Отправляет уведомление админу о действиях пользователя"""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(int(admin_id), f"👀 Admin Watch:\n{text}")
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


async def _send_message_to_admins(
    bot: Bot,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(int(admin_id), text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send admin control message to %s", admin_id)


async def _send_home(target: Message, state: FSMContext, prefix: str | None = None) -> None:
    await send_home_to_chat(target.bot, target.chat.id, state=state, prefix=prefix)


async def _prompt_city_input(target: Message, state: FSMContext, *, text: str | None = None) -> None:
    await state.set_state(OrderState.waiting_for_city)
    await target.answer(text or kb.CITY_INPUT_TEXT, reply_markup=kb.get_base_keyboard())


async def _close_user_support_chat(
    event: Message | CallbackQuery,
    state: FSMContext,
    bot: Bot,
    *,
    notify: bool = True,
) -> None:
    current_state = await state.get_state()
    was_open = current_state == OrderState.chat_with_support.state
    prefix = "🚪 <b>Чат закрыт.</b> Вы вернулись в начало."

    if isinstance(event, CallbackQuery):
        await _safe_answer_callback(event)
        try:
            await event.message.delete()
        except Exception:
            pass
        await send_home_to_chat(bot, event.message.chat.id, state=state, prefix=prefix)
    else:
        await send_home_to_chat(bot, event.chat.id, state=state, prefix=prefix)

    if notify and was_open:
        user_id = event.from_user.id
        username = event.from_user.username or "-"
        await notify_admin(bot, f"Пользователь {user_id} (@{username}) закрыл чат поддержки.")


def _apply_rf_card_cancel_policy(user_id: int) -> tuple[str | None, str | None]:
    count = UserService.increment_rf_card_cancel_streak(user_id)
    if count >= CANCEL_BAN_LIMIT:
        until = (get_now_msk() + timedelta(hours=CANCEL_BAN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        UserService.set_rf_card_block_until(user_id, until)
        return (
            f"🛑️ Оплата картой РФ заблокирована на {CANCEL_BAN_HOURS} ч, до <b>{until}</b> (МСК).",
            until,
        )
    return None, None


def _apply_yoomoney_cancel_policy(user_id: int) -> tuple[str | None, str | None]:
    count = UserService.increment_yoomoney_cancel_streak(user_id)
    if count >= CANCEL_BAN_LIMIT:
        until = (get_now_msk() + timedelta(hours=CANCEL_BAN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        UserService.set_yoomoney_block_until(user_id, until)
        return (
            f"🛑️ Оплата ЮMoney заблокирована на {CANCEL_BAN_HOURS} ч, до <b>{until}</b> (МСК).",
            until,
        )
    return None, None


async def _schedule_rf_payment_reminders(bot: Bot, user_id: int, request_id: str, dispatcher: Dispatcher):
    schedule = [
        (5 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 7 минут."),
        (4 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 3 минуты."),
        (2 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталась 1 минута."),
    ]

    for delay, text in schedule:
        await asyncio.sleep(delay)
        active_order = UserService.get_active_order(user_id)
        if not active_order or active_order.get("payment_request_id") != request_id:
            return
        try:
            await bot.send_message(user_id, text, reply_markup=kb.get_cancel_payment_keyboard())
        except Exception:
            return

    await asyncio.sleep(60)
    active_order = UserService.get_active_order(user_id)
    if not active_order or active_order.get("payment_request_id") != request_id:
        return

    UserService.remove_active_order(user_id)
    rf_note, rf_block_until = _apply_rf_card_cancel_policy(user_id)
    extra = f"\n\n{rf_note}" if rf_note else ""
    await send_user_to_start(
        bot,
        dispatcher,
        user_id,
        "🛑️ <b>Заказ отменен по истечению времени.</b>\n"
        f"Счет закрыт. Новый заказ — кнопка <b>{html.escape(kb.BTN_BUY)}</b>." + extra,
    )

    await notify_admin(bot, f"⏱ Заказ по карте РФ отменен по таймауту. User: {user_id}")
    if rf_block_until:
        await notify_admin(bot, f"🛑️ RF карта блок {CANCEL_BAN_HOURS}ч. User: {user_id} до {rf_block_until}")


async def _schedule_yoomoney_payment_reminders(bot: Bot, user_id: int, request_id: str, dispatcher: Dispatcher):
    schedule = [
        (10 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 15 минут."),
        (7 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 8 минут."),
        (5 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 3 минуты."),
        (2 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталась 1 минута."),
    ]

    for delay, text in schedule:
        await asyncio.sleep(delay)
        active_order = UserService.get_active_order(user_id)
        if not active_order or active_order.get("payment_request_id") != request_id:
            return
        try:
            await bot.send_message(user_id, text, reply_markup=kb.get_cancel_payment_keyboard())
        except Exception:
            return

    await asyncio.sleep(60)
    active_order = UserService.get_active_order(user_id)
    if not active_order or active_order.get("payment_request_id") != request_id:
        return

    UserService.remove_active_order(user_id)
    ym_note, ym_block_until = _apply_yoomoney_cancel_policy(user_id)
    extra = f"\n\n{ym_note}" if ym_note else ""
    await send_user_to_start(
        bot,
        dispatcher,
        user_id,
        "🛑️ <b>Заказ отменен по истечению времени.</b>\n"
        f"Счет закрыт. Новый заказ — кнопка <b>{html.escape(kb.BTN_BUY)}</b>." + extra,
    )
    await notify_admin(bot, f"⏱ Заказ ЮMoney отменен по таймауту. User: {user_id}")
    if ym_block_until:
        await notify_admin(bot, f"🛑️ ЮMoney блок {CANCEL_BAN_HOURS}ч. User: {user_id} до {ym_block_until}")


async def _forward_with_admin_controls(bot: Bot, source_message: Message, header: str = ""):
    """Forward a user message to admin with a delete-for-all control."""
    if not ADMIN_IDS:
        return

    user_id = source_message.from_user.id
    username = source_message.from_user.username or "-"

    lines = []
    if header:
        lines.append(header)
    lines.append(f"👤 Пользователь: {user_id} (@{username})")
    lines.append(f"🆔 Сообщение: {source_message.message_id}")
    info_text = "\n".join(lines)

    for admin_id in ADMIN_IDS:
        forwarded_id = 0
        try:
            forwarded = await source_message.forward(admin_id)
            forwarded_id = forwarded.message_id
        except Exception:
            forwarded_id = 0

        delete_cb = f"admin_delmsg_{user_id}_{source_message.message_id}_{admin_id}_{forwarded_id}"
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить у всех", callback_data=delete_cb)],
            [InlineKeyboardButton(text="💬 Ответить", callback_data=f"admin_chat_{user_id}")],
            [InlineKeyboardButton(text="🚪 Закрыть чат", callback_data=f"admin_close_chat_{user_id}")],
        ])

        try:
            await bot.send_message(admin_id, info_text, reply_markup=markup)
        except TelegramBadRequest:
            pass


def _format_race_left(delta) -> str:
    if not delta:
        return "гонка завершена"
    total = int(delta.total_seconds())
    if total <= 0:
        return "гонка завершена"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"ещё {hours} ч {minutes} мин"
    return f"ещё {minutes} мин"


def _race_place_mark(place: int) -> str:
    if place == 1:
        return "🥇"
    if place == 2:
        return "🥈"
    if place == 3:
        return "🥉"
    return "▫️"


def _race_row_line(place: int, combat: int, gray: int, *, self_row: bool = False) -> str:
    you = "  ← это вы" if self_row else ""
    return (
        f"{_race_place_mark(place)} <b>#{place}</b>  "
        f"🔥 {combat}  ·  👤 {gray}{you}"
    )


def _race_profile_block(user_id: int) -> str:
    board = ReferralRaceService.board(user_id)
    if not board.get("running"):
        return ""
    race = board.get("race") or {}
    if not race.get("id"):
        return ""
    self_row = board.get("self") or {"gray": 0, "combat": 0, "place": board.get("total") or 1}
    combat = int(self_row.get("combat") or 0)
    gray = int(self_row.get("gray") or 0)
    place = int(self_row.get("place") or 0)
    total = int(board.get("total") or 0)
    status = f"🟢 идёт сейчас · {_format_race_left(board.get('left'))}"
    lines = [
        "🏁 <b>Гонка рефералов</b>",
        f"{status}\n",
        "🏆 <b>Призы на баланс</b>",
        f"🥇 1 место - <b>{RACE_PRIZES[1]} ₽</b>",
        f"🥈 2 место - <b>{RACE_PRIZES[2]} ₽</b>",
        f"🥉 3 место - <b>{RACE_PRIZES[3]} ₽</b>\n",
        "📌 <b>Что считается</b>",
        "🔥 <b>Боевые</b> - друг перешел по вашей ссылке и сделал заказ",
        "👤 <b>Серые</b> - друг перешел по ссылке и просто зашел в бота\n",
        "👥 <b>Вы пригласили за эту гонку</b>",
        f"🔥 боевых: <b>{combat}</b>",
        f"👤 серых: <b>{gray}</b>",
        f"\n📍 Ваше место в топе: <b>#{place}</b> из {total}",
        "📊 Места без имен - кто сколько пригласил:\n",
    ]
    top_rows = (board.get("rows") or [])[:8]
    for row in top_rows:
        lines.append(
            _race_row_line(
                int(row["place"]),
                int(row["combat"]),
                int(row["gray"]),
                self_row=bool(row.get("self")),
            )
        )
    if place > 8:
        lines.append("…")
        lines.append(_race_row_line(place, combat, gray, self_row=True))
    lines.append("\n<i>5% с покупки друга</i>")
    return "\n".join(lines)


def _profile_body(user: dict, user_id: int, username: str, ref_link: str) -> str:
    balance = user.get("balance", 0.0) if user else 0.0
    purchases = user.get("purchases_count", 0) if user else 0
    discount_info = UserService.get_personal_discount(user_id)
    if discount_info.get("active"):
        discount_line = (
            f"🏷 Личная скидка {discount_info['percent']}% "
            f"действует еще {discount_info['left']} заказов"
        )
    else:
        discount_line = "🏷 Личной скидки сейчас нет"
    refs = user.get("referral_count", 0) if user else 0
    text = (
        f"👤 <b>Аккаунт {html.escape(kb.SHOP_NAME)}</b>\n\n"
        f"ID покупателя: <code>{user_id}</code>\n"
        f"📚 Оплаченных заказов: <b>{purchases}</b>\n"
        f"💳 В кошельке: <b>{kb.format_price_label(balance)} ₽</b>\n"
        f"{discount_line}\n"
        f"👥 Приглашенных друзей: {refs}\n\n"
        f"<i>С покупки друга начисляется 5% в кошелек. Кнопка «Пригласить друга» откроет выбор чата.</i>"
    )
    race_block = _race_profile_block(user_id)
    if race_block:
        text += "\n\n" + race_block
    return text


def _invite_share_url(ref_link: str) -> str:
    text = "Заходи в PlayStation™Store"
    return f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(text, safe='')}"


def _profile_keyboard(ref_link: str):
    return kb.get_profile_actions_keyboard(_invite_share_url(ref_link))


def _wallet_history_text(user_id: int) -> str:
    user = UserService.get_user(user_id) or {}
    balance = UserService.get_balance(user_id)
    events = user.get("wallet_events") if isinstance(user.get("wallet_events"), list) else []
    lines = [
        f"💳 <b>Кошелек {html.escape(kb.SHOP_NAME)}</b>",
        "",
        f"Доступно: <b>{kb.format_price_label(balance)} ₽</b>",
        "",
        "<b>Пополнения и бонусы</b>",
    ]
    if not events:
        lines.append("Зачислений пока нет. Они появятся после пополнения или бонуса.")
    else:
        for item in events[:12]:
            label = html.escape(str(item.get("label") or "Зачисление"))
            when = html.escape(str(item.get("at") or ""))
            amount = kb.format_price_label(item.get("amount") or 0)
            lines.append(f"• {when} · {label} · <b>+{amount} ₽</b>")
    lines.extend(["", "Кнопка ниже открывает пополнение баланса."])
    return "\n".join(lines)


def _paid_orders_text(user_id: int) -> str:
    user = UserService.get_user(user_id) or {}
    total = int(user.get("purchases_count") or 0)
    orders = user.get("paid_orders") if isinstance(user.get("paid_orders"), list) else []
    lines = [
        "📚 <b>Оплаченные заказы</b>",
        "",
        f"Успешных оплат: <b>{total}</b>",
        "",
    ]
    if not orders:
        lines.append("Список успешных заказов появится после следующей оплаты.")
    else:
        for index, item in enumerate(orders[:12], start=1):
            title = html.escape(str(item.get("title") or "Заказ"))
            when = html.escape(str(item.get("at") or ""))
            city = html.escape(str(item.get("city") or "").strip())
            amount = kb.format_price_label(item.get("amount") or 0)
            place = f" · {city}" if city else ""
            lines.append(f"{index}. {when}{place}\n{title} · <b>{amount} ₽</b>")
        if total > len(orders):
            lines.extend(["", "Более ранние оплаты в списке не сохранены."])
    return "\n".join(lines)


# --- Глобальная навигация (Обработчики кнопок Назад и Отмена) ---

@router.callback_query(F.data == "nav_cancel")
@router.message(F.text.in_(kb.CANCEL_TEXT_ALIASES))
async def navigation_cancel(event: Message | CallbackQuery, state: FSMContext, bot: Bot):
    user_id = event.from_user.id
    current_state = await state.get_state()
    if current_state == OrderState.chat_with_support.state:
        await _close_user_support_chat(event, state, bot)
        return

    if current_state and str(current_state).startswith("DisputeState"):
        prefix = "🚪 Диспут отменён."
        if isinstance(event, CallbackQuery):
            await event.answer()
            try:
                await event.message.delete()
            except Exception:
                pass
            await send_home_to_chat(bot, event.message.chat.id, state=state, prefix=prefix)
        else:
            await send_home_to_chat(bot, event.chat.id, state=state, prefix=prefix)
        return

    # Check TopUp cancellation
    topup_cancel_notified = False
    if current_state and "TopUpState" in current_state:
        username = event.from_user.username or str(user_id)
        data = await state.get_data()
        amount = data.get('topup_amount', 0)
        await notify_admin(bot, f"📉 Пользователь {username} отменил пополнение баланса.\nСумма: {amount} руб.")
        topup_cancel_notified = True

    # Check active order cancellation
    active_order = UserService.get_active_order(user_id)
    warning_msg = None

    if active_order:
        UserService.remove_active_order(user_id)
        rf_note = None
        rf_block_until = None
        if CryptoService.is_balance_topup(active_order):
            if not topup_cancel_notified:
                username = event.from_user.username or str(user_id)
                amount = active_order.get("price") or 0
                await notify_admin(
                    bot,
                    f"📉 Пользователь {username} отменил пополнение баланса.\n"
                    f"Сумма: {amount} руб. ({str(active_order.get('type') or '').upper()})",
                )
        elif CryptoService.is_chest_boost(active_order):
            username = event.from_user.username or str(user_id)
            amount = active_order.get("price") or 0
            await notify_admin(
                bot,
                f"🎁 Пользователь {username} отменил усиление сундука.\n"
                f"Сумма: {amount} руб. ({str(active_order.get('type') or '').upper()})",
            )
        elif active_order.get("type") == "card_rf" and str(user_id) != str(ADMIN_ID):
            rf_note, rf_block_until = _apply_rf_card_cancel_policy(user_id)
            if rf_block_until:
                await notify_admin(bot, f"🛑️ RF карта блок {CANCEL_BAN_HOURS}ч. User: {user_id} до {rf_block_until}")
        elif active_order.get("type") == "yoomoney" and str(user_id) != str(ADMIN_ID):
            ym_note, ym_block_until = _apply_yoomoney_cancel_policy(user_id)
            if ym_block_until:
                await notify_admin(bot, f"🛑️ ЮMoney блок {CANCEL_BAN_HOURS}ч. User: {user_id} до {ym_block_until}")
            rf_note = ym_note
        
        # Если это админ или отмена пополнения - не считаем отмены и не баним
        if str(user_id) != str(ADMIN_ID) and not CryptoService.is_balance_topup(active_order) and not CryptoService.is_chest_boost(active_order):
            current_cancels = UserService.increment_cancel_count(user_id)
            username = event.from_user.username
            if is_ratecore_order(active_order):
                await notify_admins(
                    bot,
                    admin_user_cancelled_text(
                        user_id,
                        username,
                        active_order,
                        cancel_count=current_cancels,
                        cancel_limit=CANCEL_BAN_LIMIT,
                    ),
                    reply_markup=admin_created_keyboard(user_id, active_order),
                )
            else:
                await notify_admin(
                    bot,
                    f"🗑 Пользователь {username or user_id} отменил активный заказ. "
                    f"Отмен: {current_cancels}/{CANCEL_BAN_LIMIT}",
                )
            
            if current_cancels >= CANCEL_BAN_LIMIT:
                ban_until = (get_now_msk() + timedelta(hours=CANCEL_BAN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
                UserService.set_ban_until(user_id, ban_until)
                
                text = (
                    "🔒 <b>ДОСТУП В STORE ЗАКРЫТ</b>\n\n"
                    f"Подряд закрыто заказов: {CANCEL_BAN_LIMIT}.\n"
                    f"Пауза на {CANCEL_BAN_HOURS} часов.\n"
                    f"Снова можно зайти: {ban_until}"
                )
                
                await state.clear()
                if isinstance(event, CallbackQuery):
                    await event.message.edit_text(text)
                else:
                    await event.answer(text, reply_markup=ReplyKeyboardRemove())
                return
            
            if current_cancels >= CANCEL_CRITICAL_COUNT:
                warning_msg = (
                    "🔶 <b>Последнее предупреждение Store</b>\n"
                    f"Заказ закрыт. Отмен: {current_cancels}/{CANCEL_BAN_LIMIT}.\n"
                    f"Следующая, {CANCEL_BAN_LIMIT}-я, закроет доступ на {CANCEL_BAN_HOURS} часов.\n"
                    "Счетчик обнулится после оплаченного заказа."
                )
            else:
                warning_msg = (
                    f"Заказ закрыт. Отмен: {current_cancels}/{CANCEL_BAN_LIMIT}.\n"
                    f"На {CANCEL_BAN_LIMIT}-й Store закроет доступ на {CANCEL_BAN_HOURS} часов.\n"
                    "Счетчик обнулится после оплаченного заказа."
                )
            if rf_note:
                warning_msg += f"\n\n{rf_note}"
        else:
            # Для админа просто уведомление (опционально) или ничего
            pass

    prefix = "🔵 Вы на главной PlayStation™Store."
    if warning_msg:
        prefix = warning_msg + "\n\n" + prefix

    if isinstance(event, CallbackQuery):
        try:
            await event.message.delete()
        except Exception:
            pass
        await send_home_to_chat(bot, event.message.chat.id, state=state, prefix=prefix)
        await _safe_answer_callback(event)
    else:
        await send_home_to_chat(bot, event.chat.id, state=state, prefix=prefix)


@router.message(F.text.in_({kb.BTN_CLOSE_CHAT, "закрыть канал"}))
async def user_close_support_chat(event: Message, state: FSMContext, bot: Bot):
    await _close_user_support_chat(event, state, bot)


@router.callback_query(F.data == "order_back_to_pay")
async def order_back_to_pay(callback: CallbackQuery, dispatcher: Dispatcher):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    try:
        await callback.message.delete()
    except Exception:
        pass
    order = UserService.get_active_order(callback.from_user.id)
    await callback.message.answer(USER_WAIT_TEXT, reply_markup=kb.get_base_keyboard())
    if is_ratecore_order(order):
        try:
            await refresh_order(callback.bot, dispatcher, callback.from_user.id, order)
        except Exception:
            logger.exception("Failed to refresh Ratecore order for %s", callback.from_user.id)

@router.callback_query(F.data == "nav_back_city")
async def navigation_back_to_city(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    # Удаляем старое, отправляем новое с клавиатурой
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        kb.CITY_INPUT_TEXT,
        reply_markup=kb.get_base_keyboard(),
    )
    await state.set_state(OrderState.waiting_for_city)
    await callback.answer()

@router.message(F.text == kb.BTN_BACK, OrderState.choosing_district)
async def navigation_back_to_products(message: Message, state: FSMContext):
    data = await state.get_data()
    catalog_text = await _build_catalog_text(
        state,
        data['city'],
        data.get('population', 0),
        header=f"💎 <b>В магазин</b>\n--",
    )
    await _replace_menu_message(
        message,
        state,
        catalog_text,
        reply_markup=_build_catalog_markup(
            data['city'],
            data.get('population', 0),
            region_name=data.get('region'),
        ),
    )
    # Используем edit_text нельзя, так как предыдущее сообщение было текстовым (BTN_BACK)
    await state.set_state(OrderState.choosing_product)

@router.callback_query(F.data == "nav_back_district", OrderState.choosing_payment)
async def navigation_back_to_districts(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("courier_job") or CatalogService.is_courier_job(data.get("product")):
        await _safe_answer_callback(callback)
        if callback.message:
            await _return_to_catalog(callback.message, state)
        return
    available_districts = data.get('available_districts', [])
    await _show_districts_text(callback.message, state, data.get('city', ''), available_districts)
    await _safe_answer_callback(callback)

# --- Профиль и Пополнение ---

@router.message(F.text.in_({kb.BTN_PROFILE, kb.BTN_PROFILE_LEGACY, "👤 Профиль"}))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user = UserService.get_user(user_id)
    
    username = message.from_user.username or "Неизвестно"
    bot_user = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_user.username}?start={user_id}"
    text = _profile_body(user, user_id, username, ref_link)
    await message.answer(text, reply_markup=_profile_keyboard(ref_link))


@router.message(F.text == kb.BTN_CONTACTS)
async def show_contacts(message: Message):
    await message.answer(SettingsService.get_contacts_text(), reply_markup=kb.get_base_keyboard())


_USER_GUIDE_PDF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "Инструкция_для_пользователя.pdf",
)

_USER_GUIDE_CAPTION = (
    f"📘 <b>Гайд {html.escape(kb.SHOP_NAME)}</b>\n\n"
    "В PDF: главная, каталог, район, выдача и касса.\n"
    "Файл открывается прямо в этом чате."
)


async def _send_user_guide_pdf(message: Message) -> None:
    if not os.path.isfile(_USER_GUIDE_PDF):
        await message.answer(
            "🔶 Файл инструкции сейчас недоступен. Напишите в поддержку.",
            reply_markup=kb.get_base_keyboard(),
        )
        return
    with open(_USER_GUIDE_PDF, "rb") as handle:
        payload = handle.read()
    await message.answer_document(
        BufferedInputFile(payload, filename="Инструкция_по_боту.pdf"),
        caption=_USER_GUIDE_CAPTION,
        reply_markup=kb.get_base_keyboard(),
    )


@router.message(F.text == kb.BTN_GUIDE)
async def send_user_guide(message: Message):
    await _send_user_guide_pdf(message)


async def _start_coupon_activation(target: Message | CallbackQuery, state: FSMContext):
    user_id = target.from_user.id
    status = CouponService.get_activation_status(user_id)
    cancel_markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")]]
    )

    if not status.get("can_redeem"):
        text = CouponService.build_limit_message(status)
        if isinstance(target, CallbackQuery):
            try:
                await target.message.edit_text(text, reply_markup=kb.get_profile_actions_keyboard())
            except TelegramBadRequest:
                await target.message.answer(text, reply_markup=kb.get_base_keyboard())
            await target.answer()
        else:
            await target.answer(text, reply_markup=kb.get_base_keyboard())
        await state.set_state(None)
        return

    text = CouponService.build_prompt_message(status)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=cancel_markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=cancel_markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=cancel_markup)

    await state.set_state(CouponState.waiting_for_code)


@router.message(F.text == kb.BTN_COUPON)
async def start_coupon_activation_by_button(message: Message, state: FSMContext):
    await _start_coupon_activation(message, state)


@router.callback_query(F.data == "profile_coupon_activate")
async def start_coupon_activation_by_callback(callback: CallbackQuery, state: FSMContext):
    await _start_coupon_activation(callback, state)


@router.message(CouponState.waiting_for_code)
async def process_coupon_code(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        return

    ok, amount, payload = CouponService.redeem_coupon(
        code=(message.text or ""),
        user_id=user_id,
        username=message.from_user.username,
    )

    if not ok:
        await message.answer(f"🔶 {payload}\nПопробуйте снова или нажмите Отмена.")
        await notify_bonus_cap(message.bot, user_id)
        return

    await state.clear()
    status = CouponService.get_activation_status(user_id)
    await send_home_to_chat(
        message.bot,
        message.chat.id,
        state=state,
        prefix=CouponService.build_success_message(payload, amount, status),
    )
    await _send_message_to_admins(
        message.bot,
        CouponService.build_admin_redeem_message(
            user_id,
            message.from_user.username,
            payload,
            amount,
        ),
    )
    await notify_bonus_cap(message.bot, user_id)

@router.callback_query(F.data == "profile_topup")
async def start_topup(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"💳 <b>Кошелек {html.escape(kb.SHOP_NAME)}</b>\n\n"
        "Какую сумму положить на счет?\n"
        "Напишите только цифры, без слова «рублей».\n"
        "Ниже 1 500 ₽ платеж не откроется.\n"
        "Образец: <code>2500</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")]])
    )
    await state.set_state(TopUpState.waiting_for_amount)
    await callback.answer()

@router.message(TopUpState.waiting_for_amount)
async def process_topup_amount(message: Message, state: FSMContext):
    if BlacklistService.is_banned(message.from_user.id):
        return

    try:
        amount = float(message.text.strip())
        if amount < 1500:
             await message.answer("🔶 В кошелек Store нельзя положить меньше 1 500 ₽. Напишите 1500 или больше.")
             return
    except ValueError:
        await message.answer("🔶 Здесь принимаются только цифры. Образец: 2500.")
        return

    await state.update_data(topup_amount=amount, payment_purpose="balance_topup", requisites_fail_count=0)
    await _show_ratecore_methods(
        message,
        state,
        purpose="balance_topup",
        summary_text=f"💳 В кошелек поступит: <b>{amount:g} ₽</b>",
        back_callback="nav_cancel",
    )

@router.callback_query(F.data == "topup_card", TopUpState.choosing_method)
async def process_topup_card(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Старый способ отключён. Выберите оплату из списка.", show_alert=True)
    data = await state.get_data()
    await _show_ratecore_methods(
        callback,
        state,
        purpose="balance_topup",
        summary_text=f"💳 В кошелек поступит: <b>{data.get('topup_amount') or 'сумма не указана'} ₽</b>",
        back_callback="nav_cancel",
    )
    return
    if BlacklistService.is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    blocked, until = UserService.is_manual_payment_blocked(callback.from_user.id)
    if blocked:
        await callback.answer(
            f"🛑️ Ручная оплата временно недоступна до {until}. Попробуйте позже.",
            show_alert=True,
        )
        try:
            await callback.message.edit_text(
                "🛑️ Ручная оплата временно недоступна. Выберите другой способ оплаты:",
                reply_markup=kb.get_topup_keyboard(),
            )
        except TelegramBadRequest:
            pass
        return

    data = await state.get_data()
    amount = data.get('topup_amount')
    
    await callback.answer() # Убираем лоадер
    
    # Имитация поиска оператора с анимацией
    spinner_frames = ["⏳", "🔄", "🕒", "🔁"]
    frame_text = "{icon} <b>Поиск свободного оператора...</b>\nПожалуйста, не закрывайте меню."
    initial_state = await state.get_state()

    for step in range(15):  # ~30 секунд ожидания с обновлением каждые 2 секунды
        icon = spinner_frames[step % len(spinner_frames)]
        try:
            await callback.message.edit_text(
                frame_text.format(icon=icon),
                reply_markup=kb.get_cancel_payment_keyboard()
            )
        except TelegramBadRequest:
            return

        await asyncio.sleep(2)
        if await state.get_state() != initial_state:
            return

    expires_at = (get_now_msk() + timedelta(minutes=20)).strftime("%H:%M")
    await state.update_data(topup_expires_at=expires_at)

    try:
        await callback.message.edit_text(
            f"💳 <b>Кошелек Store, карта</b>\n"
            f"✅ <b>Куратор кассы на связи.</b>\n\n"
            f"💰 Сумма: <b>{amount} руб.</b>\n"
            f"📝 Счет пришлет куратор, обычно до 5 минут.\n\n"
            f"ℹ️ Можно писать сюда и прислать снимок перевода.\n\n"
            f"⏳ Окно оплаты до <b>{expires_at}</b> (МСК).",
             reply_markup=kb.get_cancel_payment_keyboard()
        )
    except TelegramBadRequest:
        # Сообщение удалено или недоступно
        return

    # В реальном сценарии здесь мы ждем скриншот или переводим на чат с админом
    await state.set_state(TopUpState.waiting_for_receipt)
    try:
        amount_value = float(amount or 0)
    except (TypeError, ValueError):
        amount_value = 0.0
    UserService.set_active_order(callback.from_user.id, {
        "type": "manual_topup",
        "purpose": "balance_topup",
        "product": "Пополнение баланса",
        "price": amount_value,
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_minutes": 20,
        "message_id": callback.message.message_id,
    })

    try:
        amount_display = f"{float(amount):.0f}"
    except (TypeError, ValueError):
        amount_display = str(amount)
    admin_text = (
        "🆘 Запрос на ручную оплату (Карта)\n"
        f"👤 Пользователь: {callback.from_user.id} (@{callback.from_user.username or 'anon'})\n\n"
        f"💵 Сумма: {amount_display} руб."
    )

    await _send_message_to_admins(
        callback.bot,
        f"👀 Admin Watch:\n{admin_text}",
        reply_markup=kb.get_admin_topup_request_keyboard(
            callback.from_user.id,
            SettingsService.get_rf_cards(),
        ),
    )

@router.message(TopUpState.waiting_for_receipt)
async def process_topup_receipt(message: Message, state: FSMContext, bot: Bot):
    """Handle receipt for balance top-up payments."""
    from app.services.ratecore import RatecoreClient, RatecoreError
    from io import BytesIO
    
    user_id = message.from_user.id
    active_order = UserService.get_active_order(user_id)
    
    # Check if this is a Ratecore order
    if not is_ratecore_order(active_order):
        await message.answer(USER_WAIT_TEXT, reply_markup=kb.get_base_keyboard())
        return
    
    payment_id = str(active_order.get("payment_id") or "")
    if not payment_id:
        await message.answer(
            "🔶 Счет кошелька не найден или уже закрыт. Откройте пополнение заново.",
            reply_markup=kb.get_base_keyboard(),
        )
        await state.clear()
        return
    
    # Handle file uploads (photo or document)
    if message.photo or message.document:
        method_key = str(active_order.get("method") or "").lower()
        receipt_required = active_order.get("receipt_required") or "pdf" in method_key

        # PDF-methods: user MUST upload the receipt on the Ratecore page, not to the bot.
        # We forward the file to admins as a backup and redirect the user to the payment page.
        if "pdf" in method_key:
            await _forward_with_admin_controls(
                bot,
                message,
                (
                    f"🧾 Чек пополнения (PDF-метод, прислан в бот) от ({user_id})\n"
                    f"📝 Payment ID: <code>{payment_id}</code>\n"
                    f"📝 Способ: {active_order.get('method_name') or active_order.get('method')}"
                ),
            )
            await message.answer(
                pdf_redirect_text(payment_id),
                reply_markup=pdf_redirect_markup(active_order.get("payment_url"))
                or kb.get_base_keyboard(),
            )
            return

        if receipt_required and active_order.get("receipt_allows_upload", True):
            # Try to upload to Ratecore
            loading_msg = await message.answer("⏳ <b>Загружаю чек...</b>")
            
            try:
                if message.photo:
                    file_obj = message.photo[-1]
                    filename = "receipt.jpg"
                    content_type = "image/jpeg"
                else:
                    file_obj = message.document
                    filename = file_obj.file_name or "receipt.pdf"
                    if filename.lower().endswith(".pdf"):
                        content_type = "application/pdf"
                    elif filename.lower().endswith((".jpg", ".jpeg")):
                        content_type = "image/jpeg"
                    elif filename.lower().endswith(".png"):
                        content_type = "image/png"
                    else:
                        content_type = "application/octet-stream"
                
                file = await bot.get_file(file_obj.file_id)
                buffer = BytesIO()
                await bot.download_file(file.file_path, buffer)
                file_bytes = buffer.getvalue()
                
                client = RatecoreClient()
                await client.upload_receipt(
                    payment_id=payment_id,
                    file_bytes=file_bytes,
                    filename=filename,
                    content_type=content_type,
                )
                
                active_order["receipt_uploaded"] = True
                UserService.set_active_order(user_id, active_order)
                
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
                
                await message.answer(
                    "✅ <b>Чек загружен!</b>\n\n"
                    "Ожидайте подтверждения. Баланс будет пополнен автоматически.",
                    reply_markup=kb.get_base_keyboard(),
                )
                
                await notify_admin(
                    bot,
                    f"📤 <b>Чек пополнения загружен в Ratecore</b>\n"
                    f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
                    f"💵 Сумма: {active_order.get('payer_amount') or active_order.get('price')} ₽\n"
                    f"📝 Payment ID: <code>{payment_id}</code>",
                )
                logger.info("TopUp receipt uploaded for user %s, payment %s", user_id, payment_id)
                return
                
            except RatecoreError as exc:
                logger.warning("Ratecore receipt upload failed for topup %s: %s", user_id, exc)
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
                # Fall through to admin notification
                
            except Exception as exc:
                logger.exception("Error uploading topup receipt for %s", user_id)
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
                # Fall through to admin notification
        
        # Forward to admins as fallback
        admin_note = (
            f"🧾 <b>Чек пополнения баланса</b>\n"
            f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
            f"💵 Сумма: {active_order.get('payer_amount') or active_order.get('price')} ₽\n"
            f"📝 Payment ID: <code>{payment_id}</code>\n"
            f"📝 Способ: {active_order.get('method_name') or active_order.get('method')}"
        )
        await notify_admin(bot, admin_note)
        await _forward_with_admin_controls(bot, message, f"🧾 Чек пополнения от ({user_id})")
        
        await _send_fake_payment_check(
            message,
            processing_text="⏳ <b>Проверка чека...</b> Пожалуйста, подождите.",
        )
        return
    
    # Text message - forward to admin
    if ADMIN_IDS:
        await _forward_with_admin_controls(bot, message, f"📩 Сообщение пополнения от ({user_id})")
    await message.answer("✅ Сообщение отправлено оператору.")

@router.callback_query(F.data == "topup_btc", TopUpState.choosing_method)
async def process_topup_btc(callback: CallbackQuery, state: FSMContext):
    await process_topup_card(callback, state)


@router.callback_query(F.data == "topup_ltc", TopUpState.choosing_method)
async def process_topup_ltc(callback: CallbackQuery, state: FSMContext):
    await process_topup_card(callback, state)


@router.callback_query(F.data == "topup_crypto", TopUpState.choosing_method)
async def process_topup_crypto(callback: CallbackQuery, state: FSMContext):
    await process_topup_card(callback, state)

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def user_blocked_bot(event: ChatMemberUpdated):
    """Пользователь заблокировал бота"""
    UserService.set_active(event.from_user.id, False)
    BotStatsService.note_block(event.from_user.id)

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def user_unblocked_bot(event: ChatMemberUpdated):
    """Пользователь разблокировал бота"""
    UserService.set_active(event.from_user.id, True)
    BotStatsService.note_unblock(event.from_user.id)

# ------------------------------------------------------------------

@router.callback_query(F.data == "nav_cancel_delete")
async def navigation_cancel_delete(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@router.message(F.text == "⭐️ Отзывы")
async def show_reviews(message: Message, state: FSMContext):
    # Reset search filter
    await state.update_data(
        rev_search_city=None, rev_search_region=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
    )
    
    await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)


async def _open_all_cities_reviews_message(message: Message, state: FSMContext):
    await state.update_data(
        rev_search_city=None, rev_search_region=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
        rev_all_cities_mode=True,
        rev_all_cities_page=0,
    )
    await _show_all_cities_reviews_page(
        lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
        0,
        state,
    )


async def _show_all_cities_reviews_page(send_method, page_index: int, state: FSMContext):
    reviews = ReviewsService.get_filtered_reviews()
    total_reviews = len(reviews)

    if total_reviews == 0:
        await state.update_data(rev_all_cities_mode=True, rev_all_cities_page=0)
        await send_method(
            ReviewsService.format_reviews_header(empty_note="Пока пусто"),
            reply_markup=kb.get_all_cities_reviews_pagination_kb(0, 1),
        )
        await state.set_state(ReviewState.browsing_reviews)
        return

    total_pages = (total_reviews + REVIEWS_ALL_CITIES_PAGE_SIZE - 1) // REVIEWS_ALL_CITIES_PAGE_SIZE
    safe_page = max(0, min(int(page_index or 0), total_pages - 1))
    start = safe_page * REVIEWS_ALL_CITIES_PAGE_SIZE
    end = start + REVIEWS_ALL_CITIES_PAGE_SIZE
    page_reviews = reviews[start:end]

    rating = ReviewsService.rating_stats_from_reviews(reviews)
    lines = [
        ReviewsService.format_reviews_header(
            rating=rating,
            page_label=f"{safe_page + 1}/{total_pages}",
        )
    ]
    for offset, review in enumerate(page_reviews, start=1):
        lines.append(ReviewsService.format_public_review_card(review, index=start + offset))

    await state.update_data(rev_all_cities_mode=True, rev_all_cities_page=safe_page)
    await send_method(
        "\n".join(lines).rstrip(),
        reply_markup=kb.get_all_cities_reviews_pagination_kb(safe_page, total_pages),
    )
    await state.set_state(ReviewState.browsing_reviews)

async def _show_review_page(send_method, index, state):
    data = await state.get_data()
    city_filter = data.get('rev_search_city')
    region_filter = data.get('rev_search_region')
    product_filter = data.get('rev_search_product')
    product_id = data.get('rev_search_product_id')
    all_city_mode = bool(data.get('rev_all_city_mode'))
    city_label = ReviewsService.format_city_region_label(city_filter, region_filter) if city_filter else None
    await state.update_data(rev_current_index=index)
    
    review, total_real = ReviewsService.get_paginated_review(
        index,
        city_filter,
        product_filter,
        product_id_filter=product_id,
        region_filter=region_filter,
    )
    
    if not review:
        if product_filter and city_filter:
            text = ReviewsService.format_reviews_header(
                empty_note=(
                    "По этой позиции пусто,\n"
                    "смотрите отзывы города"
                ),
                city=str(city_label or city_filter),
                product=str(product_filter),
            )
        else:
            text = ReviewsService.format_reviews_header(
                empty_note="Пока пусто",
                city=str(city_label) if city_label else None,
                product=str(product_filter) if product_filter else None,
            )
        buttons = []
        if product_id:
            buttons.append([
                InlineKeyboardButton(text="🛒 Взять этот товар", callback_data=f"prod_{product_id}"),
                InlineKeyboardButton(text="🎮 Вернуться в каталог", callback_data="rev_back_products"),
            ])
            buttons.append([
                InlineKeyboardButton(
                    text=kb.reviews_button_text(str(city_filter or ""), region_name=region_filter),
                    callback_data="catalog_all_reviews",
                )
            ])
        elif all_city_mode:
            buttons.append([InlineKeyboardButton(text="🎮 Вернуться в каталог", callback_data="rev_back_products")])
        elif city_filter:
            buttons.append([
                InlineKeyboardButton(
                    text=kb.reviews_button_text(str(city_filter), region_name=region_filter),
                    callback_data="catalog_all_reviews",
                )
            ])
        else:
            buttons.append([InlineKeyboardButton(text="Поиск города", callback_data="rev_search_start")])
        buttons.append([InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")])

        await send_method(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await state.set_state(ReviewState.browsing_reviews)
        return

    # Visual Counter based purely on real data
    total_visual = total_real
    current_visual = total_visual - index
    if current_visual < 1:
        current_visual = 1
        total_visual = max(total_visual, 1)

    rating = ReviewsService.get_rating_stats(
        city_filter,
        product_filter,
        product_id_filter=product_id,
        region_filter=region_filter,
    )
    header = ReviewsService.format_reviews_header(
        rating=rating,
        page_label=f"{current_visual}/{total_visual}",
        city=str(city_label) if city_label else None,
        product=str(product_filter) if product_filter else None,
    )
    text = (
        f"{header}\n"
        f"{ReviewsService.format_public_review_card(review)}"
    )

    display_str = f"{current_visual}/{total_visual}"
    review_markup = kb.get_reviews_pagination_kb(
        index,
        total_real,
        city_filter,
        display_text=display_str,
        product_id=product_id,
        back_callback="rev_back_products" if all_city_mode and not product_id else None,
        show_city_reviews=bool(city_filter and not all_city_mode),
        region_name=region_filter,
    )
    await send_method(text, reply_markup=review_markup)
    await state.set_state(ReviewState.browsing_reviews)

@router.callback_query(F.data == 'rev_back_products')
async def rev_back_products(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get('city')
    population = data.get('population', 0)

    await state.update_data(
        rev_search_city=None, rev_search_region=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        population,
        header=f"💎 <b>В магазин · {html.escape(str(city_name or '-'))}</b>",
        scope="regular",
    )
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(
        callback,
        catalog_text,
        reply_markup=_build_catalog_markup(city_name, population, region_name=data.get("region")),
        state=state,
    )
    await state.set_state(OrderState.choosing_product)


@router.callback_query(F.data == "catalog_all_reviews", OrderState.choosing_product)
@router.callback_query(F.data == "catalog_all_reviews", ReviewState.browsing_reviews)
async def catalog_all_reviews(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("catalog_scope") == "miniopt":
        await callback.answer("Для mini-опта отзывы не используются.", show_alert=True)
        return
    city_name = data.get("city")
    if not city_name:
        await callback.answer("Сначала выберите город.", show_alert=True)
        return

    await _safe_answer_callback(callback, "Открываю отзывы...")
    await _send_all_city_reviews(callback.message, state, city_name)

@router.callback_query(F.data.startswith("prod_review_"))
async def show_product_reviews(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get('city')
    if not city_name:
        await callback.answer("Сессия города истекла. Введите город заново.", show_alert=True)
        return

    product_id = callback.data.split('_', 2)[2]
    product = CatalogService.get_product_by_id(
        product_id,
        city_name=city_name,
        region_name=data.get("region"),
    )
    if not product:
        await callback.answer("Товар недоступен.", show_alert=True)
        return

    await _safe_answer_callback(callback, "Открываю оценки...")
    if callback.message:
        try:
            await callback.message.edit_text("⭐ <b>Открываю оценки этой позиции...</b>")
            await _remember_menu_message(state, callback.message)
        except TelegramBadRequest:
            pass

    await state.update_data(
        rev_search_city=city_name,
        rev_search_region=data.get("region"),
        rev_search_product=product.get('name'),
        rev_search_product_id=product_id,
        rev_all_city_mode=False,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    await _show_review_page(lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state), 0, state)

async def _safe_answer_callback(callback: CallbackQuery, *args, **kwargs):
    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest as err:
        error_text = str(err).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            return
        pass

async def _safe_edit_or_send(callback: CallbackQuery, text: str, reply_markup=None, state: FSMContext | None = None):
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
        if state:
            await _remember_menu_message(state, callback.message)
    except TelegramBadRequest:
        try:
            sent = await callback.message.answer(text, reply_markup=reply_markup)
            if state:
                await _remember_menu_message(state, sent)
        except TelegramBadRequest:
            pass
    
@router.callback_query(F.data.startswith('rev_nav_'))
async def process_rev_nav(callback: CallbackQuery, state: FSMContext):
    try:
        page_idx = int(callback.data.split('_')[2])
    except:
        page_idx = 0

    await _safe_answer_callback(callback)
    await _show_review_page(lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state), page_idx, state)


@router.callback_query(F.data.startswith('rev_all_nav_'))
async def process_rev_all_nav(callback: CallbackQuery, state: FSMContext):
    try:
        page_idx = int(callback.data.split('_')[3])
    except Exception:
        page_idx = 0

    await _safe_answer_callback(callback)
    await _show_all_cities_reviews_page(
        lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state),
        page_idx,
        state,
    )

@router.callback_query(F.data == 'rev_search_start')
async def rev_search_start(callback: CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(
        callback,
        "⭐ <b>Оценки покупателей</b>\n\nНапишите город, по которому искать оценки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")]]),
        state=state,
    )
    await state.set_state(ReviewState.waiting_for_search_city)

@router.message(ReviewState.waiting_for_search_city)
async def rev_search_process(message: Message, state: FSMContext):
    city_query = message.text.strip()
    await state.update_data(
        rev_search_city=city_query,
        rev_search_region=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)

@router.callback_query(F.data == 'rev_search_reset')
async def rev_search_reset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(
        rev_search_city=None, rev_search_region=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    await _safe_answer_callback(callback)
    await _show_review_page(lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state), 0, state)


@router.message(ReviewState.browsing_reviews)
async def process_review_commands(message: Message, state: FSMContext):
    data = await state.get_data()
    current_index = int(data.get('rev_current_index', 0) or 0)
    current_all_cities_page = int(data.get('rev_all_cities_page', 0) or 0)
    all_cities_mode = bool(data.get('rev_all_cities_mode'))
    city_for_dump = data.get('rev_search_city') or data.get('city')
    all_city_mode = bool(data.get('rev_all_city_mode'))

    if _matches_command(message.text, "след", "next"):
        if all_cities_mode:
            await _show_all_cities_reviews_page(
                lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
                current_all_cities_page + 1,
                state,
            )
        else:
            await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), current_index + 1, state)
        return
    if _matches_command(message.text, "пред", "prev"):
        if all_cities_mode:
            await _show_all_cities_reviews_page(
                lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
                max(0, current_all_cities_page - 1),
                state,
            )
        else:
            await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), max(0, current_index - 1), state)
        return
    if _matches_command(message.text, "каталог", "catalog"):
        city_name = data.get('city')
        population = data.get('population', 0)
        if not city_name:
            await state.clear()
            await send_home_to_chat(
                message.bot,
                message.chat.id,
                state=state,
                prefix=f"🔵 Сначала нажмите «{kb.BTN_BUY}» и напишите город.",
            )
            return
        await state.update_data(
            rev_search_city=None, rev_search_region=None,
            rev_search_product=None,
            rev_search_product_id=None,
            rev_all_city_mode=False,
            rev_all_cities_mode=False,
            rev_all_cities_page=0,
            rev_return_city=None,
            rev_return_product=None,
            rev_return_product_id=None,
            rev_return_index=None,
        )
        catalog_text = await _build_catalog_text(
            state,
            city_name,
            population,
            header=f"💎 <b>В магазин · {html.escape(str(city_name or '-'))}</b>",
        )
        await _replace_menu_message(
            message,
            state,
            catalog_text,
            reply_markup=_build_catalog_markup(city_name, population, region_name=data.get("region")),
        )
        await state.set_state(OrderState.choosing_product)
        return
    if _matches_command(message.text, "назад", "back") and data.get('rev_all_city_mode'):
        await _restore_after_all_city_reviews(
            lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
            state,
        )
        return
    if _matches_command(message.text, "купить", "buy"):
        product_id = data.get('rev_search_product_id')
        if not product_id:
            await message.answer("🔶 Эта команда доступна только в отзывах по конкретному товару.")
            return
        await _start_product_checkout(message.from_user.id, message, state, product_id)
        return
    if _matches_command(message.text, "сброс", "reset"):
        if all_cities_mode:
            await _show_all_cities_reviews_page(
                lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
                0,
                state,
            )
        else:
            await state.update_data(
                rev_search_city=None, rev_search_region=None,
                rev_search_product=None,
                rev_search_product_id=None,
                rev_all_city_mode=False,
                rev_all_cities_mode=False,
                rev_all_cities_page=0,
                rev_return_city=None,
                rev_return_product=None,
                rev_return_product_id=None,
                rev_return_index=None,
            )
            await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)
        return
    if _matches_command(message.text, "allrew"):
        if not city_for_dump:
            await message.answer("🔶 Сначала выберите город, затем откройте отзывы кнопкой в каталоге.")
            return
        await _send_all_city_reviews(message, state, city_for_dump)
        return

    search_match = re.fullmatch(r"/?поискотзывов\s+(.+)", (message.text or "").strip(), flags=re.IGNORECASE)
    if search_match:
        city_query = search_match.group(1).strip()
        await state.update_data(
            rev_search_city=city_query,
            rev_search_region=None,
            rev_search_product=None,
            rev_search_product_id=None,
            rev_all_cities_mode=False,
            rev_all_cities_page=0,
        )
        await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)
        return

    await message.answer("🔶 Здесь работают кнопки под оценками: листать, вернуться в каталог или искать другой город.")

@router.callback_query(F.data == "write_review")
async def start_review(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if BlacklistService.is_banned(user_id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    # User request: Only admin can write reviews manually via this button.
    # Others get the "need 1 purchase" message.
    if str(user_id) != str(ADMIN_ID):
        await callback.answer("🔒 Оставлять отзывы могут только клиенты с минимум 1 покупкой!", show_alert=True)
        return
        
    await callback.message.answer(
        "✍️ <b>Новый отзыв (Admin Mode)</b>\n"
        "Пожалуйста, напишите ваш честный отзыв одним сообщением.", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")]])
    )
    await state.set_state(ReviewState.waiting_for_text)

@router.message(ReviewState.waiting_for_text)
async def process_review_text(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        return

    # Проверяем "тихий" режим (если заказ подтвержден вручную)
    if UserService.get_silent_review_mode(user_id):
        # Если включен тихий режим - отзыв НЕ отправляется админу
        # Но пользователь видит успех.
        # Сбрасываем режим
        UserService.set_silent_review_mode(user_id, False)
        
        await message.answer("✅ <b>Успешно!</b>\nВаш отзыв принят системой! Спасибо за доверие.")
        await state.clear()
        return

    # Имитация сохранения (обычный режим)
    await message.answer("✅ <b>Успешно!</b>\nВаш отзыв отправлен на модерацию и скоро появится в списке.")
    
    # Уведомляем админа просто для информации (не обязательно, но полезно)
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.from_user.id}"
    await notify_admin(bot, f"📝 Новый отзыв от {user_info}:\n{message.text}")
    
    await state.clear()


@router.message(
    F.text.in_(
        {
            kb.BTN_RULES,
            kb.BTN_SUPPORT,
            "УСЛОВИЯ ПОЛЬЗОВАНИЯ",
            "❓ Поддержка и правила",
            "Поддержка и правила",
            "❓ Ненаходы и поддержка",
            "Ненаходы и поддержка",
            "📜 Правила",
        }
    )
)
async def show_rules(message: Message, state: FSMContext):
    text = build_shop_rules_text(SettingsService.get_support_contact())
    await _send_long_html_chunks(message, text, reply_markup=kb.get_rules_keyboard(), state=state)


async def open_support_chat(
    target: Message,
    state: FSMContext,
    bot: Bot,
    *,
    user_id: int,
    username: str | None,
) -> None:
    if BlacklistService.is_banned(user_id):
        await target.answer("🔒 Вы заблокированы и не можете писать в поддержку.")
        return

    await target.answer(
        "💬 <b>Поддержка PlayStation™Store</b>\n\n"
        "📝 Опишите вопрос кратко и одним сообщением.\n"
        "Непонятные и нелогичные сообщения будут проигнорированы.\n\n"
        "Закрыть диалог можно кнопкой <b>↩️ На главную</b>.",
        reply_markup=kb.get_support_chat_keyboard(),
    )
    await state.set_state(OrderState.chat_with_support)

    uname = username or "-"
    await _send_message_to_admins(
        bot,
        f"🆘 <b>Поддержка</b>\n"
        f"Пользователь {user_id} (@{uname}) открыл чат.\n"
        f"Ожидайте сообщения от него.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")]
        ]),
    )


@router.message(F.text.in_({kb.BTN_SUPPORT_CHAT, "вызвать оператора", "❓ Поддержка", "Поддержка"}))
async def start_support_from_menu(message: Message, state: FSMContext, bot: Bot):
    await open_support_chat(
        message,
        state,
        bot,
        user_id=message.from_user.id,
        username=message.from_user.username,
    )


@router.callback_query(F.data.in_({"support_write", "rules_contact_support"}))
async def start_support_from_button(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if BlacklistService.is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы и не можете писать в поддержку.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await open_support_chat(
            callback.message,
            state,
            bot,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
        )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    # Check permanent ban
    if BlacklistService.is_banned(message.from_user.id):
        return 
        
    # Check temporary ban
    is_temp_banned, until = UserService.is_temp_banned(message.from_user.id)
    if is_temp_banned:
        await message.answer(f"🔒 <b>Вы временно заблокированы</b> до {until} за частые отмены заказов.")
        return

    # Разбираем реферальный аргумент (например /start 12345)
    args = message.text.split()
    referrer_id = None
    if len(args) > 1:
        potential_ref = args[1]
        if potential_ref.isdigit(): # простой чек на ID
            referrer_id = potential_ref

    await state.clear()
    await state.update_data(pending_referrer_id=referrer_id)
    BotStatsService.note_start(message.from_user.id)
    await _send_access_captcha(message, state)

    ref_info = f" (по приглашению {referrer_id})" if referrer_id else ""
    await notify_admin(bot, f"Пользователь {message.from_user.id} (@{message.from_user.username}) запустил бота{ref_info} (проходит капчу).")

@router.message(F.text.contains("Профиль"))
@router.message(Command("profile"))
async def show_profile(message: Message, bot: Bot):
    user = UserService.get_user(message.from_user.id)
    if not user:
        await message.answer("Личный счёт закрыт, введите /start")
        return

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    username = user.get("username") or message.from_user.username or "Неизвестно"
    text = _profile_body(user, message.from_user.id, username, ref_link)
    await message.answer(text, reply_markup=_profile_keyboard(ref_link))


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city") or data.get("rev_search_city")
    await message.answer(_build_user_help_text(city_name))

@router.message(OrderState.waiting_for_captcha)
async def process_captcha(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    correct = str(data.get("captcha_correct") or "")
    kind = str(data.get("captcha_kind") or "letters")

    if answers_match(message.text, correct):
        referrer_id = data.get("pending_referrer_id")
        is_new = UserService.add_user(message.from_user.id, message.from_user.username, referrer_id)
        UserService.touch_last_bot(message.from_user.id, bot.id)
        BotStatsService.note_captcha(message.from_user.id, is_new=bool(is_new))

        if is_new:
            ref_info = f" (по приглашению {referrer_id})" if referrer_id else ""
            await notify_admin(
                bot,
                f"✅ Новый пользователь {message.from_user.id} (@{message.from_user.username}) прошел капчу{ref_info}."
            )
            if referrer_id:
                ReferralRaceService.note_gray(referrer_id, message.from_user.id)

        await send_home_to_chat(message.bot, message.chat.id, state=state)
        return

    if kind == "math":
        await message.answer("❗ Неверный ответ. Отправляю новый пример.")
    else:
        await message.answer("❗ Символы не совпали. Отправляю новую капчу.")
    BotStatsService.note_captcha_fail(message.from_user.id)
    await _send_access_captcha(message, state)


@router.callback_query(F.data.startswith("captcha_"), OrderState.waiting_for_captcha)
async def process_legacy_captcha_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Капча обновилась. Введите ответ с новой картинки.", show_alert=True)
    await _send_access_captcha(callback.message, state)


async def _handle_legacy_menu_text(message: Message, state: FSMContext, bot: Bot, city_name: str) -> bool:
    panel = _reply_panel_action(city_name)
    if panel == "balance":
        await _show_balance_card(message, message.from_user.id)
        return True
    if panel == "purchases":
        await _show_purchases_card(message, message.from_user.id)
        return True
    if city_name in {kb.BTN_BUY, kb.BTN_BUY_LEGACY, "Купить", "🛒 Купить", "В магазин"}:
        await _prompt_city_input(message, state)
        return True
    if city_name in {"👤 Профиль", kb.BTN_PROFILE, "КАБИНЕТ"} or "Профиль" in city_name:
        await show_profile(message, bot)
        return True
    if city_name in {kb.BTN_COUPON, "КУПОН"}:
        await _start_coupon_activation(message, state)
        return True
    if city_name in {kb.BTN_CHEST, "БОНУС"}:
        await _send_chest(message)
        return True
    if city_name == kb.BTN_ROULETTE:
        await _send_roulette_games(message)
        return True
    if city_name in {kb.BTN_ALL_CITIES_REVIEWS, "СИГНАЛЫ"}:
        await _open_all_cities_reviews_message(message, state)
        return True
    if city_name in {kb.BTN_CONTACTS, "СВЯЗЬ"}:
        await show_contacts(message)
        return True
    if city_name in {kb.BTN_GUIDE, "мануал"}:
        await _send_user_guide_pdf(message)
        return True
    if city_name in {
        kb.BTN_RULES,
        kb.BTN_SUPPORT,
        "УСЛОВИЯ ПОЛЬЗОВАНИЯ",
        "❓ Поддержка и правила",
        "Поддержка и правила",
        "❓ Ненаходы и поддержка",
        "Ненаходы и поддержка",
        "📜 Правила",
    }:
        await show_rules(message, state)
        return True
    if city_name in {kb.BTN_SUPPORT_CHAT, "вызвать оператора", "❓ Поддержка", "Поддержка"}:
        await open_support_chat(
            message,
            state,
            bot,
            user_id=message.from_user.id,
            username=message.from_user.username,
        )
        return True
    if city_name in {kb.BTN_DISPUTE, "Создать Диспут", "Создать диспут"}:
        from app.handlers.dispute import start_dispute_flow

        await start_dispute_flow(message, state, user_id=message.from_user.id)
        return True
    if city_name in {kb.BTN_MISSIONS, "Миссии", "🎯 Миссии"}:
        await _show_missions_for(message, message.from_user.id)
        return True
    return False


async def _show_profile_for(target: Message, bot: Bot, user_id: int, username: str | None) -> None:
    user = UserService.get_user(user_id)
    if not user:
        await target.answer("Личный счёт закрыт, введите /start")
        return
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    shown_name = username or user.get("username") or "Неизвестно"
    text = _profile_body(user, user_id, shown_name, ref_link)
    await target.answer(text, reply_markup=_profile_keyboard(ref_link))


@router.message(OrderState.main_menu)
async def process_main_menu_text(message: Message, state: FSMContext, bot: Bot):
    city_name = " ".join((message.text or "").strip().split())
    if not city_name:
        await message.answer("Выберите раздел кнопкой под этим сообщением.", reply_markup=kb.get_main_menu_keyboard())
        return
    if await _handle_legacy_menu_text(message, state, bot, city_name):
        return
    await message.answer(
        f"Нужен раздел на экране или «{kb.BTN_BUY}» внизу.",
        reply_markup=kb.get_main_menu_keyboard(),
    )


def _split_price_text(text: str, max_len: int = 3900) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return ["◆ <b>Товар</b>"]
    if len(value) <= max_len:
        return [value]
    chunks: list[str] = []
    current = ""
    for paragraph in value.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > max_len:
            chunks.append(paragraph[:max_len])
            paragraph = paragraph[max_len:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks or [value[:max_len]]


async def _send_price_text(bot: Bot, chat_id: int, text: str, markup) -> None:
    chunks = _split_price_text(text)
    last_index = len(chunks) - 1
    for index, chunk in enumerate(chunks):
        keyboard = markup if index == last_index else None
        try:
            await bot.send_message(
                chat_id,
                chunk,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await bot.send_message(
                chat_id,
                html.escape(chunk),
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )


async def _show_price_list(callback: CallbackQuery) -> None:
    catalog = SettingsService.get_featured_catalog()
    items = catalog.get("items") or []
    if not SettingsService.is_price_menu_visible(catalog):
        await callback.answer("Прайс пока пустой", show_alert=True)
        return
    text = "🏷 <b>Цены Store</b>\n\nОткройте карточку товара кнопкой ниже."
    markup = kb.get_price_list_keyboard(items)
    message = callback.message
    if message:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            try:
                await message.delete()
            except Exception:
                pass
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


async def _show_price_item_card(callback: CallbackQuery, item_id: str) -> None:
    item = SettingsService.get_featured_item(item_id)
    if not item:
        await callback.answer("Позиция не найдена", show_alert=True)
        await _show_price_list(callback)
        return
    text = SettingsService.featured_item_text(item)
    markup = kb.get_price_item_keyboard()
    chat_id = callback.from_user.id
    photo = item.get("photo_file_id")
    sent = False
    if photo:
        try:
            await callback.bot.send_photo(chat_id, photo)
            sent = True
        except Exception:
            logger.exception("Failed to send price photo for %s", item_id)
    await _send_price_text(callback.bot, chat_id, text, markup)
    sent = True
    if sent and callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass


@router.callback_query(F.data == "menu_price")
async def menu_price(callback: CallbackQuery):
    await callback.answer()
    await _show_price_list(callback)


@router.callback_query(F.data.startswith("price_item_"))
async def menu_price_item(callback: CallbackQuery):
    await callback.answer()
    item_id = (callback.data or "")[len("price_item_"):]
    await _show_price_item_card(callback, item_id)


async def _open_valley_shop(message: Message, state: FSMContext, user_id: int) -> None:
    if UserService.get_active_order(user_id):
        await message.answer(
            "🕒 <b>Заказ еще не закрыт</b>\n\n"
            f"Сначала нажмите <b>↩️ На главную</b>, потом снова <b>{html.escape(kb.BTN_BUY)}</b>.",
            reply_markup=kb.get_base_keyboard(user_id),
        )
        return
    await state.set_state(OrderState.waiting_for_city)
    await message.answer(kb.CITY_INPUT_TEXT, reply_markup=kb.get_base_keyboard(user_id))


async def _show_balance_card(message: Message, user_id: int) -> None:
    await message.answer(
        _wallet_history_text(user_id),
        reply_markup=kb.get_wallet_keyboard(),
    )


async def _show_purchases_card(message: Message, user_id: int) -> None:
    await message.answer(
        _paid_orders_text(user_id),
        reply_markup=kb.get_base_keyboard(user_id),
    )


@router.message(F.text.in_({kb.BTN_BUY, kb.BTN_BUY_LEGACY, kb.BTN_BUY_OLD, "В магазин", "Купить"}))
async def valley_open_shop(message: Message, state: FSMContext):
    await _open_valley_shop(message, state, message.from_user.id)


@router.callback_query(F.data == "menu_buy")
async def menu_buy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message:
        await _open_valley_shop(callback.message, state, callback.from_user.id)


@router.callback_query(F.data == "menu_profile")
async def menu_profile(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if callback.message:
        await _show_profile_for(
            callback.message,
            bot,
            callback.from_user.id,
            callback.from_user.username,
        )


async def _show_missions_for(target: Message, user_id: int) -> None:
    view = LoyaltyService.get_view(int(user_id))
    await target.answer(
        LoyaltyService.build_missions_text(int(user_id)),
        reply_markup=kb.get_missions_keyboard(view.get("can_claim")),
    )


@router.callback_query(F.data == "menu_missions")
async def menu_missions(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await _show_missions_for(callback.message, callback.from_user.id)


@router.callback_query(F.data == "missions_claim")
async def missions_claim(callback: CallbackQuery):
    result = LoyaltyService.claim_weekly_reward(callback.from_user.id)
    if result.get("ok"):
        amount = result.get("amount") or 0
        balance = result.get("balance") or 0
        await callback.answer("Награда зачислена", show_alert=True)
        text = (
            f"✅ Награда недели зачислена: <b>+{amount:.0f} ₽</b>\n"
            f"💰 Баланс: <b>{balance:.0f} ₽</b>\n\n"
            + LoyaltyService.build_missions_text(callback.from_user.id)
        )
        if callback.message:
            try:
                await callback.message.edit_text(
                    text,
                    reply_markup=kb.get_missions_keyboard(False),
                )
            except TelegramBadRequest:
                await callback.message.answer(
                    text,
                    reply_markup=kb.get_missions_keyboard(False),
                )
        await notify_bonus_cap(callback.bot, callback.from_user.id)
        return
    errors = {
        "claimed": "Награда за эту неделю уже получена.",
        "incomplete": "Сначала закройте все задания недели.",
        "missions_off": "Сейчас миссии выключены.",
        "no_tasks": "Задания ещё не настроены.",
        "no_reward": "Награда не задана.",
        "bonus_cap": "Лимит бонусов 1500 ₽. Снять его можно, если приглашённый реферал купит.",
    }
    await callback.answer(errors.get(result.get("error"), "Не удалось забрать награду."), show_alert=True)
    if result.get("error") == "bonus_cap":
        await notify_bonus_cap(callback.bot, callback.from_user.id)


@router.callback_query(F.data == "menu_contacts")
async def menu_contacts(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(SettingsService.get_contacts_text(), reply_markup=kb.get_base_keyboard())


def _chest_text(snapshot: dict) -> str:
    if snapshot.get("bonus_capped"):
        return (
            "🎰 <b>Рулетка Store на паузе</b>\n\n"
            "В кошельке уже потолок бонусов, 1 500 ₽.\n"
            "Спин откроется, когда друг по вашей ссылке оплатит заказ."
        )
    if snapshot.get("free_used"):
        return (
            "🎰 <b>Спин на сегодня уже сыгран</b>\n\n"
            "Колесо снова откроется завтра.\n"
            "Один заход в сутки."
        )
    return (
        "🎰 <b>Рулетка PlayStation™Store</b>\n\n"
        "Один спин в сутки.\n"
        "Секторы колеса: <b>10 ₽ – 700 ₽</b>.\n"
        "Выигрыш сразу падает в кошелек."
    )


async def _send_chest(message: Message) -> None:
    snapshot = ChestService.snapshot(message.from_user.id)
    await message.answer(_chest_text(snapshot), reply_markup=kb.get_chest_keyboard(snapshot))


@router.callback_query(F.data == "menu_chest")
async def menu_chest(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await _send_chest(callback.message)


@router.callback_query(F.data == "chest_open")
async def chest_open(callback: CallbackQuery):
    result = ChestService.open(callback.from_user.id)
    if not result.get("ok"):
        if result.get("error") == "bonus_cap":
            await callback.answer("Лимит бонусов 1500 ₽", show_alert=True)
            await notify_bonus_cap(callback.bot, callback.from_user.id)
            if callback.message:
                snapshot = ChestService.snapshot(callback.from_user.id)
                try:
                    await callback.message.edit_text(
                        _chest_text(snapshot),
                        reply_markup=kb.get_chest_keyboard(snapshot),
                    )
                except TelegramBadRequest:
                    pass
            return
        await callback.answer("Сегодняшний спин уже использован", show_alert=True)
        return
    prize = float(result.get("prize") or 0)
    text = (
        "🎰 <b>Колесо остановилось</b>\n\n"
        f"Сектор: <b>{prize:.0f} ₽</b>\n"
        f"В кошельке: <b>{float(result.get('balance') or 0):.0f} ₽</b>\n\n"
        "Следующий спин будет завтра."
    )
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.get_chest_keyboard(ChestService.snapshot(callback.from_user.id)))
    await notify_bonus_cap(callback.bot, callback.from_user.id)


@router.callback_query(F.data.startswith("chest_boost_"))
async def chest_boost(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Усиление недоступно. Есть только один шанс в день.", show_alert=True)


@router.callback_query(F.data == "menu_coupon")
async def menu_coupon(callback: CallbackQuery, state: FSMContext):
    await _start_coupon_activation(callback, state)


@router.callback_query(F.data == "menu_reviews")
async def menu_reviews(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message:
        await _open_all_cities_reviews_message(callback.message, state)


@router.callback_query(F.data == "menu_guide")
async def menu_guide(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await _send_user_guide_pdf(callback.message)


@router.callback_query(F.data == "menu_rules")
async def menu_rules(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message:
        text = build_shop_rules_text(SettingsService.get_support_contact())
        await _send_long_html_chunks(callback.message, text, reply_markup=kb.get_rules_keyboard(), state=state)


@router.callback_query(F.data == "menu_support")
async def menu_support(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if BlacklistService.is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы и не можете писать в поддержку.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await open_support_chat(
            callback.message,
            state,
            bot,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
        )


@router.callback_query(F.data == "menu_dispute")
async def menu_dispute(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message:
        from app.handlers.dispute import start_dispute_flow

        await start_dispute_flow(callback.message, state, user_id=callback.from_user.id)


@router.message(OrderState.waiting_for_city)
async def process_city(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        return
        
    raw_city_name = message.text or ""
    city_name = " ".join(raw_city_name.strip().split())
    print(f"[CityFlow] main search start user={user_id} query='{city_name}'")
    if not city_name:
        await message.answer("🔶 Название города не может быть пустым. Попробуйте еще раз.")
        return
    
    if await _handle_legacy_menu_text(message, state, bot, city_name):
        return
        
    # Проверка на навигацию
    if city_name in kb.CANCEL_TEXT_ALIASES or city_name in {kb.BTN_CLOSE_CHAT, "закрыть канал"}:
         return

    # 1. Пытаемся удалить сообщение пользователя для чистоты чата
    try:
        await message.delete()
    except:
        pass

    msg = await message.answer(f"🔎 <b>Проверяю город</b>: {city_name}")
    
    try:
        candidates = await GeoService.search_cities(city_name)
        print(f"[CityFlow] main search first attempt user={user_id} query='{city_name}' count={len(candidates or [])}")
        if not candidates:
            # Автоповтор запроса на случай кратковременного сбоя
            await asyncio.sleep(0.4)
            candidates = await GeoService.search_cities(city_name)
            print(f"[CityFlow] main search retry user={user_id} query='{city_name}' count={len(candidates or [])}")
    except Exception as e:
        print(f"[CityFlow] main search exception user={user_id} query='{city_name}' err='{e}'")
        BotStatsService.note_city_search(user_id, found=False)
        await msg.edit_text(f"🔶 <b>Сервис недоступен</b>\nНе удалось проверить город (проблемы с подключением).")
        return
    
    found = bool(candidates)
    BotStatsService.note_city_search(user_id, found=found)
    if not candidates:
        print(f"[CityFlow] main search no candidates user={user_id} query='{city_name}'")
        data = await state.get_data()
        fail_count = data.get("city_fail_count", 0) + 1
        await state.update_data(city_fail_count=fail_count)

        await msg.edit_text(
            "❗ <b>Такой город Store не нашел.</b>\n" \
            "Проверьте написание и пришлите название еще раз."
        )

        is_banned_now = False
        if fail_count >= 2:
            is_banned_now = BlacklistService.report_bad_input(user_id)
            if is_banned_now:
                await message.answer(
                    "🛑️ <b>Вход в Store закрыт</b>\n"
                    "Слишком много неверных городов подряд. Доступ временно закрыт."
                )
                await notify_admin(bot, f"🔨 АВТО-БАН: Пользователь {user_id} заблокирован за спам невалидными городами.")
                await state.clear()
        return

    await state.update_data(city_fail_count=0)

    # Если вариантов несколько - предлагаем выбор
    if len(candidates) > 1:
        await state.update_data(city_candidates=candidates)
        lines = [f"🤔 Мы нашли несколько вариантов для <b>{city_name}</b>.", "", "Выберите нужный город кнопкой ниже:"]
        buttons = []
        for index, candidate in enumerate(candidates, start=1):
            label = candidate['name']
            if candidate.get('region'):
                label += f" ({candidate['region']})"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"city_opt_{index - 1}")])
        buttons.append([InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")])
        await msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await _remember_menu_message(state, msg)
        await state.set_state(OrderState.choosing_city_option)
        return

    # Если вариант только один - используем его сразу
    # Передаем msg, чтобы не плодить новые сообщения, а редактировать это
    await _finalize_city_selection(message, state, bot, candidates[0], existing_msg=msg)

@router.callback_query(F.data.startswith("city_opt_"), OrderState.choosing_city_option)
async def process_city_option(callback: CallbackQuery, state: FSMContext, bot: Bot):
    idx = int(callback.data.split("_")[2])
    data = await state.get_data()
    candidates = data.get('city_candidates', [])
    
    if idx < 0 or idx >= len(candidates):
        await callback.answer("Ошибка выбора варианта.", show_alert=True)
        return
        
    selected_candidate = candidates[idx]
    
    # Редактируем сообщение с меню выбора
    await _finalize_city_selection(callback.message, state, bot, selected_candidate, existing_msg=callback.message)


@router.message(OrderState.choosing_city_option)
async def process_city_option_text(message: Message, state: FSMContext, bot: Bot):
    choice = _parse_index_command(message.text, "city", "город")
    data = await state.get_data()
    candidates = data.get('city_candidates', [])
    if not choice or choice < 1 or choice > len(candidates):
        await message.answer("🔶 Выберите нужный вариант города кнопкой ниже.")
        return
    await _finalize_city_selection(message, state, bot, candidates[choice - 1])

async def _finalize_city_selection(message: Message, state: FSMContext, bot: Bot, candidate: dict, existing_msg: Message = None):
    # Теперь дополучаем полную инфу (районы и т.д.)
    if existing_msg:
        await existing_msg.edit_text(
            f"🔵 <b>Собираю каталог Store</b>\nГород: <code>{html.escape(str(candidate['name']))}</code>"
        )
        searching_msg = existing_msg
        await _remember_menu_message(state, existing_msg)
    else:
        searching_msg = await _replace_menu_message(
            message,
            state,
            f"🔵 <b>Собираю каталог Store</b>\nГород: <code>{html.escape(str(candidate['name']))}</code>",
        )
    
    city_info = await GeoService.get_city_details(candidate)
    
    # Каталог доступен только для городов с подтвержденным населением от 25k.
    # Это исключает обходы через fallback-значения и кэш малых населенных пунктов.
    if not GeoService.is_city_population_allowed(city_info, min_population=MIN_CITY_POPULATION):
        await searching_msg.edit_text(
            "🔒 <b>В этом населенном пункте Store не работает</b>\n\n"
            "Пришлите ближайший крупный город отдельным сообщением."
        )
        await state.set_state(OrderState.waiting_for_city)
        return
    
    # --- TRACKING: Save city as active for AI reviews ---
    ReviewsService.register_active_city(city_info['name'])
    
    # Сохраняем в память вычищенные данные, включая население
    await state.update_data(
        city=city_info['name'], 
        region=city_info.get('region') or "",
        districts=city_info['districts'],
        population=city_info.get('population', 0)
    )
    
    region_info = f" ({city_info['region']})" if city_info.get('region') else ""

    catalog_text = await _build_catalog_text(
        state,
        city_info['name'],
        city_info.get('population', 0),
        header=None,
    )
    await searching_msg.edit_text(
        catalog_text,
        reply_markup=_build_catalog_markup(
            city_info['name'],
            city_info.get('population', 0),
            region_name=city_info.get('region'),
        ),
    )
    await _remember_menu_message(state, searching_msg)
    await state.set_state(OrderState.choosing_product)
    
    # Получаем дату обновления прайса (если есть) или берем текущую
    updated_at = CatalogService.get_last_update_time(city_info['name'])
    if not updated_at:
        updated_at = get_now_msk().strftime("%d.%m.%Y %H:%M")
    
    await notify_admin(bot, f"Пользователь {message.chat.id} выбрал город: {city_info['name']}{region_info}")
    # await message.answer(f"🕒 Прайс обновлен: {updated_at}")


@router.callback_query(F.data.startswith("prod_"), ~F.data.startswith("prod_review_"))
async def process_product_selection(callback: CallbackQuery, state: FSMContext):
    product_id = str(callback.data or "")[len("prod_"):]
    await _safe_answer_callback(callback, "Открываю товар...")
    await _remember_menu_message(state, callback.message)
    await _start_product_checkout(callback.from_user.id, callback.message, state, product_id)


def _get_preorder_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Склад: что уже есть", callback_data="catalog_preorder_ready")],
        [InlineKeyboardButton(text="Заказать отдельно", callback_data="catalog_preorder_new")],
        [InlineKeyboardButton(text=kb.BTN_BACK, callback_data="catalog_regular")],
    ])


@router.callback_query(F.data == "catalog_preorder_menu")
async def catalog_preorder_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city")
    if not city_name:
        await callback.answer("Сначала выберите город.", show_alert=True)
        return
    await _safe_answer_callback(callback)
    text = (
        f"📦 <b>Крупный заказ · {html.escape(kb.SHOP_NAME)}</b>\n\n"
        "Два варианта:\n"
        "• Склад - позиции, которые можно забрать сейчас.\n"
        "• Отдельный заказ - напишите поддержке товар и объем."
    )
    await _safe_edit_or_send(
        callback,
        text,
        reply_markup=_get_preorder_menu_markup(),
        state=state,
    )


@router.callback_query(F.data == "catalog_preorder_ready")
async def catalog_preorder_ready(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city")
    if not city_name:
        await callback.answer("Сначала выберите город.", show_alert=True)
        return
    population = data.get("population", 0)
    region_name = data.get("region")
    products = CatalogService.get_miniopt_products_for_city(
        city_name, population, region_name=region_name
    )
    if not products:
        await _safe_answer_callback(callback)
        empty_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Заказать отдельно", callback_data="catalog_preorder_new")],
            [InlineKeyboardButton(text=kb.BTN_BACK, callback_data="catalog_preorder_menu")],
        ])
        await _safe_edit_or_send(
            callback,
            (
                "📦 <b>На складе сейчас пусто</b>\n\n"
                "Можно оставить отдельный заказ: поддержка согласует товар и объем."
            ),
            reply_markup=empty_markup,
            state=state,
        )
        return

    product_ids = [item.get("id") for item in products if item.get("id")]
    await state.update_data(catalog_product_ids=product_ids, catalog_scope="miniopt")
    header = kb.format_catalog_board(
        city_name,
        products,
        kind=_build_miniopt_header(city_name, region_name),
    )
    markup = kb.get_miniopt_products_keyboard(city_name, population, region_name=region_name)
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(callback, header, reply_markup=markup, state=state)
    await state.set_state(OrderState.choosing_product)


@router.callback_query(F.data == "catalog_preorder_new")
async def catalog_preorder_new(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city")
    if not city_name:
        await callback.answer("Сначала выберите город.", show_alert=True)
        return
    text = SettingsService.get_preorder_text()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="написать", callback_data="preorder_chat_start")],
        [InlineKeyboardButton(text=kb.BTN_BACK, callback_data="catalog_preorder_menu")],
    ])
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(callback, text, reply_markup=markup, state=state)


@router.callback_query(F.data == "preorder_chat_start")
async def preorder_chat_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    if BlacklistService.is_banned(user_id):
        await callback.answer("🔒 Вы заблокированы и не можете писать в поддержку.", show_alert=True)
        return
    data = await state.get_data()
    city_name = str(data.get("city") or "-")
    region_name = str(data.get("region") or "").strip()

    await _safe_answer_callback(callback)
    if callback.message:
        try:
            await callback.message.edit_text(
                "📝 Опишите одним сообщением: <b>что</b> нужно, <b>объём</b> и уточнения.\n"
                "Чтобы вернуться на главную, нажмите <b>↩️ На главную</b>.",
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Чат с оператором открыт. Пишите сюда.",
            reply_markup=kb.get_support_chat_keyboard(),
        )
    await state.set_state(OrderState.chat_with_support)

    username = callback.from_user.username or "-"
    location = html.escape(city_name)
    if region_name:
        location += f" · {html.escape(region_name)}"
    admin_text = (
        "🛒 <b>ПРЕДЗАКАЗ / ОПТ</b>\n"
        f"👤 Пользователь: {user_id} (@{html.escape(str(username))})\n"
        f"🏙 Город: {location}\n\n"
        "Клиент открыл чат по предзаказу. Ожидайте сообщения."
    )
    admin_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
    ])
    await _send_message_to_admins(bot, admin_text, reply_markup=admin_markup)


@router.callback_query(F.data == "catalog_miniopt", OrderState.choosing_product)
@router.callback_query(F.data == "catalog_miniopt", ReviewState.browsing_reviews)
async def open_miniopt_catalog(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city")
    population = data.get("population", 0)
    if not city_name:
        await callback.answer("Сначала выберите город.", show_alert=True)
        return
    await state.update_data(
        rev_search_city=None, rev_search_region=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_cities_mode=False,
        rev_all_cities_page=0,
    )
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        population,
        scope="regular",
    )
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(
        callback,
        catalog_text,
        reply_markup=_build_catalog_markup(
            city_name,
            population,
            scope="regular",
            region_name=data.get("region"),
        ),
        state=state,
    )
    await state.set_state(OrderState.choosing_product)


@router.callback_query(F.data == "catalog_regular", OrderState.choosing_product)
@router.callback_query(F.data == "catalog_regular", ReviewState.browsing_reviews)
async def open_regular_catalog(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city")
    population = data.get("population", 0)
    if not city_name:
        await callback.answer("Сначала выберите город.", show_alert=True)
        return
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        population,
        header=f"💎 <b>В магазин · {html.escape(str(city_name or '-'))}</b>",
        scope="regular",
    )
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(
        callback,
        catalog_text,
        reply_markup=_build_catalog_markup(
            city_name,
            population,
            scope="regular",
            region_name=data.get("region"),
        ),
        state=state,
    )
    await state.set_state(OrderState.choosing_product)


@router.callback_query(F.data.startswith("mini_prod_"))
async def process_miniopt_selection(callback: CallbackQuery, state: FSMContext):
    product_id = callback.data.split("_", 2)[2]
    await _safe_answer_callback(callback, "Открываю товар...")
    await _remember_menu_message(state, callback.message)
    await _start_scoped_product_checkout(callback.from_user.id, callback.message, state, product_id, "miniopt")


@router.message(OrderState.choosing_product)
async def process_product_selection_text(message: Message, state: FSMContext):
    data = await state.get_data()
    scope = data.get("catalog_scope", "regular")

    if (message.text or "").strip() == kb.BTN_ALL_CITIES_REVIEWS:
        if scope == "miniopt":
            await message.answer("🔶 Для mini-опта отзывы не используются.")
            return
        await _open_all_cities_reviews_message(message, state)
        return

    if _matches_command(message.text, "другойгород", "cityreset"):
        await state.clear()
        await message.answer(kb.CITY_INPUT_TEXT, reply_markup=kb.get_base_keyboard())
        await state.set_state(OrderState.waiting_for_city)
        return

    buy_index = _parse_index_command(message.text, "order", "buy", "купить")
    if buy_index:
        product_id = _resolve_product_id_by_index(data, buy_index)
        if not product_id:
            await message.answer("🔶 Нет товара с таким номером. Используйте номер из каталога.")
            return
        await _start_product_checkout(message.from_user.id, message, state, product_id)
        return

    review_index = _parse_index_command(message.text, "rew", "reviews", "отзывы")
    if review_index:
        product_id = _resolve_product_id_by_index(data, review_index)
        if not product_id:
            await message.answer("🔶 Нет товара с таким номером. Используйте номер из каталога.")
            return
        await _open_product_reviews_message(message, state, product_id)
        return

    if _matches_command(message.text, "allrew"):
        city_name = data.get("city")
        if not city_name:
            await message.answer("🔶 Сначала выберите город, затем откройте отзывы кнопкой в каталоге.")
            return
        await _send_all_city_reviews(message, state, city_name)
        return

    if _matches_command(message.text, "help"):
        await message.answer(_build_user_help_text(data.get("city")))
        return

    await message.answer("🔶 Используйте кнопки у товаров на экране.")


async def _return_to_catalog(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    city_name = data.get("city")
    if not city_name:
        await _prompt_city_input(message, state, text="🔶 Сессия города сброшена. Введите город заново.")
        return
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        data.get("population", 0),
        scope="regular",
    )
    await _replace_menu_message(
        message,
        state,
        catalog_text,
        reply_markup=_build_catalog_markup(
            city_name,
            data.get("population", 0),
            scope="regular",
            region_name=data.get("region"),
        ),
    )
    await state.set_state(OrderState.choosing_product)


async def _continue_after_district(message: Message, state: FSMContext, district: str) -> None:
    data = await state.get_data()
    allowed_districts = data.get("available_districts") or data.get("districts") or []
    await state.update_data(district=district, treasure_type=None)

    product = data.get("product") or {}
    product_id = product.get("id")
    product_scope = data.get("product_scope", "regular")
    district_stash_map = data.get("district_stash_map") or {}
    raw_types = district_stash_map.get(district)
    if not raw_types and product_id:
        if product_scope == "miniopt":
            raw_types = CatalogService.get_miniopt_district_stash_types(
                data.get("city", ""),
                product_id,
                district,
                allowed_districts,
            )
        else:
            raw_types = CatalogService.get_district_stash_types(
                data.get("city", ""),
                product_id,
                district,
                allowed_districts,
            )
    if not raw_types:
        raw_types = (
            CatalogService.get_available_miniopt_stash_types()
            if product_scope == "miniopt"
            else CatalogService.get_available_stash_types()
        )

    unique_types = []
    for item in raw_types:
        if isinstance(item, str) and item not in unique_types:
            unique_types.append(item)

    if len(unique_types) < 2 and product_id:
        if product_scope == "miniopt":
            fallback = CatalogService.get_miniopt_district_stash_types(
                data.get("city", ""),
                product_id,
                district,
                allowed_districts,
            )
        else:
            fallback = CatalogService.get_district_stash_types(
                data.get("city", ""),
                product_id,
                district,
                allowed_districts,
            )
        for item in fallback:
            if item not in unique_types:
                unique_types.append(item)
            if len(unique_types) == 2:
                break

    if len(unique_types) >= 2:
        shuffled = random.sample(unique_types, k=len(unique_types))
        display_options = shuffled[:2]
    else:
        display_options = unique_types[:]

    if len(display_options) < 2:
        fallback_pool = (
            CatalogService.get_available_miniopt_stash_types()
            if product_scope == "miniopt"
            else CatalogService.get_available_stash_types()
        )
        extras = [t for t in fallback_pool if t not in display_options]
        display_options.extend(extras[: max(0, 2 - len(display_options))])

    display_options = display_options[:2]
    await state.update_data(pending_treasure_types=display_options, available_treasure_types=display_options)
    await _show_treasure_types_text(message, state, product.get("name", "-"), display_options)


async def _continue_after_treasure_type(message: Message, state: FSMContext, choice: str) -> None:
    await state.update_data(treasure_type=choice, pending_treasure_types=None)
    data = await state.get_data()
    product = data.get("product", {})
    district = data.get("district")
    city = data.get("city")
    price_line = f"Итог кассы: <b>{kb.format_price_label(product.get('price', 0))} ₽</b>"
    original = product.get("original_price")
    applied = product.get("applied_discount_percent")
    try:
        original_num = float(original) if original is not None else None
        current_num = float(product.get("price") or 0)
    except (TypeError, ValueError):
        original_num = None
        current_num = 0
    if original_num is not None and applied and original_num > current_num:
        price_line = (
            f"Было {original_num:g} ₽, к кассе <b>{current_num:g} ₽</b>\n"
            f"Скидка серии {int(applied)}% уже внутри"
        )

    weight, title = kb.catalog_name_parts(str(product.get("name") or "позиция"))
    position = title if not weight else f"{weight}   {title}"
    summary = (
        f"🎮 <b>Корзина {html.escape(kb.SHOP_NAME)}</b>\n\n"
        f"🌍 Город доставки: {html.escape(str(city or 'не выбран'))}\n"
        f"📍 Район: {html.escape(str(district or 'не выбран'))}\n"
        f"🏷 Позиция: {html.escape(position)}\n"
        f"📦 Выдача: {html.escape(str(choice or 'не выбрана'))}\n"
        f"💳 {price_line}\n\n"
        "Следующий экран - выбор оплаты. Заявка появится после него."
    )
    await _offer_checkout_coupon(message, state, summary_text=summary)


@router.callback_query(F.data == "nav_back_products")
async def navigation_back_to_products_callback(callback: CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    if callback.message:
        await _return_to_catalog(callback.message, state)


@router.callback_query(F.data.startswith("dist_"), OrderState.choosing_district)
async def process_district_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    allowed_districts = data.get("available_districts") or data.get("districts") or []
    try:
        index = int((callback.data or "").split("_", 1)[1])
        district = allowed_districts[index]
    except (ValueError, IndexError, TypeError):
        await callback.answer("Выберите район кнопкой на экране.", show_alert=True)
        return
    await _safe_answer_callback(callback)
    if callback.message:
        await _continue_after_district(callback.message, state, district)


@router.callback_query(F.data == "nav_back_district_from_type", OrderState.choosing_treasure_type)
async def back_to_district_from_type_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    allowed = data.get("available_districts", data.get("districts", []))
    await state.update_data(treasure_type=None, pending_treasure_types=None)
    await _safe_answer_callback(callback)
    if callback.message:
        await _show_districts_text(callback.message, state, data.get("city", ""), allowed)


@router.callback_query(F.data.startswith("stash_"), OrderState.choosing_treasure_type)
async def process_treasure_type_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    options = data.get("pending_treasure_types") or data.get("available_treasure_types") or []
    try:
        index = int((callback.data or "").split("_", 1)[1])
        choice = options[index]
    except (ValueError, IndexError, TypeError):
        await callback.answer("Выберите формат кнопкой на экране.", show_alert=True)
        return
    await _safe_answer_callback(callback)
    if callback.message:
        await _continue_after_treasure_type(callback.message, state, choice)


@router.message(OrderState.choosing_district)
async def process_district(message: Message, state: FSMContext):
    district = (message.text or "").strip()
    data = await state.get_data()

    if not data.get("city"):
        await _prompt_city_input(message, state, text="🔶 Сессия выбора города сброшена. Введите город заново.")
        return

    allowed_districts = data.get("available_districts") or data.get("districts") or []

    if not allowed_districts:
        await _prompt_city_input(message, state, text="🔶 Не удалось загрузить список районов. Введите город заново.")
        return

    if _matches_command(district, "назад", "back"):
        await _return_to_catalog(message, state)
        return

    district_index = _parse_index_command(district, "district", "район")
    if district_index:
        if 1 <= district_index <= len(allowed_districts):
            district = allowed_districts[district_index - 1]
        else:
            await message.answer("🔶 Выберите район кнопкой на экране.")
            return
    elif district not in allowed_districts:
        await message.answer("🔶 Выберите район кнопкой на экране.")
        return

    await _continue_after_district(message, state, district)


@router.message(F.text == kb.BTN_BACK, OrderState.choosing_treasure_type)
async def back_to_district_from_type(message: Message, state: FSMContext):
    data = await state.get_data()
    allowed = data.get('available_districts', data.get('districts', []))
    await state.update_data(treasure_type=None, pending_treasure_types=None)
    await _show_districts_text(message, state, data.get('city', ''), allowed)


@router.message(OrderState.choosing_treasure_type)
async def process_treasure_type(message: Message, state: FSMContext):
    choice = (message.text or "").strip()
    data = await state.get_data()
    options = data.get('pending_treasure_types') or data.get('available_treasure_types') or []

    if _matches_command(choice, "назад", "back"):
        allowed = data.get('available_districts', data.get('districts', []))
        await state.update_data(treasure_type=None, pending_treasure_types=None)
        await _show_districts_text(message, state, data.get('city', ''), allowed)
        return

    option_index = _parse_index_command(choice, "format", "формат")
    if option_index:
        if 1 <= option_index <= len(options):
            choice = options[option_index - 1]
        else:
            await message.answer("🔶 Выберите формат кнопкой на экране.")
            return
    elif choice not in options:
        await message.answer("🔶 Выберите формат кнопкой на экране.")
        return

    await _continue_after_treasure_type(message, state, choice)


async def _handle_ratecore_method_pick(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    channel: str,
    method_key: str,
) -> None:
    if BlacklistService.is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return
    if UserService.get_active_order(callback.from_user.id):
        await callback.answer("У вас уже есть активная заявка.", show_alert=True)
        return
    data = await state.get_data()
    methods = data.get("ratecore_methods") or []
    assets = data.get("ratecore_assets") or []
    source = assets if channel == "crypto" else group_payment_methods(methods)
    match = next(
        (
            child
            for child in iter_payment_method_nodes(source)
            if str(child.get("key")) == method_key
        ),
        None,
    )
    if match is None:
        await callback.answer("Этот способ сейчас недоступен. Обновите список.", show_alert=True)
        return
    await callback.answer()
    try:
        await start_ratecore_payment(
            bot=bot,
            dispatcher=dispatcher,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
            state_data=data,
            method_key=method_key,
            method_name=str(match.get("name") or method_key),
            channel=channel,
            message=callback.message,
        )
    except RatecoreError as exc:
        logger.warning("Ratecore create failed for %s: %s", callback.from_user.id, exc)
        if is_no_requisites_failure(error=exc):
            failed_order = {
                "purpose": data.get("payment_purpose") or "order",
                "method": method_key,
                "method_name": str(match.get("name") or method_key),
                "city": data.get("city"),
                "district": data.get("district"),
                "treasure_type": data.get("treasure_type"),
                "product": (data.get("product") or {}).get("name"),
                "price": (data.get("product") or {}).get("price") or data.get("topup_amount") or data.get("chest_boost_rub"),
            }
            try:
                await notify_admins(
                    bot,
                    admin_requisites_failed_text(
                        callback.from_user.id,
                        callback.from_user.username,
                        failed_order,
                    ),
                    reply_markup=admin_created_keyboard(callback.from_user.id, failed_order),
                )
            except Exception:
                logger.exception(
                    "Failed to notify admins that requisites were unavailable for %s",
                    callback.from_user.id,
                )
            restored = await restore_payment_methods(
                bot,
                dispatcher,
                callback.from_user.id,
                failed_order,
                failed_without_requisites=True,
            )
            if restored:
                return
        purpose = data.get("payment_purpose") or "order"
        await callback.message.answer(
            f"🔶 Не удалось создать заявку.\n<code>{html.escape(str(exc))}</code>",
            reply_markup=kb.get_ratecore_methods_keyboard(
                methods,
                0,
                back_callback=data.get("ratecore_back_callback") or "nav_cancel",
                crypto_assets=assets,
                show_balance=purpose != "balance_topup",
                amount=checkout_base_amount(data),
            ),
        )


@router.callback_query(F.data.startswith("rcp_"))
async def process_ratecore_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int((callback.data or "rcp_0").split("_", 1)[1])
    except (ValueError, IndexError):
        page = 0
    data = await state.get_data()
    purpose = data.get("payment_purpose") or "order"
    await _show_ratecore_methods(
        callback,
        state,
        purpose=purpose,
        back_callback=data.get("ratecore_back_callback") or "nav_cancel",
        page=page,
    )


@router.callback_query(F.data.startswith("rcm_"))
async def process_ratecore_fiat(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    method_key = (callback.data or "")[4:]
    await _handle_ratecore_method_pick(
        callback, state, bot, dispatcher, channel="fiat", method_key=method_key
    )


@router.callback_query(F.data.startswith("rcg_"))
async def process_ratecore_group(callback: CallbackQuery, state: FSMContext):
    raw = (callback.data or "rcg_0")[4:]
    data = await state.get_data()
    methods = data.get("ratecore_methods") or []
    assets = data.get("ratecore_assets") or []
    amount = checkout_base_amount(data)
    if raw == "crypto":
        name = "Крипта"
        markup = kb.get_ratecore_submethods_keyboard(methods, "crypto", crypto_assets=assets, amount=amount)
    else:
        try:
            method_index = int(raw)
        except (ValueError, TypeError):
            method_index = 0
        grouped = group_payment_methods(methods)
        try:
            method = grouped[method_index]
        except (IndexError, TypeError):
            await callback.answer("Группа способов устарела. Обновите список.", show_alert=True)
            return
        name = str(method.get("name") or "Способ оплаты")
        markup = kb.get_ratecore_submethods_keyboard(grouped, method_index, crypto_assets=assets, amount=amount)
    try:
        await callback.message.edit_text(
            f"💳 <b>{html.escape(name)}</b>\n\nВыберите подходящий вариант оплаты:",
            reply_markup=markup,
        )
        await callback.answer()
    except TelegramBadRequest:
        await callback.answer("Не удалось открыть список способов", show_alert=True)


@router.callback_query(F.data.startswith("rcn_"))
async def process_ratecore_nested(callback: CallbackQuery, state: FSMContext):
    nest_key = (callback.data or "")[4:]
    data = await state.get_data()
    methods = data.get("ratecore_methods") or []
    assets = data.get("ratecore_assets") or []
    grouped = group_payment_methods(methods)
    parent_index, nest = find_nested_group(grouped, nest_key)
    if nest is None:
        await callback.answer("Группа способов устарела. Обновите список.", show_alert=True)
        return
    name = str(nest.get("name") or "Способ оплаты")
    markup = kb.get_ratecore_submethods_keyboard(
        methods,
        parent_index if parent_index is not None else 0,
        crypto_assets=assets,
        nest_key=nest_key,
        amount=checkout_base_amount(data),
    )
    try:
        await callback.message.edit_text(
            f"💳 <b>{html.escape(name)}</b>\n\nВыберите подходящий вариант оплаты:",
            reply_markup=markup,
        )
        await callback.answer()
    except TelegramBadRequest:
        await callback.answer("Не удалось открыть список способов", show_alert=True)


@router.callback_query(F.data == "rc_back_groups")
async def process_ratecore_group_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _show_ratecore_methods(
        callback,
        state,
        purpose=data.get("payment_purpose") or "order",
        back_callback=data.get("ratecore_back_callback") or "nav_cancel",
    )


@router.callback_query(F.data.startswith("rcc_"))
async def process_ratecore_crypto(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    method_key = (callback.data or "")[4:]
    await _handle_ratecore_method_pick(
        callback, state, bot, dispatcher, channel="crypto", method_key=method_key
    )


@router.message(OrderState.choosing_payment)
async def process_payment_text(message: Message, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    if _matches_command(message.text, "назад", "back"):
        data = await state.get_data()
        allowed = data.get('available_districts', data.get('districts', []))
        await _show_districts_text(message, state, data.get('city', ''), allowed)
        return
    await message.answer("🔶 Выберите способ оплаты кнопкой ниже.")

async def _legacy_pay_disabled(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Старый способ оплаты отключён. Выберите способ из списка API.", show_alert=True)
    data = await state.get_data()
    await _show_ratecore_methods(
        callback,
        state,
        purpose=data.get("payment_purpose") or "order",
        back_callback=data.get("ratecore_back_callback") or "nav_back_district",
    )


def _crypto_payment_purpose(data: dict) -> str:
    raw = str(data.get("payment_purpose") or "order")
    if raw == "balance_topup":
        return "topup"
    if raw == "chest_boost":
        return "chest_boost"
    return "order"


@router.callback_query(
    F.data == "store_crypto_menu",
    StateFilter(OrderState.choosing_payment, TopUpState.choosing_method),
)
async def store_crypto_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = checkout_base_amount(data)
    extra = f"\n💵 Сумма заказа: <b>{kb.format_price_label(amount)} ₽</b>\n" if amount > 0 else "\n"
    try:
        await callback.message.edit_text(
            f"💎 <b>Оплата Crypto</b>{extra}\nВыберите <b>BTC</b> или <b>LTC</b> — покажем сумму в рублях и в монете.",
            reply_markup=kb.get_store_crypto_coin_keyboard(back_callback="rc_back_groups"),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            f"💎 <b>Оплата Crypto</b>{extra}\nВыберите монету:",
            reply_markup=kb.get_store_crypto_coin_keyboard(back_callback="rc_back_groups"),
        )
    await callback.answer()


@router.callback_query(
    F.data.in_({"store_crypto_btc", "store_crypto_ltc"}),
    StateFilter(OrderState.choosing_payment, TopUpState.choosing_method),
)
async def store_crypto_coin(callback: CallbackQuery, state: FSMContext, bot: Bot):
    coin = "btc" if callback.data == "store_crypto_btc" else "ltc"
    data = await state.get_data()
    await _start_crypto_payment(
        callback,
        state,
        bot,
        coin,
        purpose=_crypto_payment_purpose(data),
    )


@router.callback_query(F.data == "pay_btc", OrderState.choosing_payment)
async def process_pay_btc(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await _start_crypto_payment(callback, state, bot, "btc", purpose=_crypto_payment_purpose(data))


@router.callback_query(F.data == "pay_ltc", OrderState.choosing_payment)
async def process_pay_ltc(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await _start_crypto_payment(callback, state, bot, "ltc", purpose=_crypto_payment_purpose(data))


@router.callback_query(F.data == "pay_crypto", OrderState.choosing_payment)
async def store_crypto_menu_legacy(callback: CallbackQuery, state: FSMContext):
    await store_crypto_menu(callback, state)


@router.callback_query(F.data == "pay_balance", OrderState.choosing_payment)
async def process_balance_payment(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    
    if BlacklistService.is_banned(user_id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    data = await state.get_data()
    product = data.get('product')
    price = product['price'] if product else 0
    
    # Пытаемся списать
    if UserService.deduct_balance(user_id, price):
        district = data.get('district')
        treasure = data.get('treasure_type', 'Тайник')
        bonus_payload = await _complete_balance_purchase(
            bot,
            user_id,
            username=callback.from_user.username,
            data=data,
            product=product,
            price=price,
        )
        
        if CatalogService.is_courier_job(product):
            await callback.message.edit_text(_courier_job_paid_text(product.get("name"), price))
        else:
            await callback.message.edit_text(
                build_balance_order_paid_text(
                    product_name=product.get("name"),
                    district=district,
                    price=price,
                    treasure=treasure,
                )
            )
        await notify_purchase_loyalty(bot, user_id, bonus_payload)
        await state.clear()
    else:
        bal = UserService.get_balance(user_id)
        await callback.answer(f"❗ В кошельке не хватает.\nСейчас: {bal} руб.\nК кассе: {price} руб.", show_alert=True)


@router.callback_query(F.data == "pay_card", OrderState.choosing_payment)
async def process_card(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await _legacy_pay_disabled(callback, state)


@router.callback_query(F.data == "pay_yoomoney", OrderState.choosing_payment)
async def process_yoomoney(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    await _legacy_pay_disabled(callback, state)


@router.callback_query(F.data == "pay_card_rf", OrderState.choosing_payment)
async def process_card_rf(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    await _legacy_pay_disabled(callback, state)
    return
    user_id = callback.from_user.id

    if BlacklistService.is_banned(user_id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    blocked_rf, until_rf = UserService.is_rf_card_blocked(user_id)
    if blocked_rf:
        await callback.answer(
            f"🛑️ Оплата картой РФ временно недоступна до {until_rf}. Выберите оплату с баланса.",
            show_alert=True,
        )
        return

    if SettingsService.is_redirect_active():
        await callback.answer("🔶 Оплата картой временно недоступна. Попробуйте позже или оплатите с баланса.", show_alert=True)
        return

    blocked, until = UserService.is_manual_payment_blocked(user_id)
    if blocked:
        await callback.answer(
            f"🛑️ Оплата картой временно недоступна до {until}. Попробуйте позже или оплатите с баланса.",
            show_alert=True,
        )
        return

    cards = SettingsService.get_rf_cards()
    last_card = UserService.get_last_rf_card(user_id)
    if not cards:
        card_number = None
    elif len(cards) == 1:
        card_number = cards[0]
    else:
        candidates = [item for item in cards if item != last_card]
        if not candidates:
            candidates = list(cards)
        card_number = random.choice(candidates)
    if not card_number:
        await callback.answer("🔶 Карты для оплаты не настроены. Выберите другой способ.", show_alert=True)
        await callback.message.edit_text(
            "🔶 Способ оплаты картой РФ временно недоступен. Выберите оплату с баланса:",
            reply_markup=kb.get_payment_keyboard(),
        )
        return

    UserService.set_last_rf_card(user_id, card_number)

    data = await state.get_data()
    product = data.get('product')
    rf_commission = SettingsService.get_rf_card_commission()
    base_price = product['price'] if product else 0
    total_price = base_price + rf_commission

    payment_request_id = uuid.uuid4().hex
    expires_at = (get_now_msk() + timedelta(minutes=12)).strftime("%H:%M")

    order_data = {
        "type": "card_rf",
        "product": product['name'] if product else "Unknown",
        "price": total_price,
        "treasure_type": data.get('treasure_type', 'Тайник'),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": callback.message.message_id,
        "payment_request_id": payment_request_id,
        "card_number": card_number,
    }
    UserService.set_active_order(user_id, order_data)

    await state.update_data(
        payment_request_id=payment_request_id,
        payment_card=card_number,
        payment_amount=total_price,
    )

    admin_text = _build_card_rf_payment_text(
        total_price,
        card_number,
        payment_request_id,
        expires_at,
        rf_commission,
        product_name=product['name'] if product else None,
        user_id=user_id,
        username=callback.from_user.username,
        city=data.get('city'),
        district=data.get('district'),
        treasure_type=data.get('treasure_type'),
    )
    pay_text = _build_card_rf_payment_text(
        total_price,
        card_number,
        payment_request_id,
        expires_at,
        rf_commission,
        product_name=product['name'] if product else None,
        user_id=user_id,
        username=callback.from_user.username,
        city=data.get('city'),
        district=data.get('district'),
        treasure_type=data.get('treasure_type'),
        include_instructions=True,
    )
    await callback.message.edit_text(
        pay_text,
        reply_markup=kb.get_pending_order_inline_keyboard()
    )
    await callback.message.answer(
        "📩 После оплаты пришлите скриншот или PDF чека.",
        reply_markup=kb.get_pending_order_keyboard(),
    )

    await notify_admin(bot, admin_text)
    await bot.send_message(
        ADMIN_ID,
        "🔽 Нажмите для чата с пользователем",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
            [InlineKeyboardButton(text="❗ Отменить заказ", callback_data=f"admin_cancel_order_{user_id}")],
            [InlineKeyboardButton(text="✅ Подтвердить (+покупка)", callback_data=f"admin_confirm_order_{user_id}")]
        ])
    )

    await state.set_state(OrderState.waiting_for_receipt)
    await callback.answer()

    asyncio.create_task(_schedule_rf_payment_reminders(bot, user_id, payment_request_id, dispatcher))


@router.callback_query(F.data == "pay_sbp", OrderState.choosing_payment)
async def process_sbp_unavailable(callback: CallbackQuery, state: FSMContext):
    await _legacy_pay_disabled(callback, state)


@router.callback_query(F.data == "pay_foreign", OrderState.choosing_payment)
async def process_foreign_unavailable(callback: CallbackQuery, state: FSMContext):
    await _legacy_pay_disabled(callback, state)


def _payment_not_found_text() -> str:
    support = SettingsService.get_support_contact()
    return (
        "🔶 <b>Перевод пока не виден кассе.</b>\n"
        "Повторите проверку через 3-5 минут.\n\n"
        f"Если сумма уже ушла, напишите: {support}"
    )


def _fake_check_processing_text(order: dict | None = None) -> str:
    if CryptoService.is_crypto_order(order):
        return (
            "⏳ <b>Касса Store сверяет перевод.</b>\n"
            "Обычно это до 5 минут. Если сумма верная, адрес придет сам."
        )
    return "⏳ <b>Касса Store сверяет перевод.</b> Подождите немного."


async def _send_fake_payment_check(message: Message, *, processing_text: str) -> None:
    checking_msg = await message.answer(processing_text)
    await asyncio.sleep(20)
    text = _payment_not_found_text()
    try:
        await checking_msg.edit_text(text, reply_markup=kb.get_rf_check_retry_keyboard())
    except Exception:
        await message.answer(text, reply_markup=kb.get_rf_check_retry_keyboard())


@router.message(OrderState.waiting_for_receipt)
async def process_order_receipt(message: Message, state: FSMContext, bot: Bot):
    from app.services.ratecore import RatecoreClient, RatecoreError
    from io import BytesIO
    
    user_id = message.from_user.id
    active_order = UserService.get_active_order(user_id)
    data = await state.get_data()
    request_id = data.get("payment_request_id")
    amount = data.get("payment_amount")
    card = data.get("payment_card")

    # For Ratecore orders, check payment_id instead of request_id
    if is_ratecore_order(active_order):
        payment_id = str(active_order.get("payment_id") or "")
        fsm_payment_id = str(data.get("payment_id") or "")
        if not active_order or (fsm_payment_id and payment_id != fsm_payment_id):
            await message.answer(
                "🔶 Счет не найден или уже закрыт. Оформите новый заказ в каталоге.",
                reply_markup=kb.get_base_keyboard(),
            )
            await state.clear()
            return
    elif not active_order or active_order.get("payment_request_id") != request_id:
        await message.answer(
            "🔶 Счет не найден или уже закрыт. Оформите новый заказ в каталоге.",
            reply_markup=kb.get_base_keyboard(),
        )
        await state.clear()
        return

    if CryptoService.is_crypto_order(active_order):
        tx_hash = CryptoService.extract_tx_hash(message.text or message.caption or "")
        if not tx_hash:
            await message.answer(
                "🔗 Пришлите <b>хеш транзакции</b> одним сообщением (txid).\n"
                "Снимка экрана мало: кассе нужен сам хеш.",
                reply_markup=kb.get_pending_order_keyboard(),
            )
            return

        active_order["tx_hash"] = tx_hash
        UserService.set_active_order(user_id, active_order)
        await state.update_data(payment_tx_hash=tx_hash)

        expires_raw = active_order.get("timestamp")
        expires_at = None
        if expires_raw:
            try:
                started = datetime.strptime(str(expires_raw), "%Y-%m-%d %H:%M:%S")
                expires_at = (started + timedelta(hours=1)).strftime("%d.%m %H:%M")
            except ValueError:
                expires_at = None

        await _send_crypto_application_to_admins(
            bot,
            user_id=user_id,
            username=message.from_user.username,
            order=active_order,
            status="хеш получен - подтвердите или отмените",
            expires_at=expires_at,
        )
        await _send_fake_payment_check(
            message,
            processing_text=(
                "⏳ <b>Усиление бонуса ушло куратору Store.</b>\n"
                "После подтверждения напишем, что ежедневный бонус можно открыть."
                if CryptoService.is_chest_boost(active_order)
                else (
                    "⏳ <b>Касса Store сверяет перевод.</b>\n"
                    "Обычно это до 5 минут. Если сумма верная, адрес придет сам."
                )
            ),
        )
        return

    # Handle Ratecore orders with receipt required
    if is_ratecore_order(active_order) and active_order.get("receipt_required"):
        method_key = str(active_order.get("method") or "").lower()
        if not (message.photo or message.document):
            await message.answer(
                "📩 Пришлите <b>фото или PDF чека</b> для подтверждения оплаты.",
                reply_markup=kb.get_pending_order_keyboard(),
            )
            return

        payment_id = str(active_order.get("payment_id") or "")
        if not payment_id:
            await message.answer(
                "🔶 Ошибка: ID платежа не найден. Обратитесь в поддержку.",
                reply_markup=kb.get_base_keyboard(),
            )
            return

        # PDF-methods: user MUST upload the receipt on the Ratecore page, not to the bot.
        # We forward the file to admins as a backup and redirect the user to the payment page.
        if "pdf" in method_key:
            await _forward_with_admin_controls(
                bot,
                message,
                (
                    f"🧾 Чек Ratecore (PDF-метод, прислан в бот) от ({user_id})\n"
                    f"📝 Payment ID: <code>{payment_id}</code>\n"
                    f"📝 Способ: {active_order.get('method_name') or active_order.get('method')}"
                ),
            )
            await message.answer(
                pdf_redirect_text(payment_id),
                reply_markup=pdf_redirect_markup(active_order.get("payment_url"))
                or kb.get_base_keyboard(),
            )
            return

        # Show loading message
        loading_msg = await message.answer("⏳ <b>Загружаю чек...</b> Пожалуйста, подождите.")
        
        # Download file from Telegram
        try:
            if message.photo:
                file_obj = message.photo[-1]  # Largest size
                filename = "receipt.jpg"
                content_type = "image/jpeg"
            else:
                file_obj = message.document
                filename = file_obj.file_name or "receipt.pdf"
                if filename.lower().endswith(".pdf"):
                    content_type = "application/pdf"
                elif filename.lower().endswith((".jpg", ".jpeg")):
                    content_type = "image/jpeg"
                elif filename.lower().endswith(".png"):
                    content_type = "image/png"
                else:
                    content_type = "application/octet-stream"
            
            file = await bot.get_file(file_obj.file_id)
            buffer = BytesIO()
            await bot.download_file(file.file_path, buffer)
            file_bytes = buffer.getvalue()
            
            # Upload to Ratecore
            client = RatecoreClient()
            result = await client.upload_receipt(
                payment_id=payment_id,
                file_bytes=file_bytes,
                filename=filename,
                content_type=content_type,
            )
            
            # Mark receipt as uploaded
            active_order["receipt_uploaded"] = True
            UserService.set_active_order(user_id, active_order)
            
            # Delete loading message
            try:
                await loading_msg.delete()
            except Exception:
                pass
            
            await message.answer(
                "✅ <b>Чек загружен!</b>\n\n"
                "Ожидайте подтверждения оплаты. Обычно это занимает до 5 минут.",
                reply_markup=kb.get_base_keyboard(),
            )
            
            # Notify admins
            await notify_admin(
                bot,
                f"📤 <b>Чек загружен в Ratecore</b>\n"
                f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
                f"📝 Payment ID: <code>{payment_id}</code>\n"
                f"📁 Файл: {filename}",
            )
            
            logger.info("Receipt uploaded to Ratecore for user %s, payment %s", user_id, payment_id)
            return
            
        except RatecoreError as exc:
            logger.warning("Ratecore receipt upload failed for %s: %s", user_id, exc)
            try:
                await loading_msg.delete()
            except Exception:
                pass
            
            # If upload fails, fall back to admin review
            await message.answer(
                "🔶 Не удалось автоматически загрузить чек. Передаём оператору для проверки.",
                reply_markup=kb.get_base_keyboard(),
            )
            # Fall through to admin notification below
            
        except Exception as exc:
            logger.exception("Unexpected error uploading receipt for %s", user_id)
            try:
                await loading_msg.delete()
            except Exception:
                pass
            await message.answer(
                "🔶 Произошла ошибка. Передаём чек оператору для проверки.",
                reply_markup=kb.get_base_keyboard(),
            )
            # Fall through to admin notification below

    # Legacy flow: forward to admins
    if message.photo or message.document:
        wallet = active_order.get("yoomoney_wallet") or card
        if _is_yoomoney_order(active_order):
            admin_note = (
                "🧾 <b>Чек по оплате ЮMoney</b>\n"
                f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
                f"💵 Сумма: {amount} руб.\n"
                f"📝 Номер заявки: <code>{request_id}</code>\n"
                f"📝 Кошелёк: <code>{html.escape(str(wallet or ''))}</code>"
            )
            forward_header = f"🧾 Чек ЮMoney от клиента ({user_id})"
        elif is_ratecore_order(active_order):
            payment_id = active_order.get("payment_id") or ""
            admin_note = (
                "🧾 <b>Чек по оплате Ratecore (PDF)</b>\n"
                f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
                f"💵 Сумма: {active_order.get('payer_amount') or active_order.get('price')} руб.\n"
                f"📝 Payment ID: <code>{payment_id}</code>\n"
                f"📝 Способ: {active_order.get('method_name') or active_order.get('method')}"
            )
            forward_header = f"🧾 Чек Ratecore от клиента ({user_id})"
        else:
            admin_note = (
                "🧾 <b>Чек по оплате картой РФ</b>\n"
                f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
                f"💵 Сумма: {amount} руб.\n"
                f"📝 Номер заявки: <code>{request_id}</code>\n"
                f"📝 Карта: <code>{card}</code>"
            )
            forward_header = f"🧾 Чек от клиента ({user_id})"
        await notify_admin(bot, admin_note)
        await _forward_with_admin_controls(bot, message, forward_header)

        await _send_fake_payment_check(
            message,
            processing_text="⏳ <b>Проверка чека...</b> Пожалуйста, подождите.",
        )
        return

    if ADMIN_IDS:
        label = "ЮMoney" if _is_yoomoney_order(active_order) else ("Ratecore" if is_ratecore_order(active_order) else "Карта РФ")
        await _forward_with_admin_controls(bot, message, f"📩 Сообщение от клиента ({user_id}) [{label}]")
    await message.answer("✅ Сообщение отправлено оператору.")


@router.callback_query(F.data == "rf_check_retry", OrderState.waiting_for_receipt)
async def rf_check_retry(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    data = await state.get_data()
    request_id = data.get("payment_request_id")
    amount = data.get("payment_amount")
    card = data.get("payment_card")

    active_order = UserService.get_active_order(user_id)
    if not active_order or active_order.get("payment_request_id") != request_id:
        try:
            await callback.answer("Заявка не найдена или истекла", show_alert=True)
        except TelegramBadRequest:
            pass
        await callback.message.edit_text(
            "🔶 Счет не найден или уже закрыт. Оформите новый заказ в каталоге.",
            reply_markup=kb.get_base_keyboard(),
        )
        await state.clear()
        return

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    checking_msg = await callback.message.edit_text(_fake_check_processing_text(active_order))
    await asyncio.sleep(20)

    try:
        await checking_msg.edit_text(_payment_not_found_text(), reply_markup=kb.get_rf_check_retry_keyboard())
    except Exception:
        await callback.message.answer(_payment_not_found_text(), reply_markup=kb.get_rf_check_retry_keyboard())

@router.message(OrderState.chat_with_support)
async def process_support_chat(message: Message, state: FSMContext, bot: Bot):
    """Пересылает любые сообщения пользователя админу"""
    if BlacklistService.is_banned(message.from_user.id):
        await message.answer("🔒 Вы заблокированы.")
        return

    if (message.text or "").strip() in {kb.BTN_CLOSE_CHAT, "закрыть канал"}:
        await _close_user_support_chat(message, state, bot)
        return

    if ADMIN_ID:
        header = f"📩 Сообщение от клиента ({message.from_user.id})"
        await _forward_with_admin_controls(bot, message, header)
    
    await message.answer(
        "✅ Сообщение ушло в поддержку Store. Ждите ответ.\n"
        "Закончить диалог можно кнопкой <b>↩️ На главную</b>.",
        reply_markup=kb.get_support_chat_keyboard(),
    )

# --- Авто-обмен (новый метод) ---

@router.callback_query(F.data == "pay_auto_exchange", OrderState.choosing_payment)
async def process_auto_exchange(callback: CallbackQuery, state: FSMContext):
    await _legacy_pay_disabled(callback, state)

@router.callback_query(F.data == "exchange_enter_id")
async def ask_exchange_order_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderState.waiting_for_exchange_order_id)
    await callback.message.answer(
        "📝 Введите <b>ID заявки</b> из обменника:\n"
        "(Только число, например: <code>12345</code>)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")]])
    )
    await callback.answer()

@router.message(OrderState.waiting_for_exchange_order_id)
async def process_exchange_order_id(message: Message, state: FSMContext, bot: Bot):
    order_id = message.text
    user = message.from_user
    
    data = await state.get_data()
    product = data.get('product')
    district = data.get('district')
    # Для авто-обмена комиссию НЕ добавляем
    total_price = product['price'] if product else 0
    
    # 1. Уведомляем админа
    await notify_admin(bot, 
        f"🔄 <b>Заявка на авто-обмен</b>\n"
        f"👤 Пользователь: {user.id} (@{user.username})\n"
        f"📦 Товар: {product['name'] if product else 'Неизвестно'} ({district})\n"
        f"💰 К оплате: {total_price} руб.\n"
        f"📝 Номер заявки: <code>{order_id}</code>\n"
        f"(Проверьте поступление средств/статус заявки в обменнике)"
    )
    
    # 2. Имитация проверки (думает...)
    checking_msg = await message.answer("⏳ <b>Проверка статуса заявки...</b>")
    await asyncio.sleep(3) # Имитация задержки поиска
    
    # 3. Ответ "Не найдено"
    await checking_msg.delete()
    await message.answer(
        f"🔶 <b>Счет #{order_id} касса не видит.</b>\n\n"
        f"Проверьте номер. Если перевод только что ушел, зачисление может занять до 10 минут.\n"
        f"Повторите проверку чуть позже.",
        reply_markup=kb.get_exchange_retry_keyboard()
    )
    # Стейт не сбрасываем? Или сбрасываем? 
    # Кнопка "Проверить оплату (Ввести номер)" снова триггернет 'exchange_enter_id' -> ввод номера.
    # Поэтому можно сбросить или оставить. Лучше сбросить, чтобы "Ввести номер" сработала чисто.
    await state.set_state(OrderState.choosing_payment) 


# --- Fallback (Catch-all) для непонятных сообщений ---
@router.message()
async def unknown_message(message: Message):
    """
    Обработчик-ловушка для всех сообщений, которые не были обработаны другими хендлерами.
    Срабатывает, если пользователь прислал текст, когда ожидалась кнопка или другое действие.
    """
    # Игнорируем команды (они должны обрабатываться отдельно или это реально неизвестная команда)
    if message.text and message.text.startswith("/"):
        return

    await message.answer(
        "🔵 <b>Store ждет кнопку или текст по шагу.</b>\n\n"
        "Сейчас нужно нажать кнопку на экране или прислать запрошенные данные.\n"
        "Если застряли, нажмите «↩️ На главную».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=kb.BTN_DISMISS, callback_data="nav_cancel")]
        ])
    )

