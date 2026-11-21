import html
import json
import os
import random
import re
import asyncio
import logging
import threading
import secrets
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from time import perf_counter
from app.config import OPENAI_API_KEY
from app.services.catalog import CatalogService
from app.services.geo import GeoService
from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService

# Попробуем импортировать openai
try:
    from openai import AsyncOpenAI
    has_openai = True
except ImportError:
    has_openai = False


logger = logging.getLogger("app.services.reviews")

AI_REVIEW_CONCURRENCY = 8
AI_REQUEST_TIMEOUT_SEC = 20.0
AI_MAX_RETRIES = 1


class ReviewsService:
    FILE_PATH = "storage/reviews.json"
    SETTINGS_FILE = "storage/review_settings.json"
    ACTIVE_CITIES_FILE = "storage/active_cities.json"
    PROMPTS_FILE = "storage/review_prompts.json"
    LAST_BATCH_FILE = "storage/reviews_last_batch.json"
    REVIEWS_DOC_KEY = "reviews"
    SETTINGS_DOC_KEY = "review_settings"
    ACTIVE_CITIES_DOC_KEY = "active_cities"
    PROMPTS_DOC_KEY = "review_prompts"
    LAST_BATCH_DOC_KEY = "reviews_last_batch"
    HIDDEN_USERNAME = "Скрыто"
    DISTRICT_HINTS = {
        "октябрьский",
        "ленинский",
        "советский",
        "центральный",
        "кировский",
        "заводской",
        "индустриальный",
        "комсомольский",
        "московский",
        "свердловский",
        "фрунзенский",
        "первореченский",
        "железнодорожный",
        "приокский",
        "пролетарский",
        "калининский",
        "краснооктябрьский",
        "дворцовый",
        "новоильинский",
        "заречный",
    }
    
    DEFAULT_SETTINGS = {
        "interval_hours": 12,
        "min_reviews": 5,
        "max_reviews": 10
    }

    @staticmethod
    def _normalize_city_key(value: str | None) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = text.replace("ё", "е")
        text = re.sub(r"\(.*?\)", "", text)
        text = re.sub(r"\bг\.?\s+", "", text)
        text = re.sub(r"\bгород\s+", "", text)
        text = re.sub(r"[^\w\s-]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _city_name_and_region(value: str | None) -> tuple[str, str]:
        raw = str(value or "").strip()
        if not raw:
            return "", ""
        name, region = GeoService._split_city_region_key(raw)
        return ReviewsService._normalize_city_key(name or raw), str(region or "").strip()

    @staticmethod
    def _region_from_key(raw_key: str | None) -> str:
        _, region = ReviewsService._city_name_and_region(raw_key)
        return region

    @staticmethod
    def format_city_region_label(city: str | None, region: str | None = None) -> str:
        city_name, embedded = GeoService._split_city_region_key(str(city or "").strip())
        city_name = city_name or str(city or "").strip()
        region_name = str(region or embedded or "").strip()
        if city_name and region_name and region_name.lower() not in city_name.lower():
            return f"{city_name} · {region_name}"
        return city_name

    @staticmethod
    def _normalize_district_name(value: str | None) -> str:
        text = str(value or "").strip().lower().replace("ё", "е")
        text = re.sub(r"[^\w\s-]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _homonym_regions(city_name: str | None) -> list[str]:
        """Regions of same-name cities stored in the districts cache."""
        cache = GeoService._load_cache()
        if not isinstance(cache, dict):
            return []
        target = ReviewsService._normalize_city_key(city_name)
        if not target:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for raw_key in cache.keys():
            if not isinstance(raw_key, str):
                continue
            name, region = ReviewsService._city_name_and_region(raw_key)
            if name != target or not region:
                continue
            token = GeoService._normalize_region_name(region)
            if not token or token in seen:
                continue
            seen.add(token)
            found.append(region)
        return found

    @staticmethod
    def _regions_for_district(city_name: str | None, district: str | None) -> list[str]:
        needle = ReviewsService._normalize_district_name(district)
        target = ReviewsService._normalize_city_key(city_name)
        if not needle or not target:
            return []
        cache = GeoService._load_cache()
        if not isinstance(cache, dict):
            return []
        found: list[str] = []
        seen: set[str] = set()
        for raw_key, districts in cache.items():
            if not isinstance(raw_key, str):
                continue
            name, region = ReviewsService._city_name_and_region(raw_key)
            if name != target or not region:
                continue
            token = GeoService._normalize_region_name(region)
            if not token or token in seen:
                continue
            names = districts if isinstance(districts, list) else []
            if any(ReviewsService._normalize_district_name(item) == needle for item in names):
                seen.add(token)
                found.append(region)
        return found

    @staticmethod
    def _resolve_review_region(review: dict, city_name: str | None = None) -> str:
        explicit = str((review or {}).get("region") or "").strip()
        if explicit:
            return explicit
        _, item_city, item_district = ReviewsService._split_item_info((review or {}).get("item_info"))
        for source in ((review or {}).get("city"), item_city):
            _, region = ReviewsService._city_name_and_region(source)
            if region:
                return region
        district = str((review or {}).get("district") or item_district or "").strip()
        regions = ReviewsService._regions_for_district(city_name or (review or {}).get("city"), district)
        if len(regions) == 1:
            return regions[0]
        return ""

    @staticmethod
    def _review_matches_place(
        review: dict,
        city_filter: str | None,
        region_filter: str | None = None,
        *,
        require_region: bool | None = None,
    ) -> bool:
        target_name, embedded_region = ReviewsService._city_name_and_region(city_filter)
        target_region = str(region_filter or embedded_region or "").strip()
        if city_filter and not target_name:
            return False
        if not target_name:
            return True

        _, item_city, _ = ReviewsService._split_item_info((review or {}).get("item_info"))
        names = []
        for value in ((review or {}).get("city"), item_city):
            name, _ = ReviewsService._city_name_and_region(value)
            if name:
                names.append(name)
        if target_name not in names:
            return False

        if require_region is None:
            require_region = bool(target_region) and len(ReviewsService._homonym_regions(target_name)) > 1
        if not require_region:
            return True
        review_region = ReviewsService._resolve_review_region(review, target_name)
        if not review_region:
            return False
        return GeoService._region_matches(review_region, target_region)

    @staticmethod
    def _city_targets(products_cache: dict, districts_cache: dict) -> list[tuple[str, str]]:
        """Pair each catalog city with every distinct region, not the first cache hit."""
        if not isinstance(products_cache, dict) or not isinstance(districts_cache, dict):
            return []
        pairs: list[tuple[str, str]] = []
        for p_key in products_cache:
            if not isinstance(p_key, str):
                continue
            matches = [
                d_key
                for d_key in districts_cache
                if isinstance(d_key, str) and ReviewsService._is_city_key_match(p_key, d_key)
            ]
            p_region = ReviewsService._region_from_key(p_key)
            if p_region:
                regional = [
                    d_key for d_key in matches
                    if ReviewsService._region_from_key(d_key)
                    and GeoService._region_matches(ReviewsService._region_from_key(d_key), p_region)
                ]
                plain = [d_key for d_key in matches if not ReviewsService._region_from_key(d_key)]
                chosen = regional or plain or matches[:1]
            else:
                regional = [d_key for d_key in matches if ReviewsService._region_from_key(d_key)]
                chosen = regional or matches[:1]
            seen_regions: set[str] = set()
            for d_key in chosen:
                token = GeoService._normalize_region_name(ReviewsService._region_from_key(d_key)) or ""
                if token in seen_regions:
                    continue
                seen_regions.add(token)
                pairs.append((p_key, d_key))

        deduped: list[tuple[str, str]] = []
        seen_places: set[tuple[str, str]] = set()
        for p_key, d_key in pairs:
            city_name, _ = ReviewsService._city_name_and_region(p_key)
            region_name = GeoService._normalize_region_name(
                ReviewsService._region_from_key(d_key) or ReviewsService._region_from_key(p_key)
            )
            token = (city_name, region_name)
            if not city_name or token in seen_places:
                continue
            seen_places.add(token)
            deduped.append((p_key, d_key))
        return deduped

    @staticmethod
    def _normalize_product_key(value: str | None) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = text.replace("ё", "е")
        text = re.sub(r"\([^)]*\)", " ", text)
        text = re.sub(r"[^\w\s-]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _is_city_key_match(product_key: str | None, district_key: str | None) -> bool:
        prod_name, prod_region = ReviewsService._city_name_and_region(product_key)
        dist_name, dist_region = ReviewsService._city_name_and_region(district_key)
        if not prod_name or not dist_name:
            return False
        same_name = (
            prod_name == dist_name
            or dist_name.startswith(prod_name + " ")
            or prod_name.startswith(dist_name + " ")
        )
        if not same_name:
            return False
        if prod_region and dist_region and not GeoService._region_matches(prod_region, dist_region):
            return False
        return True

    _ALLOWED_SHORT_TOKENS = {
        "и", "в", "к", "с", "я", "а", "у", "о", "на", "не", "но", "да", "ок",
    }
    _CHOPPED_TOKENS = {
        "ск", "будт", "прсто", "качк", "точн", "ровн", "сни", "забр",
        "мест", "фасов", "кла", "кор",
    }

    @staticmethod
    def _is_invalid_ai_text(text: str | None) -> bool:
        if not text:
            return True
        compact = text.strip()
        if not compact:
            return True
        lowered = compact.lower()
        if "заглушка" in lowered:
            return True
        if ReviewsService._looks_truncated(compact):
            return True
        return False

    @staticmethod
    def _looks_truncated(text: str | None) -> bool:
        compact = str(text or "").strip()
        if not compact:
            return True
        if compact.endswith("-") or compact.endswith("—"):
            return True
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё]+", compact)
        if not tokens:
            return True
        last = tokens[-1].lower().replace("ё", "е")
        if last in ReviewsService._CHOPPED_TOKENS:
            return True
        if len(last) == 1 and last not in ReviewsService._ALLOWED_SHORT_TOKENS:
            return True
        return False

    @staticmethod
    def _filter_invalid_reviews(entries: list[dict], context: str) -> tuple[list[dict], int]:
        cleaned = []
        removed = 0
        for item in entries or []:
            if ReviewsService._is_invalid_ai_text(item.get("text")):
                removed += 1
                continue
            cleaned.append(item)
        if removed:
            logger.info(
                "[ReviewGen] Removed %s placeholder reviews during %s stage",
                removed,
                context,
            )
        return cleaned, removed

    @staticmethod
    def _normalize_review_text(text: str | None) -> str:
        if not text:
            return ""
        lowered = text.lower()
        cleaned = re.sub(r"[^\w\s]", " ", lowered)
        return " ".join(cleaned.split())

    @staticmethod
    def _collect_recent_texts(reviews: list[dict], limit: int = 400) -> list[str]:
        if not isinstance(reviews, list) or limit <= 0:
            return []
        norms: list[str] = []
        for item in reviews[-limit:]:
            if not isinstance(item, dict):
                continue
            normalized = ReviewsService._normalize_review_text(item.get("text"))
            if normalized:
                norms.append(normalized)
        return norms

    @staticmethod
    def _is_duplicate_text(
        candidate: str | None,
        existing_norms: set[str] | list[str],
        similar_threshold: float = 0.8,
    ) -> bool:
        if not candidate:
            return False
        normalized = ReviewsService._normalize_review_text(candidate)
        if not normalized:
            return False
        if isinstance(existing_norms, set):
            if normalized in existing_norms:
                return True
            pool = list(existing_norms)[:80]
        else:
            if normalized in existing_norms:
                return True
            pool = list(existing_norms[-80:])
        for other in pool:
            if not other or other == normalized:
                continue
            if SequenceMatcher(None, normalized, other).ratio() >= similar_threshold:
                return True
        return False

    @staticmethod
    def get_gen_settings():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(
                ReviewsService.SETTINGS_DOC_KEY,
                ReviewsService.DEFAULT_SETTINGS.copy(),
            )
            if not isinstance(data, dict):
                data = ReviewsService.DEFAULT_SETTINGS.copy()
            for k, v in ReviewsService.DEFAULT_SETTINGS.items():
                if k not in data:
                    data[k] = v
            return data
        if not os.path.exists(ReviewsService.SETTINGS_FILE):
             return ReviewsService.DEFAULT_SETTINGS.copy()
        try:
             with open(ReviewsService.SETTINGS_FILE, "r", encoding="utf-8") as f:
                 data = json.load(f)
                 # Ensure all keys exist
                 for k, v in ReviewsService.DEFAULT_SETTINGS.items():
                     if k not in data:
                         data[k] = v
                 return data
        except Exception:
             return ReviewsService.DEFAULT_SETTINGS.copy()

    @staticmethod
    def update_gen_settings(interval=None, min_r=None, max_r=None):
        settings = ReviewsService.get_gen_settings()
        if interval is not None: settings["interval_hours"] = float(interval)
        if min_r is not None: settings["min_reviews"] = int(min_r)
        if max_r is not None: settings["max_reviews"] = int(max_r)
        
        # Validation
        if settings["min_reviews"] > settings["max_reviews"]:
             settings["max_reviews"] = settings["min_reviews"]

        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.SETTINGS_DOC_KEY, settings)
            return True
             
        try:
            with open(ReviewsService.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            return True
        except Exception as e:
            print(f"Err saves settings: {e}")
            return False

    TEXTS = [
        "В касание, от души! 🤝",
        "Дома! Кура красава, место топ.",
        "Птичка в клетке. 10/10",
        "Пришлось покопаться, но все ровно.",
        "Кач пушка, по весу тоже норм.",
        "Съем в секунду, спасибо магазину.",
        "Все гуд, забегу еще.",
        "Место, конечно, дебри, но зато надежно. Снял.",
        "Ровно. Рекомендую.",
        "Всё по красоте, как всегда.",
        "Магазин ровный, беру не первый раз.",
        "Четко. Вес соответствует.",
        "Поднял без проблем.",
        "Корды точные, фото понятное. 5+",
        "Был ненаход в прошлый раз, дали пз, его забрал! Магазин честный.",
        "На изи, спс.",
        "Шкуроходы мимо, кладмен гений)",
        "Забрал, качество огонь 🔥",
        "Долго искал локацию, но по метке все четко.",
        "Магаз топчик, процветания!",
        "Всё супер, спасибо за работу.",
        "Квест легкий, стафф рабочий.",
        "Первый раз тут, все прошло гладко.",
        "Качество 10/10, вес 10/10, место 8/10.",
        "Все на месте, спасибо!",
        "Координаты четкие, забрал в касание.",
        "От души душевной, место тихое.",
        "По весу ровно, качество вышка.",
        "Накур плотный, доволен как слон 🐘",
        "Стафф 10 из 10, давно такого не курил.",
        "Стафф топ, спасибо команде!",
        "Товар зачет, качество порадовало.",
        "Качество огонь, всем советую! 🔥🔥🔥",
        "Мощно, одной хапки хватило.",
        "Убойная вещь, буду брать еще.",
        "Магазин держит марку, стафф отличный.",
        "Шишки ароматные, эффект бомба.",
        "Камень твердый, крошится хорошо, прет отлично.",
        "Криссы белоснежные, качество вышка.",
        
        # Более длинные и живые отзывы
        "Сложно было найти место. Поищу в следующий раз. Если лежит-найду.",
        "Молодцы Вы. Спасибо за проделанную работу. Всё как всегда на высоте. 10/10/10. Магазин рекомендую.",
        "Снят, через 30 минут после покупки. Качество не пробовал меня вряд-ли возьмет тк я курю каждый день…",
        "Магазин рекомендую, всегда на месте, качество неизменно, всегда вышка!",
        "В касание пришёл наклонился забрал Кура Красава качество не пробовал еще",
        "Место шкуроходное, но кура молодец, спрятал надежно. Подъем в касание.",
        "Пришел, увидел, победил. Вес ровный, качество бомба, буду брать ещё 🤝",
        "Не первый раз беру, всегда ровно. В этот раз пришлось поискать, но все на месте.",
        "От души! Клад в касание, стафф рабочий, настроение поднято.",
        "Всё чётко, как в аптеке. Снял за секунду, качество пушка, всем доволен.",
        "Спасибо команде за ровный движ. Место укромное, чайки мимо.",
        "Забрал без проблем. Упаковано надежно, по весу все ровно. Качество топчик 🔥",
        "Долго шел к месту, ноги промочил, но оно того стоило. Качество перекрывает все минусы.",
        "Координаты четкие, описание понятное. Подошел и забрал. Магазу респект.",
        "Всё дома! Спасибо за сервис, буду рекомендовать друзьям.",
    ]

    # Фразы для конструктора (для создания уникальных комбинаций)
    PHRASES_INTRO = [
        "В касание.", "Все дома.", "На месте.", "Поднял.", "Забрал.",
        "Съем быстрый.", "Квест пройден.", "Нашел сразу.", "Всё гуд.",
        "Дома.", "Изи квест.", "Снял.", "На базе."
    ]
    PHRASES_BODY = [
        "Вес ровный.", "Качество огонь.", "Кладмен красава.", "Место тихое.",
        "Упаковка надежная.", "Стафф рабочий.", "По весу четко.", "Корды точные.",
        "Камень мощный.", "Курнул и забыл.", "Проблем не возникло.", "Чайки мимо."
    ]
    PHRASES_OUTRO = [
        "Советую.", "10/10.", "Забегу еще.", "Магазу респект.", "Обнял.",
        "Рекомендую.", "Вернусь.", "Всем мир.", "Спасибо.", "От души.",
        "Магазин топ.", "Вы лучшие."
    ]

    PHRASES_EXTRA = [
        "Жаль только, что не живу рядом.",
        "Быстрее бы открыли ещё одно такое место.",
        "Мимо проходил, а тут такой кайф.",
        "После похода настроение как после праздника.",
        "Уровень сервиса завёл меня окончательно.",
        "Все в духе честного старого Витринаа.",
        "Эх, если бы у меня был такой кладу - не вылазил бы из дома.",
        "Отдал бы тимуренку, кто сделал метку, пятёрку.",
        "Отходняк не возьмет, все по делу.",
        "Сказали просто: бери и забывай - так и получилось."
    ]

    PRODUCT_ALIAS_CONFIG = [
        (("kriss", "крисс", "криss", "крiss"), ["крисс", "крис", "криссы", "крисы"]),
        (("шиш", "бошк", "og kush", "white widow"), ["шиш", "хашка", "бошки"]),
        (("меf", "mef", "меф", "мефедрон"), ["меф", "мефодий", "кристалл"]),
        (("метадон", "мёд", "мед"), ["мёд", "мед", "сладкий"])
    ]

    NAMES = [
        # --- Английские Ники ---
        "Alex", "Dmitry", "Sergey", "Andrey", "Maxim", "Ivan", 
        "Kirill", "Anton", "Stas", "Vlad", "Egor", "Nikita", "Ilya",
        "Artem", "Roman", "Vadan", "Misha", "Sanek", "Dimon",
        "Hottabych", "Joker", "Speedy", "Stalker", "Ghost", "Viper",
        "Kladmen", "Pikachu", "Demon", "Neo", "Morpheus", "Riddick",
        "Shaman", "Ninja", "User", "Gamer", "Pro", "Anon", "Dark",
        # --- Русские имена и клички ---
        "Саня", "Димон", "Леха", "Колян", "Серга", "Влад", "Артем",
        "Мишаня", "Жека", "Пашок", "Кирюха", "Макс", "Андрюха",
        "Стас", "Виталик", "Юрок", "Толян", "Серый", "Тема",
        "Никитос", "Илюха", "Ромыч", "Денчик", "Вадя", "Гриша",
        "Вован", "Славик", "Тимур", "Руслан", "Мага", "Ахмед",
        "Бодя", "Сеня", "Федя", "Гоша", "Ярик", "Даня",
        "Малой", "Лысый", "Рыжий", "Хмурый", "Дикий", "Четкий",
        "Бродяга", "Кот", "Заяц", "Волк", "Медведь", "Тигр",
        "Зверь", "Псих", "Док", "Кэп", "Шеф", "Босс", "Мастер",
        "Профессор", "Студент", "Повар", "Химик", "Гровер",
        "Танкист", "Летчик", "Моряк", "Дед", "Батя", "Брат"
    ]

    # База знаний о городах и их реальных районах/местах
    # Эту структуру можно расширять автоматически в будущем
    CITY_KNOWLEDGE = {
        "Москва": ["ЦАО", "Южное Бутово", "Басманный", "Химки", "Москва-Сити", "ВДНХ", "Парк Горького", "Таганка", "Чертаново"],
        "Санкт-Петербург": ["Думская", "Василеостровский", "Мурино", "Купчино", "Невский", "Лиговка", "Девяткино"],
        "Екатеринбург": ["Уралмаш", "Химмаш", "Ельцин Центр", "Вайнера", "Академический", "Сортировка"],
        "Новосибирск": ["Академгородок", "Ленинский", "Заельцовский", "Березовая роща", "Речной вокзал"],
        "Казань": ["Баумана", "Кремль", "Московский р-н", "Азино", "Квартала"],
        "Сочи": ["Адлер", "Мамайка", "Центр", "Красная Поляна", "Хоста"],
        "Краснодар": ["ФМР", "ЮМР", "Парк Галицкого", "Музыкальный", "Энка"],
        "Ростов-на-Дону": ["Северный", "Западный", "Нахичевань", "Левбердон", "Центр"]
    }
    
    # Общие локации для городов, которых нет в базе или для разнообразия
    GENERIC_LOCATIONS = [
        "Центр", "Вокзал", "Парк", "Набережная", "Промзона", 
        "Гаражи", "Частный сектор", "Лесополоса", "Дачи", "Спальный р-н"
    ]

    FAKE_ITEMS = [
        "Шишки (OG Kush) 1г", "Шишки (Amnesia) 2г", "Шишки (White Widow) 3г",
        "Гашиш (Euro) 1г", "Гашиш (Ice-O-Lator) 2г", "Мефедрон (Кристалл) 0.5г",
        "Мефедрон (Кристалл) 1г", "Альфа-ПВП (Белая) 0.5г", "Амфетамин (HQ) 1г",
        "Кокаин (Колумбия) 0.5г", "Бошки (AK-47) 5г", "XTC (Punisher) 2шт"
    ]

    @staticmethod
    def _get_real_usernames():
        """Получает список реальных никнеймов из базы пользователей."""
        try:
            data = UserService._load_db()
        except Exception:
            return []

        users = data.get("users", {}) if isinstance(data, dict) else {}
        names = []
        for u in users.values():
            uname = u.get("username")
            if uname and len(uname) > 3:
                names.append(uname)
        return names

    @staticmethod
    def _generate_username():
        """Wrapper for _generate_fake_user used by AI task"""
        # We ignore the 'base_name' return value and just take the formatted one
        username, _ = ReviewsService._generate_fake_user()
        return username

    @staticmethod
    def _generate_fake_user(used_base_names=None):
        """
        Возвращает маскированное имя для отображения в отзывах.
        """
        return ReviewsService.HIDDEN_USERNAME, ReviewsService.HIDDEN_USERNAME

    @staticmethod
    def _load_reviews():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.REVIEWS_DOC_KEY, [])
            reviews = data if isinstance(data, list) else []
            cleaned, removed = ReviewsService._filter_invalid_reviews(reviews, "load")
            if removed or ReviewsService._ensure_review_fields(cleaned):
                ReviewsService._save_reviews(cleaned)
            return cleaned
        if os.path.exists(ReviewsService.FILE_PATH):
            try:
                with open(ReviewsService.FILE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    reviews = data if isinstance(data, list) else []
            except Exception:
                return []
            cleaned, removed = ReviewsService._filter_invalid_reviews(reviews, "load")
            if removed:
                ReviewsService._save_reviews(cleaned)
            if ReviewsService._ensure_review_fields(cleaned):
                ReviewsService._save_reviews(cleaned)
            return cleaned
        return []

    @staticmethod
    def _save_reviews(reviews):
        # Removed limit to store ALL reviews as requested
        # if len(reviews) > 500:
        #    reviews = reviews[-500:]
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.REVIEWS_DOC_KEY, reviews)
            return
            
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
            
        try:
            with open(ReviewsService.FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(reviews, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to save reviews")

    @staticmethod
    def _ensure_review_fields(reviews: list[dict]) -> bool:
        changed = False
        for item in reviews or []:
            if not isinstance(item, dict):
                continue
            if not item.get("id"):
                item["id"] = secrets.token_hex(6)
                changed = True
            if "hidden" not in item:
                item["hidden"] = False
                changed = True
        return changed

    @staticmethod
    def _split_item_info(item_info: str | None) -> tuple[str, str, str]:
        raw = str(item_info or "").strip()
        if not raw:
            return "", "", ""

        if "|" not in raw:
            return raw, "", ""

        product_part, location_part = raw.split("|", 1)
        product = product_part.strip()
        location = location_part.strip()

        if not location:
            return product, "", ""

        if "," not in location:
            return product, location, ""

        city_part, district_part = location.split(",", 1)
        return product, city_part.strip(), district_part.strip()

    @staticmethod
    def _build_item_info(product: str, city: str, district: str, region: str | None = None) -> str:
        product_value = str(product or "").strip()
        city_value = str(city or "").strip()
        district_value = str(district or "").strip()
        region_value = str(region or "").strip()
        if city_value and region_value and region_value.lower() not in city_value.lower():
            city_value = f"{city_value} ({region_value})"
        if city_value and district_value:
            location_value = f"{city_value}, {district_value}"
        else:
            location_value = city_value or district_value

        if product_value and location_value:
            return f"{product_value} | {location_value}"
        if product_value:
            return product_value
        return location_value

    @staticmethod
    def _save_last_batch(iso_dates: list[str]) -> None:
        payload = {
            "iso_dates": iso_dates or [],
            "saved_at": datetime.now().isoformat(),
        }
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.LAST_BATCH_DOC_KEY, payload)
            return
        try:
            os.makedirs(os.path.dirname(ReviewsService.LAST_BATCH_FILE), exist_ok=True)
            with open(ReviewsService.LAST_BATCH_FILE, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to persist last review batch marker")

    @staticmethod
    def _load_last_batch() -> list[str]:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.LAST_BATCH_DOC_KEY, {})
            iso_dates = data.get("iso_dates") if isinstance(data, dict) else None
            if isinstance(iso_dates, list):
                return [str(item) for item in iso_dates if isinstance(item, str)]
            return []
        if not os.path.exists(ReviewsService.LAST_BATCH_FILE):
            return []
        try:
            with open(ReviewsService.LAST_BATCH_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            iso_dates = data.get("iso_dates")
            if isinstance(iso_dates, list):
                return [str(item) for item in iso_dates if isinstance(item, str)]
        except Exception:
            logger.exception("Failed to load last review batch marker")
        return []

    @staticmethod
    def remove_last_generated_batch() -> int:
        iso_dates = ReviewsService._load_last_batch()
        if not iso_dates:
            return 0

        reviews = ReviewsService._load_reviews()
        if not reviews:
            ReviewsService._save_last_batch([])
            return 0

        target_set = set(iso_dates)
        filtered = [rev for rev in reviews if rev.get("iso_date") not in target_set]
        removed = len(reviews) - len(filtered)

        if removed:
            ReviewsService._save_reviews(filtered)
        ReviewsService._save_last_batch([])
        return removed

    @staticmethod
    def generate_mass_reviews():
        """Generates random 4-10 reviews for EACH available city."""
        # Load caches
        products_cache = CatalogService._load_cache()
        districts_cache = GeoService._load_cache()
        all_products = CatalogService.get_all_products()
        
        # Find intersections (valid targets)
        targets = ReviewsService._city_targets(products_cache, districts_cache)
        
        if not targets:
            return 0
            
        reviews = ReviewsService._load_reviews()
        now = datetime.now()
        count = 0
        
        for p_key, d_key in targets:
             # Random number of reviews per city/target (4 to 10)
             num_reviews = random.randint(4, 10)
             
             for _ in range(num_reviews):
                 # Spread time over last 48 hours to make it look organic
                 # But weighted towards recent? No, simple random across 2 days is fine.
                 minutes_back = random.randint(0, 48 * 60) 
                 dt = now - timedelta(minutes=minutes_back)
                 
                 new_rev = ReviewsService._create_review_dict(
                     dt,
                     reviews,
                     forced_city_info=(p_key, d_key),
                     products_cache=products_cache,
                     districts_cache=districts_cache,
                     all_products=all_products,
                 )
                 if new_rev:
                     reviews.append(new_rev)
                     count += 1
        
        # Sort by date so new ones are at the end (or correctly ordered)
        reviews.sort(key=lambda x: x['iso_date'])
        
        ReviewsService._save_reviews(reviews)
        return count

    @staticmethod
    def force_regenerate():
        """Удаляет текущие отзывы и генерирует новые на основе истории"""
        if os.path.exists(ReviewsService.FILE_PATH):
            try:
                os.remove(ReviewsService.FILE_PATH)
            except:
                pass
        
        # Запускаем первоначальный посев
        return ReviewsService._seed_initial_reviews()

    @staticmethod
    def _polish_review_text(text: str, *, lowercase_chance: float = 0.18) -> str:
        assembled = re.sub(r"\s+", " ", str(text or "")).strip()
        assembled = assembled.replace("—", "-").replace("–", "-")
        if not assembled:
            return assembled
        if random.random() < lowercase_chance:
            assembled = assembled[:1].lower() + assembled[1:]
        else:
            assembled = assembled[:1].upper() + assembled[1:]
        return assembled

    @staticmethod
    def _remember_review_norm(store: set[str] | list[str] | None, text: str) -> None:
        if store is None:
            return
        normalized = ReviewsService._normalize_review_text(text)
        if not normalized:
            return
        if isinstance(store, set):
            store.add(normalized)
        else:
            store.append(normalized)

    @staticmethod
    def _generate_local_review_text(product_name: str | None = None,
                                    city_name: str | None = None,
                                    district_name: str | None = None,
                                    price: int | float | None = None,
                                    forbidden_norms: set[str] | list[str] | None = None) -> str:
        rng = random

        product = (product_name or rng.choice(ReviewsService.FAKE_ITEMS)).strip()
        product_simple = product.split("|")[0].split("(")[0].strip() or "стафф"
        product_lower = product_simple.lower()

        alias = ""
        for keywords, options in ReviewsService.PRODUCT_ALIAS_CONFIG:
            if any(key in product_lower for key in keywords):
                candidates = [opt.strip() for opt in options if opt and len(opt.strip()) >= 3]
                if candidates:
                    alias = rng.choice(candidates)
                break

        district = str(district_name or "").strip()
        city = str(city_name or "").strip()
        place_phrase = ReviewsService._location_prepositional_phrase(district) if district else ""

        actions = [
            "забрал в касание",
            "снял без суеты",
            "поднял сразу",
            "нашёл с ходу",
            "унёс спокойно",
            "взял и поехал",
            "добрался и снял",
            "точка нашлась быстро",
            "подошёл и забрал",
            "снял без лишнего шума",
            "нашёл с первого раза",
            "зашёл и унёс",
            "поднял без нервов",
            "снял за минуту",
            "забрал без проблем",
            "на месте всё лежало",
            "подошёл без суеты",
            "закрыл вопрос быстро",
        ]
        places = [
            "место тихое",
            "точка удобная",
            "без палева",
            "описание совпало",
            "корды точные",
            "фото понятное",
            "место спокойное",
            "точка ровная",
            "всё как в описании",
            "искать почти не пришлось",
            "заход простой",
            "квест лёгкий",
        ]
        if place_phrase:
            places.extend([
                f"{place_phrase} тихо",
                f"{place_phrase} спокойно",
                f"{place_phrase} нашёл быстро",
                f"{place_phrase} без суеты",
            ])
        elif city:
            city_phrase = ReviewsService._location_prepositional_phrase(city)
            places.extend([
                f"по {city} всё ровно",
                f"{city_phrase} без проблем" if city_phrase else f"в {city} без проблем",
            ])

        qualities = [
            "кач ровный",
            "кач порадовал",
            "фасовка аккуратная",
            "без сюрпризов",
            "всё на месте",
            "качество норм",
            "кач огонь",
            "фасовка опрятная",
            "вес ровный",
            "всё как ждал",
            "без лишнего",
            "чисто и ровно",
        ]
        if alias:
            qualities.extend([
                f"{alias} зашёл мягко",
                f"{alias} ровный",
                f"{alias} без сюрпризов",
                f"взял {alias}, кач норм",
            ])

        moods = [
            "доволен",
            "настрой хороший",
            "буду брать ещё",
            "всё ок",
            "без нервов",
            "кайф",
            "норм заход",
            "мне зашло",
            "в этот раз гладко",
            "поехал домой спокойно",
            "спасибо, всё ровно",
            "без вопросов",
        ]

        def punct() -> str:
            return rng.choice([". ", ", ", " - "])

        def cap(part: str) -> str:
            part = part.strip().rstrip(".,!")
            return part

        def build_once() -> str:
            action = cap(rng.choice(actions))
            place = cap(rng.choice(places))
            quality = cap(rng.choice(qualities))
            mood = cap(rng.choice(moods))
            pattern = rng.randint(0, 9)
            if pattern == 0:
                text = f"{action}{punct()}{place}"
            elif pattern == 1:
                text = f"{action}{punct()}{quality}"
            elif pattern == 2:
                text = f"{place}{punct()}{action}"
            elif pattern == 3:
                text = f"{action}{punct()}{quality}{punct()}{mood}"
            elif pattern == 4:
                text = f"{quality}{punct()}{action}"
            elif pattern == 5:
                text = f"{action}{punct()}{mood}"
            elif pattern == 6:
                text = f"{place}{punct()}{quality}{punct()}{mood}"
            elif pattern == 7:
                text = f"{mood}{punct()}{action}{punct()}{place}"
            elif pattern == 8:
                text = f"{action}{punct()}{place}{punct()}{quality}"
            else:
                text = f"{place}{punct()}{action}{punct()}{mood}"
            text = re.sub(r"\s+", " ", text).strip(" ,.-")
            if rng.random() < 0.28:
                text += "!"
            elif rng.random() < 0.45:
                text += "."
            if rng.random() < 0.08:
                text += " " + rng.choice(["🔥", "🤝", "👌"])
            return text

        for _ in range(80):
            text = ReviewsService._polish_review_text(build_once())
            if ReviewsService._looks_truncated(text):
                continue
            if forbidden_norms is not None and ReviewsService._is_duplicate_text(text, forbidden_norms, similar_threshold=0.88):
                continue
            ReviewsService._remember_review_norm(forbidden_norms, text)
            return text

        fallback_text = ReviewsService._polish_review_text(build_once())
        ReviewsService._remember_review_norm(forbidden_norms, fallback_text)
        return fallback_text

    @staticmethod
    def _get_random_text(product_name: str | None = None,
                         city_name: str | None = None,
                         district_name: str | None = None,
                         price: int | float | None = None,
                         forbidden_norms: set[str] | list[str] | None = None) -> str:
        ai_text = ReviewsService._generate_ai_text_sync(
            product_name=product_name,
            city_name=city_name,
            district_name=district_name,
            item_price=price
        )
        if ai_text and not ReviewsService._is_invalid_ai_text(ai_text):
            if forbidden_norms and ReviewsService._is_duplicate_text(ai_text, forbidden_norms):
                logger.info("[ReviewGen] AI sync duplicate detected, switching to template fallback")
            else:
                ReviewsService._remember_review_norm(forbidden_norms, ai_text)
                return ai_text
        if ai_text and forbidden_norms:
            logger.info("[AI Review] Sync response rejected (duplicate or invalid) for city=%s", city_name or "?")
        if ai_text:
            logger.warning(
                "[AI Review] Dropped invalid AI response, switching to template (city=%s, district=%s, product=%s)",
                city_name or "?",
                district_name or "?",
                product_name or "?",
            )
        else:
            logger.info(
                "[AI Review] Fallback to template (city=%s, district=%s, product=%s)",
                city_name or "?",
                district_name or "?",
                product_name or "?",
            )
        return ReviewsService._generate_local_review_text(
            product_name,
            city_name,
            district_name,
            price,
            forbidden_norms=forbidden_norms
        )

    @staticmethod
    def _create_review_dict(
        dt_obj,
        recent_reviews=None,
        forced_city_info=None,
        *,
        products_cache=None,
        districts_cache=None,
        all_products=None,
    ):
        product_name = None
        product_price = None
        product_id_value = None
        city = None
        region = ""
        district = None
        item = None

        products_cache = products_cache if products_cache is not None else CatalogService._load_cache()
        districts_cache = districts_cache if districts_cache is not None else GeoService._load_cache()
        all_prods = all_products if all_products is not None else CatalogService.get_all_products()
        
        # Пересечение: города, которые есть И там И там (с учетом неточного совпадения ключей)
        targets = ReviewsService._city_targets(products_cache, districts_cache)

        used_catalog = False
        
        # Если есть города в кэше, выбираем из них
        if targets:
            # Выбираем город
            if forced_city_info:
               # forced_city_info is (p_key, d_key)
               chosen_p_key, chosen_d_key = forced_city_info
               # Verify it exists in our map or cache? Trust caller?
               # Let's trust caller but verify cache presence briefly
               if chosen_p_key not in products_cache or chosen_d_key not in districts_cache:
                   # Fallback to random if invalid
                   chosen_p_key, chosen_d_key = random.choice(targets)
            else:
               chosen_p_key, chosen_d_key = random.choice(targets)
            
            city = ReviewsService._format_city_display(chosen_p_key)
            region = ReviewsService._region_from_key(chosen_d_key) or ReviewsService._region_from_key(chosen_p_key)

            # Теперь получаем список DISTRICTS
            districts_list = districts_cache[chosen_d_key]

            # Получаем список ТОВАРОВ
            prod_entry = products_cache[chosen_p_key]
            prod_ids = prod_entry.get("ids", []) if isinstance(prod_entry, dict) else prod_entry
            real_products = [p for p in all_prods if p["id"] in prod_ids]
            
            if real_products and districts_list:
                # Ура, у нас есть всё реальное для этого города
                prod = random.choice(real_products)
                dist_raw = random.choice(districts_list)
                dist = ReviewsService._format_location_display(dist_raw)
                
                item = f"{prod['name']}"
                district = dist
                product_name = prod['name']
                product_price = prod.get('base_price')
                product_id_value = prod.get("id")
                used_catalog = True
        
        if not used_catalog:
             # Кэша нет. Используем Generic данные (Fallback для первого запуска)
             f_city_name = random.choice(list(ReviewsService.CITY_KNOWLEDGE.keys()))
             f_districts = ReviewsService.CITY_KNOWLEDGE[f_city_name]
             f_district_raw = random.choice(f_districts)
             f_district = ReviewsService._format_location_display(f_district_raw)
             
             # Items fallback
             # Or use actual catalog if available
             prods = all_prods
             if prods:
                 p = random.choice(prods)
                 item = f"{p['name']}"
                 product_name = p['name']
                 product_price = p.get('base_price')
                 product_id_value = p.get("id")
             else:
                 item = random.choice(ReviewsService.FAKE_ITEMS)
                 product_name = item
                 product_price = None
             
             city = f_city_name
             region = ""
             district = f_district
             used_catalog = True # Marked as used to proceed

        if not product_name:
            product_name = item or "Стафф"

        text = ReviewsService._generate_local_review_text(
            product_name=product_name,
            city_name=city,
            district_name=district,
            price=product_price,
        )

        # Собираем список имен...
        used_names = []
        if recent_reviews:
            check_slice = recent_reviews[-15:]
            for r in check_slice:
                if 'base_name' in r:
                    used_names.append(r['base_name'])

        user_display, base_name = ReviewsService._generate_fake_user(used_base_names=used_names)
        
        stars_count = 5 if random.random() > 0.1 else 4
        
        return {
            "user": user_display,
            "base_name": base_name, 
            "text": text,
            "product": product_name,
            "product_id": product_id_value,
            "city": city, # Added specific field
            "region": region or None,
            "district": district,
            "item_info": ReviewsService._build_item_info(product_name or item, city, district, region),
            "stars": stars_count,
            "iso_date": dt_obj.isoformat(),
            "display_date": dt_obj.strftime("%d.%m %H:%M"),
            "hidden": False,
            "id": secrets.token_hex(6),
        }



    @staticmethod
    def _seed_initial_reviews():
        """Генерирует 'историю' отзывов за последние 24-48 часов"""
        reviews = []
        now = datetime.now()
        current_ts = now - timedelta(hours=24)
        products_cache = CatalogService._load_cache()
        districts_cache = GeoService._load_cache()
        all_products = CatalogService.get_all_products()

        while current_ts < now:
            review = ReviewsService._create_review_dict(
                current_ts,
                recent_reviews=reviews,
                products_cache=products_cache,
                districts_cache=districts_cache,
                all_products=all_products,
            )
            if review:
                reviews.append(review)
            
            # Следующий отзыв через 15-40 минут
            step_minutes = random.randint(15, 40)
            current_ts += timedelta(minutes=step_minutes)
            
        ReviewsService._save_reviews(reviews)
        return reviews

    @staticmethod
    def check_and_update():
        """
        Read-path helper for reviews requests.
        Не генерирует стартовые отзывы автоматически: пустая база должна
        оставаться пустой, пока админ или отдельная задача не создаст отзывы.
        """
        reviews = ReviewsService._load_reviews()

        if not reviews:
            return []
        
        last_review = reviews[-1]
        try:
            last_ts = datetime.fromisoformat(last_review['iso_date'])
        except:
            return []
            
        now = datetime.now()
        
        if last_ts > now:
            last_ts = now - timedelta(minutes=60)

        diff_minutes = (now - last_ts).total_seconds() / 60
        required_gap = random.randint(15, 40)
        
        if diff_minutes > required_gap:
            return reviews

        return reviews


    @staticmethod
    def _format_location_display(name: str) -> str:
        """Return location name without additional prefixes."""
        return str(name or "").strip()

    @staticmethod
    def _decline_prepositional_token(token: str) -> str:
        raw = str(token or "").strip()
        if not raw:
            return ""
        if re.fullmatch(r"[\dIVXLCDM./№\-]+", raw, flags=re.I):
            return raw
        lower = raw.lower()
        nouns = {
            "автовокзал": "автовокзале",
            "вокзал": "вокзале",
            "центр": "центре",
            "рынок": "рынке",
            "парк": "парке",
            "сквер": "сквере",
            "мост": "мосту",
            "квартал": "квартале",
            "посёлок": "посёлке",
            "поселок": "поселке",
            "микрорайон": "микрорайоне",
            "район": "районе",
            "переулок": "переулке",
            "проспект": "проспекте",
            "бульвар": "бульваре",
            "площадь": "площади",
            "набережная": "набережной",
            "остановка": "остановке",
            "платформа": "платформе",
            "деревня": "деревне",
            "село": "селе",
            "кольцо": "кольце",
            "заречье": "заречье",
            "остров": "острове",
            "порт": "порту",
            "аэропорт": "аэропорту",
            "магазин": "магазине",
            "больница": "больнице",
            "школа": "школе",
        }
        if lower in nouns:
            declined = nouns[lower]
        elif lower.endswith(("ого", "его", "ому", "ему")):
            declined = lower
        elif lower.endswith("ая"):
            declined = lower[:-2] + "ой"
        elif lower.endswith("яя"):
            declined = lower[:-2] + "ей"
        elif lower.endswith("ое"):
            declined = lower[:-2] + "ом"
        elif lower.endswith("ее"):
            declined = lower[:-2] + "ем"
        elif lower.endswith("ский") or lower.endswith("цкий"):
            declined = lower[:-2] + "ом"
        elif lower.endswith("ий"):
            declined = lower[:-2] + "ом"
        elif lower.endswith("ый"):
            declined = lower[:-2] + "ом"
        elif lower.endswith("ой") and len(lower) > 3:
            declined = lower[:-2] + "ом"
        elif lower.endswith(("а", "я")) and not lower.endswith(("ина", "ова", "ева", "ёва", "ская", "цкая")):
            # Keep person-genitive street names: Ленина, Кирова, Гагарина.
            declined = lower
        elif lower.endswith(("ина", "ова", "ева", "ёва")):
            declined = lower
        else:
            declined = lower
        if raw[0].isupper():
            return declined[:1].upper() + declined[1:]
        return declined

    @staticmethod
    def _decline_prepositional_name(name: str) -> str:
        parts = str(name or "").strip().split()
        if not parts:
            return ""
        return " ".join(ReviewsService._decline_prepositional_token(part) for part in parts)

    @staticmethod
    def _location_prepositional_phrase(name: str) -> str:
        """Разговорная форма места: на автовокзале, на улице Слоновой, в Кировском районе."""
        original = " ".join(str(name or "").strip().split())
        if not original:
            return ""

        lower = original.lower()
        kind_specs = (
            (("улица ", "ул. ", "ул "), "street", "на", "улице"),
            (("проспект ", "пр-т ", "пр. "), "prospect", "на", "проспекте"),
            (("переулок ", "пер. "), "lane", "в", "переулке"),
            (("шоссе ", "ш. "), "highway", "на", "шоссе"),
            (("бульвар ", "бул. "), "boulevard", "на", "бульваре"),
            (("площадь ", "пл. "), "square", "на", "площади"),
            (("набережная ", "наб. "), "embankment", "на", "набережной"),
            (("микрорайон ", "мкр. ", "мкр "), "micro", "в", "микрорайоне"),
            (("посёлок ", "поселок ", "пос. "), "village", "в", "посёлке"),
            (("остановка ", "ост. "), "stop", "на", "остановке"),
            (("платформа ",), "platform", "на", "платформе"),
            (("деревня ",), "village", "в", "деревне"),
            (("село ",), "village", "в", "селе"),
        )
        for prefixes, _kind, prep, type_word in kind_specs:
            for prefix in prefixes:
                if lower.startswith(prefix):
                    rest = original[len(prefix):].strip(" .")
                    if _kind in {"stop", "platform"}:
                        titled = rest
                    else:
                        titled = ReviewsService._decline_prepositional_name(rest)
                    return f"{prep} {type_word} {titled}".strip()

        if lower.endswith(" район") or lower.startswith("район "):
            rest = original[:-6].strip() if lower.endswith(" район") else original[6:].strip()
            titled = ReviewsService._decline_prepositional_name(rest)
            return f"в {titled} районе".strip() if titled else "в районе"

        whole = {
            "автовокзал": "на автовокзале",
            "вокзал": "на вокзале",
            "ж/д вокзал": "на вокзале",
            "железнодорожный вокзал": "на вокзале",
            "центр": "в центре",
            "рынок": "на рынке",
            "центральный рынок": "на центральном рынке",
            "парк": "в парке",
            "сквер": "в сквере",
            "мост": "на мосту",
            "аэропорт": "в аэропорту",
            "порт": "в порту",
        }
        if lower in whole:
            return whole[lower]

        if lower.endswith(("ая", "яя")):
            return f"на улице {ReviewsService._decline_prepositional_name(original)}"
        if any(token in lower for token in ("автовокзал", "вокзал")):
            return f"на {ReviewsService._decline_prepositional_name(original)}"

        declined = ReviewsService._decline_prepositional_name(original)
        prep = "на" if any(token in lower for token in ("рынок", "мост", "площад", "набереж")) else "в"
        return f"{prep} {declined}"

    @staticmethod
    def _format_city_display(name: str) -> str:
        text = re.sub(r"\s*\(.*?\)\s*", " ", str(name or "")).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            return ""
        if any(ch.isupper() for ch in text):
            return text
        return " ".join((part[0].upper() + part[1:]) if part else part for part in text.split(" "))


        # --- Persistent Cities Tracking ---
    @staticmethod
    def _load_active_cities():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.ACTIVE_CITIES_DOC_KEY, [])
            return data if isinstance(data, list) else []
        if not os.path.exists(ReviewsService.ACTIVE_CITIES_FILE):
            return []
        try:
            with open(ReviewsService.ACTIVE_CITIES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []

    @staticmethod
    def _save_active_cities(cities):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.ACTIVE_CITIES_DOC_KEY, cities)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(ReviewsService.ACTIVE_CITIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cities, f, ensure_ascii=False, indent=2)
        except:
            pass

    @staticmethod
    def register_active_city(city_name):
        cities = ReviewsService._load_active_cities()
        if city_name not in cities:
            cities.append(city_name)
            ReviewsService._save_active_cities(cities)

    @staticmethod
    def get_active_cities():
        return ReviewsService._load_active_cities()

    @staticmethod
    def get_manual_city_options(limit: int | None = None) -> list[dict[str, str]]:
        """Возвращает города с доступными товарами и районами для ручных отзывов."""
        products_cache = CatalogService._load_cache()
        districts_cache = GeoService._load_cache()

        if not products_cache or not districts_cache:
            return []

        active_cities = ReviewsService.get_active_cities()
        active_lookup: dict[str, str] = {}
        for name in active_cities:
            if isinstance(name, str):
                key = name.lower().strip()
                if key:
                    active_lookup[key] = name.strip()

        def _has_products(entry) -> bool:
            if isinstance(entry, dict):
                ids = entry.get("ids")
                return isinstance(ids, list) and bool(ids)
            if isinstance(entry, list):
                return bool(entry)
            return False

        def _has_district(city_key: str) -> bool:
            for d_key in districts_cache.keys():
                if isinstance(d_key, str) and ReviewsService._is_city_key_match(city_key, d_key):
                    return True
            return False

        seen: set[str] = set()
        prioritized: list[dict[str, str]] = []
        for display in active_cities:
            if not isinstance(display, str):
                continue
            city_key = display.lower().strip()
            if not city_key or city_key in seen:
                continue
            entry = products_cache.get(city_key)
            if not _has_products(entry):
                continue
            if not _has_district(city_key):
                continue
            prioritized.append({
                "display": display.strip(),
                "city_key": city_key
            })
            seen.add(city_key)

        extra: list[dict[str, str]] = []
        for raw_key, entry in products_cache.items():
            if not isinstance(raw_key, str):
                continue
            city_key = raw_key.lower().strip()
            if not city_key or city_key in seen:
                continue
            if not _has_products(entry):
                continue
            if not _has_district(city_key):
                continue
            display = active_lookup.get(city_key) or city_key.title()
            extra.append({
                "display": display.strip(),
                "city_key": city_key
            })
            seen.add(city_key)

        extra.sort(key=lambda item: item["display"])
        combined = prioritized + extra

        if limit is not None and limit > 0 and len(combined) > limit:
            return combined[:limit]

        return combined

    @staticmethod
    def add_manual_review(username: str, city: str, product: str, district: str, text: str, product_id: str | None = None, region: str | None = None):
        reviews = ReviewsService._load_reviews()
        if not reviews:
            reviews = []
            
        now = datetime.now()
        city_name, embedded_region = ReviewsService._city_name_and_region(city)
        region_value = str(region or embedded_region or "").strip()
        city_display = ReviewsService._format_city_display(city_name or city)
        item_info = ReviewsService._build_item_info(product, city_display, district, region_value)
        masked_user = ReviewsService.HIDDEN_USERNAME
        product_id_value = str(product_id or "").strip() or None

        new_review = {
            "id": secrets.token_hex(6),
            "user": masked_user,
            "base_name": masked_user,
            "text": text,
            "product": product,
            "product_id": product_id_value,
            "city": city_display,
            "region": region_value or None,
            "district": str(district or "").strip(),
            "item_info": item_info,
            "stars": 5, 
            "iso_date": now.isoformat(),
            "display_date": now.strftime("%d.%m %H:%M"),
            "hidden": False
        }
        
        reviews.append(new_review)
        ReviewsService._save_reviews(reviews)
        return True

    # --- Custom Prompts Management ---
    @staticmethod
    def _load_custom_prompts():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.PROMPTS_DOC_KEY, [])
            return data if isinstance(data, list) else []
        if not os.path.exists(ReviewsService.PROMPTS_FILE):
            return []
        try:
            with open(ReviewsService.PROMPTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []

    @staticmethod
    def add_custom_prompt(phrase):
        phrases = ReviewsService._load_custom_prompts()
        if phrase not in phrases:
            phrases.append(phrase)
            if PostgresDocumentStore.is_enabled():
                PostgresDocumentStore.set_document(ReviewsService.PROMPTS_DOC_KEY, phrases)
                return True
            try:
                with open(ReviewsService.PROMPTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(phrases, f, ensure_ascii=False, indent=2)
                return True
            except:
                return False
        return False

    @staticmethod
    def get_custom_prompts():
        return ReviewsService._load_custom_prompts()

    @staticmethod
    def remove_custom_prompt(phrase):
        phrases = ReviewsService._load_custom_prompts()
        if phrase in phrases:
            phrases.remove(phrase)
            if PostgresDocumentStore.is_enabled():
                PostgresDocumentStore.set_document(ReviewsService.PROMPTS_DOC_KEY, phrases)
                return True
            try:
                with open(ReviewsService.PROMPTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(phrases, f, ensure_ascii=False, indent=2)
                return True
            except:
                return False
        return False

    # --- AI Review Generation ---

    @staticmethod
    def _make_openai_client():
        if not has_openai or not OPENAI_API_KEY:
            return None
        return AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            timeout=AI_REQUEST_TIMEOUT_SEC,
            max_retries=AI_MAX_RETRIES,
        )

    @staticmethod
    async def _generate_ai_text(
        product_name,
        city_name,
        district_name,
        item_price,
        avoid_examples: list[str] | None = None,
        client=None,
    ):
        """Generates feedback text using ChatGPT"""
        if not has_openai or not OPENAI_API_KEY:
            return None

        styles = [
            "спокойный, коротко и сухо",
            "новичок, чуть удивлён что всё нашлось",
            "опытный, говорит по делу",
            "уставший, но довольный",
            "сдержанный, почти без эмоций",
            "лёгкий сленг, без кринжа",
            "очень короткий, как в чат написал на ходу",
            "чуть теплее обычного, но без рекламы",
        ]
        hooks = [
            "начни с действия",
            "начни с места",
            "начни с качества",
            "начни с настроения",
            "сначала факт, потом ощущение",
            "сначала ощущение, потом факт",
        ]
        style = random.choice(styles)
        hook = random.choice(hooks)

        normalized = str(product_name or "").lower()
        alias_options = []
        for keywords, options in ReviewsService.PRODUCT_ALIAS_CONFIG:
            if any(k in normalized for k in keywords):
                alias_options = [opt.strip() for opt in options if opt and len(str(opt).strip()) >= 3]
                break

        stash_type = random.choice(["тайник", "магнит", "прикоп"])
        short_mode = random.random() < 0.4

        custom_phrases = ReviewsService._load_custom_prompts()
        custom_instructions = ""
        if custom_phrases:
            phrases_str = ", ".join(f'"{p}"' for p in custom_phrases[:8])
            custom_instructions = (
                "\nЕсли уместно, вплети одну из фраз, слегка перефразировав: "
                f"{phrases_str}"
            )

        avoid_block = ""
        cleaned_examples = [str(item).strip() for item in (avoid_examples or []) if str(item).strip()]
        if cleaned_examples:
            sample = " | ".join(cleaned_examples[-8:])
            avoid_block = f"\nНе повторяй близко эти формулировки: {sample}"

        alias_line = ", ".join(alias_options[:5]) if alias_options else ""
        alias_instruction = (
            f" Прозвище товара можно вставить целиком, без обрывков: {alias_line}."
            if alias_line else ""
        )

        system_message = (
            "Ты пишешь короткий живой отзыв от реального покупателя закрытого магазина. "
            "Пиши как человек в чате: разговорно, без рекламы, без списков и без заголовков. "
            "Только законченные слова и фразы, никогда не обрывай слово. "
            "Не пиши КАПСОМ. Не используй однобуквенные обрывки вроде 'ск', 'м', 'кр'. "
            "Не выдумывай улицы, дома, координаты, цены и время. "
            "Не пиши, что текст сгенерирован. "
            f"Характер: {style}. Как начать: {hook}."
        )

        location_phrase = ReviewsService._location_prepositional_phrase(district_name) if district_name else "в городе"
        location_hint = (
            f"Место: {location_phrase}. "
            "Пиши это место уже в нужном падеже, как в живой речи. "
            "Нельзя: «в Автовокзал», «улица Слоновая забрал». "
            f"Нужно как «{location_phrase} тихо» / «забрал {location_phrase}»."
        )

        if short_mode:
            user_message = (
                f"Это {stash_type} {location_phrase}. {location_hint} "
                "Напиши одну короткую живую фразу на 5-10 слов. "
                "Каждый раз другая конструкция, не копируй прошлые отзывы. "
                "Можно запятую, точку или '!'. Без кавычек и без переносов. "
                "Часть отзывов спокойные, не все восторженные."
                f"{alias_instruction}{custom_instructions}{avoid_block}"
            )
        else:
            user_message = (
                f"Это {stash_type} {location_phrase}. {location_hint} "
                "Напиши 1-2 коротких предложения, как человек после удачного забора. "
                "Меняй порядок мыслей, не начинай одинаково. "
                "Можно про тихое место, точные корды или ровный кач, но не копируй одну схему. "
                "Без кавычек, списков и обращений к оператору."
                f"{alias_instruction}{custom_instructions}{avoid_block}"
            )

        start_time = perf_counter()
        own_client = client is None
        if own_client:
            client = ReviewsService._make_openai_client()
            if client is None:
                return None

        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=90 if short_mode else 150,
                temperature=1.05,
                top_p=0.95,
                frequency_penalty=0.45,
                presence_penalty=0.35,
            )
            raw_text = response.choices[0].message.content or ""
            text = " ".join(seg.strip() for seg in raw_text.replace("\r", "\n").split("\n") if seg.strip())
            text = re.sub(r"\s+", " ", text).strip().strip('"“”«»')
            text = text.replace("—", "-").replace("–", "-")
            text = re.sub(r"(?i)\b(спасибо вам большое|очень выручили)\b", "", text)
            text = re.sub(r"\s{2,}", " ", text).strip()

            if text and random.random() < 0.18:
                slang_pairs = [
                    (r"\bсейчас\b", "щас"),
                    (r"\bочень\b", "оч"),
                    (r"\bвообще\b", "ваще"),
                    (r"\bнормально\b", "норм"),
                ]
                for pattern, replacement in slang_pairs:
                    if random.random() < 0.4:
                        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

            text = ReviewsService._polish_review_text(text, lowercase_chance=0.22)
            if ReviewsService._is_invalid_ai_text(text):
                elapsed = perf_counter() - start_time
                logger.warning(
                    "[AI Review] Invalid AI payload received in %.2fs (short_mode=%s)",
                    elapsed,
                    short_mode,
                )
                return None
            elapsed = perf_counter() - start_time
            logger.info(
                "[AI Review] Generated text in %.2fs (short_mode=%s)",
                elapsed,
                short_mode,
            )
            return text
        except Exception:
            elapsed = perf_counter() - start_time
            logger.exception("OpenAI request failed after %.2fs", elapsed)
            return None
        finally:
            if own_client and client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    @staticmethod
    def _generate_ai_text_sync(product_name, city_name, district_name, item_price):
        if not has_openai or not OPENAI_API_KEY:
            return None

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop and running_loop.is_running():
                result_holder: dict[str, str | None] = {}
                error_holder: dict[str, Exception] = {}

                def run_in_thread() -> None:
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        result_holder["value"] = new_loop.run_until_complete(
                            ReviewsService._generate_ai_text(
                                product_name=product_name,
                                city_name=city_name,
                                district_name=district_name,
                                item_price=item_price
                            )
                        )
                    except Exception as thread_exc:  # noqa: BLE001
                        error_holder["error"] = thread_exc
                    finally:
                        asyncio.set_event_loop(None)
                        new_loop.close()

                worker = threading.Thread(target=run_in_thread, name="reviews-ai-call")
                worker.start()
                worker.join()

                if error_holder:
                    raise error_holder["error"]

                return result_holder.get("value")
            else:
                return asyncio.run(
                    ReviewsService._generate_ai_text(
                        product_name=product_name,
                        city_name=city_name,
                        district_name=district_name,
                        item_price=item_price
                    )
                )
        except Exception:
            logger.exception("AI sync wrapper failed")
            return None

    @staticmethod
    def _generate_reviews_batch(trigger: str = "auto") -> int:
        """
        Mass review generation for every city in the product/district cache.
        Uses local templates so a full wave finishes in seconds, same as the
        other bot. ChatGPT is not called here: one request per city is what
        froze the admin button for minutes.
        """
        reviews = ReviewsService._load_reviews()

        trigger_label = trigger or "auto"
        started = perf_counter()
        logger.info("[ReviewGen] Generation triggered (%s)", trigger_label)

        reviews, removed_invalid = ReviewsService._filter_invalid_reviews(reviews, f"pre-{trigger_label}")
        if removed_invalid:
            ReviewsService._save_reviews(reviews)

        products_cache = CatalogService._load_cache()
        districts_cache = GeoService._load_cache()
        all_prods = CatalogService.get_all_products()
        products_by_id = {
            str(item.get("id")): item
            for item in all_prods
            if isinstance(item, dict) and item.get("id")
        }

        targets = ReviewsService._city_targets(products_cache, districts_cache)
        if not targets:
            logger.warning("[ReviewGen] No cache intersection (trigger=%s)", trigger_label)
            return 0

        random.shuffle(targets)
        existing_norms = ReviewsService._collect_recent_texts(reviews)
        now_base = datetime.now()
        time_cursor = now_base
        new_batch_iso: list[str] = []
        count_gen = 0

        logger.info("[ReviewGen] Local batch for %s cities (trigger=%s)", len(targets), trigger_label)

        for target_p_key, target_d_key in targets:
            districts_list = districts_cache.get(target_d_key, [])
            if not districts_list:
                logger.warning("[ReviewGen] No districts for %s", target_p_key)
                continue

            prod_entry = products_cache.get(target_p_key, {})
            prod_ids = prod_entry.get("ids", []) if isinstance(prod_entry, dict) else prod_entry
            real_products = [
                products_by_id[str(pid)]
                for pid in prod_ids or []
                if str(pid) in products_by_id
            ]
            if not real_products:
                logger.warning("[ReviewGen] No products for %s", target_p_key)
                continue

            target_product = random.choice(real_products)
            target_district = ReviewsService._format_location_display(random.choice(districts_list))
            city_display = ReviewsService._format_city_display(target_p_key)
            region_display = ReviewsService._region_from_key(target_d_key) or ReviewsService._region_from_key(target_p_key)
            product_name = target_product.get("name", "Unknown product")
            text = ReviewsService._generate_local_review_text(
                product_name,
                city_display,
                target_district,
                target_product.get("base_price"),
                forbidden_norms=existing_norms,
            )

            if count_gen == 0:
                time_cursor = (
                    now_base
                    - timedelta(minutes=random.randint(0, 3), seconds=random.randint(0, 55))
                ).replace(microsecond=0)
            else:
                time_cursor = (
                    time_cursor
                    - timedelta(minutes=random.randint(5, 18), seconds=random.randint(5, 55))
                ).replace(microsecond=0)

            user_display = ReviewsService._generate_username()
            new_review = {
                "id": secrets.token_hex(6),
                "user": user_display,
                "base_name": user_display,
                "text": text,
                "product": product_name,
                "product_id": target_product.get("id"),
                "city": city_display,
                "region": region_display or None,
                "district": target_district,
                "item_info": ReviewsService._build_item_info(product_name, city_display, target_district, region_display),
                "stars": 5 if random.random() > 0.12 else 4,
                "iso_date": time_cursor.isoformat(),
                "display_date": time_cursor.strftime("%d.%m %H:%M"),
                "hidden": False,
            }
            reviews.append(new_review)
            count_gen += 1
            new_batch_iso.append(new_review["iso_date"])

        ReviewsService._save_last_batch(new_batch_iso)
        reviews.sort(key=lambda x: x["iso_date"])
        ReviewsService._save_reviews(reviews)

        elapsed = perf_counter() - started
        logger.info(
            "[ReviewGen] Generation finished (%s), total=%s, elapsed=%.2fs",
            trigger_label,
            count_gen,
            elapsed,
        )
        return count_gen

    @staticmethod
    async def generate_new_review_task(trigger: str = "auto"):
        return await asyncio.to_thread(ReviewsService._generate_reviews_batch, trigger or "auto")


    @staticmethod
    async def get_reviews_text(limit=10):
        # Чтение отзывов не должно вызывать автогенерацию.
        ReviewsService.check_and_update()
        reviews = ReviewsService._load_reviews()
        
        selection = reviews[-limit:]
        selection.reverse() 
        
        rating = ReviewsService.rating_stats_from_reviews(
            [item for item in reviews if not item.get("hidden")]
        )
        lines = [ReviewsService.format_reviews_header(rating=rating)]
        for r in selection:
            lines.append(ReviewsService.format_public_review_card(r))
        return "\n".join(lines)

    @staticmethod
    def get_filtered_reviews(
        city_filter: str = None,
        product_filter: str = None,
        *,
        include_hidden: bool = False,
        newest_first: bool = True,
        product_id_filter: str = None,
        region_filter: str = None,
    ) -> list[dict]:
        """Return reviews filtered by city/product using the same rules as pagination."""
        ReviewsService.check_and_update()
        reviews = ReviewsService._load_reviews()
        review_items = list(reversed(reviews)) if newest_first else list(reviews)

        filtered = []

        cf_norm, embedded_region = ReviewsService._city_name_and_region(city_filter) if city_filter else ("", "")
        target_region = str(region_filter or embedded_region or "").strip()
        require_region = bool(target_region) and len(ReviewsService._homonym_regions(cf_norm)) > 1
        pf_norm = ReviewsService._normalize_product_key(product_filter) if product_filter else ""
        pid_norm = str(product_id_filter or "").strip()
        if city_filter and not cf_norm:
            return []
        if product_filter and not pf_norm:
            return []
        if product_id_filter is not None and not pid_norm:
            return []

        for r in review_items:
            if not include_hidden and r.get("hidden"):
                continue

            review_product = str(r.get("product") or "")
            review_product_id = str(r.get("product_id") or "").strip()
            item_product, _, _ = ReviewsService._split_item_info(r.get("item_info"))

            if cf_norm:
                if not ReviewsService._review_matches_place(
                    r,
                    city_filter,
                    target_region,
                    require_region=require_region,
                ):
                    continue

            if pid_norm:
                if review_product_id:
                    if review_product_id != pid_norm:
                        continue
                elif pf_norm:
                    product_candidates = [review_product, item_product]
                    product_name_match = any(ReviewsService._normalize_product_key(value) == pf_norm for value in product_candidates if value)
                    if not product_name_match:
                        continue
            elif pf_norm:
                product_candidates = [review_product, item_product]
                if not any(ReviewsService._normalize_product_key(value) == pf_norm for value in product_candidates if value):
                    continue

            filtered.append(r)

        return filtered

    @staticmethod
    def get_paginated_review(index: int, city_filter: str = None, product_filter: str = None, product_id_filter: str = None, region_filter: str = None):
        """Returns (review_obj, total_count) for specific index (0-based, 0 = newest)."""
        filtered = ReviewsService.get_filtered_reviews(
            city_filter,
            product_filter,
            product_id_filter=product_id_filter,
            region_filter=region_filter,
        )
            
        total = len(filtered)
        
        if total == 0:
            return None, 0
            
        # Wrap index
        if index < 0: index = 0
        if index >= total: index = total - 1
        
        return filtered[index], total

    @staticmethod
    def get_admin_review(index: int, include_hidden: bool = True):
        """Returns (review_obj, total_count) for admin moderation, newest first."""
        reviews = ReviewsService._load_reviews()
        reviews_rev = list(reversed(reviews))
        if not include_hidden:
            reviews_rev = [r for r in reviews_rev if not r.get("hidden")]

        total = len(reviews_rev)
        if total == 0:
            return None, 0

        if index < 0:
            index = 0
        if index >= total:
            index = total - 1

        return reviews_rev[index], total

    @staticmethod
    def get_last_batch_admin_review(index: int, include_hidden: bool = True):
        """Returns (review_obj, total_count) for last generated batch, newest first."""
        iso_dates = ReviewsService._load_last_batch()
        if not iso_dates:
            return None, 0

        reviews = ReviewsService._load_reviews()
        reviews_rev = list(reversed(reviews))
        target_set = set(iso_dates)
        filtered = [r for r in reviews_rev if r.get("iso_date") in target_set]
        if not include_hidden:
            filtered = [r for r in filtered if not r.get("hidden")]

        total = len(filtered)
        if total == 0:
            return None, 0

        if index < 0:
            index = 0
        if index >= total:
            index = total - 1

        return filtered[index], total

    @staticmethod
    def export_last_batch_texts() -> dict:
        """Export editable payload for last generated batch.

        Payload schema:
        {
          "meta": {...},
                    "reviews": [{"id": "...", "text": "...", "district": "..."}, ...]
        }
        """
        iso_dates = set(ReviewsService._load_last_batch())
        reviews = ReviewsService._load_reviews()
        if not iso_dates or not reviews:
            return {
                "meta": {
                    "source": "last_generated_batch",
                    "count": 0,
                },
                "reviews": [],
            }

        rows = []
        for item in reviews:
            if item.get("iso_date") in iso_dates:
                _, _, district = ReviewsService._split_item_info(item.get("item_info", ""))
                rows.append({
                    "id": item.get("id", ""),
                    "text": item.get("text", ""),
                    "district": district,
                    "city": item.get("city", ""),
                    "item_info": item.get("item_info", ""),
                    "hidden": bool(item.get("hidden", False)),
                })

        return {
            "meta": {
                "source": "last_generated_batch",
                "count": len(rows),
            },
            "reviews": rows,
        }

    @staticmethod
    def apply_last_batch_texts(payload) -> dict:
        """Apply text and district updates for last generated batch only.

        Accepts either list[{id,...}] or dict with key "reviews".
        Returns counters: updated, skipped, invalid.
        """
        if isinstance(payload, dict):
            raw_rows = payload.get("reviews", [])
        else:
            raw_rows = payload

        if not isinstance(raw_rows, list):
            return {"updated": 0, "skipped": 0, "invalid": 1}

        updates: dict[str, dict[str, str]] = {}
        invalid = 0
        for row in raw_rows:
            if not isinstance(row, dict):
                invalid += 1
                continue
            review_id = str(row.get("id", "")).strip()
            if not review_id:
                invalid += 1
                continue

            patch: dict[str, str] = {}

            if "text" in row:
                text = row.get("text")
                if not isinstance(text, str) or not text.strip():
                    invalid += 1
                    continue
                patch["text"] = text.strip()

            if "district" in row:
                district = row.get("district")
                if not isinstance(district, str) or not district.strip():
                    invalid += 1
                    continue
                patch["district"] = district.strip()

            if "item_info" in row:
                item_info = row.get("item_info")
                if not isinstance(item_info, str) or not item_info.strip():
                    invalid += 1
                    continue
                patch["item_info"] = item_info.strip()

            if not patch:
                invalid += 1
                continue
            updates[review_id] = patch

        if not updates:
            return {"updated": 0, "skipped": 0, "invalid": max(1, invalid)}

        batch_iso = set(ReviewsService._load_last_batch())
        reviews = ReviewsService._load_reviews()
        if not batch_iso or not reviews:
            return {"updated": 0, "skipped": len(updates), "invalid": invalid}

        updated = 0
        now = datetime.now().isoformat()
        for item in reviews:
            if item.get("iso_date") not in batch_iso:
                continue
            review_id = item.get("id")
            if review_id in updates:
                patch = updates[review_id]
                changed = False

                new_text = patch.get("text")
                if new_text is not None and item.get("text") != new_text:
                    item["text"] = new_text
                    changed = True

                new_item_info = None
                if "district" in patch:
                    item["district"] = patch["district"]
                    current_product, current_city, _ = ReviewsService._split_item_info(item.get("item_info", ""))
                    product_name = current_product
                    city_name = str(item.get("city") or current_city).strip() or current_city

                    if "item_info" in patch:
                        patch_product, patch_city, _ = ReviewsService._split_item_info(patch["item_info"])
                        if patch_product:
                            product_name = patch_product
                        if patch_city:
                            city_name = patch_city

                    rebuilt = ReviewsService._build_item_info(product_name, city_name, patch["district"])
                    if rebuilt:
                        new_item_info = rebuilt
                elif "item_info" in patch:
                    new_item_info = patch["item_info"]

                if new_item_info is not None and item.get("item_info") != new_item_info:
                    item["item_info"] = new_item_info
                    changed = True

                if changed:
                    item["edited_at"] = now
                    updated += 1

        if updated:
            ReviewsService._save_reviews(reviews)

        skipped = max(0, len(updates) - updated)
        return {"updated": updated, "skipped": skipped, "invalid": invalid}

    @staticmethod
    def set_review_hidden(review_id: str, hidden: bool = True) -> bool:
        reviews = ReviewsService._load_reviews()
        changed = False
        for item in reviews:
            if item.get("id") == review_id:
                item["hidden"] = bool(hidden)
                changed = True
                break
        if changed:
            ReviewsService._save_reviews(reviews)
        return changed

    @staticmethod
    def update_review_text(review_id: str, new_text: str) -> bool:
        reviews = ReviewsService._load_reviews()
        changed = False
        now = datetime.now().isoformat()
        for item in reviews:
            if item.get("id") == review_id:
                item["text"] = new_text
                item["edited_at"] = now
                changed = True
                break
        if changed:
            ReviewsService._save_reviews(reviews)
        return changed

    @staticmethod
    def normalize_stars(value, default: int = 5) -> int:
        try:
            stars = int(value)
        except (TypeError, ValueError):
            stars = default
        return max(1, min(5, stars))

    @staticmethod
    def rating_stats_from_reviews(reviews: list[dict] | None) -> dict:
        counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        total = 0
        star_sum = 0
        for item in reviews or []:
            stars = ReviewsService.normalize_stars(item.get("stars"))
            counts[stars] += 1
            star_sum += stars
            total += 1
        average = (star_sum / total) if total else 0.0
        return {
            "count": total,
            "sum": star_sum,
            "average": round(average, 2),
            "counts": counts,
        }

    @staticmethod
    def get_rating_stats(
        city_filter: str = None,
        product_filter: str = None,
        *,
        include_hidden: bool = False,
        product_id_filter: str = None,
        region_filter: str = None,
    ) -> dict:
        reviews = ReviewsService.get_filtered_reviews(
            city_filter,
            product_filter,
            include_hidden=include_hidden,
            product_id_filter=product_id_filter,
            region_filter=region_filter,
        )
        return ReviewsService.rating_stats_from_reviews(reviews)

    @staticmethod
    def format_rating_line(stats: dict | None = None, *, prefix: str = "Рейтинг") -> str:
        data = stats or {}
        count = int(data.get("count") or 0)
        if count <= 0:
            return f"{prefix} · нет данных"
        avg = float(data.get("average") or 0)
        avg_text = f"{avg:.2f}".replace(".", ",")
        return f"{prefix} · <b>{avg_text}</b> / 5 · {count}"

    @staticmethod
    def format_rating_compact(stats: dict | None = None) -> str:
        data = stats or {}
        count = int(data.get("count") or 0)
        if count <= 0:
            return ""
        avg = float(data.get("average") or 0)
        avg_text = f"{avg:.2f}".replace(".", ",")
        return f"<b>{count}</b> · {avg_text} / 5"

    @staticmethod
    def format_reviews_header(
        *,
        rating: dict | None = None,
        page_label: str | None = None,
        city: str | None = None,
        product: str | None = None,
        empty_note: str | None = None,
    ) -> str:
        lines = [
            "⭐ <b>Отзывы PlayStation™Store</b>",
            "<code>После покупки оценка открывается в течение суток</code>",
            "👤 <b>Скрыто</b>",
        ]
        if city:
            lines.append(f"🌍 Город: <b>{html.escape(str(city))}</b>")
        if product:
            lines.append(f"🏷 Позиция: <b>{html.escape(str(product))}</b>")
        if empty_note:
            lines.append(empty_note)
            return "\n".join(lines)
        compact = ReviewsService.format_rating_compact(rating)
        if compact:
            lines.append(compact)
        if page_label:
            lines.append(page_label)
        return "\n".join(lines)

    @staticmethod
    def format_public_review_card(review: dict | None = None, *, index: int | None = None) -> str:
        item = review or {}
        filled = ReviewsService.normalize_stars(item.get("stars", 5))
        stars = "★" * filled + "☆" * (5 - filled)
        date = html.escape(str(item.get("display_date") or "")).strip()
        item_info = html.escape(str(item.get("item_info") or "")).strip()
        text = html.escape(str(item.get("text") or "")).strip()
        author = html.escape(ReviewsService.HIDDEN_USERNAME)
        head = f"<b>{int(index):02d}</b>  {stars}" if index is not None else stars
        lines = [head]
        if date:
            lines.append(f"<code>{date}</code>")
        lines.append(f"👤 <b>{author}</b>")
        if item_info:
            lines.append(item_info)
        if text:
            lines.append(f"<i>{text}</i>")
        return "▫️ " + "\n".join(lines)

    @staticmethod
    def update_review_stars(review_id: str, stars: int) -> bool:
        stars = ReviewsService.normalize_stars(stars)
        reviews = ReviewsService._load_reviews()
        now = datetime.now().isoformat()
        for item in reviews:
            if item.get("id") != review_id:
                continue
            if ReviewsService.normalize_stars(item.get("stars")) == stars:
                return True
            item["stars"] = stars
            item["edited_at"] = now
            ReviewsService._save_reviews(reviews)
            return True
        return False

    @staticmethod
    def delete_review(review_id: str) -> bool:
        reviews = ReviewsService._load_reviews()
        before = len(reviews)
        reviews = [r for r in reviews if r.get("id") != review_id]
        if len(reviews) == before:
            return False
        ReviewsService._save_reviews(reviews)
        return True

    @staticmethod
    def find_similar_reviews(review_id: str, threshold: float = 0.82, limit: int = 5) -> list[dict]:
        reviews = ReviewsService._load_reviews()
        target = None
        for item in reviews:
            if item.get("id") == review_id:
                target = item
                break
        if not target:
            return []

        target_norm = ReviewsService._normalize_review_text(target.get("text"))
        if not target_norm:
            return []

        matches: list[dict] = []
        for item in reviews:
            if item.get("id") == review_id:
                continue
            other_norm = ReviewsService._normalize_review_text(item.get("text"))
            if not other_norm:
                continue
            ratio = SequenceMatcher(None, target_norm, other_norm).ratio()
            if ratio >= threshold:
                matches.append({
                    "id": item.get("id"),
                    "ratio": ratio,
                    "text": item.get("text", ""),
                    "user": item.get("user", ReviewsService.HIDDEN_USERNAME),
                    "city": item.get("city", ""),
                    "item_info": item.get("item_info", ""),
                    "display_date": item.get("display_date", ""),
                    "hidden": item.get("hidden", False)
                })

        matches.sort(key=lambda x: x["ratio"], reverse=True)
        if limit and len(matches) > limit:
            matches = matches[:limit]
        return matches

