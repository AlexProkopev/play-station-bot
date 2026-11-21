from __future__ import annotations

import logging

from aiogram import Bot

from app.services.users import UserService

logger = logging.getLogger(__name__)

REFERRAL_PERCENT = 5.0


def calculate_referral_bonus(purchase_amount) -> float:
    try:
        amount = float(purchase_amount or 0)
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    return round(amount * REFERRAL_PERCENT / 100.0, 2)


def _format_rub(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return f"{value:.0f}"
    return f"{value:.2f}"


def apply_purchase_referral(buyer_id: int, purchase_amount) -> dict | None:
    buyer = UserService.get_user(buyer_id) or {}
    raw_ref = buyer.get("referrer_id")
    if not raw_ref:
        return None
    try:
        referrer_id = int(raw_ref)
        buyer_int = int(buyer_id)
    except (TypeError, ValueError):
        return None
    if referrer_id == buyer_int:
        return None
    if not UserService.get_user(referrer_id):
        return None

    UserService.note_paying_referral(referrer_id)
    bonus = calculate_referral_bonus(purchase_amount)
    if bonus <= 0:
        return None

    UserService.add_balance(referrer_id, bonus, source="referral")
    buyer_username = buyer.get("username") or UserService.get_username(buyer_int)
    return {
        "referrer_id": referrer_id,
        "buyer_id": buyer_int,
        "buyer_username": buyer_username,
        "purchase_amount": float(purchase_amount or 0),
        "percent": REFERRAL_PERCENT,
        "bonus": bonus,
        "balance": UserService.get_balance(referrer_id),
    }


def complete_product_purchase(buyer_id: int, purchase_amount, *, title: str | None = None, city: str | None = None) -> dict:
    buyer = UserService.get_user(buyer_id) or {}
    try:
        previous = int(buyer.get("purchases_count") or 0)
    except (TypeError, ValueError):
        previous = 0
    from app.services.loyalty import LoyaltyService

    loyalty = LoyaltyService.note_counted_purchase(buyer_id)
    UserService.record_paid_order(buyer_id, title=title, amount=purchase_amount, city=city)
    if previous == 0:
        from app.services.referral_race import ReferralRaceService

        ReferralRaceService.note_combat(buyer.get("referrer_id"), buyer_id)
    payload = apply_purchase_referral(buyer_id, purchase_amount) or {}
    payload["loyalty"] = loyalty or {}
    try:
        from app.services.bot_stats import BotStatsService

        BotStatsService.note_purchase(buyer_id, purchase_amount)
    except Exception:
        logger.exception("Failed to record purchase stats for %s", buyer_id)
    return payload


def build_referrer_bonus_text(payload: dict) -> str:
    username = (payload.get("buyer_username") or "").strip()
    who = f"@{username}" if username else f"ID {payload.get('buyer_id')}"
    percent = payload.get("percent") or REFERRAL_PERCENT
    percent_label = f"{percent:.0f}" if float(percent).is_integer() else str(percent)
    purchase = _format_rub(float(payload.get("purchase_amount") or 0))
    bonus = _format_rub(float(payload.get("bonus") or 0))
    balance = _format_rub(float(payload.get("balance") or 0))
    return (
        "🎉 <b>Бонус за реферала</b>\n\n"
        f"Ваш реферал {who} купил на <b>{purchase} руб.</b>\n"
        f"💎 Ваши {percent_label}%: <b>{bonus} руб.</b>\n\n"
        "✅ Начислено на баланс.\n"
        f"💰 Текущий баланс: <b>{balance} руб.</b>"
    )


async def notify_referrer_bonus(bot: Bot, payload: dict | None) -> None:
    if not payload:
        return
    referrer_id = payload.get("referrer_id")
    if not referrer_id:
        return
    try:
        await bot.send_message(int(referrer_id), build_referrer_bonus_text(payload))
    except Exception:
        logger.exception("Failed to notify referrer %s about purchase bonus", referrer_id)


async def notify_bonus_cap(bot: Bot, user_id) -> None:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    text = UserService.consume_bonus_cap_notice(uid)
    if not text:
        return
    try:
        await bot.send_message(uid, text)
    except Exception:
        logger.exception("Failed to notify user %s about bonus cap", uid)


async def notify_bonus_cap_many(bot: Bot, user_ids) -> None:
    seen = set()
    for raw in user_ids or []:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        await notify_bonus_cap(bot, uid)
