import unittest
from datetime import date

from bauer_rag_v2.fusion import analyze_query
from bauer_rag_v2.repository import (
    _lexical_tsquery,
    _maximum_pressure_bar,
    _publication_date,
)


class V2RepositoryTests(unittest.TestCase):
    def test_publication_date_is_native_for_asyncpg(self):
        self.assertEqual(_publication_date("2026-04-01"), date(2026, 4, 1))
        self.assertIsNone(_publication_date(None))

    def test_invalid_publication_date_fails_before_database_insert(self):
        with self.assertRaises(ValueError):
            _publication_date("2026-99-01")

    def test_lexical_query_uses_bounded_or_terms(self):
        query = _lexical_tsquery(
            analyze_query("Find maximum operating pressure and compressor product family")
        )
        self.assertIn("maximum", query)
        self.assertIn("pressure", query)
        self.assertIn(" | ", query)
        self.assertNotIn("find", query)

    def test_maximum_pressure_prefers_upper_range_bound(self):
        content = (
            "Max. operating pressure 350 - 420 bar. "
            "VERTICUS SERIES, 310 - 510 l/min, 420 - 525 bar"
        )
        self.assertEqual(_maximum_pressure_bar(content), 525.0)


if __name__ == "__main__":
    unittest.main()
