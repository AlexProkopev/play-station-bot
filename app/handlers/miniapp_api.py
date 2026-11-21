"""
Mini App API endpoints for the Telegram Web App.
Handles user data, catalog, orders, games, etc.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.parse
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher

from app.config import BOT_TOKEN
from app.services.users import UserService
from app.services.catalog import CatalogService
from app.services.geo import GeoService
from app.services.roulette import RouletteService

logger = logging.getLogger("app.handlers.miniapp_api")


def validate_init_data(init_data: str) -> dict | None:
    """
    Validate Telegram Mini App initData and extract user info.
    Returns user dict or None if validation fails.
    """
    if not init_data:
        return None
    
    try:
        # Parse the init data
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        
        # Get the hash
        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return None
        
        # Sort and create data-check-string
        items = sorted(parsed.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in items)
        
        # Create secret key
        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()
        
        # Calculate hash
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(calculated_hash, received_hash):
            logger.warning("Mini App: Invalid hash")
            return None
        
        # Extract user
        user_json = parsed.get("user", "{}")
        user = json.loads(user_json)
        return user
    except Exception as e:
        logger.exception("Mini App validation error: %s", e)
        return None


def api_response(data: Any = None, error: str | None = None, status: int = 200) -> web.Response:
    """Helper to create JSON API responses."""
    body = {"ok": error is None}
    if data is not None:
        body["data"] = data
    if error:
        body["error"] = error
    return web.json_response(body, status=status)


async def get_user(request: web.Request) -> web.Response:
    """GET /api/user - Get current user data."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    # Allow dev mode without validation
    if not tg_user:
        if init_data:
            return api_response(error="Invalid init data", status=401)
        # Dev mode - return mock
        return api_response({
            "id": 123456789,
            "username": "dev_user",
            "balance": 1500.0,
            "purchases_count": 5,
            "discount_percent": 0,
            "discount_purchases_left": 0,
            "spin_games": 3,
            "spin_winnings": 500.0,
            "joined": "2024-01-01 12:00:00",
        })
    
    user_id = tg_user.get("id")
    if not user_id:
        return api_response(error="No user ID", status=400)
    
    # Get or create user
    UserService.add_user(user_id, tg_user.get("username"))
    user = UserService.get_user(user_id) or {}
    
    # Get game stats
    spin_played, spin_won = UserService.get_spin_stats(user_id)
    roulette_played, roulette_won = RouletteService.user_stats(user_id)
    
    return api_response({
        "id": user_id,
        "username": tg_user.get("username") or user.get("username"),
        "balance": float(user.get("balance") or 0),
        "purchases_count": int(user.get("purchases_count") or 0),
        "discount_percent": int(user.get("discount_percent") or 0),
        "discount_purchases_left": int(user.get("discount_purchases_left") or 0),
        "spin_games": spin_played + roulette_played,
        "spin_winnings": spin_won + roulette_won,
        "joined": user.get("joined", ""),
    })


