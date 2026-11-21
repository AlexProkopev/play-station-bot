import asyncio
import logging
import math
import re
import secrets
import json
import html
from io import BytesIO
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import ADMIN_ID, is_admin, is_ratecore_sandbox
from app.services.blacklist import BlacklistService
from app.services.users import UserService
from app.services.catalog import (
    CatalogService,
    COMPACT_CITY_MAX_POPULATION,
    MEDIUM_CITY_MAX_POPULATION,
    SMALL_CITY_MAX_POPULATION,
)
from app.services.reviews import ReviewsService
from app.services.geo import GeoService, MIN_CITY_POPULATION
from app.services.pricing import PricingService
from app.services.analytics import ProductInterestService
from app.services.pdf_export import PdfExportService
from app.services.roulette import RouletteService
from app.services.chest import ChestService
from app.services.referral_race import ReferralRaceService, PRIZES as RACE_PRIZES
from app.services.coupons import CouponService
from app.services.referrals import complete_product_purchase, notify_bonus_cap, notify_bonus_cap_many, notify_referrer_bonus
from app.services.loyalty import LoyaltyService, notify_purchase_loyalty
from app.services.bot_stats import BotStatsService, build_stats_text
from app.utils import get_now_msk, clear_user_order_fsm, get_user_fsm, send_user_to_start
from app.states import OrderState, TopUpState
from app.services.crypto import CryptoService
from app.services.payments import (
    build_order_fulfillment_wait_text,
    is_ratecore_order,
    sandbox_complete_for_user,
)
from app.services.ratecore import RatecoreError
from app.handlers.user_flow import build_topup_card_payment_text
import app.keyboards as kb

router = Router()
commands_router = Router()

logger = logging.getLogger("app.handlers.admin")


async def _safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> bool:
    try:
        await callback.answer(*args, **kwargs)
        return True
    except TelegramBadRequest as err:
        error_text = str(err).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            return False
        raise


@router.callback_query(F.data.startswith("admin_delmsg_"))
async def admin_delete_forwarded(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    payload = callback.data[len("admin_delmsg_"):]
    parts = payload.split("_")
    if len(parts) == 4:
        user_id_str, original_id_str, admin_chat_id_str, forwarded_id_str = parts
    elif len(parts) == 3:
        user_id_str, original_id_str, forwarded_id_str = parts
        admin_chat_id_str = str(callback.message.chat.id) if callback.message else str(callback.from_user.id)
    else:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    try:
        user_id = int(user_id_str)
        original_id = int(original_id_str)
        admin_chat_id = int(admin_chat_id_str)
        forwarded_id = int(forwarded_id_str)
    except ValueError:
        await callback.answer("Ошибка идентификаторов", show_alert=True)
        return

    user_deleted = False
    admin_deleted = False

    try:
        await bot.delete_message(user_id, original_id)
        user_deleted = True
    except TelegramBadRequest as err:
        logger.warning("Не удалось удалить сообщение у пользователя %s: %s", user_id, err)
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка при удалении сообщения у пользователя %s", user_id)

    if forwarded_id:
        try:
            await bot.delete_message(admin_chat_id, forwarded_id)
            admin_deleted = True
        except TelegramBadRequest as err:
            logger.warning("Не удалось удалить форвард у админа %s: %s", forwarded_id, err)
        except Exception as err:  # noqa: BLE001
            logger.exception("Ошибка при удалении форварда у админа %s", forwarded_id)

    status_lines: list[str] = ["🗑 Результат удаления"]
    status_lines.append("✅ Удалили у клиента" if user_deleted else "🔶 Не удалось удалить у клиента")
    if forwarded_id:
        status_lines.append("✅ Удалили форвард" if admin_deleted else "🔶 Форвард остался у админа")

    if callback.message:
        try:
            await callback.message.edit_text("\n".join(status_lines))
        except TelegramBadRequest:
            pass

    await callback.answer("Готово")

class AdminState(StatesGroup):
    chatting_with_user = State()
    waiting_for_dispute_decision = State()
    waiting_for_dispute_tpl_percent = State()
    waiting_for_dispute_tpl_text = State()
    waiting_for_dispute_tpl_edit = State()
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()
    
    waiting_for_broadcast = State()
    waiting_for_auto_broadcast_text = State()
    waiting_for_auto_broadcast_interval = State()
    waiting_for_dm_id = State()
    waiting_for_dm_text = State()
    waiting_for_catalog_city = State() # Ожидание города для редактирования
    
    waiting_for_districts_city = State() # Ожидание города для районов
    waiting_for_district_name = State() # Ожидание имени района
    waiting_for_manual_city_name = State()
    waiting_for_manual_city_region = State()
    waiting_for_manual_city_population = State()
    waiting_for_manual_city_districts = State()
    
    waiting_for_new_prod_name = State() # Товар Custom
    waiting_for_new_prod_price = State()
    
    # Review States
    # Since we have ReviewState for users, let's use AdminState specific ones to avoid conflicts or shared handlers
    waiting_for_rev_nick = State()
    waiting_for_rev_mode = State()
    waiting_for_rev_city = State()
    waiting_for_rev_prod = State() # ID
    waiting_for_rev_dist = State() 
    waiting_for_rev_text = State()
    waiting_for_search_city = State() # NEW for user search
    
    # Product District Edit
    # Usually we don't need a state if we just use callback toggles, but good to have context
    editing_product_districts = State() 
    editing_product_stash = State()
    
    waiting_for_review_phrase = State() # Добавление фразы
    waiting_for_review_del_idx = State() # Удаление фразы по номеру

    waiting_for_rev_mod_edit_text = State()
    waiting_for_rev_gen_import_json = State()
    
    # Настройки авто-отзывов
    waiting_for_rev_set_interval = State()
    waiting_for_rev_set_min = State()
    waiting_for_rev_set_max = State()

    waiting_for_stash_type_add = State()

    waiting_for_pricing_region_name = State()
    waiting_for_pricing_value = State()

    waiting_for_btc_wallet = State()
    waiting_for_ltc_wallet = State()

    waiting_for_manual_payment_template = State()

    waiting_for_manual_commission = State()

    waiting_for_rf_commission = State()
    waiting_for_yoomoney_commission = State()

    waiting_for_support_contact = State()
    waiting_for_contacts_text = State()
    waiting_for_home_screen_text = State()
    waiting_for_home_link_title = State()
    waiting_for_home_link_url = State()
    waiting_for_home_link_rename = State()
    waiting_for_home_link_reurl = State()
    waiting_for_featured_title = State()
    waiting_for_featured_description = State()
    waiting_for_featured_photo = State()
    waiting_for_preorder_text = State()
    waiting_for_mission_reward = State()
    waiting_for_mission_task_title = State()
    waiting_for_mission_task_count = State()
    waiting_for_mission_task_reward = State()
    waiting_for_streak_required = State()
    waiting_for_streak_percent = State()

    waiting_for_exchange_bot_url = State()

    waiting_for_rf_cards = State()
    waiting_for_yoomoney_wallets = State()

    waiting_for_rf_unblock_id = State()

    waiting_for_roulette_title = State()
    waiting_for_roulette_max = State()
    waiting_for_roulette_conditions = State()
    waiting_for_roulette_channel = State()
    waiting_for_roulette_winners_count = State()
    waiting_for_roulette_place_prizes = State()
    waiting_for_roulette_prize = State()
    waiting_for_roulette_winner_ids = State()
    waiting_for_roulette_place_winner = State()
    waiting_for_mix_fixed_winner = State()
    waiting_for_mix_random_count = State()
    waiting_for_mix_prizes = State()
    waiting_for_coupon_code = State()
    waiting_for_coupon_amount = State()
    
    # Payment method aliases
    waiting_for_payment_alias_original = State()
    waiting_for_payment_alias_custom = State()
    waiting_for_payment_fallback_method = State()
    waiting_for_payment_fallback_alts = State()
    waiting_for_user_lookup = State()
    waiting_for_user_discount_percent = State()
    waiting_for_user_discount_uses = State()

def resolve_user_input(text: str):
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("/user"):
        parts = raw.split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        if not raw:
            return None

    if raw.lower().startswith("https://t.me/"):
        raw = raw.rstrip("/").rsplit("/", 1)[-1]
        if raw.lower().startswith("user?"):
            raw = raw.split("id=", 1)[-1]

    collapsed = raw.replace(" ", "")
    explicit_id = re.search(
        r"(?:tg://user\?id=|(?<![A-Za-z0-9_])id[=:])(\d{5,})",
        collapsed,
        flags=re.IGNORECASE,
    )
    pure_id = re.fullmatch(r"@?(\d{5,})", collapsed)
    nick = raw.lstrip("@").strip()

    if pure_id:
        return int(pure_id.group(1))

    found = UserService.get_user_id_by_username(nick)
    if found:
        return int(found)

    if explicit_id:
        return int(explicit_id.group(1))
    return None


def resolve_user_from_message(message: Message):
    for entity in message.entities or []:
        if getattr(entity, "type", None) == "text_mention" and getattr(entity, "user", None):
            return int(entity.user.id)
        if getattr(entity, "type", None) == "mention" and message.text:
            username = message.text[entity.offset : entity.offset + entity.length]
            found = UserService.get_user_id_by_username(username)
            if found:
                return int(found)
    return resolve_user_input(message.text or "")

# --- Управление Балансом ---

@commands_router.message(Command("set_balance"))
async def cmd_set_balance(message: Message):
    if not is_admin(message.from_user.id):
            return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer("🔶 Используйте: /set_balance <id/username> <сумма>")
        return

    target = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        await message.answer("🔶 Сумма должна быть числом.")
        return

    user_id = resolve_user_input(target)
    if not user_id:
        await message.answer(f"🔶 Пользователь '{target}' не найден.")
        return

    if UserService.set_balance(user_id, amount):
        await message.answer(
            f"✅ Баланс пользователя {user_id} установлен: {UserService.get_balance(user_id)} руб."
        )
    else:
        await message.answer("❗ Ошибка при сохранении.")

@commands_router.message(Command("add_balance"))
async def cmd_add_balance(message: Message):
    if not is_admin(message.from_user.id):
            return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer("🔶 Используйте: /add_balance <id/username> <сумма>")
        return

    target = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        await message.answer("🔶 Сумма должна быть числом.")
        return

    user_id = resolve_user_input(target)
    if not user_id:
        await message.answer(f"🔶 Пользователь '{target}' не найден.")
        return
    
    before = UserService.get_balance(user_id)
    if not UserService.add_balance(user_id, amount, source="admin"):
        await message.answer("❗ Не удалось начислить баланс. Пользователь не сохранился.")
        return
    new_bal = UserService.get_balance(user_id)
    await message.answer(
        f"✅ Баланс пользователя {user_id} пополнен на {amount}. "
        f"Было: {before} руб. Сейчас: {new_bal} руб."
    )


@router.callback_query(F.data == 'admin_coupon_create_start')
async def admin_coupon_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    await state.set_state(AdminState.waiting_for_coupon_code)
    await callback.message.edit_text(
        "🎟 <b>Создание купона</b>\n\n"
        "Введите код купона (например SALE500):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_back_main")]])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_coupon_code)
async def admin_coupon_receive_code(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code:
        await message.answer("🔶 Код купона не может быть пустым.")
        return

    await state.update_data(coupon_code=code)
    await state.set_state(AdminState.waiting_for_coupon_amount)
    await message.answer(
        "Введите сумму купона в рублях (например 500):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_back_main")]])
    )


@router.message(AdminState.waiting_for_coupon_amount)
async def admin_coupon_receive_amount(message: Message, state: FSMContext):
    raw_amount = (message.text or "").replace(",", ".").strip()
    try:
        amount = float(raw_amount)
    except ValueError:
        await message.answer("🔶 Введите корректную сумму числом.")
        return

    data = await state.get_data()
    code = data.get("coupon_code", "")
    ok, result = CouponService.create_coupon(code, amount, message.from_user.id)

    await state.clear()
    if not ok:
        await message.answer(f"❗ {result}", reply_markup=kb.get_admin_keyboard())
        return

    await message.answer(
        "✅ <b>Купон создан</b>\n"
        f"Код: <code>{result}</code>\n"
        f"Сумма: <b>{amount:.2f} руб.</b>",
        reply_markup=kb.get_admin_keyboard()
    )


def _admin_user_profile_text(user_id: int) -> str | None:
    user = UserService.get_user(user_id)
    if not user:
        return None
    username = user.get("username") or "нет"
    disc = UserService.get_personal_discount(user_id)
    if disc.get("active"):
        disc_line = f"📉 Скидка: <b>{disc['percent']}%</b> ещё на <b>{disc['left']}</b> пок."
    else:
        disc_line = "📉 Скидка: нет"
    order = UserService.get_active_order(user_id)
    banned, until = UserService.is_temp_banned(user_id)
    return (
        f"👤 <b>Профиль пользователя</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👤 Username: @{html.escape(str(username))}\n"
        f"🛒 Покупок: {user.get('purchases_count') or 0}\n"
        f"{disc_line}\n"
        f"👥 Рефералов: {user.get('referral_count') or 0}\n"
        f"💰 Баланс: <code>{user.get('balance') or 0}</code> руб.\n"
        f"📅 В боте с: {user.get('joined') or '-'}\n"
        f"📦 Активный заказ: {'есть' if order else 'нет'}\n"
        f"{'🔒 Временный бан до ' + str(until) if banned else '✅ Бан: нет'}"
    )


async def _show_admin_user_profile(target: Message | CallbackQuery, user_id: int) -> bool:
    text = _admin_user_profile_text(user_id)
    if not text:
        return False
    has_discount = UserService.get_personal_discount(user_id).get("active")
    markup = kb.get_admin_user_keyboard(user_id, has_discount)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)
    return True


@router.callback_query(F.data == "admin_user_lookup")
async def admin_user_lookup_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_user_lookup)
    await callback.message.edit_text(
        "👤 <b>Найти пользователя</b>\n\n"
        "Введите @username или ID пользователя.\n"
        "Примеры: <code>@nick</code>, <code>nick</code>, <code>123456789</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_back_main")]]
        ),
    )
    await callback.answer()


@router.message(Command("user"))
@router.message(AdminState.waiting_for_user_lookup)
async def admin_user_lookup_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    command_only = bool(re.fullmatch(r"/user(?:@\w+)?", raw, flags=re.IGNORECASE))
    if command_only:
        await state.set_state(AdminState.waiting_for_user_lookup)
        await message.answer("Введите @username или ID пользователя.")
        return
    user_id = resolve_user_from_message(message)
    if not user_id or not UserService.get_user(user_id):
        await message.answer("❗ Пользователь не найден. Введите @username или ID ещё раз.")
        await state.set_state(AdminState.waiting_for_user_lookup)
        return
    await state.update_data(lookup_user_id=user_id)
    await state.clear()
    await _show_admin_user_profile(message, user_id)


