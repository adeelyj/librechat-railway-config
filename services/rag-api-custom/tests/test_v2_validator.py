import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_rag_v2.validator import validate_answer  # noqa: E402


EVIDENCE = [
    {
        "citation_id": "V2-1",
        "file_id": "file-k28",
        "filename": "k-range.md",
        "content": "Technical data. Row K 28. Maximum pressure 525 bar.",
        "row_label": "K 28",
        "row_values": ["525", "bar"],
        "source_type": "public_document",
    }
]


class V2ValidatorTests(unittest.TestCase):
    def codes(self, result):
        return {item["code"] for item in result["violations"]}

    def test_supported_number_identifier_and_citation_pass(self):
        result = validate_answer(
            answer="K 28 has a documented maximum pressure of 525 bar [V2-1].",
            evidence=EVIDENCE,
        )
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["valid"])

    def test_fabricated_identifier_is_rejected(self):
        result = validate_answer(
            answer="Model K 99 is rated to 525 bar [V2-1].",
            evidence=EVIDENCE,
        )
        self.assertIn("unsupported_identifier", self.codes(result))
        self.assertEqual(result["status"], "refuse")

    def test_unsupported_high_risk_number_is_rejected(self):
        result = validate_answer(
            answer="K 28 is rated to 550 bar [V2-1].",
            evidence=EVIDENCE,
        )
        self.assertIn("unsupported_number", self.codes(result))

    def test_unknown_citation_is_rejected(self):
        result = validate_answer(
            answer="K 28 is rated to 525 bar [V2-7].",
            evidence=EVIDENCE,
        )
        self.assertIn("citation_not_retrieved", self.codes(result))

    def test_quotation_must_exist_verbatim_after_normalization(self):
        result = validate_answer(
            answer='The source says "maximum compressor pressure is 600 bar" [V2-1].',
            evidence=EVIDENCE,
        )
        self.assertIn("quotation_not_in_evidence", self.codes(result))

    def test_synthetic_evidence_must_be_labelled(self):
        result = validate_answer(
            answer="SYN-BK-N2-420-500 is the selected record [V2-1].",
            evidence=[
                {
                    "citation_id": "V2-1",
                    "content": "SYN-BK-N2-420-500",
                    "source_type": "synthetic_demo",
                }
            ],
        )
        self.assertIn("synthetic_source_unlabelled", self.codes(result))

    def test_safe_refusal_expectation_is_deterministic(self):
        missing = validate_answer(
            answer="The system is compatible.",
            evidence=[],
            safe_refusal_expected=True,
        )
        self.assertIn("required_safe_refusal_missing", self.codes(missing))
        passing = validate_answer(
            answer="The requested limit was not established in the indexed Bauer sources.",
            evidence=[],
            safe_refusal_expected=True,
        )
        self.assertEqual(passing["status"], "pass")
        compatible = validate_answer(
            answer="No compatible synthetic helium project was found.",
            evidence=[],
            safe_refusal_expected=True,
        )
        self.assertTrue(compatible["safe_refusal_detected"])

    def test_mandatory_constraint_cannot_disappear(self):
        result = validate_answer(
            answer="A compatible compressor was found.",
            evidence=[],
            mandatory_constraints={"medium": "helium", "pressure_bar": 420},
        )
        self.assertIn("mandatory_constraint_missing", self.codes(result))


if __name__ == "__main__":
    unittest.main()
