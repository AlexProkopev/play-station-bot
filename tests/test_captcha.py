import random
import unittest

from app.services.captcha import (
    LETTER_ALPHABET,
    answers_match,
    create_challenge,
    normalize_answer,
)


class CaptchaServiceTests(unittest.TestCase):
    def test_letter_challenge_uses_safe_alphabet(self) -> None:
        challenge = create_challenge(random.Random(1), kind="letters")
        self.assertEqual(challenge.kind, "letters")
        self.assertEqual(len(challenge.answer), 5)
        self.assertTrue(all(char in LETTER_ALPHABET for char in challenge.answer))
        self.assertTrue(challenge.image_png.startswith(b"\x89PNG"))
        self.assertIn("символы с картинки", challenge.caption)

    def test_math_challenge_answer_is_numeric(self) -> None:
        challenge = create_challenge(random.Random(3), kind="math")
        self.assertEqual(challenge.kind, "math")
        self.assertTrue(challenge.answer.isdigit())
        self.assertGreaterEqual(int(challenge.answer), 1)
        self.assertTrue(challenge.image_png.startswith(b"\x89PNG"))
        self.assertIn("пример с картинки", challenge.caption)

    def test_answers_match_ignores_case_and_spaces(self) -> None:
        self.assertTrue(answers_match(" ab 12 ", "AB12"))
        self.assertTrue(answers_match("14", "14"))
        self.assertFalse(answers_match("AB12", "AB13"))
        self.assertEqual(normalize_answer("a-b 3"), "AB3")

    def test_both_kinds_appear(self) -> None:
        kinds = {create_challenge(random.Random(seed)).kind for seed in range(40)}
        self.assertEqual(kinds, {"letters", "math"})


if __name__ == "__main__":
    unittest.main()
