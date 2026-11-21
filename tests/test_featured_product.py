import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import settings as settings_module
from app.services.settings import SettingsService
import app.keyboards as kb


class FeaturedProductTests(unittest.TestCase):
    def _isolate(self, temp_dir: str):
        settings_file = os.path.join(temp_dir, "settings.json")
        return (
            patch.object(settings_module, "SETTINGS_FILE", settings_file),
            patch.object(settings_module.PostgresDocumentStore, "is_enabled", return_value=False),
        )

    def test_price_button_hidden_until_items_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                self.assertFalse(SettingsService.is_price_menu_visible())
                texts = [btn.text for row in kb.get_main_menu_keyboard().inline_keyboard for btn in row]
                callbacks = [
                    btn.callback_data for row in kb.get_main_menu_keyboard().inline_keyboard for btn in row
                ]
                self.assertIn(kb.BTN_BUY, texts)
                self.assertNotIn(kb.BTN_PRICE, texts)
                self.assertNotIn("menu_price", callbacks)

    def test_price_menu_lists_items_and_card_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                first = SettingsService.add_featured_item("Шишки", "Свежий завоз, длинное описание.")
                SettingsService.add_featured_item("Гашиш")
                self.assertTrue(SettingsService.is_price_menu_visible())
                texts = [btn.text for row in kb.get_main_menu_keyboard().inline_keyboard for btn in row]
                callbacks = [
                    btn.callback_data for row in kb.get_main_menu_keyboard().inline_keyboard for btn in row
                ]
                self.assertIn(kb.BTN_PRICE, texts)
                self.assertIn("menu_price", callbacks)

                price_kb = kb.get_price_list_keyboard(SettingsService.list_featured_products())
                price_texts = [btn.text for row in price_kb.inline_keyboard for btn in row]
                self.assertIn("Шишки", price_texts)
                self.assertIn("Гашиш", price_texts)
                self.assertIn(kb.BTN_DISMISS, price_texts)

                text = SettingsService.featured_item_text(first)
                self.assertIn("Шишки", text)
                self.assertIn("Свежий завоз", text)

    def test_legacy_single_product_migrates_into_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                settings = SettingsService._load_settings()
                settings["featured_product"] = {
                    "title": "Старый товар",
                    "description": "Было одно описание",
                    "photo_file_id": None,
                    "enabled": True,
                }
                SettingsService._save_settings(settings)
                catalog = SettingsService.get_featured_catalog()
                self.assertEqual(len(catalog["items"]), 1)
                self.assertEqual(catalog["items"][0]["title"], "Старый товар")
                self.assertTrue(SettingsService.is_price_menu_visible(catalog))

    def test_disable_hides_price_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._isolate(temp_dir)
            with patches[0], patches[1]:
                SettingsService.add_featured_item("Товар")
                SettingsService.save_featured_catalog(enabled=False)
                callbacks = [
                    btn.callback_data for row in kb.get_main_menu_keyboard().inline_keyboard for btn in row
                ]
                self.assertNotIn("menu_price", callbacks)


class HomeScreenTests(unittest.TestCase):
    def test_links_render_under_shop_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_file = os.path.join(temp_dir, "settings.json")
            with patch.object(settings_module, "SETTINGS_FILE", settings_file), patch.object(
                settings_module.PostgresDocumentStore, "is_enabled", return_value=False
            ):
                SettingsService.set_home_screen_text("Жила открыта")
                ok, _note = SettingsService.add_home_link("Оператор", "@valley_op")
                self.assertTrue(ok)
                ok, _note = SettingsService.add_home_link("Сайт", "https://valley.example")
                self.assertTrue(ok)
                self.assertFalse(SettingsService.add_home_link("Пусто", "не ссылка")[0])
                links = SettingsService.get_home_links()
                self.assertEqual(links[0]["url"], "https://t.me/valley_op")
                menu = kb.get_main_menu_keyboard()
                first = menu.inline_keyboard[0][0]
                self.assertEqual(first.callback_data, "menu_buy")
                self.assertEqual(menu.inline_keyboard[1][0].text, "Оператор")
                self.assertEqual(menu.inline_keyboard[1][0].url, "https://t.me/valley_op")
                self.assertEqual(menu.inline_keyboard[2][0].url, "https://valley.example")
                self.assertTrue(SettingsService.update_home_link(links[0]["id"], title="Смотритель")[0])
                self.assertTrue(SettingsService.delete_home_link(links[1]["id"]))
                left = SettingsService.get_home_links()
                self.assertEqual([item["title"] for item in left], ["Смотритель"])
                self.assertIn("Жила открыта", SettingsService.get_home_screen_text())


if __name__ == "__main__":
    unittest.main()
