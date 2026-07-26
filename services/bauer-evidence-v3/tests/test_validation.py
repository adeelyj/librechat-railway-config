from __future__ import annotations

import unittest
from dataclasses import replace

from bauer_evidence_v3.evidence import EvidenceCitation, EvidencePackage
from bauer_evidence_v3.planner import analyze_query
from bauer_evidence_v3.validation import ValidationDisposition, validate_answer


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.citation = EvidenceCitation(
            citation_id="E-ABCDEF123456",
            evidence_id="evidence",
            source_document_id="source",
            external_file_id=None,
            source_version_id="version",
            source_sha256="ab" * 32,
            title="Technical data",
            source_type="public_document",
            page_number=4,
            printed_page_label="3",
            content="Model BM 40 has a maximum operating pressure of 350 bar.",
            table_headers=("Model", "Maximum pressure"),
            table_values=("BM 40", "350 bar"),
            unit="bar",
            footnotes=(),
            channels=("exact", "table"),
            score=1.0,
        )
        self.package = EvidencePackage(
            release_id="release",
            tenant_id="tenant",
            knowledge_base_id="kb",
            question="question",
            citations=(self.citation,),
            truncated=False,
        )

    def test_number_and_identifier_require_correct_adjacent_citation(self) -> None:
        plan = analyze_query("What is the pressure for BM 40?")
        valid = validate_answer(
            answer="BM 40 has a maximum pressure of 350 bar [E-ABCDEF123456].",
            package=self.package,
            plan=plan,
        )
        self.assertTrue(valid.valid)
        uncited = validate_answer(
            answer="BM 40 has a maximum pressure of 350 bar.",
            package=self.package,
            plan=plan,
        )
        self.assertFalse(uncited.valid)
        self.assertIn(
            "important_claim_without_adjacent_citation",
            {item.code for item in uncited.violations},
        )

    def test_wrong_evidence_value_and_unknown_citation_are_rejected(self) -> None:
        plan = analyze_query("What is the pressure for BM 40?")
        result = validate_answer(
            answer="BM 40 has a maximum pressure of 500 bar [E-000000000000].",
            package=self.package,
            plan=plan,
        )
        codes = {item.code for item in result.violations}
        self.assertIn("citation_not_retrieved", codes)
        self.assertIn("unsupported_number", codes)
        self.assertEqual(result.disposition, ValidationDisposition.REPAIR)

    def test_constraint_loss_and_forbidden_claim_are_hard_failures(self) -> None:
        plan = analyze_query(
            "Find a nitrogen compressor",
            mandatory_constraints={"medium": "nitrogen"},
            forbidden_claim_values=("oxygen",),
        )
        result = validate_answer(
            answer="The oxygen option is BM 40 [E-ABCDEF123456].",
            package=self.package,
            plan=plan,
        )
        codes = {item.code for item in result.violations}
        self.assertIn("mandatory_constraint_missing", codes)
        self.assertIn("forbidden_claim_value", codes)

    def test_numeric_constraints_are_validated_semantically(self) -> None:
        plan = analyze_query(
            "Which BM 40 pressure is suitable?",
            mandatory_constraints={"pressure": "> 300 bar"},
        )
        valid = validate_answer(
            answer=(
                "BM 40 has a maximum operating pressure of 350 bar "
                "[E-ABCDEF123456]."
            ),
            package=self.package,
            plan=plan,
        )
        self.assertNotIn(
            "mandatory_constraint_missing",
            {item.code for item in valid.violations},
        )
        self.assertNotIn(
            "numeric_constraint_missing",
            {item.code for item in valid.violations},
        )

        missing = validate_answer(
            answer="BM 40 is listed [E-ABCDEF123456].",
            package=self.package,
            plan=plan,
        )
        self.assertIn(
            "numeric_constraint_missing",
            {item.code for item in missing.violations},
        )

        forbidden_plan = analyze_query(
            "What is the pressure for BM 40?",
            forbidden_claim_values=("> 300 bar",),
        )
        forbidden = validate_answer(
            answer=(
                "BM 40 has a maximum operating pressure of 350 bar "
                "[E-ABCDEF123456]."
            ),
            package=self.package,
            plan=forbidden_plan,
        )
        self.assertIn(
            "forbidden_claim_value",
            {item.code for item in forbidden.violations},
        )

    def test_quotation_must_be_in_its_cited_evidence(self) -> None:
        plan = analyze_query("Quote BM 40")
        result = validate_answer(
            answer='"BM 40 is certified for Mars" [E-ABCDEF123456].',
            package=self.package,
            plan=plan,
        )
        self.assertIn("unsupported_quotation", {item.code for item in result.violations})

    def test_ordinary_factual_claims_require_adjacent_grounded_citations(self) -> None:
        plan = analyze_query("Is BM 40 suitable for explosive atmospheres?")
        for answer in (
            "This model is suitable for explosive atmospheres.",
            "It is certified for oxygen service.",
            "The maximum pressure is high.",
        ):
            with self.subTest(answer=answer):
                result = validate_answer(
                    answer=answer,
                    package=self.package,
                    plan=plan,
                )
                self.assertFalse(result.valid)
                self.assertIn(
                    "factual_claim_without_adjacent_citation",
                    {item.code for item in result.violations},
                )

        cited_but_unsupported = validate_answer(
            answer=(
                "This model is suitable for explosive atmospheres "
                "[E-ABCDEF123456]."
            ),
            package=self.package,
            plan=plan,
        )
        self.assertIn(
            "unsupported_factual_claim",
            {item.code for item in cited_but_unsupported.violations},
        )

    def test_prose_hyphenation_and_relative_clauses_match_source_terms(
        self,
    ) -> None:
        citation = replace(
            self.citation,
            content=(
                "The free air delivery for model BM 40 is 420 l/min."
            ),
            table_headers=("Model", "Free air delivery"),
            table_values=("BM 40", "420 l/min"),
        )
        package = replace(self.package, citations=(citation,))
        result = validate_answer(
            answer=(
                "The free-air delivery, which is documented for BM 40, "
                "is 420 l/min [E-ABCDEF123456]."
            ),
            package=package,
            plan=analyze_query("What is the free-air delivery for BM 40?"),
        )

        self.assertTrue(result.valid)

    def test_cited_source_reference_wording_is_not_an_engineering_claim(
        self,
    ) -> None:
        pressure_variant = replace(
            self.citation,
            citation_id="E-111111111111",
            evidence_id="pressure-variant",
            content=(
                "Group: VERTICUS I 420 - 525 bar; model "
                "I 15.11-11-V; maximum operating pressure 525; motor "
                "power 11."
            ),
            table_headers=(),
            table_values=(),
        )
        package = replace(
            self.package,
            question=(
                "Report the technical-data table for I 15.11-11-V."
            ),
            citations=(pressure_variant,),
        )
        result = validate_answer(
            answer=(
                "The technical-data table lists a maximum operating "
                "pressure of 525, depending on the pressure group "
                "[E-111111111111]."
            ),
            package=package,
            plan=analyze_query(package.question),
        )

        self.assertTrue(result.valid)

    def test_cited_two_value_comparison_allows_relational_language(
        self,
    ) -> None:
        citation = replace(
            self.citation,
            content=(
                "B-SAFE supports breathing air up to 300 bar and Nitrox "
                "up to 200 bar."
            ),
            table_headers=("Medium", "Maximum operating pressure"),
            table_values=("Breathing air 300 bar", "Nitrox 200 bar"),
        )
        package = replace(
            self.package,
            question=(
                "Is a 300 bar Nitrox cylinder supported by B-SAFE?"
            ),
            citations=(citation,),
        )
        result = validate_answer(
            answer=(
                "The requested 300 bar Nitrox condition exceeds the "
                "documented limit of 200 bar [E-ABCDEF123456]."
            ),
            package=package,
            plan=analyze_query(package.question),
        )

        self.assertTrue(result.valid)
        unsupported_without_two_values = validate_answer(
            answer=(
                "The BM 40 exceeds the documented limit "
                "[E-ABCDEF123456]."
            ),
            package=self.package,
            plan=analyze_query("Does BM 40 exceed the limit?"),
        )
        self.assertIn(
            "unsupported_factual_claim",
            {
                item.code
                for item in unsupported_without_two_values.violations
            },
        )

    def test_refusal_phrase_cannot_mask_a_later_unsupported_assertion(self) -> None:
        plan = analyze_query(
            "Is oxygen supported?",
            forbidden_claim_values=("oxygen is supported",),
        )
        result = validate_answer(
            answer=(
                "I cannot confirm oxygen compatibility. "
                "Oxygen is supported."
            ),
            package=self.package,
            plan=plan,
        )
        codes = {item.code for item in result.violations}
        self.assertIn("factual_claim_without_adjacent_citation", codes)
        self.assertIn("forbidden_claim_value", codes)

    def test_safe_refusal_may_repeat_question_value_without_claiming_it(self) -> None:
        plan = analyze_query(
            "Is a 300 bar Nitrox cylinder supported by B-SAFE?",
        )
        package = EvidencePackage(
            release_id=self.package.release_id,
            tenant_id=self.package.tenant_id,
            knowledge_base_id=self.package.knowledge_base_id,
            question="Is a 300 bar Nitrox cylinder supported by B-SAFE?",
            citations=self.package.citations,
            truncated=False,
        )
        result = validate_answer(
            answer="The requested 300 bar Nitrox condition is not supported.",
            package=package,
            plan=plan,
        )
        self.assertTrue(result.valid)
        self.assertTrue(result.safe_refusal_detected)

    def test_question_terms_are_context_only_when_present_in_evidence(self) -> None:
        product_context = replace(
            self.citation,
            citation_id="E-111111111111",
            evidence_id="product-context",
            content="B-SAFE product overview.",
            table_headers=(),
            table_values=(),
        )
        supported_context = EvidencePackage(
            release_id=self.package.release_id,
            tenant_id=self.package.tenant_id,
            knowledge_base_id=self.package.knowledge_base_id,
            question="What is documented for B-SAFE model BM 40?",
            citations=(self.citation, product_context),
            truncated=False,
        )
        valid = validate_answer(
            answer=(
                "For B-SAFE, model BM 40 has a maximum operating pressure of 350 bar "
                "[E-ABCDEF123456]."
            ),
            package=supported_context,
            plan=analyze_query(supported_context.question),
        )
        self.assertTrue(valid.valid)

        unsupported_context = EvidencePackage(
            release_id=self.package.release_id,
            tenant_id=self.package.tenant_id,
            knowledge_base_id=self.package.knowledge_base_id,
            question="Is BM 40 suitable for explosive atmospheres?",
            citations=self.package.citations,
            truncated=False,
        )
        invalid = validate_answer(
            answer=(
                "BM 40 is suitable for explosive atmospheres "
                "[E-ABCDEF123456]."
            ),
            package=unsupported_context,
            plan=analyze_query(unsupported_context.question),
        )
        self.assertIn(
            "unsupported_factual_claim",
            {item.code for item in invalid.violations},
        )

    def test_citation_uses_retrieved_support_from_same_source_only(self) -> None:
        same_source_detail = replace(
            self.citation,
            citation_id="E-222222222222",
            evidence_id="same-source-detail",
            content="The motor output is 11 kW.",
            table_headers=("Motor output",),
            table_values=("11 kW",),
        )
        other_source_detail = replace(
            self.citation,
            citation_id="E-333333333333",
            evidence_id="other-source-detail",
            source_document_id="other-source",
            content="The motor output is 15 kW.",
            table_headers=("Motor output",),
            table_values=("15 kW",),
        )
        package = EvidencePackage(
            release_id=self.package.release_id,
            tenant_id=self.package.tenant_id,
            knowledge_base_id=self.package.knowledge_base_id,
            question="What are the pressure and motor output for BM 40?",
            citations=(self.citation, same_source_detail, other_source_detail),
            truncated=False,
        )
        same_source = validate_answer(
            answer=(
                "BM 40 has a maximum pressure of 350 bar and motor output "
                "of 11 kW [E-ABCDEF123456]."
            ),
            package=package,
            plan=analyze_query(package.question),
        )
        self.assertTrue(same_source.valid)

        cross_source = validate_answer(
            answer=(
                "BM 40 has a maximum pressure of 350 bar and motor output "
                "of 15 kW [E-ABCDEF123456]."
            ),
            package=package,
            plan=analyze_query(package.question),
        )
        self.assertIn(
            "unsupported_number",
            {item.code for item in cross_source.violations},
        )


if __name__ == "__main__":
    unittest.main()
