import os
from dotenv import load_dotenv

load_dotenv()


def parse_admin_ids(raw_value: str | None) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()

    if not raw_value:
        return tuple(values)

    normalized_source = str(raw_value).replace(";", ",").replace("\n", ",")
    for chunk in normalized_source.split(","):
        value = chunk.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)

    return tuple(values)


def _build_database_url() -> str | None:
    direct_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if direct_url:
        return direct_url

    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST")
    port = os.getenv("PGPORT") or os.getenv("POSTGRES_PORT")
    database = (
        os.getenv("PGDATABASE")
        or os.getenv("POSTGRES_DB")
        or os.getenv("POSTGRES_DATABASE")
    )
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")

    if all([host, port, database, user, password]):
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

    return None

SHOP_NAME = (os.getenv("SHOP_NAME") or "PlayStation™Store").strip() or "PlayStation™Store"
DIAMOND_SITE_URL = (os.getenv("DIAMOND_SITE_URL") or "").strip()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS"))
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else None
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") # Ключ для ChatGPT
DATABASE_URL = _build_database_url()
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY") # Ключ для Яндекс Карт (Search API)
DGIS_API_KEY = os.getenv("DGIS_API_KEY") # Ключ для 2GIS
LTC_WALLET_ADDRESS = os.getenv("LTC_WALLET_ADDRESS", "").strip()
BTC_WALLET_ADDRESS = os.getenv("BTC_WALLET_ADDRESS", "").strip()

RATECORE_ENV = (os.getenv("RATECORE_ENV") or "sandbox").strip().lower()
RATECORE_BASE_URL = (os.getenv("RATECORE_BASE_URL") or "https://api.ratecore.net").rstrip("/")
RATECORE_API_KEY = (os.getenv("RATECORE_API_KEY") or "").strip()
RATECORE_API_SECRET = (os.getenv("RATECORE_API_SECRET") or "").strip()
RATECORE_CALLBACK_SECRET = (os.getenv("RATECORE_CALLBACK_SECRET") or "").strip()
RATECORE_CALLBACK_URL = (os.getenv("RATECORE_CALLBACK_URL") or "").strip()
RATECORE_HTTP_HOST = (os.getenv("RATECORE_HTTP_HOST") or "0.0.0.0").strip()
RATECORE_HTTP_PORT = os.getenv("RATECORE_HTTP_PORT") or os.getenv("PORT")

# Mini App URL (Telegram WebApp)
MINIAPP_URL = (os.getenv("MINIAPP_URL") or "").strip()


def is_ratecore_sandbox() -> bool:
    return RATECORE_ENV not in {"production", "prod", "live"}


def resolve_ratecore_callback_url() -> str:
    if RATECORE_CALLBACK_URL:
        return RATECORE_CALLBACK_URL.rstrip("/")
    public_domain = (
        os.getenv("RAILWAY_PUBLIC_DOMAIN")
        or os.getenv("RAILWAY_STATIC_URL")
        or ""
    ).strip()
    if public_domain:
        if public_domain.startswith("http://") or public_domain.startswith("https://"):
            return public_domain.rstrip("/") + "/webhooks/ratecore"
        return f"https://{public_domain}/webhooks/ratecore"
    return ""


def is_admin(user_id: int | str | None, admin_ids: tuple[str, ...] | None = None) -> bool:
    if user_id is None:
        return False
    normalized = str(user_id).strip()
    if not normalized:
        return False
    effective_ids = admin_ids if admin_ids is not None else ADMIN_IDS
    return normalized in effective_ids

if not BOT_TOKEN:
    print("Ошибка: Не указан BOT_TOKEN в переменных окружения")
