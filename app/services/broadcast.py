from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from app.services.broadcast_media import MAX_MEDIA_BYTES, BroadcastMediaStore
from app.services.users import UserService

logger = logging.getLogger(__name__)

SEND_PAUSE_SEC = 0.04
ALBUM_WAIT_SEC = 1.2
CAPTION_LIMIT = 1024

MEDIA_KIND_LABELS = {
    "photo": "📷 фото",
    "video": "🎬 видео",
    "document": "📎 файл",
    "animation": "🎞 GIF",
    "audio": "🎵 аудио",
    "voice": "🎤 голосовое",
    "video_note": "⏺ кружок",
    "sticker": "🌟 стикер",
    "album": "🖼 альбом",
}

_album_lock = asyncio.Lock()
_album_buckets: dict[str, list[Message]] = {}
_album_leaders: set[str] = set()


def media_kind_label(kind: str | None) -> str:
    return MEDIA_KIND_LABELS.get(str(kind or "").lower(), "медиа")


def is_copyable_message(message: Message) -> bool:
    if message_media_kind(message):
        return True
    for attr in ("contact", "venue", "location", "poll", "dice"):
        if getattr(message, attr, None):
            return True
    return False


def message_html(message: Message) -> str | None:
    text = (getattr(message, "html_text", None) or "").strip()
    return text or None


def message_media_kind(message: Message) -> str | None:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "animation", None):
        return "animation"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "video_note", None):
        return "video_note"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "text", None):
        return "text"
    return None


def _telegram_file(message: Message):
    kind = message_media_kind(message)
    if kind == "photo":
        photos = message.photo or []
        return photos[-1] if photos else None
    if kind == "animation":
        return message.animation
    if kind == "video":
        return message.video
    if kind == "video_note":
        return message.video_note
    if kind == "voice":
        return message.voice
    if kind == "audio":
        return message.audio
    if kind == "sticker":
        return message.sticker
    if kind == "document":
        return message.document
    return None


def _filename_for(message: Message, kind: str) -> str:
    if kind == "photo":
        return "photo.jpg"
    if kind == "animation":
        name = getattr(message.animation, "file_name", None)
        return name or "animation.mp4"
    if kind == "video":
        name = getattr(message.video, "file_name", None)
        return name or "video.mp4"
    if kind == "video_note":
        return "video_note.mp4"
    if kind == "voice":
        return "voice.ogg"
    if kind == "audio":
        name = getattr(message.audio, "file_name", None)
        return name or "audio.mp3"
    if kind == "sticker":
        sticker = message.sticker
        if getattr(sticker, "is_video", False):
            return "sticker.webm"
        if getattr(sticker, "is_animated", False):
            return "sticker.tgs"
        return "sticker.webp"
    if kind == "document":
        name = getattr(message.document, "file_name", None)
        return name or "file.bin"
    return "file.bin"


async def collect_album(message: Message, *, delay: float = ALBUM_WAIT_SEC) -> list[Message] | None:
    """Return the full media group, or None if this update is a follower part."""
    group_id = getattr(message, "media_group_id", None)
    if not group_id:
        return [message]
    chat_id = message.chat.id if message.chat else 0
    key = f"{chat_id}:{group_id}"
    async with _album_lock:
        bucket = _album_buckets.setdefault(key, [])
        bucket.append(message)
        is_leader = key not in _album_leaders
        if is_leader:
            _album_leaders.add(key)
    if not is_leader:
        return None
    await asyncio.sleep(delay)
    async with _album_lock:
        messages = list(_album_buckets.pop(key, []))
        _album_leaders.discard(key)
    messages.sort(key=lambda item: item.message_id or 0)
    return messages


async def _sleep_retry(err: TelegramRetryAfter) -> None:
    wait_for = float(getattr(err, "retry_after", 1) or 1)
    await asyncio.sleep(min(wait_for + 0.15, 35))


