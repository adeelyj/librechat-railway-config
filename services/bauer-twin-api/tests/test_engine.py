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

    def test_exact_part_id(self) -> None:
        part_id = "SYN-P-SNS-PRESSURE-500"
        result = self.engine.search_parts(part_id)
        self.assertEqual(part_id, result["results"][0]["part_id"])
        self.assertEqual(1.0, result["results"][0]["score"])

    def test_part_pressure_exclusion(self) -> None:
        result = self.engine.search_parts("pressure valve nitrogen 420 bar")
        self.assertTrue(all(float(item["max_pressure_bar"]) >= 420 for item in result["results"]))

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

