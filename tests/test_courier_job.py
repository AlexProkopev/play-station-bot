import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import catalog as catalog_module
from app.services.catalog import COURIER_JOB_ID, COURIER_JOB_PRICE, CatalogService
from app.keyboards import get_products_keyboard


class CourierJobCatalogTests(unittest.TestCase):
    def test_courier_job_not_in_city_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cache_file = catalog_module.PRODUCTS_CACHE_FILE
            original_price_seed = catalog_module.PRICE_SEED_FILE
            try:
                catalog_module.PRODUCTS_CACHE_FILE = os.path.join(temp_dir, "products_cache.json")
                catalog_module.PRICE_SEED_FILE = os.path.join(temp_dir, "price_seed.json")
                with open(catalog_module.PRICE_SEED_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"seed": "123456"}, file_obj, ensure_ascii=False)

                with patch.object(CatalogService, "_get_city_multiplier", return_value=1.0), patch.object(
                    catalog_module.PostgresDocumentStore,
                    "is_enabled",
                    return_value=False,
                ):
                    products = CatalogService.get_products_for_city("Казань", 1_250_000)

                self.assertFalse(any(item.get("id") == COURIER_JOB_ID for item in products))
                self.assertEqual(
                    CatalogService.get_product_by_id(COURIER_JOB_ID, city_name="Казань")["price"],
                    COURIER_JOB_PRICE,
                )
            finally:
                catalog_module.PRODUCTS_CACHE_FILE = original_cache_file
                catalog_module.PRICE_SEED_FILE = original_price_seed

    def test_catalog_keyboard_has_no_vacancy_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cache_file = catalog_module.PRODUCTS_CACHE_FILE
            original_price_seed = catalog_module.PRICE_SEED_FILE
            try:
                catalog_module.PRODUCTS_CACHE_FILE = os.path.join(temp_dir, "products_cache.json")
                catalog_module.PRICE_SEED_FILE = os.path.join(temp_dir, "price_seed.json")
                with open(catalog_module.PRICE_SEED_FILE, "w", encoding="utf-8") as file_obj:
                    json.dump({"seed": "123456"}, file_obj, ensure_ascii=False)

                with patch.object(CatalogService, "_get_city_multiplier", return_value=1.0), patch.object(
                    catalog_module.PostgresDocumentStore,
                    "is_enabled",
                    return_value=False,
                ):
                    markup = get_products_keyboard("Казань", 1_250_000)
            finally:
                catalog_module.PRODUCTS_CACHE_FILE = original_cache_file
                catalog_module.PRICE_SEED_FILE = original_price_seed

        product_buttons = [
            btn
            for row in markup.inline_keyboard
            for btn in row
            if str(btn.callback_data or "").startswith("prod_")
        ]
        self.assertTrue(product_buttons)
        self.assertFalse(any(f"prod_{COURIER_JOB_ID}" == str(btn.callback_data) for btn in product_buttons))
        self.assertFalse(any("ВАКАНСИЯ" in str(btn.text) for btn in product_buttons))


if __name__ == "__main__":
    unittest.main()