async def send_text_to_all_users(bot: Bot, text: str, bot_id=None, *, include_unassigned: bool = True) -> tuple[int, int]:
    """Each bot token can only deliver to people who started THAT bot.

    Try every known user id. Telegram failures (never started / blocked) are normal
    when several bots share one user database - do not mark those users inactive.
    """
    del include_unassigned, bot_id
    if not (text or "").strip():
        return 0, 0
    return await _deliver_to_all(bot, lambda chat_id: bot.send_message(chat_id, text))


async def _deliver_to_all(bot: Bot, sender) -> tuple[int, int]:
    users = UserService.get_all_users()
    delivered = 0
    failed = 0
    for user_id in users:
        chat_id = int(user_id)
        try:
            await sender(chat_id)
            delivered += 1
        except TelegramRetryAfter as err:
            await _sleep_retry(err)
            try:
                await sender(chat_id)
                delivered += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(SEND_PAUSE_SEC)
    return delivered, failed


async def copy_messages_to_all_users(bot: Bot, messages: list[Message]) -> tuple[int, int]:
    usable = [item for item in messages if item and item.message_id]
    if not usable:
        return 0, 0
    from_chat_id = usable[0].chat.id
    message_ids = [item.message_id for item in usable]

    async def sender(chat_id: int):
        if len(message_ids) == 1:
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_ids[0],
                )
                return
            except TelegramBadRequest:
                await usable[0].send_copy(chat_id=chat_id)
                return
        try:
            await bot.copy_messages(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_ids=message_ids,
            )
        except TelegramBadRequest:
            for item in usable:
                try:
                    await bot.copy_message(
                        chat_id=chat_id,
                        from_chat_id=from_chat_id,
                        message_id=item.message_id,
                    )
                except TelegramBadRequest:
                    await item.send_copy(chat_id=chat_id)

    return await _deliver_to_all(bot, sender)


async def download_message_media(bot: Bot, message: Message) -> dict:
    kind = message_media_kind(message)
    if not kind or kind == "text":
        raise ValueError("no media")
    file_obj = _telegram_file(message)
    if file_obj is None:
        raise ValueError("no media")
    size = int(getattr(file_obj, "file_size", 0) or 0)
    if size > MAX_MEDIA_BYTES:
        raise ValueError("file too large")
    buffer = BytesIO()
    await bot.download(file_obj, destination=buffer)
    data = buffer.getvalue()
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("file too large")
    mime = getattr(file_obj, "mime_type", None)
    return {
        "type": kind,
        "filename": _filename_for(message, kind),
        "mime": mime,
        "data": data,
    }


async def save_auto_broadcast_media(bot: Bot, worker_id: str, messages: list[Message]) -> dict:
    """Download media from admin messages and persist it for every bot token."""
    items = []
    caption_html = None
    errors = []
    for message in messages:
        html = message_html(message)
        if html and not caption_html:
            caption_html = html
        kind = message_media_kind(message)
        if kind in (None, "text"):
            continue
        try:
            items.append(await download_message_media(bot, message))
        except Exception as exc:
            logger.warning("Broadcast media download failed: %s", exc)
            errors.append(str(exc))
    wanted_media = any(message_media_kind(item) not in (None, "text") for item in messages)
    if wanted_media and not items:
        raise ValueError(errors[0] if errors else "no media")
    if not items and not caption_html:
        raise ValueError(errors[0] if errors else "empty")
    total = sum(len(item.get("data") or b"") for item in items)
    if total > MAX_MEDIA_BYTES * 2:
        raise ValueError("file too large")
    if items:
        BroadcastMediaStore.save(
            worker_id,
            {"caption_html": caption_html, "items": items},
        )
        kinds = {item["type"] for item in items}
        if len(items) > 1:
            kind = "album"
        else:
            kind = items[0]["type"]
        return {
            "text": caption_html,
            "media": {
                "kind": kind,
                "filename": items[0]["filename"],
                "count": len(items),
                "types": sorted(kinds),
            },
        }
    BroadcastMediaStore.delete(worker_id)
    return {"text": caption_html, "media": None}


