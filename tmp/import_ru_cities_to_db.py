from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import DATABASE_URL
from app.services.catalog import CatalogService
from app.services.geo import (
    GEO_CITY_POPULATION_DOC_KEY,
    GEO_DISTRICTS_CACHE_DOC_KEY,
    GeoService,
)
from app.services.postgres_store import PostgresDocumentStore

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

STREET_SEARCH_RADIUS_METERS = 15000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Russian cities >= min population into Postgres geo caches from OSM/Overpass.",
    )
    parser.add_argument("--min-population", type=int, default=25000)
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--delay-seconds", type=float, default=0.4)
    parser.add_argument("--flush-every", type=int, default=10)
    parser.add_argument("--city", action="append", default=[], help="Import specific city names in addition to OSM batch list")
    parser.add_argument("--skip-existing", action="store_true", default=False)
    parser.add_argument("--only-existing", action="store_true", default=False, help="Update only cities that already exist in DB geo caches")
    parser.add_argument("--warm-catalog", action="store_true", default=False)
    parser.add_argument("--allow-fallback", action="store_true", default=False)
    parser.add_argument("--street-limit", type=int, default=20, help="Max streets per city when districts are unavailable")
    return parser.parse_args()


def _parse_population(value) -> int:
    return GeoService._parse_population_value(value)


def _clean_region(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _build_cache_key(city_name: str, region_name: str = "") -> str:
    city_key = GeoService._normalize_city_name(city_name)
    region_key = " ".join(_clean_region(region_name).lower().split())
    if city_key and region_key:
        return f"{city_key} ({region_key})"
    return city_key


def _request_overpass(query: str, timeout_seconds: int = 180) -> dict:
    headers = {
        "User-Agent": "bot-city-import/1.0",
        "Accept": "application/json",
    }
    last_error = None
    for url in OVERPASS_URLS:
        try:
            response = requests.post(url, data={"data": query}, headers=headers, timeout=timeout_seconds)
            if response.status_code != 200:
                last_error = RuntimeError(f"{url} returned HTTP {response.status_code}")
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                last_error = RuntimeError(f"{url} returned non-dict JSON")
                continue
            return payload
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"Overpass request failed: {last_error}")
    raise RuntimeError("Overpass request failed without details")


def _extract_region_from_tags(tags: dict) -> str:
    candidates = [
        tags.get("addr:region"),
        tags.get("is_in:state"),
        tags.get("addr:state"),
        tags.get("official_name:region"),
        tags.get("addr:province"),
        tags.get("is_in:province"),
    ]
    for candidate in candidates:
        cleaned = _clean_region(candidate)
        if cleaned:
            return cleaned
    return ""


def _extract_city_center(element: dict) -> tuple[float | None, float | None]:
    if isinstance(element.get("center"), dict):
        center = element["center"]
        return center.get("lat"), center.get("lon")
    return element.get("lat"), element.get("lon")


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lon)
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360.0) % 360.0


def _is_district_like(value: str) -> bool:
    text = " ".join((value or "").strip().lower().split())
    if not text:
        return False
    return any(marker in text for marker in ("район", "округ", "мкр", "микрорайон", "квартал"))


def _is_street_like(value: str) -> bool:
    text = " ".join((value or "").strip().lower().split())
    if not text:
        return False
    return any(
        marker in text
        for marker in ("ул.", "улица", "пр-т", "проспект", "пер.", "переулок", "пр-д", "проезд", "бул.", "бульвар", "ш.", "шоссе")
    )


def _has_major_districts(values: list[str] | None) -> bool:
    if not isinstance(values, list) or not values:
        return False
    return any(_is_district_like(item) for item in values if isinstance(item, str))


