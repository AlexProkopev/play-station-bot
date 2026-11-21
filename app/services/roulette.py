import json
import os
import random
import re
import html
from typing import Any

from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService
from app.utils import get_now_msk

_MONEY_PRIZE_RE = re.compile(
    r"^(?:деньги\s+|баланс\s+)?(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|р\.?|руб(?:\.|лей|ля|ль)?)?$",
    re.IGNORECASE,
)

ROULETTE_FILE = "storage/roulette_games.json"
ROULETTE_DOC_KEY = "roulette_games"

DEFAULT_DB = {
    "next_id": 1,
    "games": [],
}


class RouletteService:
    @staticmethod
    def parse_prize_line(raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        match = _MONEY_PRIZE_RE.match(text.replace("\xa0", " ").strip())
        if match:
            amount_raw = match.group(1).replace(" ", "").replace(",", ".")
            try:
                amount = float(amount_raw)
            except ValueError:
                return None
            if amount <= 0:
                return None
            if amount == int(amount):
                label = f"{int(amount)} руб"
            else:
                label = f"{amount:.2f} руб"
            return {"kind": "money", "amount": amount, "label": label}
        return {"kind": "item", "amount": 0.0, "label": text}

    @staticmethod
    def parse_prize_lines(raw: str, expected_count: int) -> tuple[list[dict[str, Any]] | None, str]:
        chunks: list[str] = []
        for line in str(raw or "").replace(";", "\n").splitlines():
            parts = [chunk.strip() for chunk in line.split(",") if chunk.strip()]
            chunks.extend(parts)
        if len(chunks) != int(expected_count):
            return None, f"Нужно указать ровно {expected_count} приз(а/ов) - по одному на место."
        prizes: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks, start=1):
            parsed = RouletteService.parse_prize_line(chunk)
            if not parsed:
                return None, f"Не удалось разобрать приз для {idx} места."
            prizes.append({"place": idx, **parsed})
        return prizes, ""

    @staticmethod
    def prizes_of(game: dict[str, Any]) -> list[dict[str, Any]]:
        prizes = game.get("prizes")
        if isinstance(prizes, list) and prizes:
            normalized = []
            for idx, item in enumerate(prizes, start=1):
                if not isinstance(item, dict):
                    continue
                place = int(item.get("place", idx) or idx)
                kind = str(item.get("kind") or "item")
                try:
                    amount = float(item.get("amount", 0.0) or 0.0)
                except (TypeError, ValueError):
                    amount = 0.0
                label = str(item.get("label") or "").strip()
                if not label:
                    label = f"{amount:.0f} руб" if kind == "money" and amount > 0 else "приз"
                normalized.append({"place": place, "kind": kind, "amount": amount, "label": label})
            if normalized:
                return sorted(normalized, key=lambda item: int(item.get("place", 0) or 0))

        try:
            legacy_amount = float(game.get("prize_per_winner", 0.0) or 0.0)
        except (TypeError, ValueError):
            legacy_amount = 0.0
        winners_count = int(game.get("winners_count", 0) or 0)
        if winners_count <= 0:
            winners_count = max(1, len(game.get("winners") or []), len(game.get("money_winners") or []))
        if legacy_amount > 0 and winners_count > 0:
            label = f"{int(legacy_amount)} руб" if legacy_amount == int(legacy_amount) else f"{legacy_amount:.2f} руб"
            return [
                {"place": idx, "kind": "money", "amount": legacy_amount, "label": label}
                for idx in range(1, winners_count + 1)
            ]
        return []

    @staticmethod
    def place_winners_of(game: dict[str, Any]) -> list[dict[str, Any]]:
        stored = game.get("place_winners")
        if isinstance(stored, list) and stored:
            result = []
            for item in stored:
                if not isinstance(item, dict):
                    continue
                uid = int(item.get("user_id", 0) or 0)
                if uid <= 0:
                    continue
                result.append(
                    {
                        "place": int(item.get("place", 0) or 0),
                        "user_id": uid,
                        "kind": str(item.get("kind") or "item"),
                        "amount": float(item.get("amount", 0.0) or 0.0),
                        "label": str(item.get("label") or "").strip() or "приз",
                        "paid": bool(item.get("paid")),
                    }
                )
            if result:
                return sorted(result, key=lambda item: int(item.get("place", 0) or 0))

        money_winners = game.get("money_winners") or []
        if money_winners:
            result = []
            for item in money_winners:
                if not isinstance(item, dict):
                    continue
                uid = int(item.get("user_id", 0) or 0)
                if uid <= 0:
                    continue
                amount = float(item.get("amount", 0.0) or 0.0)
                label = f"{int(amount)} руб" if amount == int(amount) else f"{amount:.2f} руб"
                result.append(
                    {
                        "place": int(item.get("place", 0) or 0),
                        "user_id": uid,
                        "kind": "money",
                        "amount": amount,
                        "label": label,
                        "paid": bool(item.get("paid")),
                    }
                )
            product_winner_id = game.get("product_winner_id")
            if product_winner_id is not None:
                result.insert(
                    0,
                    {
                        "place": 0,
                        "user_id": int(product_winner_id),
                        "kind": "item",
                        "amount": 0.0,
                        "label": "главный приз",
                        "paid": False,
                    },
                )
            if result:
                return result

        winners = [int(item) for item in game.get("winners") or [] if int(item or 0) > 0]
        prizes = RouletteService.prizes_of(game)
        result = []
        for idx, uid in enumerate(winners):
            prize = prizes[idx] if idx < len(prizes) else {"kind": "item", "amount": 0.0, "label": "приз"}
            result.append(
                {
                    "place": idx + 1,
                    "user_id": uid,
                    "kind": str(prize.get("kind") or "item"),
                    "amount": float(prize.get("amount", 0.0) or 0.0),
                    "label": str(prize.get("label") or "приз"),
                    "paid": int(uid) in {int(item) for item in game.get("paid_winner_ids") or []},
                }
            )
        return result

    @staticmethod
    def is_participant(game: dict[str, Any], user_id: int) -> bool:
        uid = int(user_id)
        return any(int(item.get("user_id", 0) or 0) == uid for item in game.get("participants") or [])

    @staticmethod
    def next_unassigned_place(game: dict[str, Any]) -> int | None:
        prizes = RouletteService.prizes_of(game)
        assigned = {int(item.get("place", 0) or 0) for item in RouletteService.place_winners_of(game)}
        for prize in prizes:
            place = int(prize.get("place", 0) or 0)
            if place not in assigned:
                return place
        return None

    @staticmethod
    def all_places_assigned(game: dict[str, Any]) -> bool:
        prizes = RouletteService.prizes_of(game)
        if not prizes:
            return bool(game.get("winners") or game.get("money_winners"))
        assigned = {int(item.get("place", 0) or 0) for item in RouletteService.place_winners_of(game) if int(item.get("user_id", 0) or 0) > 0}
        return all(int(item.get("place", 0) or 0) in assigned for item in prizes)

    @staticmethod
    def should_deliver_on_bot(participant: dict[str, Any], bot_id) -> bool:
        uid = int(participant.get("user_id", 0) or 0)
        if uid <= 0:
            return False
        bot_key = str(bot_id or "")
        last_bot = str(UserService.user_last_bot_id(uid) or "").strip()
        joined_bot = str(participant.get("joined_bot_id") or "").strip()
        target = last_bot or joined_bot
        if not target:
            return True
        return target == bot_key

    @staticmethod
    def _normalize_game(game: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(game.get("participants"), list):
            game["participants"] = []
        if not isinstance(game.get("winners"), list):
            game["winners"] = []
        if "prize_per_winner" not in game:
            game["prize_per_winner"] = 0.0
        if not isinstance(game.get("paid_winner_ids"), list):
            game["paid_winner_ids"] = []
        if not isinstance(game.get("payouts"), list):
            game["payouts"] = []
        if "product_winner_id" not in game:
            game["product_winner_id"] = None
        if not isinstance(game.get("money_winners"), list):
            game["money_winners"] = []
        if "result_announced" not in game:
            game["result_announced"] = False
        if "announced_at" not in game:
            game["announced_at"] = None
        if not isinstance(game.get("result_delivered_user_ids"), list):
            game["result_delivered_user_ids"] = []
        if "status" not in game:
            game["status"] = "closed"
        description = str(game.get("description") or game.get("conditions_text") or "").strip()
        game["description"] = description
        if not str(game.get("conditions_text") or "").strip():
            game["conditions_text"] = description
        if not isinstance(game.get("prizes"), list):
            game["prizes"] = []
        game["prizes"] = RouletteService.prizes_of(game)
        if not isinstance(game.get("place_winners"), list):
            game["place_winners"] = []
        winners_count = int(game.get("winners_count", 0) or 0)
        if winners_count <= 0:
            winners_count = len(game["prizes"]) or 1
        game["winners_count"] = winners_count
        return game

    @staticmethod
    def _load_db() -> dict[str, Any]:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ROULETTE_DOC_KEY, DEFAULT_DB)
            if not isinstance(data, dict):
                data = DEFAULT_DB.copy()
            if not isinstance(data.get("games"), list):
                data["games"] = []
            if not isinstance(data.get("next_id"), int) or data.get("next_id", 0) < 1:
                data["next_id"] = 1
            data["games"] = [RouletteService._normalize_game(game) for game in data.get("games", []) if isinstance(game, dict)]
            return data

        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)

        if not os.path.exists(ROULETTE_FILE):
            return DEFAULT_DB.copy()

        try:
            with open(ROULETTE_FILE, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except Exception:
            return DEFAULT_DB.copy()

        if not isinstance(data, dict):
            return DEFAULT_DB.copy()

        if not isinstance(data.get("games"), list):
            data["games"] = []
        if not isinstance(data.get("next_id"), int) or data.get("next_id", 0) < 1:
            data["next_id"] = 1
        data["games"] = [RouletteService._normalize_game(game) for game in data.get("games", []) if isinstance(game, dict)]
        return data

    @staticmethod
    def _save_db(data: dict[str, Any]) -> None:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ROULETTE_DOC_KEY, data)
            return

        with open(ROULETTE_FILE, "w", encoding="utf-8") as file_obj:
            json.dump(data, file_obj, ensure_ascii=False, indent=2)

    @staticmethod
    def create_game(
        created_by: int,
        title: str,
        max_participants: int,
        conditions_text: str,
        required_channel: str | None = None,
        prize_per_winner: float = 0.0,
        description: str | None = None,
        winners_count: int = 0,
        prizes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        db = RouletteService._load_db()
        game_id = int(db.get("next_id", 1))
        db["next_id"] = game_id + 1

        description_text = str(description if description is not None else conditions_text).strip()
        normalized_prizes = []
        for idx, item in enumerate(prizes or [], start=1):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "item")
            try:
                amount = float(item.get("amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            label = str(item.get("label") or "").strip() or ("приз" if kind != "money" else f"{amount:.0f} руб")
            normalized_prizes.append(
                {
                    "place": int(item.get("place", idx) or idx),
                    "kind": kind,
                    "amount": amount,
                    "label": label,
                }
            )
        if not normalized_prizes and float(prize_per_winner or 0.0) > 0:
            count = max(1, int(winners_count or 1))
            amount = float(prize_per_winner)
            label = f"{int(amount)} руб" if amount == int(amount) else f"{amount:.2f} руб"
            normalized_prizes = [
                {"place": idx, "kind": "money", "amount": amount, "label": label}
                for idx in range(1, count + 1)
            ]
        winners_count = int(winners_count or len(normalized_prizes) or 1)

        game = {
            "id": game_id,
            "created_by": int(created_by),
            "created_at": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
            "title": str(title).strip(),
            "max_participants": int(max_participants),
            "description": description_text,
            "conditions_text": description_text,
            "required_channel": str(required_channel).strip() if required_channel else None,
            "prize_per_winner": float(prize_per_winner or 0.0),
            "winners_count": winners_count,
            "prizes": normalized_prizes,
            "status": "active",
            "participants": [],
            "winners": [],
            "place_winners": [],
            "paid_winner_ids": [],
            "payouts": [],
            "product_winner_id": None,
            "money_winners": [],
            "result_announced": False,
            "announced_at": None,
            "result_delivered_user_ids": [],
        }

        db["games"].append(RouletteService._normalize_game(game))
        RouletteService._save_db(db)
        return game

    @staticmethod
    def get_game(game_id: int) -> dict[str, Any] | None:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) == int(game_id):
                return game
        return None

    @staticmethod
    def delete_game(game_id: int) -> bool:
        db = RouletteService._load_db()
        games = db.get("games", [])
        before = len(games)
        db["games"] = [game for game in games if int(game.get("id", 0)) != int(game_id)]
        changed = len(db["games"]) != before
        if changed:
            RouletteService._save_db(db)
        return changed

    @staticmethod
    def get_all_games(include_closed: bool = True) -> list[dict[str, Any]]:
        db = RouletteService._load_db()
        games = db.get("games", [])
        if not include_closed:
            games = [game for game in games if game.get("status") == "active"]
        return sorted(games, key=lambda item: int(item.get("id", 0)), reverse=True)

    @staticmethod
    def get_active_games() -> list[dict[str, Any]]:
        return RouletteService.get_all_games(include_closed=False)

    @staticmethod
    def close_game(game_id: int) -> bool:
        db = RouletteService._load_db()
        changed = False
        for game in db.get("games", []):
            if int(game.get("id", 0)) == int(game_id):
                game["status"] = "closed"
                changed = True
                break
        if changed:
            RouletteService._save_db(db)
        return changed

    @staticmethod
    def mark_result_announced(game_id: int) -> bool:
        db = RouletteService._load_db()
        changed = False
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue
            game["result_announced"] = True
            game["announced_at"] = get_now_msk().strftime("%Y-%m-%d %H:%M:%S")
            changed = True
            break
        if changed:
            RouletteService._save_db(db)
        return changed

    @staticmethod
    def mark_result_delivered(game_id: int, user_id: int) -> bool:
        db = RouletteService._load_db()
        changed = False
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue
            delivered = game.setdefault("result_delivered_user_ids", [])
            uid = int(user_id)
            if uid not in [int(item) for item in delivered]:
                delivered.append(uid)
                changed = True
            break
        if changed:
            RouletteService._save_db(db)
        return changed

    @staticmethod
    def get_pending_results_for_user(user_id: int) -> list[dict[str, Any]]:
        uid = int(user_id)
        db = RouletteService._load_db()
        result: list[dict[str, Any]] = []
        for game in db.get("games", []):
            if game.get("status") != "closed":
                continue
            if not game.get("result_announced"):
                continue

            participants = game.get("participants", [])
            if not any(int(item.get("user_id", 0) or 0) == uid for item in participants):
                continue

            delivered = {int(item) for item in game.get("result_delivered_user_ids", [])}
            if uid in delivered:
                continue

            result.append(game)

        return sorted(result, key=lambda item: int(item.get("id", 0)), reverse=True)

    @staticmethod
    def reset_result_announced(game_id: int) -> bool:
        db = RouletteService._load_db()
        changed = False
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue
            game["result_announced"] = False
            game["announced_at"] = None
            game["result_delivered_user_ids"] = []
            changed = True
            break
        if changed:
            RouletteService._save_db(db)
        return changed

    @staticmethod
    def reopen_game(game_id: int) -> bool:
        db = RouletteService._load_db()
        changed = False
        for game in db.get("games", []):
            if int(game.get("id", 0)) == int(game_id):
                game["status"] = "active"
                game["result_announced"] = False
                game["announced_at"] = None
                game["result_delivered_user_ids"] = []
                changed = True
                break
        if changed:
            RouletteService._save_db(db)
        return changed

    @staticmethod
    def add_participant(
        game_id: int,
        user_id: int,
        username: str | None,
        full_name: str | None,
        bot_id=None,
    ) -> tuple[bool, str]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            if game.get("status") != "active":
                return False, "Этот розыгрыш Store уже закрыт."

            participants = game.setdefault("participants", [])
            if any(int(p.get("user_id", 0)) == int(user_id) for p in participants):
                return False, "Вы уже в этом розыгрыше Store."

            limit = int(game.get("max_participants", 0) or 0)
            if limit > 0 and len(participants) >= limit:
                return False, "Места в этом розыгрыше уже заняты."

            participants.append(
                {
                    "user_id": int(user_id),
                    "username": username or None,
                    "full_name": full_name or "Участник",
                    "joined_at": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
                    "joined_bot_id": str(bot_id) if bot_id else None,
                }
            )

            RouletteService._save_db(db)
            return True, "Вы успешно записаны в игру."

        return False, "Игра не найдена."

    @staticmethod
    def set_winners(game_id: int, winner_ids: list[int]) -> tuple[bool, str]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            participant_ids = {int(item.get("user_id", 0)) for item in game.get("participants", [])}
            valid_winners = []
            for raw_uid in winner_ids:
                uid = int(raw_uid)
                if uid in participant_ids and uid not in valid_winners:
                    valid_winners.append(uid)

            game["winners"] = valid_winners
            game["result_announced"] = False
            game["announced_at"] = None
            game["result_delivered_user_ids"] = []
            RouletteService._save_db(db)
            return True, "Победители обновлены."

        return False, "Игра не найдена."

    @staticmethod
    def assign_place_winner(game_id: int, place: int, user_id: int) -> tuple[bool, str]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            prizes = RouletteService.prizes_of(game)
            prize = next((item for item in prizes if int(item.get("place", 0) or 0) == int(place)), None)
            if prize is None:
                return False, "Такого места в этой игре нет."

            uid = int(user_id)
            participant_ids = {int(item.get("user_id", 0) or 0) for item in game.get("participants") or []}
            if uid not in participant_ids:
                return False, "Этот пользователь не участвует в игре."

            place_winners = [
                item
                for item in RouletteService.place_winners_of(game)
                if int(item.get("place", 0) or 0) != int(place)
            ]
            if any(int(item.get("user_id", 0) or 0) == uid for item in place_winners):
                return False, "Этот участник уже назначен на другое место."

            place_winners.append(
                {
                    "place": int(place),
                    "user_id": uid,
                    "kind": str(prize.get("kind") or "item"),
                    "amount": float(prize.get("amount", 0.0) or 0.0),
                    "label": str(prize.get("label") or "приз"),
                    "paid": False,
                }
            )
            place_winners.sort(key=lambda item: int(item.get("place", 0) or 0))
            game["place_winners"] = place_winners
            game["winners"] = [int(item.get("user_id", 0) or 0) for item in place_winners]
            game["money_winners"] = [
                {
                    "place": int(item.get("place", 0) or 0),
                    "user_id": int(item.get("user_id", 0) or 0),
                    "amount": float(item.get("amount", 0.0) or 0.0),
                    "paid": bool(item.get("paid")),
                }
                for item in place_winners
                if str(item.get("kind") or "") == "money"
            ]
            game["result_announced"] = False
            game["announced_at"] = None
            game["result_delivered_user_ids"] = []
            RouletteService._save_db(db)
            return True, "Победитель места сохранён."

        return False, "Игра не найдена."

    @staticmethod
    def apply_finish_payouts(game_id: int) -> tuple[bool, str, dict[str, Any]]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            place_winners = RouletteService.place_winners_of(game)
            if not place_winners:
                return False, "Сначала назначьте победителей по местам.", {"credited": 0, "total": 0.0}

            participants_map = {
                int(item.get("user_id", 0)): item
                for item in game.get("participants", [])
                if int(item.get("user_id", 0) or 0) > 0
            }

            credited = 0
            total = 0.0
            paid_ids = {int(item) for item in game.get("paid_winner_ids") or []}
            payout_user_ids = []
            stored_places = []
            for winner in place_winners:
                entry = dict(winner)
                uid = int(entry.get("user_id", 0) or 0)
                kind = str(entry.get("kind") or "item")
                try:
                    amount = float(entry.get("amount", 0.0) or 0.0)
                except (TypeError, ValueError):
                    amount = 0.0
                if kind == "money" and amount > 0 and uid > 0 and not entry.get("paid"):
                    participant = participants_map.get(uid, {})
                    username = participant.get("username") if isinstance(participant, dict) else None
                    UserService.add_user(uid, username)
                    UserService.add_balance(uid, amount)
                    entry["paid"] = True
                    credited += 1
                    total += amount
                    paid_ids.add(uid)
                    payout_user_ids.append(uid)
                    game.setdefault("payouts", []).append(
                        {
                            "user_id": uid,
                            "amount": amount,
                            "place": int(entry.get("place", 0) or 0),
                            "type": "place_prize",
                            "created_at": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                stored_places.append(entry)

            game["place_winners"] = stored_places
            game["paid_winner_ids"] = sorted(paid_ids)
            game["money_winners"] = [
                {
                    "place": int(item.get("place", 0) or 0),
                    "user_id": int(item.get("user_id", 0) or 0),
                    "amount": float(item.get("amount", 0.0) or 0.0),
                    "paid": bool(item.get("paid")),
                }
                for item in stored_places
                if str(item.get("kind") or "") == "money"
            ]
            RouletteService._save_db(db)
            return True, "Выплаты начислены.", {"credited": credited, "total": total, "user_ids": payout_user_ids}

        return False, "Игра не найдена.", {"credited": 0, "total": 0.0}

    @staticmethod
    def get_games_pending_delivery() -> list[dict[str, Any]]:
        db = RouletteService._load_db()
        result: list[dict[str, Any]] = []
        for game in db.get("games", []):
            if game.get("status") != "closed":
                continue
            if not game.get("result_announced"):
                continue
            delivered = {int(item) for item in game.get("result_delivered_user_ids") or []}
            pending = [
                item
                for item in game.get("participants") or []
                if int(item.get("user_id", 0) or 0) > 0
                and int(item.get("user_id", 0) or 0) not in delivered
            ]
            if pending:
                result.append(game)
        return result

    @staticmethod
    def draw_with_fixed_and_random(
        game_id: int,
        fixed_winner_id: int | None,
        random_winners_count: int,
    ) -> tuple[bool, str, list[int]]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            participants = game.get("participants", [])
            participant_ids = [int(item.get("user_id", 0)) for item in participants if int(item.get("user_id", 0)) > 0]
            if not participant_ids:
                return False, "Нет участников для розыгрыша.", []

            winners: list[int] = []
            if fixed_winner_id is not None:
                fixed_winner_id = int(fixed_winner_id)
                if fixed_winner_id not in participant_ids:
                    return False, "Ваш победитель не найден среди участников.", []
                winners.append(fixed_winner_id)

            random_count = max(0, int(random_winners_count))
            pool = [uid for uid in participant_ids if uid not in winners]
            if random_count > len(pool):
                return False, "Слишком много случайных победителей для текущего списка.", []

            if random_count > 0:
                winners.extend(random.sample(pool, random_count))

            game["winners"] = winners
            RouletteService._save_db(db)
            return True, "Победители определены.", winners

        return False, "Игра не найдена.", []

    @staticmethod
    def draw_money_winners_by_places(
        game_id: int,
        product_winner_id: int | None,
        place_prizes: list[float],
    ) -> tuple[bool, str, dict[str, Any]]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            participants = game.get("participants", [])
            participant_ids = [int(item.get("user_id", 0)) for item in participants if int(item.get("user_id", 0)) > 0]
            if not participant_ids:
                return False, "Нет участников для розыгрыша.", {}

            normalized_prizes: list[float] = []
            for raw in place_prizes:
                try:
                    amount = float(raw)
                except (TypeError, ValueError):
                    return False, "Суммы призов должны быть числами.", {}
                if amount <= 0:
                    return False, "Каждый приз должен быть больше 0.", {}
                normalized_prizes.append(amount)

            if not normalized_prizes:
                return False, "Добавьте хотя бы один денежный приз.", {}

            fixed_id: int | None = None
            if product_winner_id is not None:
                fixed_id = int(product_winner_id)
                if fixed_id not in participant_ids:
                    return False, "Победитель товара не найден среди участников.", {}

            pool = [uid for uid in participant_ids if uid != fixed_id]
            if len(normalized_prizes) > len(pool):
                return False, "Недостаточно участников для заданного количества денежных мест.", {}

            selected_money_ids = random.sample(pool, len(normalized_prizes))
            money_winners = []
            for idx, uid in enumerate(selected_money_ids, start=1):
                money_winners.append(
                    {
                        "place": idx,
                        "user_id": uid,
                        "amount": normalized_prizes[idx - 1],
                        "paid": False,
                    }
                )

            game["product_winner_id"] = fixed_id
            game["money_winners"] = money_winners
            combined_winners = ([fixed_id] if fixed_id is not None else []) + [item["user_id"] for item in money_winners]
            game["winners"] = combined_winners
            game["result_announced"] = False
            game["announced_at"] = None
            game["result_delivered_user_ids"] = []
            RouletteService._save_db(db)

            return True, "Розыгрыш по местам выполнен.", {
                "product_winner_id": fixed_id,
                "money_winners": money_winners,
            }

        return False, "Игра не найдена.", {}

    @staticmethod
    def apply_money_prize_payout(game_id: int) -> tuple[bool, str, dict[str, Any]]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            money_winners = game.get("money_winners", [])
            if not money_winners:
                return False, "Сначала проведите денежный розыгрыш по местам.", {"credited": 0, "total": 0.0}

            participants_map = {
                int(item.get("user_id", 0)): item
                for item in game.get("participants", [])
                if int(item.get("user_id", 0)) > 0
            }

            credited = 0
            total = 0.0
            for winner in money_winners:
                if winner.get("paid"):
                    continue

                uid = int(winner.get("user_id", 0) or 0)
                if uid <= 0:
                    continue

                try:
                    amount = float(winner.get("amount", 0.0) or 0.0)
                except (TypeError, ValueError):
                    amount = 0.0
                if amount <= 0:
                    continue

                participant = participants_map.get(uid, {})
                username = participant.get("username") if isinstance(participant, dict) else None
                UserService.add_user(uid, username)
                UserService.add_balance(uid, amount)

                winner["paid"] = True
                credited += 1
                total += amount
                game.setdefault("payouts", []).append(
                    {
                        "user_id": uid,
                        "amount": amount,
                        "place": int(winner.get("place", 0) or 0),
                        "type": "money_place",
                        "created_at": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

            RouletteService._save_db(db)
            return True, "Выплата по местам выполнена.", {"credited": credited, "total": total}

        return False, "Игра не найдена.", {"credited": 0, "total": 0.0}

    @staticmethod
    def apply_prize_payout(game_id: int, prize_per_winner: float | None = None) -> tuple[bool, str, dict[str, Any]]:
        db = RouletteService._load_db()
        for game in db.get("games", []):
            if int(game.get("id", 0)) != int(game_id):
                continue

            winners = [int(item) for item in game.get("winners", [])]
            if not winners:
                return False, "Сначала назначьте победителей.", {"credited": 0, "skipped": 0, "amount": 0.0}

            amount = game.get("prize_per_winner", 0.0) if prize_per_winner is None else prize_per_winner
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                amount = 0.0
            if amount <= 0:
                return False, "Размер приза должен быть больше 0.", {"credited": 0, "skipped": 0, "amount": 0.0}

            paid_ids = {int(item) for item in game.get("paid_winner_ids", [])}
            participants_map = {
                int(item.get("user_id", 0)): item
                for item in game.get("participants", [])
                if int(item.get("user_id", 0)) > 0
            }

            credited = 0
            skipped = 0
            for uid in winners:
                if uid in paid_ids:
                    skipped += 1
                    continue

                participant = participants_map.get(uid, {})
                username = participant.get("username") if isinstance(participant, dict) else None
                UserService.add_user(uid, username)
                UserService.add_balance(uid, amount)

                credited += 1
                paid_ids.add(uid)
                game.setdefault("payouts", []).append(
                    {
                        "user_id": uid,
                        "amount": amount,
                        "created_at": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

            game["paid_winner_ids"] = sorted(list(paid_ids))
            game["prize_per_winner"] = amount
            RouletteService._save_db(db)
            return True, "Выплата выполнена.", {"credited": credited, "skipped": skipped, "amount": amount}

        return False, "Игра не найдена.", {"credited": 0, "skipped": 0, "amount": 0.0}

    @staticmethod
    def format_participant_name(item: dict[str, Any]) -> str:
        username = (item.get("username") or "").strip()
        full_name = (item.get("full_name") or "").strip() or "Участник"
        user_id = int(item.get("user_id", 0) or 0)

        if username:
            return f"@{username}"
        return f"{full_name} (ID: {user_id})"

    @staticmethod
    def format_public_game_text(game: dict[str, Any]) -> str:
        participants_count = len(game.get("participants") or [])
        max_participants = int(game.get("max_participants", 0) or 0)
        status_text = "🟢 Набор открыт" if game.get("status") == "active" else "⚪️ Набор закрыт"
        if game.get("status") == "active" and max_participants > 0 and participants_count >= max_participants:
            status_text = "🟡 Места заняты, ждем розыгрыш Store"

        description = str(game.get("description") or game.get("conditions_text") or "").strip() or "Без описания"
        lines = [
            f"🎲 <b>Розыгрыш Store · {html.escape(str(game.get('title') or 'Игра'))}</b>",
            status_text,
            f"Игроков: <b>{participants_count}/{max_participants}</b>",
            "",
            html.escape(description),
        ]
        prizes = RouletteService.prizes_of(game)
        if prizes:
            lines.extend(["", "🏆 Призы:"])
            for prize in prizes:
                place = int(prize.get("place", 0) or 0)
                lines.append(f"{place} место - {html.escape(str(prize.get('label') or 'приз'))}")
        channel = game.get("required_channel")
        if channel:
            lines.extend(["", f"Нужна подписка: <code>{html.escape(str(channel))}</code>"])
        if game.get("result_announced"):
            lines.extend(["", "🏁 Розыгрыш завершён. Нажмите «Результаты»."])
        elif game.get("status") != "active":
            lines.extend(["", "Розыгрыш скоро будет объявлен."])
        return "\n".join(lines)

    @staticmethod
    def format_public_participants_text(game: dict[str, Any], *, show_winners: bool = False) -> str:
        participants = game.get("participants") or []
        if not participants:
            return "👥 <b>Список игроков Store</b>\n\nПока никто не записался."

        lines = [
            f"👥 <b>Игроки розыгрыша</b>",
            f"{html.escape(str(game.get('title') or 'Игра'))}",
            f"Всего: <b>{len(participants)}</b> / {int(game.get('max_participants', 0) or 0)}",
            "",
        ]
        winner_ids = set()
        if show_winners and game.get("result_announced"):
            winner_ids = {int(item.get("user_id", 0) or 0) for item in RouletteService.place_winners_of(game)}
        limit = 80
        for idx, item in enumerate(participants[:limit], start=1):
            uid = int(item.get("user_id", 0) or 0)
            mark = "🏆 " if uid in winner_ids else ""
            lines.append(f"{idx}. {mark}{html.escape(RouletteService.format_participant_name(item))}")
        if len(participants) > limit:
            lines.append(f"… и ещё {len(participants) - limit}")
        return "\n".join(lines)

    @staticmethod
    def format_public_results_text(game: dict[str, Any]) -> str:
        if not game.get("result_announced"):
            return (
                "🏆 <b>Итоги розыгрыша Store</b>\n\n"
                "Розыгрыш ещё не проведён. Для всех участников он пройдёт одинаково."
            )

        participants_map = {
            int(item.get("user_id", 0)): item
            for item in game.get("participants") or []
            if int(item.get("user_id", 0) or 0) > 0
        }
        lines = [
            "🎉 <b>Итоги розыгрыша Store</b>",
            f"Игра: <b>{html.escape(str(game.get('title') or 'Игра'))}</b>",
            "",
            "🏆 Победители:",
        ]
        places = RouletteService.place_winners_of(game)
        if not places:
            lines.append("Победители не определены.")
        else:
            for item in places:
                uid = int(item.get("user_id", 0) or 0)
                name = RouletteService.format_participant_name(participants_map.get(uid, {"user_id": uid}))
                place = int(item.get("place", 0) or 0)
                label = str(item.get("label") or "приз")
                prefix = f"{place} место" if place > 0 else "Главный приз"
                lines.append(f"{prefix}: <b>{html.escape(name)}</b> - {html.escape(label)}")
        lines.extend(["", "Спасибо всем за участие."])
        return "\n".join(lines)