@router.callback_query(F.data.startswith("admin_udisc_clear_"))
async def admin_user_discount_clear(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        user_id = int(callback.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка ID", show_alert=True)
        return
    UserService.clear_personal_discount(user_id)
    try:
        await bot.send_message(user_id, "📉 Персональная скидка снята.")
    except Exception:
        pass
    await _show_admin_user_profile(callback, user_id)


@router.callback_query(F.data.startswith("admin_udisc_"))
async def admin_user_discount_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        user_id = int(callback.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка ID", show_alert=True)
        return
    await state.update_data(lookup_user_id=user_id)
    await state.set_state(AdminState.waiting_for_user_discount_percent)
    await callback.message.edit_text(
        f"📉 Скидка для <code>{user_id}</code>\n\n"
        "Введите процент скидки (1-90), например <code>15</code>:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=f"admin_user_back_{user_id}")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user_back_"))
async def admin_user_back(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    try:
        user_id = int(callback.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка ID", show_alert=True)
        return
    await _show_admin_user_profile(callback, user_id)


@router.message(AdminState.waiting_for_user_discount_percent)
async def admin_user_discount_percent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace("%", "").strip()
    try:
        percent = int(raw)
    except ValueError:
        await message.answer("🔶 Введите число от 1 до 90.")
        return
    if percent < 1 or percent > 90:
        await message.answer("🔶 Скидка должна быть от 1 до 90%.")
        return
    await state.update_data(discount_percent=percent)
    await state.set_state(AdminState.waiting_for_user_discount_uses)
    await message.answer(
        f"Скидка <b>{percent}%</b>.\n"
        "На сколько покупок она действует? Например <code>3</code>."
    )


@router.message(AdminState.waiting_for_user_discount_uses)
async def admin_user_discount_uses(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    try:
        uses = int((message.text or "").strip())
    except ValueError:
        await message.answer("🔶 Введите целое число покупок, например 3.")
        return
    if uses < 1 or uses > 50:
        await message.answer("🔶 Число покупок от 1 до 50.")
        return
    data = await state.get_data()
    user_id = int(data.get("lookup_user_id") or 0)
    percent = int(data.get("discount_percent") or 0)
    if not user_id or not percent:
        await state.clear()
        await message.answer("🔶 Сессия сброшена. Найдите пользователя заново.", reply_markup=kb.get_admin_keyboard())
        return
    UserService.set_personal_discount(user_id, percent, uses)
    await state.clear()
    try:
        await bot.send_message(
            user_id,
            f"📉 Вам выдана скидка <b>{percent}%</b> на <b>{uses}</b> покупки.\n"
            "Она применится при заказе и сгорит, когда лимит закончится.",
        )
    except Exception:
        pass
    await message.answer(f"✅ Скидка {percent}% на {uses} пок. выдана.")
    await _show_admin_user_profile(message, user_id)


# --- Главная Панель ---

@router.message(Command('admin'))
async def cmd_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    
    if not is_admin(message.from_user.id):
        print(f'Unauthorized admin attempt: {message.from_user.id} vs {ADMIN_ID}')
        return

    await message.answer(
        '🛠 <b>Панель Администратора</b>\n'
        'Выберите действие:',
        reply_markup=kb.get_admin_keyboard()
    )

@router.callback_query(F.data == 'admin_close')
async def admin_close(callback: CallbackQuery):
    await callback.message.delete()

@router.callback_query(F.data == 'admin_back_main')
async def admin_back_main(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        '🛠 <b>Панель Администратора</b>\nВыберите действие:',
        reply_markup=kb.get_admin_keyboard()
    )


def _format_admin_roulette_menu_text() -> str:
    return (
        "🎰 <b>Игры / розыгрыши</b>\n\n"
        "Создайте игру: название, описание, лимит участников, число победителей и приз каждого места.\n"
        "Победителей по местам выбираете только вы - для игроков это обычный розыгрыш."
    )


def _format_admin_prizes_block(game: dict) -> str:
    prizes = RouletteService.prizes_of(game)
    if not prizes:
        return "Призы не заданы"
    lines = []
    assigned = {
        int(item.get("place", 0) or 0): int(item.get("user_id", 0) or 0)
        for item in RouletteService.place_winners_of(game)
    }
    for prize in prizes:
        place = int(prize.get("place", 0) or 0)
        kind = "💰" if prize.get("kind") == "money" else "🎁"
        winner_id = assigned.get(place)
        secret = f" → ID <code>{winner_id}</code>" if winner_id else " → не выбран"
        lines.append(f"{place} место {kind} {html.escape(str(prize.get('label') or 'приз'))}{secret}")
    return "\n".join(lines)


def _format_admin_roulette_game_text(game: dict) -> str:
    participants = game.get("participants", [])
    status_text = "🟢 Активна" if game.get("status") == "active" else "⚪️ Закрыта"
    channel = game.get("required_channel") or "не требуется"
    announced = "да" if game.get("result_announced") else "нет"
    description = game.get("description") or game.get("conditions_text") or "-"
    return (
        f"🎰 <b>Игра #{game.get('id')}</b>\n"
        f"Название: <b>{html.escape(game.get('title', 'Без названия'))}</b>\n"
        f"Статус: {status_text}\n"
        f"Лимит участников: <b>{len(participants)}/{game.get('max_participants', 0)}</b>\n"
        f"Победителей: <b>{game.get('winners_count', 0)}</b>\n"
        f"Результат объявлен: <b>{announced}</b>\n"
        f"Канал для проверки: <code>{html.escape(str(channel))}</code>\n\n"
        f"Описание:\n{html.escape(str(description))}\n\n"
        f"Призы (видно только вам, кто выбран):\n{_format_admin_prizes_block(game)}"
    )


def _admin_roulette_kb(game: dict | None):
    if not game:
        return None
    return kb.get_admin_roulette_manage_keyboard(
        int(game.get("id", 0) or 0),
        is_active=game.get("status") == "active",
        result_announced=bool(game.get("result_announced")),
    )


def _format_admin_roulette_participants_text(game: dict) -> str:
    participants = game.get("participants", [])
    if not participants:
        return "👥 <b>Участники</b>\n\nПока никто не записался."

    lines = [f"👥 <b>Участники игры #{game.get('id')}</b>"]
    winner_ids = {int(item) for item in game.get("winners", [])}
    for idx, item in enumerate(participants, start=1):
        uid = int(item.get("user_id", 0) or 0)
        winner_mark = "🏆 " if uid in winner_ids else ""
        lines.append(f"{idx}. {winner_mark}{RouletteService.format_participant_name(item)}")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_roulette_menu")
async def admin_roulette_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.clear()
    games = RouletteService.get_all_games(include_closed=True)
    await callback.message.edit_text(
        _format_admin_roulette_menu_text(),
        reply_markup=kb.get_admin_roulette_menu_keyboard(has_games=bool(games)),
    )
    await callback.answer()


def _admin_race_text() -> str:
    board = ReferralRaceService.board()
    race = board.get("race") or {}
    running = board.get("running")
    real = ReferralRaceService.real_leaderboard()
    awarded = race.get("awarded") or {}
    lines = [
        "🏁 <b>Гонка рефералов</b>\n",
        f"Статус: <b>{'идёт' if running else 'нет активной'}</b>",
    ]
    if race.get("started_at"):
        lines.append(f"Старт: {race.get('started_at')}")
        lines.append(f"Финиш: {race.get('ends_at')}")
        lines.append(f"Массовка: {len(race.get('fakes') or [])} участников (имена скрыты у людей)")
    lines.append("Призы: 5000 / 2500 / 1000 ₽ на баланс.\n")
    if awarded:
        lines.append("Выдано:")
        for place in ("1", "2", "3"):
            if awarded.get(place):
                lines.append(f"• {place} место → <code>{awarded[place]}</code> ({RACE_PRIZES[int(place)]} ₽)")
        lines.append("")
    lines.append("<b>Живые участники</b> (боевые / просто зашли):")
    if not real:
        lines.append("пока никого")
    for item in real[:15]:
        lines.append(
            f"#{item['place']} <code>{item['id']}</code> - заказ {item['combat']}, зашли {item['gray']}"
        )
    lines.append("\nКнопки 🥇🥈🥉 выдают приз этому ID.")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_race_menu")
async def admin_race_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    running = ReferralRaceService.is_running()
    real = ReferralRaceService.real_leaderboard()
    await callback.message.edit_text(
        _admin_race_text(),
        reply_markup=kb.get_admin_race_keyboard(running, real),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_race_start")
async def admin_race_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if ReferralRaceService.is_running():
        await callback.answer("Гонка уже идёт", show_alert=True)
        return
    ReferralRaceService.start()
    running = True
    real = ReferralRaceService.real_leaderboard()
    await callback.message.edit_text(
        _admin_race_text(),
        reply_markup=kb.get_admin_race_keyboard(running, real),
    )
    await callback.answer("Гонка запущена на 48 часов")


@router.callback_query(F.data == "admin_race_stop")
async def admin_race_stop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ReferralRaceService.stop()
    real = ReferralRaceService.real_leaderboard()
    await callback.message.edit_text(
        _admin_race_text(),
        reply_markup=kb.get_admin_race_keyboard(False, real),
    )
    await callback.answer("Гонка остановлена")


@router.callback_query(F.data.regexp(r"^arw[123]_\d+$"))
async def admin_race_award(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = callback.data or ""
    try:
        place = int(data[3])
        user_id = int(data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    result = ReferralRaceService.award(place, user_id)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "place":
            await callback.answer("Это место уже выдано", show_alert=True)
        elif reason == "already":
            await callback.answer("Этому ID уже выдали место", show_alert=True)
        else:
            await callback.answer("Не удалось выдать", show_alert=True)
        return
    prize = int(result["prize"])
    credit = UserService.credit_balance(user_id, float(prize), source="bonus")
    credited = float(credit.get("credited") or 0)
    try:
        await bot.send_message(
            user_id,
            f"🏁 <b>Гонка рефералов</b>\n\n"
            f"Вы заняли <b>{place} место</b>.\n"
            f"На баланс зачислено: <b>{credited:.0f} ₽</b>.",
            reply_markup=kb.get_base_keyboard(),
        )
    except Exception:
        pass
    await notify_bonus_cap(bot, user_id)
    real = ReferralRaceService.real_leaderboard()
    await callback.message.edit_text(
        _admin_race_text(),
        reply_markup=kb.get_admin_race_keyboard(ReferralRaceService.is_running(), real),
    )
    await callback.answer(f"{place} место: {prize} ₽")


@router.callback_query(F.data == "admin_roulette_create_start")
async def admin_roulette_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminState.waiting_for_roulette_title)
    await callback.message.edit_text(
        "🎰 <b>Создание игры</b>\n\n"
        "Шаг 1/6: отправьте название игры.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_roulette_menu")]]
        ),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_roulette_title)
async def admin_roulette_create_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("🔶 Название слишком короткое. Минимум 3 символа.")
        return

    await state.update_data(roulette_title=title)
    await state.set_state(AdminState.waiting_for_roulette_conditions)
    await message.answer("Шаг 2/6: отправьте описание игры одним сообщением.")


@router.message(AdminState.waiting_for_roulette_max)
async def admin_roulette_create_max(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("🔶 Введите целое число.")
        return

    value = int(raw)
    if value < 2 or value > 10000:
        await message.answer("🔶 Лимит должен быть от 2 до 10000.")
        return

    await state.update_data(roulette_max_participants=value)
    await state.set_state(AdminState.waiting_for_roulette_winners_count)
    await message.answer("Шаг 4/6: сколько будет победителей? (целое число, например 3).")


@router.message(AdminState.waiting_for_roulette_conditions)
async def admin_roulette_create_conditions(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    conditions_text = (message.text or "").strip()
    if len(conditions_text) < 3:
        await message.answer("🔶 Описание слишком короткое.")
        return

    await state.update_data(roulette_conditions_text=conditions_text)
    await state.set_state(AdminState.waiting_for_roulette_max)
    await message.answer("Шаг 3/6: укажите лимит участников (число, например 50).")


@router.message(AdminState.waiting_for_roulette_channel)
async def admin_roulette_create_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    channel_raw = (message.text or "").strip()
    required_channel = None if channel_raw in {"-", "нет", "no"} else channel_raw

    data = await state.get_data()
    prizes = data.get("roulette_prizes") or []
    game = RouletteService.create_game(
        created_by=message.from_user.id,
        title=data.get("roulette_title", "Игра"),
        max_participants=int(data.get("roulette_max_participants", 0) or 0),
        conditions_text=data.get("roulette_conditions_text", ""),
        description=data.get("roulette_conditions_text", ""),
        required_channel=required_channel,
        winners_count=int(data.get("roulette_winners_count", 0) or 0),
        prizes=prizes,
    )

    await state.clear()
    await message.answer(
        "✅ <b>Игра создана</b>\n"
        f"ID: <code>{game.get('id')}</code>\n"
        f"Название: <b>{html.escape(game.get('title', ''))}</b>\n"
        f"Участников: <b>{game.get('max_participants')}</b>\n"
        f"Победителей: <b>{game.get('winners_count')}</b>\n\n"
        "Игроки видят обычный розыгрыш. Победителей по местам выбираете только вы.",
        reply_markup=_admin_roulette_kb(game),
    )


@router.message(AdminState.waiting_for_roulette_winners_count)
async def admin_roulette_create_winners_count(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("🔶 Введите целое число.")
        return

    value = int(raw)
    data = await state.get_data()
    max_participants = int(data.get("roulette_max_participants", 0) or 0)
    if value < 1 or value > 50:
        await message.answer("🔶 Количество победителей должно быть от 1 до 50.")
        return
    if max_participants and value > max_participants:
        await message.answer("🔶 Победителей не может быть больше лимита участников.")
        return

    await state.update_data(roulette_winners_count=value)
    await state.set_state(AdminState.waiting_for_roulette_place_prizes)
    await message.answer(
        f"Шаг 5/6: что выиграют места? Нужно <b>{value}</b> приз(а/ов).\n"
        "Каждое место с новой строки или через запятую.\n"
        "Если приз - деньги, напишите сумму числом: она сразу уйдёт на баланс победителя.\n"
        "Если товар или другой приз - напишите текстом.\n\n"
        "Пример:\n<code>5000\n3000\nНаушники</code>"
    )


@router.message(AdminState.waiting_for_roulette_place_prizes)
async def admin_roulette_create_place_prizes(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    winners_count = int(data.get("roulette_winners_count", 0) or 0)
    prizes, error = RouletteService.parse_prize_lines(message.text or "", winners_count)
    if error or not prizes:
        await message.answer(f"🔶 {error or 'Не удалось разобрать призы.'}")
        return

    await state.update_data(roulette_prizes=prizes)
    await state.set_state(AdminState.waiting_for_roulette_channel)
    await message.answer(
        "Шаг 6/6: канал для проверки подписки\n"
        "(например @mychannel или -1001234567890).\n"
        "Если проверка не нужна, отправьте: <code>-</code>"
    )


@router.callback_query(F.data == "admin_roulette_games")
async def admin_roulette_games(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    games = RouletteService.get_all_games(include_closed=True)
    if not games:
        await callback.message.edit_text(
            "🎰 Игр пока нет.",
            reply_markup=kb.get_admin_roulette_menu_keyboard(has_games=False),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "🎛 <b>Управление играми</b>\nВыберите игру:",
        reply_markup=kb.get_admin_roulette_games_keyboard(games),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_roulette_game_"))
async def admin_roulette_game_open(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    await callback.message.edit_text(
        _format_admin_roulette_game_text(game),
        reply_markup=_admin_roulette_kb(game),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_roulette_participants_"))
async def admin_roulette_participants(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    await callback.message.edit_text(
        _format_admin_roulette_participants_text(game),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 К игре", callback_data=f"admin_roulette_game_{game_id}")]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_roulette_set_winners_"))
async def admin_roulette_set_winners(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    await _open_roulette_winner_picker(callback, state, game_id)


async def _open_roulette_winner_picker(
    callback: CallbackQuery,
    state: FSMContext,
    game_id: int,
    *,
    early_draw_note: bool = False,
):
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    participants = game.get("participants", [])
    if not participants:
        await callback.answer("Нет участников", show_alert=True)
        return

    place = RouletteService.next_unassigned_place(game)
    if place is None:
        await callback.answer("Все места уже назначены. Можно запускать розыгрыш.", show_alert=True)
        await callback.message.edit_text(
            _format_admin_roulette_game_text(game),
            reply_markup=_admin_roulette_kb(game),
        )
        return

    prize = next((item for item in RouletteService.prizes_of(game) if int(item.get("place", 0) or 0) == place), None)
    lines = [
        f"🏆 <b>Секретный выбор победителей</b>",
        f"Игра #{game_id}: {html.escape(str(game.get('title') or ''))}",
        "",
        "Игроки этого не видят. Для них это обычный розыгрыш.",
    ]
    if early_draw_note:
        lines.append("🎯 Назначьте победителя на каждое место, затем запустите розыгрыш.")
    lines.extend(
        [
            "",
            f"Сейчас место <b>{place}</b> из {int(game.get('winners_count') or 0)}.",
            f"Приз: <b>{html.escape(str((prize or {}).get('label') or 'приз'))}</b>",
            "",
            "Участники:",
        ]
    )
    for idx, item in enumerate(participants, start=1):
        uid = int(item.get("user_id", 0) or 0)
        lines.append(f"{idx}. {RouletteService.format_participant_name(item)} - ID: <code>{uid}</code>")
    lines.extend(
        [
            "",
            "Отправьте ID или @username победителя этого места.",
        ]
    )

    await state.set_state(AdminState.waiting_for_roulette_place_winner)
    await state.update_data(roulette_winner_game_id=game_id, roulette_winner_place=place)
    await callback.message.edit_text("\n".join(lines))
    await callback.answer()


@router.message(AdminState.waiting_for_roulette_place_winner)
async def admin_roulette_save_place_winner(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    game_id = int(data.get("roulette_winner_game_id", 0) or 0)
    place = int(data.get("roulette_winner_place", 0) or 0)
    if not game_id or not place:
        await state.clear()
        await message.answer("🔶 Контекст потерян. Откройте игру заново из админ-панели.")
        return

    resolved_user_id = resolve_user_input((message.text or "").strip())
    if not resolved_user_id:
        await message.answer("🔶 Не удалось распознать пользователя. Формат: 123456 или @username")
        return

    ok, status = RouletteService.assign_place_winner(game_id, place, int(resolved_user_id))
    if not ok:
        await message.answer(f"🔶 {status}")
        return

    game = RouletteService.get_game(game_id)
    if not game:
        await state.clear()
        await message.answer("🔶 Игра не найдена.")
        return

    next_place = RouletteService.next_unassigned_place(game)
    if next_place is None:
        await state.clear()
        await message.answer(
            "✅ Все места назначены. Игроки об этом не знают.\n"
            "Нажмите «Запуск розыгрыша», когда будете готовы объявить результат.",
            reply_markup=_admin_roulette_kb(game),
        )
        return

    prize = next((item for item in RouletteService.prizes_of(game) if int(item.get("place", 0) or 0) == next_place), None)
    await state.update_data(roulette_winner_place=next_place)
    await message.answer(
        f"✅ {place} место сохранено.\n\n"
        f"Следующее: место <b>{next_place}</b>.\n"
        f"Приз: <b>{html.escape(str((prize or {}).get('label') or 'приз'))}</b>\n"
        "Отправьте ID или @username победителя."
    )


@router.callback_query(F.data.startswith("admin_roulette_force_draw_"))
async def admin_roulette_force_draw(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    if not game.get("participants"):
        await callback.answer("Нельзя провести розыгрыш без участников", show_alert=True)
        return

    if not RouletteService.all_places_assigned(game):
        await callback.answer("Сначала назначьте победителя на каждое место.", show_alert=True)
        return

    RouletteService.close_game(game_id)
    payout_ok, payout_msg, payout_info = RouletteService.apply_finish_payouts(game_id)
    if not payout_ok:
        await callback.answer(payout_msg, show_alert=True)
        return

    RouletteService.mark_result_announced(game_id)
    updated_game = RouletteService.get_game(game_id)
    if not updated_game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    from app.handlers.user_flow import deliver_pending_roulette_results_for_bot

    sent, failed, failure_notes = await deliver_pending_roulette_results_for_bot(bot, game_id=game_id)
    await state.clear()
    await callback.message.edit_text(
        _format_admin_roulette_game_text(updated_game),
        reply_markup=_admin_roulette_kb(updated_game),
    )
    details = ""
    if failure_notes:
        top = "\n".join(failure_notes[:5])
        details = f"\n\nПричины (первые 5):\n<code>{html.escape(top)}</code>"
    credited = int((payout_info or {}).get("credited") or 0)
    total = float((payout_info or {}).get("total") or 0.0)
    await notify_bonus_cap_many(bot, (payout_info or {}).get("user_ids"))

    await callback.message.answer(
        "✅ Розыгрыш завершён. Для игроков это обычный результат.\n"
        f"💰 На баланс начислено: <b>{credited}</b> выплат на <b>{total:.2f} руб</b>\n"
        f"📨 На этом боте отправлено: <b>{sent}</b>\n"
        f"⏳ Остальным уйдёт с того бота, где пользователь открыт, как в авторассылке."
        f"{details}"
    )
    await callback.answer("Розыгрыш завершен")


@router.callback_query(F.data.startswith("admin_roulette_del_req_"))
async def admin_roulette_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    await callback.message.edit_text(
        f"🗑 Удалить игру <b>#{game_id}</b> навсегда?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_roulette_del_yes_{game_id}")],
                [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"admin_roulette_game_{game_id}")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_roulette_del_yes_"))
async def admin_roulette_delete_execute(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    if not RouletteService.delete_game(game_id):
        await callback.answer("Игра не найдена", show_alert=True)
        return

    games = RouletteService.get_all_games(include_closed=True)
    await callback.message.edit_text(
        "✅ Игра удалена.",
        reply_markup=kb.get_admin_roulette_games_keyboard(games)
        if games
        else kb.get_admin_roulette_menu_keyboard(has_games=False),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_roulette_close_"))
async def admin_roulette_close(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    if not RouletteService.close_game(game_id):
        await callback.answer("Игра не найдена", show_alert=True)
        return

    game = RouletteService.get_game(game_id)
    await callback.message.edit_text(
        _format_admin_roulette_game_text(game),
        reply_markup=_admin_roulette_kb(game),
    )
    await callback.answer("Набор закрыт")


@router.callback_query(F.data.startswith("admin_roulette_reopen_"))
async def admin_roulette_reopen(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    game_id = int(callback.data.rsplit("_", 1)[1])
    game = RouletteService.get_game(game_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    max_participants = int(game.get("max_participants", 0) or 0)
    current_count = len(game.get("participants", []))
    if max_participants > 0 and current_count >= max_participants:
        await callback.answer("Лимит уже заполнен, открыть нельзя", show_alert=True)
        return

    RouletteService.reopen_game(game_id)
    game = RouletteService.get_game(game_id)
    await callback.message.edit_text(
        _format_admin_roulette_game_text(game),
        reply_markup=_admin_roulette_kb(game),
    )
    await callback.answer("Набор открыт")


@router.callback_query(F.data == 'admin_users_clear_confirm')
async def admin_users_clear_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, очистить", callback_data="admin_users_clear_exec")],
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin_back_main")],
        ]
    )
    await callback.message.edit_text(
        "🔶 <b>Очистка базы пользователей</b>\n\n"
        "Будут удалены только пользователи без баланса и без покупок.\n"
        "Пользователи с ненулевым балансом или историей покупок останутся в users_db.\n"
        "Продолжить?",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_users_clear_exec')
async def admin_users_clear_exec(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    removed = UserService.clear_users_db()
    await callback.message.edit_text(
        "✅ <b>База пользователей очищена.</b>\n"
        f"Удалено записей: <b>{removed}</b>\n\n"
        "Пользователи с балансом или покупками сохранены. Новые и вернувшиеся пользователи будут создаваться автоматически при оформлении заказа.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="↩️ В админку", callback_data="admin_back_main")]]
        ),
    )
    await callback.answer("Готово")


@router.message(Command('export_catalog_pdf'))
async def cmd_export_catalog_pdf(message: Message):
    if not is_admin(message.from_user.id):
        return

    try:
        pdf_bytes = await asyncio.to_thread(PdfExportService.build_catalog_pdf)
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка генерации PDF выгрузки каталога")
        await message.answer(f"🔶 Не удалось собрать PDF: {err}")
        return

    file = BufferedInputFile(pdf_bytes, filename="catalog_export.pdf")
    await message.answer_document(
        file,
        caption="📄 PDF-выгрузка каталога готова. Внутри города, товары, веса, районы, типы кладов и наценки."
    )


@router.callback_query(F.data == 'admin_export_catalog_pdf')
async def admin_export_catalog_pdf(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        pdf_bytes = await asyncio.to_thread(PdfExportService.build_catalog_pdf)
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка генерации PDF выгрузки каталога")
        await callback.answer("Не удалось собрать PDF", show_alert=True)
        await callback.message.answer(f"🔶 Не удалось собрать PDF: {err}")
        return

    file = BufferedInputFile(pdf_bytes, filename="catalog_export.pdf")
    await callback.message.answer_document(
        file,
        caption="📄 PDF-выгрузка каталога готова. Внутри города, товары, веса, районы, типы кладов и наценки."
    )
    await callback.answer("PDF отправлен")

from app.services.settings import SettingsService
from app.services.crypto import CryptoService


def _build_global_stash_text(types: list[str], title: str = "🏷 <b>Глобальные типы кладов</b>") -> str:
    lines = [title, "Эти варианты доступны при настройке товаров:"]
    for idx, item in enumerate(types, start=1):
        lines.append(f"{idx}. {item}")
    lines.append("\nМинимум два типа должны оставаться в списке.")
    return "\n".join(lines)


async def _show_global_stash_menu(target, state: FSMContext | None = None, scope: str = "regular"):
    is_miniopt = scope == "miniopt"
    types = CatalogService.get_available_miniopt_stash_types() if is_miniopt else CatalogService.get_available_stash_types()
    text = _build_global_stash_text(
        types,
        title="🧺 <b>Глобальные типы кладов miniopt</b>" if is_miniopt else "🏷 <b>Глобальные типы кладов</b>",
    )
    markup = kb.get_admin_global_stash_menu(
        types,
        delete_prefix="adm_global_miniopt_stash_del_" if is_miniopt else "adm_global_stash_del_",
        add_callback="adm_global_miniopt_stash_add" if is_miniopt else "adm_global_stash_add",
        back_callback="admin_back_main",
    )
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_pricing_menu(
    target: CallbackQuery | Message,
    state: FSMContext,
    *,
    bot: Bot | None = None,
    edit_chat_id: int | None = None,
    edit_message_id: int | None = None
):
    region_entries = PricingService.get_region_entries()
    await state.update_data(
        pricing_region_options=region_entries,
        pricing_prompt_chat_id=None,
        pricing_prompt_message_id=None
    )

    lines: list[str] = ["💰 <b>Управление наценками</b>"]
    if region_entries:
        lines.append("\n<b>Регионы:</b>")
        for entry in region_entries:
            lines.append(f"- {entry['label']}: x{entry['value']:.2f}")
    else:
        lines.append("\nРегионы: наценки не заданы.")

    lines.append("\nℹ️ Введите множитель, например 1.25. Значение 1 отключает наценку.")

    buttons: list[list[InlineKeyboardButton]] = []
    for idx, entry in enumerate(region_entries):
        label = f"{entry['label']} ({entry['value']:.2f}x)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"adm_price_reg_{idx}")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить регион", callback_data="adm_price_region_add")])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="admin_back_main")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "\n".join(lines)

    if bot and edit_chat_id and edit_message_id:
        try:
            await bot.edit_message_text(text, chat_id=edit_chat_id, message_id=edit_message_id, reply_markup=markup)
            await state.set_state(None)
            return
        except TelegramBadRequest:
            pass

    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    await state.set_state(None)


def _format_crypto_rate_stamp(updated_at: str | None) -> str | None:
    if not updated_at:
        return None
    text = str(updated_at).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return str(updated_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


async def _show_crypto_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    btc_rate, ltc_rate, updated_at = SettingsService.get_crypto_rates()
    text, markup = kb.get_admin_crypto_menu(
        SettingsService.get_btc_wallets(),
        SettingsService.get_ltc_wallets(),
        btc_rate,
        ltc_rate,
        _format_crypto_rate_stamp(updated_at),
    )

    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)


async def _show_ltc_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    await _show_crypto_menu(target, state)


def _build_manual_payment_template_text(template: str | None) -> str:
    base = [
        "✉️ <b>Шаблон ручной оплаты</b>",
        "Этот текст отправляется клиенту после подключения оператора в ручной оплате.",
        "",
        "Доступные плейсхолдеры:",
        "• {user_id}",
        "• {amount}",
        "• {expires_at}",
        "• {city}",
        "• {district}",
        "• {product}",
        "• {card}",
        "",
    ]
    if template:
        base.append("Текущий шаблон:")
        base.append(f"<pre>{template}</pre>")
    else:
        base.append("Текущий шаблон: <i>не задан</i>")
    return "\n".join(base)


def _normalize_rf_card(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 12 or len(digits) > 19:
        return None
    groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
    return " ".join(groups)


def _build_rf_cards_text(cards: list[str]) -> str:
    lines = ["💳 <b>Карты РФ для оплаты</b>", "Выдаются случайно при оплате на карту РФ.", ""]
    if cards:
        lines.append("Текущие карты:")
        for idx, card in enumerate(cards, start=1):
            lines.append(f"{idx}. <code>{card}</code>")
        lines.append("\nНажмите ❗ у карты, чтобы удалить её.")
    else:
        lines.append("Текущие карты: <i>не заданы</i>")
    lines.append("Добавление: по одной карте в строке (или через запятую).")
    return "\n".join(lines)


def _rf_cards_keyboard(cards: list[str]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, card in enumerate(cards):
        buttons.append([
            InlineKeyboardButton(text=card, callback_data=f"admin_rf_card_noop_{idx}"),
            InlineKeyboardButton(text="❗", callback_data=f"admin_rf_card_del_{idx}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="admin_rf_cards_set")])
    if cards:
        buttons.append([InlineKeyboardButton(text="🗑 Очистить все", callback_data="admin_rf_cards_clear")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_rf_cards_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    cards = SettingsService.get_rf_cards()
    text = _build_rf_cards_text(cards)
    markup = _rf_cards_keyboard(cards)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)


def _wallet_button_text(wallet: str) -> str:
    if len(wallet) <= 28:
        return wallet
    return wallet[:25] + "..."


def _build_yoomoney_wallets_text(wallets: list[str]) -> str:
    lines = [
        "💜 <b>Кошельки ЮMoney</b>",
        "Выдаются случайно при оплате ЮMoney. Перевод только с ЮMoney на ЮMoney.",
        "Таймер заявки: 25 минут.",
        "",
    ]
    if wallets:
        lines.append("Текущие кошельки:")
        for idx, wallet in enumerate(wallets, start=1):
            lines.append(f"{idx}. <code>{wallet}</code>")
        lines.append("\nНажмите ❗ у кошелька, чтобы удалить его.")
    else:
        lines.append("Текущие кошельки: <i>не заданы</i>")
    lines.append("Добавление: по одному номеру в строке (или через запятую).")
    return "\n".join(lines)


def _yoomoney_wallets_keyboard(wallets: list[str]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, wallet in enumerate(wallets):
        buttons.append([
            InlineKeyboardButton(text=_wallet_button_text(wallet), callback_data=f"admin_yoomoney_noop_{idx}"),
            InlineKeyboardButton(text="❗", callback_data=f"admin_yoomoney_del_{idx}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="admin_yoomoney_set")])
    if wallets:
        buttons.append([InlineKeyboardButton(text="🗑 Очистить все", callback_data="admin_yoomoney_clear")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_yoomoney_wallets_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    wallets = SettingsService.get_yoomoney_wallets()
    text = _build_yoomoney_wallets_text(wallets)
    markup = _yoomoney_wallets_keyboard(wallets)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)


async def _show_manual_payment_template_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    template = SettingsService.get_manual_payment_template()
    text = _build_manual_payment_template_text(template)
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_manual_pay_template_set")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="admin_manual_pay_template_clear")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)

@router.callback_query(F.data == 'admin_toggle_redirect')
async def admin_toggle_redirect(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    current = SettingsService.is_redirect_active()
    new_state = not current
    SettingsService.set_redirect_active(new_state)
    
    status = "ON" if new_state else "OFF"
    await callback.answer(f"Редирект на Авто-обмен: {status}")
    
    # Обновляем клавиатуру
    await callback.message.edit_reply_markup(reply_markup=kb.get_admin_keyboard())

# --- Управление наценками ---

@router.callback_query(F.data == 'admin_pricing_menu')
async def admin_pricing_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_pricing_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.in_({"admin_ltc_menu", "admin_crypto_menu"}))
async def admin_crypto_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_crypto_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_manual_pay_template')
async def admin_manual_pay_template(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_manual_payment_template_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_cards_menu')
async def admin_rf_cards_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_rf_cards_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_unblock_menu')
async def admin_rf_unblock_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_rf_unblock_id)
    await callback.message.edit_text(
        "Введите ID или @username для разблокировки оплаты картой РФ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_back_main")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_manual_pay_template_set')
async def admin_manual_pay_template_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_manual_payment_template)
    await callback.message.edit_text(
        "Введите текст шаблона одним сообщением. Можно использовать плейсхолдеры {user_id}, {amount}, {expires_at}, {city}, {district}, {product}, {card}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_manual_pay_template")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_cards_set')
async def admin_rf_cards_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_rf_cards)
    await callback.message.edit_text(
        "Введите карты для добавления (по одной в строке или через запятую).\n"
        "Они будут добавлены к текущему списку, дубликаты пропустятся.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_rf_cards_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_manual_pay_template_clear')
async def admin_manual_pay_template_clear(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_manual_payment_template(None)
    await _show_manual_payment_template_menu(callback, state)
    await callback.answer("Шаблон очищен")


@router.callback_query(F.data == 'admin_rf_cards_clear')
async def admin_rf_cards_clear(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_rf_cards([])
    await _show_rf_cards_menu(callback, state)
    await callback.answer("Карты очищены")


@router.callback_query(F.data.startswith("admin_rf_card_noop_"))
async def admin_rf_card_noop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer("Нажмите ❗ справа, чтобы удалить карту")


@router.callback_query(F.data.startswith("admin_rf_card_del_"))
async def admin_rf_card_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    raw_idx = callback.data[len("admin_rf_card_del_"):]
    try:
        idx = int(raw_idx)
    except (TypeError, ValueError):
        await callback.answer("Некорректная карта", show_alert=True)
        return

    removed = SettingsService.remove_rf_card_at(idx)
    if not removed:
        await callback.answer("Карта уже удалена")
        await _show_rf_cards_menu(callback, state)
        return

    await _show_rf_cards_menu(callback, state)
    await callback.answer(f"Удалено: {removed}")


@router.message(AdminState.waiting_for_manual_payment_template)
async def admin_manual_pay_template_save(message: Message, state: FSMContext):
    template = (message.text or "").strip()
    if not template:
        await message.answer("🔶 Шаблон не может быть пустым.")
        return

    SettingsService.set_manual_payment_template(template)
    await message.answer("✅ Шаблон сохранен.")
    await state.set_state(None)
    await _show_manual_payment_template_menu(message, state)


@router.message(AdminState.waiting_for_rf_cards)
async def admin_rf_cards_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("🔶 Список карт не может быть пустым.")
        return

    chunks = []
    for line in raw.splitlines():
        parts = re.split(r"[,;]", line)
        for part in parts:
            item = part.strip()
            if item:
                chunks.append(item)

    cards: list[str] = []
    for item in chunks:
        normalized = _normalize_rf_card(item)
        if normalized and normalized not in cards:
            cards.append(normalized)

    if not cards:
        await message.answer("🔶 Не удалось распознать номера карт. Пример: 2200 7020 2889 9388")
        return

    existing = SettingsService.get_rf_cards()
    merged = list(existing)
    added = 0
    for card in cards:
        if card not in merged:
            merged.append(card)
            added += 1

    SettingsService.set_rf_cards(merged)
    if added:
        skipped = len(cards) - added
        extra = f" (дубликатов пропущено: {skipped})" if skipped else ""
        await message.answer(f"✅ Добавлено карт: {added}.{extra}")
    else:
        await message.answer("🔶 Все введённые карты уже есть в списке.")
    await state.set_state(None)
    await _show_rf_cards_menu(message, state)


@router.callback_query(F.data == "admin_yoomoney_menu")
async def admin_yoomoney_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_yoomoney_wallets_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_yoomoney_set")
async def admin_yoomoney_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_yoomoney_wallets)
    await callback.message.edit_text(
        "Введите кошельки ЮMoney для добавления (по одному в строке или через запятую).\n"
        "Номер кошелька, например <code>410011605020257</code>.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_yoomoney_menu")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_yoomoney_clear")
async def admin_yoomoney_clear(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_yoomoney_wallets([])
    await _show_yoomoney_wallets_menu(callback, state)
    await callback.answer("Кошельки очищены")


@router.callback_query(F.data.startswith("admin_yoomoney_noop_"))
async def admin_yoomoney_noop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer("Нажмите ❗ справа, чтобы удалить кошелёк")


@router.callback_query(F.data.startswith("admin_yoomoney_del_"))
async def admin_yoomoney_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    raw_idx = callback.data[len("admin_yoomoney_del_"):]
    try:
        idx = int(raw_idx)
    except (TypeError, ValueError):
        await callback.answer("Некорректный кошелёк", show_alert=True)
        return

    removed = SettingsService.remove_yoomoney_wallet_at(idx)
    if not removed:
        await callback.answer("Кошелёк уже удалён")
        await _show_yoomoney_wallets_menu(callback, state)
        return

    await _show_yoomoney_wallets_menu(callback, state)
    await callback.answer(f"Удалено: {removed}")


@router.message(AdminState.waiting_for_yoomoney_wallets)
async def admin_yoomoney_wallets_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("🔶 Список кошельков не может быть пустым.")
        return

    chunks = []
    for line in raw.splitlines():
        parts = re.split(r"[,;]", line)
        for part in parts:
            item = part.strip()
            if item:
                chunks.append(item)

    wallets: list[str] = []
    for item in chunks:
        normalized = SettingsService.normalize_yoomoney_wallet(item)
        if normalized and normalized not in wallets:
            wallets.append(normalized)

    if not wallets:
        await message.answer("🔶 Не удалось распознать кошельки. Пример: <code>410011605020257</code>")
        return

    existing = SettingsService.get_yoomoney_wallets()
    merged = list(existing)
    added = 0
    for wallet in wallets:
        if wallet not in merged:
            merged.append(wallet)
            added += 1

    SettingsService.set_yoomoney_wallets(merged)
    if added:
        skipped = len(wallets) - added
        extra = f" (дубликатов пропущено: {skipped})" if skipped else ""
        await message.answer(f"✅ Добавлено кошельков: {added}.{extra}")
    else:
        await message.answer("🔶 Все введённые кошельки уже есть в списке.")
    await state.set_state(None)
    await _show_yoomoney_wallets_menu(message, state)


@router.message(AdminState.waiting_for_rf_unblock_id)
async def admin_rf_unblock_by_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)

    if not user_id:
        await message.answer("❗ Пользователь не найден. Введите корректный ID или @username.")
        return

    if UserService.unblock_rf_card_payment(user_id):
        await message.answer(f"✅ Оплата картой РФ разблокирована для {user_id}.")
    else:
        await message.answer("🔶 У пользователя нет активной блокировки по карте РФ.")

    await state.clear()
    await message.answer("🛠 Панель администратора:", reply_markup=kb.get_admin_keyboard())


@router.callback_query(F.data == "admin_btc_wallet_set")
async def admin_btc_wallet_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_btc_wallet)
    await callback.message.edit_text(
        "Добавьте BTC-кошелёк одним сообщением (можно несколько — будут выдаваться случайно):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_crypto_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == "admin_ltc_wallet_set")
async def admin_ltc_wallet_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_ltc_wallet)
    await callback.message.edit_text(
        "Добавьте LTC-кошелёк одним сообщением (можно несколько — будут выдаваться случайно):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_crypto_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == "admin_crypto_refresh")
async def admin_crypto_refresh(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    ok = await CryptoService.refresh_rates(force=True)
    await _show_crypto_menu(callback, state)
    await callback.answer("Курс обновлён" if ok else "CoinGecko не ответил, оставлен прошлый курс")


@router.message(AdminState.waiting_for_btc_wallet)
async def admin_btc_wallet_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    wallet = (message.text or "").strip()
    if len(wallet) < 20:
        await message.answer("🔶 Слишком короткий адрес. Пришлите BTC-кошелёк целиком.")
        return
    SettingsService.add_btc_wallet(wallet)
    await message.answer("✅ BTC-кошелёк добавлен в список.")
    await state.set_state(None)
    await _show_crypto_menu(message, state)


@router.callback_query(F.data.startswith("admin_btc_wallet_del_"))
async def admin_btc_wallet_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        index = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return
    if SettingsService.remove_btc_wallet_at(index):
        await callback.answer("BTC-кошелёк удалён")
    else:
        await callback.answer("Не найден", show_alert=True)
    await _show_crypto_menu(callback, state)


@router.callback_query(F.data.startswith("admin_ltc_wallet_del_"))
async def admin_ltc_wallet_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        index = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return
    if SettingsService.remove_ltc_wallet_at(index):
        await callback.answer("LTC-кошелёк удалён")
    else:
        await callback.answer("Не найден", show_alert=True)
    await _show_crypto_menu(callback, state)


@router.callback_query(F.data.startswith("admin_btc_wallet_noop_"))
@router.callback_query(F.data.startswith("admin_ltc_wallet_noop_"))
async def admin_crypto_wallet_noop(callback: CallbackQuery):
    await callback.answer()


@router.message(AdminState.waiting_for_ltc_wallet)
async def admin_ltc_wallet_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    wallet = (message.text or "").strip()
    if len(wallet) < 20:
        await message.answer("🔶 Слишком короткий адрес. Пришлите LTC-кошелёк целиком.")
        return
    SettingsService.add_ltc_wallet(wallet)
    await message.answer("✅ LTC-кошелёк добавлен в список.")
    await state.set_state(None)
    await _show_crypto_menu(message, state)


# --- Управление комиссией ручной оплаты ---

async def _show_commission_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    from app.services.settings import SettingsService
    commission = SettingsService.get_manual_commission()
    text = (
        "💸 <b>Комиссия при ручном пополнении</b>\n\n"
        f"Текущая комиссия: <b>{commission} руб.</b>\n\n"
        "Эта сумма добавляется к цене товара при оплате через оператора (Карта / СБП).\n"
        "Установите 0, чтобы убрать комиссию."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_commission_set")],
        [InlineKeyboardButton(text="0️⃣ Обнулить", callback_data="admin_commission_zero")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_rf_commission_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    commission = SettingsService.get_rf_card_commission()
    text = (
        "💸 <b>Комиссия для оплаты картой РФ</b>\n\n"
        f"Текущая комиссия: <b>{commission} руб.</b>\n\n"
        "Эта сумма добавляется к цене товара при оплате на карту РФ.\n"
        "Установите 0, чтобы убрать комиссию."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_rf_commission_set")],
        [InlineKeyboardButton(text="0️⃣ Обнулить", callback_data="admin_rf_commission_zero")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_commission_menu')
async def admin_commission_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_commission_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_commission_menu')
async def admin_rf_commission_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_rf_commission_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_commission_set')
async def admin_commission_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_manual_commission)
    await callback.message.edit_text(
        "Введите размер комиссии в рублях (целое число, например 600 или 0):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_commission_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_commission_set')
async def admin_rf_commission_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_rf_commission)
    await callback.message.edit_text(
        "Введите размер комиссии для карты РФ в рублях (целое число, например 300 или 0):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_rf_commission_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_commission_zero')
async def admin_commission_zero(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    from app.services.settings import SettingsService
    SettingsService.set_manual_commission(0)
    await _show_commission_menu(callback, state)
    await callback.answer("Комиссия обнулена")


@router.callback_query(F.data == 'admin_rf_commission_zero')
async def admin_rf_commission_zero(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_rf_card_commission(0)
    await _show_rf_commission_menu(callback, state)
    await callback.answer("Комиссия обнулена")


@router.message(AdminState.waiting_for_manual_commission)
async def admin_commission_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("🔶 Введите целое число, например 600 или 0.")
        return
    if value < 0:
        await message.answer("🔶 Комиссия не может быть отрицательной.")
        return
    from app.services.settings import SettingsService
    SettingsService.set_manual_commission(value)
    await message.answer(f"✅ Комиссия сохранена: {value} руб.")
    await state.set_state(None)
    await _show_commission_menu(message, state)


@router.message(AdminState.waiting_for_rf_commission)
async def admin_rf_commission_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("🔶 Введите целое число, например 300 или 0.")
        return
    if value < 0:
        await message.answer("🔶 Комиссия не может быть отрицательной.")
        return
    SettingsService.set_rf_card_commission(value)
    await message.answer(f"✅ Комиссия карты РФ сохранена: {value} руб.")
    await state.set_state(None)
    await _show_rf_commission_menu(message, state)


async def _show_ym_commission_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    commission = SettingsService.get_yoomoney_commission()
    text = (
        "💜 <b>Комиссия ЮMoney</b>\n\n"
        f"Текущая комиссия: <b>{commission} руб.</b>\n\n"
        "Эта сумма прибавляется к цене товара при оплате ЮMoney.\n"
        "Установите 0, чтобы убрать комиссию."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_ym_commission_set")],
        [InlineKeyboardButton(text="0️⃣ Обнулить", callback_data="admin_ym_commission_zero")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ])
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_ym_commission_menu')
async def admin_ym_commission_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await _show_ym_commission_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_ym_commission_set')
async def admin_ym_commission_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_yoomoney_commission)
    await callback.message.edit_text(
        "Введите размер комиссии для ЮMoney в рублях (целое число, например 300 или 0):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_ym_commission_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_ym_commission_zero')
async def admin_ym_commission_zero(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    SettingsService.set_yoomoney_commission(0)
    await _show_ym_commission_menu(callback, state)
    await callback.answer("Комиссия ЮMoney обнулена")


@router.message(AdminState.waiting_for_yoomoney_commission)
async def admin_ym_commission_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("🔶 Введите целое число, например 300 или 0.")
        return
    if value < 0:
        await message.answer("🔶 Комиссия не может быть отрицательной.")
        return
    SettingsService.set_yoomoney_commission(value)
    await message.answer(f"✅ Комиссия ЮMoney сохранена: {value} руб.")
    await state.set_state(None)
    await _show_ym_commission_menu(message, state)


# --- Кастомные названия способов оплаты ---

async def _show_payment_aliases_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    aliases = SettingsService.get_payment_method_aliases()
    fallbacks = SettingsService.get_payment_fallback_methods()
    
    text = "🏷 <b>Названия способов оплаты</b>\n\n"
    
    if aliases:
        text += "<b>Переименования:</b>\n"
        for orig, custom in aliases.items():
            text += f"• {orig} → <b>{custom}</b>\n"
    else:
        text += "<i>Нет переименований</i>\n"
    
    text += "\n"
    
    if fallbacks:
        text += "<b>Альтернативы (при занятости):</b>\n"
        for method, alts in fallbacks.items():
            text += f"• {method} → {', '.join(alts)}\n"
    else:
        text += "<i>Нет альтернатив</i>\n"
    
    text += "\n💡 <b>Как использовать:</b>\n"
    text += "1. Добавьте переименование, чтобы изменить отображаемое название способа\n"
    text += "2. Добавьте альтернативы, чтобы при занятости реквизитов предлагались другие способы"
    
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить переименование", callback_data="admin_pa_add_alias")],
        [InlineKeyboardButton(text="➕ Добавить альтернативу", callback_data="admin_pa_add_fallback")],
        [InlineKeyboardButton(text="🗑 Удалить переименование", callback_data="admin_pa_del_alias")],
        [InlineKeyboardButton(text="🗑 Удалить альтернативу", callback_data="admin_pa_del_fallback")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_payment_aliases_menu')
async def admin_payment_aliases_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await _show_payment_aliases_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_pa_add_alias')
async def admin_pa_add_alias(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_payment_alias_original)
    await callback.message.edit_text(
        "✏️ <b>Добавить переименование</b>\n\n"
        "Введите <b>оригинальное название</b> способа оплаты (как оно приходит из Ratecore).\n\n"
        "Например: <code>Карти - чек пдф</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_payment_aliases_menu")]
        ])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_payment_alias_original)
async def admin_pa_alias_original(message: Message, state: FSMContext):
    original = (message.text or "").strip()
    if not original:
        await message.answer("🔶 Введите название способа.")
        return
    await state.update_data(pa_original=original)
    await state.set_state(AdminState.waiting_for_payment_alias_custom)
    await message.answer(
        f"Оригинал: <b>{original}</b>\n\n"
        "Теперь введите <b>новое название</b>, которое будет показано пользователю.\n\n"
        "Например: <code>Оплата по карте - 2 обменка</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_payment_aliases_menu")]
        ])
    )


@router.message(AdminState.waiting_for_payment_alias_custom)
async def admin_pa_alias_custom(message: Message, state: FSMContext):
    custom = (message.text or "").strip()
    if not custom:
        await message.answer("🔶 Введите новое название.")
        return
    data = await state.get_data()
    original = data.get("pa_original", "")
    SettingsService.set_payment_method_alias(original, custom)
    await message.answer(f"✅ Сохранено:\n{original} → <b>{custom}</b>")
    await state.set_state(None)
    await _show_payment_aliases_menu(message, state)


@router.callback_query(F.data == 'admin_pa_del_alias')
async def admin_pa_del_alias(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    aliases = SettingsService.get_payment_method_aliases()
    if not aliases:
        await callback.answer("Нет переименований для удаления", show_alert=True)
        return
    buttons = []
    for orig in aliases:
        buttons.append([InlineKeyboardButton(text=f"❗ {orig}", callback_data=f"admin_pa_del_a_{orig[:50]}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="admin_payment_aliases_menu")])
    await callback.message.edit_text(
        "🗑 Выберите переименование для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_pa_del_a_'))
async def admin_pa_del_alias_do(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    original = callback.data.replace('admin_pa_del_a_', '')
    SettingsService.remove_payment_method_alias(original)
    await callback.answer(f"Удалено: {original}")
    await _show_payment_aliases_menu(callback, state)


@router.callback_query(F.data == 'admin_pa_add_fallback')
async def admin_pa_add_fallback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_payment_fallback_method)
    await callback.message.edit_text(
        "🔄 <b>Добавить альтернативу</b>\n\n"
        "Введите <b>ключ способа оплаты</b> (method key из Ratecore), для которого нужны альтернативы.\n\n"
        "Например: <code>card_pdf_check</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_payment_aliases_menu")]
        ])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_payment_fallback_method)
async def admin_pa_fallback_method(message: Message, state: FSMContext):
    method = (message.text or "").strip()
    if not method:
        await message.answer("🔶 Введите ключ метода.")
        return
    await state.update_data(pa_fallback_method=method)
    await state.set_state(AdminState.waiting_for_payment_fallback_alts)
    await message.answer(
        f"Метод: <b>{method}</b>\n\n"
        "Введите <b>ключи альтернативных методов</b> через запятую.\n\n"
        "Например: <code>sbp_pdf_check, card_pdf_check_2</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_payment_aliases_menu")]
        ])
    )


@router.message(AdminState.waiting_for_payment_fallback_alts)
async def admin_pa_fallback_alts(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("🔶 Введите ключи альтернатив.")
        return
    data = await state.get_data()
    method = data.get("pa_fallback_method", "")
    alts = [a.strip() for a in raw.split(",") if a.strip()]
    SettingsService.set_payment_fallback_methods(method, alts)
    await message.answer(f"✅ Сохранено:\n{method} → {', '.join(alts)}")
    await state.set_state(None)
    await _show_payment_aliases_menu(message, state)


@router.callback_query(F.data == 'admin_pa_del_fallback')
async def admin_pa_del_fallback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    fallbacks = SettingsService.get_payment_fallback_methods()
    if not fallbacks:
        await callback.answer("Нет альтернатив для удаления", show_alert=True)
        return
    buttons = []
    for method in fallbacks:
        buttons.append([InlineKeyboardButton(text=f"❗ {method}", callback_data=f"admin_pa_del_f_{method[:50]}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="admin_payment_aliases_menu")])
    await callback.message.edit_text(
        "🗑 Выберите метод для удаления альтернатив:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_pa_del_f_'))
async def admin_pa_del_fallback_do(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    method = callback.data.replace('admin_pa_del_f_', '')
    SettingsService.set_payment_fallback_methods(method, [])
    await callback.answer(f"Удалено: {method}")
    await _show_payment_aliases_menu(callback, state)


# --- Управление контактом поддержки ---

async def _show_contacts_text_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    text_value = SettingsService.get_contacts_text()
    text = (
        "📇 <b>Кнопка Контакты</b>\n\n"
        "Текущий текст, который увидит пользователь при нажатии на кнопку <b>Контакты</b>:\n\n"
        f"{text_value}\n\n"
        "Можно использовать HTML-разметку и ссылки."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_contacts_text_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)

async def _show_support_contact_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    from app.services.settings import SettingsService
    contact = SettingsService.get_support_contact()
    text = (
        "📞 <b>Контакт поддержки</b>\n\n"
        f"Текущий контакт: <b>{contact}</b>\n\n"
        "Это значение подставляется во все сообщения бота, где упоминается поддержка.\n"
        "Например: правила магазина, инструкция, стартовое сообщение."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_support_contact_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_exchange_bot_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    from app.services.settings import SettingsService
    exchange_bot_url = SettingsService.get_exchange_bot_url() or "-"
    text = (
        "🔗 <b>Ссылка на бота авто-оплаты</b>\n\n"
        f"Текущая ссылка: <b>{exchange_bot_url}</b>\n\n"
        "Эта ссылка используется в оплате \"Карта / СБП (самостоятельно)\"."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_exchange_bot_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_support_contact_menu')
async def admin_support_contact_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_support_contact_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_contacts_text_menu')
async def admin_contacts_text_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_contacts_text_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_exchange_bot_menu')
async def admin_exchange_bot_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_exchange_bot_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_support_contact_set')
async def admin_support_contact_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_support_contact)
    await callback.message.edit_text(
        "Введите новый контакт поддержки (например @username или ссылку):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_support_contact_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_contacts_text_set')
async def admin_contacts_text_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_contacts_text)
    await callback.message.edit_text(
        "Введите текст для кнопки Контакты. Можно использовать HTML и ссылки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_contacts_text_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_exchange_bot_set')
async def admin_exchange_bot_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_exchange_bot_url)
    await callback.message.edit_text(
        "Введите ссылку на бота авто-оплаты (например https://t.me/your_bot):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_exchange_bot_menu")]])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_support_contact)
async def admin_support_contact_save(message: Message, state: FSMContext):
    contact = (message.text or "").strip()
    if not contact:
        await message.answer("🔶 Контакт не может быть пустым.")
        return
    from app.services.settings import SettingsService
    SettingsService.set_support_contact(contact)
    await message.answer(f"✅ Контакт поддержки сохранён: {contact}")
    await state.set_state(None)
    await _show_support_contact_menu(message, state)


def _featured_item_id_from_data(data: dict | None) -> str:
    return str((data or {}).get("featured_item_id") or "").strip()


async def _show_featured_product_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    catalog = SettingsService.get_featured_catalog()
    items = catalog.get("items") or []
    visible = "да" if SettingsService.is_price_menu_visible(catalog) else "нет"
    lines = [
        "📦 <b>Прайс в меню</b>\n",
        "В главном меню кнопка <b>Прайс</b>. Внутри - позиции, которые вы добавите.\n",
        f"В меню: <b>{visible}</b>",
        f"Позиций: <b>{len(items)}</b>\n",
    ]
    if items:
        for item in items:
            photo = " · фото" if item.get("photo_file_id") else ""
            lines.append(f"• {html.escape(item.get('title') or 'Товар')}{photo}")
    else:
        lines.append("Позиций пока нет.")
    text = "\n".join(lines)
    toggle_text = "🔒 Скрыть Прайс" if catalog.get("enabled", True) else "✅ Показать Прайс"
    buttons = [[InlineKeyboardButton(text="➕ Добавить позицию", callback_data="admin_fp_add")]]
    for item in items:
        title = str(item.get("title") or "Товар")
        buttons.append(
            [InlineKeyboardButton(text=f"✏️ {title[:40]}", callback_data=f"admin_fp_open_{item.get('id')}")]
        )
    buttons.append([InlineKeyboardButton(text=toggle_text, callback_data="admin_featured_toggle")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.update_data(featured_item_id=None)
        await state.set_state(None)


async def _show_featured_item_menu(target: CallbackQuery | Message, item_id: str, state: FSMContext | None = None):
    item = SettingsService.get_featured_item(item_id)
    if not item:
        await _show_featured_product_menu(target, state)
        return
    photo_label = "есть" if item.get("photo_file_id") else "нет"
    description = html.escape(item.get("description") or "-")
    if len(description) > 800:
        description = description[:800] + "…"
    text = (
        "📦 <b>Позиция прайса</b>\n\n"
        f"Название: <b>{html.escape(item.get('title') or '-')}</b>\n"
        f"Фото: <b>{photo_label}</b>\n\n"
        f"Описание:\n{description}"
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Название", callback_data="admin_featured_title_set")],
        [InlineKeyboardButton(text="✏️ Описание", callback_data="admin_featured_desc_set")],
        [InlineKeyboardButton(text="🖼 Фото", callback_data="admin_featured_photo_set")],
        [InlineKeyboardButton(text="🗑 Убрать фото", callback_data="admin_featured_photo_clear")],
        [InlineKeyboardButton(text="❗ Удалить позицию", callback_data="admin_fp_delete")],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="admin_featured_product_menu")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if state:
        await state.update_data(featured_item_id=item_id)
        await state.set_state(None)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_featured_product_menu")
async def admin_featured_product_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_featured_product_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_fp_add")
async def admin_fp_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.update_data(featured_item_id=None)
    await state.set_state(AdminState.waiting_for_featured_title)
    await callback.message.edit_text(
        "Введите название позиции. Оно будет кнопкой внутри <b>Прайс</b>:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_featured_product_menu")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_fp_open_"))
async def admin_fp_open(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    item_id = (callback.data or "")[len("admin_fp_open_"):]
    await _show_featured_item_menu(callback, item_id, state)
    await callback.answer()


@router.callback_query(F.data == "admin_featured_title_set")
async def admin_featured_title_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    data = await state.get_data()
    if not _featured_item_id_from_data(data):
        await callback.answer("Сначала откройте позицию", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_featured_title)
    await callback.message.edit_text(
        "Введите новое название позиции:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_featured_product_menu")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_featured_desc_set")
async def admin_featured_desc_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    data = await state.get_data()
    if not _featured_item_id_from_data(data):
        await callback.answer("Сначала откройте позицию", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_featured_description)
    await callback.message.edit_text(
        "Введите описание позиции обычным текстом. Длинный текст можно:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_featured_product_menu")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_featured_photo_set")
async def admin_featured_photo_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    data = await state.get_data()
    if not _featured_item_id_from_data(data):
        await callback.answer("Сначала откройте позицию", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_featured_photo)
    await callback.message.edit_text(
        "Пришлите фото позиции одним сообщением.\n"
        "Фото необязательно - можно нажать Отмена.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_featured_product_menu")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_featured_photo_clear")
async def admin_featured_photo_clear(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    data = await state.get_data()
    item_id = _featured_item_id_from_data(data)
    if item_id:
        SettingsService.update_featured_item(item_id, photo_file_id=None)
        await _show_featured_item_menu(callback, item_id, state)
    else:
        await _show_featured_product_menu(callback, state)
    await callback.answer("Фото убрано")


@router.callback_query(F.data == "admin_fp_delete")
async def admin_fp_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    data = await state.get_data()
    item_id = _featured_item_id_from_data(data)
    if item_id:
        SettingsService.delete_featured_item(item_id)
    await _show_featured_product_menu(callback, state)
    await callback.answer("Удалено")


@router.callback_query(F.data == "admin_featured_toggle")
async def admin_featured_toggle(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    catalog = SettingsService.get_featured_catalog()
    SettingsService.save_featured_catalog(enabled=not catalog.get("enabled", True))
    await _show_featured_product_menu(callback, state)
    await callback.answer()


@router.message(AdminState.waiting_for_featured_title)
async def admin_featured_title_save(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("🔶 Название не может быть пустым.")
        return
    data = await state.get_data()
    item_id = _featured_item_id_from_data(data)
    if item_id:
        SettingsService.update_featured_item(item_id, title=title)
        await message.answer("✅ Название сохранено.")
        await _show_featured_item_menu(message, item_id, state)
        return
    item = SettingsService.add_featured_item(title)
    await message.answer("✅ Позиция создана. Теперь можно добавить описание и фото.")
    await _show_featured_item_menu(message, item["id"], state)


@router.message(AdminState.waiting_for_featured_description)
async def admin_featured_desc_save(message: Message, state: FSMContext):
    text_value = (message.text or message.caption or "").strip()
    if not text_value:
        await message.answer("🔶 Описание не может быть пустым.")
        return
    data = await state.get_data()
    item_id = _featured_item_id_from_data(data)
    if not item_id:
        await message.answer("🔶 Сначала откройте позицию.")
        return
    SettingsService.update_featured_item(item_id, description=text_value)
    await message.answer("✅ Описание сохранено.")
    await _show_featured_item_menu(message, item_id, state)


@router.message(AdminState.waiting_for_featured_photo)
async def admin_featured_photo_save(message: Message, state: FSMContext):
    photo = None
    if message.photo:
        photo = message.photo[-1].file_id
    elif message.document and str(message.document.mime_type or "").startswith("image/"):
        photo = message.document.file_id
    if not photo:
        await message.answer("🔶 Пришлите фото картинкой.")
        return
    data = await state.get_data()
    item_id = _featured_item_id_from_data(data)
    if not item_id:
        await message.answer("🔶 Сначала откройте позицию.")
        return
    SettingsService.update_featured_item(item_id, photo_file_id=photo)
    await message.answer("✅ Фото сохранено.")
    await _show_featured_item_menu(message, item_id, state)


@router.message(AdminState.waiting_for_contacts_text)
async def admin_contacts_text_save(message: Message, state: FSMContext):
    text_value = (message.html_text or message.text or "").strip()
    if not text_value:
        await message.answer("🔶 Текст контактов не может быть пустым.")
        return
    SettingsService.set_contacts_text(text_value)
    await message.answer("✅ Текст кнопки Контакты сохранён.")
    await state.set_state(None)
    await _show_contacts_text_menu(message, state)


def _home_screen_markup(links: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✏️ Текст сверху", callback_data="admin_home_text_set")],
        [InlineKeyboardButton(text="➕ Ссылка", callback_data="admin_home_link_add")],
    ]
    for item in links:
        buttons.append([
            InlineKeyboardButton(text=f"🔗 {item['title']}", callback_data=f"admin_hl:{item['id']}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _home_screen_body(links: list[dict]) -> str:
    preview = SettingsService.get_home_screen_text()
    if len(preview) > 1200:
        preview = preview[:1200] + "…"
    if links:
        listed = "\n".join(f"• {html.escape(item['title'])} - {html.escape(item['url'])}" for item in links)
    else:
        listed = "Пока пусто. Добавьте кнопку: имя и ссылка."
    return (
        "💎 <b>Экран долины</b>\n\n"
        "Этот текст клиент видит над кнопками. Можно любой, с HTML.\n\n"
        f"{preview}\n\n"
        "<b>Ссылки под кнопкой «В магазин»</b>\n"
        f"{listed}"
    )


async def _show_home_screen_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    links = SettingsService.get_home_links()
    text = _home_screen_body(links)
    markup = _home_screen_markup(links)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
    if state:
        await state.set_state(None)


async def _show_home_link_card(target: CallbackQuery | Message, link_id: str, state: FSMContext | None = None):
    item = next((link for link in SettingsService.get_home_links() if link["id"] == link_id), None)
    if not item:
        await _show_home_screen_menu(target, state)
        return
    text = (
        "🔗 <b>Кнопка-ссылка</b>\n\n"
        f"Имя: <b>{html.escape(item['title'])}</b>\n"
        f"Ссылка: {html.escape(item['url'])}"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Имя", callback_data=f"admin_hl_name:{link_id}")],
        [InlineKeyboardButton(text="✏️ Ссылка", callback_data=f"admin_hl_url:{link_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_hl_del:{link_id}")],
        [InlineKeyboardButton(text="🔙 К экрану", callback_data="admin_home_screen")],
    ])
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
    if state:
        await state.update_data(home_link_id=link_id)
        await state.set_state(None)


@router.callback_query(F.data == "admin_home_screen")
async def admin_home_screen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_home_screen_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_home_text_set")
async def admin_home_text_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_home_screen_text)
    await callback.message.edit_text(
        "Пришлите текст, который будет сверху у клиента.\n"
        "Можно с переносами строк и HTML: &lt;b&gt;жирный&lt;/b&gt;.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_home_screen")]
        ]),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_home_screen_text)
async def admin_home_text_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text_value = (message.html_text or message.text or "").strip()
    if not text_value:
        await message.answer("Текст пустой. Пришлите сообщение ещё раз.")
        return
    SettingsService.set_home_screen_text(text_value)
    await message.answer("Текст экрана сохранён.")
    await _show_home_screen_menu(message, state)


@router.callback_query(F.data == "admin_home_link_add")
async def admin_home_link_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_home_link_title)
    await callback.message.edit_text(
        "Имя кнопки. Так её увидит клиент. Например: Оператор или Сайт.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_home_screen")]
        ]),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_home_link_title)
async def admin_home_link_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    title = " ".join((message.text or "").split())
    if not title:
        await message.answer("Имя пустое. Напишите, как подписать кнопку.")
        return
    await state.update_data(home_link_title=title[:60])
    await state.set_state(AdminState.waiting_for_home_link_url)
    await message.answer(
        f"Ссылка для «{html.escape(title[:60])}».\n"
        "Подойдёт https://..., t.me/... или @username.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_home_screen")]
        ]),
    )


@router.message(AdminState.waiting_for_home_link_url)
async def admin_home_link_url(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    ok, note = SettingsService.add_home_link(data.get("home_link_title") or "", message.text or "")
    if not ok:
        await message.answer(note)
        return
    await message.answer(note)
    await _show_home_screen_menu(message, state)


@router.callback_query(F.data.startswith("admin_hl:"))
async def admin_home_link_open(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_home_link_card(callback, callback.data.split(":", 1)[1], state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_hl_name:"))
async def admin_home_link_rename(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    link_id = callback.data.split(":", 1)[1]
    await state.update_data(home_link_id=link_id)
    await state.set_state(AdminState.waiting_for_home_link_rename)
    await callback.message.edit_text(
        "Новое имя кнопки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=f"admin_hl:{link_id}")]
        ]),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_home_link_rename)
async def admin_home_link_rename_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    link_id = str(data.get("home_link_id") or "")
    ok, note = SettingsService.update_home_link(link_id, title=message.text or "")
    if not ok:
        await message.answer(note)
        return
    await message.answer(note)
    await _show_home_link_card(message, link_id, state)


@router.callback_query(F.data.startswith("admin_hl_url:"))
async def admin_home_link_reurl(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    link_id = callback.data.split(":", 1)[1]
    await state.update_data(home_link_id=link_id)
    await state.set_state(AdminState.waiting_for_home_link_reurl)
    await callback.message.edit_text(
        "Новая ссылка: https://..., t.me/... или @username.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=f"admin_hl:{link_id}")]
        ]),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_home_link_reurl)
async def admin_home_link_reurl_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    link_id = str(data.get("home_link_id") or "")
    ok, note = SettingsService.update_home_link(link_id, url=message.text or "")
    if not ok:
        await message.answer(note)
        return
    await message.answer(note)
    await _show_home_link_card(message, link_id, state)


@router.callback_query(F.data.startswith("admin_hl_del:"))
async def admin_home_link_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.delete_home_link(callback.data.split(":", 1)[1])
    await callback.answer("Кнопка удалена")
    await _show_home_screen_menu(callback, state)


async def _show_preorder_text_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    text_value = SettingsService.get_preorder_text()
    text = (
        "🛒 <b>Текст предзаказа</b>\n\n"
        "Показывается клиенту вместе с кнопкой <b>Чат с админом</b> "
        "при нажатии «Оформить предзаказ» в каталоге.\n\n"
        "Текущий текст:\n\n"
        f"{text_value}\n\n"
        "Можно использовать HTML-разметку и ссылки."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_preorder_text_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_preorder_text_menu')
async def admin_preorder_text_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_preorder_text_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_preorder_text_set')
async def admin_preorder_text_set(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_preorder_text)
    await callback.message.edit_text(
        "Введите текст, который увидит клиент при нажатии «Оформить предзаказ».\n"
        "Можно использовать HTML и ссылки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin_preorder_text_menu")],
        ]),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_preorder_text)
async def admin_preorder_text_save(message: Message, state: FSMContext):
    text_value = (message.html_text or message.text or "").strip()
    if not text_value:
        await message.answer("🔶 Текст предзаказа не может быть пустым.")
        return
    SettingsService.set_preorder_text(text_value)
    await message.answer("✅ Текст предзаказа сохранён.")
    await state.set_state(None)
    await _show_preorder_text_menu(message, state)


def _mission_kind_label(kind: str) -> str:
    return "приглашения" if kind == "referrals" else "покупки"


def _missions_cancel_markup(callback_data: str = "adm_ms") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=callback_data)]]
    )


async def _show_admin_missions(target: CallbackQuery | Message, state: FSMContext | None = None):
    cfg = SettingsService.get_weekly_missions()
    streak = SettingsService.get_purchase_streak()
    total = LoyaltyService.claim_total(cfg)
    status = "включены" if cfg.get("enabled") else "выключены"
    st_status = "вкл" if streak.get("enabled") else "выкл"
    streak_percent = int(streak.get("percent") or 0)
    streak_bonus = (
        "бонус бесплатно"
        if streak_percent >= 100
        else f"−{streak_percent}% на бонус"
    )
    lines = [
        "🎯 <b>Миссии недели</b>",
        "",
        f"Статус: <b>{status}</b>",
        f"Награда за набор: <b>{cfg.get('reward', 0)} ₽</b>",
        f"Итого к выплате: <b>{total} ₽</b> (набор + награды заданий)",
        f"🎁 Акция: <b>{st_status}</b> · {streak.get('required')}+1 ({streak_bonus})",
        "",
        "Задания:",
    ]
    tasks = cfg.get("tasks") or []
    if not tasks:
        lines.append("Пока нет заданий.")
    for idx, task in enumerate(tasks, start=1):
        mark = "✅" if task.get("enabled") else "❗"
        lines.append(
            f"{idx}. {mark} {task.get('title')}\n"
            f"    {_mission_kind_label(task.get('kind'))} ≥ {task.get('count')} · {task.get('reward', 0)} ₽"
        )
    buttons = [
        [InlineKeyboardButton(
            text="🟢 Выключить миссии" if cfg.get("enabled") else "⚪️ Включить миссии",
            callback_data="adm_ms:tg",
        )],
        [InlineKeyboardButton(text="💰 Награда за набор", callback_data="adm_ms:rw")],
        [InlineKeyboardButton(text="🎁 Акция 5+1", callback_data="adm_ms:st")],
    ]
    for task in tasks:
        mark = "✅" if task.get("enabled") else "❗"
        title = str(task.get("title") or "Задание")[:28]
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {title}",
            callback_data=f"adm_ms:t:{task.get('id')}",
        )])
    if len(tasks) < 12:
        buttons.append([InlineKeyboardButton(text="➕ Добавить задание", callback_data="adm_ms:add")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_admin_mission_task(target: CallbackQuery | Message, task_id: str, state: FSMContext | None = None):
    task = SettingsService.get_mission_task(task_id)
    if not task:
        await _show_admin_missions(target, state)
        return
    mark = "включено" if task.get("enabled") else "выключено"
    text = (
        f"🎯 <b>{task.get('title')}</b>\n\n"
        f"Тип: <b>{_mission_kind_label(task.get('kind'))}</b>\n"
        f"Цель: <b>{task.get('count')}</b>\n"
        f"Награда задания: <b>{task.get('reward', 0)} ₽</b>\n"
        f"Статус: <b>{mark}</b>"
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"adm_ms:tt:{task_id}")],
        [InlineKeyboardButton(text="🎯 Цель", callback_data=f"adm_ms:tc:{task_id}")],
        [InlineKeyboardButton(text="💰 Награда задания", callback_data=f"adm_ms:tr:{task_id}")],
        [InlineKeyboardButton(
            text="❗ Выключить" if task.get("enabled") else "✅ Включить",
            callback_data=f"adm_ms:td:{task_id}",
        )],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_ms:tx:{task_id}")],
        [InlineKeyboardButton(text="🔙 К миссиям", callback_data="adm_ms")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_admin_streak(target: CallbackQuery | Message, state: FSMContext | None = None):
    streak = SettingsService.get_purchase_streak()
    status = "включена" if streak.get("enabled") else "выключена"
    required = int(streak.get("required") or 5)
    percent = int(streak.get("percent") or 100)
    bonus_no = required + 1
    bonus_line = (
        f"Бонус: <b>{bonus_no}-я покупка бесплатно</b>"
        if percent >= 100
        else f"Бонус на {bonus_no}-ю: <b>−{percent}%</b>"
    )
    text = (
        f"🎁 <b>Акция {required}+1</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Платных покупок до бонуса: <b>{required}</b>\n"
        f"{bonus_line}\n\n"
        "Счётчик сбрасывается после использования бонуса."
    )
    buttons = [
        [InlineKeyboardButton(
            text="🟢 Выключить" if streak.get("enabled") else "⚪️ Включить",
            callback_data="adm_ms:stg",
        )],
        [InlineKeyboardButton(text="🔢 Число покупок", callback_data="adm_ms:stn")],
        [InlineKeyboardButton(text="📉 Процент скидки", callback_data="adm_ms:stp")],
        [InlineKeyboardButton(text="🔙 К миссиям", callback_data="adm_ms")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == "adm_ms")
async def admin_missions_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_admin_missions(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ms:"))
async def admin_missions_actions(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = (callback.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if action == "tg":
        current = SettingsService.get_weekly_missions()
        SettingsService.update_weekly_missions(enabled=not current.get("enabled"))
        await _show_admin_missions(callback, state)
        await callback.answer()
        return
    if action == "rw":
        await state.set_state(AdminState.waiting_for_mission_reward)
        await state.update_data(mission_reward_target="pack", mission_task_id=None)
        await callback.message.edit_text(
            "Введите награду за выполнение всех заданий, в рублях (например 1000):",
            reply_markup=_missions_cancel_markup(),
        )
        await callback.answer()
        return
    if action == "add":
        await callback.message.edit_text(
            "Какой тип задания добавить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Покупки", callback_data="adm_ms:ak:purchases")],
                [InlineKeyboardButton(text="👥 Приглашения", callback_data="adm_ms:ak:referrals")],
                [InlineKeyboardButton(text="Отмена", callback_data="adm_ms")],
            ]),
        )
        await callback.answer()
        return
    if action == "ak":
        kind = arg if arg in {"purchases", "referrals"} else "purchases"
        await state.set_state(AdminState.waiting_for_mission_task_title)
        await state.update_data(mission_task_id=None, mission_new_kind=kind, mission_new_task={})
        await callback.message.edit_text(
            "Введите название задания (например «Совершить покупку»):",
            reply_markup=_missions_cancel_markup(),
        )
        await callback.answer()
        return
    if action == "st":
        await _show_admin_streak(callback, state)
        await callback.answer()
        return
    if action == "stg":
        current = SettingsService.get_purchase_streak()
        SettingsService.update_purchase_streak(enabled=not current.get("enabled"))
        await _show_admin_streak(callback, state)
        await callback.answer()
        return
    if action == "stn":
        await state.set_state(AdminState.waiting_for_streak_required)
        await callback.message.edit_text(
            "Сколько платных покупок до бонуса? Число 2–20 (например 5 → шестая бонусом):",
            reply_markup=_missions_cancel_markup("adm_ms:st"),
        )
        await callback.answer()
        return
    if action == "stp":
        await state.set_state(AdminState.waiting_for_streak_percent)
        await callback.message.edit_text(
            "Скидка на бонусную покупку, 1–100 (100 = бесплатно):",
            reply_markup=_missions_cancel_markup("adm_ms:st"),
        )
        await callback.answer()
        return

    if not arg:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    task = SettingsService.get_mission_task(arg)
    if action != "t" and not task:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    if action == "t":
        await _show_admin_mission_task(callback, arg, state)
        await callback.answer()
        return
    if action == "td":
        task["enabled"] = not task.get("enabled", True)
        SettingsService.upsert_mission_task(task)
        await _show_admin_mission_task(callback, arg, state)
        await callback.answer()
        return
    if action == "tx":
        SettingsService.delete_mission_task(arg)
        await _show_admin_missions(callback, state)
        await callback.answer("Удалено")
        return
    if action == "tt":
        await state.set_state(AdminState.waiting_for_mission_task_title)
        await state.update_data(mission_task_id=arg, mission_new_kind=None)
        await callback.message.edit_text(
            "Новое название задания:",
            reply_markup=_missions_cancel_markup(f"adm_ms:t:{arg}"),
        )
        await callback.answer()
        return
    if action == "tc":
        await state.set_state(AdminState.waiting_for_mission_task_count)
        await state.update_data(mission_task_id=arg)
        await callback.message.edit_text(
            "Сколько раз нужно выполнить? Число от 1 до 50:",
            reply_markup=_missions_cancel_markup(f"adm_ms:t:{arg}"),
        )
        await callback.answer()
        return
    if action == "tr":
        await state.set_state(AdminState.waiting_for_mission_task_reward)
        await state.update_data(mission_task_id=arg, mission_reward_target="task")
        await callback.message.edit_text(
            "Награда именно за это задание, в рублях (0 - без отдельной выплаты):",
            reply_markup=_missions_cancel_markup(f"adm_ms:t:{arg}"),
        )
        await callback.answer()
        return
    await callback.answer()


@router.message(AdminState.waiting_for_mission_reward)
async def admin_mission_reward_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace(" ", "").replace("₽", "").strip()
    try:
        amount = int(float(raw.replace(",", ".")))
    except ValueError:
        await message.answer("🔶 Введите целое число, например 1000.")
        return
    if amount < 0 or amount > 1_000_000:
        await message.answer("🔶 Сумма от 0 до 1 000 000.")
        return
    SettingsService.update_weekly_missions(reward=amount)
    await message.answer(f"✅ Награда за набор: {amount} ₽")
    await state.set_state(None)
    await _show_admin_missions(message, state)


@router.message(AdminState.waiting_for_mission_task_title)
async def admin_mission_task_title_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("🔶 Название не может быть пустым.")
        return
    data = await state.get_data()
    task_id = data.get("mission_task_id")
    if task_id:
        task = SettingsService.get_mission_task(task_id)
        if not task:
            await state.set_state(None)
            await message.answer("🔶 Задание не найдено.")
            await _show_admin_missions(message, state)
            return
        task["title"] = title
        SettingsService.upsert_mission_task(task)
        await message.answer("✅ Название сохранено.")
        await state.set_state(None)
        await _show_admin_mission_task(message, task_id, state)
        return
    await state.update_data(mission_new_title=title[:80])
    await state.set_state(AdminState.waiting_for_mission_task_count)
    await message.answer("Сколько раз нужно выполнить? Например 1, 2 или 3:")


@router.message(AdminState.waiting_for_mission_task_count)
async def admin_mission_task_count_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        count = int((message.text or "").strip())
    except ValueError:
        await message.answer("🔶 Введите целое число от 1 до 50.")
        return
    if count < 1 or count > 50:
        await message.answer("🔶 Число от 1 до 50.")
        return
    data = await state.get_data()
    task_id = data.get("mission_task_id")
    if task_id:
        task = SettingsService.get_mission_task(task_id)
        if not task:
            await state.set_state(None)
            await message.answer("🔶 Задание не найдено.")
            await _show_admin_missions(message, state)
            return
        task["count"] = count
        SettingsService.upsert_mission_task(task)
        await message.answer("✅ Цель сохранена.")
        await state.set_state(None)
        await _show_admin_mission_task(message, task_id, state)
        return
    await state.update_data(mission_new_count=count)
    await state.set_state(AdminState.waiting_for_mission_task_reward)
    await message.answer(
        "Награда именно за это задание, в рублях (можно 0 - тогда выплатится только награда за набор):"
    )


@router.message(AdminState.waiting_for_mission_task_reward)
async def admin_mission_task_reward_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace(" ", "").replace("₽", "").strip()
    try:
        amount = int(float(raw.replace(",", ".")))
    except ValueError:
        await message.answer("🔶 Введите целое число, например 0 или 250.")
        return
    if amount < 0 or amount > 1_000_000:
        await message.answer("🔶 Сумма от 0 до 1 000 000.")
        return
    data = await state.get_data()
    task_id = data.get("mission_task_id")
    if task_id:
        task = SettingsService.get_mission_task(task_id)
        if not task:
            await state.set_state(None)
            await message.answer("🔶 Задание не найдено.")
            await _show_admin_missions(message, state)
            return
        task["reward"] = amount
        SettingsService.upsert_mission_task(task)
        await message.answer("✅ Награда задания сохранена.")
        await state.set_state(None)
        await _show_admin_mission_task(message, task_id, state)
        return
    kind = data.get("mission_new_kind") or "purchases"
    title = str(data.get("mission_new_title") or "").strip()
    count = int(data.get("mission_new_count") or 1)
    new_id = secrets.token_hex(4)
    saved = SettingsService.upsert_mission_task({
        "id": new_id,
        "title": title or "Задание",
        "kind": kind,
        "count": count,
        "enabled": True,
        "reward": amount,
    })
    if not saved:
        await message.answer("🔶 Не удалось добавить задание. Проверьте лимит (макс. 12).")
        await state.set_state(None)
        await _show_admin_missions(message, state)
        return
    await message.answer("✅ Задание добавлено.")
    await state.set_state(None)
    await _show_admin_mission_task(message, saved["id"], state)


@router.message(AdminState.waiting_for_streak_required)
async def admin_streak_required_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        required = int((message.text or "").strip())
    except ValueError:
        await message.answer("🔶 Введите целое число от 2 до 20.")
        return
    if required < 2 or required > 20:
        await message.answer("🔶 Число от 2 до 20.")
        return
    SettingsService.update_purchase_streak(required=required)
    await message.answer(f"✅ Акция {required}+1: бонус на {required + 1}-ю покупку.")
    await state.set_state(None)
    await _show_admin_streak(message, state)


@router.message(AdminState.waiting_for_streak_percent)
async def admin_streak_percent_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace("%", "").strip()
    try:
        percent = int(raw)
    except ValueError:
        await message.answer("🔶 Введите число от 1 до 100.")
        return
    if percent < 1 or percent > 100:
        await message.answer("🔶 Скидка от 1 до 100% (100 = бонус бесплатно).")
        return
    SettingsService.update_purchase_streak(percent=percent)
    label = "бонус бесплатно" if percent >= 100 else f"скидка {percent}% на бонус"
    await message.answer(f"✅ Акция: {label}.")
    await state.set_state(None)
    await _show_admin_streak(message, state)


@router.message(AdminState.waiting_for_exchange_bot_url)
async def admin_exchange_bot_save(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    if not url:
        await message.answer("🔶 Ссылка не может быть пустой.")
        return
    from app.services.settings import SettingsService
    SettingsService.set_exchange_bot_url(url)
    await message.answer(f"✅ Ссылка сохранена: {url}")
    await state.set_state(None)
    await _show_exchange_bot_menu(message, state)


@router.callback_query(F.data == 'adm_price_region_add')
async def adm_price_region_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_pricing_region_name)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_pricing_menu")]])
    await callback.message.edit_text(
        "Введите название региона, для которого требуется настроить наценку:",
        reply_markup=markup
    )
    await state.update_data(
        pricing_target_type="region",
        pricing_region_key=None,
        pricing_region_label=None,
        pricing_prompt_chat_id=callback.message.chat.id,
        pricing_prompt_message_id=callback.message.message_id
    )
    await callback.answer()


@router.callback_query(F.data.startswith('adm_price_reg_'))
async def adm_price_region_pick(callback: CallbackQuery, state: FSMContext):
    idx_raw = callback.data[len('adm_price_reg_'):]
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return

    data = await state.get_data()
    options = data.get("pricing_region_options")
    if not options:
        options = PricingService.get_region_entries()
        await state.update_data(pricing_region_options=options)

    if idx < 0 or idx >= len(options):
        await callback.answer("Элемент недоступен.", show_alert=True)
        return

    entry = options[idx]
    await state.update_data(
        pricing_target_type="region",
        pricing_region_key=entry["key"],
        pricing_region_label=entry["label"]
    )
    await state.set_state(AdminState.waiting_for_pricing_value)

    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_pricing_menu")]])
    await callback.message.edit_text(
        f"{entry['label']}\nТекущая наценка: x{entry['value']:.2f}\n\nВведите новый множитель (например 1.25). Значение 1 отключит наценку.",
        reply_markup=markup
    )
    await state.update_data(
        pricing_prompt_chat_id=callback.message.chat.id,
        pricing_prompt_message_id=callback.message.message_id
    )
    await callback.answer()


@router.message(AdminState.waiting_for_pricing_region_name)
async def adm_price_region_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("🔶 Название не может быть пустым. Попробуйте снова.")
        return

    await state.update_data(
        pricing_target_type="region",
        pricing_region_label=name,
        pricing_region_key=None
    )
    await state.set_state(AdminState.waiting_for_pricing_value)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_pricing_menu")]])
    data = await state.get_data()
    chat_id = data.get("pricing_prompt_chat_id")
    msg_id = data.get("pricing_prompt_message_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                f"Регион: {name}\nВведите множитель (например 1.25). Значение 1 отключит наценку.",
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=markup
            )
        except TelegramBadRequest:
            sent = await message.answer(
                f"Регион: {name}\nВведите множитель (например 1.25). Значение 1 отключит наценку.",
                reply_markup=markup
            )
            await state.update_data(
                pricing_prompt_chat_id=sent.chat.id,
                pricing_prompt_message_id=sent.message_id
            )
    else:
        sent = await message.answer(
            f"Регион: {name}\nВведите множитель (например 1.25). Значение 1 отключит наценку.",
            reply_markup=markup
        )
        await state.update_data(
            pricing_prompt_chat_id=sent.chat.id,
            pricing_prompt_message_id=sent.message_id
        )


@router.message(AdminState.waiting_for_pricing_value)
async def adm_price_value(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        await message.answer("🔶 Введите число, например 1.25.")
        return

    if value <= 0:
        await message.answer("🔶 Множитель должен быть больше нуля.")
        return

    data = await state.get_data()
    target_type = data.get("pricing_target_type")
    response_text = "Настройка обновлена."
    prompt_chat = data.get("pricing_prompt_chat_id")
    prompt_msg = data.get("pricing_prompt_message_id")

    if target_type == "region":
        label = data.get("pricing_region_label") or data.get("pricing_region_key")
        key = data.get("pricing_region_key") or label
        if not label:
            await message.answer("🔶 Не удалось определить регион. Повторите настройку.")
            await state.set_state(None)
            return

        if value <= 1.0:
            PricingService.clear_region_multiplier(key or label)
            response_text = f"Наценка для региона '{label}' отключена."
        else:
            PricingService.set_region_multiplier(label, value)
            response_text = f"Наценка для региона '{label}' установлена: x{value:.2f}."

    else:
        await message.answer("🔶 Неизвестный тип цели. Настройка не сохранена.")
        await state.set_state(None)
        return

    await message.answer(f"✅ {response_text}")
    await state.update_data(
        pricing_target_type=None,
        pricing_region_key=None,
        pricing_region_label=None,
        pricing_prompt_chat_id=None,
        pricing_prompt_message_id=None
    )
    await state.set_state(None)
    await _show_pricing_menu(
        message,
        state,
        bot=message.bot,
        edit_chat_id=prompt_chat,
        edit_message_id=prompt_msg
    )

# --- Управление Каталогами ---

async def _show_catalog_generation_settings(target: CallbackQuery | Message):
    all_products = CatalogService.get_all_products()
    selected_ids = CatalogService.get_allowed_generation_product_ids()
    mode = "Все товары (фильтр выключен)" if not selected_ids else f"Выбрано товаров: {len(selected_ids)}"

    text = (
        "🧬 <b>Настройки генерации прайса</b>\n\n"
        "Этот фильтр влияет на товары, которые попадают в НОВЫЙ сгенерированный прайс при выборе города.\n"
        "Уже созданные прайсы в кэше не меняются.\n\n"
        f"Режим: <b>{mode}</b>"
    )

    markup = kb.get_admin_catalog_generation_menu(all_products, selected_ids)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _show_miniopt_generation_settings(target: CallbackQuery | Message):
    all_products = CatalogService.get_all_miniopt_products()
    selected_ids = CatalogService.get_allowed_miniopt_generation_product_ids()
    mode = "Все miniopt-товары участвуют" if not selected_ids else f"Выбрано: {len(selected_ids)}"
    text = (
        "🧺 <b>Настройки генерации mini-опта</b>\n\n"
        "Этот фильтр влияет на товары, которые попадают в НОВЫЙ miniopt-каталог города.\n"
        "Уже созданные miniopt-каталоги в кэше не меняются.\n\n"
        f"Режим: <b>{mode}</b>"
    )

    markup = kb.get_admin_generation_menu(
        all_products,
        selected_ids,
        select_all_callback="adm_miniopt_gen_select_all",
        reset_callback="adm_miniopt_gen_reset",
        toggle_prefix="adm_miniopt_gen_tog_",
        back_callback="admin_back_main",
        reset_label="↺ Сбросить фильтр miniopt",
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == 'admin_catalog_generation_settings')
async def admin_catalog_generation_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data == 'admin_miniopt_generation_settings')
async def admin_miniopt_generation_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _show_miniopt_generation_settings(callback)


@router.callback_query(F.data == 'adm_cat_gen_select_all')
async def adm_cat_gen_select_all(callback: CallbackQuery):
    all_ids = [p.get("id") for p in CatalogService.get_all_products() if isinstance(p, dict) and p.get("id")]
    CatalogService.set_allowed_generation_product_ids(all_ids)
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data == 'adm_cat_gen_reset')
async def adm_cat_gen_reset(callback: CallbackQuery):
    CatalogService.reset_generation_product_filter()
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data == 'adm_miniopt_gen_select_all')
async def adm_miniopt_gen_select_all(callback: CallbackQuery):
    all_ids = [p.get("id") for p in CatalogService.get_all_miniopt_products() if isinstance(p, dict) and p.get("id")]
    CatalogService.set_allowed_miniopt_generation_product_ids(all_ids)
    await _show_miniopt_generation_settings(callback)


@router.callback_query(F.data == 'adm_miniopt_gen_reset')
async def adm_miniopt_gen_reset(callback: CallbackQuery):
    CatalogService.reset_miniopt_generation_product_filter()
    await _show_miniopt_generation_settings(callback)


@router.callback_query(F.data.startswith('adm_cat_gen_tog_'))
async def adm_cat_gen_toggle(callback: CallbackQuery):
    product_id = callback.data[len('adm_cat_gen_tog_'):]
    CatalogService.toggle_allowed_generation_product_id(product_id)
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data.startswith('adm_miniopt_gen_tog_'))
async def adm_miniopt_gen_toggle(callback: CallbackQuery):
    product_id = callback.data[len('adm_miniopt_gen_tog_'):]
    CatalogService.toggle_allowed_miniopt_generation_product_id(product_id)
    await _show_miniopt_generation_settings(callback)

@router.callback_query(F.data == 'admin_catalog_search')
async def admin_catalog_search(callback: CallbackQuery, state: FSMContext):
    await state.update_data(catalog_edit_scope="regular")
    await state.set_state(AdminState.waiting_for_catalog_city)
    await callback.message.edit_text(
        '🏙 <b>Редактор каталогов</b>\n'
        'Введите название города (например, "Москва" или "Томск"), чтобы посмотреть или изменить список товаров для него:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_back_main')]])
    )


@router.callback_query(F.data == 'admin_miniopt_search')
async def admin_miniopt_search(callback: CallbackQuery, state: FSMContext):
    await state.update_data(catalog_edit_scope="miniopt")
    await state.set_state(AdminState.waiting_for_catalog_city)
    await callback.message.edit_text(
        '🧺 <b>Редактор mini-опта</b>\n'
        'Введите название города, чтобы посмотреть или изменить miniopt-список товаров:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_back_main')]])
    )

@router.message(AdminState.waiting_for_catalog_city)
async def admin_process_catalog_city(message: Message, state: FSMContext):
    city_name = message.text.strip()
    data = await state.get_data()
    scope = data.get("catalog_edit_scope", "regular")
    
    # Пытаемся получить текущие товары. Если города нет в кэше, он сгенерируется.
    # Но админу лучше знать, есть он или нет.
    # CatalogService.get_products_for_city(city_name) САМ создаст если нет.
    # Это удобно: админ пишет город, если его не было - он создается.
    
    # Обновляем состояние, храним город
    await state.set_state(None)
    await state.update_data(current_edit_city=city_name, catalog_edit_scope=scope)

    if scope == "miniopt":
        await _show_city_miniopt_menu(message, city_name)
    else:
        await _show_city_catalog_menu(message, city_name)

async def _show_city_catalog_menu(message_or_callback, city_name):
    products = CatalogService.get_products_for_city(city_name)
    update_time = CatalogService.get_last_update_time(city_name) or "Только что"
    
    products_text = "\n".join([f"{i+1}. {p['name']} ({p['price']}р)" for i, p in enumerate(products)])
    
    text = (
        f"🏙 Город: <b>{city_name}</b>\n"
        f"🕒 Обновлено: {update_time}\n\n"
        f"📦 <b>Текущие товары ({len(products)} шт):</b>\n"
        f"{products_text}"
    )
    
    mk = kb.get_admin_catalog_menu(city_name)
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=mk)
    else:
        await message_or_callback.message.edit_text(text, reply_markup=mk)


async def _show_city_miniopt_menu(message_or_callback, city_name):
    products = CatalogService.get_miniopt_products_for_city(city_name)
    update_time = CatalogService.get_last_miniopt_update_time(city_name) or "Только что"

    products_text = "\n".join([f"{i+1}. {p['name']} ({p['price']}р)" for i, p in enumerate(products)])
    if not products_text:
        products_text = "Список пуст"

    text = (
        f"🧺 Город: <b>{city_name}</b>\n"
        f"🕒 Обновлено: {update_time}\n\n"
        f"📦 <b>Текущие miniopt-товары ({len(products)} шт):</b>\n"
        f"{products_text}"
    )

    mk = kb.get_admin_miniopt_menu(city_name)

    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=mk)
    else:
        await message_or_callback.message.edit_text(text, reply_markup=mk)


