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
        self.assertEqual(ranked[0].metadata["reranker"], "deterministic-multilingual-fallback-v1")

    def test_evidence_cap_limits_each_file_and_fills_from_others(self):
        candidates = [candidate(name, name) for name in ("a-1", "a-2", "a-3", "b-1", "c-1")]
        for item in candidates[:3]:
            item.file_id = "file-a"
        selected = limit_candidates_per_file(candidates, top_n=4, max_per_file=2)
        self.assertEqual(
            [item.chunk_id for item in selected],
            ["a-1", "a-2", "b-1", "c-1"],
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
