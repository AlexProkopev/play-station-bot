from __future__ import annotations

import random
from typing import Any

from app.services.users import UserService

WIN_CHANCE = 0.005
MIN_STAKE = 200.0
CHANCE_LABEL = "0.5%"


class ProductSpinService:
    @staticmethod
    def stake_for_product(product: dict[str, Any] | None) -> float:
        if not isinstance(product, dict):
            return 0.0
        raw = product.get("original_price")
        if raw is None:
            raw = product.get("price")
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def can_play(product: dict[str, Any] | None) -> tuple[bool, str]:
        stake = ProductSpinService.stake_for_product(product)
        if stake < MIN_STAKE:
            return False, f"Ставки от {MIN_STAKE:.0f} ₽. Этот товар дешевле - выберите другой."
        return True, ""

    @staticmethod
    def play(user_id: int, stake: float, *, rng: random.Random | None = None) -> dict[str, Any]:
        amount = float(stake)
        if amount < MIN_STAKE:
            return {"ok": False, "error": "min_stake", "stake": amount}
        if not UserService.deduct_balance(int(user_id), amount):
            return {
                "ok": False,
                "error": "balance",
                "stake": amount,
                "balance": UserService.get_balance(int(user_id)),
            }

        UserService.add_spin_play(int(user_id))
        roller = rng or random.Random()
        win = roller.random() < WIN_CHANCE
        if win:
            UserService.add_spin_winnings(int(user_id), amount)
        return {
            "ok": True,
            "win": win,
            "stake": amount,
            "chance": WIN_CHANCE,
            "balance": UserService.get_balance(int(user_id)),
        }
