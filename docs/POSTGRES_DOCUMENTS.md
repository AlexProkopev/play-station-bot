# PostgreSQL documents

The bot stores JSON documents in the `app_documents` table. To create the
tables and add only missing defaults, run:

```powershell
$env:DATABASE_URL = "postgresql://postgres:REAL_PASSWORD@REAL_HOST:REAL_PORT/railway"
python scripts/seed_postgres_defaults.py
```

The URL in the command must contain the real Railway values. The placeholder
`PASSWORD`/`HOST` URL is only a template and cannot be used for a connection.

## Documents created by the seed

The script adds these documents only when the key does not exist:

`settings`, `region_pricing`, `chest_config`, `referral_race`,
`roulette_games`, `coupons_db`, `blacklist`, `product_interest_stats`,
`reviews`, `review_settings`, `active_cities`, `review_prompts`,
`reviews_last_batch`, `catalog_all_products`, `catalog_products_cache`,
`catalog_generation_settings`, `catalog_inventory`,
`catalog_product_stash_types`, `catalog_stash_registry`,
`miniopt_all_products`, `miniopt_products_cache`,
`miniopt_generation_settings`, `miniopt_inventory`,
`miniopt_product_stash_types`, `miniopt_stash_registry`.

## Intentionally empty geography

These keys are created with empty JSON objects and must be filled manually:

- `geo_districts_cache` -> `{ "city": ["район 1", "район 2"] }`
- `geo_city_population_cache` -> `{ "city": { "population": 100000 } }`

`active_cities` is also initialized as an empty JSON array. User data is not
seeded: the `app_users` table is created by the application and is filled at
runtime.