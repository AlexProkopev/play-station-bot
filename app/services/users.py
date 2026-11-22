import json
import logging
import os
import queue
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from app.utils import get_now_msk
from app.services.postgres_store import PostgresDocumentStore

USERS_FILE = "storage/users_db.json"
USERS_DOC_KEY = "users_db"
CANCEL_BAN_LIMIT = 30
CANCEL_CRITICAL_COUNT = 29
CANCEL_BAN_HOURS = 12
USER_CACHE_TTL_SECONDS = 1.5
BONUS_BALANCE_CAP = 1500.0
WELCOME_BONUS_AMOUNT = 50.0
BONUS_CAP_NOTICE = (
    "🔶 <b>Потолок бонусов Store</b>\n\n"
    "В кошельке уже <b>1500 ₽</b> бонусами. Промокоды, ежедневный бонус, "
    "розыгрыши и недельные цели больше не зачислятся.\n\n"
    "Снять потолок можно так: пригласите друга по реферальной ссылке, "
    "и он должен оплатить заказ. После этого лимит снимется."
)

logger = logging.getLogger(__name__)


class _UserDbWriter:
    def __init__(self):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="user-db-writer")
        self._started = False
        self._start_lock = threading.Lock()

    def ensure_started(self) -> None:
        with self._start_lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def upsert(self, user_id: str, payload: dict) -> None:
        self.ensure_started()
        self._queue.put(("upsert", str(user_id), deepcopy(payload)))

    def delete(self, user_ids: list[str]) -> None:
        if not user_ids:
            return
        self.ensure_started()
        self._queue.put(("delete", [str(uid) for uid in user_ids]))

    def replace_all(self, users: dict) -> None:
        self.ensure_started()
        done = threading.Event()
        self._queue.put(("replace", deepcopy(users), done))
        if not done.wait(60):
            logger.warning("user db replace_all timed out")

    def flush(self, timeout: float = 15) -> None:
        if not self._started:
            return
        done = threading.Event()
        self._queue.put(("flush", done))
        done.wait(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            kind = item[0]
            try:
                if kind == "upsert":
                    self._upsert_with_retry(item[1], item[2])
                elif kind == "delete":
                    PostgresDocumentStore.delete_users(item[1])
                elif kind == "replace":
                    PostgresDocumentStore.replace_all_users(item[1])
                    item[2].set()
                elif kind == "flush":
                    item[1].set()
            except Exception:
                logger.exception("user db writer failed: %s", kind)
                if kind in {"replace", "flush"}:
                    try:
                        item[-1].set()
                    except Exception:
                        pass

    @staticmethod
    def _upsert_with_retry(user_id: str, payload: dict, attempts: int = 3) -> None:
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                PostgresDocumentStore.upsert_user(user_id, payload)
                return
            except Exception as exc:
                last_error = exc
                logger.exception("user upsert failed (attempt %s/%s)", attempt, attempts)
                if attempt < attempts:
                    threading.Event().wait(0.25 * attempt)
        if last_error:
            raise last_error


_user_db_writer = _UserDbWriter()


class UserService:
    _users_cache: dict | None = None
    _cache_key = None
    _lock = threading.RLock()
    _user_fresh_at: dict[str, float] = {}

    @classmethod
    def reset_runtime_cache(cls) -> None:
        with cls._lock:
            cls._users_cache = None
            cls._cache_key = None
            cls._user_fresh_at = {}

    @classmethod
    def warmup(cls) -> int:
        data = cls._load_db()
        users = data.get("users") or {}
        now = time.monotonic()
        with cls._lock:
            cls._user_fresh_at = {str(uid): now for uid in users}
        logger.info("Users loaded into memory: %s", len(users))
        return len(users)

    @classmethod
    def flush_writes(cls, timeout: float = 15) -> None:
        _user_db_writer.flush(timeout)

    @staticmethod
    def _backend_key():
        if PostgresDocumentStore.is_enabled():
            return ("postgres",)
        return ("file", USERS_FILE)

    @staticmethod
    def _read_users_from_backend() -> dict:
        if PostgresDocumentStore.is_enabled():
            users = PostgresDocumentStore.load_all_users()
            return users if isinstance(users, dict) else {}
        if not os.path.exists(USERS_FILE):
            return {}
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        users = data.get("users") if isinstance(data, dict) else {}
        return users if isinstance(users, dict) else {}

    @staticmethod
    def _write_users_file(users: dict) -> None:
        directory = os.path.dirname(USERS_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump({"users": users}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving users db: {e}")

    @staticmethod
    def _build_default_user(username: str | None = None) -> dict:
        return {
            "active": True,
            "username": username,
            "joined": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
            "purchases_count": 0,
            "coupons_activated_count": 0,
            "last_coupon_at": None,
            "discount_percent": 0,
            "discount_purchases_left": 0,
            "balance": 0.0,
            "referral_count": 0,
            "paid_referral_count": 0,
            "topup_count": 0,
            "bonus_cap_notified": False,
            "bonus_cap_notice_pending": False,
            "referrer_id": None,
            "manual_payment_block_until": None,
            "rf_card_block_until": None,
            "rf_card_failures": [],
            "rf_card_cancel_streak": 0,
            "last_rf_card": None,
            "last_yoomoney_wallet": None,
            "yoomoney_block_until": None,
            "yoomoney_cancel_streak": 0,
            "last_bot_id": None,
            "spin_games": 0,
            "spin_winnings": 0.0,
            "welcome_bonus_granted": False,
        }

    @staticmethod
    def _load_db():
        with UserService._lock:
            key = UserService._backend_key()
            if UserService._users_cache is None or UserService._cache_key != key:
                UserService._users_cache = UserService._read_users_from_backend()
                UserService._cache_key = key
            return {"users": UserService._users_cache}

    @staticmethod
    def _save_db(data, changed_ids=None, replace_all=False, deleted_ids=None):
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, dict):
            users = {}
            if isinstance(data, dict):
                data["users"] = users

        with UserService._lock:
            UserService._users_cache = users
            UserService._cache_key = UserService._backend_key()

            if not PostgresDocumentStore.is_enabled():
                UserService._write_users_file(users)
                return

            snapshot = users
            do_replace = replace_all
            to_delete = [str(uid) for uid in deleted_ids] if deleted_ids else []
            to_upsert = list(users.keys()) if changed_ids is None else list(changed_ids)

        if do_replace:
            _user_db_writer.replace_all(snapshot)
            return

        if to_delete:
            PostgresDocumentStore.delete_users(to_delete)

        for uid in to_upsert:
            str_id = str(uid)
            payload = snapshot.get(str_id)
            if isinstance(payload, dict):
                UserService._persist_user_now(str_id, payload)

    @staticmethod
    def _sync_user_on_start(user: dict, username: str | None) -> None:
        if username and user.get("username") != username:
            user["username"] = username
        if not user.get("active", True):
            user["active"] = True
        if "welcome_bonus_granted" not in user:
            user["welcome_bonus_granted"] = True
        defaults = UserService._build_default_user(username)
        for key, value in defaults.items():
            if key in {"joined", "welcome_bonus_granted"}:
                continue
            if key not in user:
                user[key] = value

    @staticmethod
    def add_user(user_id: int, username: str, referrer_id: str = None):
        str_id = str(user_id)
        ref = str(referrer_id) if referrer_id and str(referrer_id) != str_id else None

        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.mutate_user(
                str_id,
                lambda user: UserService._sync_user_on_start(user, username),
            )
            if payload is None:
                new_user = UserService._build_default_user(username)
                new_user["referrer_id"] = ref
                payload = PostgresDocumentStore.mutate_user(
                    str_id,
                    lambda user: None,
                    create_payload=new_user,
                )
                if ref:
                    def bump_referral(user):
                        user["referral_count"] = int(user.get("referral_count") or 0) + 1
                        from app.services.loyalty import LoyaltyService
                        LoyaltyService.apply_referral_progress(user)
                    ref_payload = PostgresDocumentStore.mutate_user(ref, bump_referral)
                    if isinstance(ref_payload, dict):
                        UserService._put_user_cache(ref, ref_payload)
                    try:
                        from app.services.bot_stats import BotStatsService
                        BotStatsService.note_referral(int(ref))
                    except (TypeError, ValueError):
                        pass
                if isinstance(payload, dict):
                    UserService._put_user_cache(str_id, payload)
                return True
            if isinstance(payload, dict):
                UserService._put_user_cache(str_id, payload)
            return False

        UserService._merge_fresh_user(user_id, force=True)
        if ref:
            UserService._merge_fresh_user(ref, force=True)
        data = UserService._load_db()
        
        # Если юзера нет или он был неактивен, обновляем
        if str_id not in data["users"]:
            new_user = UserService._build_default_user(username)
            new_user["referrer_id"] = ref
            
            # Если есть реферер, начисляем ему приглашение (простая логика)
            if ref and ref in data["users"]:
                 ref_user = data["users"][ref]
                 ref_user["referral_count"] = ref_user.get("referral_count", 0) + 1
                 from app.services.loyalty import LoyaltyService
                 LoyaltyService.apply_referral_progress(ref_user)
                 try:
                     from app.services.bot_stats import BotStatsService
                     BotStatsService.note_referral(int(ref))
                 except (TypeError, ValueError):
                     pass
            
            data["users"][str_id] = new_user
            changed = [str_id]
            if ref and ref in data["users"]:
                changed.append(ref)
            UserService._save_db(data, changed_ids=changed)
            return True # Новый юзер
        else:
            user = data["users"][str_id]
            before = deepcopy(user)
            UserService._sync_user_on_start(user, username)
            if user != before:
                UserService._save_db(data, changed_ids=[str_id])

    @classmethod
    def _mark_user_fresh(cls, user_id: str) -> None:
        cls._user_fresh_at[str(user_id)] = time.monotonic()

    @classmethod
    def _is_user_fresh(cls, user_id: str) -> bool:
        ts = cls._user_fresh_at.get(str(user_id))
        if ts is None:
            return False
        return (time.monotonic() - ts) < USER_CACHE_TTL_SECONDS

    @classmethod
    def _put_user_cache(cls, user_id: str, payload: dict) -> dict:
        str_id = str(user_id)
        data = cls._load_db()
        data["users"][str_id] = payload
        with cls._lock:
            if cls._users_cache is None:
                cls._users_cache = data["users"]
            else:
                cls._users_cache[str_id] = payload
            cls._mark_user_fresh(str_id)
        return payload

    @classmethod
    def _merge_fresh_user(cls, user_id, *, force: bool = False) -> dict | None:
        str_id = str(user_id)
        if PostgresDocumentStore.is_enabled():
            if not force and cls._is_user_fresh(str_id):
                data = cls._load_db()
                user = data["users"].get(str_id)
                if isinstance(user, dict):
                    return user
            payload = PostgresDocumentStore.get_user(str_id)
            if isinstance(payload, dict):
                return cls._put_user_cache(str_id, payload)
        data = cls._load_db()
        user = data["users"].get(str_id)
        return user if isinstance(user, dict) else None

    @classmethod
    def _update_user(cls, user_id, mutator, *, create: bool = False):
        str_id = str(user_id)
        extra = {"value": None}

        def wrapped(payload):
            extra["value"] = mutator(payload)

        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.mutate_user(
                str_id,
                wrapped,
                create_payload=cls._build_default_user() if create else None,
            )
            if isinstance(payload, dict):
                cls._put_user_cache(str_id, payload)
                return extra["value"]
            return None

        user = cls._merge_fresh_user(user_id, force=True)
        if not isinstance(user, dict):
            if not create:
                return None
            user = cls._build_default_user()
            data = cls._load_db()
            data["users"][str_id] = user
        extra["value"] = mutator(user)
        data = cls._load_db()
        data["users"][str_id] = user
        cls._save_db(data, changed_ids=[str_id])
        cls._mark_user_fresh(str_id)
        return extra["value"]

    @staticmethod
    def _as_balance(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _count_paid_referrals(cls, user_id) -> int:
        try:
            sid = str(int(user_id))
        except (TypeError, ValueError):
            return 0
        data = cls._load_db()
        users = data.get("users") if isinstance(data.get("users"), dict) else {}
        counted = 0
        for other in users.values():
            if not isinstance(other, dict):
                continue
            if str(other.get("referrer_id") or "") != sid:
                continue
            if cls._as_int(other.get("purchases_count")) > 0:
                counted += 1
        return counted

    @classmethod
    def _refresh_paid_referral_count(cls, user: dict, user_id=None) -> int:
        stored = max(0, cls._as_int(user.get("paid_referral_count")))
        if stored > 0:
            return stored
        counted = cls._count_paid_referrals(user_id) if user_id is not None else 0
        if counted > stored:
            user["paid_referral_count"] = counted
        return max(stored, counted)

    @classmethod
    def is_bonus_unlocked(cls, user: dict | None, user_id=None) -> bool:
        if not isinstance(user, dict):
            return False
        if cls._as_int(user.get("purchases_count")) > 0:
            return True
        return cls._refresh_paid_referral_count(user, user_id) > 0

    @classmethod
    def bonus_room(cls, user: dict | None, user_id=None) -> float:
        if cls.is_bonus_unlocked(user, user_id):
            return float("inf")
        current = cls._as_balance((user or {}).get("balance"))
        return max(0.0, BONUS_BALANCE_CAP - current)

    @classmethod
    def bonus_room_for_user(cls, user_id: int) -> float:
        return cls.bonus_room(cls.get_user(user_id) or {}, user_id)

    @classmethod
    def is_at_bonus_cap(cls, user: dict | None) -> bool:
        return cls._as_balance((user or {}).get("balance")) + 1e-9 >= BONUS_BALANCE_CAP

    @classmethod
    def _should_queue_cap_notice(cls, user: dict) -> bool:
        if user.get("bonus_cap_notified"):
            return False
        if cls._as_int(user.get("purchases_count")) > 0:
            return False
        if cls._as_int(user.get("paid_referral_count")) > 0:
            return False
        if not cls.is_at_bonus_cap(user):
            return False
        return True

    @classmethod
    def _queue_cap_notice_on(cls, user: dict) -> bool:
        if not cls._should_queue_cap_notice(user):
            return False
        user["bonus_cap_notified"] = True
        user["bonus_cap_notice_pending"] = True
        return True

    @classmethod
    def _apply_balance_credit(cls, user: dict, amount, *, source: str = "bonus", user_id=None) -> tuple[float, bool]:
        change = float(amount or 0)
        source = str(source or "bonus").strip().lower() or "bonus"
        if source == "referral":
            user["bonus_cap_notified"] = False
            user["bonus_cap_notice_pending"] = False
        elif source == "topup":
            user["topup_count"] = max(0, cls._as_int(user.get("topup_count"))) + 1
            user["has_topped_up"] = True

        current = cls._as_balance(user.get("balance"))
        credited = change
        if source == "bonus" and change > 0:
            if not cls.is_bonus_unlocked(user, user_id):
                room = max(0.0, BONUS_BALANCE_CAP - current)
                credited = min(change, room)

        if abs(credited) > 1e-12:
            user["balance"] = current + credited
            if credited > 0:
                cls._append_wallet_event(user, source, credited)
        notify = source == "bonus" and cls._queue_cap_notice_on(user)
        return credited, notify

    @staticmethod
    def _wallet_label(source: str) -> str:
        labels = {
            "topup": "Пополнение",
            "bonus": "Бонус",
            "referral": "Бонус с друга",
        }
        return labels.get(str(source or "").strip().lower(), "Зачисление")

    @classmethod
    def _append_wallet_event(cls, user: dict, source: str, amount: float) -> None:
        rows = user.get("wallet_events")
        if not isinstance(rows, list):
            rows = []
        rows.insert(0, {
            "source": str(source or "bonus"),
            "label": cls._wallet_label(source),
            "amount": round(float(amount), 2),
            "at": get_now_msk().strftime("%d.%m %H:%M"),
        })
        user["wallet_events"] = rows[:20]

    @classmethod
    def record_paid_order(cls, user_id: int, *, title: str | None, amount, city: str | None = None) -> None:
        try:
            price = float(amount or 0)
        except (TypeError, ValueError):
            price = 0.0
        name = " ".join(str(title or "Заказ").split()) or "Заказ"
        place = " ".join(str(city or "").split())

        def mutate(user):
            rows = user.get("paid_orders")
            if not isinstance(rows, list):
                rows = []
            rows.insert(0, {
                "title": name[:80],
                "city": place[:40],
                "amount": round(price, 2),
                "at": get_now_msk().strftime("%d.%m %H:%M"),
            })
            user["paid_orders"] = rows[:20]

        cls._update_user(user_id, mutate, create=True)

    @classmethod
    def credit_balance(cls, user_id: int, amount: float, *, source: str = "bonus") -> dict:
        cls._ensure_user_record(user_id)
        change = float(amount or 0)
        result = {
            "ok": False,
            "credited": 0.0,
            "requested": change,
            "capped": False,
            "balance": 0.0,
            "notify": False,
        }

        def mutate(user):
            credited, notify = cls._apply_balance_credit(
                user,
                change,
                source=source,
                user_id=user_id,
            )
            result["ok"] = True
            result["credited"] = credited
            result["notify"] = notify
            result["capped"] = str(source or "bonus").strip().lower() == "bonus" and change > 0 and credited + 1e-12 < change
            result["balance"] = cls._as_balance(user.get("balance"))
            return result

        payload = cls._update_user(user_id, mutate, create=True)
        return payload if isinstance(payload, dict) else result

    @classmethod
    def grant_welcome_bonus(cls, user_id: int) -> dict:
        """One-time +50 RUB for brand-new accounts (after captcha)."""
        result = {
            "granted": False,
            "credited": 0.0,
            "balance": 0.0,
        }

        def mutate(user):
            if user.get("welcome_bonus_granted"):
                result["balance"] = cls._as_balance(user.get("balance"))
                return result
            credited, _notify = cls._apply_balance_credit(
                user,
                WELCOME_BONUS_AMOUNT,
                source="bonus",
                user_id=user_id,
            )
            user["welcome_bonus_granted"] = True
            result["granted"] = credited > 0
            result["credited"] = credited
            result["balance"] = cls._as_balance(user.get("balance"))
            return result

        payload = cls._update_user(user_id, mutate, create=False)
        return payload if isinstance(payload, dict) and "granted" in payload else result

    @classmethod
    def consume_bonus_cap_notice(cls, user_id: int) -> str | None:
        text = {"value": None}

        def mutate(user):
            if not user.get("bonus_cap_notice_pending"):
                return
            user["bonus_cap_notice_pending"] = False
            text["value"] = BONUS_CAP_NOTICE

        cls._update_user(user_id, mutate)
        return text["value"]

    @classmethod
    def _ensure_user_record(cls, user_id: int, username: str | None = None) -> dict:
        user = cls._merge_fresh_user(user_id, force=True)
        if isinstance(user, dict):
            return user
        cls.add_user(user_id, username)
        user = cls._merge_fresh_user(user_id)
        if isinstance(user, dict):
            return user
        str_id = str(user_id)
        user = cls._build_default_user(username)
        data = cls._load_db()
        data["users"][str_id] = user
        cls._save_db(data, changed_ids=[str_id])
        return user

    @classmethod
    def _persist_user_now(cls, user_id: str, payload: dict) -> None:
        if not PostgresDocumentStore.is_enabled() or not isinstance(payload, dict):
            return
        try:
            _UserDbWriter._upsert_with_retry(str(user_id), deepcopy(payload))
        except Exception:
            logger.exception("immediate user persist failed for %s", user_id)
            _user_db_writer.upsert(str(user_id), payload)

    @staticmethod
    def get_balance(user_id: int) -> float:
        user = UserService._merge_fresh_user(user_id, force=True)
        return UserService._as_balance((user or {}).get("balance"))

    @staticmethod
    def set_balance(user_id: int, amount: float):
        UserService._ensure_user_record(user_id)
        str_id = str(user_id)
        amount_value = UserService._as_balance(amount)
        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.set_user_balance(str_id, amount_value)
            if isinstance(payload, dict):
                UserService._put_user_cache(str_id, payload)
                return True
        data = UserService._load_db()
        data["users"][str_id]["balance"] = amount_value
        UserService._save_db(data, changed_ids=[str_id])
        UserService._persist_user_now(str_id, data["users"][str_id])
        return True

    @staticmethod
    def add_balance(user_id: int, amount: float, *, source: str = "bonus"):
        result = UserService.credit_balance(user_id, amount, source=source)
        if not result.get("ok"):
            return False
        source_name = str(source or "bonus").strip().lower() or "bonus"
        if source_name == "bonus" and float(amount or 0) > 0 and float(result.get("credited") or 0) <= 0:
            return False
        return True

    @staticmethod
    def deduct_balance(user_id: int, amount: float):
        UserService._merge_fresh_user(user_id)
        str_id = str(user_id)
        change = float(amount)
        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.add_user_balance(
                str_id,
                -change,
                require_non_negative=True,
            )
            if isinstance(payload, dict):
                UserService._put_user_cache(str_id, payload)
                return True
            return False
        data = UserService._load_db()
        if str_id in data["users"]:
            current = UserService._as_balance(data["users"][str_id].get("balance"))
            if current >= change:
                data["users"][str_id]["balance"] = current - change
                UserService._save_db(data, changed_ids=[str_id])
                UserService._persist_user_now(str_id, data["users"][str_id])
                return True
        return False

    @staticmethod
    def add_spin_play(user_id: int) -> None:
        def mutate(user):
            user["spin_games"] = int(user.get("spin_games") or 0) + 1

        UserService._update_user(user_id, mutate)

    @staticmethod
    def add_spin_winnings(user_id: int, amount: float) -> None:
        change = float(amount or 0)
        if change <= 0:
            return

        def mutate(user):
            current = UserService._as_balance(user.get("spin_winnings"))
            user["spin_winnings"] = current + change

        UserService._update_user(user_id, mutate)

    @staticmethod
    def get_spin_stats(user_id: int) -> tuple[int, float]:
        user = UserService.get_user(user_id) or {}
        try:
            played = max(0, int(user.get("spin_games") or 0))
        except (TypeError, ValueError):
            played = 0
        won = UserService._as_balance(user.get("spin_winnings"))
        return played, won

    @staticmethod
    def get_user(user_id: int):
        return UserService._merge_fresh_user(user_id, force=True)

    @classmethod
    def note_paying_referral(cls, referrer_id) -> None:
        try:
            uid = int(referrer_id)
        except (TypeError, ValueError):
            return

        def mutate(user):
            user["paid_referral_count"] = max(0, cls._as_int(user.get("paid_referral_count"))) + 1
            user["paid_referral_synced"] = True
            user["bonus_cap_notified"] = False
            user["bonus_cap_notice_pending"] = False

        cls._update_user(uid, mutate)

    @staticmethod
    def increment_purchase(user_id: int):
        def mutate(user):
            user["purchases_count"] = int(user.get("purchases_count") or 0) + 1
            user["cancel_count"] = 0
            percent = UserService._discount_percent(user)
            left = UserService._discount_left(user)
            if percent > 0 and left > 0:
                left -= 1
                user["discount_purchases_left"] = left
                if left <= 0:
                    user["discount_percent"] = 0
                    user["discount_purchases_left"] = 0
                    user["discount_source"] = None
        UserService._update_user(user_id, mutate)

    @staticmethod
    def _discount_percent(user: dict | None) -> int:
        if not isinstance(user, dict):
            return 0
        try:
            return max(0, min(100, int(user.get("discount_percent") or 0)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _discount_left(user: dict | None) -> int:
        if not isinstance(user, dict):
            return 0
        try:
            return max(0, int(user.get("discount_purchases_left") or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def get_personal_discount(cls, user_id: int) -> dict:
        user = cls.get_user(user_id) or {}
        percent = cls._discount_percent(user)
        left = cls._discount_left(user)
        if percent <= 0 or left <= 0:
            return {"percent": 0, "left": 0, "active": False}
        return {"percent": percent, "left": left, "active": True}

    @classmethod
    def set_personal_discount(cls, user_id: int, percent: int, purchases: int) -> dict:
        percent = max(0, min(100, int(percent)))
        purchases = max(0, int(purchases))
        if percent <= 0 or purchases <= 0:
            return cls.clear_personal_discount(user_id)

        def mutate(user):
            user["discount_percent"] = percent
            user["discount_purchases_left"] = purchases
            user["discount_source"] = "admin"
            return {"percent": percent, "left": purchases, "active": True}

        result = cls._update_user(user_id, mutate, create=True)
        return result or {"percent": percent, "left": purchases, "active": True}

    @classmethod
    def clear_personal_discount(cls, user_id: int) -> dict:
        def mutate(user):
            user["discount_percent"] = 0
            user["discount_purchases_left"] = 0
            user["discount_source"] = None
            return {"percent": 0, "left": 0, "active": False}

        result = cls._update_user(user_id, mutate, create=True)
        return result or {"percent": 0, "left": 0, "active": False}

    @classmethod
    def price_with_discount(cls, user_id: int, price) -> float:
        try:
            amount = float(price or 0)
        except (TypeError, ValueError):
            amount = 0.0
        info = cls.get_personal_discount(user_id)
        if not info.get("active"):
            return amount
        discounted = round(amount * (100 - int(info["percent"])) / 100.0)
        return max(0.0, float(discounted))

    @staticmethod
    def reserve_coupon_activation(
        user_id: int,
        redeemed_from_db: int = 0,
        last_known_at: str | datetime | None = None,
    ) -> tuple[bool, dict]:
        def _parse_dt(value):
            if isinstance(value, datetime):
                return value.replace(tzinfo=None) if value.tzinfo else value
            if not value:
                return None
            try:
                return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return None

        def mutate(user):
            try:
                purchases = max(0, int(user.get("purchases_count") or 0))
            except (TypeError, ValueError):
                purchases = 0
            try:
                stored = max(0, int(user.get("coupons_activated_count") or 0))
            except (TypeError, ValueError):
                stored = 0
            try:
                from_db = max(0, int(redeemed_from_db or 0))
            except (TypeError, ValueError):
                from_db = 0
            used = max(stored, from_db)
            now = get_now_msk()
            last_at = _parse_dt(user.get("last_coupon_at")) or _parse_dt(last_known_at)

            if purchases <= 0:
                next_at = (last_at + timedelta(days=7)) if last_at else None
                if next_at is not None and now < next_at:
                    return {
                        "ok": False,
                        "purchases": purchases,
                        "used": used,
                        "unlimited": False,
                        "can_redeem": False,
                        "last_coupon_at": last_at.strftime("%Y-%m-%d %H:%M:%S") if last_at else None,
                        "next_at": next_at.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                user["coupons_activated_count"] = used + 1
                stamp = now.strftime("%Y-%m-%d %H:%M:%S")
                user["last_coupon_at"] = stamp
                next_stamp = (now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                return {
                    "ok": True,
                    "purchases": purchases,
                    "used": used + 1,
                    "unlimited": False,
                    "can_redeem": True,
                    "last_coupon_at": stamp,
                    "next_at": next_stamp,
                }

            user["coupons_activated_count"] = used + 1
            stamp = now.strftime("%Y-%m-%d %H:%M:%S")
            user["last_coupon_at"] = stamp
            return {
                "ok": True,
                "purchases": purchases,
                "used": used + 1,
                "unlimited": True,
                "can_redeem": True,
                "last_coupon_at": stamp,
                "next_at": None,
            }

        result = UserService._update_user(user_id, mutate, create=True)
        if not isinstance(result, dict):
            return False, {"purchases": 0, "used": 0, "unlimited": False, "can_redeem": False}
        return bool(result.get("ok")), result

    @staticmethod
    def release_coupon_activation(user_id: int) -> None:
        def mutate(user):
            try:
                current = max(0, int(user.get("coupons_activated_count") or 0))
            except (TypeError, ValueError):
                current = 0
            user["coupons_activated_count"] = max(0, current - 1)

        UserService._update_user(user_id, mutate)

    @staticmethod
    def reset_cancel_count(user_id: int):
        UserService._update_user(user_id, lambda user: user.__setitem__("cancel_count", 0))

    @staticmethod
    def set_silent_review_mode(user_id: int, mode: bool):
        UserService._update_user(user_id, lambda user: user.__setitem__("silent_review_mode", mode))
            
    @staticmethod
    def get_silent_review_mode(user_id: int) -> bool:
        user = UserService._merge_fresh_user(user_id)
        return bool(user.get("silent_review_mode", False)) if isinstance(user, dict) else False

    @staticmethod
    def set_active_order(user_id: int, order_data: dict):
        def mutate(user):
            user["active_order"] = order_data
        UserService._update_user(user_id, mutate, create=True)

    @staticmethod
    def get_active_order(user_id: int):
        user = UserService._merge_fresh_user(user_id, force=True)
        if isinstance(user, dict):
            return user.get("active_order")
        return None

    @staticmethod
    def get_all_active_orders():
        if PostgresDocumentStore.is_enabled():
            users = PostgresDocumentStore.load_users_with_active_orders()
            orders = []
            for uid, udata in users.items():
                order = udata.get("active_order")
                if order:
                    UserService._put_user_cache(uid, udata)
                    orders.append({
                        "user_id": uid,
                        "username": udata.get("username"),
                        "order": order,
                    })
            return orders
        data = UserService._load_db()
        orders = []
        for uid, udata in data.get("users", {}).items():
            if "active_order" in udata and udata["active_order"]:
                orders.append({
                    "user_id": uid,
                    "username": udata.get("username"),
                    "order": udata["active_order"]
                })
        return orders

    @staticmethod
    def remove_active_order(user_id: int):
        def mutate(user):
            user.pop("active_order", None)
        UserService._update_user(user_id, mutate)

    @staticmethod
    def set_late_ratecore_order(user_id: int, order_data: dict):
        def mutate(user):
            user["late_ratecore_order"] = order_data
        UserService._update_user(user_id, mutate, create=True)

    @staticmethod
    def get_late_ratecore_order(user_id: int):
        user = UserService._merge_fresh_user(user_id, force=True)
        if not isinstance(user, dict):
            return None
        order = user.get("late_ratecore_order")
        return order if isinstance(order, dict) else None

    @staticmethod
    def clear_late_ratecore_order(user_id: int):
        def mutate(user):
            user.pop("late_ratecore_order", None)
        UserService._update_user(user_id, mutate)

    @staticmethod
    def get_all_late_ratecore_orders():
        if PostgresDocumentStore.is_enabled():
            users = PostgresDocumentStore.load_users_with_late_ratecore_orders()
            orders = []
            for uid, udata in users.items():
                order = udata.get("late_ratecore_order")
                if isinstance(order, dict):
                    UserService._put_user_cache(uid, udata)
                    orders.append({
                        "user_id": uid,
                        "username": udata.get("username"),
                        "order": order,
                    })
            return orders
        data = UserService._load_db()
        orders = []
        for uid, udata in data.get("users", {}).items():
            order = udata.get("late_ratecore_order") if isinstance(udata, dict) else None
            if isinstance(order, dict):
                orders.append({
                    "user_id": uid,
                    "username": udata.get("username"),
                    "order": order,
                })
        return orders

    @staticmethod
    def increment_cancel_count(user_id: int):
        def mutate(user):
            curr = int(user.get("cancel_count") or 0) + 1
            user["cancel_count"] = curr
            return curr

        result = UserService._update_user(user_id, mutate)
        return int(result or 0)

    @staticmethod
    def set_ban_until(user_id: int, until_dt: str):
        def mutate(user):
            user["banned_until"] = until_dt
            user["cancel_count"] = 0
        UserService._update_user(user_id, mutate)

    @staticmethod
    def is_temp_banned(user_id: int):
        user = UserService._merge_fresh_user(user_id, force=True)
        if not isinstance(user, dict):
            return False, None
        ban_until = user.get("banned_until")
        if ban_until:
            try:
                dt = datetime.strptime(ban_until, "%Y-%m-%d %H:%M:%S")
                if dt > get_now_msk():
                    return True, ban_until
                UserService._update_user(user_id, lambda item: item.pop("banned_until", None))
            except Exception:
                pass
        return False, None

    @staticmethod
    def set_active(user_id: int, is_active: bool):
        stamp = get_now_msk().strftime("%Y-%m-%d %H:%M:%S")

        def mutate(user):
            user["active"] = bool(is_active)
            if is_active:
                user["unblocked_at"] = stamp
            else:
                user["blocked_at"] = stamp

        UserService._update_user(user_id, mutate)

    @staticmethod
    def unban_user(user_id: int):
        box = {"changed": False}

        def mutate(user):
            if "banned_until" in user:
                del user["banned_until"]
                box["changed"] = True
            if "cancel_count" in user:
                user["cancel_count"] = 0
                box["changed"] = True

        UserService._update_user(user_id, mutate)
        return box["changed"]

    @staticmethod
    def set_manual_payment_block_until(user_id: int, until_dt: str):
        UserService._update_user(user_id, lambda user: user.__setitem__("manual_payment_block_until", until_dt))

    @staticmethod
    def is_manual_payment_blocked(user_id: int):
        user = UserService._merge_fresh_user(user_id)
        if not isinstance(user, dict):
            return False, None
        block_until = user.get("manual_payment_block_until")
        if block_until:
            try:
                dt = datetime.strptime(block_until, "%Y-%m-%d %H:%M:%S")
                if dt > get_now_msk():
                    return True, block_until
                UserService._update_user(user_id, lambda item: item.__setitem__("manual_payment_block_until", None))
            except Exception:
                pass
        return False, None

    @staticmethod
    def unblock_manual_payment(user_id: int):
        box = {"changed": False}

        def mutate(user):
            if user.get("manual_payment_block_until"):
                user["manual_payment_block_until"] = None
                box["changed"] = True

        UserService._update_user(user_id, mutate)
        return box["changed"]

    @staticmethod
    def set_rf_card_block_until(user_id: int, until_dt: str):
        def mutate(user):
            user["rf_card_block_until"] = until_dt
            user["rf_card_cancel_streak"] = 0
        UserService._update_user(user_id, mutate)

    @staticmethod
    def is_rf_card_blocked(user_id: int):
        user = UserService._merge_fresh_user(user_id)
        if not isinstance(user, dict):
            return False, None
        block_until = user.get("rf_card_block_until")
        if block_until:
            try:
                dt = datetime.strptime(block_until, "%Y-%m-%d %H:%M:%S")
                if dt > get_now_msk():
                    return True, block_until
                UserService._update_user(user_id, lambda item: item.__setitem__("rf_card_block_until", None))
            except Exception:
                pass
        return False, None

    @staticmethod
    def record_rf_card_failure(user_id: int) -> int:
        box = {"count": 0}

        def mutate(user):
            now = get_now_msk()
            cutoff = now - timedelta(hours=24)
            cleaned = []
            for item in user.get("rf_card_failures") or []:
                try:
                    dt = datetime.strptime(item, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if dt >= cutoff:
                    cleaned.append(dt.strftime("%Y-%m-%d %H:%M:%S"))
            cleaned.append(now.strftime("%Y-%m-%d %H:%M:%S"))
            user["rf_card_failures"] = cleaned
            box["count"] = len(cleaned)

        if UserService._update_user(user_id, mutate) is None and box["count"] == 0:
            return 0
        return box["count"]

    @staticmethod
    def increment_rf_card_cancel_streak(user_id: int) -> int:
        def mutate(user):
            current = int(user.get("rf_card_cancel_streak") or 0) + 1
            user["rf_card_cancel_streak"] = current
            return current

        result = UserService._update_user(user_id, mutate)
        return int(result or 0)

    @staticmethod
    def reset_rf_card_cancel_streak(user_id: int):
        UserService._update_user(user_id, lambda user: user.__setitem__("rf_card_cancel_streak", 0))

    @staticmethod
    def get_last_rf_card(user_id: int) -> str | None:
        user = UserService._merge_fresh_user(user_id)
        return user.get("last_rf_card") if isinstance(user, dict) else None

    @staticmethod
    def set_last_rf_card(user_id: int, card_number: str | None):
        UserService._update_user(
            user_id,
            lambda user: user.__setitem__("last_rf_card", card_number),
            create=True,
        )

    @staticmethod
    def get_last_yoomoney_wallet(user_id: int) -> str | None:
        user = UserService._merge_fresh_user(user_id)
        return user.get("last_yoomoney_wallet") if isinstance(user, dict) else None

    @staticmethod
    def set_last_yoomoney_wallet(user_id: int, wallet: str | None):
        UserService._update_user(
            user_id,
            lambda user: user.__setitem__("last_yoomoney_wallet", wallet),
            create=True,
        )

    @staticmethod
    def set_yoomoney_block_until(user_id: int, until_dt: str):
        def mutate(user):
            user["yoomoney_block_until"] = until_dt
            user["yoomoney_cancel_streak"] = 0
        UserService._update_user(user_id, mutate)

    @staticmethod
    def is_yoomoney_blocked(user_id: int):
        user = UserService._merge_fresh_user(user_id)
        if not isinstance(user, dict):
            return False, None
        block_until = user.get("yoomoney_block_until")
        if block_until:
            try:
                dt = datetime.strptime(block_until, "%Y-%m-%d %H:%M:%S")
                if dt > get_now_msk():
                    return True, block_until
                UserService._update_user(user_id, lambda item: item.__setitem__("yoomoney_block_until", None))
            except Exception:
                pass
        return False, None

    @staticmethod
    def increment_yoomoney_cancel_streak(user_id: int) -> int:
        def mutate(user):
            current = int(user.get("yoomoney_cancel_streak") or 0) + 1
            user["yoomoney_cancel_streak"] = current
            return current

        result = UserService._update_user(user_id, mutate)
        return int(result or 0)

    @staticmethod
    def reset_yoomoney_cancel_streak(user_id: int):
        UserService._update_user(user_id, lambda user: user.__setitem__("yoomoney_cancel_streak", 0))

    @staticmethod
    def unblock_yoomoney_payment(user_id: int) -> bool:
        box = {"changed": False}

        def mutate(user):
            if user.get("yoomoney_block_until"):
                user["yoomoney_block_until"] = None
                user["yoomoney_cancel_streak"] = 0
                box["changed"] = True

        UserService._update_user(user_id, mutate)
        return box["changed"]

    @staticmethod
    def unblock_rf_card_payment(user_id: int) -> bool:
        box = {"changed": False}

        def mutate(user):
            if user.get("rf_card_block_until"):
                user["rf_card_block_until"] = None
                user["rf_card_cancel_streak"] = 0
                box["changed"] = True

        UserService._update_user(user_id, mutate)
        return box["changed"]

    @staticmethod
    def get_temp_banned_users():
        data = UserService._load_db()
        result = []
        now = get_now_msk()
        for uid, udata in data["users"].items():
            ban_until = udata.get("banned_until")
            if ban_until:
                try:
                    dt = datetime.strptime(ban_until, "%Y-%m-%d %H:%M:%S")
                    if dt > now:
                        result.append({
                            "id": uid, 
                            "username": udata.get("username"),
                            "until": ban_until
                        })
                except:
                    pass
        return result

    @staticmethod
    def get_stats():
        snap = UserService.get_audience_snapshot()
        return {
            "total": snap["total"],
            "active": snap["active"],
            "blocked": snap["blocked"],
        }

    @staticmethod
    def get_audience_snapshot() -> dict:
        data = UserService._load_db()
        users = data.get("users") if isinstance(data.get("users"), dict) else {}
        total = 0
        active = 0
        buyers = 0
        with_balance = 0
        referred = 0
        by_bot: dict[str, int] = {}
        for user in users.values():
            if not isinstance(user, dict):
                continue
            total += 1
            if user.get("active", True):
                active += 1
            try:
                if int(user.get("purchases_count") or 0) > 0:
                    buyers += 1
            except (TypeError, ValueError):
                pass
            if UserService._as_balance(user.get("balance")) > 0:
                with_balance += 1
            if user.get("referrer_id"):
                referred += 1
            bot_key = str(user.get("last_bot_id") or "-")
            by_bot[bot_key] = by_bot.get(bot_key, 0) + 1
        return {
            "total": total,
            "active": active,
            "blocked": max(0, total - active),
            "buyers": buyers,
            "with_balance": with_balance,
            "referred": referred,
            "by_bot": by_bot,
        }

    @staticmethod
    def get_user_id_by_username(username: str):
        """Поиск ID пользователя по никнейму (без @)"""
        clean_username = username.lstrip("@").lower()
        if not clean_username:
            return None
        if PostgresDocumentStore.is_enabled():
            found = PostgresDocumentStore.find_user_id_by_username(clean_username)
            if found:
                return int(found)
            return None
        data = UserService._load_db()
        
        for uid, info in data.get("users", {}).items():
            stored_username = (info.get("username") or "").lstrip("@").lower()
            if stored_username and stored_username == clean_username:
                return int(uid)
        return None

    @staticmethod
    def get_username(user_id: int):
        user = UserService._merge_fresh_user(user_id, force=True)
        if user:
            return user.get("username")
        return None

    @staticmethod
    def get_all_users():
        if PostgresDocumentStore.is_enabled():
            return [int(uid) for uid in PostgresDocumentStore.load_all_user_ids()]
        data = UserService._load_db()
        return [int(uid) for uid in data.get("users", {}).keys()]

    @staticmethod
    def get_broadcast_user_ids(bot_id, *, include_unassigned: bool = True) -> list[int]:
        bot_key = str(bot_id or "")
        if PostgresDocumentStore.is_enabled():
            return [
                int(uid)
                for uid in PostgresDocumentStore.load_user_ids_for_broadcast(
                    bot_key,
                    include_unassigned=include_unassigned,
                )
            ]
        data = UserService._load_db()
        result = []
        for uid, info in data.get("users", {}).items():
            last_bot = str((info or {}).get("last_bot_id") or "")
            if last_bot == bot_key or (include_unassigned and last_bot == ""):
                try:
                    result.append(int(uid))
                except (TypeError, ValueError):
                    continue
        return result

    @staticmethod
    def user_last_bot_id(user_id: int) -> str | None:
        user = UserService._merge_fresh_user(user_id)
        if not isinstance(user, dict):
            return None
        value = user.get("last_bot_id")
        return str(value) if value else None

    @staticmethod
    def touch_last_bot(user_id: int, bot_id) -> None:
        if not bot_id:
            return
        bot_key = str(bot_id)
        cached = None
        data = UserService._load_db()
        user = data.get("users", {}).get(str(user_id))
        if isinstance(user, dict):
            cached = user
        if cached is not None and str(cached.get("last_bot_id") or "") == bot_key:
            return

        def mutate(item):
            if str(item.get("last_bot_id") or "") == bot_key:
                return False
            item["last_bot_id"] = bot_key
            return True

        UserService._update_user(user_id, mutate)

    @staticmethod
    def clear_users_db() -> int:
        data = UserService._load_db()
        users = data.get("users", {})
        total = len(users) if isinstance(users, dict) else 0

        if not isinstance(users, dict):
            data["users"] = {}
            UserService._save_db(data, replace_all=True)
            return total

        kept_users = {}
        for user_id, user_data in users.items():
            balance = float(user_data.get("balance", 0.0) or 0.0)
            purchases_count = int(user_data.get("purchases_count", 0) or 0)

            if balance != 0.0 or purchases_count > 0:
                kept_users[user_id] = user_data

        data["users"] = kept_users
        UserService._save_db(data, replace_all=True)
        return total - len(kept_users)