async def _show_city_delete_menu(callback: CallbackQuery, city_name: str):
    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"➖ Удаление товара из <b>{city_name}</b>:\nВыберите товар для удаления:",
        reply_markup=kb.get_admin_del_product_list(city_name, products)
    )


async def _show_city_add_menu(callback: CallbackQuery, city_name: str):
    current_products = CatalogService.get_products_for_city(city_name)
    current_ids = [p['id'] for p in current_products]
    all_products = CatalogService.ALL_PRODUCTS

    await callback.message.edit_text(
        f"➕ Добавление товара в <b>{city_name}</b>:",
        reply_markup=kb.get_admin_add_product_list(city_name, current_ids, all_products)
    )


async def _show_city_miniopt_delete_menu(callback: CallbackQuery, city_name: str):
    products = CatalogService.get_miniopt_products_for_city(city_name)
    await callback.message.edit_text(
        f"➖ Удаление miniopt-товара из <b>{city_name}</b>:\nВыберите товар для удаления:",
        reply_markup=kb.get_admin_del_miniopt_product_list(city_name, products)
    )


async def _show_city_miniopt_add_menu(callback: CallbackQuery, city_name: str):
    current_products = CatalogService.get_miniopt_products_for_city(city_name)
    current_ids = [p['id'] for p in current_products]
    all_products = CatalogService.get_all_miniopt_products()

    await callback.message.edit_text(
        f"➕ Добавление miniopt-товара в <b>{city_name}</b>:",
        reply_markup=kb.get_admin_add_miniopt_product_list(city_name, current_ids, all_products)
    )