async def get_cities(request: web.Request) -> web.Response:
    """GET /api/cities - Get list of available cities."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    # Get cities from GeoService
    try:
        cities_data = GeoService.get_all_cities()
        cities = []
        
        for city_key, city_info in cities_data.items():
            if not isinstance(city_info, dict):
                continue
            name = city_info.get("name") or city_key
            
            # Count products for this city
            products = CatalogService.get_products_by_city(city_key)
            products_count = len(products) if products else 0
            
            if products_count > 0:
                cities.append({
                    "id": city_key,
                    "name": name,
                    "products_count": products_count,
                })
        
        # Sort by product count
        cities.sort(key=lambda c: c["products_count"], reverse=True)
        return api_response(cities)
    except Exception as e:
        logger.exception("Error getting cities: %s", e)
        return api_response(error=str(e), status=500)


async def get_products(request: web.Request) -> web.Response:
    """GET /api/products - Get products, optionally filtered by city."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    city_id = request.query.get("city")
    
    try:
        if city_id:
            products_data = CatalogService.get_products_by_city(city_id)
        else:
            products_data = CatalogService.get_all_products()
        
        products = []
        for product_id, product_info in (products_data or {}).items():
            if not isinstance(product_info, dict):
                continue
            
            price = float(product_info.get("price") or 0)
            original_price = float(product_info.get("original_price") or price)
            
            city_key = product_info.get("city") or city_id or ""
            city_name = GeoService.get_city_name(city_key) if city_key else ""
            
            products.append({
                "id": product_id,
                "name": product_info.get("name") or product_id,
                "city": city_key,
                "city_name": city_name,
                "price": price,
                "original_price": original_price if original_price > price else None,
                "weight": product_info.get("weight"),
                "description": product_info.get("description"),
                "available": bool(product_info.get("available", True)),
            })
        
        return api_response(products)
    except Exception as e:
        logger.exception("Error getting products: %s", e)
        return api_response(error=str(e), status=500)


async def get_product(request: web.Request) -> web.Response:
    """GET /api/products/{product_id} - Get single product details."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    product_id = request.match_info.get("product_id", "")
    
    try:
        product_info = CatalogService.get_product(product_id)
        if not product_info:
            return api_response(error="Product not found", status=404)
        
        price = float(product_info.get("price") or 0)
        original_price = float(product_info.get("original_price") or price)
        city_key = product_info.get("city") or ""
        city_name = GeoService.get_city_name(city_key) if city_key else ""
        
        # Get districts for this product
        districts = []
        districts_data = product_info.get("districts") or {}
        for dist_id, dist_info in districts_data.items():
            if not isinstance(dist_info, dict):
                continue
            stash_types = []
            for st_id, st_info in (dist_info.get("stash_types") or {}).items():
                stash_types.append({
                    "id": st_id,
                    "name": st_info.get("name") or st_id if isinstance(st_info, dict) else st_id,
                    "available": bool(st_info.get("available", True)) if isinstance(st_info, dict) else True,
                })
            districts.append({
                "id": dist_id,
                "name": dist_info.get("name") or dist_id,
                "stash_types": stash_types,
            })
        
        return api_response({
            "id": product_id,
            "name": product_info.get("name") or product_id,
            "city": city_key,
            "city_name": city_name,
            "price": price,
            "original_price": original_price if original_price > price else None,
            "weight": product_info.get("weight"),
            "description": product_info.get("description"),
            "available": bool(product_info.get("available", True)),
            "districts": districts,
        })
    except Exception as e:
        logger.exception("Error getting product: %s", e)
        return api_response(error=str(e), status=500)


async def get_districts(request: web.Request) -> web.Response:
    """GET /api/products/{product_id}/districts - Get districts for a product."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    product_id = request.match_info.get("product_id", "")
    
    try:
        product_info = CatalogService.get_product(product_id)
        if not product_info:
            return api_response(error="Product not found", status=404)
        
        districts = []
        districts_data = product_info.get("districts") or {}
        for dist_id, dist_info in districts_data.items():
            if not isinstance(dist_info, dict):
                continue
            stash_types = []
            for st_id, st_info in (dist_info.get("stash_types") or {}).items():
                stash_types.append({
                    "id": st_id,
                    "name": st_info.get("name") or st_id if isinstance(st_info, dict) else st_id,
                    "available": bool(st_info.get("available", True)) if isinstance(st_info, dict) else True,
                })
            districts.append({
                "id": dist_id,
                "name": dist_info.get("name") or dist_id,
                "stash_types": stash_types,
            })
        
        return api_response(districts)
    except Exception as e:
        logger.exception("Error getting districts: %s", e)
        return api_response(error=str(e), status=500)


