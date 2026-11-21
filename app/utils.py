from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.states import OrderState


def get_now_msk() -> datetime:
    """
    Возвращает текущее время МСК (UTC+3) как naive datetime (без информации о таймзоне),
    чтобы сохранить совместимость с текущим кодом (сравнение с strptime).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)


def order_ttl(order: dict | None) -> timedelta:
    """How long an unpaid active order lives before auto-cancel."""
    payload = order or {}
    order_type = str(payload.get("type") or "")
    if payload.get("status") == "paid_waiting_admin":
        return timedelta(days=2)
    if order_type == "ratecore":
        try:
            minutes = int(payload.get("ttl_minutes") or 20)
        except (TypeError, ValueError):
            minutes = 20
        return timedelta(minutes=max(5, minutes))
    if order_type in {"btc", "ltc", "crypto"}:
        return timedelta(hours=1)
    if order_type == "yoomoney":
        return timedelta(minutes=25)
    try:
        minutes = int(payload.get("ttl_minutes") or 20)
    except (TypeError, ValueError):
        minutes = 20
    return timedelta(minutes=max(1, minutes))


def get_user_fsm(bot: Bot, dispatcher: Dispatcher, user_id: int) -> FSMContext:
    uid = int(user_id)
    return FSMContext(
        storage=dispatcher.fsm.storage,
        key=StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid),
    )


async def clear_user_order_fsm(bot: Bot, dispatcher: Dispatcher, user_id: int) -> None:
    """Снимает ожидание чека/чата и возвращает пользователя в главное меню."""
    fsm = get_user_fsm(bot, dispatcher, user_id)
    await fsm.clear()
    await fsm.set_state(OrderState.main_menu)


async def send_home_to_chat(
    bot: Bot,
    chat_id: int,
    *,
    state: FSMContext | None = None,
    prefix: str | None = None,
) -> None:
    import app.keyboards as kb

    if state is not None:
        await state.clear()
        await state.set_state(OrderState.main_menu)

    from app.services.settings import SettingsService

    screen = SettingsService.get_home_screen_text()
    extra = (prefix or "").strip()
    if extra:
        screen = extra + "\n\n" + screen
    try:
        await bot.send_message(
            int(chat_id),
            "🛒 <b>Панель Store</b>\n«Начать покупку» — выбор города и товаров. «На главную» — стартовый экран.",
            reply_markup=kb.get_base_keyboard(int(chat_id)),
            disable_web_page_preview=True,
        )
        await bot.send_message(
            int(chat_id),
            screen,
            reply_markup=kb.get_main_menu_keyboard(),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def send_user_to_start(
    bot: Bot,
    dispatcher: Dispatcher,
    user_id: int,
    text: str,
) -> None:
    fsm = get_user_fsm(bot, dispatcher, user_id)
    await send_home_to_chat(bot, int(user_id), state=fsm, prefix=text)