@router.callback_query(F.data.startswith('adm_cat_back_'))
async def admin_cat_back(callback: CallbackQuery):
    city_name = callback.data.split('_', 3)[3]
    await _show_city_catalog_menu(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_back_'))
async def admin_miniopt_back(callback: CallbackQuery):
    city_name = callback.data.split('_', 3)[3]
    await _show_city_miniopt_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_reset_'))
async def admin_cat_reset(callback: CallbackQuery):
    city_name = callback.data.split('_', 3)[3]
    CatalogService.force_refresh_products(city_name)
    await callback.answer("🔄 Каталог пересоздан (случайно).")
    try:
        await _show_city_catalog_menu(callback, city_name)
    except Exception:
        # Игнорируем ошибку, если текст сообщения визуально не изменился
        # (такое бывает, если при регенерации выпал ТОТ ЖЕ список товаров, что редко, но возможно, или если меняется только разметка)
        pass


@router.callback_query(F.data.startswith('adm_miniopt_reset_'))
async def admin_miniopt_reset(callback: CallbackQuery):
    city_name = callback.data.split('_', 3)[3]
    CatalogService.force_refresh_miniopt_products(city_name)
    await callback.answer("🔄 Miniopt-каталог пересоздан.")
    try:
        await _show_city_miniopt_menu(callback, city_name)
    except Exception:
        pass

@router.callback_query(F.data.startswith('adm_cat_del_menu_'))
async def admin_cat_del_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_cat_del_menu_'):]
    await _show_city_delete_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_del_'))
# Формат adm_cat_del_CITYname_ProdID. 
# Внимание: city_name может содержать подчеркивания? Надеемся нет или парсим аккуратно.
# Лучше парсить split с конца или использовать separator.
# Но у нас ID товара простой (p1, p2...).
async def admin_cat_del_do(callback: CallbackQuery):
    payload = callback.data[len('adm_cat_del_'):]
    city_name, p_id = payload.rsplit('_', 1)
    
    if CatalogService.remove_product_from_city(city_name, p_id):
        await callback.answer("✅ Удалено")
    else:
        await callback.answer("🔶 Ошибка")
        
    await _show_city_delete_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_add_menu_'))
async def admin_cat_add_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_cat_add_menu_'):]
    await _show_city_add_menu(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_add_menu_'))
async def admin_miniopt_add_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_miniopt_add_menu_'):]
    await _show_city_miniopt_add_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_add_'))
async def admin_cat_add_do(callback: CallbackQuery):
    payload = callback.data[len('adm_cat_add_'):]
    city_name, p_id = payload.rsplit('_', 1)
    
    if CatalogService.add_product_to_city(city_name, p_id):
        await callback.answer("✅ Добавлено")
    else:
        await callback.answer("🔶 Ужe есть")
        
    await _show_city_add_menu(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_add_'))
async def admin_miniopt_add_do(callback: CallbackQuery):
    payload = callback.data[len('adm_miniopt_add_'):]
    city_name, p_id = payload.rsplit('_', 1)

    if CatalogService.add_miniopt_product_to_city(city_name, p_id):
        await callback.answer("✅ Добавлено")
    else:
        await callback.answer("🔶 Уже есть")

    await _show_city_miniopt_add_menu(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_del_menu_'))
async def admin_miniopt_del_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_miniopt_del_menu_'):]
    await _show_city_miniopt_delete_menu(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_del_'))
async def admin_miniopt_del_do(callback: CallbackQuery):
    payload = callback.data[len('adm_miniopt_del_'):]
    city_name, p_id = payload.rsplit('_', 1)

    if CatalogService.remove_miniopt_product_from_city(city_name, p_id):
        await callback.answer("✅ Удалено")
    else:
        await callback.answer("🔶 Ошибка")

    await _show_city_miniopt_delete_menu(callback, city_name)

# --- Создание Custom товара ---

@router.callback_query(F.data.startswith('adm_cat_new_'))
async def admin_cat_new_product_start(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data.split('_', 3)[3]
    await state.update_data(target_city_for_product=city_name, target_catalog_scope='regular')
    
    await state.set_state(AdminState.waiting_for_new_prod_name)
    await callback.message.edit_text(
        "✨ <b>Создание нового товара</b>\n\n"
        "Введите НАЗВАНИЕ товара (например: 'Супер Микс 0.5г'):"
    )

@router.message(AdminState.waiting_for_new_prod_name)
async def admin_cat_new_product_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(new_prod_name=name)
    
    await state.set_state(AdminState.waiting_for_new_prod_price)
    await message.answer("💰 Введите БАЗОВУЮ ЦЕНУ (число, в рублях):")

@router.message(AdminState.waiting_for_new_prod_price)
async def admin_cat_new_product_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.answer("🔶 Нужно ввести целое число.")
        return
        
    data = await state.get_data()
    name = data['new_prod_name']
    city_name = data.get('target_city_for_product')
    scope = data.get('target_catalog_scope', 'regular')
    edit_product_id = data.get('edit_product_id')

    if scope == 'miniopt' and edit_product_id:
        updated = CatalogService.update_miniopt_product(edit_product_id, name, price)
        if updated:
            await message.answer(f"✅ Miniopt-товар '{name}' обновлен!")
        else:
            await message.answer("🔶 Не удалось обновить miniopt-товар.")
    elif scope == 'miniopt':
        new_prod = CatalogService.create_new_miniopt_product(name, price)
        if city_name:
            CatalogService.add_miniopt_product_to_city(city_name, new_prod['id'])
            await message.answer(f"✅ Miniopt-товар '{name}' создан и добавлен в город {city_name}!")
        else:
            await message.answer(f"✅ Miniopt-товар '{name}' создан в глобальной базе!")
    else:
        new_prod = CatalogService.create_new_product(name, price)
        if city_name:
            CatalogService.add_product_to_city(city_name, new_prod['id'])
            await message.answer(f"✅ Товар '{name}' создан и добавлен в город {city_name}!")
        else:
            await message.answer(f"✅ Товар '{name}' создан в глобальной базе!")
    
    # Возвращаемся в меню добавления (или меню города)
    await state.clear()
    
    # Трюк: чтобы показать меню, нам нужен callback object или сообщение с клавиатурой.
    # Но мы в message handler. Отправим новое сообщение.
    if city_name:
        if scope == 'miniopt':
            await _show_city_miniopt_menu(message, city_name)
        else:
            await _show_city_catalog_menu(message, city_name)
    elif scope == 'miniopt':
        await message.answer(
            "🧺 Глобальная база miniopt:",
            reply_markup=kb.get_admin_miniopt_products_global_menu(CatalogService.get_all_miniopt_products())
        )


@router.callback_query(F.data == 'admin_miniopt_products_global_menu')
async def admin_miniopt_products_global_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    products = CatalogService.get_all_miniopt_products()
    await callback.message.edit_text(
        "🧺 <b>Глобальная база miniopt-товаров</b>",
        reply_markup=kb.get_admin_miniopt_products_global_menu(products)
    )


@router.callback_query(F.data == 'admin_miniopt_products_global_add')
async def admin_miniopt_products_global_add(callback: CallbackQuery, state: FSMContext):
    await state.update_data(target_catalog_scope='miniopt', target_city_for_product=None)
    await state.set_state(AdminState.waiting_for_new_prod_name)
    await callback.message.edit_text(
        "✨ <b>Создание нового miniopt-товара</b>\n\n"
        "Введите НАЗВАНИЕ товара:"
    )


@router.callback_query(F.data == 'admin_miniopt_products_global_del_menu')
async def admin_miniopt_products_global_del_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    products = CatalogService.get_all_miniopt_products()
    await callback.message.edit_text(
        "🧺 <b>Удаление miniopt-товара из глобальной базы</b>",
        reply_markup=kb.get_admin_miniopt_products_global_delete_list(products)
    )


@router.callback_query(F.data == 'admin_miniopt_products_global_edit_menu')
async def admin_miniopt_products_global_edit_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    products = CatalogService.get_all_miniopt_products()
    await callback.message.edit_text(
        "🧺 <b>Изменение miniopt-товара</b>",
        reply_markup=kb.get_admin_miniopt_products_global_edit_list(products)
    )


@router.callback_query(F.data.startswith('adm_glob_miniopt_edit_'))
async def admin_miniopt_products_global_edit(callback: CallbackQuery, state: FSMContext):
    product_id = callback.data[len('adm_glob_miniopt_edit_'):]
    products = CatalogService.get_all_miniopt_products()
    product = next((item for item in products if isinstance(item, dict) and item.get('id') == product_id), None)
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await state.update_data(
        target_catalog_scope='miniopt',
        target_city_for_product=None,
        edit_product_id=product_id,
    )
    await state.set_state(AdminState.waiting_for_new_prod_name)
    await callback.message.edit_text(
        "✏️ <b>Изменение miniopt-товара</b>\n\n"
        f"Текущее название: <b>{html.escape(str(product.get('name', '')))}</b>\n"
        f"Текущая базовая цена: <b>{int(product.get('base_price', 0))} руб.</b>\n\n"
        "Введите новое НАЗВАНИЕ товара:"
    )


@router.callback_query(F.data.startswith('adm_glob_miniopt_del_'))
async def admin_miniopt_products_global_delete(callback: CallbackQuery):
    product_id = callback.data[len('adm_glob_miniopt_del_'):]
    if CatalogService.delete_miniopt_product(product_id):
        await callback.answer("✅ Товар удален")
    else:
        await callback.answer("🔶 Товар не найден", show_alert=True)
    products = CatalogService.get_all_miniopt_products()
    await callback.message.edit_text(
        "🧺 <b>Удаление miniopt-товара из глобальной базы</b>",
        reply_markup=kb.get_admin_miniopt_products_global_delete_list(products)
    )


# --- Управление Районами ---

@router.callback_query(F.data == 'admin_districts_menu')
async def admin_districts_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_districts_city)
    await callback.message.edit_text(
        '🏘 <b>Редактор районов</b>\n'
        'Введите название города, например <code>Якутск</code>.\n'
        'Можно и с областью: <code>Якутск (Республика Саха (Якутия))</code>',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_back_main')]])
    )

async def _district_edit_city(state: FSMContext, callback: CallbackQuery | None = None, prefix: str = "") -> str:
    data = await state.get_data()
    city_name = str(data.get("dist_edit_city") or "").strip()
    if city_name:
        return city_name
    if callback and prefix and callback.data.startswith(prefix):
        return callback.data[len(prefix):].strip()
    return ""

async def _ask_district_city_choice(message: Message, state: FSMContext, options: list[dict]) -> None:
    mapping: dict[str, str] = {}
    buttons: list[list[InlineKeyboardButton]] = []
    for option in options:
        token = secrets.token_hex(3)
        while token in mapping:
            token = secrets.token_hex(3)
        mapping[token] = option["cache_key"]
        city = option.get("city") or "Город"
        region = option.get("region") or "без области"
        buttons.append([
            InlineKeyboardButton(
                text=f"{city} ({region})",
                callback_data=f"adm_dist_pick_{token}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="❗ Отмена", callback_data="admin_back_main")])
    await state.update_data(dist_pick_map=mapping)
    await message.answer(
        "🤔 Найдено несколько городов с таким названием.\n"
        "Это разные области - выберите нужный вариант:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(AdminState.waiting_for_districts_city)
async def admin_process_districts_city(message: Message, state: FSMContext):
    city_name = " ".join((message.text or "").split())
    if len(city_name) < 2:
        await message.answer("🔶 Слишком короткое название города.")
        return

    _, region_part = GeoService._split_city_region_key(city_name)
    options = GeoService.list_city_region_options(city_name)
    if not region_part and len(options) > 1:
        await _ask_district_city_choice(message, state, options)
        return

    districts, cache_key = GeoService.get_districts_for_city(city_name)
    edit_id = cache_key or GeoService.build_city_cache_key(city_name, region_part) or city_name

    if not cache_key:
        await message.answer(
            f"🔶 Город «{html.escape(city_name)}» не найден в кэше районов.\n"
            "Будет создан новый список."
        )

    await state.update_data(dist_edit_city=edit_id)
    await state.set_state(None)
    await _show_district_editor(message, edit_id)


@router.callback_query(F.data.startswith("adm_dist_pick_"))
async def admin_dist_pick_city(callback: CallbackQuery, state: FSMContext):
    token = callback.data[len("adm_dist_pick_"):]
    data = await state.get_data()
    mapping: dict[str, str] = data.get("dist_pick_map") or {}
    cache_key = mapping.get(token)
    if not cache_key:
        await callback.answer("Данные устарели, введите город заново.", show_alert=True)
        return
    await state.update_data(dist_edit_city=cache_key)
    await state.set_state(None)
    await _show_district_editor(callback, cache_key)
    await callback.answer()

async def _show_district_editor(message_or_callback, city_name):
    # Reload from service to get latest
    districts, cache_key = GeoService.get_districts_for_city(city_name)
    if districts is None: districts = []

    city_label, region_label = GeoService._parse_cache_city_key(cache_key or city_name)
    header = f"🏘 Районы города: <b>{html.escape(city_label or city_name)}</b>"
    if region_label:
        header += f"\n🗺 {html.escape(region_label)}"
    if cache_key:
        header += f"\n🔑 <code>{html.escape(cache_key)}</code>"
    if cache_key and not region_label:
        header += (
            "\n\n🔶 У города не указана область - региональная наценка не применится.\n"
            "Добавьте его заново через «➕ Добавить город в базу» с областью."
        )

    d_list = "\n".join([f"- {html.escape(str(d))}" for d in districts])
    if not d_list: d_list = "(пусто)"

    text = f"{header}\n\n{d_list}"
    mk = kb.get_admin_districts_menu(city_name, districts)

    try:
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, reply_markup=mk)
        else:
            await message_or_callback.message.edit_text(text, reply_markup=mk)
    except TelegramBadRequest as err:
        logger.warning("district editor send failed city=%s err=%s", city_name, err)
        fallback = message_or_callback if isinstance(message_or_callback, Message) else message_or_callback.message
        if fallback:
            await fallback.answer(text)

@router.callback_query(F.data.startswith('adm_dist_add'))
async def admin_dist_add(callback: CallbackQuery, state: FSMContext):
    city_name = await _district_edit_city(state, callback, "adm_dist_add_")
    if not city_name:
        await callback.answer("Откройте редактор районов заново.", show_alert=True)
        return

    await state.update_data(dist_edit_city=city_name)
    await state.set_state(AdminState.waiting_for_district_name)

    await callback.message.edit_text(
        f"➕ Добавление районов в <b>{html.escape(city_name)}</b>\n\n"
        "Пришлите один район или сразу список.\n"
        "Можно с новой строки, через запятую или точку с запятой, например:\n"
        "<code>Центр\nЗаречье\nЮжный</code>\n"
        "или <code>Центр, Заречье, Южный</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_dist_back")]])
    )
    await callback.answer()

@router.message(AdminState.waiting_for_district_name, F.text, ~F.text.startswith("/"))
async def admin_save_district(message: Message, state: FSMContext):
    data = await state.get_data()
    city_name = data.get('dist_edit_city')
    names = GeoService.parse_district_list(message.text or "")
    if not names:
        await message.answer("🔶 Пришлите хотя бы одно название района.")
        return

    added, skipped, _ = GeoService.add_districts(city_name, names)
    lines = []
    if added:
        lines.append("✅ Добавлено: " + ", ".join(added))
    if skipped:
        lines.append("ℹ️ Уже были: " + ", ".join(skipped))
    if not lines:
        lines.append("🔶 Не удалось добавить районы.")
    await message.answer("\n".join(lines))

    await state.update_data(dist_edit_city=city_name)
    await state.set_state(None)
    await _show_district_editor(message, city_name)

@router.callback_query(F.data.startswith('adm_dist_del_menu'))
async def admin_dist_del_menu_show(callback: CallbackQuery, state: FSMContext):
    city_name = await _district_edit_city(state, callback, "adm_dist_del_menu_")
    if not city_name:
        await callback.answer("Откройте редактор районов заново.", show_alert=True)
        return

    districts, _ = GeoService.get_districts_for_city(city_name)
    if not districts:
        districts = []

    entries: list[tuple[str, str]] = []
    mapping: dict[str, str] = {}
    for dist in districts:
        token = secrets.token_hex(3)
        while token in mapping:
            token = secrets.token_hex(3)
        mapping[token] = dist
        entries.append((token, dist))

    await state.update_data(
        dist_edit_city=city_name,
        dist_del_city=city_name,
        dist_del_map=mapping,
        dist_del_order=[token for token, _ in entries]
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for token, dist in entries:
        buttons.append([
            InlineKeyboardButton(text=f"❗ {dist}", callback_data=f"adm_dist_del_do_{token}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_dist_back")])

    await callback.message.edit_text(
        f"➖ Удаление района из <b>{html.escape(city_name)}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@router.callback_query(F.data.startswith('adm_dist_del_do_'))
async def admin_dist_del_do(callback: CallbackQuery, state: FSMContext):
    token = callback.data[len("adm_dist_del_do_"):]
    data = await state.get_data()
    city_name = data.get("dist_del_city")
    mapping: dict[str, str] = data.get("dist_del_map") or {}
    order: list[str] = data.get("dist_del_order") or []

    if not city_name or token not in mapping:
        await callback.answer("Данные устарели, откройте меню снова.", show_alert=True)
        return

    dist_name = mapping[token]
    if GeoService.remove_district(city_name, dist_name):
        await callback.answer(f"🗑 {dist_name} удален")
        mapping.pop(token, None)
        order = [t for t in order if t != token]
    else:
        await callback.answer("Ошибка удаления", show_alert=True)
        return

    await state.update_data(dist_del_map=mapping, dist_del_order=order)

    entries = [(t, mapping[t]) for t in order if t in mapping]
    buttons: list[list[InlineKeyboardButton]] = []
    for entry_token, dist in entries:
        buttons.append([
            InlineKeyboardButton(text=f"❗ {dist}", callback_data=f"adm_dist_del_do_{entry_token}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_dist_back")])

    await callback.message.edit_text(
        f"➖ Удаление района из <b>{html.escape(city_name)}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith('adm_dist_back'))
async def admin_dist_back(callback: CallbackQuery, state: FSMContext):
    city_name = await _district_edit_city(state, callback, "adm_dist_back_")
    if not city_name:
        await callback.answer("Откройте редактор районов заново.", show_alert=True)
        return
    await _show_district_editor(callback, city_name)
    await callback.answer()


def _manual_city_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❗ Отмена", callback_data="admin_back_main")]]
    )


def _format_population(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _manual_city_population_tier(population: int) -> str:
    if 0 < population < COMPACT_CITY_MAX_POPULATION:
        return "компактный прайс"
    if population < SMALL_CITY_MAX_POPULATION:
        return "малый прайс"
    if population < MEDIUM_CITY_MAX_POPULATION:
        return "средний прайс"
    return "крупный прайс"


def _format_manual_city_preview(city: str, region: str, districts: list[str], population: int) -> str:
    lines = "\n".join(f"• {html.escape(name)}" for name in districts)
    return (
        "📋 <b>Проверьте город</b>\n\n"
        f"🏙 Город: <b>{html.escape(city)}</b>\n"
        f"🗺 Область / муниципалитет: <b>{html.escape(region)}</b>\n"
        f"👥 Население: <b>{_format_population(population)}</b> ({_manual_city_population_tier(population)})\n"
        f"🏘 Районы ({len(districts)}):\n{lines}\n\n"
        "Если список районов неверный - пришлите его заново.\n"
        "Если всё верно - нажмите «Сохранить в базу»."
    )


@router.callback_query(F.data == "admin_manual_city_start")
async def admin_manual_city_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_manual_city_name)
    await callback.message.edit_text(
        "➕ <b>Добавление города в базу</b>\n\n"
        "Шаг 1/4. Введите название города, например:\n"
        "<code>Луганск</code>",
        reply_markup=_manual_city_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_manual_city_name, F.text, ~F.text.startswith("/"))
async def admin_manual_city_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    city = " ".join((message.text or "").split())
    if len(city) < 2:
        await message.answer("🔶 Слишком короткое название города.")
        return
    await state.update_data(manual_city_name=city)
    await state.set_state(AdminState.waiting_for_manual_city_region)
    await message.answer(
        f"🏙 Город: <b>{html.escape(city)}</b>\n\n"
        "Шаг 2/4. Введите область / муниципалитет, например:\n"
        "<code>Луганская область</code>",
        reply_markup=_manual_city_cancel_kb(),
    )


@router.message(AdminState.waiting_for_manual_city_region, F.text, ~F.text.startswith("/"))
async def admin_manual_city_region(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    region = " ".join((message.text or "").split())
    if len(region) < 2:
        await message.answer("🔶 Слишком короткое название области / муниципалитета.")
        return
    data = await state.get_data()
    city = data.get("manual_city_name") or ""
    await state.update_data(manual_city_region=region)
    await state.set_state(AdminState.waiting_for_manual_city_population)
    await message.answer(
        f"🏙 Город: <b>{html.escape(city)}</b>\n"
        f"🗺 Область: <b>{html.escape(region)}</b>\n\n"
        "Шаг 3/4. Введите население города числом, например:\n"
        "<code>350000</code> или <code>350 000</code>\n\n"
        "От этого зависит прайс:\n"
        f"• до {_format_population(COMPACT_CITY_MAX_POPULATION)} - компактный\n"
        f"• до {_format_population(SMALL_CITY_MAX_POPULATION)} - малый\n"
        f"• до {_format_population(MEDIUM_CITY_MAX_POPULATION)} - средний\n"
        f"• от {_format_population(MEDIUM_CITY_MAX_POPULATION)} - крупный\n\n"
        f"Минимум для каталога: {_format_population(MIN_CITY_POPULATION)}.",
        reply_markup=_manual_city_cancel_kb(),
    )


@router.message(AdminState.waiting_for_manual_city_population, F.text, ~F.text.startswith("/"))
async def admin_manual_city_population(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    population = GeoService._parse_population_value(message.text)
    if population < MIN_CITY_POPULATION:
        await message.answer(
            f"🔶 Введите население числом, не меньше {_format_population(MIN_CITY_POPULATION)}.\n"
            "Например: <code>120000</code>"
        )
        return

    data = await state.get_data()
    city = data.get("manual_city_name") or ""
    region = data.get("manual_city_region") or ""
    await state.update_data(manual_city_population=population)
    await state.set_state(AdminState.waiting_for_manual_city_districts)
    await message.answer(
        f"🏙 Город: <b>{html.escape(city)}</b>\n"
        f"🗺 Область: <b>{html.escape(region)}</b>\n"
        f"👥 Население: <b>{_format_population(population)}</b> ({_manual_city_population_tier(population)})\n\n"
        "Шаг 4/4. Пришлите районы города.\n"
        "Можно с новой строки, через запятую или точку с запятой, например:\n"
        "<code>Ленинский\nЖовтневый\nКаменнобродский</code>\n"
        "или <code>Ленинский, Жовтневый, Каменнобродский</code>",
        reply_markup=_manual_city_cancel_kb(),
    )


@router.message(AdminState.waiting_for_manual_city_districts, F.text, ~F.text.startswith("/"))
async def admin_manual_city_districts(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    city = data.get("manual_city_name") or ""
    region = data.get("manual_city_region") or ""
    population = int(data.get("manual_city_population") or 0)
    if not city or not region or population < MIN_CITY_POPULATION:
        await state.clear()
        await message.answer("🔶 Данные мастера устарели. Откройте добавление города заново.")
        return

    names = GeoService.parse_district_list(message.text or "")
    if not names:
        await message.answer("🔶 Пришлите хотя бы одно название района.")
        return

    await state.update_data(manual_city_districts=names)
    await message.answer(
        _format_manual_city_preview(city, region, names, population),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💾 Сохранить в базу", callback_data="adm_manual_city_save")],
                [InlineKeyboardButton(text="❗ Отмена", callback_data="admin_back_main")],
            ]
        ),
    )


@router.callback_query(F.data == "adm_manual_city_save")
async def admin_manual_city_save(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    data = await state.get_data()
    city = data.get("manual_city_name") or ""
    region = data.get("manual_city_region") or ""
    districts = data.get("manual_city_districts") or []
    population = int(data.get("manual_city_population") or 0)
    if not city or not region or not districts or population < MIN_CITY_POPULATION:
        await callback.answer("Данные устарели, заполните город заново.", show_alert=True)
        return

    try:
        result = GeoService.create_manual_city(city, region, districts, population)
    except ValueError:
        await callback.answer("Не удалось сохранить: проверьте город, область, население и районы.", show_alert=True)
        return

    await state.clear()
    replaced_note = (
        "\n\nℹ️ Этот город уже был в базе - районы и население перезаписаны."
        if result.get("replaced")
        else ""
    )
    district_lines = "\n".join(f"• {html.escape(name)}" for name in result["districts"])
    saved_population = int(result.get("population") or population)
    await callback.message.edit_text(
        "✅ <b>Город записан в базу</b>\n\n"
        f"🏙 {html.escape(result['city'])}\n"
        f"🗺 {html.escape(result['region'])}\n"
        f"👥 Население: {_format_population(saved_population)} ({_manual_city_population_tier(saved_population)})\n"
        f"🔑 Ключ: <code>{html.escape(result['cache_key'])}</code>\n"
        f"🏘 Районов: {len(result['districts'])}\n{district_lines}"
        f"{replaced_note}\n\n"
        "Пользователи смогут найти этот город в поиске.",
    )
    await callback.answer("Сохранено")
    edit_id = result.get("cache_key") or result["city"]
    await state.update_data(dist_edit_city=edit_id)
    await _show_district_editor(callback.message, edit_id)

# ----------------------------------------

from app.services.reviews import ReviewsService # Импорт

# --- Управление фразами для отзывов ---

@router.callback_query(F.data == 'admin_reviews_menu')
async def admin_reviews_main_menu_show(callback: CallbackQuery):
    await callback.message.edit_text(
        "⭐️ <b>Управление отзывами</b>\nЧто хотите сделать?",
        reply_markup=kb.get_admin_reviews_menu()
    )

@router.callback_query(F.data == 'admin_rev_phrases_menu')
async def admin_phrases_menu(callback: CallbackQuery):
    phrases = ReviewsService.get_custom_prompts()
    
    text = "🗣 <b>Настройка стиля отзывов</b>\n\nТекущие пользовательские фразы (AI будет стараться использовать их):\n"
    if not phrases:
        text += "<i>(Список пуст)</i>"
    else:
        for i, p in enumerate(phrases):
            text += f"{i+1}. {p}\n"
    
    # We need a sub-keyboard for phrases management here
    # Since get_admin_reviews_menu is now the MAIN menu, we need custom buttons here
    # Let's inline them here for simplicity or add to kb
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить фразу", callback_data="admin_rev_add")],
        [InlineKeyboardButton(text="🗑 Удалить фразу", callback_data="admin_rev_del")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- REVIEW MODERATION ---

def _trim_review_text(text: str, limit: int = 160) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _build_admin_review_mod_markup(
    index: int,
    total: int,
    review_id: str,
    hidden: bool,
    nav_prefix: str = "adm_rev_mod_nav_",
    current_stars: int = 5,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    nav_row: list[InlineKeyboardButton] = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"{nav_prefix}{index - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))
    nav_row.append(InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="ignore"))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"{nav_prefix}{index + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))
    buttons.append(nav_row)

    stars = ReviewsService.normalize_stars(current_stars)
    buttons.append([
        InlineKeyboardButton(
            text=f"✅{value}⭐" if value == stars else f"{value}⭐",
            callback_data=f"adm_rev_mod_stars_{review_id}_{value}",
        )
        for value in range(1, 6)
    ])

    hide_btn = InlineKeyboardButton(
        text="👁️ Показать" if hidden else "🙈 Скрыть",
        callback_data=f"adm_rev_mod_unhide_{review_id}" if hidden else f"adm_rev_mod_hide_{review_id}"
    )
    buttons.append([
        hide_btn,
        InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"adm_rev_mod_edit_{review_id}")
    ])

    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_rev_mod_del_{review_id}"),
        InlineKeyboardButton(text="🔎 Похожие", callback_data=f"adm_rev_mod_dups_{review_id}")
    ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _resume_review_moderation(target: CallbackQuery | Message, state: FSMContext):
    data = await state.get_data()
    source = data.get("adm_rev_mod_source", "all")
    index = data.get("adm_rev_mod_index", 0)
    if source == "generated":
        await _show_admin_review_moderation_generated(target, index, state)
    else:
        await _show_admin_review_moderation(target, index, state)


async def _show_admin_review_moderation(target: CallbackQuery | Message, index: int, state: FSMContext):
    review, total = ReviewsService.get_admin_review(index)

    if not review:
        text = "🛡 <b>Модерация отзывов</b>\n\nПока нет отзывов."
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]])
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        return

    await state.update_data(
        adm_rev_mod_index=index,
        adm_rev_mod_total=total,
        adm_rev_mod_id=review.get("id"),
        adm_rev_mod_source="all",
    )

    status = "🙈 Скрыт" if review.get("hidden") else "👁️ Показан"
    star_count = ReviewsService.normalize_stars(review.get("stars", 5))
    stars = "⭐️" * star_count
    rating = ReviewsService.get_rating_stats()
    user = html.escape(str(review.get("user") or ReviewsService.HIDDEN_USERNAME))
    date = html.escape(str(review.get("display_date", "")))
    item_info = html.escape(str(review.get("item_info", "")))
    text_body = html.escape(str(review.get("text", "")))

    text = (
        "🛡 <b>Модерация отзывов</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"ID: <b>{review.get('id', '-')}</b>\n"
        f"{ReviewsService.format_rating_line(rating)}\n\n"
        f"👤 <b>{user}</b> ({date})\n"
        f"{stars} ({star_count}/5)\n"
        f"🛍 <i>{item_info}</i>\n\n"
        f"🗣 <i>{text_body}</i>\n"
    )

    markup = _build_admin_review_mod_markup(
        index,
        total,
        review.get("id", ""),
        review.get("hidden", False),
        current_stars=star_count,
    )

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _show_admin_review_moderation_generated(target: CallbackQuery | Message, index: int, state: FSMContext):
    review, total = ReviewsService.get_last_batch_admin_review(index)

    if not review:
        text = "🧪 <b>Модерация сгенерированных</b>\n\nПока нет сгенерированных отзывов в последней волне."
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]])
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        return

    await state.update_data(
        adm_rev_mod_index=index,
        adm_rev_mod_total=total,
        adm_rev_mod_id=review.get("id"),
        adm_rev_mod_source="generated",
    )

    status = "🙈 Скрыт" if review.get("hidden") else "👁️ Показан"
    star_count = ReviewsService.normalize_stars(review.get("stars", 5))
    stars = "⭐️" * star_count
    rating = ReviewsService.get_rating_stats()
    user = html.escape(str(review.get("user") or ReviewsService.HIDDEN_USERNAME))
    date = html.escape(str(review.get("display_date", "")))
    item_info = html.escape(str(review.get("item_info", "")))
    text_body = html.escape(str(review.get("text", "")))

    text = (
        "🧪 <b>Модерация сгенерированных</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"ID: <b>{review.get('id', '-')}</b>\n"
        f"{ReviewsService.format_rating_line(rating)}\n\n"
        f"👤 <b>{user}</b> ({date})\n"
        f"{stars} ({star_count}/5)\n"
        f"🛍 <i>{item_info}</i>\n\n"
        f"🗣 <i>{text_body}</i>\n"
    )

    markup = _build_admin_review_mod_markup(
        index,
        total,
        review.get("id", ""),
        review.get("hidden", False),
        nav_prefix="adm_rev_gen_mod_nav_",
        current_stars=star_count,
    )

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm_rev_mod_start")
async def adm_rev_mod_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_admin_review_moderation(callback, 0, state)
    await callback.answer()


@router.callback_query(F.data == "adm_rev_gen_mod_start")
async def adm_rev_gen_mod_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_admin_review_moderation_generated(callback, 0, state)
    await callback.answer()


@router.callback_query(F.data == "adm_rev_gen_export_json")
async def adm_rev_gen_export_json(callback: CallbackQuery):
    payload = ReviewsService.export_last_batch_texts()
    rows = payload.get("reviews", []) if isinstance(payload, dict) else []
    if not rows:
        await callback.answer("Последняя волна пуста", show_alert=True)
        return

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    file = BufferedInputFile(body.encode("utf-8"), filename="generated_reviews_batch.json")
    await callback.message.answer_document(
        file,
        caption=(
            "📤 Выгрузка последней волны готова.\n"
            "Можно менять поля `text`, `district` и при необходимости `item_info`, затем загрузите файл обратно кнопкой '📥 Загрузить JSON'."
        )
    )
    await callback.answer("JSON отправлен")


@router.callback_query(F.data == "adm_rev_gen_import_json")
async def adm_rev_gen_import_json_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_rev_gen_import_json)
    await callback.message.edit_text(
        "📥 Отправьте JSON-файл с правками последней волны.\n\n"
        "Формат: список объектов с `id` и редактируемыми полями или объект с полем `reviews`.\n"
        "Поддерживаются поля `text`, `district` и `item_info`.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_gen_import_cancel")]]
        )
    )
    await callback.answer()


