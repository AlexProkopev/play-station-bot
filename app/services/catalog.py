import json
import os
import random
import itertools
from datetime import datetime
from app.utils import get_now_msk
from app.services.postgres_store import PostgresDocumentStore
from app.services.pricing import PricingService

PRODUCTS_CACHE_FILE = "storage/products_cache.json"
PRICE_SEED_FILE = "storage/price_seed.json"
CATALOG_GENERATION_SETTINGS_FILE = "storage/catalog_generation_settings.json"
MINIOPT_GENERATION_SETTINGS_FILE = "storage/miniopt_generation_settings.json"
CATALOG_PRODUCTS_CACHE_DOC_KEY = "catalog_products_cache"
CATALOG_PRICE_SEED_DOC_KEY = "catalog_price_seed"
CATALOG_GENERATION_SETTINGS_DOC_KEY = "catalog_generation_settings"
CATALOG_INVENTORY_DOC_KEY = "catalog_inventory"
CATALOG_STASH_TYPES_DOC_KEY = "catalog_product_stash_types"
CATALOG_STASH_REGISTRY_DOC_KEY = "catalog_stash_registry"
CATALOG_ALL_PRODUCTS_DOC_KEY = "catalog_all_products"
MINIOPT_PRODUCTS_CACHE_DOC_KEY = "miniopt_products_cache"
MINIOPT_GENERATION_SETTINGS_DOC_KEY = "miniopt_generation_settings"
MINIOPT_INVENTORY_DOC_KEY = "miniopt_inventory"
MINIOPT_STASH_TYPES_DOC_KEY = "miniopt_product_stash_types"
MINIOPT_STASH_REGISTRY_DOC_KEY = "miniopt_stash_registry"
MINIOPT_ALL_PRODUCTS_DOC_KEY = "miniopt_all_products"
COURIER_JOB_ID = "pwork"
COURIER_JOB_NAME = "Работа курьер"
COURIER_JOB_PRICE = 3500
COURIER_JOB_NOTICE = (
    "После оплаты вакансии куратор Store напишет в течение часа "
    "и пришлет анкету."
)
DEFAULT_STASH_TYPES = ["Тайник", "Прикоп", "Магнит"]
DEFAULT_MINIOPT_STASH_TYPES = ["Тайник", "Прикоп земляной"]
AUTO_PRODUCT_DISTRICT_MIN = 1
AUTO_PRODUCT_DISTRICT_MAX = 5
AUTO_PRODUCT_STREET_MIN = 4
AUTO_PRODUCT_STREET_MAX = 7
DENSE_CITY_DISTRICT_THRESHOLD = 10
COMPACT_CITY_MAX_POPULATION = 40000
SMALL_CITY_MAX_POPULATION = 100000
MEDIUM_CITY_MAX_POPULATION = 500000
LEGACY_STASH_TYPE_ALIASES = {
    "тайник": "Тайник",
    "тайник-камень": "Тайник",
    "тайник камень": "Тайник",
    "прикоп": "Прикоп",
    "прикоп снежный": "Прикоп",
    "магнит": "Магнит",
}
MINIOPT_PRICE_FACTOR = 0.85
DEFAULT_ALL_PRODUCTS = [
  {
    "id": "p1",
    "name": "0.55г Hash | 40% Frappuccino, EAGLE",
    "base_price": 2100
  },
  {
    "id": "p4",
    "name": "1.0г Hash | 50% Frappuccino, EAGLE ",
    "base_price": 3000
  },
  {
    "id": "p6",
    "name": "0.5г ★ Мефедрон VHQ Crystall ★ 💎 ",
    "base_price": 2550
  },
  {
    "id": "p7",
    "name": "0.8г ★ Мефедрон VHQ Crystall ★ 💎 ",
    "base_price": 3350
  },
  {
    "id": "p10",
    "name": "1.0г ★ Мефедрон VHQ Crystall ★ 💎 ",
    "base_price": 4350
  },
  {
    "id": "p8",
    "name": "0.55г A-PVP | Crystals ",
    "base_price": 2550
  },
  {
    "id": "p9",
    "name": "1.0г A-PVP | Crystals ",
    "base_price": 4250
  },
  {
    "id": "p11",
    "name": "2.0г A-PVP | Crystals ",
    "base_price": 6600
  },
  {
    "id": "p15",
    "name": "0.55г A-PVP | Мука",
    "base_price": 2300
  },
  {
    "id": "p17",
    "name": "1.0г A-PVP | Мука",
    "base_price": 4000
  },
  {
    "id": "p18",
    "name": "2.0г A-PVP | Мука",
    "base_price": 6000
  },
  {
    "id": "p23",
    "name": "1.0г ★Бoшки HQ AK - 47★ ",
    "base_price": 3500
  },
  {
    "id": "p24",
    "name": "0.55г ★Бoшки HQ AK - 47★ ",
    "base_price": 2500
  },
  {
    "id": "p25",
    "name": "0.25г ★ Метадон ★ ",
    "base_price": 2700
  },
  {
    "id": "p26",
    "name": "0.5г ★ Метадон ★ ",
    "base_price": 3700
  },
  {
    "id": "p27",
    "name": "1.0г ★ Метадон ★ ",
    "base_price": 6000
  }
]

DEFAULT_MINIOPT_PRODUCTS: list[dict] = []

CORE_PRODUCT_FAMILIES = {
    "p1": "hash",
    "p4": "hash",
    "p6": "meph",
    "p7": "meph",
    "p10": "meph",
    "p8": "pvp",
    "p9": "pvp",
    "p11": "pvp",
    "p15": "pvp",
    "p17": "pvp",
    "p18": "pvp",
    "p23": "lyrica",
    "p24": "lyrica",
    "p25": "methadone",
    "p26": "methadone",
    "p27": "methadone",
}
FAMILY_LABELS = {
    "hash": "гаш",
    "meph": "меф",
    "pvp": "PVP / альфа",
    "lyrica": "лирика",
    "methadone": "метадон",
    "extra": "доп. товар",
}
FAMILY_SLOT_HINTS = {
    "hash": "только гаш (слоты p1 и p4)",
    "meph": "только меф (слоты p6, p7, p10)",
    "pvp": "только PVP / альфа (слоты p8, p9, p11, p15, p17, p18)",
    "lyrica": "только лирика (слоты p23, p24)",
    "methadone": "только метадон (слоты p25, p26, p27)",
    "extra": "доп. товар: в каталог по % и мин. населению",
}


def family_for_product_id(product_id: str, stored_family: str | None = None) -> str:
    pid = str(product_id or "").strip()
    if pid in CORE_PRODUCT_FAMILIES:
        return CORE_PRODUCT_FAMILIES[pid]
    text = str(stored_family or "").strip().lower()
    return text if text in FAMILY_LABELS else "extra"


def is_core_product_id(product_id: str) -> bool:
    return str(product_id or "").strip() in CORE_PRODUCT_FAMILIES


def product_display_name(product: dict | None) -> str:
    payload = product or {}
    name = str(payload.get("name") or "").strip()
    weight = str(payload.get("weight") or "").strip()
    if weight and weight.lower() not in name.lower():
        return f"{name} {weight}".strip()
    return name


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family, family or "-")


def _copy_default_products() -> list[dict]:
    return [dict(item) for item in DEFAULT_ALL_PRODUCTS]


def _copy_default_miniopt_products() -> list[dict]:
    return [dict(item) for item in DEFAULT_MINIOPT_PRODUCTS]


def _normalize_products_payload(data) -> list[dict]:
    if not isinstance(data, list):
        return []

    normalized = []
    seen_ids = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not product_id or not name or product_id in seen_ids:
            continue
        try:
            base_price = int(item.get("base_price"))
        except (TypeError, ValueError):
            continue
        normalized.append(_normalize_product_entry(item, product_id, name, base_price))
        seen_ids.add(product_id)
    return normalized


def _normalize_product_entry(item: dict, product_id: str, name: str, base_price: int) -> dict:
    family = family_for_product_id(product_id, item.get("family"))
    entry = {
        "id": product_id,
        "name": name,
        "base_price": base_price,
        "family": family,
    }
    weight = str(item.get("weight") or "").strip()
    if weight:
        entry["weight"] = weight
    try:
        if item.get("show_percent") is not None:
            entry["show_percent"] = max(0, min(100, int(item.get("show_percent"))))
    except (TypeError, ValueError):
        pass
    try:
        if item.get("min_population") is not None:
            entry["min_population"] = max(0, int(item.get("min_population")))
    except (TypeError, ValueError):
        pass
    return entry


def _normalize_allowed_product_ids(values) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized = []
    seen = set()
    for item in values:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _normalize_city_key(city_name: str | None) -> str:
    """Canonical city key used across catalog/inventory caches.

    Admin can type cities with region suffix (e.g. "Томск (Томская область)").
    User flow stores city as plain name. We keep a single key format to avoid split caches.
    Nested region names like "Республика Саха (Якутия)" must stay on the city name.
    """
    from app.services.geo import GeoService

    return GeoService._normalize_city_name(city_name)


def _resolve_legacy_city_key(data: dict, city_name: str | None) -> str:
    """Resolve existing city key in cache/inventory, falling back to canonical key."""
    normalized = _normalize_city_key(city_name)
    if normalized in data:
        return normalized

    raw = " ".join((city_name or "").strip().lower().split())
    if raw in data:
        return raw

    if normalized:
        pref = normalized + " ("
        candidates = [k for k in data.keys() if isinstance(k, str) and k.startswith(pref)]
        if candidates:
            return sorted(candidates)[0]

    return normalized


def _purge_invalid_city_cache_entries(cache: dict) -> tuple[dict, bool]:
    """Remove bogus city keys that are actually product IDs from old admin callback bugs."""
    if not isinstance(cache, dict) or not cache:
        return cache, False

    try:
        product_ids = {
            item.get("id")
            for item in CatalogService.get_all_products()
            if isinstance(item, dict) and item.get("id")
        }
    except Exception:
        return cache, False

    invalid_keys = [key for key in list(cache.keys()) if isinstance(key, str) and key in product_ids]
    if not invalid_keys:
        return cache, False

    for key in invalid_keys:
        cache.pop(key, None)

    return cache, True


def _purge_invalid_city_cache_entries_for_ids(cache: dict, product_ids: set[str]) -> tuple[dict, bool]:
    if not isinstance(cache, dict) or not cache:
        return cache, False

    invalid_keys = [key for key in list(cache.keys()) if isinstance(key, str) and key in product_ids]
    if not invalid_keys:
        return cache, False

    for key in invalid_keys:
        cache.pop(key, None)

    return cache, True


def _load_price_seed() -> str:
    """Загружает текущий сид прайса. Если нет - создаёт новый."""
    if PostgresDocumentStore.is_enabled():
        data = PostgresDocumentStore.get_document(CATALOG_PRICE_SEED_DOC_KEY, {})
        if isinstance(data, dict):
            seed = str(data.get("seed") or "").strip()
            if seed:
                return seed
    if os.path.exists(PRICE_SEED_FILE):
        try:
            with open(PRICE_SEED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get("seed", "initial"))
        except Exception:
            pass
    seed = str(random.randint(100000, 999999))
    _save_price_seed(seed)
    return seed


