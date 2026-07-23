import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_rag_v2.fusion import (  # noqa: E402
    Candidate,
    analyze_query,
    deterministic_rerank,
    filter_candidates_by_score,
    limit_candidates_per_file,
    reciprocal_rank_fusion,
)


def candidate(chunk_id, content, *, kind="prose", page=1):
    return Candidate(
        chunk_id=chunk_id,
        file_id=f"file-{chunk_id}",
        filename=f"{chunk_id}.md",
        title="Technical data",
        language="en",
        publication_date=None,
        certificates=[],
        product_families=[],
        media=[],
        component_categories=[],
        standards=[],
        content=content,
        page=page,
        section_path=["Technical data"],
        chunk_kind=kind,
        row_label="K 28" if kind == "table_row" else None,
    )


class V2FusionTests(unittest.TestCase):
    def test_query_analysis_preserves_identifiers_numbers_and_table_intent(self):
        analysis = analyze_query('Find model "K 28" maximum pressure 525 bar in the table')
        self.assertIn("525 bar", analysis.number_units)
        self.assertIn("k 28", analysis.quoted_phrases)
        self.assertTrue(analysis.table_intent)
        self.assertFalse(analysis.citation_intent)

    def test_query_analysis_preserves_single_letter_and_punctuated_model(self):
        analysis = analyze_query("Find Bauer model I 15.11-11-V")
        self.assertIn("i 15 11 11 v", analysis.identifiers)

    def test_query_analysis_detects_citation_location_intent(self):
        analysis = analyze_query(
            "Find model I 15.11-11-V and cite the exact source page."
        )
        self.assertTrue(analysis.citation_intent)

    def test_query_analysis_extracts_standard_without_broad_exact_tokens(self):
        analysis = analyze_query(
            "Find the English EN ISO 3834-2 certificate for Bauer Kompressoren"
        )
        self.assertIn("en iso 3834 2", analysis.standards)
        self.assertEqual(analysis.exact_terms, ("en iso 3834 2",))
        self.assertNotIn("certificate", analysis.exact_terms)

    def test_query_analysis_marks_document_number_lookup(self):
        analysis = analyze_query("Find document N47183")
        self.assertIn("n47183", analysis.identifiers)
        self.assertTrue(analysis.document_lookup)

    def test_non_exact_pressure_question_has_no_exact_terms(self):
        analysis = analyze_query("What is the highest documented maximum operating pressure?")
        self.assertEqual(analysis.exact_terms, ())
        self.assertFalse(analysis.table_intent)
        self.assertTrue(analysis.pressure_extremum)

    def test_first_person_words_are_not_model_identifiers(self):
        analysis = analyze_query("I do not know the Bauer product names")
        self.assertNotIn("i do", analysis.identifiers)

    def test_query_analysis_adds_controlled_german_metadata_aliases(self):
        analysis = analyze_query("Stickstoff-Nachverdichter mit 420 bar")
        self.assertIn("nitrogen", analysis.tokens)
        self.assertIn("booster", analysis.tokens)
        self.assertIn("420 bar", analysis.number_units)

    def test_rrf_rewards_multi_channel_candidates(self):
        shared = candidate("shared", "K 28 maximum pressure 525 bar", kind="table_row")
        exact_only = candidate("exact-only", "K 28")
        fused = reciprocal_rank_fusion(
            {
                "exact": [shared, exact_only],
                "lexical": [shared],
                "vector": [shared],
            }
        )
        self.assertEqual(fused[0].chunk_id, "shared")
        self.assertEqual(set(fused[0].channels), {"exact", "lexical", "vector"})

    def test_rrf_deduplicates_same_source_location_and_content(self):
        first = candidate("one", "same content")
        duplicate = candidate("two", "same content")
        duplicate.file_id = first.file_id
        fused = reciprocal_rank_fusion({"exact": [first], "lexical": [duplicate]})
        self.assertEqual(len(fused), 1)

    def test_deterministic_fallback_prioritizes_exact_table_row(self):
        analysis = analyze_query("What is the maximum pressure for K 28 in the technical table?")
        prose = candidate("prose", "The K range is designed for demanding work.")
        row = candidate("row", "Row K 28 values maximum pressure 525 bar", kind="table_row")
        prose.fusion_score = 0.04
        row.fusion_score = 0.04
        ranked = deterministic_rerank(analysis, [prose, row])
        self.assertEqual(ranked[0].chunk_id, "row")
        self.assertEqual(ranked[0].metadata["reranker"], "deterministic-multilingual-fallback-v2")

    def test_citation_intent_prefers_versioned_page_addressable_source(self):
        analysis = analyze_query(
            "Find model I 15.11-11-V and cite the exact technical-data table."
        )
        web = candidate(
            "web",
            "I 15.11-11-V 420 l/min 525 bar 4 stages 11 kW",
            kind="table_row",
            page=None,
        )
        web.publication_date = None
        brochure = candidate(
            "brochure",
            "I 15.11-11-V 420 l/min 525 bar 4 stages 11 kW",
            kind="table_row",
            page=23,
        )
        brochure.publication_date = "2026-04-01"
        web.fusion_score = 0.16
        brochure.fusion_score = 0.04
        ranked = deterministic_rerank(analysis, [web, brochure])
        self.assertEqual(ranked[0].chunk_id, "brochure")

    def test_evidence_cap_limits_each_file_and_fills_from_others(self):
        candidates = [candidate(name, name) for name in ("a-1", "a-2", "a-3", "b-1", "c-1")]
        for item in candidates[:3]:
            item.file_id = "file-a"
        selected = limit_candidates_per_file(candidates, top_n=4, max_per_file=2)
        self.assertEqual(
            [item.chunk_id for item in selected],
            ["a-1", "a-2", "b-1", "c-1"],
        )

    def test_evidence_cap_can_require_distinct_pages_within_file(self):
        candidates = [
            candidate("page-1-a", "a", page=1),
            candidate("page-1-b", "b", page=1),
            candidate("page-2", "c", page=2),
        ]
        for item in candidates:
            item.file_id = "file-a"
        selected = limit_candidates_per_file(
            candidates,
            top_n=3,
            max_per_file=3,
            max_per_page=1,
        )
        self.assertEqual(
            [item.chunk_id for item in selected],
            ["page-1-a", "page-2"],
        )

    def test_minimum_score_removes_weak_or_non_finite_evidence(self):
        strong = candidate("strong", "strong")
        weak = candidate("weak", "weak")
        invalid = candidate("invalid", "invalid")
        strong.final_score = 0.2
        weak.final_score = 0.079
        invalid.final_score = float("nan")
        selected = filter_candidates_by_score(
            [strong, weak, invalid],
            minimum_score=0.08,
        )
        self.assertEqual([item.chunk_id for item in selected], ["strong"])


if __name__ == "__main__":
    unittest.main()
