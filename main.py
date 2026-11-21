import asyncio
import json
import logging
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from app.config import (
    ADMIN_IDS,
    BOT_TOKEN,
    RATECORE_HTTP_HOST,
    RATECORE_HTTP_PORT,
    is_ratecore_sandbox,
    resolve_ratecore_callback_url,
)
from app.http_ssl import apply_process_ssl, windows_ssl_context
from app.concurrency import get_blocking_pool, shutdown_blocking_pool
from app.handlers import user_flow, admin, dispute
from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService
from app.services.reviews import ReviewsService
from app.services.settings import SettingsService
from app.services.broadcast import send_auto_broadcast
from datetime import datetime, timedelta
import random
from app.utils import get_now_msk, send_user_to_start, order_ttl

# Настройка логирования
logging.basicConfig(level=logging.INFO)


apply_process_ssl()


def _telegram_ssl_context() -> ssl.SSLContext:
    return windows_ssl_context()


def create_ipv4_session() -> AiohttpSession:
    """Force IPv4: aiohttp on Windows can hang on IPv6 to api.telegram.org."""
    session = AiohttpSession()
    session._connector_init["family"] = socket.AF_INET
    session._connector_init["ssl"] = _telegram_ssl_context()
    return session


def delete_webhook_sync(token: str) -> None:
    url = f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true"
    try:
        with urllib.request.urlopen(url, timeout=15, context=_telegram_ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            logging.info("deleteWebhook: %s", payload)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logging.warning("delete_webhook skipped: %s", exc)

async def order_expiration_worker(bot: Bot, dispatcher: Dispatcher):
    try:
        while True:
            try:
                now = get_now_msk()
                expired_ids = []
                for item in UserService.get_all_active_orders():
                    order = item.get("order") or {}
                    if str(order.get("type") or "") == "ratecore":
                        continue
                    timestamp = order.get("timestamp")
                    if not timestamp:
                        continue
                    try:
                        order_time = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if now - order_time > order_ttl(order):
                        try:
                            expired_ids.append((int(item["user_id"]), order))
                        except (TypeError, ValueError):
                            continue

                for uid, order in expired_ids:
                    UserService.remove_active_order(uid)
                    await send_user_to_start(
                        bot,
                        dispatcher,
                        uid,
                        "◆ <b>ТАЙМЕР</b>\n"
                        "——\n"
                        "заявка закрыта по времени\n"
                        "новый заказ — сектор Витрина",
                    )
                    order_type = str(order.get("type") or "")
                    if order_type in {"btc", "ltc", "crypto"}:
                        symbol = str(order_type).upper()
                        note = (
                            f"⏱ Крипто-заявка {symbol} истекла (1 час без оплаты).\n"
                            f"User: {uid}\n"
                            f"Товар: {order.get('product')}\n"
                            f"Сумма: {order.get('crypto_amount')} {symbol}\n"
                            f"Хеш: {order.get('tx_hash') or 'не прислан'}\n"
                            f"Заявка: {order.get('payment_request_id')}"
                        )
                        for admin_id in ADMIN_IDS:
                            try:
                                await bot.send_message(int(admin_id), f"👀 Admin Watch:\n{note}")
                            except Exception:
                                logging.exception("Failed to notify admin %s about expired crypto order", admin_id)
                    elif order_type == "yoomoney":
                        note = (
                            f"⏱ Заявка ЮMoney истекла ({order.get('ttl_minutes') or 25} мин).\n"
                            f"User: {uid}\n"
                            f"Товар: {order.get('product')}\n"
                            f"Сумма: {order.get('price')}\n"
                            f"Кошелёк: {order.get('yoomoney_wallet') or order.get('card_number')}\n"
                            f"Заявка: {order.get('payment_request_id')}"
                        )
                        for admin_id in ADMIN_IDS:
                            try:
                                await bot.send_message(int(admin_id), f"👀 Admin Watch:\n{note}")
                            except Exception:
                                logging.exception("Failed to notify admin %s about expired yoomoney order", admin_id)
            except Exception as e:
                logging.error(f"Error in expiration worker: {e}")

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("Order expiration worker stopped")
        raise


async def auto_broadcast_worker(bot: Bot):
    try:
        while True:
            try:
                now = get_now_msk()
                jobs = SettingsService.claim_due_auto_broadcasts(bot.id, now)
                for _text, _is_first, worker_id in jobs:
                    delivered, failed = await send_auto_broadcast(bot, worker_id)
                    logging.info(
                        "Auto broadcast %s sent by bot %s: delivered=%s failed=%s",
                        worker_id,
                        bot.id,
                        delivered,
                        failed,
                    )
            except Exception as exc:
                logging.error("Error in auto broadcast worker: %s", exc)

            await asyncio.sleep(20)
    except asyncio.CancelledError:
        logging.info("Auto broadcast worker stopped")
        raise


async def roulette_results_worker(bot: Bot):
    from app.services.roulette import RouletteService

    try:
        while True:
            try:
                from app.handlers.user_flow import _send_roulette_games
                for item in RouletteService.get_games_pending_delivery():
                    for participant in item.get("participants") or []:
                        user_id = int(participant.get("user_id", 0) or 0)
                        if user_id <= 0:
                            continue
                        if not RouletteService.should_deliver_on_bot(participant, getattr(bot, "id", None)):
                            continue
                        delivered = {int(value) for value in item.get("result_delivered_user_ids") or []}
                        if user_id in delivered:
                            continue
                        await bot.send_message(user_id, RouletteService.format_public_results_text(item))
                        RouletteService.mark_result_delivered(int(item.get("id", 0) or 0), user_id)
            except Exception:
                logging.exception("Error in roulette results worker")
            await asyncio.sleep(20)
    except asyncio.CancelledError:
        logging.info("Roulette results worker stopped")
        raise


async def crypto_rate_worker():
    from app.services.crypto import CryptoService

    try:
        try:
            await CryptoService.refresh_rates(force=False)
        except Exception:
            logging.exception("Initial CoinGecko rate refresh failed")

        while True:
            await asyncio.sleep(60 * 60)
            try:
                await CryptoService.refresh_rates(force=False)
            except Exception:
                logging.exception("Scheduled CoinGecko rate refresh failed")
    except asyncio.CancelledError:
        logging.info("Crypto rate worker stopped")
        raise


def warmup_hot_data() -> None:
    try:
        from app.services.geo import GeoService
        GeoService._load_cache()
        GeoService._load_population_cache()
    except Exception:
        logging.exception("Geo cache warmup failed")
    try:
        from app.services.catalog import CatalogService
        CatalogService.get_all_products()
        CatalogService._load_cache()
    except Exception:
        logging.exception("Catalog warmup failed")
    try:
        from app.services.settings import SettingsService
        SettingsService._load_settings()
    except Exception:
        logging.exception("Settings warmup failed")
    try:
        from app.services.reviews import ReviewsService
        ReviewsService._load_reviews()
    except Exception:
        logging.exception("Reviews warmup failed")


async def ratecore_poll_worker(bot: Bot, dispatcher: Dispatcher):
    from app.services.payments import poll_active_payments

    try:
        while True:
            try:
                await poll_active_payments(bot, dispatcher)
            except Exception:
                logging.exception("Ratecore poll worker failed")
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        logging.info("Ratecore poll worker stopped")
        raise


async def configure_ratecore_runtime() -> None:
    from app.services.payments import set_callback_secret
    from app.services.ratecore import RatecoreClient, RatecoreError

    callback_url = resolve_ratecore_callback_url()
    client = RatecoreClient()
    if not client.configured():
        logging.warning("Ratecore API keys are not configured")
        return
    logging.info(
        "Ratecore mode=%s prefix=%s callback=%s",
        "sandbox" if is_ratecore_sandbox() else "live",
        client.prefix,
        callback_url or "polling-only",
    )
    if is_ratecore_sandbox() and callback_url:
        try:
            await client.configure_sandbox_webhook(callback_url)
            logging.info("Sandbox Ratecore webhook set to %s", callback_url)
        except RatecoreError:
            logging.exception("Failed to configure sandbox Ratecore webhook")
        try:
            secret = await client.reveal_sandbox_callback_secret()
            if secret:
                set_callback_secret(secret)
                logging.info("Loaded sandbox Ratecore callback secret")
        except RatecoreError:
            logging.exception("Failed to reveal sandbox Ratecore callback secret")


async def main():
    if not BOT_TOKEN:
        print("Ошибка: Не задан токен бота в .env файле.")
        return

    asyncio.get_running_loop().set_default_executor(get_blocking_pool())
    PostgresDocumentStore.initialize()
    UserService.warmup()
    warmup_hot_data()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=create_ipv4_session(),
    )
    dp = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
    )
    worker_tasks = []
    http_runner = None

    try:
        worker_tasks.append(asyncio.create_task(order_expiration_worker(bot, dp)))
        worker_tasks.append(asyncio.create_task(auto_broadcast_worker(bot)))
        worker_tasks.append(asyncio.create_task(roulette_results_worker(bot)))
        worker_tasks.append(asyncio.create_task(crypto_rate_worker()))
        worker_tasks.append(asyncio.create_task(ratecore_poll_worker(bot, dp)))
        await configure_ratecore_runtime()

        # Сначала админ-команды (чтобы /clear_cache и другие работали даже из FSM-состояний)
        dp.include_router(admin.commands_router)
        dp.include_router(admin.router)
        dp.include_router(dispute.router)
        dp.include_router(user_flow.router)

        deployment_commit = (
            os.getenv("RAILWAY_GIT_COMMIT_SHA")
            or os.getenv("RENDER_GIT_COMMIT")
            or "unknown"
        )
        deployment_service = (
            os.getenv("RAILWAY_SERVICE_NAME")
            or os.getenv("RENDER_SERVICE_NAME")
            or "local"
        )
        storage_backend = "postgres" if PostgresDocumentStore.is_enabled() else "json-fallback"
        print(
            f"✅ Бот запущен... service={deployment_service} "
            f"commit={deployment_commit} storage={storage_backend} "
            f"ratecore={'sandbox' if is_ratecore_sandbox() else 'LIVE'}"
        )
        print("[GeoDebug] build marker: geo-no-gpt-landmarks-v2")
        if RATECORE_HTTP_PORT:
            from app.handlers.ratecore_http import start_http_server

            http_runner = await start_http_server(bot, dp, RATECORE_HTTP_HOST, int(RATECORE_HTTP_PORT))
            logging.info("HTTP health/webhook on %s:%s", RATECORE_HTTP_HOST, RATECORE_HTTP_PORT)
        delete_webhook_sync(BOT_TOKEN)
        logging.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        if http_runner is not None:
            await http_runner.cleanup()
        for task in worker_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        UserService.flush_writes()
        PostgresDocumentStore.close()
        UserService.reset_runtime_cache()
        shutdown_blocking_pool()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
