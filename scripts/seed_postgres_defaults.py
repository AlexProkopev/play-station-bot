from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the bot's PostgreSQL tables and seed missing default documents."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional PostgreSQL URL. Prefer DATABASE_URL in the environment to avoid exposing secrets in shell history.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    from app.services.catalog import (
        CatalogService,
        CATALOG_ALL_PRODUCTS_DOC_KEY,
        CATALOG_GENERATION_SETTINGS_DOC_KEY,
        CATALOG_INVENTORY_DOC_KEY,
        CATALOG_PRODUCTS_CACHE_DOC_KEY,
        CATALOG_STASH_REGISTRY_DOC_KEY,
        CATALOG_STASH_TYPES_DOC_KEY,
        DEFAULT_STASH_TYPES,
        MINIOPT_ALL_PRODUCTS_DOC_KEY,
        MINIOPT_GENERATION_SETTINGS_DOC_KEY,
        MINIOPT_INVENTORY_DOC_KEY,
        MINIOPT_PRODUCTS_CACHE_DOC_KEY,
        MINIOPT_STASH_REGISTRY_DOC_KEY,
        MINIOPT_STASH_TYPES_DOC_KEY,
        DEFAULT_MINIOPT_STASH_TYPES,
    )
    from app.services.chest import CHEST_DOC_KEY, DEFAULT_CONFIG
    from app.services.postgres_store import PostgresDocumentStore
    from app.services.pricing import PRICING_DOC_KEY, PricingService
    from app.services.referral_race import DEFAULT_RACE, RACE_DOC_KEY
    from app.services.reviews import ReviewsService
    from app.services.roulette import DEFAULT_DB, ROULETTE_DOC_KEY
    from app.services.settings import DEFAULT_SETTINGS, SETTINGS_DOC_KEY
    from app.services.blacklist import BLACKLIST_DOC_KEY
    from app.services.coupons import COUPONS_DOC_KEY
    from app.services.analytics import STATS_DOC_KEY

    if not PostgresDocumentStore.is_enabled():
        raise SystemExit("DATABASE_URL is not configured. Set it in the environment or pass --database-url.")

    defaults = {
        SETTINGS_DOC_KEY: DEFAULT_SETTINGS,
        PRICING_DOC_KEY: PricingService._ensure_defaults(),
        CHEST_DOC_KEY: DEFAULT_CONFIG,
        RACE_DOC_KEY: DEFAULT_RACE,
        ROULETTE_DOC_KEY: DEFAULT_DB,
        COUPONS_DOC_KEY: {"coupons": {}},
        BLACKLIST_DOC_KEY: [],
        STATS_DOC_KEY: {"cities": {}, "updated_at": None},
        ReviewsService.REVIEWS_DOC_KEY: [],
        ReviewsService.SETTINGS_DOC_KEY: ReviewsService.DEFAULT_SETTINGS,
        ReviewsService.ACTIVE_CITIES_DOC_KEY: [],
        ReviewsService.PROMPTS_DOC_KEY: [],
        ReviewsService.LAST_BATCH_DOC_KEY: {},
        CATALOG_ALL_PRODUCTS_DOC_KEY: CatalogService.ALL_PRODUCTS,
        CATALOG_PRODUCTS_CACHE_DOC_KEY: {},
        CATALOG_GENERATION_SETTINGS_DOC_KEY: CatalogService.DEFAULT_GENERATION_SETTINGS,
        CATALOG_INVENTORY_DOC_KEY: {},
        CATALOG_STASH_TYPES_DOC_KEY: {},
        CATALOG_STASH_REGISTRY_DOC_KEY: list(DEFAULT_STASH_TYPES),
        MINIOPT_ALL_PRODUCTS_DOC_KEY: CatalogService.MINIOPT_ALL_PRODUCTS,
        MINIOPT_PRODUCTS_CACHE_DOC_KEY: {},
        MINIOPT_GENERATION_SETTINGS_DOC_KEY: CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS,
        MINIOPT_INVENTORY_DOC_KEY: {},
        MINIOPT_STASH_TYPES_DOC_KEY: {},
        MINIOPT_STASH_REGISTRY_DOC_KEY: list(DEFAULT_MINIOPT_STASH_TYPES),
        "geo_districts_cache": {},
        "geo_city_population_cache": {},
    }

    PostgresDocumentStore.initialize()
    created = 0
    skipped = 0
    for key, payload in defaults.items():
        if PostgresDocumentStore.set_document_if_missing(key, copy.deepcopy(payload)):
            created += 1
            print(f"created: {key}")
        else:
            skipped += 1
            print(f"kept: {key}")
    print(f"Done. Created: {created}; kept existing: {skipped}.")


if __name__ == "__main__":
    main()