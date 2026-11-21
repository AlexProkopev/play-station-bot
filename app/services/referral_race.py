from __future__ import annotations

import json
import os
import random
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta

from app.services.postgres_store import PostgresDocumentStore
from app.utils import get_now_msk

RACE_DOC_KEY = "referral_race"
RACE_FILE = "storage/referral_race.json"
RACE_HOURS = 48
PRIZES = {1: 5000, 2: 2500, 3: 1000}
TIME_FMT = "%Y-%m-%d %H:%M:%S"

DEFAULT_RACE = {
    "active": False,
    "id": None,
    "started_at": None,
    "ends_at": None,
    "fakes": [],
    "participants": {},
    "awarded": {},
}

_lock = threading.RLock()


def _parse_time(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:19], TIME_FMT)
    except ValueError:
        return None


def _fmt(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(TIME_FMT)


class ReferralRaceService:
    @classmethod
    def _load(cls) -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(RACE_DOC_KEY, DEFAULT_RACE)
            return cls._normalize(data)
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        if not os.path.exists(RACE_FILE):
            return deepcopy(DEFAULT_RACE)
        try:
            with open(RACE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return deepcopy(DEFAULT_RACE)
        return cls._normalize(data)

    @classmethod
    def _save(cls, data: dict) -> None:
        stored = cls._normalize(data)
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(RACE_DOC_KEY, stored, force=True)
            return
        os.makedirs("storage", exist_ok=True)
        with open(RACE_FILE, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, ensure_ascii=False, indent=2)

    @classmethod
    def _mutate(cls, mutator):
        with _lock:
            if PostgresDocumentStore.is_enabled():
                def wrapped(payload):
                    normalized = cls._normalize(payload)
                    payload.clear()
                    payload.update(normalized)
                    mutator(payload)

                return PostgresDocumentStore.mutate_document(
                    RACE_DOC_KEY,
                    wrapped,
                    default=DEFAULT_RACE,
                )
            data = cls._load()
            mutator(data)
            cls._save(data)
            return data

    @classmethod
    def _normalize(cls, data) -> dict:
        merged = deepcopy(DEFAULT_RACE)
        if isinstance(data, dict):
            merged.update(data)
        merged["active"] = bool(merged.get("active"))
        if not isinstance(merged.get("fakes"), list):
            merged["fakes"] = []
        if not isinstance(merged.get("participants"), dict):
            merged["participants"] = {}
        if not isinstance(merged.get("awarded"), dict):
            merged["awarded"] = {}
        merged["awarded"] = {str(k): v for k, v in merged["awarded"].items()}
        return merged

    @classmethod
    def get_race(cls, now: datetime | None = None) -> dict:
        race = cls._load()
        current = now or get_now_msk()
        ends = _parse_time(race.get("ends_at"))
        if race.get("active") and ends and current >= ends:
            def expire(payload):
                if payload.get("active"):
                    end_at = _parse_time(payload.get("ends_at"))
                    if end_at and current >= end_at:
                        payload["active"] = False

            race = cls._mutate(expire) or race
            race = cls._normalize(race)
        return deepcopy(race)

    @classmethod
    def is_running(cls, now: datetime | None = None) -> bool:
        race = cls.get_race(now)
        return bool(race.get("active") and race.get("id"))

    @classmethod
    def start(cls, *, now: datetime | None = None, rng: random.Random | None = None) -> dict:
        current = now or get_now_msk()
        dice = rng or random.Random(f"{current.timestamp()}-{uuid.uuid4().hex}")
        race_id = uuid.uuid4().hex[:10]
        ends = current + timedelta(hours=RACE_HOURS)
        fakes = cls._build_fakes(dice)

        def mutate(payload):
            payload.clear()
            payload.update(
                {
                    "active": True,
                    "id": race_id,
                    "started_at": _fmt(current),
                    "ends_at": _fmt(ends),
                    "fakes": fakes,
                    "participants": {},
                    "awarded": {},
                }
            )

        return deepcopy(cls._mutate(mutate))

    @classmethod
    def stop(cls) -> dict:
        def mutate(payload):
            payload["active"] = False

        return deepcopy(cls._mutate(mutate))

    @staticmethod
    def _build_fakes(rng: random.Random) -> list[dict]:
        count = rng.randint(16, 24)
        fakes = []
        for index in range(count):
            gray0 = rng.choice([0, 0, 1, 1, 2, 3])
            combat0 = rng.choice([0, 0, 0, 1, 1])
            events = [
                {
                    "h": round(rng.uniform(6.0, 20.0), 2),
                    "g": rng.choice([1, 1, 2, 2, 5]),
                    "c": rng.choice([0, 0, 0, 1]),
                },
                {
                    "h": round(rng.uniform(24.0, 42.0), 2),
                    "g": rng.choice([1, 2, 2, 5]),
                    "c": rng.choice([0, 0, 1]),
                },
            ]
            if rng.random() < 0.4:
                events.append(
                    {
                        "h": round(rng.uniform(10.0, 36.0), 2),
                        "g": rng.choice([1, 1, 2]),
                        "c": 0,
                    }
                )
            events.sort(key=lambda item: float(item["h"]))
            fakes.append(
                {
                    "id": f"m{index + 1}",
                    "g0": int(gray0),
                    "c0": int(combat0),
                    "ev": events,
                }
            )
        return fakes

    @classmethod
    def _fake_scores(cls, race: dict, now: datetime) -> list[dict]:
        started = _parse_time(race.get("started_at")) or now
        elapsed_h = max(0.0, (now - started).total_seconds() / 3600.0)
        rows = []
        for fake in race.get("fakes") or []:
            gray = int(fake.get("g0") or 0)
            combat = int(fake.get("c0") or 0)
            for event in fake.get("ev") or []:
                if float(event.get("h") or 0) <= elapsed_h:
                    gray += int(event.get("g") or 0)
                    combat += int(event.get("c") or 0)
            rows.append(
                {
                    "kind": "fake",
                    "id": str(fake.get("id") or ""),
                    "gray": gray,
                    "combat": combat,
                }
            )
        return rows

    @classmethod
    def _real_scores(cls, race: dict) -> list[dict]:
        rows = []
        participants = race.get("participants") if isinstance(race.get("participants"), dict) else {}
        for uid, stats in participants.items():
            if not isinstance(stats, dict):
                continue
            gray = int(stats.get("gray") or 0)
            combat = int(stats.get("combat") or 0)
            if gray <= 0 and combat <= 0:
                continue
            rows.append(
                {
                    "kind": "real",
                    "id": str(uid),
                    "gray": gray,
                    "combat": combat,
                }
            )
        return rows

    @classmethod
    def board(cls, user_id: int | None = None, now: datetime | None = None) -> dict:
        current = now or get_now_msk()
        race = cls.get_race(current)
        rows = cls._fake_scores(race, current) + cls._real_scores(race)
        viewer = str(user_id) if user_id is not None else None
        if viewer and not any(item["kind"] == "real" and item["id"] == viewer for item in rows):
            stats = (race.get("participants") or {}).get(viewer) or {}
            rows.append(
                {
                    "kind": "real",
                    "id": viewer,
                    "gray": int(stats.get("gray") or 0),
                    "combat": int(stats.get("combat") or 0),
                    "self": True,
                }
            )
        rows.sort(key=lambda item: (-int(item["combat"]), -int(item["gray"]), str(item["id"])))
        for index, item in enumerate(rows, 1):
            item["place"] = index
            if viewer and item.get("id") == viewer and item.get("kind") == "real":
                item["self"] = True
        self_row = next((item for item in rows if item.get("self")), None)
        ends = _parse_time(race.get("ends_at"))
        left = None
        if race.get("active") and ends:
            left = max(timedelta(0), ends - current)
        return {
            "race": race,
            "rows": rows,
            "self": self_row,
            "total": len(rows),
            "left": left,
            "running": bool(race.get("active") and race.get("id")),
        }

    @classmethod
    def note_gray(cls, referrer_id, invited_id) -> None:
        if not referrer_id or not invited_id:
            return
        if str(referrer_id) == str(invited_id):
            return
        now = get_now_msk()
        if not cls.is_running(now):
            return

        def mutate(payload):
            ends = _parse_time(payload.get("ends_at"))
            if not payload.get("active") or (ends and now >= ends):
                return
            people = payload.setdefault("participants", {})
            rec = people.setdefault(str(referrer_id), {"gray": 0, "combat": 0, "gray_ids": [], "combat_ids": []})
            invited = str(invited_id)
            seen = rec.setdefault("gray_ids", [])
            if invited in seen:
                return
            seen.append(invited)
            rec["gray"] = int(rec.get("gray") or 0) + 1

        cls._mutate(mutate)

    @classmethod
    def note_combat(cls, referrer_id, buyer_id) -> None:
        if not referrer_id or not buyer_id:
            return
        if str(referrer_id) == str(buyer_id):
            return
        now = get_now_msk()
        if not cls.is_running(now):
            return

        def mutate(payload):
            ends = _parse_time(payload.get("ends_at"))
            if not payload.get("active") or (ends and now >= ends):
                return
            people = payload.setdefault("participants", {})
            rec = people.setdefault(str(referrer_id), {"gray": 0, "combat": 0, "gray_ids": [], "combat_ids": []})
            buyer = str(buyer_id)
            seen = rec.setdefault("combat_ids", [])
            if buyer in seen:
                return
            seen.append(buyer)
            rec["combat"] = int(rec.get("combat") or 0) + 1

        cls._mutate(mutate)

    @classmethod
    def award(cls, place: int, user_id: int) -> dict:
        place = int(place)
        if place not in PRIZES:
            raise ValueError("bad place")
        uid = int(user_id)
        box = {"ok": False, "reason": "missing"}

        def mutate(payload):
            awarded = payload.setdefault("awarded", {})
            for existing_place, existing_uid in list(awarded.items()):
                if str(existing_uid) == str(uid) and int(existing_place) != place:
                    box.update({"ok": False, "reason": "already"})
                    return
            if str(place) in awarded:
                box.update({"ok": False, "reason": "place"})
                return
            awarded[str(place)] = uid
            box.update({"ok": True, "place": place, "user_id": uid, "prize": PRIZES[place]})

        cls._mutate(mutate)
        return box

    @classmethod
    def real_leaderboard(cls, now: datetime | None = None) -> list[dict]:
        race = cls.get_race(now)
        rows = cls._real_scores(race)
        rows.sort(key=lambda item: (-int(item["combat"]), -int(item["gray"]), str(item["id"])))
        for index, item in enumerate(rows, 1):
            item["place"] = index
        return rows
