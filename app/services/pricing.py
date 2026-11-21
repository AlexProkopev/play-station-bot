import json
import os
from typing import Any, Dict, List, Optional
from app.services.postgres_store import PostgresDocumentStore

PRICING_FILE = os.path.join("storage", "region_pricing.json")
PRICING_DOC_KEY = "region_pricing"


class PricingService:
    _cache: Optional[Dict[str, Any]] = None

    @staticmethod
    def _normalize(value: str) -> str:
        cleaned = value.lower().strip()
        cleaned = cleaned.replace("ё", "е")
        cleaned = cleaned.replace("—", "-").replace("–", "-")
        cleaned = " ".join(cleaned.split())
        return cleaned

    @staticmethod
    def _ensure_defaults() -> Dict[str, Any]:
        default_regions: List[Dict[str, Any]] = [
            {"label": "Ханты-Мансийский автономный округ - Югра", "value": 1.5},
            {"label": "Забайкальский край", "value": 1.3},
            {"label": "Сахалинская область", "value": 1.8},
            {"label": "Алтайский край", "value": 1.3},
            {"label": "Республика Алтай", "value": 1.3},
            {"label": "Кабардино-Балкарская Республика", "value": 1.4},
            {"label": "Красноярский край", "value": 1.3},
            {"label": "Новосибирская область", "value": 1.3},
            {"label": "Иркутская область", "value": 1.3},
            {"label": "Республика Саха (Якутия)", "value": 1.3},
        ]

        regions: Dict[str, Any] = {}
        for item in default_regions:
            key = PricingService._normalize(item["label"])
            regions[key] = {"label": item["label"], "value": item["value"]}

        return {"regions": regions, "cities": {}}

    @staticmethod
    def _normalize_payload(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = PricingService._ensure_defaults()
        if "regions" not in data or "cities" not in data:
            base = PricingService._ensure_defaults()
            base["regions"] = data.get("regions", base["regions"]) if isinstance(data, dict) else base["regions"]
            base["cities"] = data.get("cities", base["cities"]) if isinstance(data, dict) else base["cities"]
            data = base

        for bucket in ("regions", "cities"):
            raw = data.get(bucket, {})
            clean: Dict[str, Any] = {}
            if isinstance(raw, dict):
                for key, value in raw.items():
                    norm_key = PricingService._normalize(str(key))
                    if isinstance(value, dict):
                        label = value.get("label") or key
                        numeric = float(value.get("value", 1.0) or 1.0)
                    else:
                        label = key
                        numeric = float(value or 1.0)
                    clean[norm_key] = {"label": label, "value": numeric}
            data[bucket] = clean
        return data

    @staticmethod
    def _load() -> Dict[str, Any]:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(PRICING_DOC_KEY, PricingService._ensure_defaults())
            return PricingService._normalize_payload(data)

        if PricingService._cache is not None:
            return PricingService._cache

        if not os.path.exists(os.path.dirname(PRICING_FILE)):
            os.makedirs(os.path.dirname(PRICING_FILE), exist_ok=True)

        if not os.path.exists(PRICING_FILE):
            data = PricingService._ensure_defaults()
            PricingService._save(data)
            return data

        try:
            with open(PRICING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = PricingService._ensure_defaults()
            PricingService._save(data)
            return data

        data = PricingService._normalize_payload(data)
        PricingService._cache = data
        return data

    @staticmethod
    def _save(data: Dict[str, Any]) -> None:
        payload = PricingService._normalize_payload(data)
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(PRICING_DOC_KEY, payload, force=True)
            PricingService._cache = None
            return

        if not os.path.exists(os.path.dirname(PRICING_FILE)):
            os.makedirs(os.path.dirname(PRICING_FILE), exist_ok=True)

        with open(PRICING_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        PricingService._cache = payload

    @staticmethod
    def invalidate_cache() -> None:
        PricingService._cache = None

    @staticmethod
    def _extract_value(entry: Optional[Dict[str, Any]]) -> Optional[float]:
        if not entry:
            return None
        numeric = float(entry.get("value", 0) or 0)
        if numeric <= 0:
            return None
        return numeric

    @staticmethod
    def get_region_entries() -> List[Dict[str, Any]]:
        data = PricingService._load()
        entries = []
        for key, entry in data.get("regions", {}).items():
            entries.append({
                "key": key,
                "label": entry.get("label") or key,
                "value": float(entry.get("value", 1.0) or 1.0)
            })
        entries.sort(key=lambda item: item["label"].lower())
        return entries

    @staticmethod
    def set_region_multiplier(label: str, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("Multiplier must be positive")

        data = PricingService._load()
        key = PricingService._normalize(label)
        data.setdefault("regions", {})[key] = {"label": label, "value": float(multiplier)}
        PricingService._save(data)

    @staticmethod
    def clear_region_multiplier(label_or_key: str) -> None:
        data = PricingService._load()
        key = PricingService._normalize(label_or_key)
        if key in data.get("regions", {}):
            del data["regions"][key]
            PricingService._save(data)

    @staticmethod
    def get_multiplier(
        city_name: Optional[str] = None,
        region_name: Optional[str] = None,
        cache_key: Optional[str] = None
    ) -> Optional[float]:
        data = PricingService._load()

        def match_bucket(bucket: str, target: Optional[str]) -> Optional[float]:
            if not target:
                return None
            norm = PricingService._normalize(target)
            if not norm:
                return None
            items = data.get(bucket, {})
            exact = items.get(norm)
            if exact:
                value = PricingService._extract_value(exact)
                if value:
                    return value

            best_value = None
            best_len = 0
            for key, entry in items.items():
                entry_label = PricingService._normalize(entry.get("label") or "")
                for candidate in (key, entry_label):
                    if not candidate or len(candidate) < 8:
                        continue
                    if candidate == norm or candidate in norm or (len(norm) >= 8 and norm in candidate):
                        value = PricingService._extract_value(entry)
                        if value and len(candidate) > best_len:
                            best_value = value
                            best_len = len(candidate)
            return best_value

        city_value = match_bucket("cities", city_name)
        if city_value:
            return city_value

        region_value = match_bucket("regions", region_name)
        if region_value:
            return region_value

        if cache_key:
            key_value = match_bucket("regions", cache_key)
            if key_value:
                return key_value

        return None

    @staticmethod
    def set_city_multiplier(label: str, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("Multiplier must be positive")

        data = PricingService._load()
        key = PricingService._normalize(label)
        data.setdefault("cities", {})[key] = {"label": label, "value": float(multiplier)}
        PricingService._save(data)

    @staticmethod
    def clear_city_multiplier(label_or_key: str) -> None:
        data = PricingService._load()
        key = PricingService._normalize(label_or_key)
        if key in data.get("cities", {}):
            del data["cities"][key]
            PricingService._save(data)

    @staticmethod
    def get_city_entries() -> List[Dict[str, Any]]:
        data = PricingService._load()
        entries = []
        for key, entry in data.get("cities", {}).items():
            entries.append({
                "key": key,
                "label": entry.get("label") or key,
                "value": float(entry.get("value", 1.0) or 1.0)
            })
        entries.sort(key=lambda item: item["label"].lower())
        return entries