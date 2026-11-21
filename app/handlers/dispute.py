import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import ADMIN_IDS
from app.services.blacklist import BlacklistService
from app.services.disputes import (
    KIND_EMPTY,
    KIND_LOST,
    KIND_UNDERWEIGHT,
    LOCATION_PHOTO_STEPS,
    LOCATION_QUESTIONS,
    UNDERWEIGHT_STEPS,
    build_admin_ticket_text,
    build_dispute_need_purchase_text,
    build_dispute_start_text,
    build_location_intro_text,
    build_location_photo_step_text,
    build_location_preview_text,
    build_location_question_text,
    build_underweight_ready_text,
    build_underweight_step_text,
    build_user_ticket_copy,
    user_can_open_dispute,
)
from app.states import DisputeState, OrderState
from app.utils import send_home_to_chat
import app.keyboards as kb

router = Router()
logger = logging.getLogger(__name__)

_DISPUTE_START_TEXTS = {
    kb.BTN_DISPUTE,
    "Создать Диспут",
    "⚖️ Создать диспут",
    "Создать диспут",
}

_KIND_BY_CALLBACK = {
    "dispute_kind_underweight": KIND_UNDERWEIGHT,
    "dispute_kind_lost": KIND_LOST,
    "dispute_kind_empty": KIND_EMPTY,
}


async def _safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest:
        pass


def _extract_media(message: Message) -> dict[str, str] | None:
    if message.photo:
        photo = message.photo[-1]
        return {"file_id": photo.file_id, "kind": "photo"}
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        return {"file_id": document.file_id, "kind": "document"}
    return None


async def _notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(int(admin_id), text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send dispute ticket to admin %s", admin_id)


async def _send_photos_to_admins(bot: Bot, photos: list[dict]) -> None:
    for index, item in enumerate(photos, start=1):
        file_id = item.get("file_id")
        if not file_id:
            continue
        caption = f"{index}/{len(photos)} {item.get('title') or 'Фото'}"
        for admin_id in ADMIN_IDS:
            try:
                if item.get("kind") == "document":
                    await bot.send_document(int(admin_id), file_id, caption=caption)
                else:
                    await bot.send_photo(int(admin_id), file_id, caption=caption)
            except Exception:
                logger.exception("Failed to send dispute photo to admin %s", admin_id)


def _dispute_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": data.get("dispute_kind") or KIND_UNDERWEIGHT,
        "photos": list(data.get("dispute_photos") or []),
        "answers": list(data.get("dispute_answers") or []),
        "comment": str(data.get("dispute_comment") or ""),
    }


async def _reset_to_city(target: Message, state: FSMContext, text: str) -> None:
    await send_home_to_chat(target.bot, target.chat.id, state=state, prefix=text)


async def _cancel_dispute(target: Message, state: FSMContext) -> None:
    await _reset_to_city(
        target,
        state,
        "🚪 Диспут отменён.",
    )


async def start_dispute_flow(target: Message, state: FSMContext, *, user_id: int) -> None:
    if BlacklistService.is_banned(user_id):
        await target.answer("🔒 Доступ закрыт: обращение в Store открыть нельзя.")
        return

    if not user_can_open_dispute(user_id):
        await target.answer(
            build_dispute_need_purchase_text(),
            reply_markup=kb.get_dispute_need_purchase_keyboard(),
        )
        return

    await state.set_state(DisputeState.choosing_kind)
    await state.update_data(
        dispute_kind=None,
        dispute_step=0,
        dispute_photos=[],
        dispute_answers=[],
        dispute_comment="",
    )
    await target.answer(
        build_dispute_start_text(),
        reply_markup=kb.get_dispute_flow_keyboard(),
    )
    await target.answer(
        "👇 Выберите причину:",
        reply_markup=kb.get_dispute_kind_keyboard(),
    )


async def _show_underweight_step(message: Message, state: FSMContext, step_index: int) -> None:
    await state.update_data(dispute_step=step_index)
    await state.set_state(DisputeState.waiting_photo)
    await message.answer(
        build_underweight_step_text(step_index),
        reply_markup=kb.get_dispute_photo_keyboard(),
    )


async def _show_location_question(message: Message, state: FSMContext, step_index: int) -> None:
    await state.update_data(dispute_step=step_index)
    await state.set_state(DisputeState.waiting_answer)
    await message.answer(build_location_question_text(step_index))


async def _show_location_photo_step(message: Message, state: FSMContext, step_index: int) -> None:
    await state.update_data(dispute_step=step_index)
    await state.set_state(DisputeState.waiting_photo)
    await message.answer(
        build_location_photo_step_text(step_index),
        reply_markup=kb.get_dispute_photo_keyboard(),
    )