async def send_media_to_all_users(bot: Bot, items: list[dict], caption: str | None = None) -> tuple[int, int]:
    if not items:
        if caption:
            return await send_text_to_all_users(bot, caption)
        return 0, 0
    caption_html = (caption or "").strip() or None
    extra_text = None
    if caption_html and len(caption_html) > CAPTION_LIMIT:
        extra_text = caption_html
        caption_html = None

    async def sender(chat_id: int):
        await _send_media_bundle(bot, chat_id, items, caption_html, extra_text)

    return await _deliver_to_all(bot, sender)


def _input_file(item: dict) -> BufferedInputFile:
    return BufferedInputFile(item["data"], filename=str(item.get("filename") or "file.bin"))


async def _send_media_bundle(
    bot: Bot,
    chat_id: int,
    items: list[dict],
    caption_html: str | None,
    extra_text: str | None,
) -> None:
    if len(items) == 1:
        await _send_one_media(bot, chat_id, items[0], caption_html)
    else:
        types = {str(item.get("type") or "") for item in items}
        if types <= {"photo", "video"} or types <= {"document"}:
            media = []
            for index, item in enumerate(items):
                cap = caption_html if index == 0 else None
                media.append(_to_input_media(item, cap))
            await bot.send_media_group(chat_id, media)
        else:
            first = True
            for item in items:
                await _send_one_media(bot, chat_id, item, caption_html if first else None)
                first = False
    if extra_text:
        await bot.send_message(chat_id, extra_text)


def _to_input_media(item: dict, caption: str | None):
    file = _input_file(item)
    kind = str(item.get("type") or "document")
    kwargs = {}
    if caption:
        kwargs["caption"] = caption
        kwargs["parse_mode"] = "HTML"
    if kind == "photo":
        return InputMediaPhoto(media=file, **kwargs)
    if kind == "video":
        return InputMediaVideo(media=file, **kwargs)
    return InputMediaDocument(media=file, **kwargs)


async def _send_one_media(bot: Bot, chat_id: int, item: dict, caption: str | None) -> None:
    file = _input_file(item)
    kind = str(item.get("type") or "document")
    caption_kwargs = {}
    if caption and kind in {"photo", "video", "document", "animation", "audio"}:
        caption_kwargs["caption"] = caption
        caption_kwargs["parse_mode"] = "HTML"
        caption = None
    if kind == "photo":
        await bot.send_photo(chat_id, file, **caption_kwargs)
    elif kind == "video":
        await bot.send_video(chat_id, file, **caption_kwargs)
    elif kind == "animation":
        await bot.send_animation(chat_id, file, **caption_kwargs)
    elif kind == "audio":
        await bot.send_audio(chat_id, file, **caption_kwargs)
    elif kind == "voice":
        await bot.send_voice(chat_id, file)
    elif kind == "video_note":
        await bot.send_video_note(chat_id, file)
    elif kind == "sticker":
        await bot.send_sticker(chat_id, file)
    else:
        await bot.send_document(chat_id, file, **caption_kwargs)
    if caption:
        await bot.send_message(chat_id, caption)


async def send_auto_broadcast(bot: Bot, worker_id: str) -> tuple[int, int]:
    from app.services.settings import SettingsService

    cfg = SettingsService.get_auto_broadcast(worker_id)
    text = (cfg.get("text") or "").strip() or None
    media_meta = cfg.get("media")
    if media_meta:
        bundle = BroadcastMediaStore.load_decoded(str(worker_id))
        if bundle and bundle.get("items"):
            caption = (bundle.get("caption_html") or text or "").strip() or None
            return await send_media_to_all_users(bot, bundle["items"], caption=caption)
    if text:
        return await send_text_to_all_users(bot, text)
    return 0, 0
