import unittest
from unittest.mock import patch

from app.handlers.admin import resolve_user_input
import app.keyboards as kb


class ResolveUserInputTests(unittest.TestCase):
    def test_numeric_id(self) -> None:
        self.assertEqual(resolve_user_input("123456789"), 123456789)
        self.assertEqual(resolve_user_input("  123456789  "), 123456789)
        self.assertEqual(resolve_user_input("/user 123456789"), 123456789)

    def test_id_with_prefix(self) -> None:
        self.assertEqual(resolve_user_input("id:123456789"), 123456789)
        self.assertEqual(resolve_user_input("id=123456789"), 123456789)

    def test_username_not_treated_as_embedded_digits(self) -> None:
        with patch("app.handlers.admin.UserService.get_user_id_by_username", return_value=None):
            self.assertIsNone(resolve_user_input("nick12345"))

    def test_username_lookup(self) -> None:
        with patch("app.handlers.admin.UserService.get_user_id_by_username", return_value=42):
            self.assertEqual(resolve_user_input("@Nick"), 42)
            self.assertEqual(resolve_user_input("Nick"), 42)


class MainMenuKeyboardTests(unittest.TestCase):
    def test_buy_on_screen_cancel_on_panel(self) -> None:
        menu = kb.get_main_menu_keyboard()
        texts = [btn.text for row in menu.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for row in menu.inline_keyboard for btn in row]
        self.assertEqual(texts[0], kb.BTN_BUY)
        self.assertEqual(callbacks[0], "menu_buy")
        panel = kb.get_base_keyboard()
        panel_texts = [btn.text for row in panel.keyboard for btn in row]
        self.assertIn(kb.BTN_BUY, panel_texts)
        self.assertTrue(any(text.startswith(kb.BTN_BALANCE) for text in panel_texts))
        self.assertTrue(any(text.startswith(kb.BTN_PURCHASES) for text in panel_texts))
        self.assertIn(kb.BTN_CANCEL, panel_texts)
