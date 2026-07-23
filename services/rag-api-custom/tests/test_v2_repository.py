import unittest
from datetime import date

from bauer_rag_v2.fusion import Candidate, analyze_query
from bauer_rag_v2.repository import (
    _is_pressure_evidence_candidate,
    _lexical_tsquery,
    _maximum_pressure_bar,
    _publication_date,
    _select_pressure_evidence,
)


def pressure_candidate(
    chunk_id,
    content,
    maximum,
    *,
    page,
    publication_date="2026-04-01",
):
    return Candidate(
        chunk_id=chunk_id,
        file_id=f"file-{chunk_id}",
        filename=f"{chunk_id}.md",
        title="Pressure evidence",
        language="en",
        publication_date=publication_date,
        certificates=[],
        product_families=[],
        media=[],
        component_categories=["compressor"],
        standards=[],
        content=content,
        page=page,
        section_path=[],
        chunk_kind="prose",
        metadata={"pressure_extremum_bar": maximum},
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

    def test_pressure_evidence_reserves_primary_booster_and_qualification_roles(self):
        primary = [
            pressure_candidate(f"primary-{index}", "Compressor 525 bar", 525, page=index)
            for index in range(1, 10)
        ]
        booster = pressure_candidate(
            "booster",
            "Water-cooled booster pressure 520 bar",
            520,
            page=7,
        )
        qualification = pressure_candidate(
            "qualification",
            "525 bar; max setting safety valve; final pressure lower",
            525,
            page=23,
        )
        selected = _select_pressure_evidence(
            [*primary, booster, qualification],
            limit=12,
        )
        ids = {candidate.chunk_id for candidate in selected}
        self.assertIn("booster", ids)
        self.assertIn("qualification", ids)
        self.assertEqual(
            [candidate.metadata["pressure_evidence_priority"] for candidate in selected],
            list(range(len(selected))),
        )

    def test_pressure_evidence_rejects_non_compressor_accessory_limit(self):
        dryer = pressure_candidate(
            "dryer",
            "B-KOOL III maximum operating pressure 550 bar",
            550,
            page=12,
        )
        dryer.row_label = "B-KOOL III"
        dryer.chunk_kind = "table_row"
        self.assertFalse(_is_pressure_evidence_candidate(dryer))
        fitting = pressure_candidate(
            "fitting",
            "Cutting ring screwed fitting pressure range to 630 bar",
            630,
            page=61,
        )
        self.assertFalse(_is_pressure_evidence_candidate(fitting))


if __name__ == "__main__":
    unittest.main()