async def _show_underweight_ready(message: Message, state: FSMContext) -> None:
    await state.set_state(DisputeState.underweight_ready)
    await message.answer(
        build_underweight_ready_text(),
        reply_markup=kb.get_dispute_underweight_ready_keyboard(),
    )


async def _show_location_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    snap = _dispute_snapshot(data)
    await state.set_state(DisputeState.location_ready)
    await message.answer(
        build_location_preview_text(snap["kind"], snap["answers"], snap["photos"]),
        reply_markup=kb.get_dispute_preview_keyboard(),
    )


async def _submit_dispute(
    target: Message,
    state: FSMContext,
    bot: Bot,
    *,
    user_id: int,
    username: str | None,
) -> None:
    data = await state.get_data()
    snap = _dispute_snapshot(data)

    ticket = build_admin_ticket_text(
        user_id=user_id,
        username=username,
        kind=snap["kind"],
        answers=snap["answers"],
        photos=snap["photos"],
        comment=snap["comment"],
    )
    await _notify_admins(bot, ticket, reply_markup=kb.get_admin_dispute_keyboard(user_id))
    await _send_photos_to_admins(bot, snap["photos"])

    await _reset_to_city(
        target,
        state,
        build_user_ticket_copy(
            kind=snap["kind"],
            answers=snap["answers"],
            photos=snap["photos"],
            comment=snap["comment"],
        ),
    )


@router.message(F.text.in_(_DISPUTE_START_TEXTS))
async def dispute_start_from_menu(message: Message, state: FSMContext):
    user = message.from_user
    await start_dispute_flow(message, state, user_id=user.id if user else 0)


@router.callback_query(F.data == "dispute_start")
async def dispute_start_from_rules(callback: CallbackQuery, state: FSMContext):
    await _safe_callback_answer(callback)
    if callback.message:
        await start_dispute_flow(callback.message, state, user_id=callback.from_user.id)


@router.callback_query(F.data == "dispute_cancel")
async def dispute_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await _safe_callback_answer(callback, "Диспут отменён")
    if callback.message:
        await _cancel_dispute(callback.message, state)


@router.message(StateFilter(DisputeState), F.text.in_(kb.CANCEL_TEXT_ALIASES))
async def dispute_cancel_message(message: Message, state: FSMContext):
    await _cancel_dispute(message, state)


@router.callback_query(F.data.in_(set(_KIND_BY_CALLBACK)))
async def dispute_choose_kind(callback: CallbackQuery, state: FSMContext):
    kind = _KIND_BY_CALLBACK.get(callback.data or "")
    if not kind:
        await _safe_callback_answer(callback, "Неизвестный тип", show_alert=True)
        return

    if BlacklistService.is_banned(callback.from_user.id):
        await _safe_callback_answer(callback, "Вы заблокированы", show_alert=True)
        return

    if not user_can_open_dispute(callback.from_user.id):
        await _safe_callback_answer(callback, "Нужна хотя бы 1 покупка", show_alert=True)
        if callback.message:
            await callback.message.answer(
                build_dispute_need_purchase_text(),
                reply_markup=kb.get_dispute_need_purchase_keyboard(),
            )
        return

    await _safe_callback_answer(callback)
    await state.update_data(
        dispute_kind=kind,
        dispute_step=0,
        dispute_photos=[],
        dispute_answers=[],
        dispute_comment="",
    )
    message = callback.message
    if message is None:
        return

    if kind == KIND_UNDERWEIGHT:
        await _show_underweight_step(message, state, 0)
        return

    await state.set_state(DisputeState.location_intro)
    await message.answer(
        build_location_intro_text(kind),
        reply_markup=kb.get_dispute_location_intro_keyboard(),
    )


@router.callback_query(F.data == "dispute_location_form")
async def dispute_start_location_form(callback: CallbackQuery, state: FSMContext):
    await _safe_callback_answer(callback)
    data = await state.get_data()
    if data.get("dispute_kind") not in {KIND_LOST, KIND_EMPTY}:
        if callback.message:
            await start_dispute_flow(callback.message, state, user_id=callback.from_user.id)
        return
    if callback.message:
        await _show_location_question(callback.message, state, 0)


@router.message(DisputeState.choosing_kind)
async def dispute_kind_hint(message: Message):
    if (message.text or "").strip() in _DISPUTE_START_TEXTS:
        return
    await message.answer("👇 Выберите причину кнопкой на экране.", reply_markup=kb.get_dispute_kind_keyboard())


@router.message(DisputeState.location_intro)
async def dispute_location_intro_hint(message: Message):
    if (message.text or "").strip() in _DISPUTE_START_TEXTS:
        return
    await message.answer(
        "👇 Нажмите «Заполнить анкету Store», если точку проверить не удалось.",
        reply_markup=kb.get_dispute_location_intro_keyboard(),
    )


