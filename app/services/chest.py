from __future__ import annotations

import json
import os
import random
import threading
from copy import deepcopy

from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService
from app.utils import get_now_msk

CHEST_DOC_KEY = "chest_config"
CHEST_FILE = "storage/chest_config.json"
FREE_CHANCE = 100
BOOST_STEP_RUB = 100
BOOST_STEP_PERCENT = 10
BOOST_MAX_PERCENT = 50
SPIN_MIN = 10
SPIN_MAX = 500
FREE_PRIZE = SPIN_MAX
BOOST_PRIZE_MIN = 500
BOOST_PRIZE_MAX = 1000
BOOST_PRIZE_STEP = 50
# Только в текстах для клиента. Реально 3000 никогда не начисляется.
JACKPOT_PRIZE = 3000

DEFAULT_CONFIG = {"enabled": True}

_lock = threading.RLock()


def boost_percent_for_rub(rub) -> int:
    try:
        amount = int(rub)
    except (TypeError, ValueError):
        return 0
    steps = amount // BOOST_STEP_RUB
    return max(0, min(BOOST_MAX_PERCENT, steps * BOOST_STEP_PERCENT))


def win_chance(boost_percent: int = 0) -> int:
    return max(0, min(BOOST_MAX_PERCENT, int(boost_percent or 0)))


def _boost_floor(paid_rub) -> int:
    try:
        paid = int(paid_rub or 0)
    except (TypeError, ValueError):
        paid = 0
    return max(FREE_PRIZE, paid)


def _real_boost_prize(dice, chance: int, floor: int) -> int:
    hit = dice.random() * 100 < chance
    if not hit:
        return floor
    rolled = dice.randrange(BOOST_PRIZE_MIN, BOOST_PRIZE_MAX + 1, BOOST_PRIZE_STEP)
    if rolled >= JACKPOT_PRIZE or rolled > BOOST_PRIZE_MAX:
        rolled = BOOST_PRIZE_MAX
    return max(floor, min(rolled, BOOST_PRIZE_MAX))


def spin_amount(dice) -> int:
    picker = getattr(dice, "randrange", None)
    if picker is None:
        rolled = SPIN_MIN + int(float(dice.random()) * (SPIN_MAX - SPIN_MIN + 1))
    else:
        rolled = int(picker(SPIN_MIN, SPIN_MAX + 1))
    return max(SPIN_MIN, min(SPIN_MAX, rolled))


def _today() -> str:
    return get_now_msk().strftime("%Y-%m-%d")


class ChestService:
    @classmethod
    def _load_config(cls) -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CHEST_DOC_KEY, DEFAULT_CONFIG)
            if not isinstance(data, dict):
                data = DEFAULT_CONFIG.copy()
            data["enabled"] = bool(data.get("enabled", True))
            return data
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        if not os.path.exists(CHEST_FILE):
            return DEFAULT_CONFIG.copy()
        try:
            with open(CHEST_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return DEFAULT_CONFIG.copy()
        if not isinstance(data, dict):
            return DEFAULT_CONFIG.copy()
        data["enabled"] = bool(data.get("enabled", True))
        return data

    @classmethod
    def _save_config(cls, data: dict) -> None:
        stored = {"enabled": bool(data.get("enabled", True))}
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CHEST_DOC_KEY, stored, force=True)
            return
        os.makedirs("storage", exist_ok=True)
        with open(CHEST_FILE, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, ensure_ascii=False, indent=2)

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(cls._load_config().get("enabled", True))

    @classmethod
    def set_enabled(cls, enabled: bool) -> dict:
        with _lock:
            data = cls._load_config()
            data["enabled"] = bool(enabled)
            cls._save_config(data)
            return deepcopy(data)

    @staticmethod
    def _sync_day(user: dict, today: str | None = None) -> None:
        day = today or _today()
        if str(user.get("chest_day") or "") == day:
            return
        user["chest_day"] = day
        user["chest_free_used"] = False
        user["chest_boost_percent"] = 0
        user["chest_boost_ready"] = False
        user["chest_boost_opened"] = False
        user["chest_boost_rub"] = 0

    @classmethod
    def snapshot(cls, user_id: int) -> dict:
        user = deepcopy(UserService.get_user(user_id) or {})
        cls._sync_day(user)
        boost_percent = int(user.get("chest_boost_percent") or 0)
        boost_ready = bool(user.get("chest_boost_ready"))
        return {
            "enabled": cls.is_enabled(),
            "day": user.get("chest_day"),
            "free_used": bool(user.get("chest_free_used")),
            "boost_percent": boost_percent if boost_ready else 0,
            "boost_ready": boost_ready,
            "boost_opened": bool(user.get("chest_boost_opened")),
            "boost_rub": int(user.get("chest_boost_rub") or 0) if boost_ready else 0,
            "free_chance": FREE_CHANCE,
            "boosted_chance": win_chance(boost_percent) if boost_ready else win_chance(0),
            "bonus_capped": UserService.bonus_room(user, user_id) <= 0,
        }

    @classmethod
    def grant_boost(cls, user_id: int, rub_amount) -> dict:
        percent = boost_percent_for_rub(rub_amount)
        if percent <= 0:
            raise ValueError("invalid boost")

        def mutate(user):
            cls._sync_day(user)
            user["chest_boost_percent"] = percent
            user["chest_boost_ready"] = True
            user["chest_boost_opened"] = False
            user["chest_boost_rub"] = int(rub_amount)
            return {
                "percent": percent,
                "rub": int(rub_amount),
                "chance": win_chance(percent),
            }

        result = UserService._update_user(user_id, mutate, create=True)
        return result or {"percent": percent, "rub": int(rub_amount), "chance": win_chance(percent)}

    @classmethod
    def can_open(cls, user_id: int) -> tuple[bool, str]:
        if not cls.is_enabled():
            return False, "disabled"
        snap = cls.snapshot(user_id)
        if not snap["free_used"]:
            return True, "free"
        return False, "already"

    @classmethod
    def open(cls, user_id: int, *, rng: random.Random | None = None) -> dict:
        dice = rng or random
        box: dict = {}

        def mutate(user):
            cls._sync_day(user)
            if user.get("chest_free_used"):
                box.update({"ok": False, "error": "already"})
                return
            if UserService.bonus_room(user, user_id) <= 0:
                UserService._queue_cap_notice_on(user)
                box.update({"ok": False, "error": "bonus_cap", "balance": UserService._as_balance(user.get("balance"))})
                return
            prize = spin_amount(dice)
            credited, _ = UserService._apply_balance_credit(
                user,
                float(prize),
                source="bonus",
                user_id=user_id,
            )
            user["chest_free_used"] = True
            box.update(
                {
                    "ok": True,
                    "win": credited > 0,
                    "jackpot": credited >= SPIN_MAX,
                    "prize": credited,
                    "sector": prize,
                    "boosted": False,
                    "chance": FREE_CHANCE,
                    "balance": UserService._as_balance(user.get("balance")),
                }
            )

        UserService._update_user(user_id, mutate, create=True)
        return box
