from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta

from app.services.postgres_store import PostgresDocumentStore
from app.utils import get_now_msk

STATS_FILE = "storage/bot_stats.json"
STATS_DOC_KEY = "bot_stats"
KEEP_DAYS = 120
STAMP_FMT = "%Y-%m-%d %H:%M:%S"
DAY_FMT = "%Y-%m-%d"

_lock = threading.RLock()
_EMPTY = {"days": {}, "starts": {}, "backfilled_joined": False}


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (STAMP_FMT, "%Y-%m-%dT%H:%M:%S", DAY_FMT):
        try:
            return datetime.strptime(text[:19] if fmt != DAY_FMT else text[:10], fmt)
        except ValueError:
            continue
    return None


def _day_key(now: datetime | None = None) -> str:
    return (now or get_now_msk()).strftime(DAY_FMT)


def _fmt_int(value) -> str:
    return f"{max(0, _as_int(value)):,}".replace(",", " ")


def _fmt_money(value) -> str:
    number = max(0.0, _as_float(value))
    if abs(number - round(number)) < 0.001:
        return f"{number:,.0f}".replace(",", " ")
    return f"{number:,.2f}".replace(",", " ")


def _pct(part, whole) -> str:
    total = _as_int(whole)
    if total <= 0:
        return "-"
    return f"{(max(0, _as_int(part)) * 100.0 / total):.0f}%"


def _unique_map(value) -> dict:
    if isinstance(value, dict):
        return {str(key): 1 for key in value}
    if isinstance(value, list):
        return {str(item): 1 for item in value if item is not None}
    return {}


def _empty_day() -> dict:
    return {
        "starts": 0,
        "starts_u": {},
        "returning_u": {},
        "captcha": 0,
        "captcha_u": {},
        "captcha_new": 0,
        "captcha_fail": 0,
        "blocked": 0,
        "blocked_u": {},
        "unblocked": 0,
        "purchases": 0,
        "buyers_u": {},
        "revenue": 0.0,
        "referrals": 0,
        "city": 0,
        "city_u": {},
        "city_found": 0,
        "city_found_u": {},
        "city_miss": 0,
    }


