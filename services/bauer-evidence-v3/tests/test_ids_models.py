from __future__ import annotations

import unittest

from bauer_evidence_v3.ids import canonical_json, content_object_key, sha256_json, stable_id
from bauer_evidence_v3.models import BoundingBox, EvidenceItem, SourceCoordinate, TypedFact


class IdentifierAndModelTests(unittest.TestCase):
    def test_canonical_json_and_stable_ids_ignore_mapping_order(self) -> None:
        left = {"b": 2, "a": {"z": 1, "x": 0}}
        right = {"a": {"x": 0, "z": 1}, "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(sha256_json(left), sha256_json(right))
        self.assertEqual(stable_id("block", "source", 1, 2), stable_id("block", "source", 1, 2))

    def test_content_key_is_partitioned_and_rejects_traversal_suffix(self) -> None:
        digest = "ab" * 32
        self.assertEqual(
            content_object_key(digest, suffix=".json"),
            f"sha256/ab/ab/{digest}.json",
        )
        with self.assertRaises(ValueError):
            content_object_key(digest, suffix="../../x")

    def test_source_coordinate_enforces_physical_and_structured_location(self) -> None:
        with self.assertRaises(ValueError):
            SourceCoordinate(page_number=0)
        with self.assertRaises(ValueError):
            SourceCoordinate(page_number=1, row_index=0)
        with self.assertRaises(ValueError):
            SourceCoordinate(page_number=1, character_start=0)
        coordinate = SourceCoordinate(
            page_number=2,
            printed_page_label="1",
            bounding_box=BoundingBox(1, 2, 3, 4),
            table_id="table_1",
            row_index=0,
        )
        self.assertEqual(coordinate.page_number, 2)

    def test_generated_summary_cannot_be_citable_evidence(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceItem(
                evidence_id="ev_1",
                release_id="release_1",
                tenant_id="tenant_1",
                knowledge_base_id="kb_1",
                source_document_id="source_1",
                source_version_id="version_1",
                source_sha256="ab" * 32,
                source_type="navigation_summary",
                title="Compressors",
                content="Generated category summary",
                coordinate=SourceCoordinate(page_number=1),
                generated_summary=True,
                is_citable=True,
            )

    def test_typed_fact_requires_one_matching_value_and_provenance(self) -> None:
        fact = TypedFact(
            fact_id="fact_1",
            artifact_set_id="artifact_1",
            subject="Model A",
            predicate="maximum_pressure",
            value_kind="numeric",
            raw_value="350 bar",
            numeric_value="350",
            raw_unit="bar",
            normalized_unit="bar",
            provenance_evidence_ids=("evidence_1",),
        )
        self.assertEqual(fact.numeric_value, "350")
        with self.assertRaises(ValueError):
            TypedFact(
                fact_id="fact_2",
                artifact_set_id="artifact_1",
                subject="Model A",
                predicate="maximum_pressure",
                value_kind="numeric",
                raw_value="350 bar",
                numeric_value="350",
                text_value="350",
                provenance_evidence_ids=("evidence_1",),
            )


if __name__ == "__main__":
    unittest.main()
