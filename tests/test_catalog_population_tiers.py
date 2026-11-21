import json
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from app.services import catalog as catalog_module
from app.services.catalog import (
    COURIER_JOB_ID,
    CatalogService,
    _build_compact_city_product_ids,
    _build_non_small_city_product_ids,
)


class CatalogPopulationTierTests(unittest.TestCase):
    def test_compact_city_generates_six_to_seven_products(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cache_file = catalog_module.PRODUCTS_CACHE_FILE
            original_price_seed = catalog_module.PRICE_SEED_FILE

            try:
                catalog_module.PRODUCTS_CACHE_FILE = os.path.join(temp_dir, "products_cache.json")
                catalog_module.PRICE_SEED_FILE = os.path.join(temp_dir, "price_seed.json")

                with open(catalog_module.PRICE_SEED_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"seed": "123456"}, file_obj, ensure_ascii=False)

                with patch.object(CatalogService, "_get_city_multiplier", return_value=1.0):
                    products = CatalogService.get_products_for_city("Павловск", 25000)

                self.assertGreaterEqual(len(products), 6)
                self.assertLessEqual(len(products), 7)
                self.assertFalse(any(item.get("id") == COURIER_JOB_ID for item in products))
            finally:
                catalog_module.PRODUCTS_CACHE_FILE = original_cache_file
                catalog_module.PRICE_SEED_FILE = original_price_seed

    def test_small_city_generates_six_to_nine_products(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cache_file = catalog_module.PRODUCTS_CACHE_FILE
            original_price_seed = catalog_module.PRICE_SEED_FILE

            try:
                catalog_module.PRODUCTS_CACHE_FILE = os.path.join(temp_dir, "products_cache.json")
                catalog_module.PRICE_SEED_FILE = os.path.join(temp_dir, "price_seed.json")

                with open(catalog_module.PRICE_SEED_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"seed": "123456"}, file_obj, ensure_ascii=False)

                with patch.object(CatalogService, "_get_city_multiplier", return_value=1.0):
                    products = CatalogService.get_products_for_city("Россошь", 40000)

                self.assertGreaterEqual(len(products), 6)
                self.assertLessEqual(len(products), 9)
                self.assertFalse(any(item.get("id") == COURIER_JOB_ID for item in products))
            finally:
                catalog_module.PRODUCTS_CACHE_FILE = original_cache_file
                catalog_module.PRICE_SEED_FILE = original_price_seed

    def test_p10_appears_in_about_ninety_percent_of_cities(self) -> None:
        compact_hits = 0
        large_hits = 0
        samples = 400
        for seed in range(samples):
            compact_ids = _build_compact_city_product_ids(random.Random(seed))
            large_ids = _build_non_small_city_product_ids(random.Random(seed + 10_000))
            compact_hits += int("p10" in compact_ids)
            large_hits += int("p10" in large_ids)

        compact_rate = compact_hits / samples
        large_rate = large_hits / samples
        self.assertGreaterEqual(compact_rate, 0.85)
        self.assertLessEqual(compact_rate, 0.95)
        self.assertGreaterEqual(large_rate, 0.85)
        self.assertLessEqual(large_rate, 0.95)


if __name__ == "__main__":
    unittest.main()