async def create_order(request: web.Request) -> web.Response:
    """POST /api/orders - Create a new order."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    if not tg_user:
        return api_response(error="Unauthorized", status=401)
    
    user_id = tg_user.get("id")
    if not user_id:
        return api_response(error="No user ID", status=400)
    
    try:
        body = await request.json()
    except Exception:
        return api_response(error="Invalid JSON", status=400)
    
    product_id = body.get("product_id")
    district_id = body.get("district_id")
    stash_type = body.get("stash_type")
    
    if not all([product_id, district_id, stash_type]):
        return api_response(error="Missing required fields", status=400)
    
    try:
        # Get product and check availability
        product = CatalogService.get_product(product_id)
        if not product:
            return api_response(error="Product not found", status=404)
        
        price = float(product.get("price") or 0)
        
        # Check balance
        balance = UserService.get_balance(user_id)
        if balance < price:
            return api_response(error="Insufficient balance", status=400)
        
        # Deduct balance
        if not UserService.deduct_balance(user_id, price):
            return api_response(error="Failed to deduct balance", status=500)
        
        # Create order record
        import uuid
        order_id = str(uuid.uuid4())[:8]
        
        # TODO: Actually process the order through the existing order flow
        # For now, just record it and notify admin
        from app.services.referrals import complete_product_purchase, notify_referrer_bonus
        from app.services.loyalty import notify_purchase_loyalty
        bonus_payload = complete_product_purchase(
            user_id,
            price,
            title=str(product.get("name") or ""),
        )
        
        # Get bot instance and send notification
        bot: Bot = request.app.get("bot")
        if bot:
            await notify_referrer_bonus(bot, bonus_payload)
            await notify_purchase_loyalty(bot, user_id, bonus_payload)
            from app.config import ADMIN_IDS
            admin_text = (
                f"📱 <b>Заказ из Mini App</b>\n\n"
                f"👤 User: {user_id} (@{tg_user.get('username', 'N/A')})\n"
                f"📦 Товар: {product.get('name')}\n"
                f"📍 Район: {district_id}\n"
                f"📦 Тип: {stash_type}\n"
                f"💰 Сумма: {price} ₽\n"
                f"🆔 Order: {order_id}"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(int(admin_id), admin_text)
                except Exception:
                    pass
        
        return api_response({
            "order_id": order_id,
            "success": True,
        })
    except Exception as e:
        logger.exception("Error creating order: %s", e)
        return api_response(error=str(e), status=500)


async def get_orders(request: web.Request) -> web.Response:
    """GET /api/orders - Get user's order history."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    if not tg_user:
        return api_response(error="Unauthorized", status=401)
    
    # TODO: Implement order history from actual storage
    # For now return empty list
    return api_response([])


async def get_games(request: web.Request) -> web.Response:
    """GET /api/games - Get available games/giveaways."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    user_id = tg_user.get("id") if tg_user else None
    
    try:
        games_data = RouletteService.get_all_games()
        games = []
        
        for game in games_data:
            if not isinstance(game, dict):
                continue
            
            game_id = int(game.get("id") or 0)
            participants = game.get("participants") or []
            
            # Check if user joined
            joined = False
            if user_id:
                joined = RouletteService.is_participant(game_id, user_id)
            
            # Get prizes
            prizes = []
            for prize in RouletteService.prizes_of(game):
                prizes.append({
                    "place": int(prize.get("place") or 0),
                    "kind": prize.get("kind", "money"),
                    "amount": prize.get("amount"),
                    "label": prize.get("label"),
                })
            
            games.append({
                "id": game_id,
                "title": game.get("title") or f"Розыгрыш #{game_id}",
                "description": game.get("description"),
                "max_participants": int(game.get("max_participants") or 100),
                "participants_count": len(participants),
                "winners_count": int(game.get("winners_count") or 1),
                "prizes": prizes,
                "status": "finished" if game.get("result_announced") else "active",
                "result_announced": bool(game.get("result_announced")),
                "joined": joined,
            })
        
        return api_response(games)
    except Exception as e:
        logger.exception("Error getting games: %s", e)
        return api_response(error=str(e), status=500)


async def join_game(request: web.Request) -> web.Response:
    """POST /api/games/{game_id}/join - Join a game."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    if not tg_user:
        return api_response(error="Unauthorized", status=401)
    
    user_id = tg_user.get("id")
    username = tg_user.get("username")
    game_id = request.match_info.get("game_id", "")
    
    try:
        game_id_int = int(game_id)
    except ValueError:
        return api_response(error="Invalid game ID", status=400)
    
    try:
        # Get bot ID for tracking
        bot: Bot = request.app.get("bot")
        bot_id = getattr(bot, "id", None) if bot else None
        
        success = RouletteService.add_participant(game_id_int, user_id, username, bot_id=bot_id)
        if not success:
            return api_response(error="Could not join game", status=400)
        
        return api_response({"success": True})
    except Exception as e:
        logger.exception("Error joining game: %s", e)
        return api_response(error=str(e), status=500)


