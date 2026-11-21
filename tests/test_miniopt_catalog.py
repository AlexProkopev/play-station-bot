import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import catalog as catalog_module
from app.services.catalog import CatalogService


class MinioptCatalogTests(unittest.TestCase):
    def test_city_generates_three_to_seven_products(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cache_file = CatalogService.MINIOPT_PRODUCTS_CACHE_FILE
            original_price_seed = catalog_module.PRICE_SEED_FILE
            original_products = CatalogService.MINIOPT_ALL_PRODUCTS
            original_stash = CatalogService.MINIOPT_PRODUCT_STASH_TYPES
            original_types = CatalogService.MINIOPT_STASH_TYPES

            try:
                CatalogService.MINIOPT_PRODUCTS_CACHE_FILE = os.path.join(temp_dir, "miniopt_products_cache.json")
                catalog_module.PRICE_SEED_FILE = os.path.join(temp_dir, "price_seed.json")
                CatalogService.MINIOPT_ALL_PRODUCTS = [
                    {"id": f"mo{idx}", "name": f"Miniopt {idx}", "base_price": 1000 + idx * 100}
                    for idx in range(1, 21)
                ]
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {}
                CatalogService.MINIOPT_STASH_TYPES = ["Тайник", "Прикоп земляной", "Магнит"]

                with open(catalog_module.PRICE_SEED_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"seed": "123456"}, file_obj, ensure_ascii=False)

                with patch.object(CatalogService, "_get_city_multiplier", return_value=1.0):
                    products = CatalogService.get_miniopt_products_for_city("Павловск", 25000)

                self.assertGreaterEqual(len(products), 3)
                self.assertLessEqual(len(products), 7)
                ids = [item["id"] for item in products]
                self.assertEqual(len(ids), len(set(ids)))
            finally:
                CatalogService.MINIOPT_PRODUCTS_CACHE_FILE = original_cache_file
                catalog_module.PRICE_SEED_FILE = original_price_seed
                CatalogService.MINIOPT_ALL_PRODUCTS = original_products
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES = original_stash
                CatalogService.MINIOPT_STASH_TYPES = original_types

    def test_miniopt_price_applies_discount_only_with_markup(self) -> None:
        original_products = CatalogService.MINIOPT_ALL_PRODUCTS
        original_stash = CatalogService.MINIOPT_PRODUCT_STASH_TYPES
        original_types = CatalogService.MINIOPT_STASH_TYPES

        try:
            CatalogService.MINIOPT_ALL_PRODUCTS = [
                {"id": "mo1", "name": "Miniopt test", "base_price": 5000}
            ]
            CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {"mo1": ["Тайник", "Прикоп земляной"]}
            CatalogService.MINIOPT_STASH_TYPES = ["Тайник", "Прикоп земляной"]

            with patch.object(CatalogService, "_get_city_multiplier", return_value=1.2):
                product = CatalogService.get_miniopt_product_by_id("mo1", city_name="Томск")

            self.assertIsNotNone(product)
            self.assertEqual(product["price"], 5100)

            with patch.object(CatalogService, "_get_city_multiplier", return_value=1.0):
                product_no_markup = CatalogService.get_miniopt_product_by_id("mo1", city_name="Томск")

            self.assertIsNotNone(product_no_markup)
            self.assertEqual(product_no_markup["price"], 5000)
        finally:
            CatalogService.MINIOPT_ALL_PRODUCTS = original_products
            CatalogService.MINIOPT_PRODUCT_STASH_TYPES = original_stash
            CatalogService.MINIOPT_STASH_TYPES = original_types

    def test_update_and_delete_miniopt_product(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_products_file = CatalogService.MINIOPT_PRODUCTS_DB_FILE
            original_cache_file = CatalogService.MINIOPT_PRODUCTS_CACHE_FILE
            original_inventory_file = CatalogService.MINIOPT_INVENTORY_FILE
            original_stash_file = CatalogService.MINIOPT_STASH_TYPES_FILE
            original_products = CatalogService.MINIOPT_ALL_PRODUCTS
            original_stash = CatalogService.MINIOPT_PRODUCT_STASH_TYPES
            original_types = CatalogService.MINIOPT_STASH_TYPES

            try:
                CatalogService.MINIOPT_PRODUCTS_DB_FILE = os.path.join(temp_dir, "miniopt_all_products.json")
                CatalogService.MINIOPT_PRODUCTS_CACHE_FILE = os.path.join(temp_dir, "miniopt_products_cache.json")
                CatalogService.MINIOPT_INVENTORY_FILE = os.path.join(temp_dir, "miniopt_inventory.json")
                CatalogService.MINIOPT_STASH_TYPES_FILE = os.path.join(temp_dir, "miniopt_stash_types.json")
                CatalogService.MINIOPT_ALL_PRODUCTS = [
                    {"id": "mo1", "name": "Old name", "base_price": 2000}
                ]
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES = {"mo1": ["Тайник", "Прикоп земляной"]}
                CatalogService.MINIOPT_STASH_TYPES = ["Тайник", "Прикоп земляной"]

                with open(CatalogService.MINIOPT_PRODUCTS_DB_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump(CatalogService.MINIOPT_ALL_PRODUCTS, file_obj, ensure_ascii=False)

                with open(CatalogService.MINIOPT_PRODUCTS_CACHE_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"tomsk": {"ids": ["mo1"], "stash_types": {"mo1": ["Тайник", "Прикоп земляной"]}}}, file_obj, ensure_ascii=False)
                with open(CatalogService.MINIOPT_INVENTORY_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"tomsk": {"mo1": ["Центр"]}}, file_obj, ensure_ascii=False)

                updated = CatalogService.update_miniopt_product("mo1", "New name", 3100)
                deleted = CatalogService.delete_miniopt_product("mo1")

                self.assertTrue(updated)
                self.assertTrue(deleted)
                self.assertEqual(CatalogService.MINIOPT_ALL_PRODUCTS, [])

                with open(CatalogService.MINIOPT_PRODUCTS_CACHE_FILE, "r", encoding="utf-8") as file_obj:
                    cache = json.load(file_obj)
                self.assertEqual(cache["tomsk"]["ids"], [])

                with open(CatalogService.MINIOPT_INVENTORY_FILE, "r", encoding="utf-8") as file_obj:
                    inventory = json.load(file_obj)
                self.assertEqual(inventory, {})
            finally:
                CatalogService.MINIOPT_PRODUCTS_DB_FILE = original_products_file
                CatalogService.MINIOPT_PRODUCTS_CACHE_FILE = original_cache_file
                CatalogService.MINIOPT_INVENTORY_FILE = original_inventory_file
                CatalogService.MINIOPT_STASH_TYPES_FILE = original_stash_file
                CatalogService.MINIOPT_ALL_PRODUCTS = original_products
                CatalogService.MINIOPT_PRODUCT_STASH_TYPES = original_stash
                CatalogService.MINIOPT_STASH_TYPES = original_types


if __name__ == "__main__":
    unittest.main()