import unittest

from app.services.geo import GeoService


class DistrictListParseTests(unittest.TestCase):
    def test_parse_newlines_commas_and_semicolons(self) -> None:
        self.assertEqual(
            GeoService.parse_district_list("Центр, Заречье\nЮжный; Северный"),
            ["Центр", "Заречье", "Южный", "Северный"],
        )

    def test_parse_skips_empty_and_deduplicates_casefold(self) -> None:
        self.assertEqual(
            GeoService.parse_district_list("  Центр ,\nцентр;;  Южный  "),
            ["Центр", "Южный"],
        )

    def test_parse_empty_returns_empty_list(self) -> None:
        self.assertEqual(GeoService.parse_district_list("  \n , ; "), [])
        self.assertEqual(GeoService.parse_district_list(""), [])
        self.assertEqual(GeoService.parse_district_list(None), [])


if __name__ == "__main__":
    unittest.main()
