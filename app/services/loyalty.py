from __future__ import annotations

import logging

from aiogram import Bot

from app.services.settings import SettingsService
from app.services.users import UserService
from app.utils import get_now_msk

logger = logging.getLogger(__name__)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _purchases_word(count: int) -> str:
    n = abs(int(count)) % 100
    n1 = n % 10
    if 11 <= n <= 19:
        return "покупок"
    if n1 == 1:
        return "покупку"
    if 2 <= n1 <= 4:
        return "покупки"
    return "покупок"


def _format_rub(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if abs(number - round(number)) < 0.001:
        return f"{number:.0f}"
    return f"{number:.2f}"


def current_week_id(now=None) -> str:
    stamp = now or get_now_msk()
    iso = stamp.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class LoyaltyService:
    @staticmethod
    def apply_referral_progress(user: dict) -> None:
        if not isinstance(user, dict):
            return
        LoyaltyService._sync_mission_week(user)
        user["mission_referrals"] = max(0, _as_int(user.get("mission_referrals"))) + 1

    @staticmethod
    def note_referral(referrer_id) -> None:
        try:
            uid = int(referrer_id)
        except (TypeError, ValueError):
            return
        UserService._update_user(uid, LoyaltyService.apply_referral_progress)

    @staticmethod
    def note_counted_purchase(user_id: int) -> dict:
        streak_cfg = SettingsService.get_purchase_streak()
        missions_cfg = SettingsService.get_weekly_missions()

        def mutate(user):
            used_streak_coupon = False
            percent = UserService._discount_percent(user)
            left = UserService._discount_left(user)
            source = str(user.get("discount_source") or "")
            if percent > 0 and left > 0:
                if source == "streak":
                    used_streak_coupon = True
                left -= 1
                user["discount_purchases_left"] = left
                if left <= 0:
                    user["discount_percent"] = 0
                    user["discount_purchases_left"] = 0
                    user["discount_source"] = None

            user["purchases_count"] = max(0, _as_int(user.get("purchases_count"))) + 1
            user["cancel_count"] = 0

            if missions_cfg.get("enabled"):
                LoyaltyService._sync_mission_week(user)
                user["mission_purchases"] = max(0, _as_int(user.get("mission_purchases"))) + 1

            result = {
                "message": None,
                "streak": 0,
                "granted": False,
                "used_coupon": used_streak_coupon,
            }
            if not streak_cfg.get("enabled"):
                return result

            required = max(2, _as_int(streak_cfg.get("required"), 5))
            reward_percent = max(1, min(100, _as_int(streak_cfg.get("percent"), 100)))
            uses = max(1, _as_int(streak_cfg.get("uses"), 1))

            if used_streak_coupon:
                user["purchase_streak"] = 0
                return result

            streak = max(0, _as_int(user.get("purchase_streak"))) + 1
            if streak >= required:
                still_discounted = (
                    UserService._discount_percent(user) > 0
                    and UserService._discount_left(user) > 0
                )
                if still_discounted:
                    user["purchase_streak"] = required
                    result["streak"] = required
                    result["message"] = LoyaltyService._waiting_coupon_text(required, reward_percent)
                else:
                    user["discount_percent"] = reward_percent
                    user["discount_purchases_left"] = uses
                    user["discount_source"] = "streak"
                    user["purchase_streak"] = 0
                    result["streak"] = required
                    result["granted"] = True
                    result["message"] = LoyaltyService._granted_coupon_text(required, reward_percent)
            else:
                user["purchase_streak"] = streak
                result["streak"] = streak
                result["message"] = LoyaltyService._progress_text(streak, required, reward_percent)
            return result

        return UserService._update_user(user_id, mutate, create=True) or {}

    @staticmethod
    def _sync_mission_week(user: dict, now=None) -> str:
        week_id = current_week_id(now)
        if str(user.get("mission_week") or "") != week_id:
            user["mission_week"] = week_id
            user["mission_purchases"] = 0
            user["mission_referrals"] = 0
            user["mission_claimed"] = False
        return week_id

    @staticmethod
    def _progress_for(user: dict) -> dict:
        LoyaltyService._sync_mission_week(user)
        return {
            "week": str(user.get("mission_week") or current_week_id()),
            "purchases": max(0, _as_int(user.get("mission_purchases"))),
            "referrals": max(0, _as_int(user.get("mission_referrals"))),
            "claimed": bool(user.get("mission_claimed")),
        }

    @staticmethod
    def task_done(task: dict, progress: dict) -> bool:
        if not isinstance(task, dict) or not task.get("enabled", True):
            return False
        need = max(1, _as_int(task.get("count"), 1))
        kind = str(task.get("kind") or "purchases")
        if kind == "referrals":
            return max(0, _as_int(progress.get("referrals"))) >= need
        return max(0, _as_int(progress.get("purchases"))) >= need

    @staticmethod
    def claim_total(cfg: dict | None = None) -> int:
        data = cfg or SettingsService.get_weekly_missions()
        total = max(0, _as_int(data.get("reward")))
        for task in data.get("tasks") or []:
            if isinstance(task, dict) and task.get("enabled", True):
                total += max(0, _as_int(task.get("reward")))
        return total

    @staticmethod
    def get_view(user_id: int) -> dict:
        cfg = SettingsService.get_weekly_missions()
        user = UserService.get_user(user_id) or {}
        progress = LoyaltyService._progress_for(dict(user))
        tasks = [task for task in (cfg.get("tasks") or []) if isinstance(task, dict) and task.get("enabled", True)]
        rows = []
        all_done = True
        if not tasks:
            all_done = False
        for task in tasks:
            done = LoyaltyService.task_done(task, progress)
            if not done:
                all_done = False
            rows.append({
                "id": task.get("id"),
                "title": task.get("title") or "Задание",
                "done": done,
                "count": max(1, _as_int(task.get("count"), 1)),
                "kind": task.get("kind") or "purchases",
                "reward": max(0, _as_int(task.get("reward"))),
            })
        reward = LoyaltyService.claim_total(cfg)
        can_claim = bool(cfg.get("enabled") and all_done and not progress["claimed"] and reward > 0)
        return {
            "enabled": bool(cfg.get("enabled")),
            "tasks": rows,
            "reward": reward,
            "claimed": progress["claimed"],
            "can_claim": can_claim,
            "all_done": all_done,
            "week": progress["week"],
            "purchases": progress["purchases"],
            "referrals": progress["referrals"],
        }

    @staticmethod
    def build_missions_text(user_id: int) -> str:
        view = LoyaltyService.get_view(user_id)
        if not view["enabled"]:
            return "🏆 <b>Недельные цели Store</b>\n\nСейчас цели выключены."
        lines = ["🏆 <b>Недельные цели Store</b>", ""]
        if not view["tasks"]:
            lines.append("Куратор еще не добавил цели.")
            return "\n".join(lines)
        for task in view["tasks"]:
            mark = "✅" if task["done"] else "⬜"
            extra = f" · +{_format_rub(task['reward'])} ₽" if task["reward"] else ""
            lines.append(f"{mark} {task['title']}{extra}")
        lines.append("")
        lines.append(f"Приз: <b>+{_format_rub(view['reward'])} ₽</b> в кошелек")
        if view["claimed"]:
            lines.append("\n✅ Приз этой недели уже в кошельке.")
        elif view["all_done"]:
            lines.append("\n✅ Все цели закрыты. Нажмите кнопку и заберите приз.")
        else:
            lines.append("\nЗакройте все цели, чтобы забрать приз.")
        return "\n".join(lines)

    @staticmethod
    def claim_weekly_reward(user_id: int) -> dict:
        cfg = SettingsService.get_weekly_missions()
        if not cfg.get("enabled"):
            return {"ok": False, "error": "missions_off"}
        tasks = [task for task in (cfg.get("tasks") or []) if isinstance(task, dict) and task.get("enabled", True)]
        if not tasks:
            return {"ok": False, "error": "no_tasks"}
        amount = LoyaltyService.claim_total(cfg)
        if amount <= 0:
            return {"ok": False, "error": "no_reward"}

        result = {"ok": False, "error": "incomplete", "amount": 0, "balance": 0.0}

        def mutate(user):
            progress = LoyaltyService._progress_for(user)
            if progress["claimed"]:
                result["error"] = "claimed"
                result["balance"] = UserService._as_balance(user.get("balance"))
                return result
            if not all(LoyaltyService.task_done(task, progress) for task in tasks):
                result["error"] = "incomplete"
                return result
            credited, _ = UserService._apply_balance_credit(
                user,
                float(amount),
                source="bonus",
                user_id=user_id,
            )
            if credited <= 0:
                result["error"] = "bonus_cap"
                result["balance"] = UserService._as_balance(user.get("balance"))
                return result
            user["mission_claimed"] = True
            result["ok"] = True
            result["error"] = None
            result["amount"] = credited
            result["capped"] = credited + 1e-12 < float(amount)
            result["balance"] = UserService._as_balance(user.get("balance"))
            return result

        payload = UserService._update_user(user_id, mutate, create=True)
        return payload or result

    @staticmethod
    def _promo_header(required: int, percent: int) -> str:
        if percent >= 100:
            return f"🎁 <b>Акция {required}+1</b> · PlayStation™Store"
        return f"🎁 <b>Бонус Store</b> · PlayStation™Store"

    @staticmethod
    def _bonus_order_no(required: int) -> int:
        return required + 1

    @staticmethod
    def _progress_text(streak: int, required: int, percent: int) -> str:
        left = max(0, required - streak)
        bonus_no = LoyaltyService._bonus_order_no(required)
        head = LoyaltyService._promo_header(required, percent)
        if percent >= 100:
            if streak <= 1:
                body = (
                    "Каждый оплаченный заказ на витрине идёт в зачёт.\n"
                    f"Оформите ещё {left} {_purchases_word(left)} — "
                    f"и <b>{bonus_no}-я покупка</b> будет <b>бонусом бесплатно</b>."
                )
            else:
                body = (
                    f"В зачёте: <b>{streak}</b> из <b>{required}</b>.\n"
                    f"До бонусной {bonus_no}-й осталось <b>{left}</b> {_purchases_word(left)}."
                )
        else:
            if streak <= 1:
                body = (
                    "Считаем оплаченные заказы на витрине.\n"
                    f"Ещё {left} {_purchases_word(left)} — "
                    f"и на {bonus_no}-ю будет скидка <b>{percent}%</b>."
                )
            else:
                body = (
                    f"В зачёте: <b>{streak}</b> из <b>{required}</b>.\n"
                    f"До бонуса осталось <b>{left}</b> {_purchases_word(left)} "
                    f"(скидка <b>{percent}%</b> на {bonus_no}-ю)."
                )
        return f"{head}\n\n{body}"

    @staticmethod
    def _granted_coupon_text(required: int, percent: int) -> str:
        head = LoyaltyService._promo_header(required, percent)
        bonus_no = LoyaltyService._bonus_order_no(required)
        if percent >= 100:
            return (
                f"{head}\n\n"
                f"Готово — {required} {_purchases_word(required)} в зачёте.\n\n"
                f"🎉 Следующий заказ (<b>{bonus_no}-й</b>) — <b>бонусом бесплатно</b>.\n"
                "Применится сам при оформлении."
            )
        return (
            f"{head}\n\n"
            f"{required} {_purchases_word(required)} в зачёте.\n\n"
            f"🎁 На следующую покупку — скидка <b>{percent}%</b> (один раз).\n"
            "Применится сама при оформлении."
        )

    @staticmethod
    def _waiting_coupon_text(required: int, percent: int) -> str:
        head = LoyaltyService._promo_header(required, percent)
        bonus_no = LoyaltyService._bonus_order_no(required)
        if percent >= 100:
            return (
                f"{head}\n\n"
                f"Все {required} {_purchases_word(required)} уже в зачёте.\n"
                f"Бонусную <b>{bonus_no}-ю</b> включим, "
                "как только закончится текущая скидка Store."
            )
        return (
            f"{head}\n\n"
            f"Бонус <b>−{percent}%</b> на {bonus_no}-ю выдадим, "
            "когда закончится текущая скидка Store."
        )


async def notify_purchase_loyalty(bot: Bot, user_id: int, payload: dict | None = None, loyalty: dict | None = None) -> None:
    info = loyalty if loyalty is not None else (payload or {}).get("loyalty")
    text = (info or {}).get("message") if isinstance(info, dict) else None
    if not text:
        return
    try:
        await bot.send_message(int(user_id), text)
    except Exception:
        logger.exception("Failed to notify user %s about purchase streak", user_id)