@router.callback_query(F.data == "adm_rev_gen_import_cancel")
async def adm_rev_gen_import_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.message.edit_text(
        "⭐️ <b>Управление отзывами</b>\nЧто хотите сделать?",
        reply_markup=kb.get_admin_reviews_menu()
    )
    await callback.answer("Отменено")


@router.message(AdminState.waiting_for_rev_gen_import_json, F.document)
async def adm_rev_gen_import_json_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc:
        await message.answer("🔶 Пришлите JSON-файл документом.")
        return

    try:
        telegram_file = await message.bot.get_file(doc.file_id)
        buffer = BytesIO()
        await message.bot.download_file(telegram_file.file_path, destination=buffer)
        raw = buffer.getvalue().decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        await message.answer("🔶 Не удалось прочитать JSON. Проверьте формат и кодировку UTF-8.")
        return

    result = ReviewsService.apply_last_batch_texts(payload)
    await state.set_state(None)
    await message.answer(
        "✅ Импорт завершен.\n"
        f"Обновлено: {result.get('updated', 0)}\n"
        f"Пропущено: {result.get('skipped', 0)}\n"
        f"Ошибок строк: {result.get('invalid', 0)}"
    )
    await message.answer("⭐️ Меню отзывов:", reply_markup=kb.get_admin_reviews_menu())


@router.message(AdminState.waiting_for_rev_gen_import_json)
async def adm_rev_gen_import_json_text_hint(message: Message):
    await message.answer("🔶 Нужен именно JSON-файл документом, не текстовым сообщением.")


@router.callback_query(F.data.startswith("adm_rev_mod_nav_"))
async def adm_rev_mod_nav(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("adm_rev_mod_nav_"):]
    try:
        index = int(raw)
    except ValueError:
        index = 0
    await _show_admin_review_moderation(callback, index, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rev_gen_mod_nav_"))
async def adm_rev_gen_mod_nav(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("adm_rev_gen_mod_nav_"):]
    try:
        index = int(raw)
    except ValueError:
        index = 0
    await _show_admin_review_moderation_generated(callback, index, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rev_mod_hide_"))
async def adm_rev_mod_hide(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_hide_"):]
    ReviewsService.set_review_hidden(review_id, True)
    await _resume_review_moderation(callback, state)
    await callback.answer("Отзыв скрыт")


@router.callback_query(F.data.startswith("adm_rev_mod_unhide_"))
async def adm_rev_mod_unhide(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_unhide_"):]
    ReviewsService.set_review_hidden(review_id, False)
    await _resume_review_moderation(callback, state)
    await callback.answer("Отзыв показан")


@router.callback_query(F.data.startswith("adm_rev_mod_stars_"))
async def adm_rev_mod_stars(callback: CallbackQuery, state: FSMContext):
    raw = (callback.data or "")[len("adm_rev_mod_stars_"):]
    if "_" not in raw:
        await callback.answer("Не удалось изменить оценку", show_alert=True)
        return
    review_id, stars_raw = raw.rsplit("_", 1)
    try:
        stars = int(stars_raw)
    except ValueError:
        await callback.answer("Неверная оценка", show_alert=True)
        return
    if not ReviewsService.update_review_stars(review_id, stars):
        await callback.answer("Отзыв не найден", show_alert=True)
        return
    await _resume_review_moderation(callback, state)
    await callback.answer(f"Оценка: {ReviewsService.normalize_stars(stars)}★")


@router.callback_query(F.data.startswith("adm_rev_mod_del_"))
async def adm_rev_mod_delete(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_del_"):]
    ReviewsService.delete_review(review_id)
    await _resume_review_moderation(callback, state)
    await callback.answer("Отзыв удален")


@router.callback_query(F.data.startswith("adm_rev_mod_edit_"))
async def adm_rev_mod_edit(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_edit_"):]
    await state.update_data(adm_rev_mod_edit_id=review_id)
    await state.set_state(AdminState.waiting_for_rev_mod_edit_text)
    await callback.message.edit_text(
        "✏️ Введите новый текст отзыва одним сообщением:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_mod_cancel_edit")]])
    )
    await callback.answer()


@router.callback_query(F.data == "adm_rev_mod_cancel_edit")
async def adm_rev_mod_cancel_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _resume_review_moderation(callback, state)
    await callback.answer()


@router.message(AdminState.waiting_for_rev_mod_edit_text)
async def adm_rev_mod_edit_text(message: Message, state: FSMContext):
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("🔶 Текст не может быть пустым.")
        return

    data = await state.get_data()
    review_id = data.get("adm_rev_mod_edit_id")
    if not review_id:
        await message.answer("🔶 Отзыв не найден.")
        await state.set_state(None)
        return

    ReviewsService.update_review_text(review_id, new_text)
    await message.answer("✅ Текст отзыва обновлен.")
    await state.set_state(None)
    await _resume_review_moderation(message, state)


@router.callback_query(F.data.startswith("adm_rev_mod_dups_"))
async def adm_rev_mod_dups(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_dups_"):]
    matches = ReviewsService.find_similar_reviews(review_id)

    lines = ["🔎 <b>Похожие отзывы</b>"]
    if not matches:
        lines.append("\nПохожих отзывов не найдено.")
    else:
        for idx, item in enumerate(matches, start=1):
            ratio = item.get("ratio", 0.0)
            status = "🙈" if item.get("hidden") else "👁️"
            snippet = _trim_review_text(item.get("text", ""), 140)
            lines.append(
                f"\n{idx}. {status} <b>{ratio:.0%}</b> - {item.get('display_date', '')}"
                f"\n🛍 {item.get('item_info', '')}"
                f"\n🗣 {snippet}"
            )

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_rev_mod_back")]])

    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "adm_rev_mod_back")
async def adm_rev_mod_back(callback: CallbackQuery, state: FSMContext):
    await _resume_review_moderation(callback, state)
    await callback.answer()

# --- MANUAL REVIEWS ---

MANUAL_CITY_PAGE_SIZE = 12


def _build_manual_city_markup(options: list[dict[str, str]], page: int, page_size: int) -> InlineKeyboardMarkup:
    total = len(options)
    if total <= 0:
        # Fallback keyboard with only cancel button
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")]]
        )

    page_size = max(1, page_size)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_slice = options[start:start + page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for offset, option in enumerate(page_slice):
        label = option.get("display") or option.get("city_key") or "-"
        idx = start + offset
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_rev_city_{idx}")])

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_rev_citypage_{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_rev_citypage_{page + 1}"))
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_manual_review_mode_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Обычный отзыв", callback_data="adm_rev_mode_regular")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")],
    ])


def _normalize_product_base_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\s*(\d+(?:[\.,]\d+)?)\s*г\.?", "", name, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()

