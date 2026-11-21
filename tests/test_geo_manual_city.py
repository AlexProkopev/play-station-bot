import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import geo as geo_module
from app.services.geo import GeoService
from app.services.postgres_store import PostgresDocumentStore


class ManualCityCacheTests(unittest.TestCase):
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

    def test_create_manual_city_writes_districts_and_population(self) -> None:
        result = GeoService.create_manual_city(
            "Луганск",
            "Луганская область",
            ["Ленинский", "Жовтневый", "Каменнобродский"],
        )

        self.assertEqual(result["cache_key"], "луганск (луганская область)")
        self.assertEqual(result["city"], "Луганск")
        self.assertEqual(result["region"], "Луганская область")
        self.assertFalse(result["replaced"])
        self.assertEqual(
            result["districts"],
            ["Ленинский", "Жовтневый", "Каменнобродский"],
        )

        cache = GeoService._load_cache()
        self.assertEqual(cache[result["cache_key"]], result["districts"])
        self.assertEqual(result["population"], 100000)
        self.assertEqual(
            GeoService._get_cached_population("Луганск", "Луганская область"),
            100000,
        )

        found = GeoService._fallback_cities_from_cache("Луганск")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Луганск")
        self.assertEqual(found[0]["region"], "Луганская Область")

        districts, cache_key = GeoService.get_districts_for_city("Луганск")
        self.assertEqual(cache_key, result["cache_key"])
        self.assertEqual(districts, result["districts"])

    def test_create_manual_city_requires_city_region_and_districts(self) -> None:
        with self.assertRaises(ValueError):
            GeoService.create_manual_city("", "Луганская область", ["Центр"])
        with self.assertRaises(ValueError):
            GeoService.create_manual_city("Луганск", "", ["Центр"])
        with self.assertRaises(ValueError):
            GeoService.create_manual_city("Луганск", "Луганская область", [])
        with self.assertRaises(ValueError):
            GeoService.create_manual_city("Луганск", "Луганская область", ["  ", ","])

    def test_create_manual_city_replaces_existing_districts(self) -> None:
        GeoService.create_manual_city("Луганск", "Луганская область", ["Старый"])
        result = GeoService.create_manual_city(
            "Луганск",
            "Луганская область",
            ["Ленинский", "Артемовский"],
        )
        self.assertTrue(result["replaced"])
        cache = GeoService._load_cache()
        self.assertEqual(cache[result["cache_key"]], ["Ленинский", "Артемовский"])

    def test_create_manual_city_stores_explicit_population(self) -> None:
        result = GeoService.create_manual_city(
            "Луганск",
            "Луганская область",
            ["Ленинский"],
            population=425000,
        )
        self.assertEqual(result["population"], 425000)
        self.assertEqual(
            GeoService._get_cached_population("Луганск", "Луганская область"),
            425000,
        )


if __name__ == "__main__":
    unittest.main()