def _is_street_only(values: list[str] | None) -> bool:
    if not isinstance(values, list) or not values:
        return False
    if _has_major_districts(values):
        return False
    street_count = sum(1 for item in values if isinstance(item, str) and _is_street_like(item))
    return street_count >= max(3, len(values) // 2)


def _normalize_street_name(raw_name: str) -> str:
    clean = " ".join((raw_name or "").split())
    if not clean:
        return ""
    lower = clean.lower()

    if lower.startswith("улица "):
        clean = "ул. " + clean[6:].strip()
    elif lower.endswith(" улица"):
        clean = "ул. " + clean[:-6].strip()
    elif lower.startswith("проспект "):
        clean = "пр-т " + clean[9:].strip()
    elif lower.endswith(" проспект"):
        clean = "пр-т " + clean[:-9].strip()
    elif lower.startswith("переулок "):
        clean = "пер. " + clean[9:].strip()
    elif lower.endswith(" переулок"):
        clean = "пер. " + clean[:-9].strip()
    elif lower.startswith("проезд "):
        clean = "пр-д " + clean[7:].strip()
    elif lower.startswith("бульвар "):
        clean = "бул. " + clean[8:].strip()
    elif lower.startswith("шоссе "):
        clean = "ш. " + clean[6:].strip()
    elif not any(x in lower for x in ["ул.", "пер.", "пр.", "мкр", "район", "парк", "сквер"]):
        if lower not in ["центр", "вокзал", "набережная", "парк"]:
            clean = "ул. " + clean

    clean = clean.replace("ул.", "ул. ").replace("  ", " ").strip().rstrip(" .,;:-")
    return clean


def _fetch_streets_with_limit(city_name: str, region_name: str, street_limit: int) -> list[str]:
    limit = max(1, int(street_limit))

    default_streets = GeoService._fetch_streets_overpass(city_name, region_name)
    if len(default_streets) >= limit or limit <= 7:
        return default_streets[:limit]

    q_str = f"{city_name} {region_name}".strip()
    params = {"q": q_str, "format": "json", "limit": 1}
    data_nom = GeoService._request_json_with_retry(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        timeout=12,
        retries=2,
    )
    if not data_nom:
        return default_streets[:limit]

    city_data = data_nom[0]
    lat = city_data.get("lat")
    lon = city_data.get("lon")
    if lat is None or lon is None:
        return default_streets[:limit]

    query = (
        f"""
        [out:json][timeout:25];
        way(around:{STREET_SEARCH_RADIUS_METERS},{lat},{lon})["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|service"]["name"];
        out tags center qt 300;
        """
    )

    try:
        payload = _request_overpass(query)
    except Exception:
        payload = {}

    street_points: dict[str, tuple[float, float]] = {}
    center_lat = float(lat)
    center_lon = float(lon)
    elements = payload.get("elements") or []
    for element in elements:
        if not isinstance(element, dict):
            continue
        name = ((element.get("tags") or {}).get("name") or "").strip()
        if not name or "—" in name or " км" in name:
            continue

        normalized = _normalize_street_name(name)
        if not normalized or normalized.lower() in {"центр", "за городом"}:
            continue
        if normalized[0].isdigit():
            continue

        center = element.get("center")
        if not isinstance(center, dict):
            continue
        c_lat = center.get("lat")
        c_lon = center.get("lon")
        if c_lat is None or c_lon is None:
            continue

        try:
            point_lat = float(c_lat)
            point_lon = float(c_lon)
            distance = _distance_km(center_lat, center_lon, point_lat, point_lon)
        except Exception:
            continue

        if distance > 15.0:
            continue

        bearing = _bearing_deg(center_lat, center_lon, point_lat, point_lon)
        current = street_points.get(normalized)
        if current is None or distance < current[0]:
            street_points[normalized] = (distance, bearing)

    if not street_points:
        return default_streets[:limit]

    sector_count = 8
    sector_width = 360.0 / sector_count
    sectors: list[list[tuple[str, float]]] = [[] for _ in range(sector_count)]
    for street_name, (distance, bearing) in street_points.items():
        idx = int(bearing // sector_width) % sector_count
        sectors[idx].append((street_name, distance))

    for bucket in sectors:
        bucket.sort(key=lambda item: (item[1], item[0]))

    selected: list[str] = []
    while len(selected) < limit:
        progressed = False
        for bucket in sectors:
            if not bucket:
                continue
            street_name, _ = bucket.pop(0)
            if street_name in selected:
                continue
            selected.append(street_name)
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break

    if not selected:
        ordered = [name for name, _ in sorted(street_points.items(), key=lambda item: (item[1][0], item[0]))]
        return ordered[:limit]

    return selected[:limit]


def _fetch_city_elements(min_population: int) -> list[dict]:
    query = f"""
    [out:json][timeout:600];
    area["ISO3166-1"="RU"][admin_level=2]->.ru;
    (
      node["place"~"city|town"]["population"](area.ru);
      way["place"~"city|town"]["population"](area.ru);
      relation["place"~"city|town"]["population"](area.ru);
    );
    out tags center;
    """
    payload = _request_overpass(query)
    elements = payload.get("elements") or []
    result: dict[str, dict] = {}

    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        city_name = " ".join(str(tags.get("name") or "").split())
        if not city_name:
            continue
        population = _parse_population(tags.get("population"))
        if population < min_population:
            continue
        region_name = _extract_region_from_tags(tags)
        cache_key = _build_cache_key(city_name, region_name)
        if not cache_key:
            continue
        lat, lon = _extract_city_center(element)
        current = result.get(cache_key)
        candidate = {
            "name": city_name,
            "region": region_name,
            "population": population,
            "type": str(tags.get("place") or "city"),
            "osm_id": element.get("id"),
            "osm_type": element.get("type"),
            "lat": lat,
            "lon": lon,
        }
        if current is None or population > current.get("population", 0):
            result[cache_key] = candidate

    regionful_by_name = {
        GeoService._normalize_city_name(item["name"])
        for item in result.values()
        if item.get("region")
    }
    cities = [
        item
        for item in result.values()
        if item.get("region") or GeoService._normalize_city_name(item["name"]) not in regionful_by_name
    ]
    cities = sorted(cities, key=lambda item: (-item["population"], item["name"], item["region"]))
    return cities


def _fetch_specific_city(city_name: str, min_population: int) -> list[dict]:
    candidates = []
    try:
        search_result = asyncio.run(GeoService.search_cities(city_name))
    except Exception as exc:
        print(f"[import] specific city search failed city='{city_name}' err='{exc}'")
        return []
    for candidate in search_result or []:
        if not isinstance(candidate, dict):
            continue
        population = _parse_population(candidate.get("population"))
        if population < min_population:
            continue
        candidates.append({
            "name": str(candidate.get("name") or "").strip(),
            "region": _clean_region(candidate.get("region") or ""),
            "population": population,
            "type": str(candidate.get("type") or "city"),
            "osm_id": (candidate.get("raw_data") or {}).get("osm_id"),
            "osm_type": (candidate.get("raw_data") or {}).get("osm_type"),
            "lat": (candidate.get("raw_data") or {}).get("lat"),
            "lon": (candidate.get("raw_data") or {}).get("lon"),
        })
    return candidates


def _fetch_districts_or_streets(city: dict, allow_fallback: bool, street_limit: int) -> list[str]:
    city_name = city["name"]
    region_name = city.get("region", "")

    districts = GeoService._fetch_districts_overpass(city_name, region_name)
    if districts:
        return GeoService._filter_best_districts(districts)

    # Retry district fetch with a less strict region hint before falling back to streets.
    if region_name:
        districts = GeoService._fetch_districts_overpass(city_name, "")
        if districts:
            return GeoService._filter_best_districts(districts)

    streets = _fetch_streets_with_limit(city_name, region_name, street_limit)
    if streets:
        return streets

    if allow_fallback:
        return ["Центр", "За городом"]
    return []


def _flush_docs(population_cache: dict, districts_cache: dict) -> None:
    PostgresDocumentStore.set_document(GEO_CITY_POPULATION_DOC_KEY, population_cache)
    PostgresDocumentStore.set_document(GEO_DISTRICTS_CACHE_DOC_KEY, districts_cache)


def _load_existing_doc(key: str) -> dict:
    data = PostgresDocumentStore.get_document(key, {})
    return data if isinstance(data, dict) else {}


def _build_existing_cities_from_docs(population_cache: dict, districts_cache: dict) -> list[dict]:
    keys = set()
    for key, value in (districts_cache or {}).items():
        if isinstance(key, str) and isinstance(value, list):
            keys.add(key)
    for key, value in (population_cache or {}).items():
        if isinstance(key, str) and isinstance(value, dict):
            keys.add(key)

    cities = []
    for key in sorted(keys):
        name, region = GeoService._parse_cache_city_key(key)
        if not name:
            continue
        population = 0
        cached_pop = (population_cache.get(key) or {}).get("population") if isinstance(population_cache.get(key), dict) else 0
        population = _parse_population(cached_pop)
        cities.append(
            {
                "name": name,
                "region": region,
                "population": population,
                "type": "city",
                "osm_id": None,
                "osm_type": None,
                "lat": None,
                "lon": None,
            }
        )

    cities.sort(key=lambda item: (-item["population"], item["name"], item["region"]))
    return cities


def main() -> int:
    args = _parse_args()

    if not DATABASE_URL:
        print("DATABASE_URL is required for this import script")
        return 1

    PostgresDocumentStore.initialize()

    population_cache = _load_existing_doc(GEO_CITY_POPULATION_DOC_KEY)
    districts_cache = _load_existing_doc(GEO_DISTRICTS_CACHE_DOC_KEY)

    if args.only_existing:
        print("[import] building city list from existing DB geo docs...")
        cities = _build_existing_cities_from_docs(population_cache, districts_cache)
        if args.city:
            target_names = {GeoService._normalize_city_name(name) for name in args.city if isinstance(name, str)}
            cities = [
                item
                for item in cities
                if GeoService._normalize_city_name(item.get("name", "")) in target_names
            ]
    else:
        print("[import] fetching city list from OSM/Overpass...")
        cities = _fetch_city_elements(args.min_population)

    if args.city and not args.only_existing:
        extra_candidates = []
        for city_name in args.city:
            extra_candidates.extend(_fetch_specific_city(city_name, args.min_population))
        existing_keys = {_build_cache_key(item["name"], item.get("region", "")) for item in cities}
        for item in extra_candidates:
            key = _build_cache_key(item["name"], item.get("region", ""))
            if key and key not in existing_keys:
                cities.append(item)
                existing_keys.add(key)
        cities.sort(key=lambda item: (-item["population"], item["name"], item["region"]))

    if args.offset:
        cities = cities[args.offset :]
    if args.limit > 0:
        cities = cities[: args.limit]

    if args.only_existing and not cities:
        print("[import] no existing cities found in DB geo docs")

    print(f"[import] cities queued: {len(cities)}")

    processed = 0
    imported = 0
    skipped = 0
    failed = 0
    pending_flush = 0

    for index, city in enumerate(cities, start=1):
        city_name = city["name"]
        region_name = city.get("region", "")
        population = city["population"]
        cache_key = _build_cache_key(city_name, region_name)

        if not cache_key:
            skipped += 1
            continue

        has_population = isinstance(population_cache.get(cache_key), dict)
        has_districts = isinstance(districts_cache.get(cache_key), list) and bool(districts_cache.get(cache_key))
        if args.skip_existing and has_population and has_districts:
            skipped += 1
            print(f"[{index}/{len(cities)}] skip existing: {city_name} ({region_name or 'no-region'})")
            continue

        try:
            locations = _fetch_districts_or_streets(
                city,
                allow_fallback=args.allow_fallback,
                street_limit=args.street_limit,
            )
            if not locations:
                failed += 1
                print(f"[{index}/{len(cities)}] no districts/streets: {city_name} ({region_name or 'no-region'})")
                continue

            existing_locations = districts_cache.get(cache_key)
            # Do not overwrite known district-based cities with street-only results.
            if _has_major_districts(existing_locations) and _is_street_only(locations):
                skipped += 1
                print(
                    f"[{index}/{len(cities)}] keep existing districts: {city_name} ({region_name or 'no-region'}) "
                    f"existing={len(existing_locations)} new_streets={len(locations)}"
                )
                continue

            population_cache[cache_key] = {"population": int(population)}
            districts_cache[cache_key] = locations

            if args.warm_catalog:
                CatalogService.get_products_for_city(city_name, population)

            imported += 1
            processed += 1
            pending_flush += 1
            print(
                f"[{index}/{len(cities)}] imported: {city_name} ({region_name or 'no-region'}) "
                f"pop={population} places={len(locations)}"
            )

            if pending_flush >= max(1, args.flush_every):
                _flush_docs(population_cache, districts_cache)
                pending_flush = 0

            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
        except KeyboardInterrupt:
            print("[import] interrupted, flushing current progress...")
            if pending_flush:
                _flush_docs(population_cache, districts_cache)
            raise
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(cities)}] failed: {city_name} ({region_name or 'no-region'}) err='{exc}'")

    if pending_flush:
        _flush_docs(population_cache, districts_cache)

    print(
        "[import] done: "
        f"queued={len(cities)} imported={imported} skipped={skipped} failed={failed} processed={processed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())