@router.callback_query(F.data == 'adm_rev_manual_start')
async def adm_rev_manual_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(
        rev_nick=ReviewsService.HIDDEN_USERNAME,
        manual_city_options=None,
        manual_city_page=0,
        manual_city_page_size=MANUAL_CITY_PAGE_SIZE,
        rev_mode=None,
        rev_prod=None,
        rev_city=None,
        rev_dist=None,
    )
    await state.set_state(AdminState.waiting_for_rev_mode)
    await callback.message.edit_text(
        "✍️ <b>Добавление отзыва</b>\n\n1. Выберите тип отзыва:",
        reply_markup=_build_manual_review_mode_markup()
    )


@router.callback_query(F.data == 'adm_rev_mode_regular', AdminState.waiting_for_rev_mode)
async def adm_rev_mode_regular(callback: CallbackQuery, state: FSMContext):
    city_options = ReviewsService.get_manual_city_options()
    if not city_options:
        await callback.message.edit_text(
            "🔶 Не нашёл городов с готовым каталогом и районами. Сначала выберите город через пользовательский поток или обновите кэши.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="admin_reviews_menu")]])
        )
        await callback.answer()
        return

    await state.update_data(
        rev_mode="regular",
        manual_city_options=city_options,
        manual_city_page=0,
        manual_city_page_size=MANUAL_CITY_PAGE_SIZE,
    )
    await state.set_state(AdminState.waiting_for_rev_city)
    await callback.message.edit_text(
        "✍️ <b>Добавление обычного отзыва</b>\n\n2. Выберите <b>Город</b>:",
        reply_markup=_build_manual_city_markup(city_options, 0, MANUAL_CITY_PAGE_SIZE)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('adm_rev_citypage_'), AdminState.waiting_for_rev_city)
async def adm_rev_city_page(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    options = data.get("manual_city_options") or []
    if not options:
        await callback.answer("Список городов недоступен.", show_alert=True)
        return

    raw_page = callback.data[len('adm_rev_citypage_'):]
    try:
        requested_page = int(raw_page)
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    page_size = data.get("manual_city_page_size") or MANUAL_CITY_PAGE_SIZE
    markup = _build_manual_city_markup(options, requested_page, page_size)
    await state.update_data(manual_city_page=requested_page, manual_city_page_size=page_size)

    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        # Сообщение могло быть изменено или удалено
        pass

    await callback.answer()


@router.callback_query(F.data.startswith('adm_rev_city_'), AdminState.waiting_for_rev_city)
async def adm_rev_city(callback: CallbackQuery, state: FSMContext):
    idx_raw = callback.data.split("_", 3)[3]
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer("Некорректный выбор города.", show_alert=True)
        return

    data = await state.get_data()
    options = data.get("manual_city_options") or []
    if idx < 0 or idx >= len(options):
        await callback.answer("Город недоступен.", show_alert=True)
        return

    option = options[idx]
    city_display = option.get("display") or option.get("city_key") or "-"
    city_key = option.get("city_key") or city_display.lower().strip()

    await state.update_data(
        rev_city=city_display,
        rev_city_key=city_key,
        manual_city_options=None,
        manual_city_page=None,
        manual_city_page_size=None,
        manual_rev_dist_map=None
    )
    
    # Show products for this city
    products = CatalogService.get_products_for_city(city_display)
    if not products:
        await callback.answer("В этом городе нет активных товаров. Выберите другой вариант.", show_alert=True)
        return
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=p['name'], callback_data=f"adm_rev_prod_{p['id']}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")])

    await state.set_state(AdminState.waiting_for_rev_prod)
    await callback.message.edit_text(
        f"3. Город: {city_display}\nВыберите <b>Товар</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )



@router.callback_query(F.data.startswith('adm_rev_prod_'), AdminState.waiting_for_rev_prod)
async def adm_rev_prod(callback: CallbackQuery, state: FSMContext):
    p_id = callback.data.split("_", 3)[3]
    data = await state.get_data()
    city = data['rev_city']
    
    # Store product name for display
    prod = CatalogService.get_product_by_id(p_id, city)
    p_name = prod['name'] if prod else "Unknown"
    
    await state.update_data(rev_prod=p_name)
    
    # Show districts
    districts, _ = GeoService.get_districts_for_city(city)
    if not districts:
        districts = ["Центр"]

    dist_map = {str(i): d for i, d in enumerate(districts)}
    await state.update_data(manual_rev_dist_map=dist_map)

    buttons = []
    for idx, d in dist_map.items():
        buttons.append([InlineKeyboardButton(text=d, callback_data=f"adm_rev_dist_{idx}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")])

    await state.set_state(AdminState.waiting_for_rev_dist)
    await callback.message.edit_text(
        f"4. Товар: {p_name}\nВыберите <b>Район</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith('adm_rev_dist_'), AdminState.waiting_for_rev_dist)
async def adm_rev_dist(callback: CallbackQuery, state: FSMContext):
    # District might have spaces
    prefix = "adm_rev_dist_"
    idx = callback.data[len(prefix):]
    data = await state.get_data()
    dist_map = data.get("manual_rev_dist_map") or {}
    dist = dist_map.get(idx)
    if not dist:
        await callback.answer("Район недоступен.", show_alert=True)
        return

    await state.update_data(rev_dist=dist)
    
    await state.set_state(AdminState.waiting_for_rev_text)
    await callback.message.edit_text(
        f"5. Район: {dist}\n\n👇 Введите <b>Текст отзыва</b>:"
    )

@router.message(AdminState.waiting_for_rev_text)
async def adm_rev_final(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("🔶 Текст отзыва не может быть пустым.")
        return
    data = await state.get_data()
    city = data.get("rev_city") or "Не указан"
    product = data.get("rev_prod") or "Не указан"
    district = data.get("rev_dist") or "Центр"
    
    ReviewsService.add_manual_review(
        data['rev_nick'],
        city,
        product,
        district,
        text
    )
    
    await message.answer(
        f"✅ Отзыв добавлен!\n\n"
        f"Тип: Обычный\n"
        f"Город: {city}\n"
        f"Позиция: {product}\n"
        f"Текст: {text}"
    )
    
    # Return to menu
    await state.clear()
    await message.answer("⭐️ Меню отзывов:", reply_markup=kb.get_admin_reviews_menu())

# --- MANAGE PRODUCT DISTRICTS ---

async def _show_city_product_districts_list(callback: CallbackQuery, city_name: str):
    products = CatalogService.get_existing_products_for_city(city_name)
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"⚙️ {p['name']}", callback_data=f"adm_pd_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    await callback.message.edit_text(
        f"🏙 <b>{city_name}: Настройка районов</b>\nВыберите товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


async def _show_city_miniopt_product_districts_list(callback: CallbackQuery, city_name: str):
    products = CatalogService.get_miniopt_products_for_city(city_name)
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"⚙️ {p['name']}", callback_data=f"adm_miniopt_pd_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_miniopt_back_{city_name}")])
    await callback.message.edit_text(
        f"🧺 <b>{city_name}: Настройка районов miniopt</b>\nВыберите товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith('adm_pd_list_'))
async def adm_pd_list(callback: CallbackQuery):
    city_name = callback.data[len('adm_pd_list_'):]
    await _show_city_product_districts_list(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_pd_list_'))
async def adm_miniopt_pd_list(callback: CallbackQuery):
    city_name = callback.data[len('adm_miniopt_pd_list_'):]
    await _show_city_miniopt_product_districts_list(callback, city_name)

@router.callback_query(F.data.startswith('adm_pd_edit_'))
async def adm_pd_edit(callback: CallbackQuery, state: FSMContext):
    # data: adm_pd_edit_CITY&PID
    content = callback.data[len('adm_pd_edit_'):]
    city_name, p_id = content.split('&', 1)
    
    # Load available districts for city
    all_d, _ = GeoService.get_districts_for_city(city_name)
    if not all_d: all_d = []
    
    # Load currently assigned districts
    assigned = CatalogService.get_product_districts(city_name, p_id)
    # If None -> means "ALL" strictly or "DEFAULT"? 
    # Logic in user flow: if explicit_districts is None -> Fallback to random subset of ALL.
    # If we want to support "Select All", we can treat "All in list" as assigned.
    # If assigned is None, let's treat it as empty or 'All'?
    # To facilitate editing, if it's None, let's pre-select NONE (so user must enable districts).
    # OR pre-select ALL?
    if assigned is None:
        assigned = [] # Start blank
        
    await state.update_data(pd_city=city_name, pd_pid=p_id, pd_sel=assigned, pd_scope='regular')
    
    # Store mapping for optimized callbacks
    dist_map = {str(i): d for i, d in enumerate(all_d)}
    await state.update_data(pd_dist_map=dist_map)
    
    await _show_pd_editor(callback, city_name, p_id, all_d, assigned)


@router.callback_query(F.data.startswith('adm_miniopt_pd_edit_'))
async def adm_miniopt_pd_edit(callback: CallbackQuery, state: FSMContext):
    content = callback.data[len('adm_miniopt_pd_edit_'):]
    city_name, p_id = content.split('&', 1)

    all_d, _ = GeoService.get_districts_for_city(city_name)
    if not all_d:
        all_d = []

    assigned = CatalogService.get_miniopt_product_districts(city_name, p_id)
    if assigned is None:
        assigned = []

    await state.update_data(pd_city=city_name, pd_pid=p_id, pd_sel=assigned, pd_scope='miniopt')
    dist_map = {str(i): d for i, d in enumerate(all_d)}
    await state.update_data(pd_dist_map=dist_map)

    await _show_miniopt_pd_editor(callback, city_name, p_id, all_d, assigned)

async def _show_pd_editor(callback, city_name, p_id, all_d, assigned):
    # Use helper from kb
    mk = kb.get_admin_product_districts_selector(city_name, p_id, all_d, assigned)
    prod = CatalogService.get_product_by_id(p_id, city_name)
    prod_name = prod['name'] if prod else "???"
    
    try:
        await callback.message.edit_text(
            f"⚙️ <b>{prod_name}</b> ({city_name})\n\n"
            f"Выберите районы, где ЕСТЬ товар (отметьте ✅).\n"
            f"Если список пуст -> используется RANDOMLY сгенерированное наличие.",
            reply_markup=mk
        )
    except Exception as e:
        print(f"Edit error: {e}")
        # Fallback if message too long or other error?
        # Maybe restart menu
        await callback.answer("Ошибка обновления меню")


async def _show_miniopt_pd_editor(callback, city_name, p_id, all_d, assigned):
    mk = kb.get_admin_miniopt_product_districts_selector(city_name, p_id, all_d, assigned)
    prod = CatalogService.get_miniopt_product_by_id(p_id, city_name)
    prod_name = prod['name'] if prod else "???"

    try:
        await callback.message.edit_text(
            f"🧺 <b>{prod_name}</b> ({city_name})\n\n"
            f"Выберите районы, где ЕСТЬ miniopt-товар (отметьте ✅).\n"
            f"Если список пуст -> используется RANDOMLY сгенерированное наличие.",
            reply_markup=mk
        )
    except Exception as e:
        print(f"Miniopt edit error: {e}")
        await callback.answer("Ошибка обновления меню")

@router.callback_query(F.data.startswith('adm_pd_tog_'))
async def adm_pd_toggle(callback: CallbackQuery, state: FSMContext):
    # adm_pd_tog_{INDEX}
    idx = callback.data[len('adm_pd_tog_'):]
    
    data = await state.get_data()
    city_name = data.get('pd_city')
    p_id = data.get('pd_pid')
    dist_map = data.get('pd_dist_map', {})
    
    district = dist_map.get(idx)
    if not district:
        await callback.answer("Ошибка: район не найден")
        return
        
    current_sel = data.get('pd_sel', [])
    if district in current_sel:
        current_sel.remove(district)
    else:
        current_sel.append(district)
        
    await state.update_data(pd_sel=current_sel)
    
    # Refresh
    all_d, _ = GeoService.get_districts_for_city(city_name)
    if data.get('pd_scope') == 'miniopt':
        await _show_miniopt_pd_editor(callback, city_name, p_id, all_d, current_sel)
    else:
        await _show_pd_editor(callback, city_name, p_id, all_d, current_sel)

@router.callback_query(F.data.startswith('adm_pd_save_'))
async def adm_pd_save(callback: CallbackQuery, state: FSMContext):
    content = callback.data[len('adm_pd_save_'):]
    city_name, p_id = content.split('&', 1)
    
    data = await state.get_data()
    selected = data.get('pd_sel', [])
    
    # Save
    CatalogService.set_product_districts(city_name, p_id, selected)
    
    await callback.answer("✅ Сохранено!")
    await _show_city_product_districts_list(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_pd_save_'))
async def adm_miniopt_pd_save(callback: CallbackQuery, state: FSMContext):
    content = callback.data[len('adm_miniopt_pd_save_'):]
    city_name, p_id = content.split('&', 1)

    data = await state.get_data()
    selected = data.get('pd_sel', [])

    CatalogService.set_miniopt_product_districts(city_name, p_id, selected)

    await callback.answer("✅ Сохранено!")
    await _show_city_miniopt_product_districts_list(callback, city_name)


@router.callback_query(F.data.startswith('adm_pd_back_'))
async def adm_pd_back(callback: CallbackQuery):
    city_name = callback.data[len("adm_pd_back_"):]
    await _show_city_product_districts_list(callback, city_name)


@router.callback_query(F.data.startswith('adm_miniopt_pd_back_'))
async def adm_miniopt_pd_back(callback: CallbackQuery):
    city_name = callback.data[len("adm_miniopt_pd_back_"):]
    await _show_city_miniopt_product_districts_list(callback, city_name)


@router.callback_query(F.data == 'admin_stash_types_menu')
async def admin_stash_types_menu(callback: CallbackQuery, state: FSMContext):
    await _show_global_stash_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_miniopt_stash_types_menu')
async def admin_miniopt_stash_types_menu(callback: CallbackQuery, state: FSMContext):
    await _show_global_stash_menu(callback, state, scope='miniopt')
    await callback.answer()


@router.callback_query(F.data == 'adm_global_stash_add')
async def adm_global_stash_add(callback: CallbackQuery, state: FSMContext):
    await state.update_data(stash_registry_scope='regular')
    await state.set_state(AdminState.waiting_for_stash_type_add)
    await callback.message.edit_text(
        "➕ Введите название нового типа клада и отправьте сообщением.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_stash_types_menu")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == 'adm_global_miniopt_stash_add')
async def adm_global_miniopt_stash_add(callback: CallbackQuery, state: FSMContext):
    await state.update_data(stash_registry_scope='miniopt')
    await state.set_state(AdminState.waiting_for_stash_type_add)
    await callback.message.edit_text(
        "➕ Введите название нового типа клада для miniopt и отправьте сообщением.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_miniopt_stash_types_menu")]
        ])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_stash_type_add)
