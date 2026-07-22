from __future__ import annotations

import unittest

from bauer_twin.terminology import TerminologyResolver, normalize_text, terminology_rows


class TerminologyResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver = TerminologyResolver(terminology_rows())

    def test_unicode_and_separators_are_normalized(self) -> None:
        self.assertEqual("n2 booster", normalize_text("N₂-Booster"))

    def test_german_and_english_media_resolve_identically(self) -> None:
        english = self.resolver.resolve("medium", "nitrogen")
        german = self.resolver.resolve("medium", "Stickstoff")
        symbol = self.resolver.resolve("medium", "N₂")
        self.assertEqual("nitrogen", english.normalized)
        self.assertEqual(english.normalized, german.normalized)
        self.assertEqual(english.normalized, symbol.normalized)

    def test_short_ambiguous_symbol_is_not_inferred_from_free_text(self) -> None:
        self.assertIsNone(self.resolver.infer("medium", "He said the booster needs 420 bar"))
        self.assertEqual("helium", self.resolver.resolve("medium", "He").normalized)

    def test_longest_alias_wins(self) -> None:
        result = self.resolver.infer("medium", "stationary breathing air compressor")
        self.assertIsNotNone(result)
        self.assertEqual("breathing air", result.normalized)

    def test_unknown_value_is_preserved_without_conversion(self) -> None:
        result = self.resolver.resolve("medium", "SpecialGas-X")
        self.assertEqual("unknown", result.status)
        self.assertEqual("SpecialGas-X", result.raw)
        self.assertIsNone(result.normalized)


if __name__ == "__main__":
    unittest.main()
