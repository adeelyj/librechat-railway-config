from __future__ import annotations

import unittest

from bauer_twin.catalog import build_catalog
from bauer_twin.engine import SearchEngine


class BauerTwinEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = SearchEngine()

    def test_catalog_shape(self) -> None:
        catalog = build_catalog()
        self.assertEqual(12, len(catalog["projects"]))
        self.assertEqual(75, len(catalog["parts"]))
        self.assertEqual(8, len(catalog["documents"]))
        self.assertTrue(all(item["project_id"].startswith("SYN-") for item in catalog["projects"]))
        self.assertTrue(all(item["part_id"].startswith("SYN-") for item in catalog["parts"]))

    def test_exact_project_id_bypasses_fuzzy_ranking(self) -> None:
        result = self.engine.search_similar_projects("SYN-BK-N2-365-400")
        self.assertEqual("SYN-BK-N2-365-400", result["results"][0]["project_id"])
        self.assertEqual(1.0, result["results"][0]["score"])

    def test_nitrogen_420_bar_hard_filters(self) -> None:
        result = self.engine.search_similar_projects("nitrogen booster 420 bar 500 l/min")
        self.assertEqual("SYN-BK-N2-420-500", result["results"][0]["project_id"])
        self.assertTrue(all(item["medium"] == "nitrogen" for item in result["results"]))
        self.assertTrue(all(float(item["pressure_bar"]) >= 420 for item in result["results"]))
        excluded = {item["project_id"] for item in result["hard_exclusions"]}
        self.assertIn("SYN-BK-N2-365-500", excluded)

    def test_german_and_english_return_same_project(self) -> None:
        english = self.engine.search_similar_projects("nitrogen booster 420 bar 500 l/min")
        german = self.engine.search_similar_projects("Stickstoff Booster 420 bar 500 l/min")
        self.assertEqual(english["results"][0]["project_id"], german["results"][0]["project_id"])
        self.assertEqual("matches_found", english["status"])
        self.assertEqual("nitrogen", german["interpretation"]["medium"]["normalized"])

    def test_helium_is_a_safe_no_match_in_english(self) -> None:
        result = self.engine.search_similar_projects("helium booster 420 bar 500 l/min")
        self.assertEqual("no_compatible_match", result["status"])
        self.assertEqual("helium", result["filters"]["medium"])
        self.assertEqual([], result["results"])
        self.assertTrue(result["hard_exclusions"])
        self.assertTrue(
            all(
                any("medium mismatch" in reason for reason in item["reasons"])
                for item in result["hard_exclusions"]
            )
        )

    def test_deterministic_no_match_does_not_call_embedding_service(self) -> None:
        class UnexpectedEmbeddingClient:
            def embed(self, _: str) -> list[float]:
                raise AssertionError("Embedding service should not be called for a deterministic no-match")

        engine = SearchEngine(embedding_client=UnexpectedEmbeddingClient())
        result = engine.search_similar_projects("helium booster 420 bar 500 l/min")
        self.assertEqual("no_compatible_match", result["status"])

    def test_helium_is_a_safe_no_match_in_german(self) -> None:
        result = self.engine.search_similar_projects("Heliumgas-Nachverdichter 420 bar 500 l/min")
        self.assertEqual("no_compatible_match", result["status"])
        self.assertEqual("helium", result["filters"]["medium"])
        self.assertEqual("booster", result["filters"]["topology"])
        self.assertEqual([], result["results"])

    def test_chemical_symbol_is_accepted_when_explicit(self) -> None:
        result = self.engine.search_similar_projects(
            "booster 420 bar 500 l/min",
            medium="He",
        )
        self.assertEqual("no_compatible_match", result["status"])
        self.assertEqual("helium", result["filters"]["medium"])
        self.assertEqual("he", result["interpretation"]["medium"]["matched_alias"])

    def test_unknown_medium_does_not_fall_back_to_a_known_medium(self) -> None:
        result = self.engine.search_similar_projects(
            "SpecialGas-X booster 420 bar 500 l/min",
            medium="SpecialGas-X",
        )
        self.assertEqual("unknown_constraint", result["status"])
        self.assertEqual([], result["results"])
        self.assertNotIn("medium", result["filters"])
        self.assertEqual("SpecialGas-X", result["unknown_constraints"][0]["raw"])

    def test_exact_identifier_cannot_bypass_a_hard_medium_constraint(self) -> None:
        result = self.engine.search_similar_projects(
            "SYN-BK-N2-420-500",
            medium="helium",
        )
        self.assertEqual("no_compatible_match", result["status"])
        self.assertEqual([], result["results"])
        self.assertEqual("SYN-BK-N2-420-500", result["hard_exclusions"][0]["project_id"])

    def test_exact_part_id(self) -> None:
        part_id = "SYN-P-SNS-PRESSURE-500"
        result = self.engine.search_parts(part_id)
        self.assertEqual(part_id, result["results"][0]["part_id"])
        self.assertEqual(1.0, result["results"][0]["score"])

    def test_part_pressure_exclusion(self) -> None:
        result = self.engine.search_parts("pressure valve nitrogen 420 bar")
        self.assertTrue(all(float(item["max_pressure_bar"]) >= 420 for item in result["results"]))

    def test_part_search_rejects_unavailable_helium_compatibility(self) -> None:
        result = self.engine.search_parts("helium pressure sensor 420 bar")
        self.assertEqual("no_compatible_match", result["status"])
        self.assertEqual([], result["results"])

    def test_compare_projects_is_field_deterministic(self) -> None:
        result = self.engine.compare_projects("SYN-BK-N2-420-500", "SYN-BK-N2-365-500")
        fields = {item["field"] for item in result["differences"]}
        self.assertIn("pressure_bar", fields)
        self.assertNotIn("capacity_l_min", fields)

    def test_project_details_link_parts_and_documents(self) -> None:
        result = self.engine.get_details("project", "SYN-BK-AIR-BM40-500")
        self.assertTrue(result["found"])
        self.assertGreaterEqual(len(result["parts"]), 6)
        self.assertTrue(all(item["source_url"].startswith("https://") for item in result["documents"]))

    def test_not_found_is_explicit(self) -> None:
        result = self.engine.get_details("part", "BAUER-UNKNOWN-999")
        self.assertFalse(result["found"])
        self.assertIn("No matching", result["message"])


if __name__ == "__main__":
    unittest.main()