async def admin_stash_type_add_message(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    data = await state.get_data()
    scope = data.get('stash_registry_scope', 'regular')
    add_method = CatalogService.add_miniopt_stash_type if scope == 'miniopt' else CatalogService.add_stash_type
    try:
        added = await asyncio.to_thread(add_method, value)
    except Exception:
        logger.exception("Failed to add stash type")
        await message.answer("🔶 Не удалось сохранить тип. Попробуйте ещё раз.")
        return
    if added:
        await message.answer("✅ Тип добавлен.")
        await _show_global_stash_menu(message, state, scope=scope)
    else:
        await message.answer(
            "🔶 Не получилось добавить тип. Он пустой, уже существует или после нормализации совпадает с существующим типом."
        )


@router.callback_query(F.data.startswith('adm_global_stash_del_'))
async def adm_global_stash_delete(callback: CallbackQuery, state: FSMContext):
    index_str = callback.data[len('adm_global_stash_del_'):]
    try:
        idx = int(index_str)
    except ValueError:
        await callback.answer("Ошибка номера.", show_alert=True)
        return
    types = CatalogService.get_available_stash_types()
    if idx < 0 or idx >= len(types):
        await callback.answer("Тип не найден.", show_alert=True)
        return
    if len(types) <= 2:
        await callback.answer("🔶 Нельзя удалить: требуется минимум два типа.", show_alert=True)
        return
    target = types[idx]
    await _safe_callback_answer(callback)
    try:
        removed = await asyncio.to_thread(CatalogService.remove_stash_type, target)
    except Exception:
        logger.exception("Failed to remove stash type")
        await callback.message.answer("🔶 Не удалось удалить тип. Попробуйте ещё раз.")
        return
    if not removed:
        await callback.message.answer("🔶 Нельзя удалить: требуется минимум два типа.")
    await _show_global_stash_menu(callback, state)


@router.callback_query(F.data.startswith('adm_global_miniopt_stash_del_'))
async def adm_global_miniopt_stash_delete(callback: CallbackQuery, state: FSMContext):
    index_str = callback.data[len('adm_global_miniopt_stash_del_'):]
    try:
        idx = int(index_str)
    except ValueError:
        await callback.answer("Ошибка номера.", show_alert=True)
        return
    types = CatalogService.get_available_miniopt_stash_types()
    if idx < 0 or idx >= len(types):
        await callback.answer("Тип не найден.", show_alert=True)
        return
    if len(types) <= 2:
        await callback.answer("🔶 Нельзя удалить: требуется минимум два типа.", show_alert=True)
        return
    target = types[idx]
    await _safe_callback_answer(callback)
    try:
        removed = await asyncio.to_thread(CatalogService.remove_miniopt_stash_type, target)
    except Exception:
        logger.exception("Failed to remove miniopt stash type")
        await callback.message.answer("🔶 Не удалось удалить тип. Попробуйте ещё раз.")
        return
    if not removed:
        await callback.message.answer("🔶 Нельзя удалить: требуется минимум два типа.")
    await _show_global_stash_menu(callback, state, scope='miniopt')


@router.callback_query(F.data.startswith('adm_stash_menu_'))
async def adm_stash_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_stash_menu_'):]
    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"🏷 <b>{city_name}:</b> настройки типов кладов\nВыберите товар:",
        reply_markup=kb.get_admin_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_miniopt_stash_menu_'))
async def adm_miniopt_stash_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_miniopt_stash_menu_'):]
    products = CatalogService.get_miniopt_products_for_city(city_name)
    await callback.message.edit_text(
        f"🧺 <b>{city_name}:</b> настройки типов кладов miniopt\nВыберите товар:",
        reply_markup=kb.get_admin_miniopt_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_stash_edit_'))
async def adm_stash_edit(callback: CallbackQuery, state: FSMContext):
    payload = callback.data[len('adm_stash_edit_'):]
    city_name, product_id = payload.split('&', 1)
    all_types = list(CatalogService.get_available_stash_types())
    selected = CatalogService.get_product_stash_types(product_id)
    idx_map = {str(i): item for i, item in enumerate(all_types)}
    await state.update_data(
        stash_city=city_name,
        stash_pid=product_id,
        stash_selected=list(selected),
        stash_all_types=all_types,
        stash_type_map=idx_map,
        stash_scope='regular'
    )
    await state.set_state(AdminState.editing_product_stash)
    await _show_stash_editor(callback, city_name, product_id, all_types, selected)


@router.callback_query(F.data.startswith('adm_miniopt_stash_edit_'))
async def adm_miniopt_stash_edit(callback: CallbackQuery, state: FSMContext):
    payload = callback.data[len('adm_miniopt_stash_edit_'):]
    city_name, product_id = payload.split('&', 1)
    all_types = list(CatalogService.get_available_miniopt_stash_types())
    selected = CatalogService.get_miniopt_product_stash_types(product_id)
    idx_map = {str(i): item for i, item in enumerate(all_types)}
    await state.update_data(
        stash_city=city_name,
        stash_pid=product_id,
        stash_selected=list(selected),
        stash_all_types=all_types,
        stash_type_map=idx_map,
        stash_scope='miniopt'
    )
    await state.set_state(AdminState.editing_product_stash)
    await _show_miniopt_stash_editor(callback, city_name, product_id, all_types, selected)


async def _show_stash_editor(callback: CallbackQuery, city_name: str, product_id: str, all_types: list[str], selected: list[str]):
    product = CatalogService.get_product_by_id(product_id, city_name)
    product_name = product['name'] if product else product_id
    text = (
        f"🏷 <b>{product_name}</b> ({city_name})\n\n"
        "Отметьте ровно два типа кладов, которые будут доступны покупателю."
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.get_admin_stash_editor(city_name, product_id, all_types, selected)
        )
    except Exception as e:
        print(f"Stash editor render error: {e}")
        await callback.answer("Не удалось обновить меню", show_alert=True)


async def _show_miniopt_stash_editor(callback: CallbackQuery, city_name: str, product_id: str, all_types: list[str], selected: list[str]):
    product = CatalogService.get_miniopt_product_by_id(product_id, city_name)
    product_name = product['name'] if product else product_id
    text = (
        f"🧺 <b>{product_name}</b> ({city_name})\n\n"
        "Отметьте ровно два типа кладов miniopt, которые будут доступны покупателю."
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.get_admin_miniopt_stash_editor(city_name, product_id, all_types, selected)
        )
    except Exception as e:
        print(f"Miniopt stash editor render error: {e}")
        await callback.answer("Не удалось обновить меню", show_alert=True)


@router.callback_query(F.data.startswith('adm_stash_toggle_'), AdminState.editing_product_stash)
async def adm_stash_toggle(callback: CallbackQuery, state: FSMContext):
    idx = callback.data[len('adm_stash_toggle_'):]
    data = await state.get_data()
    all_types = data.get('stash_all_types', list(CatalogService.get_available_stash_types()))
    type_map = data.get('stash_type_map', {str(i): item for i, item in enumerate(all_types)})
    target = type_map.get(idx)
    if not target:
        await callback.answer("Тип не найден", show_alert=True)
        return

    selected = list(data.get('stash_selected', []))
    if target in selected:
        selected.remove(target)
    else:
        if len(selected) >= 2:
            await callback.answer("Допустимо только два типа. Снимите лишний.", show_alert=True)
            return
        selected.append(target)

    await state.update_data(stash_selected=selected)
    if data.get('stash_scope') == 'miniopt':
        await _show_miniopt_stash_editor(callback, data.get('stash_city', ''), data.get('stash_pid', ''), all_types, selected)
    else:
        await _show_stash_editor(callback, data.get('stash_city', ''), data.get('stash_pid', ''), all_types, selected)


@router.callback_query(F.data.startswith('adm_miniopt_stash_toggle_'), AdminState.editing_product_stash)
async def adm_miniopt_stash_toggle(callback: CallbackQuery, state: FSMContext):
    await adm_stash_toggle(callback, state)


@router.callback_query(F.data.startswith('adm_stash_save_'), AdminState.editing_product_stash)
async def adm_stash_save(callback: CallbackQuery, state: FSMContext):
    payload = callback.data[len('adm_stash_save_'):]
    city_name, product_id = payload.split('&', 1)
    data = await state.get_data()
    selected = list(data.get('stash_selected', []))
    if len(selected) != 2:
        await callback.answer("Нужно выбрать ровно два типа.", show_alert=True)
        return

    success = CatalogService.set_product_stash_types(product_id, selected)
    if success:
        await callback.answer("✅ Сохранено")
    else:
        await callback.answer("Не удалось сохранить", show_alert=True)

    await state.set_state(None)
    await state.update_data(stash_selected=None, stash_pid=None, stash_city=None, stash_all_types=None, stash_type_map=None, stash_scope=None)

    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"🏷 <b>{city_name}:</b> настройки типов кладов\nВыберите товар:",
        reply_markup=kb.get_admin_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_miniopt_stash_save_'), AdminState.editing_product_stash)
async def adm_miniopt_stash_save(callback: CallbackQuery, state: FSMContext):
    payload = callback.data[len('adm_miniopt_stash_save_'):]
    city_name, product_id = payload.split('&', 1)
    data = await state.get_data()
    selected = list(data.get('stash_selected', []))
    if len(selected) != 2:
        await callback.answer("Нужно выбрать ровно два типа.", show_alert=True)
        return

    success = CatalogService.set_miniopt_product_stash_types(product_id, selected)
    if success:
        await callback.answer("✅ Сохранено")
    else:
        await callback.answer("Не удалось сохранить", show_alert=True)

    await state.set_state(None)
    await state.update_data(stash_selected=None, stash_pid=None, stash_city=None, stash_all_types=None, stash_type_map=None, stash_scope=None)

    products = CatalogService.get_miniopt_products_for_city(city_name)
    await callback.message.edit_text(
        f"🧺 <b>{city_name}:</b> настройки типов кладов miniopt\nВыберите товар:",
        reply_markup=kb.get_admin_miniopt_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_stash_back_'))
async def adm_stash_back(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data[len('adm_stash_back_'):]
    await state.set_state(None)
    await state.update_data(stash_selected=None, stash_pid=None, stash_city=None, stash_all_types=None, stash_type_map=None, stash_scope=None)
    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"🏷 <b>{city_name}:</b> настройки типов кладов\nВыберите товар:",
        reply_markup=kb.get_admin_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_miniopt_stash_back_'))
async def adm_miniopt_stash_back(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data[len('adm_miniopt_stash_back_'):]
    await state.set_state(None)
    await state.update_data(stash_selected=None, stash_pid=None, stash_city=None, stash_all_types=None, stash_type_map=None, stash_scope=None)
    products = CatalogService.get_miniopt_products_for_city(city_name)
    await callback.message.edit_text(
        f"🧺 <b>{city_name}:</b> настройки типов кладов miniopt\nВыберите товар:",
        reply_markup=kb.get_admin_miniopt_stash_product_list(city_name, products)
    )

@router.callback_query(F.data == 'admin_rev_add')
async def admin_rev_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_review_phrase)
    await callback.message.edit_text(
        "➕ Введите новую фразу или слово:\n"
        "(Например: 'В касание', 'четко', 'магазин огонь')\n\n"
        "Для отмены нажмите /admin и вернитесь.",
        reply_markup=None 
    )

@router.message(AdminState.waiting_for_review_phrase)
async def admin_rev_save(message: Message, state: FSMContext):
    phrase = message.text.strip()
    if phrase:
        if ReviewsService.add_custom_prompt(phrase):
            await message.answer(f"✅ Фраза '{phrase}' добавлена!")
        else:
            await message.answer("🔶 Такая фраза уже есть или ошибка.")
            
    await state.clear()
    # Возвращаем меню. Придется отправить новым сообщением, так как edit не сработает на user message
    # Но лучше, если админ нажмет кнопку или команду.
    # Для удобства покажем снова панель
    await cmd_admin_panel(message, state) # Вернет в корень, придется кликать снова.
    # Или можно симулировать callback, но это сложно. 
    # Просто напишем что делать.

@router.callback_query(F.data == 'admin_rev_del')
async def admin_rev_del(callback: CallbackQuery, state: FSMContext):
    phrases = ReviewsService.get_custom_prompts()
    if not phrases:
        await callback.answer("Список пуст", show_alert=True)
        return
        
    text = "🗑 Выберите номер фразы для удаления:\n\n"
    for i, p in enumerate(phrases):
        text += f"{i+1}. {p}\n"
        
    await state.set_state(AdminState.waiting_for_review_del_idx)
    await callback.message.edit_text(text + "\nНапишите номер цифрой:")

@router.message(AdminState.waiting_for_review_del_idx)
async def admin_rev_del_do(message: Message, state: FSMContext):
    idx_str = message.text.strip()
    phrases = ReviewsService.get_custom_prompts()
    
    if idx_str.isdigit():
        idx = int(idx_str) - 1
        if 0 <= idx < len(phrases):
            todel = phrases[idx]
            if ReviewsService.remove_custom_prompt(todel):
                await message.answer(f"✅ Удалено: {todel}")
            else:
                await message.answer("Ошибка удаления.")
        else:
            await message.answer("Неверный номер.")
    else:
        await message.answer("Нужно число.")
        
    await state.clear()
    await cmd_admin_panel(message, state)


@commands_router.message(Command('reset_reviews'))
async def cmd_reset_reviews(message: Message):
    if not is_admin(message.from_user.id):
        return

    msg = await message.answer("⏳ Пересоздаю отзывы на основе активных городов...")
    new_reviews = ReviewsService.force_regenerate()
    count = len(new_reviews)
    
    await msg.edit_text(f"✅ <b>Отзывы обновлены!</b>\n"
                        f"Сгенерировано {count} шт. на основе истории посещений и доступных товаров.")

@commands_router.message(Command('reset_catalog'))
async def cmd_reset_catalog(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите название города.\nПример: /reset_catalog Томск")
        return
        
    city_name = args[1]
    if CatalogService.force_refresh_products(city_name):
        await message.answer(f"✅ Каталог для города <b>{city_name}</b> сброшен.\nПри следующем запросе сформируется новый список товаров (от 6 до 10 позиций).")
    else:
        await message.answer(f"🔶 Город <b>{city_name}</b> не найден в кэше прайс-листов (или уже был пуст).")

@commands_router.message(Command('clear_cache'))
async def cmd_clear_all_cache(message: Message):
    if not is_admin(message.from_user.id):
        return

    status_msg = await message.answer("⏳ Сбрасываю кэш товаров...")
    try:
        (
            c_cat,
            c_inventory,
            c_stash,
            c_miniopt,
            c_miniopt_inv,
            c_miniopt_stash,
        ) = CatalogService.clear_entire_cache()
        CatalogService.resync_product_stash_types(force_rebalance=True)
    except Exception:
        logger.exception("clear_cache failed")
        await status_msg.edit_text("🔶 Не удалось сбросить кэш товаров. Попробуйте ещё раз.")
        return

    def _line(ok_text: str, empty_text: str, fail_text: str, status: str) -> str:
        if status in {"removed", "cleared"}:
            return f"✅ {ok_text}\n"
        if status == "missing":
            return f"ℹ️ {empty_text}\n"
        return f"🔶 {fail_text}\n"

    msg = "♻️ <b>Сброс кэша товаров выполнен!</b>\n\n"
    msg += _line(
        "Кэш товаров удален (будет сгенерирован заново).",
        "Кэш товаров уже был пуст.",
        "Кэш товаров не удалось удалить.",
        c_cat,
    )
    msg += _line(
        "Инвентарь товаров удален (привязки к районам сброшены).",
        "Инвентарь товаров уже был пуст.",
        "Инвентарь товаров не удалось удалить.",
        c_inventory,
    )
    msg += _line(
        "Пары типов кладов сброшены (будут сгенерированы заново).",
        "Пары типов кладов уже были пусты.",
        "Пары типов кладов не удалось удалить.",
        c_stash,
    )
    msg += _line(
        "Кэш миниопта удален.",
        "Кэш миниопта уже был пуст.",
        "Кэш миниопта не удалось удалить.",
        c_miniopt,
    )
    msg += _line(
        "Инвентарь миниопта удален.",
        "Инвентарь миниопта уже был пуст.",
        "Инвентарь миниопта не удалось удалить.",
        c_miniopt_inv,
    )
    msg += _line(
        "Типы кладов миниопта сброшены.",
        "Типы кладов миниопта уже были пусты.",
        "Типы кладов миниопта не удалось удалить.",
        c_miniopt_stash,
    )
    msg += "✅ Пары типов кладов обновлены по текущему списку.\n"
    msg += "ℹ️ Списки товаров в городах пересоберутся при следующем открытии города.\n"
    msg += "ℹ️ Кэш районов/улиц сохранён (используйте /clear_geo_cache чтобы сбросить его отдельно)."

    await status_msg.edit_text(msg)


@commands_router.message(Command('clear_geo_cache'))
async def cmd_clear_geo_cache(message: Message):
    """Сброс кэша районов/улиц: весь кэш или только выбранный город.

    Примеры:
    /clear_geo_cache
    /clear_geo_cache Пермь
    """
    if not is_admin(message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) == 1:
        c_geo = GeoService.clear_districts_cache()
        if c_geo:
            await message.answer("✅ Кэш районов/улиц удалён полностью. При следующем запросе города данные будут получены заново.")
        else:
            await message.answer("🔶 Кэш районов пуст или не удалился.")
        return

    city_name = args[1].strip()
    if not city_name:
        await message.answer("🔶 Укажите город: /clear_geo_cache Пермь")
        return

    ok, removed = GeoService.clear_city_districts_cache(city_name)
    if ok:
        await message.answer(
            f"✅ Гео-кэш для города <b>{city_name}</b> очищен.\n"
            f"Удалено ключей: {removed}."
        )
    else:
        await message.answer(
            f"🔶 Для города <b>{city_name}</b> записи в гео-кэше не найдены."
        )

@router.callback_query(F.data == 'admin_cancel_op')
async def admin_cancel_operation(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text('Операция отменена.', reply_markup=kb.get_admin_keyboard())

# --- Статистика ---

def _admin_stats_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
        ]
    )


async def _render_admin_stats(callback: CallbackQuery) -> None:
    BotStatsService.backfill_from_users(UserService._load_db().get("users") or {})
    rating = ReviewsService.get_rating_stats()
    text = (
        build_stats_text(UserService.get_audience_snapshot(), BotStatsService.period_bundle())
        + "\n\n"
        + ReviewsService.format_rating_line(rating)
    )
    try:
        await callback.message.edit_text(text, reply_markup=_admin_stats_markup())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=_admin_stats_markup())


@router.callback_query(F.data == 'admin_stats')
async def admin_get_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer()
    await _render_admin_stats(callback)


@router.callback_query(F.data == 'admin_product_interest_stats')
async def admin_product_interest_stats(callback: CallbackQuery):
    city_stats = ProductInterestService.get_city_stats()

    if not city_stats:
        await callback.message.edit_text(
            "📈 <b>Интерес к товарам (просмотры)</b>\n\nПока данных нет.",
            reply_markup=kb.get_admin_keyboard()
        )
        return

    await callback.message.edit_text(
        "📈 <b>Интерес к товарам (просмотры)</b>\n\n"
        "Детальная аналитика отправлена отдельным сообщением.",
        reply_markup=kb.get_admin_keyboard()
    )

    chunk_limit = 3800
    chunks: list[str] = ["📈 <b>Интерес к товарам (до оплаты)</b>"]

    for city_row in city_stats:
        city = city_row.get("city", "-")
        total = city_row.get("total_views", 0)
        city_lines = [f"🏙 <b>{city}</b> - {total} выборов"]

        for item in city_row.get("products", []):
            name = item.get("name", item.get("id", "-"))
            pct = item.get("percent", 0.0)
            views = item.get("views", 0)
            city_lines.append(f"• {name}: {pct:.1f}% ({views})")

        city_block = "\n" + "\n".join(city_lines)
        candidate = chunks[-1] + city_block

        if len(candidate) > chunk_limit:
            chunks.append("📈 <b>Интерес к товарам (до оплаты) - продолжение</b>" + city_block)
        else:
            chunks[-1] = candidate

    for chunk in chunks:
        await callback.message.answer(chunk)

# --- Рассылка ---

@router.callback_query(F.data == 'admin_broadcast')
async def admin_ask_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text(
        '📢 <b>Рассылка всем</b>\n'
        'Пришлите текст, фото, видео, файл, альбом или перешлите пост - '
        'его получат все пользователи этого бота:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_broadcast)
async def admin_process_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Пришлите пост для рассылки или нажмите Отмена.")
        return
    from app.services.broadcast import collect_album, copy_messages_to_all_users, is_copyable_message

    if not is_copyable_message(message):
        await message.answer("🔶 Этот тип сообщения нельзя разослать. Пришлите текст, фото, видео или файл.")
        return
    messages = await collect_album(message)
    if messages is None:
        return
    await state.clear()
    msg = await message.answer("⏳ Начинаю рассылку...")
    count_ok, count_err = await copy_messages_to_all_users(bot, messages)
    await msg.edit_text(
        f"✅ Рассылка завершена.\n"
        f"Доставлено: {count_ok}\n"
        f"Не доставлено (блок / другой бот / удалён): {count_err}",
        reply_markup=kb.get_admin_keyboard(),
    )


def _auto_broadcast_worker_title(cfg: dict, index: int | None = None) -> str:
    from app.services.settings import SettingsService
    prefix = f"{index}. " if index else ""
    status = "🟢" if cfg.get("enabled") else "⚪️"
    interval = SettingsService.format_interval_minutes(int(cfg.get("interval_minutes") or 60))
    return f"{status} {prefix}{interval}"


def _auto_broadcast_menu_text(cfg: dict | None = None, index: int | None = None) -> str:
    from app.services.broadcast import media_kind_label
    from app.services.settings import SettingsService
    cfg = cfg or SettingsService.get_auto_broadcast()
    status = "🟢 включен" if cfg.get("enabled") else "⚪️ выключен"
    interval = SettingsService.format_interval_minutes(int(cfg.get("interval_minutes") or 60))
    text = cfg.get("text")
    preview = html.escape((text or "не задан")[:400])
    last = cfg.get("last_sent_at") or "ещё не отправлялась"
    nxt = cfg.get("next_send_at") or "не запланирована"
    number = f" #{index}" if index else ""
    media = cfg.get("media") if isinstance(cfg.get("media"), dict) else None
    if media and media.get("kind"):
        attachment = media_kind_label(str(media.get("kind")))
        filename = media.get("filename")
        count = int(media.get("count") or 1)
        extra = f" ×{count}" if count > 1 else ""
        file_bit = f" ({html.escape(str(filename))})" if filename else ""
        media_line = f"Вложение: <b>{html.escape(attachment)}</b>{extra}{file_bit}\n"
    else:
        media_line = "Вложение: нет\n"
    return (
        f"⏰ <b>Воркер рассылки{number}</b>\n"
        f"ID: <code>{html.escape(str(cfg.get('id') or 'default'))}</code>\n\n"
        f"Статус: {status}\n"
        f"Интервал: <b>{interval}</b>\n"
        f"Последняя отправка: <code>{html.escape(str(last))}</code>\n"
        f"Следующая отправка: <code>{html.escape(str(nxt))}</code>\n"
        f"{media_line}\n"
        f"Текст / подпись:\n{preview}"
    )


def _auto_broadcast_list_text() -> str:
    from app.services.settings import SettingsService
    workers = SettingsService.list_auto_broadcasts()
    if not workers:
        return (
            "⏰ <b>Авторассылка</b>\n\n"
            "Воркеров пока нет. Создайте нового - у каждого свой текст и интервал."
        )
    lines = ["⏰ <b>Авторассылка</b>\n", "Нажмите воркер, чтобы править, остановить или удалить.\n"]
    for index, worker in enumerate(workers, 1):
        lines.append(_auto_broadcast_worker_title(worker, index))
    return "\n".join(lines)


async def _show_auto_broadcast_list(callback: CallbackQuery) -> None:
    from app.services.settings import SettingsService
    workers = SettingsService.list_auto_broadcasts()
    await callback.message.edit_text(
        _auto_broadcast_list_text(),
        reply_markup=kb.get_admin_auto_broadcast_list_keyboard(workers),
    )


async def _show_auto_broadcast_menu(callback: CallbackQuery, worker_id: str) -> None:
    from app.services.settings import SettingsService
    workers = SettingsService.list_auto_broadcasts()
    cfg = SettingsService.get_auto_broadcast(worker_id)
    index = next((i for i, item in enumerate(workers, 1) if item.get("id") == worker_id), None)
    await callback.message.edit_text(
        _auto_broadcast_menu_text(cfg, index),
        reply_markup=kb.get_admin_auto_broadcast_keyboard(bool(cfg.get("enabled")), worker_id),
    )


def _worker_id_from_callback(data: str, prefix: str) -> str | None:
    if not data or not data.startswith(prefix):
        return None
    worker_id = data[len(prefix):].strip()
    return worker_id or None


@router.callback_query(F.data == "admin_auto_broadcast")
async def admin_auto_broadcast_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await _show_auto_broadcast_list(callback)
    await callback.answer()


@router.callback_query(F.data == "admin_ab_new")
async def admin_auto_broadcast_create(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    from app.services.settings import SettingsService
    try:
        worker = SettingsService.create_auto_broadcast()
    except ValueError:
        await callback.answer("Слишком много воркеров, удалите лишний", show_alert=True)
        return
    await state.update_data(ab_worker_id=worker["id"])
    await state.set_state(AdminState.waiting_for_auto_broadcast_text)
    await callback.message.edit_text(
        "➕ <b>Новый воркер</b>\n\n"
        "Пришлите текст, фото, видео, файл или альбом. Потом задайте интервал и запустите.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data=f"admin_ab_w:{worker['id']}")]]
        ),
    )
    await callback.answer("Воркер создан")


@router.callback_query(F.data.startswith("admin_ab_w:"))
async def admin_auto_broadcast_open(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_w:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    await state.clear()
    await _show_auto_broadcast_menu(callback, worker_id)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ab_text:"))
async def admin_auto_broadcast_ask_text(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_text:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    await state.update_data(ab_worker_id=worker_id)
    await state.set_state(AdminState.waiting_for_auto_broadcast_text)
    await callback.message.edit_text(
        "✏️ <b>Сообщение авторассылки</b>\n\n"
        "Пришлите текст, фото, видео, файл, GIF, альбом или голосовое - "
        "бот будет отправлять это всем пользователям.\n"
        "Подпись к картинке тоже сохранится.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=f"admin_ab_w:{worker_id}")]]
        ),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_auto_broadcast_text)
async def admin_auto_broadcast_save_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Пришлите пост для авторассылки или нажмите Отмена.")
        return
    from app.services.broadcast import collect_album, message_media_kind, save_auto_broadcast_media
    from app.services.settings import SettingsService

    if not message_media_kind(message):
        await message.answer("🔶 Пришлите текст, фото, видео или файл.")
        return
    messages = await collect_album(message)
    if messages is None:
        return
    data = await state.get_data()
    worker_id = data.get("ab_worker_id") or "default"
    try:
        payload = await save_auto_broadcast_media(message.bot, worker_id, messages)
    except ValueError as err:
        detail = str(err)
        if "too large" in detail:
            await message.answer("🔶 Файл слишком большой. Telegram отдаёт боту до ~18 МБ.")
        else:
            await message.answer("🔶 Не удалось сохранить это сообщение. Пришлите текст, фото, видео или файл.")
        return
    except Exception:
        logger.exception("Failed to save auto-broadcast media for %s", worker_id)
        await message.answer("🔶 Не удалось сохранить файл. Попробуйте другой формат или меньший размер.")
        return
    SettingsService.save_auto_broadcast(payload, worker_id=worker_id)
    await state.clear()
    cfg = SettingsService.get_auto_broadcast(worker_id)
    await message.answer(
        "✅ Сообщение авторассылки сохранено.\n\n" + _auto_broadcast_menu_text(cfg),
        reply_markup=kb.get_admin_auto_broadcast_keyboard(bool(cfg.get("enabled")), worker_id),
    )


@router.callback_query(F.data.startswith("admin_ab_int:"))
async def admin_auto_broadcast_ask_interval(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_int:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    await state.update_data(ab_worker_id=worker_id)
    await state.set_state(AdminState.waiting_for_auto_broadcast_interval)
    await callback.message.edit_text(
        "⏱ <b>Интервал авторассылки</b>\n\n"
        "Напишите период, например:\n"
        "• <code>30</code> или <code>30м</code> - каждые 30 минут\n"
        "• <code>2ч</code> или <code>2h</code> - каждые 2 часа",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=f"admin_ab_w:{worker_id}")]]
        ),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_auto_broadcast_interval)
async def admin_auto_broadcast_save_interval(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    from app.services.settings import SettingsService
    minutes = SettingsService.parse_interval_minutes(message.text or "")
    if not minutes:
        await message.answer("🔶 Не понял интервал. Пример: <code>30м</code> или <code>2ч</code>.")
        return
    data = await state.get_data()
    worker_id = data.get("ab_worker_id") or "default"
    SettingsService.save_auto_broadcast({"interval_minutes": minutes}, worker_id=worker_id)
    cfg = SettingsService.get_auto_broadcast(worker_id)
    if cfg.get("enabled"):
        now = get_now_msk()
        interval = SettingsService.interval_timedelta(minutes)
        last_at = SettingsService.parse_broadcast_time(cfg.get("last_sent_at"))
        next_at = SettingsService.resolve_next_send_at(
            now=now,
            interval=interval,
            last_sent_at=last_at,
            next_send_at=None,
        )
        SettingsService.save_auto_broadcast(
            {"next_send_at": SettingsService.format_broadcast_time(next_at)},
            worker_id=worker_id,
        )
        cfg = SettingsService.get_auto_broadcast(worker_id)
    await state.clear()
    await message.answer(
        f"✅ Интервал: <b>{SettingsService.format_interval_minutes(minutes)}</b>\n\n"
        + _auto_broadcast_menu_text(cfg),
        reply_markup=kb.get_admin_auto_broadcast_keyboard(bool(cfg.get("enabled")), worker_id),
    )


@router.callback_query(F.data.startswith("admin_ab_tog:"))
async def admin_auto_broadcast_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    from app.services.settings import SettingsService
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_tog:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    cfg = SettingsService.get_auto_broadcast(worker_id)
    if not cfg.get("enabled") and not SettingsService.worker_has_payload(cfg):
        await callback.answer("Сначала задайте текст, фото или файл рассылки", show_alert=True)
        return
    enabled = not bool(cfg.get("enabled"))
    payload = {"enabled": enabled}
    if enabled:
        now = get_now_msk()
        interval = SettingsService.interval_timedelta(cfg.get("interval_minutes"))
        payload["next_send_at"] = SettingsService.format_broadcast_time(now + interval)
    SettingsService.save_auto_broadcast(payload, worker_id=worker_id)
    await _show_auto_broadcast_menu(callback, worker_id)
    await callback.answer("Воркер включен" if enabled else "Воркер выключен")


@router.callback_query(F.data.startswith("admin_ab_now:"))
async def admin_auto_broadcast_send_now(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    from app.services.settings import SettingsService
    from app.services.broadcast import send_auto_broadcast
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_now:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    cfg = SettingsService.get_auto_broadcast(worker_id)
    if not SettingsService.worker_has_payload(cfg):
        await callback.answer("Сначала задайте текст, фото или файл рассылки", show_alert=True)
        return
    await callback.answer("Отправляю...")
    status = await callback.message.answer(
        "⏳ Этот бот шлёт сейчас. Остальные 4 бота дошлют своим в течение ~20 сек..."
    )
    now = get_now_msk()
    claimed, _is_first = SettingsService.claim_auto_broadcast_send(
        bot.id,
        now,
        open_now=True,
        worker_id=worker_id,
    )
    if not claimed:
        await status.edit_text("Не удалось открыть слот рассылки. Попробуйте ещё раз.")
        return
    delivered, failed = await send_auto_broadcast(bot, worker_id)
    cfg = SettingsService.get_auto_broadcast(worker_id)
    await status.edit_text(
        f"✅ С этого бота доставлено: {delivered}\n"
        f"❗ Этот бот не смог (другие боты / блок): {failed}\n"
        f"Остальные боты сами дошлют своим в течение ~20 сек.\n\n"
        + _auto_broadcast_menu_text(cfg),
        reply_markup=kb.get_admin_auto_broadcast_keyboard(bool(cfg.get("enabled")), worker_id),
    )


@router.callback_query(F.data.startswith("admin_ab_del:"))
async def admin_auto_broadcast_ask_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_del:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑 Удалить этот воркер рассылки? Его больше не будет в расписании.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"admin_ab_ok:{worker_id}")],
                [InlineKeyboardButton(text="Отмена", callback_data=f"admin_ab_w:{worker_id}")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ab_ok:"))
async def admin_auto_broadcast_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    from app.services.settings import SettingsService
    worker_id = _worker_id_from_callback(callback.data or "", "admin_ab_ok:")
    if not worker_id:
        await callback.answer("Воркер не найден", show_alert=True)
        return
    SettingsService.delete_auto_broadcast(worker_id)
    await state.clear()
    await _show_auto_broadcast_list(callback)
    await callback.answer("Воркер удалён")


@router.callback_query(F.data == 'admin_dm')
async def admin_ask_dm_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_dm_id)
    await callback.message.edit_text(
        '📩 <b>Личное сообщение</b>\n'
        'Введите ID пользователя или @username:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_dm_id)
async def admin_process_dm_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)
    
    if user_id:
        username = UserService.get_username(user_id)
        display = f'@{username}' if username else str(user_id)
        await state.update_data(dm_user_id=user_id)
        await state.set_state(AdminState.waiting_for_dm_text)
        await message.answer(
            f'✍ Введите текст сообщения для пользователя <b>{display}</b> ({user_id}):'
        )
    else:
        await message.answer('❗ Пользователь не найден. Введите корректный ID или @username:')

@router.message(AdminState.waiting_for_dm_text)
async def admin_process_dm_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = data['dm_user_id']
    text = message.text
    
    try:
        sent_msg = await bot.send_message(user_id, f'🔔 <b>Сообщение от администратора:</b>\n\n{text}')
        
        # Callback for deleting both
        del_callback = f"admin_del_dm_{user_id}_{sent_msg.message_id}"
        
        await message.answer(
            f'✅ Успешно отправлено пользователю {user_id}.', 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить у обоих", callback_data=del_callback)],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="admin_back_main")]
            ])
        )
    except Exception as e:
        await message.answer(f'❗ Ошибка отправки: {e}', reply_markup=kb.get_admin_keyboard())
    
    await state.clear()

@router.callback_query(F.data.startswith('admin_del_dm_'))
async def admin_delete_dm_both(callback: CallbackQuery, bot: Bot):
    # data: admin_del_dm_USERID_MSGID
    try:
        parts = callback.data.split('_')
        # parts: ['admin', 'del', 'dm', 'USERID', 'MSGID']
        if len(parts) < 5:
             raise ValueError("Bad data")
             
        user_id = int(parts[3])
        msg_id = int(parts[4])
        
        # 1. Delete user message
        try:
            await bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass # Already deleted or block
            
        # 2. Delete admin message (or just the button)
        await callback.message.delete()
        await callback.answer("✅ Сообщение удалено у обоих!", show_alert=True)
        
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith('admin_del_sent_'))
async def admin_delete_sent(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = callback.data.split('_')
    if len(parts) < 5:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    try:
        user_id = int(parts[3])
        msg_id = int(parts[4])
    except ValueError:
        await callback.answer("Ошибка идентификаторов", show_alert=True)
        return

    removed = False
    try:
        await bot.delete_message(user_id, msg_id)
        removed = True
    except TelegramBadRequest as err:
        logger.warning("Не удалось удалить сообщение %s у пользователя %s: %s", msg_id, user_id, err)
    except Exception:
        logger.exception("Ошибка при удалении сообщения %s у пользователя %s", msg_id, user_id)

    try:
        feedback = "✅ Сообщение удалено у клиента" if removed else "🔶 Не удалось удалить (уже удалено?)"
        await callback.message.edit_text(feedback)
    except TelegramBadRequest:
        pass

    await callback.answer("Готово")

# --- Баны и разбаны через Панель ---

@router.callback_query(F.data == 'admin_list_bans')
async def admin_show_bans(callback: CallbackQuery):
    try:
        # Постоянный бан (BlacklistService)
        banned_list_perm = BlacklistService.load_blacklist()
        
        # Временный бан (UserService)
        banned_list_temp = UserService.get_temp_banned_users()
        
        text = "📜 <b>Список заблокированных:</b>\n\n"
        
        if not banned_list_perm and not banned_list_temp:
            text += "✅ Список пуст."
        else:
            if banned_list_perm:
                text += "<b>🛑 Навсегда (Blacklist):</b>\n"
                for uid in banned_list_perm:
                    username = UserService.get_username(uid)
                    text += f"• <code>{uid}</code> (@{username if username else '?'})\n"
                text += "\n"
                
            if banned_list_temp:
                text += "<b>⏳ Временно (до ...):</b>\n"
                for item in banned_list_temp:
                    text += f"• <code>{item['id']}</code> (@{item['username']}) - до {item['until']}\n"
            
        await callback.message.edit_text(text, reply_markup=kb.get_admin_keyboard())
    except Exception as e:
        await callback.answer(f'Ошибка: {e}', show_alert=True)

@router.callback_query(F.data == 'admin_ban_user')
async def admin_ask_ban_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_ban_id)
    await callback.message.edit_text(
        '🔨 Введите ID или @username для блокировки (Навсегда):',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_ban_id)
async def admin_process_ban_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)
    
    if user_id:
        if BlacklistService.ban_user(user_id):
            await message.answer(f'✅ Пользователь {user_id} заблокирован НАВСЕГДА.', reply_markup=kb.get_admin_keyboard())
        else:
            await message.answer(f'🔶 Пользователь {user_id} уже заблокирован.', reply_markup=kb.get_admin_keyboard())
        await state.clear()
    else:
        await message.answer('❗ Пользователь не найден. Попробуйте еще раз или нажмите Отмена.')

@router.callback_query(F.data == 'admin_unban_user')
async def admin_ask_unban_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_unban_id)
    await callback.message.edit_text(
        '😇 Введите ID или @username для разблокировки (Снять любой бан):',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_unban_id)
async def admin_process_unban_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)
    
    if user_id:
        # Пробуем снять оба вида бана
        res_perm = BlacklistService.unban_user(user_id)
        res_temp = UserService.unban_user(user_id)
        
        if res_perm or res_temp:
            await message.answer(f'✅ Пользователь {user_id} разблокирован.', reply_markup=kb.get_admin_keyboard())
        else:
            await message.answer(f'🔶 Пользователь {user_id} не был найден в списках блокировки.', reply_markup=kb.get_admin_keyboard())
        await state.clear()
    else:
        await message.answer('❗ Пользователь не найден. Попробуйте еще раз.')

