from __future__ import annotations

import json
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher

from app.services.payments import callback_secret, handle_callback_event
from app.services.ratecore import verify_callback_signature
from app.handlers.miniapp_api import register_miniapp_routes

logger = logging.getLogger("app.handlers.ratecore_http")


def create_http_app(bot: Bot, dispatcher: Dispatcher) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app.router.add_get("/health", _health)
    app.router.add_post("/webhooks/ratecore", _ratecore_webhook)
    
    # Register Mini App API routes
    register_miniapp_routes(app)
    
    return app


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _ratecore_webhook(request: web.Request) -> web.Response:
    raw = await request.read()
    signature = request.headers.get("X-Signature") or ""
    secret = callback_secret()
    if not verify_callback_signature(raw, signature, secret):
        logger.warning("Rejected Ratecore callback: bad signature")
        return web.json_response({"ok": False}, status=401)
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except ValueError:
        return web.json_response({"ok": False}, status=400)
    if not isinstance(envelope, dict):
        return web.json_response({"ok": False}, status=400)
    bot: Bot = request.app["bot"]
    dispatcher: Dispatcher = request.app["dispatcher"]
    try:
        await handle_callback_event(bot, dispatcher, envelope)
    except Exception:
        logger.exception("Ratecore callback handler failed")
        return web.json_response({"ok": False}, status=500)
    return web.json_response({"ok": True})


async def start_http_server(bot: Bot, dispatcher: Dispatcher, host: str, port: int) -> web.AppRunner:
    app = create_http_app(bot, dispatcher)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logging.getLogger("app.handlers.ratecore_http").info("Ratecore HTTP listening on %s:%s", host, port)
    return runner