def _save_price_seed(seed: str) -> None:
    """Сохраняет новый сид прайса."""
    payload = {"seed": seed, "updated_at": datetime.now().isoformat()}
    if PostgresDocumentStore.is_enabled():
        PostgresDocumentStore.set_document(CATALOG_PRICE_SEED_DOC_KEY, payload, force=True)
        return
    try:
        os.makedirs("storage", exist_ok=True)
        with open(PRICE_SEED_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _normalize_stash_type(value: str | None) -> str:
    cleaned = " ".join((value or "").strip().split())
    if not cleaned:
        return ""
    return LEGACY_STASH_TYPE_ALIASES.get(cleaned.casefold(), cleaned)


def _clean_stash_types(values: list[str] | tuple[str, ...] | None, limit: int | None = None) -> list[str]:
    cleaned = []
    for item in values or []:
        normalized = _normalize_stash_type(item)
        if not normalized or normalized in cleaned:
            continue
        cleaned.append(normalized)
        if limit is not None and len(cleaned) >= limit:
            break
    return cleaned


def _clean_district_values(values: list[str] | tuple[str, ...] | None) -> list[str]:
    cleaned = []
    for item in values or []:
        if not isinstance(item, str):
            continue
        district = " ".join(item.strip().split())
        if district and district not in cleaned:
            cleaned.append(district)
    return cleaned


def _is_major_district_label(value: str) -> bool:
    label = (value or "").strip().lower()
    if not label:
        return False
    markers = (
        " район",
        "район ",
        "район",
        "округ",
        "микрорайон",
        "мкр",
        "квартал",
    )
    return any(marker in label for marker in markers)


def _is_street_label(value: str) -> bool:
    label = (value or "").strip().lower()
    if not label:
        return False
    markers = (
        "ул.",
        "улица",
        "пр-т",
        "проспект",
        "пер.",
        "переулок",
        "пр-д",
        "проезд",
        "бул.",
        "бульвар",
        "ш.",
        "шоссе",
        "набережная",
        "аллея",
    )
    return any(marker in label for marker in markers)


def _is_dense_district_pool(values: list[str] | tuple[str, ...] | None) -> bool:
    """10+ names in the city pool means microdistricts, not a few admin districts."""
    return len(_clean_district_values(values)) >= DENSE_CITY_DISTRICT_THRESHOLD


def _prepare_product_district_pool(
    city_name: str,
    all_districts: list[str] | None = None,
) -> tuple[list[str], bool]:
    raw = _clean_district_values(all_districts if all_districts is not None else CatalogService._get_city_district_pool(city_name))
    return raw, _is_dense_district_pool(raw)


def _get_stash_pool() -> list[str]:
    try:
        pool = _clean_stash_types(CatalogService.STASH_TYPES)
    except NameError:
        pool = []
    if len(pool) < 2:
        pool = list(DEFAULT_STASH_TYPES)
    # Preserve order while removing duplicates
    unique = []
    for value in pool:
        if value not in unique:
            unique.append(value)
    if len(unique) < 2:
        unique = list(DEFAULT_STASH_TYPES)
    return unique


def _pick_default_stash_types(rng: random.Random | None = None) -> list[str]:
    pool = _get_stash_pool()
    rng = rng or random
    if len(pool) <= 2:
        return pool[:2]
    return rng.sample(pool, k=2)


def _build_stash_combinations(pool: list[str]) -> list[list[str]]:
    unique_pool = []
    for item in pool:
        if item not in unique_pool:
            unique_pool.append(item)
    if len(unique_pool) <= 2:
        return [unique_pool[:2]]
    return [list(pair) for pair in itertools.combinations(unique_pool, 2)]


def _get_unique_stash_pool(pool: list[str] | None, fallback: list[str]) -> list[str]:
    cleaned = _clean_stash_types(pool)
    if len(cleaned) < 2:
        cleaned = list(fallback)
    unique = []
    for value in cleaned:
        if value not in unique:
            unique.append(value)
    if len(unique) < 2:
        unique = list(fallback)
    return unique


def _pick_default_stash_types_from_pool(
    pool: list[str] | None,
    fallback: list[str],
    rng: random.Random | None = None,
) -> list[str]:
    unique = _get_unique_stash_pool(pool, fallback)
    rng = rng or random
    if len(unique) <= 2:
        return unique[:2]
    return rng.sample(unique, k=2)


def _pick_weight_group_variants(
    rng: random.Random,
    product_ids: list[str] | tuple[str, ...],
) -> list[str]:
    variants = []
    for product_id in product_ids:
        if product_id and product_id not in variants:
            variants.append(product_id)

    if not variants:
        return []
    if len(variants) == 1:
        return list(variants)
    if len(variants) == 2:
        if rng.choice([True, False]):
            return list(variants)
        return [rng.choice(variants)]

    combinations = []
    for size in range(1, len(variants) + 1):
        for combo in itertools.combinations(variants, size):
            combinations.append(list(combo))

    return list(rng.choice(combinations))


def _unique_product_ids(values: list[str] | tuple[str, ...]) -> list[str]:
    unique = []
    for product_id in values:
        if product_id and product_id not in unique:
            unique.append(product_id)
    return unique


def _get_catalog_generation_tier(population: int) -> str:
    """Return the product generation tier for a city population.

    Unknown population uses a conservative small-city profile instead of the
    historical large-city fallback.
    """
    if 0 < population < COMPACT_CITY_MAX_POPULATION:
        return "compact"
    if population <= 0 or population < SMALL_CITY_MAX_POPULATION:
        return "small"
    if population < MEDIUM_CITY_MAX_POPULATION:
        return "medium"
    return "large"


def _get_filtered_generation_range(population: int) -> tuple[int, int]:
    tier = _get_catalog_generation_tier(population)
    if tier == "compact":
        return 4, 6
    if tier == "small":
        return 6, 9
    if tier == "medium":
        return 7, 11
    return 9, 13


def _get_miniopt_generation_range(population: int) -> tuple[int, int]:
    return 3, 7


def _pick_random_product_ids_for_pool(
    pool: list[dict],
    rng: random.Random,
    min_take: int,
    max_take: int,
) -> list[str]:
    available = [item.get("id") for item in pool if isinstance(item, dict) and item.get("id")]
    if not available:
        return []

    max_take = min(max_take, len(available))
    min_take = min(min_take, max_take)
    if max_take <= 0:
        return []

    take = rng.randint(min_take, max_take) if min_take < max_take else max_take
    return _unique_product_ids(rng.sample(available, k=take))


def _round_catalog_price(value: float | int) -> int:
    price = int(value)
    return (price // 100) * 100


MEPH_P10_CHANCE = 0.9


def _build_meph_group_b(rng: random.Random, *, allow_all_three: bool = False) -> list[str]:
    """p6 всегда. p10 в 90% городов; иначе соседний вес p7."""
    include_p10 = rng.random() < MEPH_P10_CHANCE
    if include_p10:
        if allow_all_three and rng.random() < 0.5:
            return ["p6", "p7", "p10"]
        return ["p6", "p10"]
    return ["p6", "p7"]


def _build_compact_city_product_ids(rng: random.Random) -> list[str]:
    # Группа A: гаш обязателен, для компактных городов ровно один вес.
    group_a = [rng.choice(["p1", "p4"])]

    # Группа L: Лирика обязательна, для компактных городов ровно один вес.
    group_l = [rng.choice(["p23", "p24"])]

    # Группа B: обязательный p6 + p10 в 90% городов (иначе p7).
    group_b = _build_meph_group_b(rng)

    # Группа C: обязательный p8 или p15 + один сосед.
    group_c = [rng.choice(["p8", "p15"]), rng.choice(["p9", "p11", "p17", "p18"])]

    # Группа D: для части мелких городов добавляем одну позицию p25/p26/p27.
    group_d = [rng.choice(["p25", "p26", "p27"])] if rng.choice([True, False]) else []

    return _unique_product_ids(group_a + group_l + group_b + group_c + group_d)


def _build_small_city_product_ids(rng: random.Random) -> list[str]:
    # База "small": те же обязательные группы, что и "compact".
    base = _build_compact_city_product_ids(rng)

    # Добираем до диапазона 6-9 из оставшегося пула.
    min_target = 6
    max_target = 9
    target = rng.randint(min_target, max_target)

    if len(base) >= target:
        return base

    allowed_pool = [
        "p1", "p4", "p6", "p7", "p10", "p8", "p9", "p11", "p15", "p17", "p18", "p23", "p24", "p25", "p26", "p27"
    ]
    extra_pool = [pid for pid in allowed_pool if pid not in base]
    need = min(target - len(base), len(extra_pool))
    extras = rng.sample(extra_pool, k=need) if need > 0 else []

    return _unique_product_ids(base + extras)


def _build_non_small_city_product_ids(rng: random.Random) -> list[str]:
    # Группа A: гаш обязателен. Для средних/крупных даем обе позиции.
    group_a = ["p1", "p4"]

    # Группа L: Лирика обязательна. Для средних/крупных даем обе позиции.
    group_l = ["p23", "p24"]

    # Группа B: p6 обязательно. p10 в 90% городов; в половине из них ещё p7.
    group_b = _build_meph_group_b(rng, allow_all_three=True)

    # Группа C: p8/p15 обязательно. 70/30: больше или меньше товаров в связке.
    base_c = rng.choice(["p8", "p15"])
    neighbors_c = ["p9", "p11", "p17", "p18"]
    if rng.random() < 0.7:
        group_c = [base_c] + rng.sample(neighbors_c, k=2)
    else:
        group_c = [base_c, rng.choice(neighbors_c)]

    # Группа D: для крупных берём 2 из 3.
    group_d = rng.sample(["p25", "p26", "p27"], k=2)

    return _unique_product_ids(group_a + group_l + group_b + group_c + group_d)


def _pick_extra_product_ids(all_products: list[dict], population: int, rng: random.Random) -> list[str]:
    picked: list[str] = []
    for product in all_products or []:
        if not isinstance(product, dict):
            continue
        pid = str(product.get("id") or "").strip()
        if not pid or is_core_product_id(pid):
            continue
        try:
            min_pop = int(product.get("min_population") or 0)
        except (TypeError, ValueError):
            min_pop = 0
        if population < min_pop:
            continue
        try:
            chance = int(product.get("show_percent") if product.get("show_percent") is not None else 100)
        except (TypeError, ValueError):
            chance = 100
        chance = max(0, min(100, chance))
        if chance <= 0:
            continue
        if rng.random() * 100 < chance:
            picked.append(pid)
    return _unique_product_ids(picked)


def _remove_optional_file(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    try:
        os.remove(path)
        return "removed"
    except Exception:
        return "error"

class CatalogService:
    PRODUCTS_DB_FILE = "storage/all_products.json"
    PRODUCTS_CACHE_FILE = "storage/products_cache.json"
    INVENTORY_FILE = "storage/products_inventory.json"
    STASH_TYPES_FILE = "storage/product_stash_types.json"
    STASH_TYPES_REGISTRY_FILE = "storage/stash_types.json"
    MINIOPT_PRODUCTS_DB_FILE = "storage/miniopt_all_products.json"
    MINIOPT_PRODUCTS_CACHE_FILE = "storage/miniopt_products_cache.json"
    MINIOPT_INVENTORY_FILE = "storage/miniopt_products_inventory.json"
    MINIOPT_STASH_TYPES_FILE = "storage/miniopt_product_stash_types.json"
    MINIOPT_STASH_TYPES_REGISTRY_FILE = "storage/miniopt_stash_types.json"
    STASH_TYPES = list(DEFAULT_STASH_TYPES)
    MINIOPT_STASH_TYPES = list(DEFAULT_MINIOPT_STASH_TYPES)
    PRODUCT_EMOJIS = {
        "p1": "🍫",
        "p2": "🍁",
        "p4": "🍫",
        "p5": "🍫",
        "p6": "❄️",
        "p7": "❄️",
        "p8": "💎",
        "p9": "💎",
        "p10": "❄️",
        "p11": "💎",
        "p15": "💎",
        "p17": "💎",
        "p18": "💎",
        "p19": "💎",
        "p22": "🍁",
        "p27": "🍯",
        "p24": "🟢",
        "p25": "🍯",
        "p26": "⚡",
        "p28": "⚡",
        "p20": "💥",
        "p21": "🥥",
        "p23": "🟢",
        COURIER_JOB_ID: "🛵",
    }

    ALL_PRODUCTS = _copy_default_products()
    MINIOPT_ALL_PRODUCTS = _copy_default_miniopt_products()
    PRODUCT_STASH_TYPES: dict[str, list[str]] = {}
    MINIOPT_PRODUCT_STASH_TYPES: dict[str, list[str]] = {}
    DEFAULT_GENERATION_SETTINGS = {
        "allowed_product_ids": []
    }
    MINIOPT_DEFAULT_GENERATION_SETTINGS = {
        "allowed_product_ids": []
    }

    @staticmethod
    def _load_miniopt_stash_registry():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(MINIOPT_STASH_REGISTRY_DOC_KEY, list(DEFAULT_MINIOPT_STASH_TYPES))
            if isinstance(data, list):
                cleaned = _clean_stash_types(data)
                if len(cleaned) >= 2:
                    CatalogService.MINIOPT_STASH_TYPES = cleaned
                    return CatalogService.MINIOPT_STASH_TYPES
            CatalogService.MINIOPT_STASH_TYPES = list(DEFAULT_MINIOPT_STASH_TYPES)
            return CatalogService.MINIOPT_STASH_TYPES
        if os.path.exists(CatalogService.MINIOPT_STASH_TYPES_REGISTRY_FILE):
            try:
                with open(CatalogService.MINIOPT_STASH_TYPES_REGISTRY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    cleaned = _clean_stash_types(data)
                    if len(cleaned) >= 2:
                        CatalogService.MINIOPT_STASH_TYPES = cleaned
                        if cleaned != data:
                            CatalogService._save_miniopt_stash_registry()
                        return CatalogService.MINIOPT_STASH_TYPES
            except Exception:
                pass
        CatalogService.MINIOPT_STASH_TYPES = list(DEFAULT_MINIOPT_STASH_TYPES)
        return CatalogService.MINIOPT_STASH_TYPES

    @staticmethod
    def _save_miniopt_stash_registry():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(
                MINIOPT_STASH_REGISTRY_DOC_KEY,
                list(CatalogService.MINIOPT_STASH_TYPES),
                force=True,
            )
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.MINIOPT_STASH_TYPES_REGISTRY_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.MINIOPT_STASH_TYPES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving miniopt stash registry: {e}")

    @staticmethod
    def _load_miniopt_stash_types():
        CatalogService._load_miniopt_stash_registry()
        if CatalogService.MINIOPT_PRODUCT_STASH_TYPES and not PostgresDocumentStore.is_enabled():
            return CatalogService.MINIOPT_PRODUCT_STASH_TYPES
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(MINIOPT_STASH_TYPES_DOC_KEY, {})
        elif not os.path.exists(CatalogService.MINIOPT_STASH_TYPES_FILE):
            CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {}
            CatalogService._save_miniopt_stash_types()
            return CatalogService.MINIOPT_PRODUCT_STASH_TYPES
        else:
            try:
                with open(CatalogService.MINIOPT_STASH_TYPES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        try:
            if isinstance(data, dict):
                cleaned = {}
                changed = False
                allowed_pool = _get_unique_stash_pool(CatalogService.MINIOPT_STASH_TYPES, DEFAULT_MINIOPT_STASH_TYPES)
                for key, value in data.items():
                    if not isinstance(value, list):
                        changed = True
                        continue
                    cleaned_list = [t for t in _clean_stash_types(value) if t in allowed_pool]
                    if len(cleaned_list) >= 2:
                        normalized_pair = cleaned_list[:2]
                        cleaned[key] = normalized_pair
                        if value != normalized_pair:
                            changed = True
                    else:
                        changed = True
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES = cleaned
                if changed and not PostgresDocumentStore.is_enabled():
                    CatalogService._save_miniopt_stash_types()
            else:
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {}
        except Exception:
            CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {}
        return CatalogService.MINIOPT_PRODUCT_STASH_TYPES

    @staticmethod
    def _save_miniopt_stash_types():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(
                MINIOPT_STASH_TYPES_DOC_KEY,
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES,
                force=True,
            )
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.MINIOPT_STASH_TYPES_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.MINIOPT_PRODUCT_STASH_TYPES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving miniopt stash types: {e}")

    @staticmethod
    def get_available_miniopt_stash_types() -> list[str]:
        CatalogService._load_miniopt_stash_registry()
        return list(CatalogService.MINIOPT_STASH_TYPES)

    @staticmethod
    def set_available_miniopt_stash_types(values: list[str], *, repair_products: bool = True) -> bool:
        CatalogService._load_miniopt_stash_registry()
        cleaned = _clean_stash_types(values)
        if len(cleaned) < 2:
            return False
        CatalogService.MINIOPT_STASH_TYPES = cleaned
        CatalogService._save_miniopt_stash_registry()
        if repair_products:
            CatalogService.resync_miniopt_product_stash_types(force_rebalance=False)
        return CatalogService.get_available_miniopt_stash_types() == cleaned

    @staticmethod
    def resync_miniopt_product_stash_types(force_rebalance: bool = False):
        CatalogService.get_all_miniopt_products()
        changed_ids = CatalogService._ensure_miniopt_stash_for_all_products(force_rebalance=force_rebalance)
        if force_rebalance or changed_ids:
            CatalogService._patch_miniopt_cache_stash_types(None if force_rebalance else changed_ids)

    @staticmethod
    def add_miniopt_stash_type(value: str) -> bool:
        if not isinstance(value, str):
            return False
        CatalogService._load_miniopt_stash_registry()
        value = _normalize_stash_type(value)
        if not value:
            return False
        current = CatalogService.get_available_miniopt_stash_types()
        if any(str(item).casefold() == value.casefold() for item in current):
            return False
        current.append(value)
        return CatalogService.set_available_miniopt_stash_types(current, repair_products=False)

    @staticmethod
    def remove_miniopt_stash_type(value: str) -> bool:
        CatalogService._load_miniopt_stash_registry()
        value = _normalize_stash_type(value)
        current = CatalogService.get_available_miniopt_stash_types()
        if value not in current:
            return False
        if len(current) <= 2:
            return False
        current = [item for item in current if item != value]
        return CatalogService.set_available_miniopt_stash_types(current)

    @staticmethod
    def _ensure_miniopt_stash_pair(product_id: str, force_rebalance: bool = False):
        CatalogService._load_miniopt_stash_types()
        current = CatalogService.MINIOPT_PRODUCT_STASH_TYPES.get(product_id)
        allowed_pool = _get_unique_stash_pool(CatalogService.MINIOPT_STASH_TYPES, DEFAULT_MINIOPT_STASH_TYPES)
        normalized_current = [item for item in _clean_stash_types(current, limit=2) if item in allowed_pool]
        if not force_rebalance and len(normalized_current) >= 2:
            if current != normalized_current:
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES[product_id] = normalized_current
                CatalogService._save_miniopt_stash_types()
            return
        price_seed = _load_price_seed()
        rng = random.Random(f"miniopt-stash::{product_id}::{price_seed}")
        CatalogService.MINIOPT_PRODUCT_STASH_TYPES[product_id] = _pick_default_stash_types_from_pool(
            CatalogService.MINIOPT_STASH_TYPES,
            DEFAULT_MINIOPT_STASH_TYPES,
            rng,
        )
        CatalogService._save_miniopt_stash_types()

    @staticmethod
    def _ensure_miniopt_stash_for_all_products(force_rebalance: bool = False) -> list[str]:
        CatalogService._load_miniopt_stash_types()
        changed_ids: list[str] = []
        CatalogService._load_miniopt_stash_registry()
        price_seed = _load_price_seed()
        allowed_pool = _get_unique_stash_pool(CatalogService.MINIOPT_STASH_TYPES, DEFAULT_MINIOPT_STASH_TYPES)
        for product in CatalogService.MINIOPT_ALL_PRODUCTS:
            p_id = product.get("id") if isinstance(product, dict) else None
            if not p_id:
                continue
            current = CatalogService.MINIOPT_PRODUCT_STASH_TYPES.get(p_id)
            normalized_current = [item for item in _clean_stash_types(current, limit=2) if item in allowed_pool]
            if force_rebalance or len(normalized_current) < 2:
                rng = random.Random(f"miniopt-stash::{p_id}::{price_seed}")
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES[p_id] = _pick_default_stash_types_from_pool(
                    CatalogService.MINIOPT_STASH_TYPES,
                    DEFAULT_MINIOPT_STASH_TYPES,
                    rng,
                )
                changed_ids.append(p_id)
            elif current != normalized_current:
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES[p_id] = normalized_current
                changed_ids.append(p_id)
        if changed_ids:
            CatalogService._save_miniopt_stash_types()
        return changed_ids

    @staticmethod
    def _load_all_miniopt_products():
        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.get_document(MINIOPT_ALL_PRODUCTS_DOC_KEY, _copy_default_miniopt_products())
            normalized = _normalize_products_payload(payload)
            CatalogService.MINIOPT_ALL_PRODUCTS = normalized or _copy_default_miniopt_products()
        elif os.path.exists(CatalogService.MINIOPT_PRODUCTS_DB_FILE):
            try:
                with open(CatalogService.MINIOPT_PRODUCTS_DB_FILE, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                normalized = _normalize_products_payload(payload)
                CatalogService.MINIOPT_ALL_PRODUCTS = normalized or _copy_default_miniopt_products()
            except Exception:
                CatalogService.MINIOPT_ALL_PRODUCTS = _copy_default_miniopt_products()
        else:
            CatalogService.MINIOPT_ALL_PRODUCTS = _copy_default_miniopt_products()
        CatalogService._ensure_miniopt_stash_for_all_products()

    @staticmethod
    def _save_all_miniopt_products():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(MINIOPT_ALL_PRODUCTS_DOC_KEY, CatalogService.MINIOPT_ALL_PRODUCTS)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.MINIOPT_PRODUCTS_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.MINIOPT_ALL_PRODUCTS, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving all miniopt products: {e}")

    @staticmethod
    def create_new_miniopt_product(name: str, price: int):
        CatalogService._load_all_miniopt_products()
        new_id = f"mo{random.randint(1000, 99999)}"
        while any(p['id'] == new_id for p in CatalogService.MINIOPT_ALL_PRODUCTS):
            new_id = f"mo{random.randint(1000, 99999)}"

        new_product = {
            "id": new_id,
            "name": name,
            "base_price": price,
        }
        CatalogService.MINIOPT_ALL_PRODUCTS.append(new_product)
        CatalogService._save_all_miniopt_products()
        CatalogService._ensure_miniopt_stash_pair(new_id)
        return new_product

    @staticmethod
    def delete_miniopt_product(product_id: str) -> bool:
        CatalogService._load_all_miniopt_products()
        before_count = len(CatalogService.MINIOPT_ALL_PRODUCTS)
        CatalogService.MINIOPT_ALL_PRODUCTS = [
            item for item in CatalogService.MINIOPT_ALL_PRODUCTS
            if not (isinstance(item, dict) and item.get("id") == product_id)
        ]
        if len(CatalogService.MINIOPT_ALL_PRODUCTS) == before_count:
            return False

        CatalogService._save_all_miniopt_products()
        CatalogService.MINIOPT_PRODUCT_STASH_TYPES.pop(product_id, None)
        CatalogService._save_miniopt_stash_types()

        cache = CatalogService._load_miniopt_cache()
        changed_cache = False
        for entry in cache.values():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if isinstance(ids, list) and product_id in ids:
                entry["ids"] = [item for item in ids if item != product_id]
                stash_map = entry.get("stash_types") if isinstance(entry.get("stash_types"), dict) else {}
                stash_map.pop(product_id, None)
                entry["stash_types"] = stash_map
                changed_cache = True
        if changed_cache:
            CatalogService._write_miniopt_cache(cache)

        inventory = CatalogService._load_miniopt_inventory()
        changed_inventory = False
        for city_key, product_map in list(inventory.items()):
            if isinstance(product_map, dict) and product_id in product_map:
                product_map.pop(product_id, None)
                changed_inventory = True
            if isinstance(product_map, dict) and not product_map:
                inventory.pop(city_key, None)
                changed_inventory = True
        if changed_inventory:
            CatalogService._save_miniopt_inventory(inventory)

        return True

    @staticmethod
    def update_miniopt_product(product_id: str, name: str, price: int) -> bool:
        CatalogService._load_all_miniopt_products()
        changed = False
        for product in CatalogService.MINIOPT_ALL_PRODUCTS:
            if not isinstance(product, dict) or product.get("id") != product_id:
                continue
            clean_name = str(name or "").strip()
            if not clean_name:
                return False
            if product.get("name") != clean_name:
                product["name"] = clean_name
                changed = True
            if int(product.get("base_price", 0)) != int(price):
                product["base_price"] = int(price)
                changed = True
            break
        else:
            return False

        if changed:
            CatalogService._save_all_miniopt_products()
        return True

    @staticmethod
    def get_all_miniopt_products():
        if PostgresDocumentStore.is_enabled() or not CatalogService.MINIOPT_ALL_PRODUCTS:
            CatalogService._load_all_miniopt_products()
        return CatalogService.MINIOPT_ALL_PRODUCTS

    @staticmethod
    def get_miniopt_product_stash_types(product_id: str) -> list[str]:
        CatalogService.get_all_miniopt_products()
        CatalogService._ensure_miniopt_stash_pair(product_id)
        stash = CatalogService.MINIOPT_PRODUCT_STASH_TYPES.get(product_id, [])
        if isinstance(stash, list) and len(stash) >= 2:
            return stash[:2]
        return _pick_default_stash_types_from_pool(CatalogService.MINIOPT_STASH_TYPES, DEFAULT_MINIOPT_STASH_TYPES)

    @staticmethod
    def get_miniopt_district_stash_type_map(city_name: str, product_id: str, districts: list[str] | None) -> dict[str, list[str]]:
        cleaned_districts = _clean_district_values(districts)
        if not cleaned_districts:
            return {}

        pool = CatalogService.get_available_miniopt_stash_types()
        combinations = _build_stash_combinations(pool)
        if not combinations:
            fallback = _pick_default_stash_types_from_pool(pool, DEFAULT_MINIOPT_STASH_TYPES)
            return {district: fallback for district in cleaned_districts}

        district_seed = _load_price_seed()
        rng = random.Random(f"miniopt-district-stash::{_normalize_city_key(city_name)}::{product_id}::{district_seed}")
        shuffled = list(combinations)
        rng.shuffle(shuffled)

        mapping = {}
        for index, district in enumerate(cleaned_districts):
            mapping[district] = list(shuffled[index % len(shuffled)])
        return mapping

    @staticmethod
    def get_miniopt_district_stash_types(city_name: str, product_id: str, district_name: str, districts: list[str] | None = None) -> list[str]:
        district_map = CatalogService.get_miniopt_district_stash_type_map(city_name, product_id, districts or [district_name])
        selected = district_map.get((district_name or "").strip())
        if isinstance(selected, list) and len(selected) >= 2:
            return selected[:2]
        return _pick_default_stash_types_from_pool(CatalogService.MINIOPT_STASH_TYPES, DEFAULT_MINIOPT_STASH_TYPES)

    @staticmethod
    def set_miniopt_product_stash_types(product_id: str, types: list[str]) -> bool:
        CatalogService.get_all_miniopt_products()
        CatalogService._load_miniopt_stash_registry()
        CatalogService._load_miniopt_stash_types()
        cleaned = []
        for item in types:
            if isinstance(item, str) and item in CatalogService.MINIOPT_STASH_TYPES and item not in cleaned:
                cleaned.append(item)
            if len(cleaned) == 2:
                break
        if len(cleaned) < 2:
            return False
        CatalogService.MINIOPT_PRODUCT_STASH_TYPES[product_id] = cleaned[:2]
        CatalogService._save_miniopt_stash_types()
        CatalogService._update_miniopt_cache_stash_types(product_id, cleaned[:2])
        return True

    @staticmethod
    def _load_miniopt_generation_settings() -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(
                MINIOPT_GENERATION_SETTINGS_DOC_KEY,
                CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy(),
            )
            if not isinstance(data, dict):
                return CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy()
            merged = CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy()
            merged.update(data)
            merged["allowed_product_ids"] = _normalize_allowed_product_ids(merged.get("allowed_product_ids"))
            return merged
        if not os.path.exists(MINIOPT_GENERATION_SETTINGS_FILE):
            return CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy()
        try:
            with open(MINIOPT_GENERATION_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy()
            merged = CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy()
            merged.update(data)
            merged["allowed_product_ids"] = _normalize_allowed_product_ids(merged.get("allowed_product_ids"))
            return merged
        except Exception:
            return CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy()

    @staticmethod
    def _save_miniopt_generation_settings(settings: dict) -> bool:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(MINIOPT_GENERATION_SETTINGS_DOC_KEY, settings)
            return True
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(MINIOPT_GENERATION_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    @staticmethod
    def get_allowed_miniopt_generation_product_ids() -> set[str]:
        settings = CatalogService._load_miniopt_generation_settings()
        return set(_normalize_allowed_product_ids(settings.get("allowed_product_ids")))

    @staticmethod
    def set_allowed_miniopt_generation_product_ids(product_ids: list[str]) -> bool:
        settings = CatalogService._load_miniopt_generation_settings()
        settings["allowed_product_ids"] = _normalize_allowed_product_ids(product_ids)
        return CatalogService._save_miniopt_generation_settings(settings)

    @staticmethod
    def toggle_allowed_miniopt_generation_product_id(product_id: str) -> bool:
        pid = (product_id or "").strip()
        if not pid:
            return False
        current = CatalogService.get_allowed_miniopt_generation_product_ids()
        if pid in current:
            current.remove(pid)
        else:
            current.add(pid)
        return CatalogService.set_allowed_miniopt_generation_product_ids(sorted(current))

    @staticmethod
    def reset_miniopt_generation_product_filter() -> bool:
        return CatalogService.set_allowed_miniopt_generation_product_ids([])

    @staticmethod
    def _apply_miniopt_generation_product_filter(
        generated_products: list[dict],
        rng: random.Random,
        population: int,
    ) -> list[dict]:
        allowed_ids = CatalogService.get_allowed_miniopt_generation_product_ids()
        if not allowed_ids:
            return generated_products

        filtered = [p for p in generated_products if p.get("id") in allowed_ids]
        if filtered:
            return filtered

        allowed_pool = [p for p in CatalogService.MINIOPT_ALL_PRODUCTS if p.get("id") in allowed_ids]
        if not allowed_pool:
            return generated_products

        min_take, max_take = _get_miniopt_generation_range(population)
        picked_ids = _pick_random_product_ids_for_pool(allowed_pool, rng, min_take, max_take)
        return [p for p in CatalogService.MINIOPT_ALL_PRODUCTS if p.get("id") in picked_ids]

    @staticmethod
    def _load_miniopt_cache():
        if PostgresDocumentStore.is_enabled():
            cache = PostgresDocumentStore.get_document(MINIOPT_PRODUCTS_CACHE_DOC_KEY, {})
            if not isinstance(cache, dict):
                return {}
            product_ids = {
                item.get("id")
                for item in CatalogService.get_all_miniopt_products()
                if isinstance(item, dict) and item.get("id")
            }
            cache, changed = _purge_invalid_city_cache_entries_for_ids(cache, product_ids)
            if changed:
                CatalogService._write_miniopt_cache(cache)
            return cache
        if not os.path.exists(CatalogService.MINIOPT_PRODUCTS_CACHE_FILE):
            return {}
        try:
            with open(CatalogService.MINIOPT_PRODUCTS_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            product_ids = {
                item.get("id")
                for item in CatalogService.get_all_miniopt_products()
                if isinstance(item, dict) and item.get("id")
            }
            cache, changed = _purge_invalid_city_cache_entries_for_ids(cache, product_ids)
            if changed:
                CatalogService._write_miniopt_cache(cache)
            return cache
        except Exception:
            return {}

    @staticmethod
    def _write_miniopt_cache(cache: dict, *, force: bool = False):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(MINIOPT_PRODUCTS_CACHE_DOC_KEY, cache, force=force)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.MINIOPT_PRODUCTS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving miniopt products cache: {e}")

    @staticmethod
    def _save_miniopt_to_cache(city_name: str, product_ids: list):
        cache = CatalogService._load_miniopt_cache()
        city_key = _normalize_city_key(city_name)
        stash_map = {pid: CatalogService.get_miniopt_product_stash_types(pid) for pid in product_ids}
        cache[city_key] = {
            "ids": product_ids,
            "stash_types": stash_map,
            "updated_at": get_now_msk().strftime("%d.%m.%Y %H:%M")
        }
        CatalogService._write_miniopt_cache(cache)

    @staticmethod
    def _update_miniopt_cache_stash_types(product_id: str, stash_types: list[str]):
        cache = CatalogService._load_miniopt_cache()
        changed = False
        for city_key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if isinstance(ids, list) and product_id in ids:
                stash = entry.get("stash_types")
                if not isinstance(stash, dict):
                    stash = {}
                if stash.get(product_id) != stash_types:
                    stash[product_id] = stash_types
                    entry["stash_types"] = stash
                    changed = True
        if changed:
            CatalogService._write_miniopt_cache(cache, force=True)

    @staticmethod
    def _patch_miniopt_cache_stash_types(product_ids: list[str] | None = None):
        cache = CatalogService._load_miniopt_cache()
        id_filter = set(product_ids) if product_ids is not None else None
        changed = False
        for city_key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if not isinstance(ids, list):
                continue
            stash = entry.get("stash_types") if isinstance(entry.get("stash_types"), dict) else {}
            for pid in ids:
                if id_filter is not None and pid not in id_filter:
                    continue
                new_types = list(CatalogService.MINIOPT_PRODUCT_STASH_TYPES.get(pid) or [])
                if len(new_types) < 2:
                    continue
                if stash.get(pid) != new_types:
                    stash[pid] = new_types
                    changed = True
            if entry.get("stash_types") != stash:
                entry["stash_types"] = stash
                changed = True
        if changed:
            CatalogService._write_miniopt_cache(cache, force=True)

    @staticmethod
    def _refresh_miniopt_cache_stash_types():
        CatalogService._patch_miniopt_cache_stash_types(None)

    @staticmethod
    def add_miniopt_product_to_city(city_name: str, product_id: str):
        CatalogService.get_all_miniopt_products()
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_miniopt_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(source_key)

        if source_key and source_key != city_key and data is not None:
            cache[city_key] = data
            cache.pop(source_key, None)
            CatalogService._write_miniopt_cache(cache)

        ids = []
        if isinstance(data, dict):
            ids = data.get("ids", [])
        elif isinstance(data, list):
            ids = data

        if product_id not in ids:
            ids.append(product_id)
            ids.sort(key=lambda x: next((i for i, p in enumerate(CatalogService.MINIOPT_ALL_PRODUCTS) if p["id"] == x), 999))
            CatalogService._save_miniopt_to_cache(city_key, ids)
            return True
        return False

    @staticmethod
    def remove_miniopt_product_from_city(city_name: str, product_id: str):
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_miniopt_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(source_key)

        if source_key and source_key != city_key and data is not None:
            cache[city_key] = data
            cache.pop(source_key, None)
            CatalogService._write_miniopt_cache(cache)

        ids = []
        if isinstance(data, dict):
            ids = data.get("ids", [])
        elif isinstance(data, list):
            ids = data

        if product_id in ids:
            ids.remove(product_id)
            CatalogService._save_miniopt_to_cache(city_key, ids)
            return True
        return False

    @staticmethod
    def _load_miniopt_inventory():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(MINIOPT_INVENTORY_DOC_KEY, {})
            cleaned = CatalogService._normalize_miniopt_inventory_payload(data)
            if cleaned != data:
                CatalogService._save_miniopt_inventory(cleaned)
            return cleaned
        if not os.path.exists(CatalogService.MINIOPT_INVENTORY_FILE):
            return {}
        try:
            with open(CatalogService.MINIOPT_INVENTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cleaned = CatalogService._normalize_miniopt_inventory_payload(data)
            if cleaned != data:
                CatalogService._save_miniopt_inventory(cleaned)
            return cleaned
        except Exception:
            return {}

    @staticmethod
    def _save_miniopt_inventory(data):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(MINIOPT_INVENTORY_DOC_KEY, data)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.MINIOPT_INVENTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving miniopt inventory: {e}")

    @staticmethod
    def _normalize_miniopt_inventory_payload(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        cleaned_inventory = {}
        for city_name, product_map in data.items():
            city_key = _normalize_city_key(city_name)
            if not city_key or not isinstance(product_map, dict):
                continue

            city_districts = CatalogService._get_city_district_pool(city_name)
            cleaned_products = {}
            for product_id, districts in product_map.items():
                normalized = CatalogService._filter_explicit_product_districts(city_name, districts, city_districts)
                if normalized:
                    cleaned_products[str(product_id)] = normalized

            if cleaned_products:
                cleaned_inventory[city_key] = cleaned_products

        return cleaned_inventory

    @staticmethod
    def get_available_districts_for_miniopt_product(city_name: str, product_id: str, all_districts: list[str] | None = None) -> list[str]:
        explicit_districts = CatalogService.get_miniopt_product_districts(city_name, product_id)
        if explicit_districts:
            return explicit_districts
        return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)

    @staticmethod
    def resync_miniopt_product_districts(force_rebalance: bool = False) -> bool:
        data = CatalogService._load_miniopt_inventory()
        if not data:
            return False

        changed = False
        for city_name, product_map in list(data.items()):
            if not isinstance(product_map, dict):
                continue
            city_districts = CatalogService._get_city_district_pool(city_name)
            for product_id, districts in list(product_map.items()):
                if force_rebalance:
                    normalized = CatalogService._pick_auto_product_districts(
                        city_name,
                        str(product_id),
                        city_districts,
                        previous_selection=districts,
                        force_change=True,
                    )
                else:
                    normalized = CatalogService._normalize_product_districts(city_name, str(product_id), districts, city_districts)

                if normalized:
                    if normalized != districts:
                        product_map[str(product_id)] = normalized
                        changed = True
                else:
                    if str(product_id) in product_map:
                        product_map.pop(str(product_id), None)
                        changed = True

            if not product_map:
                data.pop(city_name, None)
                changed = True

        if changed:
            CatalogService._save_miniopt_inventory(data)
        return changed

    @staticmethod
    def set_miniopt_product_districts(city_name: str, product_id: str, districts: list):
        data = CatalogService._load_miniopt_inventory()
        city_key = _normalize_city_key(city_name)

        source_key = _resolve_legacy_city_key(data, city_name)
        if source_key and source_key != city_key and source_key in data:
            data[city_key] = data.get(source_key, {})
            data.pop(source_key, None)

        if city_key not in data:
            data[city_key] = {}

        normalized = CatalogService._filter_explicit_product_districts(city_name, districts)
        data[city_key][product_id] = normalized
        CatalogService._save_miniopt_inventory(data)

    @staticmethod
    def get_miniopt_product_districts(city_name: str, product_id: str):
        data = CatalogService._load_miniopt_inventory()
        city_key = _resolve_legacy_city_key(data, city_name)

        if city_key in data and product_id in data[city_key]:
            normalized = CatalogService._filter_explicit_product_districts(city_name, data[city_key][product_id])
            if normalized != data[city_key][product_id]:
                data[city_key][product_id] = normalized
                CatalogService._save_miniopt_inventory(data)
            return normalized or None
        return None

    @staticmethod
    def force_refresh_miniopt_products(city_name: str):
        cache = CatalogService._load_miniopt_cache()
        city_key = _resolve_legacy_city_key(cache, city_name)
        if city_key in cache:
            del cache[city_key]
            try:
                CatalogService._write_miniopt_cache(cache)
                return True
            except Exception:
                pass
        return False

    @staticmethod
    def get_last_miniopt_update_time(city_name: str):
        cache = CatalogService._load_miniopt_cache()
        city_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(city_key)
        if isinstance(data, dict):
            return data.get("updated_at")
        return None

    @staticmethod
    def get_miniopt_products_for_city(city_name: str, population: int = 0, region_name: str | None = None):
        CatalogService.get_all_miniopt_products()
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_miniopt_cache()

        source_key = _resolve_legacy_city_key(cache, city_name)
        cache_data = cache.get(source_key)

        if source_key and source_key != city_key and cache_data is not None:
            cache[city_key] = cache_data
            cache.pop(source_key, None)
            CatalogService._write_miniopt_cache(cache)

        if isinstance(cache_data, list):
            saved_ids = cache_data
            cached_stash = {}
        elif isinstance(cache_data, dict):
            saved_ids = cache_data.get("ids", [])
            cached_stash = cache_data.get("stash_types", {}) if isinstance(cache_data.get("stash_types"), dict) else {}
        else:
            saved_ids = None
            cached_stash = {}

        if saved_ids:
            current_products = [p for p in CatalogService.MINIOPT_ALL_PRODUCTS if p["id"] in saved_ids]
        else:
            price_seed = _load_price_seed()
            rng = random.Random(f"miniopt::{city_key}::{price_seed}")
            print(f"[CatalogSeed] Генерирую miniopt для '{city_name}' с сидом '{price_seed}'")

            allowed_ids = CatalogService.get_allowed_miniopt_generation_product_ids()
            candidate_pool = CatalogService.MINIOPT_ALL_PRODUCTS
            if allowed_ids:
                allowed_pool = [p for p in candidate_pool if p.get("id") in allowed_ids]
                if allowed_pool:
                    candidate_pool = allowed_pool

            min_take, max_take = _get_miniopt_generation_range(population)
            picked_ids = _pick_random_product_ids_for_pool(candidate_pool, rng, min_take, max_take)
            current_products = [p for p in CatalogService.MINIOPT_ALL_PRODUCTS if p.get("id") in picked_ids]
            current_products = CatalogService._apply_miniopt_generation_product_filter(current_products, rng, population)
            saved_ids = [p["id"] for p in current_products]
            CatalogService._save_miniopt_to_cache(city_key, saved_ids)
            cached_stash = {pid: CatalogService.get_miniopt_product_stash_types(pid) for pid in saved_ids}

        display_list = []
        multiplier = CatalogService._get_city_multiplier(city_name, region_name)
        miniopt_factor = CatalogService._get_miniopt_factor_for_multiplier(multiplier)
        has_miniopt_discount = miniopt_factor < 1.0

        for product in current_products:
            price = _round_catalog_price(product["base_price"] * multiplier * miniopt_factor)
            display_list.append({
                "id": product["id"],
                "name": product["name"],
                "price": price,
                "desc": (
                    f"Мини-опт для г. {city_name} со скидкой 15%"
                    if has_miniopt_discount
                    else f"Мини-опт для г. {city_name}"
                ),
                "emoji": CatalogService.get_product_emoji(product["id"], product["name"]),
                "stash_types": cached_stash.get(product["id"]) or CatalogService.get_miniopt_product_stash_types(product["id"])
            })

        return display_list

    @staticmethod
    def get_miniopt_product_by_id(p_id, city_name=None, region_name: str | None = None):
        CatalogService.get_all_miniopt_products()
        for product in CatalogService.MINIOPT_ALL_PRODUCTS:
            if product["id"] == p_id:
                price = product["base_price"]
                has_miniopt_discount = False
                if city_name:
                    multiplier = CatalogService._get_city_multiplier(city_name, region_name)
                    miniopt_factor = CatalogService._get_miniopt_factor_for_multiplier(multiplier)
                    has_miniopt_discount = miniopt_factor < 1.0
                    price = _round_catalog_price(price * multiplier * miniopt_factor)

                return {
                    "id": product["id"],
                    "name": product["name"],
                    "price": price,
                    "desc": "Мини-опт, цены со скидкой 15%" if has_miniopt_discount else "Мини-опт",
                    "emoji": CatalogService.get_product_emoji(product["id"], product["name"]),
                    "stash_types": CatalogService.get_miniopt_product_stash_types(product["id"]),
                }
        return None

    @staticmethod
    def _load_stash_registry():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CATALOG_STASH_REGISTRY_DOC_KEY, list(DEFAULT_STASH_TYPES))
            if isinstance(data, list):
                cleaned = _clean_stash_types(data)
                if len(cleaned) >= 2:
                    CatalogService.STASH_TYPES = cleaned
                    return CatalogService.STASH_TYPES
            CatalogService.STASH_TYPES = list(DEFAULT_STASH_TYPES)
            return CatalogService.STASH_TYPES
        if os.path.exists(CatalogService.STASH_TYPES_REGISTRY_FILE):
            try:
                with open(CatalogService.STASH_TYPES_REGISTRY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    cleaned = _clean_stash_types(data)
                    if len(cleaned) >= 2:
                        CatalogService.STASH_TYPES = cleaned
                        if cleaned != data:
                            CatalogService._save_stash_registry()
                        return CatalogService.STASH_TYPES
            except Exception:
                pass
        CatalogService.STASH_TYPES = list(DEFAULT_STASH_TYPES)
        return CatalogService.STASH_TYPES

    @staticmethod
    def _save_stash_registry():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(
                CATALOG_STASH_REGISTRY_DOC_KEY,
                list(CatalogService.STASH_TYPES),
                force=True,
            )
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.STASH_TYPES_REGISTRY_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.STASH_TYPES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving stash registry: {e}")

    @staticmethod
    def _load_stash_types():
        CatalogService._load_stash_registry()
        if CatalogService.PRODUCT_STASH_TYPES and not PostgresDocumentStore.is_enabled():
            return CatalogService.PRODUCT_STASH_TYPES
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CATALOG_STASH_TYPES_DOC_KEY, {})
        elif not os.path.exists(CatalogService.STASH_TYPES_FILE):
            CatalogService.PRODUCT_STASH_TYPES = {}
            CatalogService._save_stash_types()
            return CatalogService.PRODUCT_STASH_TYPES
        else:
            try:
                with open(CatalogService.STASH_TYPES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        try:
            if isinstance(data, dict):
                cleaned = {}
                changed = False
                for key, value in data.items():
                    if not isinstance(value, list):
                        changed = True
                        continue
                    cleaned_list = [t for t in _clean_stash_types(value) if t in _get_stash_pool()]
                    if len(cleaned_list) >= 2:
                        normalized_pair = cleaned_list[:2]
                        cleaned[key] = normalized_pair
                        if value != normalized_pair:
                            changed = True
                    else:
                        changed = True
                CatalogService.PRODUCT_STASH_TYPES = cleaned
                if changed and not PostgresDocumentStore.is_enabled():
                    CatalogService._save_stash_types()
            else:
                CatalogService.PRODUCT_STASH_TYPES = {}
        except Exception:
            CatalogService.PRODUCT_STASH_TYPES = {}
        return CatalogService.PRODUCT_STASH_TYPES

    @staticmethod
    def _save_stash_types():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(
                CATALOG_STASH_TYPES_DOC_KEY,
                CatalogService.PRODUCT_STASH_TYPES,
                force=True,
            )
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.STASH_TYPES_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.PRODUCT_STASH_TYPES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving stash types: {e}")

    @staticmethod
    def get_available_stash_types() -> list[str]:
        CatalogService._load_stash_registry()
        return list(CatalogService.STASH_TYPES)

    @staticmethod
    def set_available_stash_types(values: list[str], *, repair_products: bool = True) -> bool:
        CatalogService._load_stash_registry()
        cleaned = _clean_stash_types(values)
        if len(cleaned) < 2:
            return False
        CatalogService.STASH_TYPES = cleaned
        CatalogService._save_stash_registry()
        if repair_products:
            CatalogService.resync_product_stash_types(force_rebalance=False)
        return CatalogService.get_available_stash_types() == cleaned

    @staticmethod
    def resync_product_stash_types(force_rebalance: bool = False):
        """Align product stash pairs with the current global pool and refresh caches."""
        CatalogService.get_all_products()
        changed_ids = CatalogService._ensure_stash_for_all_products(force_rebalance=force_rebalance)
        if force_rebalance or changed_ids:
            CatalogService._patch_cache_stash_types(None if force_rebalance else changed_ids)

    @staticmethod
    def add_stash_type(value: str) -> bool:
        if not isinstance(value, str):
            return False
        CatalogService._load_stash_registry()
        value = _normalize_stash_type(value)
        if not value:
            return False
        current = CatalogService.get_available_stash_types()
        if any(str(item).casefold() == value.casefold() for item in current):
            return False
        current.append(value)
        return CatalogService.set_available_stash_types(current, repair_products=False)

    @staticmethod
    def remove_stash_type(value: str) -> bool:
        CatalogService._load_stash_registry()
        value = _normalize_stash_type(value)
        current = CatalogService.get_available_stash_types()
        if value not in current:
            return False
        if len(current) <= 2:
            return False
        current = [item for item in current if item != value]
        return CatalogService.set_available_stash_types(current)

    @staticmethod
    def _ensure_stash_pair(product_id: str, force_rebalance: bool = False):
        CatalogService._load_stash_types()
        current = CatalogService.PRODUCT_STASH_TYPES.get(product_id)
        normalized_current = [item for item in _clean_stash_types(current, limit=2) if item in _get_stash_pool()]
        if not force_rebalance and len(normalized_current) >= 2:
            if current != normalized_current:
                CatalogService.PRODUCT_STASH_TYPES[product_id] = normalized_current
                CatalogService._save_stash_types()
            return
        price_seed = _load_price_seed()
        rng = random.Random(f"stash::{product_id}::{price_seed}")
        CatalogService.PRODUCT_STASH_TYPES[product_id] = _pick_default_stash_types(rng)
        CatalogService._save_stash_types()

    @staticmethod
    def _ensure_stash_for_all_products(force_rebalance: bool = False) -> list[str]:
        CatalogService._load_stash_types()
        changed_ids: list[str] = []
        CatalogService._load_stash_registry()
        price_seed = _load_price_seed()
        for product in CatalogService.ALL_PRODUCTS:
            p_id = product.get("id") if isinstance(product, dict) else None
            if not p_id:
                continue
            current = CatalogService.PRODUCT_STASH_TYPES.get(p_id)
            normalized_current = [item for item in _clean_stash_types(current, limit=2) if item in _get_stash_pool()]
            if force_rebalance or len(normalized_current) < 2:
                rng = random.Random(f"stash::{p_id}::{price_seed}")
                CatalogService.PRODUCT_STASH_TYPES[p_id] = _pick_default_stash_types(rng)
                changed_ids.append(p_id)
            elif current != normalized_current:
                CatalogService.PRODUCT_STASH_TYPES[p_id] = normalized_current
                changed_ids.append(p_id)
        if changed_ids:
            CatalogService._save_stash_types()
        return changed_ids
    
    @staticmethod
    def _load_all_products():
        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.get_document(CATALOG_ALL_PRODUCTS_DOC_KEY, _copy_default_products())
            normalized = _normalize_products_payload(payload)
            CatalogService.ALL_PRODUCTS = normalized or _copy_default_products()
        elif os.path.exists(CatalogService.PRODUCTS_DB_FILE):
            try:
                with open(CatalogService.PRODUCTS_DB_FILE, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                normalized = _normalize_products_payload(payload)
                CatalogService.ALL_PRODUCTS = normalized or _copy_default_products()
            except Exception:
                CatalogService.ALL_PRODUCTS = _copy_default_products()
        else:
            CatalogService.ALL_PRODUCTS = _copy_default_products()
        CatalogService._ensure_core_products()
        CatalogService._ensure_stash_for_all_products()

    @staticmethod
    def _save_all_products():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_ALL_PRODUCTS_DOC_KEY, CatalogService.ALL_PRODUCTS)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.PRODUCTS_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.ALL_PRODUCTS, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving all products: {e}")

    @staticmethod
    def _ensure_core_products():
        """Make sure essential items are always present in the catalog file."""
        core_defaults = [
  {
    "id": "p1",
    "name": "0.55г Hash | 40% Frappuccino, EAGLE",
    "base_price": 2100
  },
  {
    "id": "p4",
    "name": "1.0г Hash | 50% Frappuccino, EAGLE ",
    "base_price": 3000
  },
  {
    "id": "p6",
    "name": "0.5г ★ Мефедрон VHQ Crystall ★ 💎 ",
    "base_price": 2550
  },
  {
    "id": "p7",
    "name": "0.8г ★ Мефедрон VHQ Crystall ★ 💎 ",
    "base_price": 3350
  },
  {
    "id": "p10",
    "name": "1.0г ★ Мефедрон VHQ Crystall ★ 💎 ",
    "base_price": 4350
  },
  {
    "id": "p8",
    "name": "0.55г A-PVP | Crystals ",
    "base_price": 2550
  },
  {
    "id": "p9",
    "name": "1.0г A-PVP | Crystals ",
    "base_price": 4250
  },
  {
    "id": "p11",
    "name": "2.0г A-PVP | Crystals ",
    "base_price": 6600
  },
  {
    "id": "p15",
    "name": "0.55г A-PVP | Мука",
    "base_price": 2300
  },
  {
    "id": "p17",
    "name": "1.0г A-PVP | Мука",
    "base_price": 4000
  },
  {
    "id": "p18",
    "name": "2.0г A-PVP | Мука",
    "base_price": 6000
  },
  {
    "id": "p23",
    "name": "1.0г ★Бoшки HQ AK - 47★ ",
    "base_price": 3500
  },
  {
    "id": "p24",
    "name": "0.55г ★Бoшки HQ AK - 47★ ",
    "base_price": 2500
  },
  {
    "id": "p25",
    "name": "0.25г ★ Метадон ★ ",
    "base_price": 2700
  },
  {
    "id": "p26",
    "name": "0.5г ★ Метадон ★ ",
    "base_price": 3700
  },
  {
    "id": "p27",
    "name": "1.0г ★ Метадон ★ ",
    "base_price": 6000
  }
]

        if not isinstance(CatalogService.ALL_PRODUCTS, list):
            CatalogService.ALL_PRODUCTS = []

        existing_ids = {item.get("id") for item in CatalogService.ALL_PRODUCTS if isinstance(item, dict)}
        by_id = {
            item.get("id"): item
            for item in CatalogService.ALL_PRODUCTS
            if isinstance(item, dict) and item.get("id")
        }
        order_map = {item["id"]: idx for idx, item in enumerate(core_defaults)}
        changed = False
        for item in core_defaults:
            existing = by_id.get(item["id"])
            if existing is None:
                CatalogService.ALL_PRODUCTS.append(item)
                changed = True
                continue

            if existing.get("name") != item["name"] or int(existing.get("base_price", 0)) != item["base_price"]:
                existing["name"] = item["name"]
                existing["base_price"] = item["base_price"]
                changed = True

        if changed:
            fallback_positions = {
                item.get("id"): idx
                for idx, item in enumerate(CatalogService.ALL_PRODUCTS)
                if isinstance(item, dict) and item.get("id")
            }

            def _sort_key(prod):
                pid = prod.get("id") if isinstance(prod, dict) else None
                if pid in order_map:
                    return order_map[pid]
                return len(order_map) + fallback_positions.get(pid, 9999)

            CatalogService.ALL_PRODUCTS.sort(key=_sort_key)
            CatalogService._save_all_products()

    @staticmethod
    def get_product_emoji(product_id: str, name: str | None = None) -> str:
        emoji = CatalogService.PRODUCT_EMOJIS.get(product_id)
        if emoji:
            return emoji

        lookup = (name or "").lower()
        keyword_map = (
            ("шиш", "🍁"),
            ("гаш", "🧱"),
            ("меф", "❄️"),
            ("крис", "💎"),
            ("кокс", "🥥"),
            ("экстази", "✨"),
            ("амф", "💥"),
            ("мёд", "🍯"),
            ("шок", "⚡"),
        )
        for marker, symbol in keyword_map:
            if marker in lookup:
                return symbol
        return "🛒"

    @staticmethod
    def get_product_stash_types(product_id: str) -> list[str]:
        CatalogService.get_all_products()
        CatalogService._ensure_stash_pair(product_id)
        stash = CatalogService.PRODUCT_STASH_TYPES.get(product_id, [])
        if isinstance(stash, list) and len(stash) >= 2:
            return stash[:2]
        return _pick_default_stash_types()

    @staticmethod
    def get_district_stash_type_map(city_name: str, product_id: str, districts: list[str] | None) -> dict[str, list[str]]:
        cleaned_districts = _clean_district_values(districts)
        if not cleaned_districts:
            return {}

        pool = CatalogService.get_available_stash_types()
        combinations = _build_stash_combinations(pool)
        if not combinations:
            fallback = _pick_default_stash_types()
            return {district: fallback for district in cleaned_districts}

        district_seed = _load_price_seed()
        rng = random.Random(f"district-stash::{_normalize_city_key(city_name)}::{product_id}::{district_seed}")
        shuffled = list(combinations)
        rng.shuffle(shuffled)

        mapping = {}
        for index, district in enumerate(cleaned_districts):
            mapping[district] = list(shuffled[index % len(shuffled)])
        return mapping

    @staticmethod
    def get_district_stash_types(city_name: str, product_id: str, district_name: str, districts: list[str] | None = None) -> list[str]:
        district_map = CatalogService.get_district_stash_type_map(city_name, product_id, districts or [district_name])
        selected = district_map.get((district_name or "").strip())
        if isinstance(selected, list) and len(selected) >= 2:
            return selected[:2]
        return _pick_default_stash_types()

    @staticmethod
    def set_product_stash_types(product_id: str, types: list[str]) -> bool:
        CatalogService.get_all_products()
        CatalogService._load_stash_registry()
        CatalogService._load_stash_types()
        cleaned = []
        for t in types:
            if isinstance(t, str) and t in CatalogService.STASH_TYPES and t not in cleaned:
                cleaned.append(t)
            if len(cleaned) == 2:
                break
        if len(cleaned) < 2:
            return False
        cleaned = cleaned[:2]
        CatalogService.PRODUCT_STASH_TYPES[product_id] = cleaned
        CatalogService._save_stash_types()
        CatalogService._update_cache_stash_types(product_id, cleaned)
        return True

    @staticmethod
    def _patch_cache_stash_types(product_ids: list[str] | None = None):
        cache = CatalogService._load_cache()
        id_filter = set(product_ids) if product_ids is not None else None
        changed = False
        for city_key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if not isinstance(ids, list):
                continue
            stash = entry.get("stash_types") if isinstance(entry.get("stash_types"), dict) else {}
            for pid in ids:
                if id_filter is not None and pid not in id_filter:
                    continue
                new_types = list(CatalogService.PRODUCT_STASH_TYPES.get(pid) or [])
                if len(new_types) < 2:
                    continue
                if stash.get(pid) != new_types:
                    stash[pid] = new_types
                    changed = True
            if entry.get("stash_types") != stash:
                entry["stash_types"] = stash
                changed = True
        if changed:
            CatalogService._write_cache(cache, force=True)

    @staticmethod
    def _refresh_cache_stash_types():
        CatalogService._patch_cache_stash_types(None)

    @staticmethod
    def create_new_product(name: str, price: int):
        # Генерируем ID
        CatalogService._load_all_products()
        new_id = f"p{random.randint(1000, 99999)}"
        # Проверяем уникальность
        while any(p['id'] == new_id for p in CatalogService.ALL_PRODUCTS):
            new_id = f"p{random.randint(1000, 99999)}"
            
        new_product = {
            "id": new_id,
            "name": name,
            "base_price": price,
        }
        CatalogService.ALL_PRODUCTS.append(new_product)
        CatalogService._save_all_products()
        CatalogService._ensure_stash_pair(new_id)
        return new_product

    @staticmethod
    def get_all_products():
        if PostgresDocumentStore.is_enabled() or not CatalogService.ALL_PRODUCTS:
            CatalogService._load_all_products()
        return CatalogService.ALL_PRODUCTS

    @staticmethod
    def _load_generation_settings() -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(
                CATALOG_GENERATION_SETTINGS_DOC_KEY,
                CatalogService.DEFAULT_GENERATION_SETTINGS.copy(),
            )
            if not isinstance(data, dict):
                return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged = CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged.update(data)
            raw_ids = merged.get("allowed_product_ids")
            if not isinstance(raw_ids, list):
                merged["allowed_product_ids"] = []
            else:
                merged["allowed_product_ids"] = [
                    str(pid).strip() for pid in raw_ids if isinstance(pid, str) and str(pid).strip()
                ]
            return merged
        if not os.path.exists(CATALOG_GENERATION_SETTINGS_FILE):
            return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
        try:
            with open(CATALOG_GENERATION_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged = CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged.update(data)
            raw_ids = merged.get("allowed_product_ids")
            if not isinstance(raw_ids, list):
                merged["allowed_product_ids"] = []
            else:
                merged["allowed_product_ids"] = [
                    str(pid).strip() for pid in raw_ids if isinstance(pid, str) and str(pid).strip()
                ]
            return merged
        except Exception:
            return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()

    @staticmethod
    def _save_generation_settings(settings: dict) -> bool:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_GENERATION_SETTINGS_DOC_KEY, settings)
            return True
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CATALOG_GENERATION_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    @staticmethod
    def get_allowed_generation_product_ids() -> set[str]:
        settings = CatalogService._load_generation_settings()
        raw_ids = settings.get("allowed_product_ids")
        if not isinstance(raw_ids, list):
            return set()
        return {str(pid).strip() for pid in raw_ids if isinstance(pid, str) and str(pid).strip()}

    @staticmethod
    def set_allowed_generation_product_ids(product_ids: list[str]) -> bool:
        settings = CatalogService._load_generation_settings()
        settings["allowed_product_ids"] = _normalize_allowed_product_ids(product_ids)
        return CatalogService._save_generation_settings(settings)

    @staticmethod
    def toggle_allowed_generation_product_id(product_id: str) -> bool:
        pid = (product_id or "").strip()
        if not pid:
            return False
        current = CatalogService.get_allowed_generation_product_ids()
        if pid in current:
            current.remove(pid)
        else:
            current.add(pid)
        return CatalogService.set_allowed_generation_product_ids(sorted(current))

    @staticmethod
    def reset_generation_product_filter() -> bool:
        # Empty list means "all products allowed"
        return CatalogService.set_allowed_generation_product_ids([])

    @staticmethod
    def _apply_generation_product_filter(
        generated_products: list[dict],
        rng: random.Random,
        population: int,
    ) -> list[dict]:
        allowed_ids = CatalogService.get_allowed_generation_product_ids()
        if not allowed_ids:
            return generated_products

        filtered = [p for p in generated_products if p.get("id") in allowed_ids]
        if filtered:
            return filtered

        # If branch-specific pool doesn't intersect with allowed set, fallback to allowed pool.
        allowed_pool = [p for p in CatalogService.ALL_PRODUCTS if p.get("id") in allowed_ids]
        if not allowed_pool:
            return generated_products

        min_take, max_take = _get_filtered_generation_range(population)

        max_take = min(max_take, len(allowed_pool))
        min_take = min(min_take, max_take)
        if max_take <= 0:
            return generated_products

        take = rng.randint(min_take, max_take) if min_take < max_take else max_take
        picked = rng.sample(allowed_pool, k=take)
        return sorted(picked, key=lambda x: CatalogService.ALL_PRODUCTS.index(x))

    @staticmethod
    def _load_cache():
        if PostgresDocumentStore.is_enabled():
            cache = PostgresDocumentStore.get_document(CATALOG_PRODUCTS_CACHE_DOC_KEY, {})
            if not isinstance(cache, dict):
                return {}
            cache, changed = _purge_invalid_city_cache_entries(cache)
            if changed:
                CatalogService._write_cache(cache)
            return cache
        if not os.path.exists(PRODUCTS_CACHE_FILE):
            return {}
        try:
            with open(PRODUCTS_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            cache, changed = _purge_invalid_city_cache_entries(cache)
            if changed:
                CatalogService._write_cache(cache)
            return cache
        except Exception:
            return {}

    @staticmethod
    def _write_cache(cache: dict, *, force: bool = False):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_PRODUCTS_CACHE_DOC_KEY, cache, force=force)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.PRODUCTS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving products cache: {e}")

    @staticmethod
    def _save_to_cache(city_name: str, product_ids: list):
        cache = CatalogService._load_cache()
        city_key = _normalize_city_key(city_name)
        stash_map = {pid: CatalogService.get_product_stash_types(pid) for pid in product_ids}
        cache[city_key] = {
            "ids": product_ids,
            "stash_types": stash_map,
            "updated_at": get_now_msk().strftime("%d.%m.%Y %H:%M")
        }
        CatalogService._write_cache(cache)

    @staticmethod
    def _update_cache_stash_types(product_id: str, stash_types: list[str]):
        cache = CatalogService._load_cache()
        changed = False
        for city_key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if isinstance(ids, list) and product_id in ids:
                stash = entry.get("stash_types")
                if not isinstance(stash, dict):
                    stash = {}
                if stash.get(product_id) != stash_types:
                    stash[product_id] = stash_types
                    entry["stash_types"] = stash
                    changed = True
        if changed:
            CatalogService._write_cache(cache, force=True)

    @staticmethod
    def add_product_to_city(city_name: str, product_id: str):
        CatalogService.get_all_products() # Ensure loaded
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(source_key)

        if source_key and source_key != city_key and data is not None:
            cache[city_key] = data
            cache.pop(source_key, None)
            CatalogService._write_cache(cache)
        
        ids = []
        if isinstance(data, dict):
            ids = data.get("ids", [])
        elif isinstance(data, list):
            ids = data
            
        if product_id not in ids:
            ids.append(product_id)
            # Сортируем по порядку в общей базе
            ids.sort(key=lambda x: next((i for i, p in enumerate(CatalogService.ALL_PRODUCTS) if p["id"] == x), 999))
            CatalogService._save_to_cache(city_key, ids)
            return True
        return False

    @staticmethod
    def _load_inventory():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CATALOG_INVENTORY_DOC_KEY, {})
            cleaned = CatalogService._normalize_inventory_payload(data)
            if cleaned != data:
                CatalogService._save_inventory(cleaned)
            return cleaned
        if not os.path.exists(CatalogService.INVENTORY_FILE):
            return {}
        try:
            with open(CatalogService.INVENTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cleaned = CatalogService._normalize_inventory_payload(data)
            if cleaned != data:
                CatalogService._save_inventory(cleaned)
            return cleaned
        except:
            return {}

    @staticmethod
    def _save_inventory(data):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_INVENTORY_DOC_KEY, data)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.INVENTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving inventory: {e}")

    @staticmethod
    def _get_city_district_pool(city_name: str) -> list[str]:
        try:
            from app.services.geo import GeoService

            cache = GeoService._load_cache()
            target = _normalize_city_key(city_name)
            if not isinstance(cache, dict) or not target:
                return []

            matched = []
            for raw_key, districts in cache.items():
                if not isinstance(raw_key, str):
                    continue
                raw_norm = " ".join(raw_key.strip().lower().split())
                if raw_norm == target or raw_norm.startswith(target + " ("):
                    for district in _clean_district_values(districts):
                        if district not in matched:
                            matched.append(district)
            return matched
        except Exception:
            return []

    @staticmethod
    def _pick_auto_product_districts(
        city_name: str,
        product_id: str,
        all_districts: list[str] | None = None,
        previous_selection: list[str] | None = None,
        force_change: bool = False,
    ) -> list[str]:
        districts, dense = _prepare_product_district_pool(city_name, all_districts)
        if not districts:
            return []
        if len(districts) == 1:
            return districts

        district_seed = _load_price_seed()
        rng = random.Random(f"districts::{_normalize_city_key(city_name)}::{product_id}::{district_seed}")
        if dense:
            min_count = min(AUTO_PRODUCT_STREET_MIN, len(districts))
            max_count = min(AUTO_PRODUCT_STREET_MAX, len(districts))
            count_weights = {4: 3, 5: 4, 6: 3, 7: 2}
        else:
            min_count = min(AUTO_PRODUCT_DISTRICT_MIN, len(districts))
            max_count = min(AUTO_PRODUCT_DISTRICT_MAX, len(districts))
            count_weights = {1: 1, 2: 4, 3: 5, 4: 2, 5: 1}

        counts = list(range(min_count, max_count + 1))
        weights = [count_weights.get(value, 1) for value in counts]
        count = rng.choices(counts, weights=weights, k=1)[0]
        sampled = set(rng.sample(districts, count))
        selected = [district for district in districts if district in sampled]

        previous = [district for district in _clean_district_values(previous_selection) if district in districts]
        if force_change and previous and selected == previous and len(districts) > count:
            for attempt in range(1, len(districts) + 1):
                alt_rng = random.Random(
                    f"districts::{_normalize_city_key(city_name)}::{product_id}::{district_seed}::{attempt}"
                )
                alt_sampled = set(alt_rng.sample(districts, count))
                alternative = [district for district in districts if district in alt_sampled]
                if alternative != previous:
                    return alternative

        return selected

    @staticmethod
    def _filter_explicit_product_districts(
        city_name: str,
        districts: list[str] | None,
        all_districts: list[str] | None = None,
    ) -> list[str]:
        """Keep admin/explicit district picks as-is, dropping only unknown names."""
        city_districts, _dense = _prepare_product_district_pool(city_name, all_districts)
        selected = _clean_district_values(districts)
        if city_districts:
            selected = [district for district in selected if district in city_districts]
        return selected

    @staticmethod
    def _normalize_product_districts(
        city_name: str,
        product_id: str,
        districts: list[str] | None,
        all_districts: list[str] | None = None,
    ) -> list[str]:
        city_districts, dense = _prepare_product_district_pool(city_name, all_districts)
        selected = CatalogService._filter_explicit_product_districts(city_name, districts, all_districts)
        if not selected:
            return []
        if city_districts:
            if dense:
                if len(city_districts) >= AUTO_PRODUCT_STREET_MIN and len(selected) < AUTO_PRODUCT_STREET_MIN:
                    return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)
                if len(selected) > AUTO_PRODUCT_STREET_MAX or len(selected) == len(city_districts):
                    return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)
            else:
                if len(selected) > AUTO_PRODUCT_DISTRICT_MAX:
                    return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)
                if len(city_districts) >= AUTO_PRODUCT_DISTRICT_MAX and len(selected) == len(city_districts):
                    return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)
        return selected

    @staticmethod
    def _normalize_inventory_payload(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        cleaned_inventory = {}
        for city_name, product_map in data.items():
            city_key = _normalize_city_key(city_name)
            if not city_key or not isinstance(product_map, dict):
                continue

            city_districts = CatalogService._get_city_district_pool(city_name)
            cleaned_products = {}
            for product_id, districts in product_map.items():
                normalized = CatalogService._filter_explicit_product_districts(city_name, districts, city_districts)
                if normalized:
                    cleaned_products[str(product_id)] = normalized

            if cleaned_products:
                cleaned_inventory[city_key] = cleaned_products

        return cleaned_inventory

    @staticmethod
    def get_available_districts_for_product(city_name: str, product_id: str, all_districts: list[str] | None = None) -> list[str]:
        explicit_districts = CatalogService.get_product_districts(city_name, product_id)
        if explicit_districts:
            return explicit_districts
        return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)

    @staticmethod
    def resync_product_districts(force_rebalance: bool = False) -> bool:
        data = CatalogService._load_inventory()
        if not data:
            return False

        changed = False
        for city_name, product_map in list(data.items()):
            if not isinstance(product_map, dict):
                continue
            city_districts = CatalogService._get_city_district_pool(city_name)
            for product_id, districts in list(product_map.items()):
                if force_rebalance:
                    normalized = CatalogService._pick_auto_product_districts(
                        city_name,
                        str(product_id),
                        city_districts,
                        previous_selection=districts,
                        force_change=True,
                    )
                else:
                    normalized = CatalogService._normalize_product_districts(city_name, str(product_id), districts, city_districts)

                if normalized:
                    if normalized != districts:
                        product_map[str(product_id)] = normalized
                        changed = True
                else:
                    if str(product_id) in product_map:
                        product_map.pop(str(product_id), None)
                        changed = True

            if not product_map:
                data.pop(city_name, None)
                changed = True

        if changed:
            CatalogService._save_inventory(data)
        return changed

    @staticmethod
    def set_product_districts(city_name: str, product_id: str, districts: list):
        """Sets explicit availability of a product in specific districts."""
        data = CatalogService._load_inventory()
        city_key = _normalize_city_key(city_name)

        source_key = _resolve_legacy_city_key(data, city_name)
        if source_key and source_key != city_key and source_key in data:
            data[city_key] = data.get(source_key, {})
            data.pop(source_key, None)
        
        if city_key not in data:
            data[city_key] = {}

        normalized = CatalogService._filter_explicit_product_districts(city_name, districts)
        data[city_key][product_id] = normalized
        CatalogService._save_inventory(data)

    @staticmethod
    def get_product_districts(city_name: str, product_id: str):
        """Returns list of districts if explicitly set, else None."""
        data = CatalogService._load_inventory()
        city_key = _resolve_legacy_city_key(data, city_name)
        
        if city_key in data and product_id in data[city_key]:
            normalized = CatalogService._filter_explicit_product_districts(city_name, data[city_key][product_id])
            if normalized != data[city_key][product_id]:
                data[city_key][product_id] = normalized
                CatalogService._save_inventory(data)
            return normalized or None
        return None
        
    @staticmethod
    def remove_product_from_city(city_name: str, product_id: str):
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(source_key)

        if source_key and source_key != city_key and data is not None:
            cache[city_key] = data
            cache.pop(source_key, None)
            CatalogService._write_cache(cache)
        
        ids = []
        if isinstance(data, dict):
            ids = data.get("ids", [])
        elif isinstance(data, list):
            ids = data
            
        if product_id in ids:
            ids.remove(product_id)
            CatalogService._save_to_cache(city_key, ids)
            return True
        return False

    @staticmethod
    def get_week_day():
        return get_now_msk().weekday()

    @staticmethod
    def force_refresh_products(city_name: str):
        """Принудительно удаляет кэш для города, заставляя перегенерировать список"""
        cache = CatalogService._load_cache()
        city_key = _resolve_legacy_city_key(cache, city_name)
        if city_key in cache:
            del cache[city_key]
            try:
                CatalogService._write_cache(cache)
                return True
            except:
                pass
        return False

    @staticmethod
    def get_last_update_time(city_name: str):
        cache = CatalogService._load_cache()
        city_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(city_key)
        if isinstance(data, dict):
            return data.get("updated_at")
        return None

    @staticmethod
    def clear_entire_cache():
        """Полная очистка кэша товаров для всех городов."""
        # Генерируем новый сид - при следующей генерации прайс будет другим
        new_seed = str(random.randint(100000, 999999))
        _save_price_seed(new_seed)
        print(f"[CatalogSeed] Новый сид прайса: {new_seed}")
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_INVENTORY_DOC_KEY, {}, force=True)
            PostgresDocumentStore.set_document(CATALOG_STASH_TYPES_DOC_KEY, {}, force=True)
            PostgresDocumentStore.set_document(CATALOG_PRODUCTS_CACHE_DOC_KEY, {}, force=True)
            PostgresDocumentStore.set_document(MINIOPT_INVENTORY_DOC_KEY, {}, force=True)
            PostgresDocumentStore.set_document(MINIOPT_STASH_TYPES_DOC_KEY, {}, force=True)
            PostgresDocumentStore.set_document(MINIOPT_PRODUCTS_CACHE_DOC_KEY, {}, force=True)
            PostgresDocumentStore.set_document(MINIOPT_STASH_REGISTRY_DOC_KEY, list(DEFAULT_MINIOPT_STASH_TYPES), force=True)
            PostgresDocumentStore.set_document(MINIOPT_GENERATION_SETTINGS_DOC_KEY, CatalogService.MINIOPT_DEFAULT_GENERATION_SETTINGS.copy(), force=True)
            CatalogService.PRODUCT_STASH_TYPES = {}
            CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {}
            CatalogService.MINIOPT_STASH_TYPES = list(DEFAULT_MINIOPT_STASH_TYPES)
            inventory_status = "cleared"
            stash_status = "cleared"
            cache_status = "cleared"
            miniopt_inventory_status = "cleared"
            miniopt_stash_status = "cleared"
            miniopt_cache_status = "cleared"
        else:
            inventory_status = _remove_optional_file(CatalogService.INVENTORY_FILE)
            stash_status = _remove_optional_file(CatalogService.STASH_TYPES_FILE)
            cache_status = _remove_optional_file(PRODUCTS_CACHE_FILE)
            miniopt_inventory_status = _remove_optional_file(CatalogService.MINIOPT_INVENTORY_FILE)
            miniopt_stash_status = _remove_optional_file(CatalogService.MINIOPT_STASH_TYPES_FILE)
            miniopt_cache_status = _remove_optional_file(CatalogService.MINIOPT_PRODUCTS_CACHE_FILE)
            _remove_optional_file(CatalogService.MINIOPT_STASH_TYPES_REGISTRY_FILE)
            _remove_optional_file(MINIOPT_GENERATION_SETTINGS_FILE)
            CatalogService.PRODUCT_STASH_TYPES = {}
            CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {}
            CatalogService.MINIOPT_STASH_TYPES = list(DEFAULT_MINIOPT_STASH_TYPES)

        return (
            cache_status,
            inventory_status,
            stash_status,
            miniopt_cache_status,
            miniopt_inventory_status,
            miniopt_stash_status,
        )

    @staticmethod
    def _get_city_multiplier(city_name: str, region_name: str | None = None) -> float:
        cache_key = None
        region_label = " ".join((region_name or "").strip().split()) or None
        try:
            from app.services.geo import GeoService
            districts_cache = GeoService._load_cache()
            city_key_lower = _normalize_city_key(city_name)
            regional_cache_keys: list[tuple[str, str]] = []
            empty_region_cache_key = None
            plain_cache_key = None

            for k in districts_cache.keys():
                if not isinstance(k, str):
                    continue
                normalized_key = " ".join(k.strip().lower().split())
                if normalized_key == city_key_lower:
                    plain_cache_key = k
                    continue
                if normalized_key.startswith(city_key_lower + " ("):
                    start = k.find("(") + 1
                    end = k.rfind(")")
                    region_part = ""
                    if start > 0 and end > start:
                        region_part = k[start:end].strip()
                    if region_part:
                        regional_cache_keys.append((k, region_part))
                        continue
                    if empty_region_cache_key is None:
                        empty_region_cache_key = k

            if region_label and regional_cache_keys:
                matched = [item for item in regional_cache_keys if GeoService._region_matches(item[1], region_label)]
                if matched:
                    matched.sort(key=lambda item: item[0].lower())
                    cache_key, region_label = matched[0]

            if cache_key is None and not region_label and regional_cache_keys:
                regional_cache_keys.sort(key=lambda item: item[0].lower())
                cache_key, fallback_region = regional_cache_keys[0]
                region_label = fallback_region

            if cache_key is None:
                cache_key = plain_cache_key or empty_region_cache_key

            if not region_label and cache_key and "(" in cache_key and ")" in cache_key:
                start = cache_key.find("(") + 1
                end = cache_key.rfind(")")
                if start > 0 and end > start:
                    region_label = cache_key[start:end].strip()

            resolved = PricingService.get_multiplier(city_name, region_label, cache_key)
            if resolved:
                return float(resolved)

        except Exception as e:
            print(f"Multiplier error: {e}")

        return 1.0

    @staticmethod
    def _get_miniopt_factor_for_multiplier(multiplier: float) -> float:
        return MINIOPT_PRICE_FACTOR if float(multiplier or 1.0) > 1.0 else 1.0

    @staticmethod
    def city_has_markup(city_name: str, region_name: str | None = None) -> bool:
        return CatalogService._get_city_multiplier(city_name, region_name) > 1.0

    @staticmethod
    def get_existing_products_for_city(city_name: str, region_name: str | None = None):
        """Return the pinned city catalog without generating a new one."""
        CatalogService.get_all_products()
        cache = CatalogService._load_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        cache_data = cache.get(source_key)
        if isinstance(cache_data, list):
            saved_ids = cache_data
            cached_stash = {}
        elif isinstance(cache_data, dict):
            saved_ids = cache_data.get("ids", [])
            cached_stash = cache_data.get("stash_types", {}) if isinstance(cache_data.get("stash_types"), dict) else {}
        else:
            return []

        if not saved_ids:
            return []

        current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in saved_ids]
        multiplier = CatalogService._get_city_multiplier(city_name, region_name)
        display_list = []
        for p in current_products:
            price = int(p["base_price"] * multiplier)
            price = (price // 100) * 100
            display_list.append({
                "id": p["id"],
                "name": p["name"],
                "price": price,
                "desc": f"Цена для г. {city_name} (актуальна сегодня)",
                "emoji": CatalogService.get_product_emoji(p["id"], p["name"]),
                "stash_types": cached_stash.get(p["id"]) or CatalogService.get_product_stash_types(p["id"]),
            })
        return display_list

    @staticmethod
    def get_products_for_city(city_name: str, population: int = 0, region_name: str | None = None):
        """
        Возвращает закрепленный за городом список товаров.
        Если список не создан - генерирует случайный набор и закрепляет его.
        population uses to filter luxury items for small cities (< 1 mln).
        """
        CatalogService.get_all_products() # Ensure loaded
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_cache()

        source_key = _resolve_legacy_city_key(cache, city_name)
        cache_data = cache.get(source_key)

        if source_key and source_key != city_key and cache_data is not None:
            cache[city_key] = cache_data
            cache.pop(source_key, None)
            CatalogService._write_cache(cache)
        
        # Поддержка старого формата кэша (просто список ID) и нового (dict)
        if isinstance(cache_data, list):
            saved_ids = cache_data
            cached_stash = {}
        elif isinstance(cache_data, dict):
            saved_ids = cache_data.get("ids", [])
            cached_stash = cache_data.get("stash_types", {}) if isinstance(cache_data.get("stash_types"), dict) else {}
        else:
            saved_ids = None
            cached_stash = {}
        
        if saved_ids:
            # Если есть в кэше - собираем объекты по ID
            current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in saved_ids]
        else:
            # === ГЕНЕРАЦИЯ НОВОГО НАБОРА ===
            price_seed = _load_price_seed()
            rng = random.Random(f"{city_key}::{price_seed}")
            print(f"[CatalogSeed] Генерирую прайс для '{city_name}' с сидом '{price_seed}'")

            generation_tier = _get_catalog_generation_tier(population)

            if generation_tier == "compact":
                compact_city_ids = _build_compact_city_product_ids(rng)
                current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in compact_city_ids]

            elif generation_tier == "small":
                small_city_ids = _build_small_city_product_ids(rng)
                current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in small_city_ids]

            elif generation_tier == "medium":
                medium_ids = _build_non_small_city_product_ids(rng)
                current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in medium_ids]
                current_products = sorted(current_products, key=lambda x: CatalogService.ALL_PRODUCTS.index(x))

            else:
                large_ids = _build_non_small_city_product_ids(rng)
                current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in large_ids]
                current_products = sorted(current_products, key=lambda x: CatalogService.ALL_PRODUCTS.index(x))
            
            current_products = CatalogService._apply_generation_product_filter(
                current_products,
                rng,
                population,
            )

            
            # Сохраняем ID-шники в кэш
            saved_ids = [p["id"] for p in current_products]
            CatalogService._save_to_cache(city_key, saved_ids)
            cached_stash = {pid: CatalogService.get_product_stash_types(pid) for pid in saved_ids}

        display_list = []
        multiplier = CatalogService._get_city_multiplier(city_name, region_name)
        
        for p in current_products:
            price = int(p["base_price"] * multiplier)
            price = (price // 100) * 100
            display_list.append({
                "id": p["id"],
                "name": p["name"],
                "price": price,
                "desc": f"Цена для г. {city_name} (актуальна сегодня)",
                "emoji": CatalogService.get_product_emoji(p["id"], p["name"]),
                "stash_types": cached_stash.get(p["id"]) or CatalogService.get_product_stash_types(p["id"])
            })
            
        return CatalogService._with_courier_job(display_list)

    @staticmethod
    def is_courier_job(product) -> bool:
        if isinstance(product, dict):
            return (
                str(product.get("id") or "") == COURIER_JOB_ID
                or product.get("kind") == "courier_job"
            )
        return str(product or "") == COURIER_JOB_ID

    @staticmethod
    def get_courier_job_product() -> dict:
        return {
            "id": COURIER_JOB_ID,
            "name": COURIER_JOB_NAME,
            "price": COURIER_JOB_PRICE,
            "desc": "Вакансия курьера",
            "emoji": CatalogService.get_product_emoji(COURIER_JOB_ID, COURIER_JOB_NAME),
            "stash_types": [],
            "kind": "courier_job",
        }

    @staticmethod
    def _with_courier_job(products: list[dict] | None) -> list[dict]:
        return [
            item
            for item in (products or [])
            if isinstance(item, dict) and str(item.get("id") or "") != COURIER_JOB_ID
        ]

    @staticmethod
    def get_product_by_id(p_id, city_name=None, region_name: str | None = None):
        if CatalogService.is_courier_job(p_id):
            return CatalogService.get_courier_job_product()
        CatalogService.get_all_products() # Load if needed
        # Ищем в общей базе
        for p in CatalogService.ALL_PRODUCTS:
            if p["id"] == p_id:
                price = p["base_price"]
                if city_name:
                    multiplier = CatalogService._get_city_multiplier(city_name, region_name)
                    price = int(price * multiplier)
                    # Round to 100
                    price = (price // 100) * 100
                    
                return {
                    "id": p["id"],
                    "name": p["name"],
                    "price": price,
                    "desc": "Актуальный прайс",
                    "emoji": CatalogService.get_product_emoji(p["id"], p["name"]),
                    "stash_types": CatalogService.get_product_stash_types(p["id"]),
                }
        return None