@router.callback_query(F.data.startswith("admin_sbx_complete_"))
async def admin_sandbox_complete(callback: CallbackQuery, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if not is_ratecore_sandbox():
        await callback.answer("Sandbox complete только в песочнице.", show_alert=True)
        return
    try:
        user_id = int(callback.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await callback.answer("🔶 Ошибка ID", show_alert=True)
        return
    try:
        payment_id = await sandbox_complete_for_user(bot, dispatcher, user_id)
    except RatecoreError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await callback.answer(f"Sandbox complete: {payment_id}", show_alert=True)


@router.callback_query(F.data.startswith('admin_cancel_order_'))
async def admin_cancel_order_callback(callback: CallbackQuery, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        user_id_str = callback.data.split('_')[-1]
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer("🔶 Ошибка ID")
        return
        
    active_order = UserService.get_active_order(user_id)
    if active_order:
        # Delete old payment message if possible
        if "message_id" in active_order:
            try:
                await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
            except:
                pass

        UserService.remove_active_order(user_id)
        await clear_user_order_fsm(bot, dispatcher, user_id)
        is_topup = CryptoService.is_balance_topup(active_order)
        is_chest = CryptoService.is_chest_boost(active_order)
        if is_chest:
            answer = "✅ Заявка сундука отменена"
            mark = "СУНДУК ОТМЕНЁН АДМИНОМ"
            user_text = (
                "ℹ️ <b>Заявка на усиление сундука отменена.</b>\n"
                "Можно открыть бесплатно или оплатить усиление заново."
            )
        elif is_topup:
            answer = "✅ Пополнение отменено"
            mark = "ПОПОЛНЕНИЕ ОТМЕНЕНО АДМИНОМ"
            user_text = _user_order_cancelled_text(topup=True)
        else:
            answer = "✅ Заказ успешно отменен"
            mark = "ЗАКАЗ ОТМЕНЕН АДМИНОМ"
            user_text = _user_order_cancelled_text(topup=False)
        await callback.answer(answer, show_alert=True)
        
        try:
            await callback.message.edit_text(
                callback.message.text + f"\n\n❗ <b>{mark}</b>",
                reply_markup=None
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                user_id, 
                user_text,
                reply_markup=kb.get_base_keyboard()
            )
        except Exception:
            pass
    else:
        await callback.answer("🔶 Активный заказ не найден (возможно, уже отменен).", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass

@router.callback_query(F.data == "admin_topup_nocards")
async def admin_topup_nocards(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Добавьте карты РФ в админке: Карты РФ (оплата).", show_alert=True)


@router.callback_query(F.data.startswith("admin_topup_send_"))
async def admin_topup_send_card(callback: CallbackQuery, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split("_")
    try:
        user_id = int(parts[3])
        card_idx = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("🔶 Ошибка данных", show_alert=True)
        return

    cards = SettingsService.get_rf_cards()
    if card_idx < 0 or card_idx >= len(cards):
        await callback.answer("Карта не найдена. Обновите список в админке.", show_alert=True)
        return
    card = cards[card_idx]

    user_fsm = get_user_fsm(bot, dispatcher, user_id)
    user_state = await user_fsm.get_state()
    state_name = str(user_state or "")
    if "TopUpState" not in state_name:
        await callback.answer("Пользователь уже не в заявке на пополнение.", show_alert=True)
        return

    data = await user_fsm.get_data()
    amount = data.get("topup_amount")
    expires_at = data.get("topup_expires_at") or (get_now_msk() + timedelta(minutes=20)).strftime("%H:%M")
    product = data.get("product") or {}
    text = build_topup_card_payment_text(
        card,
        {
            "user_id": user_id,
            "amount": amount,
            "expires_at": expires_at,
            "city": data.get("city") or "",
            "district": data.get("district") or "",
            "product": product.get("name") if isinstance(product, dict) else "",
        },
    )
    await user_fsm.update_data(topup_card=card)
    UserService.set_last_rf_card(user_id, card)

    try:
        await bot.send_message(user_id, text, reply_markup=kb.get_cancel_payment_keyboard())
    except Exception:
        logger.exception("Failed to send topup card template to %s", user_id)
        await callback.answer("Не удалось отправить клиенту. Возможно, он не писал этому боту.", show_alert=True)
        return

    await callback.answer("✅ Шаблон отправлен клиенту", show_alert=True)
    try:
        current_text = getattr(callback.message, "html_text", None) or callback.message.text or ""
        marker = f"✅ Отправлена карта: <code>{html.escape(card)}</code>"
        if "Отправлена карта:" in current_text:
            current_text = re.sub(r"✅ Отправлена карта:.*", marker, current_text, count=1)
            if marker not in current_text:
                current_text = current_text.rstrip() + "\n\n" + marker
        else:
            current_text = current_text.rstrip() + "\n\n" + marker
        await callback.message.edit_text(
            current_text,
            reply_markup=kb.get_admin_topup_request_keyboard(user_id, cards),
        )
    except TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith("admin_confirm_topup_"))
async def admin_confirm_topup_callback(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        user_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("🔶 Ошибка ID")
        return

    admin_data = await state.get_data()
    if str(admin_data.get("target_user_id") or "") == str(user_id):
        await state.clear()

    amount = None
    active_order = UserService.get_active_order(user_id)
    if active_order and (
        CryptoService.is_balance_topup(active_order) or active_order.get("type") == "manual_topup"
    ):
        amount = active_order.get("price")
    if amount is None:
        user_data = await get_user_fsm(bot, dispatcher, user_id).get_data()
        amount = user_data.get("topup_amount")
    try:
        amount_value = float(amount or 0)
    except (TypeError, ValueError):
        amount_value = 0.0
    if amount_value <= 0:
        await callback.answer("Сумма пополнения не найдена.", show_alert=True)
        return

    UserService.add_balance(user_id, amount_value, source="topup")
    UserService.remove_active_order(user_id)
    new_balance = UserService.get_balance(user_id)
    await send_user_to_start(
        bot,
        dispatcher,
        user_id,
        "✅ <b>Баланс пополнен!</b>\n\n"
        f"Зачислено: <b>{amount_value:.0f} руб.</b>\n"
        f"Текущий баланс: <b>{new_balance:.2f} руб.</b>",
    )
    await callback.answer("✅ Пополнение подтверждено", show_alert=True)

    try:
        current_text = getattr(callback.message, "html_text", None) or callback.message.text or ""
        await callback.message.edit_text(
            current_text + "\n\n✅ <b>ПОПОЛНЕНИЕ ПОДТВЕРЖДЕНО</b>",
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

@router.callback_query(F.data.startswith("admin_cancel_topup_"))
async def admin_cancel_topup_callback(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        user_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("🔶 Ошибка ID")
        return

    admin_data = await state.get_data()
    if str(admin_data.get("target_user_id") or "") == str(user_id):
        await state.clear()

    UserService.remove_active_order(user_id)
    await send_user_to_start(
        bot,
        dispatcher,
        user_id,
        "ℹ️ <b>Пополнение отменено оператором.</b>\n"
        "Чат по этой заявке закрыт.\n"
        "новый заказ - сектор Витрина",
    )
    await callback.answer("✅ Пополнение отменено, чат закрыт", show_alert=True)

    try:
        current_text = getattr(callback.message, "html_text", None) or callback.message.text or ""
        await callback.message.edit_text(
            current_text + "\n\n❗ <b>ПОПОЛНЕНИЕ ОТМЕНЕНО АДМИНОМ</b>",
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

def _user_order_cancelled_text(*, topup: bool) -> str:
    return (
        "🔶 <b>Не удалось завершить операцию.</b>\n"
        "Заявка закрыта. Если деньги списались, напишите в поддержку."
    )


async def _end_admin_chat_if_target(state: FSMContext | None, user_id: int) -> bool:
    if state is None:
        return False
    data = await state.get_data()
    if str(data.get("target_user_id") or "") != str(user_id):
        return False
    current = await state.get_state()
    if current != AdminState.chatting_with_user.state:
        return False
    await state.clear()
    return True


async def _apply_admin_order_confirmation(
    bot: Bot,
    dispatcher: Dispatcher,
    user_id: int,
    active_order: dict,
) -> tuple[str, dict | None]:
    if "message_id" in active_order:
        try:
            await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
        except Exception:
            pass

    if CryptoService.is_chest_boost(active_order):
        try:
            rub = float(active_order.get("price") or 0)
        except (TypeError, ValueError):
            rub = 0.0
        percent = int(active_order.get("boost_percent") or 0)
        ChestService.grant_boost(user_id, rub if rub else percent * 10)
        UserService.remove_active_order(user_id)
        await clear_user_order_fsm(bot, dispatcher, user_id)
        return (
            "✅ <b>Оплата принята.</b>\n\n"
            "С усилением вам <b>уже гарантированы 500 ₽</b> на баланс, "
            "плюс шанс выиграть <b>3000 ₽</b>.\n"
            "Можете пытать удачу - откройте сундук в меню."
        ), None

    if CryptoService.is_balance_topup(active_order):
        try:
            amount = float(active_order.get("price") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        UserService.add_balance(user_id, amount, source="topup")
        UserService.remove_active_order(user_id)
        await clear_user_order_fsm(bot, dispatcher, user_id)
        new_balance = UserService.get_balance(user_id)
        return (
            "✅ <b>Баланс пополнен!</b>\n\n"
            f"Зачислено: <b>{amount:.0f} руб.</b>\n"
            f"Текущий баланс: <b>{new_balance:.2f} руб.</b>"
        ), None

    loyalty = None
    if not active_order.get("purchase_counted"):
        bonus_payload = complete_product_purchase(
            user_id,
            active_order.get("price"),
            title=str(active_order.get("product") or ""),
            city=str(active_order.get("city") or ""),
        )
        await notify_referrer_bonus(bot, bonus_payload)
        loyalty = (bonus_payload or {}).get("loyalty")
    UserService.remove_active_order(user_id)
    await clear_user_order_fsm(bot, dispatcher, user_id)

    if active_order.get("type") == "card_rf":
        UserService.reset_rf_card_cancel_streak(user_id)
    elif active_order.get("type") == "yoomoney":
        UserService.reset_yoomoney_cancel_streak(user_id)

    UserService.set_silent_review_mode(user_id, True)
    return build_order_fulfillment_wait_text(), loyalty


@router.callback_query(F.data.startswith('admin_confirm_order_'))
async def admin_confirm_order_callback(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    try:
        user_id_str = callback.data.split('_')[-1]
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer("🔶 Ошибка ID")
        return

    # Check authorized admin
    if not is_admin(callback.from_user.id):
        return
        
    active_order = UserService.get_active_order(user_id)
    if active_order:
        user_text, loyalty = await _apply_admin_order_confirmation(bot, dispatcher, user_id, active_order)
        await _end_admin_chat_if_target(state, user_id)
        is_topup = CryptoService.is_balance_topup(active_order)
        is_chest = CryptoService.is_chest_boost(active_order)
        if is_chest:
            await callback.answer("✅ Сундук: можно открывать", show_alert=True)
        else:
            await callback.answer("✅ Баланс пополнен!" if is_topup else "✅ Заказ подтвержден!", show_alert=True)
        
        try:
            if is_chest:
                mark = "СУНДУК: ОПЛАТА ПОДТВЕРЖДЕНА"
            elif is_topup:
                mark = "ПОПОЛНЕНИЕ ПОДТВЕРЖДЕНО"
            else:
                mark = "ЗАКАЗ ПОДТВЕРЖДЕН ВРУЧНУЮ"
            await callback.message.edit_text(
                callback.message.text + f"\n\n✅ <b>{mark}</b>",
                reply_markup=None
            )
        except Exception:
            pass
            
        try:
             await bot.send_message(
                 user_id, 
                 user_text,
                 reply_markup=kb.get_base_keyboard()
             )
             await notify_purchase_loyalty(bot, user_id, loyalty=loyalty)
        except Exception as e:
             await callback.message.answer(f"🔶 Операция выполнена, но сообщение юзеру не дошло: {e}")
    else:
        await callback.answer("🔶 У пользователя нет активного заказа.", show_alert=True)


# --- Settings for Reviews ---

@router.callback_query(F.data == "adm_rev_settings")
async def adm_rev_settings(callback: CallbackQuery):
    settings = ReviewsService.get_gen_settings()
    text = (
        f"⚙️ <b>Настройки авто-отзывов</b>\n\n"
        f"Текущие параметры:\n"
        f"• Интервал запуска: <b>{settings.get('interval_hours')} ч.</b>\n"
        f"• Минимум на город: <b>{settings.get('min_reviews')} шт.</b>\n"
        f"• Максимум на город: <b>{settings.get('max_reviews')} шт.</b>\n\n"
        f"<i>После изменения интервала новые настройки применятся при следующем запуске процесса.</i>"
    )
    await callback.message.edit_text(text, reply_markup=kb.get_admin_reviews_settings_menu(settings))

@router.callback_query(F.data == "adm_rev_set_interval")
async def adm_rev_set_interval(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "⏳ Введите новый интервал в часах (например: 12 или 2.5):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_settings")]])
    )
    await state.set_state(AdminState.waiting_for_rev_set_interval)

@router.message(AdminState.waiting_for_rev_set_interval)
async def adm_rev_process_interval(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        if val <= 0.1: raise ValueError
        ReviewsService.update_gen_settings(interval=val)
        await message.answer("✅ Интервал сохранен.", reply_markup=kb.get_admin_reviews_main_menu())
        await state.clear()
        # Возвращаем меню настроек
        # Можно вызвать adm_rev_settings(message, ...) но это callback
        # Просто отправим сообщение
        settings = ReviewsService.get_gen_settings()
        await message.answer(
             f"Новый интервал: {settings.get('interval_hours')} ч.", 
             reply_markup=kb.get_admin_reviews_settings_menu(settings)
        )
    except ValueError:
        await message.answer("🔶 Введите корректное число (часы).")

@router.callback_query(F.data == "adm_rev_set_min")
async def adm_rev_set_min(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔢 Введите минимальное кол-во отзывов за раз (целое число):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_settings")]])
    )
    await state.set_state(AdminState.waiting_for_rev_set_min)

@router.message(AdminState.waiting_for_rev_set_min)
async def adm_rev_process_min(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val < 0: raise ValueError
        ReviewsService.update_gen_settings(min_r=val)
        await message.answer("✅ Минимум сохранен.", reply_markup=kb.get_admin_reviews_main_menu())
        await state.clear()
        settings = ReviewsService.get_gen_settings()
        await message.answer(
             f"Новые настройки: {settings.get('min_reviews')} - {settings.get('max_reviews')}", 
             reply_markup=kb.get_admin_reviews_settings_menu(settings)
        )
    except ValueError:
        await message.answer("🔶 Введите целое число.")

@router.callback_query(F.data == "adm_rev_set_max")
async def adm_rev_set_max(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔢 Введите максимальное кол-во отзывов за раз (целое число):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_settings")]])
    )
    await state.set_state(AdminState.waiting_for_rev_set_max)

@router.message(AdminState.waiting_for_rev_set_max)
async def adm_rev_process_max(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val < 0: raise ValueError
        ReviewsService.update_gen_settings(max_r=val)
        await message.answer("✅ Максимум сохранен.", reply_markup=kb.get_admin_reviews_main_menu())
        await state.clear()
        settings = ReviewsService.get_gen_settings()
        await message.answer(
             f"Новые настройки: {settings.get('min_reviews')} - {settings.get('max_reviews')}", 
             reply_markup=kb.get_admin_reviews_settings_menu(settings)
        )
    except ValueError:
         await message.answer("🔶 Введите целое число.")

# --- Старые команды (оставляем для удобства) ---

@router.callback_query(F.data == 'admin_active_orders')
async def admin_active_orders_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    orders = UserService.get_all_active_orders()
    text = "📋 <b>Активные заказы:</b>\n\n"
    buttons: list[list[InlineKeyboardButton]] = []

    if not orders:
        text += "📭 Активных заказов нет."
    else:
        for o in orders:
            uid = o['user_id']
            uname = o['username'] or "NoName"
            details = o['order']
            product = details.get('product', '???')
            price = details.get('price', 0)
            type_ = details.get('type', 'unknown')
            blocked, until = UserService.is_manual_payment_blocked(int(uid))
            block_status = f"🛑️ Ручная оплата до: {until}" if blocked else "✅ Ручная оплата: доступна"
            rf_blocked, rf_until = UserService.is_rf_card_blocked(int(uid))
            rf_status = f"🛑️ Карта РФ до: {rf_until}" if rf_blocked else "✅ Карта РФ: доступна"

            text += f"🔹 <b>User:</b> {uid} (@{uname})\n"
            text += f"   📦 {product} | {price} руб.\n"
            text += f"   💳 Тип: {type_}\n"
            if type_ in {"btc", "ltc", "crypto"}:
                text += f"   💎 {details.get('crypto_amount') or '-'} {str(type_).upper()}\n"
                text += f"   🔗 Хеш: <code>{details.get('tx_hash') or 'ещё нет'}</code>\n"
            elif type_ == "yoomoney":
                wallet = details.get("yoomoney_wallet") or details.get("card_number") or "-"
                text += f"   💜 ЮMoney: <code>{wallet}</code>\n"
            text += f"   {block_status}\n"
            text += f"   {rf_status}\n"
            text += f"   👉 /confirm_order {uid}\n"
            text += f"   👉 /cancel_order {uid}\n\n"

            row = [
                InlineKeyboardButton(text="🛑️ Блок ручных 24ч", callback_data=f"adm_manual_block_24h_{uid}"),
                InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"adm_manual_unblock_{uid}")
            ]
            if rf_blocked:
                row.append(InlineKeyboardButton(text="✅ Разбан Карта РФ", callback_data=f"adm_rf_unblock_{uid}"))
            buttons.append(row)

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("adm_manual_block_24h_"))
async def admin_manual_block_24h(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    user_id_raw = callback.data[len("adm_manual_block_24h_"):]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    until = (get_now_msk() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    UserService.set_manual_payment_block_until(user_id, until)
    await callback.answer("Блок ручной оплаты установлен на 24 часа")
    await admin_active_orders_callback(callback)


@router.callback_query(F.data.startswith("adm_manual_unblock_"))
async def admin_manual_unblock(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    user_id_raw = callback.data[len("adm_manual_unblock_"):]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    if UserService.unblock_manual_payment(user_id):
        await callback.answer("Ручная оплата разблокирована")
    else:
        await callback.answer("Блокировка не найдена", show_alert=True)
    await admin_active_orders_callback(callback)


@router.callback_query(F.data.startswith("adm_rf_unblock_"))
async def admin_rf_unblock(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    user_id_raw = callback.data[len("adm_rf_unblock_"):]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    if UserService.unblock_rf_card_payment(user_id):
        await callback.answer("Оплата картой РФ разблокирована")
    else:
        await callback.answer("Блокировка не найдена", show_alert=True)
    await admin_active_orders_callback(callback)

@router.message(Command("active_orders"))
async def cmd_active_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
        
    orders = UserService.get_all_active_orders()
    if not orders:
        await message.answer("📭 Активных заказов нет.")
        return
        
    text = "📋 <b>Активные заказы:</b>\n\n"
    for o in orders:
        uid = o['user_id']
        uname = o['username'] or "NoName"
        details = o['order']
        product = details.get('product', '???')
        price = details.get('price', 0)
        type_ = details.get('type', 'unknown')
        
        text += f"🔹 <b>User:</b> {uid} (@{uname})\n"
        text += f"   📦 {product} | {price} руб.\n"
        text += f"   💳 Тип: {type_}\n"
        if type_ in {"btc", "ltc", "crypto"}:
            text += f"   💎 {details.get('crypto_amount') or '-'} {str(type_).upper()}\n"
            text += f"   🔗 Хеш: <code>{details.get('tx_hash') or 'ещё нет'}</code>\n"
        elif type_ == "yoomoney":
            wallet = details.get("yoomoney_wallet") or details.get("card_number") or "-"
            text += f"   💜 ЮMoney: <code>{wallet}</code>\n"
        text += f"   👉 /confirm_order {uid}\n"
        text += f"   👉 /cancel_order {uid}\n\n"
        
    await message.answer(text)

@router.message(Command("confirm_order"))
async def cmd_confirm_order(message: Message, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(message.from_user.id):
        return
        
    try:
        if len(message.text.split()) < 2:
            raise IndexError
            
        user_input = message.text.split()[1]
        user_id = resolve_user_input(user_input)
        
        if not user_id:
            await message.answer("❗ Пользователь не найден")
            return
            
        active_order = UserService.get_active_order(user_id)
        if active_order:
             user_text, loyalty = await _apply_admin_order_confirmation(bot, dispatcher, user_id, active_order)
             await _end_admin_chat_if_target(state, user_id)
             is_topup = CryptoService.is_balance_topup(active_order)
             await message.answer(
                 f"✅ Пополнение пользователя {user_input} подтверждено!"
                 if is_topup
                 else f"✅ Заказ пользователя {user_input} подтвержден вручную!"
             )
             try:
                 await bot.send_message(
                     user_id, 
                     user_text,
                     reply_markup=kb.get_base_keyboard()
                 )
                 await notify_purchase_loyalty(bot, user_id, loyalty=loyalty)
             except Exception as e:
                 await message.answer(f"🔶 Юзеру не доставилось: {e}")
                 
        else:
             await message.answer(f"🔶 У пользователя {user_input} нет активных заказов.")
             
    except IndexError:
        await message.answer("🔶 Использование: /confirm_order ID или @username")

@router.message(Command("cancel_order"))
async def cmd_cancel_order(message: Message, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(message.from_user.id):
        return
    
    try:
        if len(message.text.split()) < 2:
            raise IndexError
            
        user_input = message.text.split()[1]
        user_id = resolve_user_input(user_input)
        
        if not user_id:
            await message.answer("❗ Пользователь не найден")
            return
            
        active_order = UserService.get_active_order(user_id)
        if active_order:
            # Delete msg
            if "message_id" in active_order:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
                except:
                    pass

            UserService.remove_active_order(user_id)
            await clear_user_order_fsm(bot, dispatcher, user_id)
            is_topup = CryptoService.is_balance_topup(active_order)
            await message.answer(
                f"✅ Пополнение пользователя {user_input} отменено."
                if is_topup
                else f"✅ Заказ пользователя {user_input} отменен админом."
            )
            user_text = _user_order_cancelled_text(topup=is_topup)
            try:
                await bot.send_message(
                    user_id,
                    user_text,
                    reply_markup=kb.get_base_keyboard(),
                )
            except:
                pass
        else:
            await message.answer(f"🔶 У пользователя {user_input} нет активных заказов.")
            
    except IndexError:
        await message.answer("🔶 Использование: /cancel_order ID или @username")

@router.message(Command('ban'))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        raw = message.text.split()[1]
        user_id = resolve_user_input(raw)
        if user_id:
            if BlacklistService.ban_user(user_id):
                await message.answer(f'🔨 Пользователь {user_id} заблокирован.')
            else:
                await message.answer(f'🔶 Пользователь {user_id} уже был в бане.')
        else:
             await message.answer('🔶 Пользователь не найден.')
    except:
        await message.answer('🔶 Использование: /ban ID или @username')

@router.message(Command('unban'))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        raw = message.text.split()[1]
        user_id = resolve_user_input(raw)
        if user_id:
            if BlacklistService.unban_user(user_id):
                await message.answer(f'😇 Пользователь {user_id} разблокирован.')
            else:
                await message.answer(f'🔶 Пользователь {user_id} не найден.')
        else:
             await message.answer('🔶 Пользователь не найден.')
    except:
        await message.answer('🔶 Использование: /unban ID или @username')


@router.message(Command("unblock_rf_card"))
async def cmd_unblock_rf_card(message: Message):
    if not is_admin(message.from_user.id):
        return

    try:
        if len(message.text.split()) < 2:
            raise IndexError

        user_input = message.text.split()[1]
        user_id = resolve_user_input(user_input)

        if not user_id:
            await message.answer("❗ Пользователь не найден")
            return

        if UserService.unblock_rf_card_payment(user_id):
            await message.answer(f"✅ Оплата картой РФ разблокирована для {user_input}.")
        else:
            await message.answer("🔶 У пользователя нет активной блокировки по карте РФ.")
    except IndexError:
        await message.answer("🔶 Использование: /unblock_rf_card ID или @username")

# --- Шаблоны ответа на диспут ---

def _dispute_templates_view() -> tuple[str, InlineKeyboardMarkup]:
    items = SettingsService.get_dispute_replies()
    lines = [
        "⚖️ <b>Шаблоны диспута</b>",
        "",
        "Эти кнопки появятся в карточке диспута.",
        "Нажатие отправит заготовленный текст клиенту и запишет скидку с кнопки ровно на 1 покупку.",
        "В тексте можно вставить <code>{percent}</code> - подставится число скидки.",
        "",
    ]
    if not items:
        lines.append("Шаблонов пока нет. Добавьте, например, 25%, 30% и 50%.")
    else:
        for index, item in enumerate(items, start=1):
            preview = " ".join(str(item.get("text") or "").split())
            if len(preview) > 90:
                preview = preview[:89].rstrip() + "…"
            lines.append(f"{index}. <b>{item['percent']}%</b> - {html.escape(preview)}")
    rows = [[InlineKeyboardButton(text="➕ Добавить шаблон", callback_data="adtpl_add")]]
    for index, item in enumerate(items):
        rows.append([
            InlineKeyboardButton(text=f"✏️ Текст {item['percent']}%", callback_data=f"adtpl_edit_{index}"),
            InlineKeyboardButton(text=f"🗑 {item['percent']}%", callback_data=f"adtpl_del_{index}"),
        ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_dispute_templates(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = _dispute_templates_view()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adtpl_menu")
async def admin_dispute_templates_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _show_dispute_templates(callback, state)


@router.callback_query(F.data == "adtpl_add")
async def admin_dispute_template_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if len(SettingsService.get_dispute_replies()) >= SettingsService.DISPUTE_REPLY_LIMIT:
        await callback.answer("Уже 8 шаблонов. Удалите один, чтобы добавить новый.", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_dispute_tpl_percent)
    await callback.message.edit_text(
        "⚖️ <b>Новый шаблон диспута</b>\n\n"
        "Напишите процент скидки для кнопки, от 1 до 90.\n"
        "Образец: <code>25</code>\n"
        "На кнопке у админа будет <b>25%</b>, клиенту уйдет текст, а скидка запишется на 1 покупку.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adtpl_menu")]]
        ),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_dispute_tpl_percent)
async def admin_dispute_template_percent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace("%", "").strip()
    try:
        percent = int(raw)
    except ValueError:
        await message.answer("🔶 Нужно целое число от 1 до 90. Образец: 25")
        return
    if percent < 1 or percent > 90:
        await message.answer("🔶 Скидка от 1 до 90%.")
        return
    await state.update_data(dispute_tpl_percent=percent)
    await state.set_state(AdminState.waiting_for_dispute_tpl_text)
    await message.answer(
        f"Кнопка будет <b>{percent}%</b>.\n\n"
        "Теперь пришлите текст, который уйдет клиенту.\n"
        "Можно несколько строк. <code>{percent}</code> заменится на число скидки."
    )


@router.message(AdminState.waiting_for_dispute_tpl_text)
async def admin_dispute_template_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("🔶 Пришлите текст сообщения.")
        return
    data = await state.get_data()
    percent = int(data.get("dispute_tpl_percent") or 0)
    saved = SettingsService.add_dispute_reply(percent, text)
    await state.clear()
    if not saved:
        await message.answer("❗ Шаблон не сохранен. Проверьте процент и что свободных кнопок не больше 8.")
    else:
        await message.answer(f"✅ Кнопка {saved['percent']}% добавлена в диспуты.")
    await _show_dispute_templates(message, state)


@router.callback_query(F.data.startswith("adtpl_edit_"))
async def admin_dispute_template_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        index = int(callback.data[len("adtpl_edit_"):])
    except ValueError:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    items = SettingsService.get_dispute_replies()
    if index < 0 or index >= len(items):
        await callback.answer("Шаблон уже удален", show_alert=True)
        return
    item = items[index]
    await state.set_state(AdminState.waiting_for_dispute_tpl_edit)
    await state.update_data(dispute_tpl_index=index)
    await callback.message.edit_text(
        f"✏️ Текст для кнопки <b>{item['percent']}%</b>\n\n"
        "Пришлите новый текст. Старый:\n"
        f"{html.escape(item['text'][:700])}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adtpl_menu")]]
        ),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_dispute_tpl_edit)
async def admin_dispute_template_edit_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("🔶 Пришлите текст сообщения.")
        return
    data = await state.get_data()
    saved = SettingsService.update_dispute_reply_text(int(data.get("dispute_tpl_index") or -1), text)
    await state.clear()
    if not saved:
        await message.answer("❗ Шаблон не найден. Откройте список заново.")
    else:
        await message.answer(f"✅ Текст кнопки {saved['percent']}% обновлен.")
    await _show_dispute_templates(message, state)


@router.callback_query(F.data.startswith("adtpl_del_"))
async def admin_dispute_template_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        index = int(callback.data[len("adtpl_del_"):])
    except ValueError:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    if not SettingsService.delete_dispute_reply(index):
        await callback.answer("Шаблон уже удален", show_alert=True)
    await _show_dispute_templates(callback, state)


def _render_dispute_template(text: str, percent: int) -> str:
    rendered = str(text or "").strip()
    rendered = rendered.replace("{percent}", str(percent)).replace("{скидка}", str(percent))
    return rendered


@router.callback_query(F.data.regexp(r"^adtp_\d+_\d+$"))
async def admin_dispute_apply_template(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, user_raw, index_raw = callback.data.split("_")
        user_id = int(user_raw)
        index = int(index_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    items = SettingsService.get_dispute_replies()
    if index < 0 or index >= len(items):
        await callback.answer("Этот шаблон уже удален из настроек", show_alert=True)
        return
    item = items[index]
    percent = int(item["percent"])
    body = _render_dispute_template(item["text"], percent)
    try:
        await bot.send_message(user_id, body)
    except TelegramBadRequest:
        try:
            await bot.send_message(user_id, html.escape(body))
        except Exception as exc:
            await callback.answer("Не удалось отправить текст клиенту", show_alert=True)
            await callback.message.answer(f"❗ Клиент не получил шаблон {percent}%: {exc}")
            return
    except Exception as exc:
        await callback.answer("Не удалось отправить текст клиенту", show_alert=True)
        await callback.message.answer(f"❗ Клиент не получил шаблон {percent}%: {exc}")
        return

    UserService.set_personal_discount(user_id, percent, 1)
    await callback.answer(f"Отправлено. Скидка {percent}% на 1 покупку", show_alert=True)
    await callback.message.answer(
        f"✅ Клиенту <code>{user_id}</code> отправлен шаблон <b>{percent}%</b>.\n"
        f"Скидка <b>{percent}%</b> записана в профиль ровно на 1 покупку."
    )


# --- Чат (ручной) ---

@router.callback_query(F.data.startswith("admin_dispute_verdict_"))
async def admin_dispute_verdict_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Вы не администратор.", show_alert=True)
        return

    user_id_raw = callback.data[len("admin_dispute_verdict_"):]
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID", show_alert=True)
        return

    await state.update_data(dispute_verdict_user_id=user_id)
    await state.set_state(AdminState.waiting_for_dispute_decision)
    uname = UserService.get_username(user_id)
    display = f"@{uname}" if uname else str(user_id)
    await callback.message.answer(
        f"⚖️ <b>Решение по диспуту</b>\n"
        f"Клиент: {display} (<code>{user_id}</code>)\n\n"
        "Напишите решение одним сообщением - оно уйдёт клиенту.\n"
        "Чат с покупателем открывать не нужно.\n"
        "Отмена: /cancel"
    )
    await callback.answer()


@router.message(Command("cancel"), AdminState.waiting_for_dispute_decision)
@router.message(Command("stop_chat"), AdminState.waiting_for_dispute_decision)
async def admin_dispute_verdict_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛑 Отправка решения отменена.")


@router.message(AdminState.waiting_for_dispute_decision)
async def admin_dispute_verdict_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_user_id = data.get("dispute_verdict_user_id")
    if not target_user_id:
        await state.clear()
        await message.answer("Ошибка: не выбран клиент для решения.")
        return

    try:
        uid_int = int(target_user_id)
        await bot.send_message(uid_int, "⚖️ <b>Решение по вашему диспуту</b>")
        await message.copy_to(uid_int)
    except Exception as exc:
        await message.answer(f"❗ Не удалось отправить решение: {exc}")
        return

    await state.clear()
    await message.answer("✅ Решение отправлено клиенту. Чат не открывался.")


@router.callback_query(F.data.startswith('admin_chat_'))
async def start_admin_chat(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer('Вы не администратор.', show_alert=True)
        return
        
    user_id = callback.data.split('_')[2]
    # Попробуем найти имя
    try:
        uid_int = int(user_id)
        uname = UserService.get_username(uid_int)
        display = f'@{uname}' if uname else user_id
    except Exception:
        uid_int = None
        display = user_id

    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminState.chatting_with_user)
    
    await callback.message.answer(
        f'👨‍💻 <b>Режим чата активирован.</b>\n'
        f'Вы общаетесь с пользователем: {display}\n'
        f'Все ваши сообщения будут пересланы ему.\n'
        f'Для выхода нажмите: /stop_chat'
    )
    await callback.answer()

    if uid_int is None:
        return

    user_fsm = get_user_fsm(bot, dispatcher, uid_int)
    user_state = await user_fsm.get_state()
    order = UserService.get_active_order(uid_int)
    # Клиенту не пишем, что оператор подключился: ответы уходят как обычные сообщения бота.
    if order or (user_state and "waiting_for_receipt" in str(user_state)):
        return

    await user_fsm.set_state(OrderState.chat_with_support)

@router.message(Command('stop_chat'), AdminState.chatting_with_user)
async def stop_chat(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('🛑 Чат закрыт у вас. Клиенту ничего не писали.')


@router.callback_query(F.data.startswith("admin_close_chat_"))
async def admin_close_chat_for_both(callback: CallbackQuery, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    if not is_admin(callback.from_user.id):
        await callback.answer("Вы не администратор.", show_alert=True)
        return

    user_id_raw = callback.data[len("admin_close_chat_"):]
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID", show_alert=True)
        return

    data = await state.get_data()
    if str(data.get("target_user_id") or "") == str(user_id):
        await state.clear()

    try:
        await send_user_to_start(
            bot,
            dispatcher,
            user_id,
            "🚪 <b>Чат закрыт.</b>\n"
            "Сообщения в поддержку больше не идут.\n\n"
            "👇 Введите название города:",
        )
    except Exception:
        logger.exception("Failed to close support chat for user %s", user_id)

    await callback.answer("Чат закрыт")
    try:
        await callback.message.answer(f"🚪 Чат с <code>{user_id}</code> закрыт у вас и у клиента.")
    except Exception:
        pass

@router.message(AdminState.chatting_with_user)
async def admin_message_proxy(message: Message, state: FSMContext, bot: Bot, dispatcher: Dispatcher):
    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    
    if target_user_id:
        try:
            uid_int = int(target_user_id)
            user_fsm = get_user_fsm(bot, dispatcher, uid_int)
            user_state = await user_fsm.get_state()
            reply_kb = (
                kb.get_support_chat_keyboard()
                if user_state == OrderState.chat_with_support.state
                else None
            )
            try:
                sent = await message.copy_to(chat_id=uid_int, reply_markup=reply_kb)
            except Exception:
                sent = await message.copy_to(chat_id=uid_int)
                if reply_kb is not None:
                    await bot.send_message(
                        uid_int,
                        "Чтобы закрыть чат, нажмите кнопку внизу экрана.",
                        reply_markup=reply_kb,
                    )
            await message.react([{'type': 'emoji', 'emoji': '👍'}])
            delete_cb = f"admin_del_sent_{target_user_id}_{sent.message_id}"
            await message.answer(
                '🗑 Управление сообщением',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='🗑 Удалить у клиента', callback_data=delete_cb)]
                ])
            )
        except Exception as e:
            await message.answer(f'❗ Не удалось отправить сообщение: {e}')
    else:
        await message.answer('Ошибка: не выбран пользователь для чата.')

@router.callback_query(F.data == "adm_rev_gen_mass")
async def adm_rev_gen_mass_handler(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
    logger.info("Admin %s requested manual review generation", callback.from_user.id)

    try:
        await callback.answer("⏳ Запустил генерацию...", show_alert=False)
    except TelegramBadRequest:
        logger.warning("Callback query too old for immediate answer", exc_info=False)

    admin_id = callback.from_user.id

    async def _run_generation() -> None:
        try:
            count = await ReviewsService.generate_new_review_task(trigger="manual")
        except Exception:
            logger.exception("Manual review generation failed")
            try:
                await bot.send_message(admin_id, "🔶 Ошибка генерации. Подробности в логах.")
            except Exception:
                logger.exception("Failed to tell admin that review generation failed")
            return
        logger.info("Manual review generation finished with %s reviews", count)
        try:
            await bot.send_message(admin_id, f"✅ Сгенерировано {count} новых отзывов!")
        except Exception:
            logger.exception("Failed to tell admin that review generation finished")

    asyncio.create_task(_run_generation())


@router.callback_query(F.data == "adm_rev_delete_last")
async def adm_rev_delete_last_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    deleted = ReviewsService.remove_last_generated_batch()
    if deleted:
        try:
            await callback.answer("Удалено", show_alert=False)
        except TelegramBadRequest:
            logger.warning("Callback answer failed for delete-last reviews", exc_info=False)
        await callback.message.answer(f"🧹 Удалено {deleted} отзывов из последней волны.")
    else:
        await callback.answer("Новых отзывов для удаления нет", show_alert=True)