class BotStatsService:
    @classmethod
    def _load(cls) -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(STATS_DOC_KEY, _EMPTY)
            return data if isinstance(data, dict) else deepcopy(_EMPTY)
        if not os.path.exists(STATS_FILE):
            return deepcopy(_EMPTY)
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else deepcopy(_EMPTY)
        except Exception:
            return deepcopy(_EMPTY)

    @classmethod
    def _save(cls, data: dict) -> None:
        payload = data if isinstance(data, dict) else deepcopy(_EMPTY)
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(STATS_DOC_KEY, payload)
            return
        os.makedirs("storage", exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    @classmethod
    def _mutate(cls, mutator) -> dict:
        with _lock:
            if PostgresDocumentStore.is_enabled():
                stored = PostgresDocumentStore.mutate_document(
                    STATS_DOC_KEY,
                    mutator,
                    default=deepcopy(_EMPTY),
                )
                return stored if isinstance(stored, dict) else deepcopy(_EMPTY)
            data = cls._load()
            mutator(data)
            cls._save(data)
            return data

    @classmethod
    def _prune(cls, data: dict, now: datetime | None = None) -> None:
        days = data.get("days")
        if not isinstance(days, dict):
            data["days"] = {}
            return
        cutoff = (now or get_now_msk()) - timedelta(days=KEEP_DAYS)
        stale = []
        for key in days:
            stamp = _parse_dt(key)
            if stamp is None or stamp < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                stale.append(key)
        for key in stale:
            days.pop(key, None)

    @classmethod
    def _day(cls, data: dict, day: str) -> dict:
        days = data.setdefault("days", {})
        bucket = days.get(day)
        if not isinstance(bucket, dict):
            bucket = _empty_day()
            days[day] = bucket
        for key, value in _empty_day().items():
            if key not in bucket:
                bucket[key] = deepcopy(value)
        return bucket

    @classmethod
    def _mark_unique(cls, bucket: dict, field: str, user_id) -> bool:
        store = bucket.setdefault(field, {})
        if not isinstance(store, dict):
            store = _unique_map(store)
            bucket[field] = store
        key = str(user_id)
        if key in store:
            return False
        store[key] = 1
        return True

    @classmethod
    def note_start(cls, user_id: int) -> None:
        now = get_now_msk()
        day = _day_key(now)
        stamp = now.strftime(STAMP_FMT)
        uid = str(user_id)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["starts"] = _as_int(bucket.get("starts")) + 1
            cls._mark_unique(bucket, "starts_u", uid)
            starts = data.setdefault("starts", {})
            rec = starts.get(uid) if isinstance(starts.get(uid), dict) else None
            if rec:
                rec["last"] = stamp
                rec["count"] = _as_int(rec.get("count")) + 1
                first = str(rec.get("first") or "")[:10]
                if first and first < day:
                    cls._mark_unique(bucket, "returning_u", uid)
            else:
                starts[uid] = {"first": stamp, "last": stamp, "count": 1, "captcha": False}

        cls._mutate(mutate)

    @classmethod
    def note_captcha(cls, user_id: int, *, is_new: bool = False) -> None:
        now = get_now_msk()
        day = _day_key(now)
        uid = str(user_id)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["captcha"] = _as_int(bucket.get("captcha")) + 1
            cls._mark_unique(bucket, "captcha_u", uid)
            if is_new:
                bucket["captcha_new"] = _as_int(bucket.get("captcha_new")) + 1
            starts = data.setdefault("starts", {})
            rec = starts.get(uid)
            if isinstance(rec, dict):
                rec["captcha"] = True
            else:
                stamp = now.strftime(STAMP_FMT)
                starts[uid] = {"first": stamp, "last": stamp, "count": 1, "captcha": True}

        cls._mutate(mutate)

    @classmethod
    def note_captcha_fail(cls, user_id: int) -> None:
        now = get_now_msk()
        day = _day_key(now)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["captcha_fail"] = _as_int(bucket.get("captcha_fail")) + 1

        cls._mutate(mutate)

    @classmethod
    def note_city_search(cls, user_id: int, *, found: bool) -> None:
        now = get_now_msk()
        day = _day_key(now)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["city"] = _as_int(bucket.get("city")) + 1
            cls._mark_unique(bucket, "city_u", user_id)
            if found:
                bucket["city_found"] = _as_int(bucket.get("city_found")) + 1
                cls._mark_unique(bucket, "city_found_u", user_id)
            else:
                bucket["city_miss"] = _as_int(bucket.get("city_miss")) + 1

        cls._mutate(mutate)

    @classmethod
    def note_block(cls, user_id: int) -> None:
        now = get_now_msk()
        day = _day_key(now)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["blocked"] = _as_int(bucket.get("blocked")) + 1
            cls._mark_unique(bucket, "blocked_u", user_id)

        cls._mutate(mutate)

    @classmethod
    def note_unblock(cls, user_id: int) -> None:
        now = get_now_msk()
        day = _day_key(now)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["unblocked"] = _as_int(bucket.get("unblocked")) + 1

        cls._mutate(mutate)

    @classmethod
    def note_purchase(cls, user_id: int, amount=0) -> None:
        now = get_now_msk()
        day = _day_key(now)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["purchases"] = _as_int(bucket.get("purchases")) + 1
            bucket["revenue"] = round(_as_float(bucket.get("revenue")) + max(0.0, _as_float(amount)), 2)
            cls._mark_unique(bucket, "buyers_u", user_id)

        cls._mutate(mutate)

    @classmethod
    def note_referral(cls, referrer_id) -> None:
        try:
            int(referrer_id)
        except (TypeError, ValueError):
            return
        now = get_now_msk()
        day = _day_key(now)

        def mutate(data: dict):
            cls._prune(data, now)
            bucket = cls._day(data, day)
            bucket["referrals"] = _as_int(bucket.get("referrals")) + 1

        cls._mutate(mutate)

    @classmethod
    def backfill_from_users(cls, users: dict | None) -> None:
        if not isinstance(users, dict) or not users:
            return
        current = cls._load()
        if current.get("backfilled_joined"):
            return

        def mutate(data: dict):
            if data.get("backfilled_joined"):
                return
            for uid, user in users.items():
                if not isinstance(user, dict):
                    continue
                joined = _parse_dt(user.get("joined"))
                if joined is None:
                    continue
                day = joined.strftime(DAY_FMT)
                bucket = cls._day(data, day)
                bucket["captcha"] = _as_int(bucket.get("captcha")) + 1
                bucket["captcha_new"] = _as_int(bucket.get("captcha_new")) + 1
                cls._mark_unique(bucket, "captcha_u", uid)
                bucket["starts"] = _as_int(bucket.get("starts")) + 1
                cls._mark_unique(bucket, "starts_u", uid)
                stamp = joined.strftime(STAMP_FMT)
                starts = data.setdefault("starts", {})
                if str(uid) not in starts:
                    starts[str(uid)] = {
                        "first": stamp,
                        "last": stamp,
                        "count": 1,
                        "captcha": True,
                    }
            data["backfilled_joined"] = True
            cls._prune(data)

        cls._mutate(mutate)

    @classmethod
    def _sum_days(cls, data: dict, start: datetime, end: datetime) -> dict:
        acc = {
            "starts": 0,
            "starts_u": {},
            "returning_u": {},
            "captcha": 0,
            "captcha_u": {},
            "captcha_new": 0,
            "captcha_fail": 0,
            "blocked": 0,
            "blocked_u": {},
            "unblocked": 0,
            "purchases": 0,
            "buyers_u": {},
            "revenue": 0.0,
            "referrals": 0,
            "city": 0,
            "city_u": {},
            "city_found": 0,
            "city_found_u": {},
            "city_miss": 0,
        }
        days = data.get("days") if isinstance(data.get("days"), dict) else {}
        cursor = start.date()
        last = end.date()
        while cursor <= last:
            bucket = days.get(cursor.strftime(DAY_FMT))
            cursor = cursor + timedelta(days=1)
            if not isinstance(bucket, dict):
                continue
            acc["starts"] += _as_int(bucket.get("starts"))
            acc["captcha"] += _as_int(bucket.get("captcha"))
            acc["captcha_new"] += _as_int(bucket.get("captcha_new"))
            acc["captcha_fail"] += _as_int(bucket.get("captcha_fail"))
            acc["blocked"] += _as_int(bucket.get("blocked"))
            acc["unblocked"] += _as_int(bucket.get("unblocked"))
            acc["purchases"] += _as_int(bucket.get("purchases"))
            acc["revenue"] += _as_float(bucket.get("revenue"))
            acc["referrals"] += _as_int(bucket.get("referrals"))
            acc["city"] += _as_int(bucket.get("city"))
            acc["city_found"] += _as_int(bucket.get("city_found"))
            acc["city_miss"] += _as_int(bucket.get("city_miss"))
            acc["starts_u"].update(_unique_map(bucket.get("starts_u")))
            acc["returning_u"].update(_unique_map(bucket.get("returning_u")))
            acc["captcha_u"].update(_unique_map(bucket.get("captcha_u")))
            acc["blocked_u"].update(_unique_map(bucket.get("blocked_u")))
            acc["buyers_u"].update(_unique_map(bucket.get("buyers_u")))
            acc["city_u"].update(_unique_map(bucket.get("city_u")))
            acc["city_found_u"].update(_unique_map(bucket.get("city_found_u")))
        acc["starts_unique"] = len(acc["starts_u"])
        acc["returning"] = len(acc["returning_u"])
        acc["captcha_unique"] = len(acc["captcha_u"])
        acc["blocked_unique"] = len(acc["blocked_u"])
        acc["buyers"] = len(acc["buyers_u"])
        acc["city_unique"] = len(acc["city_u"])
        acc["city_found_unique"] = len(acc["city_found_u"])
        acc["no_captcha"] = max(0, acc["starts_unique"] - acc["captcha_unique"])
        acc["no_city"] = max(0, acc["captcha_unique"] - acc["city_unique"])
        return acc

    @classmethod
    def period_bundle(cls, now: datetime | None = None) -> dict:
        stamp = now or get_now_msk()
        today = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
        data = cls._load()
        return {
            "today": cls._sum_days(data, today, today),
            "week": cls._sum_days(data, today - timedelta(days=6), today),
            "month": cls._sum_days(data, today - timedelta(days=29), today),
        }


def _period_block(title: str, stats: dict) -> str:
    starts_u = stats.get("starts_unique") or 0
    captcha_u = stats.get("captcha_unique") or 0
    city_u = stats.get("city_unique") or 0
    city_found_u = stats.get("city_found_unique") or 0
    lines = [
        f"<b>{title}</b>",
        "🚪 <b>Воронка входа</b>",
        f"🚀 Зашло: <b>{_fmt_int(starts_u)}</b> · запусков {_fmt_int(stats.get('starts'))}",
        f"✅ Ввели капчу: <b>{_fmt_int(captcha_u)}</b> ({_pct(captcha_u, starts_u)}) · новых {_fmt_int(stats.get('captcha_new'))}",
        f"🏙 Искали город: <b>{_fmt_int(city_u)}</b> ({_pct(city_u, captcha_u)}) · запросов {_fmt_int(stats.get('city'))}",
        f"   нашли {_fmt_int(city_found_u)} · не нашли {_fmt_int(stats.get('no_city'))} после капчи · пустых поисков {_fmt_int(stats.get('city_miss'))}",
        f"⏳ Не прошли капчу: <b>{_fmt_int(stats.get('no_captcha'))}</b> · ошибок ввода {_fmt_int(stats.get('captcha_fail'))}",
        f"🔴 Заблокировали бота: <b>{_fmt_int(stats.get('blocked_unique'))}</b> ({_fmt_int(stats.get('blocked'))} раз)",
        f"🟢 Разблокировали: <b>{_fmt_int(stats.get('unblocked'))}</b>",
        f"🛒 Покупки: <b>{_fmt_int(stats.get('purchases'))}</b> · покупателей <b>{_fmt_int(stats.get('buyers'))}</b> · {_fmt_money(stats.get('revenue'))} ₽",
        f"👥 Рефералы: <b>{_fmt_int(stats.get('referrals'))}</b>",
        f"🔁 Вернулись в /start: <b>{_fmt_int(stats.get('returning'))}</b>",
        f"📈 Воронка: старт→капча <b>{_pct(captcha_u, starts_u)}</b> · капча→город <b>{_pct(city_u, captcha_u)}</b> · город→покупка <b>{_pct(stats.get('buyers'), city_u)}</b>",
    ]
    return "\n".join(lines)


def build_stats_text(audience: dict, periods: dict, now: datetime | None = None) -> str:
    stamp = now or get_now_msk()
    total = _as_int(audience.get("total"))
    active = _as_int(audience.get("active"))
    blocked = _as_int(audience.get("blocked"))
    lines = [
        "📊 <b>Статистика бота</b>",
        f"<i>МСК {stamp.strftime('%d.%m.%Y %H:%M')}</i>",
        "",
        "<b>Сейчас</b>",
        f"👥 В базе (все боты): <b>{_fmt_int(total)}</b>",
        f"🟢 Не блокнули: <b>{_fmt_int(active)}</b> ({_pct(active, total)})",
        f"🔴 Заблокировали бота: <b>{_fmt_int(blocked)}</b> ({_pct(blocked, total)})",
        f"🛒 С покупкой: <b>{_fmt_int(audience.get('buyers'))}</b>",
        f"💰 С балансом: <b>{_fmt_int(audience.get('with_balance'))}</b>",
        f"🔗 Пришли по рефке: <b>{_fmt_int(audience.get('referred'))}</b>",
    ]
    by_bot = audience.get("by_bot") if isinstance(audience.get("by_bot"), dict) else {}
    if len(by_bot) > 1:
        lines.append("🤖 По ботам:")
        for bot_id, count in sorted(by_bot.items(), key=lambda item: (-item[1], str(item[0]))):
            label = "Без метки" if str(bot_id) in {"", "-", "—", "None"} else str(bot_id)
            lines.append(f"· {label}: <b>{_fmt_int(count)}</b>")
    lines.extend([
        "",
        _period_block("Сегодня", periods.get("today") or {}),
        "",
        _period_block("7 дней", periods.get("week") or {}),
        "",
        _period_block("30 дней", periods.get("month") or {}),
    ])
    return "\n".join(lines)
