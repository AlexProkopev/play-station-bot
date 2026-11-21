import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import geo as geo_module
from app.services.geo import GeoService
from app.services.postgres_store import PostgresDocumentStore


class GeoCityKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_cache_file = geo_module.CACHE_FILE
        self._original_population_file = geo_module.CITY_POPULATION_FILE
        geo_module.CACHE_FILE = os.path.join(self._temp_dir.name, "districts_cache.json")
        geo_module.CITY_POPULATION_FILE = os.path.join(
            self._temp_dir.name, "city_population_cache.json"
        )
        self._pg_patch = patch.object(PostgresDocumentStore, "is_enabled", return_value=False)
        self._pg_patch.start()

    def tearDown(self) -> None:
        self._pg_patch.stop()
        geo_module.CACHE_FILE = self._original_cache_file
        geo_module.CITY_POPULATION_FILE = self._original_population_file
        self._temp_dir.cleanup()

    def test_nested_region_parentheses_are_parsed(self) -> None:
        name, region = GeoService._parse_cache_city_key(
            "якутск (республика саха (якутия))"
        )
        self.assertEqual(name, "Якутск")
        self.assertEqual(region, "Республика Саха (Якутия)")
        self.assertEqual(
            GeoService._normalize_city_name("якутск (республика саха (якутия))"),
            "якутск",
        )
        self.assertEqual(GeoService._parse_cache_city_key("якутск ()"), ("Якутск", ""))

    def test_fallback_hides_plain_duplicate_when_region_exists(self) -> None:
        GeoService._write_cache({
            "якутск": ["Автодорожный", "Центральный"],
            "якутск ()": ["Автодорожный", "Центральный"],
            "якутск (республика саха (якутия))": ["ул. Покровский тракт"],
        })

        found = GeoService._fallback_cities_from_cache("Якутск")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Якутск")
        self.assertEqual(found[0]["region"], "Республика Саха (Якутия)")

    def test_admin_lookup_merges_streets_and_districts(self) -> None:
        GeoService._write_cache({
            "якутск": ["Автодорожный", "Центральный"],
            "якутск ()": ["Автодорожный"],
            "якутск (республика саха (якутия))": ["ул. Покровский тракт"],
        })

        districts, cache_key = GeoService.get_districts_for_city("Якутск")
        self.assertEqual(cache_key, "якутск (республика саха (якутия))")
        self.assertEqual(
            districts,
            ["ул. Покровский тракт", "Автодорожный", "Центральный"],
        )
        self.assertEqual(
            list(GeoService._load_cache().keys()),
            ["якутск (республика саха (якутия))"],
        )

    def test_resolve_without_region_prefers_single_regional_key(self) -> None:
        GeoService._write_cache({
            "якутск": ["Центральный"],
            "якутск (республика саха (якутия))": ["ул. Покровский тракт"],
        })
        self.assertEqual(
            GeoService._resolve_cache_key("Якутск"),
            "якутск (республика саха (якутия))",
        )

    def test_empty_region_does_not_build_empty_parentheses_key(self) -> None:
        self.assertEqual(GeoService.build_city_cache_key("Якутск", ""), "якутск")
        self.assertEqual(
            GeoService.build_city_cache_key("Якутск", "Республика Саха (Якутия)"),
            "якутск (республика саха (якутия))",
        )

    def test_lookup_by_full_admin_label_with_nested_region(self) -> None:
        GeoService._write_cache({
            "якутск (республика саха (якутия))": ["Центральный"],
        })
        districts, cache_key = GeoService.get_districts_for_city(
            "Якутск (Республика Саха (Якутия))"
        )
        self.assertEqual(cache_key, "якутск (республика саха (якутия))")
        self.assertEqual(districts, ["Центральный"])

    def test_district_editor_callbacks_fit_telegram_limit(self) -> None:
        from app.keyboards import get_admin_districts_menu

        markup = get_admin_districts_menu(
            "Якутск (Республика Саха (Якутия))",
            ["Центральный"],
        )
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    def test_same_city_name_keeps_separate_regions(self) -> None:
        GeoService._write_cache({
            "благовещенск (башкортостан)": ["Пробный район"],
            "благовещенск (амурская область)": ["Центральный"],
        })

        GeoService.consolidate_duplicate_city_keys("Благовещенск")
        cache = GeoService._load_cache()
        self.assertEqual(cache["благовещенск (башкортостан)"], ["Пробный район"])
        self.assertEqual(cache["благовещенск (амурская область)"], ["Центральный"])

        self.assertIsNone(GeoService._resolve_cache_key("Благовещенск"))
        amur, amur_key = GeoService.get_districts_for_city("Благовещенск (Амурская Область)")
        bashkir, bashkir_key = GeoService.get_districts_for_city("Благовещенск (Башкортостан)")
        self.assertEqual(amur_key, "благовещенск (амурская область)")
        self.assertEqual(amur, ["Центральный"])
        self.assertEqual(bashkir_key, "благовещенск (башкортостан)")
        self.assertEqual(bashkir, ["Пробный район"])

        GeoService.add_districts("Благовещенск (Амурская Область)", ["Новый"])
        cache = GeoService._load_cache()
        self.assertIn("Новый", cache["благовещенск (амурская область)"])
        self.assertEqual(cache["благовещенск (башкортостан)"], ["Пробный район"])

        found = GeoService._fallback_cities_from_cache("Благовещенск")
        regions = sorted(item["region"] for item in found)
        self.assertEqual(len(found), 2)
        self.assertEqual(regions, ["Амурская Область", "Башкортостан"])

    def test_attach_region_moves_plain_city_key(self) -> None:
        GeoService._write_cache({"лисичанск": ["11я Школа", "Центр"]})
        key = GeoService.attach_region_to_plain_city("Лисичанск", "Луганская область")
        self.assertEqual(key, "лисичанск (луганская область)")
        cache = GeoService._load_cache()
        self.assertNotIn("лисичанск", cache)
        self.assertEqual(cache[key], ["11я Школа", "Центр"])
        districts, cache_key = GeoService.get_districts_for_city("Лисичанск (Луганская область)")
        self.assertEqual(cache_key, key)
        self.assertEqual(districts, ["11я Школа", "Центр"])


if __name__ == "__main__":
    unittest.main()
