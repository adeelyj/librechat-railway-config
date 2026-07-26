from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal

from bauer_evidence_v3.answering import AnswerService
from bauer_evidence_v3.auth import AuthorizationContext
from bauer_evidence_v3.generation import SequenceModelGateway
from bauer_evidence_v3.models import ArtifactStatus, EvidenceItem, SourceCoordinate
from bauer_evidence_v3.planner import (
    NumericComparator,
    RetrievalChannel,
    analyze_query,
)
from bauer_evidence_v3.releases import (
    InMemoryReleaseRegistry,
    ReleaseGateReport,
    ReleaseSource,
)
from bauer_evidence_v3.retrieval import (
    InMemoryEvidenceIndex,
    NavigationNode,
    SearchUnit,
)


class RetrievalAndAnsweringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release_id = "release-v3"
        self.releases = InMemoryReleaseRegistry()
        self.releases.create(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label="golden",
            expected_source_count=2,
            compiler_fingerprint="compiler:test",
            versions={
                "compiler": "test",
                "parser": "test",
                "embedding": "hash-test",
                "prompt": "test",
            },
            release_id=self.release_id,
        )
        self.releases.start_build(self.release_id)
        for source in ("source-a", "source-b"):
            self.releases.record_source(
                self.release_id,
                ReleaseSource(
                    source_id=source,
                    source_version_id=f"{source}-v1",
                    artifact_set_id=f"{source}-artifact",
                    artifact_status=ArtifactStatus.VALID,
                ),
            )
        self.releases.begin_validation(self.release_id)
        self.releases.mark_ready(
            self.release_id,
            ReleaseGateReport(
                manifest_sha256=self.releases.computed_manifest_sha256(self.release_id),
                source_complete=True,
                citations_resolvable=True,
                projections_complete=True,
            ),
        )
        self.releases.activate(
            self.release_id,
            actor="test",
            reason="golden vertical slice",
        )
        self.index = InMemoryEvidenceIndex()
        self.table = self._evidence(
            evidence_id="table-a",
            source="source-a",
            page=23,
            content="I 15.11-11-V has a maximum pressure of 525 bar.",
            table_headers=("Model", "Maximum pressure"),
            table_values=("I 15.11-11-V", "525 bar"),
            table_id="table-1",
            row_index=4,
        )
        self.prose = self._evidence(
            evidence_id="prose-a",
            source="source-a",
            page=3,
            content="The product range includes I 15.11-11-V compressor systems.",
        )
        self.unauthorized = self._evidence(
            evidence_id="secret-b",
            source="source-b",
            page=1,
            content="I 15.11-11-V has a maximum pressure of 999 bar.",
            table_headers=("Model", "Maximum pressure"),
            table_values=("I 15.11-11-V", "999 bar"),
            table_id="table-secret",
            row_index=0,
        )
        self.index.add(
            SearchUnit(
                search_unit_id="unit-table",
                evidence=self.table,
                unit_type="table_row",
                search_text=self.table.content,
                exact_terms=("I 15.11-11-V",),
                subject="I 15.11-11-V",
                predicate="maximum_pressure",
                numeric_value=Decimal("525"),
                unit="bar",
            )
        )
        self.index.add(
            SearchUnit(
                search_unit_id="unit-fact",
                evidence=self.table,
                unit_type="fact",
                search_text=self.table.content,
                exact_terms=("I 15.11-11-V",),
                subject="I 15.11-11-V",
                predicate="maximum_pressure",
                numeric_value=Decimal("525"),
                unit="bar",
            )
        )
        self.index.add(
            SearchUnit(
                search_unit_id="unit-prose",
                evidence=self.prose,
                unit_type="block",
                search_text=self.prose.content,
                exact_terms=("I 15.11-11-V",),
            )
        )
        self.index.add(
            SearchUnit(
                search_unit_id="unit-secret",
                evidence=self.unauthorized,
                unit_type="table_row",
                search_text=self.unauthorized.content,
                exact_terms=("I 15.11-11-V",),
                subject="I 15.11-11-V",
                predicate="maximum_pressure",
                numeric_value=Decimal("999"),
                unit="bar",
            )
        )
        self.index.add_navigation(
            NavigationNode(
                node_id="high-pressure",
                release_id=self.release_id,
                label="High-pressure compressor family",
                description="Overview of compressor models and their technical data",
                aliases=("product range",),
                linked_evidence_ids=("table-a",),
            )
        )
        self.authorization = AuthorizationContext(
            tenant_id="tenant",
            knowledge_base_id="kb",
            user_id="user",
            agent_id="agent",
            audience="bauer-evidence-v3",
            issued_at=1,
            expires_at=2,
            authorized_source_ids=("source-a",),
        )

    def _evidence(
        self,
        *,
        evidence_id: str,
        source: str,
        page: int,
        content: str,
        table_headers: tuple[str, ...] = (),
        table_values: tuple[str, ...] = (),
        table_id: str | None = None,
        row_index: int | None = None,
    ) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=evidence_id,
            release_id=self.release_id,
            tenant_id="tenant",
            knowledge_base_id="kb",
            source_document_id=source,
            source_version_id=f"{source}-v1",
            source_sha256="ab" * 32,
            source_type="public_document",
            title="Industry brochure",
            content=content,
            coordinate=SourceCoordinate(
                page_number=page,
                table_id=table_id,
                row_index=row_index,
            ),
            table_headers=table_headers,
            table_values=table_values,
        )

    def test_planner_routes_identifier_table_and_fact_query(self) -> None:
        plan = analyze_query(
            "What is the maximum pressure in the technical table for I 15.11-11-V?"
        )
        self.assertIn("I 15.11-11-V", plan.identifiers)
        self.assertIn(RetrievalChannel.EXACT, plan.channels)
        self.assertIn(RetrievalChannel.FACT, plan.channels)
        self.assertIn(RetrievalChannel.TABLE, plan.channels)
        self.assertEqual(plan.superlative, "maximum")

    def test_planner_does_not_extend_prefixed_identifier_into_prose(
        self,
    ) -> None:
        plan = analyze_query(
            "Find the Bauer model I 15.11-11-V in the uploaded documents. "
            "Give the free air delivery, maximum operating pressure, number "
            "of stages, and motor output."
        )

        self.assertEqual(plan.identifiers, ("I 15.11-11-V",))
        self.assertNotIn(
            "I 15.11-11-V IN THE UPLOADED DOCUMENTS",
            plan.identifiers,
        )
        self.assertNotIn("GIVE THE FREE AIR DELIVERY", plan.identifiers)
        self.assertNotIn("NUMBER OF STAGES", plan.identifiers)

    def test_planner_recognizes_bounded_catalog_product_identifiers(
        self,
    ) -> None:
        plan = analyze_query(
            "Compare B-KOOL with B-SELECT and reconcile B-SAFE data."
        )

        self.assertEqual(
            plan.identifiers,
            ("B-KOOL", "B-SELECT", "B-SAFE"),
        )
        self.assertEqual(plan.channels[0], RetrievalChannel.EXACT)

    def test_planner_preserves_numeric_comparators_ranges_and_default_equality(
        self,
    ) -> None:
        cases = {
            "pressure > 300 bar": NumericComparator.GREATER_THAN,
            "pressure at least 300 bar": (
                NumericComparator.GREATER_THAN_OR_EQUAL
            ),
            "pressure below 300 bar": NumericComparator.LESS_THAN,
            "pressure <= 300 bar": NumericComparator.LESS_THAN_OR_EQUAL,
            "pressure 300 bar": NumericComparator.EQUAL,
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                plan = analyze_query(query)
                self.assertEqual(len(plan.numeric_constraints), 1)
                constraint = plan.numeric_constraints[0]
                self.assertEqual(constraint.comparator, expected)
                self.assertEqual(constraint.lower_value, Decimal("300"))
                self.assertEqual(constraint.unit, "bar")
                self.assertIn(RetrievalChannel.FACT, plan.channels)
                self.assertIn(RetrievalChannel.TABLE, plan.channels)

        between = analyze_query("pressure between 300 and 500 bar")
        self.assertEqual(
            between.numeric_constraints[0].comparator,
            NumericComparator.BETWEEN,
        )
        self.assertEqual(
            between.numeric_constraints[0].upper_value,
            Decimal("500"),
        )
        self.assertEqual(between.numeric_constraints[0].unit, "bar")

    def test_planner_types_request_constraints_and_rejects_ambiguous_units(
        self,
    ) -> None:
        plan = analyze_query(
            "Find a suitable compressor",
            mandatory_constraints={
                "medium": ("nitrogen", "air"),
                "pressure": "> 300 bar",
            },
            forbidden_claim_values=("oxygen", "<= 100 bar"),
        )
        self.assertEqual(
            plan.mandatory_text_constraints,
            {"medium": ("nitrogen", "air")},
        )
        self.assertEqual(
            plan.mandatory_numeric_constraints["pressure"][0].comparator,
            NumericComparator.GREATER_THAN,
        )
        self.assertEqual(
            plan.forbidden_numeric_constraints[0].comparator,
            NumericComparator.LESS_THAN_OR_EQUAL,
        )
        self.assertIsNone(plan.constraint_failure)

        ambiguous = analyze_query(
            "pressure between 300 bar and 500 psi"
        )
        self.assertIsNotNone(ambiguous.constraint_failure)
        self.assertEqual(ambiguous.numeric_constraints, ())

    def test_structured_evidence_outranks_prose_and_authorization_is_pre_fusion(self) -> None:
        plan = analyze_query(
            "What is the maximum pressure in the technical table for I 15.11-11-V?"
        )
        results = self.index.retrieve(
            plan,
            release_id=self.release_id,
            authorized_source_ids=frozenset({"source-a"}),
        )
        self.assertEqual(results[0].evidence.evidence_id, "table-a")
        self.assertNotIn("secret-b", {result.evidence.evidence_id for result in results})
        self.assertIn("fact", results[0].channels)

    def test_empty_authorization_scope_fails_closed(self) -> None:
        plan = analyze_query("I 15.11-11-V")
        self.assertEqual(
            self.index.retrieve(
                plan,
                release_id=self.release_id,
                authorized_source_ids=frozenset(),
            ),
            (),
        )

    def test_stable_citation_survives_result_order(self) -> None:
        from bauer_evidence_v3.evidence import build_evidence_package

        plan = analyze_query("I 15.11-11-V")
        results = self.index.retrieve(
            plan,
            release_id=self.release_id,
            authorized_source_ids=frozenset({"source-a"}),
        )
        first = build_evidence_package(
            question=plan.query,
            release_id=self.release_id,
            tenant_id="tenant",
            knowledge_base_id="kb",
            results=results,
            authorized_source_ids=frozenset({"source-a"}),
        )
        second = build_evidence_package(
            question=plan.query,
            release_id=self.release_id,
            tenant_id="tenant",
            knowledge_base_id="kb",
            results=tuple(reversed(results)),
            authorized_source_ids=frozenset({"source-a"}),
        )
        first_map = {item.evidence_id: item.citation_id for item in first.citations}
        second_map = {item.evidence_id: item.citation_id for item in second.citations}
        self.assertEqual(first_map, second_map)

    def test_invalid_first_answer_is_repaired_once(self) -> None:
        citation = "E-72DCE2A16835"
        # Derive rather than assume a rank-based citation.
        from bauer_evidence_v3.ids import sha256_json

        citation = f"E-{sha256_json('table-a')[:12].upper()}"
        model = SequenceModelGateway(
            [
                "The maximum pressure is 999 bar.",
                f"For nitrogen, the maximum pressure is 525 bar [{citation}].",
            ]
        )
        service = AnswerService(releases=self.releases, index=self.index, model=model)
        result = asyncio.run(
            service.answer(
                authorization=self.authorization,
                question="What is the maximum pressure for I 15.11-11-V?",
                mandatory_constraints={"medium": "nitrogen"},
            )
        )
        self.assertEqual(result.status, "answered_after_repair")
        self.assertTrue(result.validation.valid)
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(model.requests[1].violations)

    def test_two_invalid_answers_return_deterministic_refusal(self) -> None:
        model = SequenceModelGateway(
            [
                "The maximum pressure is 999 bar.",
                "It is definitely 998 bar.",
            ]
        )
        service = AnswerService(releases=self.releases, index=self.index, model=model)
        result = asyncio.run(
            service.answer(
                authorization=self.authorization,
                question="What is the maximum pressure for I 15.11-11-V?",
            )
        )
        self.assertEqual(result.status, "refused_after_validation")
        self.assertIn("failed deterministic claim validation", result.answer)
        self.assertEqual(len(model.requests), 2)

    def test_no_authorized_evidence_never_calls_model(self) -> None:
        model = SequenceModelGateway(["must not be used"])
        service = AnswerService(releases=self.releases, index=self.index, model=model)
        empty_auth = AuthorizationContext(
            tenant_id="tenant",
            knowledge_base_id="kb",
            user_id="user",
            agent_id="agent",
            audience="bauer-evidence-v3",
            issued_at=1,
            expires_at=2,
            authorized_source_ids=(),
        )
        result = asyncio.run(
            service.answer(
                authorization=empty_auth,
                question="What is the maximum pressure?",
            )
        )
        self.assertEqual(result.status, "refused_no_evidence")
        self.assertFalse(model.requests)


if __name__ == "__main__":
    unittest.main()