@router.callback_query(F.data == "dispute_provide_photo", DisputeState.waiting_photo)
async def dispute_provide_photo(callback: CallbackQuery):
    await _safe_callback_answer(callback, "📸 Пришлите фото в этот чат", show_alert=True)


@router.message(DisputeState.waiting_photo, F.photo | F.document)
async def dispute_catch_photo(message: Message, state: FSMContext):
    media = _extract_media(message)
    if media is None:
        await message.answer("🔶 Нужен снимок. Пришлите фото или файл изображения.")
        return

    data = await state.get_data()
    kind = data.get("dispute_kind")
    step_index = int(data.get("dispute_step") or 0)
    photos = list(data.get("dispute_photos") or [])

    if kind == KIND_UNDERWEIGHT:
        if step_index < 0 or step_index >= len(UNDERWEIGHT_STEPS):
            await message.answer("🔶 Шаг фото сброшен. Откройте обращение заново.")
            return
        step = UNDERWEIGHT_STEPS[step_index]
        photos.append({"id": step["id"], "title": f"{step['emoji']} {step['title']}", **media})
        await state.update_data(dispute_photos=photos)
        next_index = step_index + 1
        if next_index < len(UNDERWEIGHT_STEPS):
            await message.answer("✅ Фото принято.")
            await _show_underweight_step(message, state, next_index)
            return
        await message.answer("✅ Фото принято.")
        await _show_underweight_ready(message, state)
        return

    if step_index < 0 or step_index >= len(LOCATION_PHOTO_STEPS):
        await message.answer("🔶 Шаг фото сброшен. Откройте обращение заново.")
        return
    step = LOCATION_PHOTO_STEPS[step_index]
    photos.append({"id": step["id"], "title": f"{step['emoji']} {step['title']}", **media})
    await state.update_data(dispute_photos=photos)
    next_index = step_index + 1
    if next_index < len(LOCATION_PHOTO_STEPS):
        await message.answer("✅ Фото принято.")
        await _show_location_photo_step(message, state, next_index)
        return
    await message.answer("✅ Фото принято.")
    await _show_location_preview(message, state)


@router.message(DisputeState.waiting_photo)
async def dispute_photo_expected(message: Message):
    if (message.text or "").strip() in _DISPUTE_START_TEXTS:
        return
    await message.answer(
        "📸 Сейчас нужен кадр. Нажмите «Приложить фото» и отправьте снимок в чат.",
        reply_markup=kb.get_dispute_photo_keyboard(),
    )


@router.message(DisputeState.waiting_answer)
async def dispute_catch_answer(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text in _DISPUTE_START_TEXTS or text in {kb.BTN_CANCEL, kb.BTN_RULES, kb.BTN_SUPPORT}:
        return
    if not text:
        await message.answer("🔶 Напишите ответ текстом одним сообщением.")
        return

    data = await state.get_data()
    step_index = int(data.get("dispute_step") or 0)
    if step_index < 0 or step_index >= len(LOCATION_QUESTIONS):
        await message.answer("🔶 Анкета сброшена. Откройте обращение заново.")
        return

    question = LOCATION_QUESTIONS[step_index]
    answers = list(data.get("dispute_answers") or [])
    answers.append({"id": question["id"], "title": question["title"], "text": text})
    await state.update_data(dispute_answers=answers)

    next_index = step_index + 1
    if next_index < len(LOCATION_QUESTIONS):
        await _show_location_question(message, state, next_index)
        return
    await _show_location_photo_step(message, state, 0)


@router.callback_query(F.data == "dispute_add_comment", DisputeState.underweight_ready)
async def dispute_add_comment(callback: CallbackQuery, state: FSMContext):
    await _safe_callback_answer(callback)
    await state.set_state(DisputeState.waiting_comment)
    if callback.message:
        await callback.message.answer(
            "✍️ Напишите куратору Store одним сообщением. После этого обращение уйдет сразу."
        )


@router.message(DisputeState.waiting_comment)
async def dispute_catch_comment(message: Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text in kb.CANCEL_TEXT_ALIASES:
        await _cancel_dispute(message, state)
        return
    if not text:
        await message.answer("🔶 Напишите текст или нажмите «На главную».")
        return
    await state.update_data(dispute_comment=text)
    user = message.from_user
    await _submit_dispute(
        message,
        state,
        bot,
        user_id=user.id if user else 0,
        username=user.username if user else None,
    )


@router.callback_query(F.data == "dispute_send", StateFilter(DisputeState.underweight_ready, DisputeState.location_ready))
async def dispute_send_now(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await _safe_callback_answer(callback, "Отправляем оператору")
    if callback.message:
        await _submit_dispute(
            callback.message,
            state,
            bot,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
        )


@router.message(StateFilter(DisputeState.underweight_ready, DisputeState.location_ready))
async def dispute_ready_hint(message: Message):
    await message.answer("👇 Используйте кнопки под сообщением: отправить или отменить.")