async def get_game_participants(request: web.Request) -> web.Response:
    """GET /api/games/{game_id}/participants - Get game participants."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    game_id = request.match_info.get("game_id", "")
    
    try:
        game_id_int = int(game_id)
    except ValueError:
        return api_response(error="Invalid game ID", status=400)
    
    try:
        game = RouletteService.get_game(game_id_int)
        if not game:
            return api_response(error="Game not found", status=404)
        
        participants = []
        for p in game.get("participants") or []:
            username = p.get("username")
            if username:
                # Mask username for privacy
                if len(username) > 3:
                    masked = username[:2] + "*" * (len(username) - 3) + username[-1]
                else:
                    masked = username[0] + "*" * (len(username) - 1)
                participants.append({"username": masked})
        
        return api_response(participants)
    except Exception as e:
        logger.exception("Error getting participants: %s", e)
        return api_response(error=str(e), status=500)


async def topup(request: web.Request) -> web.Response:
    """POST /api/topup - Create top-up request."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data)
    
    if not tg_user:
        return api_response(error="Unauthorized", status=401)
    
    user_id = tg_user.get("id")
    
    try:
        body = await request.json()
    except Exception:
        return api_response(error="Invalid JSON", status=400)
    
    amount = float(body.get("amount") or 0)
    method = body.get("method", "card")
    
    if amount < 100:
        return api_response(error="Minimum amount is 100", status=400)
    
    # TODO: Integrate with actual payment flow
    # For now, notify admin
    bot: Bot = request.app.get("bot")
    if bot:
        from app.config import ADMIN_IDS
        admin_text = (
            f"💳 <b>Запрос на пополнение из Mini App</b>\n\n"
            f"👤 User: {user_id} (@{tg_user.get('username', 'N/A')})\n"
            f"💰 Сумма: {amount} ₽\n"
            f"💳 Способ: {method}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(int(admin_id), admin_text)
            except Exception:
                pass
    
    return api_response({"success": True})


def register_miniapp_routes(app: web.Application) -> None:
    """Register all Mini App API routes."""
    # CORS middleware for development
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
        return response
    
    app.middlewares.append(cors_middleware)
    
    # API routes
    app.router.add_get("/api/user", get_user)
    app.router.add_get("/api/cities", get_cities)
    app.router.add_get("/api/products", get_products)
    app.router.add_get("/api/products/{product_id}", get_product)
    app.router.add_get("/api/products/{product_id}/districts", get_districts)
    app.router.add_post("/api/orders", create_order)
    app.router.add_get("/api/orders", get_orders)
    app.router.add_get("/api/games", get_games)
    app.router.add_post("/api/games/{game_id}/join", join_game)
    app.router.add_get("/api/games/{game_id}/participants", get_game_participants)
    app.router.add_post("/api/topup", topup)
    
    # Handle OPTIONS for CORS preflight
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())
