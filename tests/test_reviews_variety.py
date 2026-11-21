import unittest
import os
import tempfile
import asyncio
from unittest.mock import patch

from app.services.reviews import ReviewsService


def _collect_samples(count: int = 200) -> list[str]:
    return [
        ReviewsService._generate_local_review_text(
            "Мёд 0.5г",
            "Кемерово",
            "Октябрьский",
            None,
        )
        for _ in range(count)
    ]


class LocalReviewVariationTests(unittest.TestCase):
    def test_unique_ratio_above_threshold(self) -> None:
        samples = _collect_samples()
        unique_ratio = len(set(samples)) / len(samples)
        self.assertGreater(unique_ratio, 0.55)

    def test_texts_are_complete_phrases(self) -> None:
        samples = _collect_samples()
        truncated = [text for text in samples if ReviewsService._looks_truncated(text)]
        self.assertEqual(truncated, [])
        short_ratio = sum(len(text.split()) <= 2 for text in samples) / len(samples)
        self.assertLess(short_ratio, 0.25)

    def test_average_word_count_stays_natural(self) -> None:
        samples = _collect_samples()
        avg_words = sum(len(text.split()) for text in samples) / len(samples)
        self.assertGreaterEqual(avg_words, 4)
        self.assertLessEqual(avg_words, 16)

    def test_no_placeholder_fragments(self) -> None:
        samples = _collect_samples()
        self.assertTrue(all("заглушка" not in text.lower() for text in samples))

    def test_item_info_keeps_district(self) -> None:
        info = ReviewsService._build_item_info("Мёд 0.5г", "Кемерово", "Октябрьский")
        self.assertEqual(info, "Мёд 0.5г | Кемерово, Октябрьский")

    def test_location_phrase_uses_prepositional_case(self) -> None:
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("Автовокзал"),
            "на автовокзале",
        )
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("улица Слоновая"),
            "на улице Слоновой",
        )
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("ул. Слоновая"),
            "на улице Слоновой",
        )
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("ул. Ленина"),
            "на улице Ленина",
        )
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("Кировский район"),
            "в Кировском районе",
        )
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("мкр. Северный"),
            "в микрорайоне Северном",
        )
        self.assertEqual(
            ReviewsService._location_prepositional_phrase("ост. Школа"),
            "на остановке Школа",
        )

    def test_local_review_does_not_use_nominative_place(self) -> None:
        samples = [
            ReviewsService._generate_local_review_text("Мёд 0.5г", "Томск", "Автовокзал")
            for _ in range(80)
        ]
        joined = "\n".join(samples).lower()
        self.assertNotIn("в автовокзал", joined)
        self.assertTrue(any("на автовокзале" in text.lower() for text in samples))

    def test_filter_invalid_reviews_cleans(self) -> None:
        items = [
            {"text": "Ai-заглушка от GPT"},
            {"text": " "},
            {"text": None},
            {"text": "Живой отзыв"},
        ]
        cleaned, removed = ReviewsService._filter_invalid_reviews(items, "test")
        self.assertEqual(removed, 3)
        self.assertEqual(cleaned, [{"text": "Живой отзыв"}])


class LastBatchImportTests(unittest.TestCase):
    def test_apply_last_batch_texts_updates_text_and_district(self) -> None:
        original_file_path = ReviewsService.FILE_PATH
        original_last_batch_path = ReviewsService.LAST_BATCH_FILE

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                ReviewsService.FILE_PATH = os.path.join(temp_dir, "reviews.json")
                ReviewsService.LAST_BATCH_FILE = os.path.join(temp_dir, "reviews_last_batch.json")

                with patch("app.services.reviews.PostgresDocumentStore.is_enabled", return_value=False):
                    review = {
                        "id": "rev-1",
                        "iso_date": "2026-04-05T12:00:00",
                        "text": "Старый текст",
                        "city": "Солнечногорск",
                        "item_info": "Мёд 1г | Солнечногорск, Старый район",
                        "hidden": False,
                    }

                    ReviewsService._save_reviews([review])
                    ReviewsService._save_last_batch([review["iso_date"]])

                    result = ReviewsService.apply_last_batch_texts({
                        "reviews": [
                            {
                                "id": "rev-1",
                                "text": "Новый текст",
                                "district": "Новый район",
                                "item_info": "Мёд 1г | Солнечногорск, Старый район",
                            }
                        ]
                    })

                    self.assertEqual(result["updated"], 1)
                    self.assertEqual(result["invalid"], 0)

                    saved = ReviewsService._load_reviews()
                    self.assertEqual(saved[0]["text"], "Новый текст")
                    self.assertEqual(saved[0]["item_info"], "Мёд 1г | Солнечногорск, Новый район")
            finally:
                ReviewsService.FILE_PATH = original_file_path
                ReviewsService.LAST_BATCH_FILE = original_last_batch_path


class ReviewReadPathTests(unittest.TestCase):
    def test_empty_reviews_do_not_trigger_seed_on_read(self) -> None:
        with patch.object(ReviewsService, "_load_reviews", return_value=[]), patch.object(
            ReviewsService,
            "_seed_initial_reviews",
            side_effect=AssertionError("seed should not run on read"),
        ):
            self.assertEqual(ReviewsService.check_and_update(), [])
            text = asyncio.run(ReviewsService.get_reviews_text())

        self.assertIn("Отзывы", text)


class ReviewGenerationSpeedTests(unittest.TestCase):
    def test_generate_new_review_task_loads_catalog_once(self) -> None:
        products_cache = {
            "томск": {"ids": ["p1"]},
            "кемерово": {"ids": ["p1"]},
            "новосибирск": {"ids": ["p1"]},
        }
        districts_cache = {
            "томск": ["Центр"],
            "кемерово": ["Центр"],
            "новосибирск": ["Центр"],
        }
        products = [{"id": "p1", "name": "Мёд 0.5г", "base_price": 1000}]
        load_count = {"n": 0}
        ai_count = {"n": 0}

        def fake_get_all():
            load_count["n"] += 1
            return products

        async def fake_ai(*args, **kwargs):
            ai_count["n"] += 1
            return "Этот текст не должен появиться из ChatGPT."

        with patch.object(ReviewsService, "_load_reviews", return_value=[]), patch.object(
            ReviewsService, "_save_reviews"
        ), patch.object(ReviewsService, "_save_last_batch"), patch(
            "app.services.reviews.CatalogService._load_cache",
            return_value=products_cache,
        ), patch(
            "app.services.reviews.GeoService._load_cache",
            return_value=districts_cache,
        ), patch(
            "app.services.reviews.CatalogService.get_all_products",
            side_effect=fake_get_all,
        ), patch.object(
            ReviewsService, "_generate_ai_text", side_effect=fake_ai
        ):
            count = asyncio.run(ReviewsService.generate_new_review_task(trigger="test"))

        self.assertEqual(count, 3)
        self.assertEqual(load_count["n"], 1)
        self.assertEqual(ai_count["n"], 0)


if __name__ == "__main__":
    unittest.main()
