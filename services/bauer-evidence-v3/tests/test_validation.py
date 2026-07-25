from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
