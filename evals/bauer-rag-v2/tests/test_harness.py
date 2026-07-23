from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


EVAL_ROOT = Path(__file__).resolve().parents[1]
RUNNERS = EVAL_ROOT / "runners"
sys.path.insert(0, str(RUNNERS))

import common  # noqa: E402


def load_score_module():
    path = EVAL_ROOT / "scorers" / "score.py"
    spec = importlib.util.spec_from_file_location("bauer_rag_v2_score", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


score = load_score_module()


class EvaluationHarnessTests(unittest.TestCase):
    def test_case_prompt_supports_english_and_german_cases(self):
        self.assertEqual(common.case_prompt({"id": "EN", "prompt_en": "Question"}), "Question")
        self.assertEqual(common.case_prompt({"id": "DE", "prompt_de": "Frage"}), "Frage")
        with self.assertRaisesRegex(ValueError, "has no prompt"):
            common.case_prompt({"id": "missing"})

    def test_http_runner_uses_browser_user_agent_required_by_librechat(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def open_request(request, timeout):
            captured["user_agent"] = request.get_header("User-agent")
            captured["timeout"] = timeout
            return Response()

        with patch.object(common.urllib.request, "urlopen", open_request):
            status, _, _ = common.request_json(
                method="GET",
                url="https://example.invalid/api",
                token="short-lived-test-token",
                timeout=7,
            )

        self.assertEqual(status, 200)
        self.assertIn("Chrome/", captured["user_agent"])
        self.assertIn("KHTML, like Gecko", captured["user_agent"])
        self.assertEqual(captured["timeout"], 7)

    def test_holdout_requires_explicit_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, "tuning-locked holdout"):
            common.load_cases("holdout")
        self.assertEqual(len(common.load_cases("holdout", acknowledge_holdout=True)), 10)

    def test_v1_normalizer_drops_results_outside_manifest_allowlist(self):
        payload = [
            [
                {
                    "page_content": "Allowed",
                    "metadata": {"file_id": "allowed", "source": "/files/a.md"},
                },
                0.1,
            ],
            [
                {
                    "page_content": "Forbidden",
                    "metadata": {"file_id": "other", "source": "/files/b.md"},
                },
                0.01,
            ],
        ]
        results, unauthorized = common.normalize_v1(payload, {"allowed"})
        self.assertEqual(unauthorized, 1)
        self.assertEqual([item["file_id"] for item in results], ["allowed"])

    def test_retrieval_metrics_compute_recall_mrr_duplicates_and_authorization(self):
        observations = [
            {
                "case_id": "X",
                "system": "v2",
                "repetition": 1,
                "elapsed_ms": 100,
                "results": [
                    {"file_id": "irrelevant", "content": "x"},
                    {
                        "file_id": "gold",
                        "content": "Model K 28",
                        "row_label": "K 28",
                        "headers": ["Model", "Pressure"],
                        "row_values": ["K 28", "525"],
                        "units": ["bar"],
                    },
                    {
                        "file_id": "gold",
                        "content": "Model K 28",
                        "row_label": "K 28",
                        "headers": ["Model", "Pressure"],
                        "row_values": ["K 28", "525"],
                        "units": ["bar"],
                    },
                ],
            }
        ]
        gold = {
            "X": {
                "required_evidence": [
                    {"file_id": "gold", "row": "K 28", "table": None}
                ]
            }
        }
        report = score.retrieval_metrics(
            observations, gold, {"gold", "irrelevant"}
        )
        item = report["observations"][0]
        self.assertEqual(item["recall_at"]["1"], 0)
        self.assertEqual(item["recall_at"]["3"], 1)
        self.assertEqual(item["reciprocal_rank"], 0.5)
        self.assertGreater(item["duplicate_rate"], 0)
        self.assertEqual(report["authorization_violations"], 0)

    def test_answer_metrics_detect_unsupported_identifier_and_number(self):
        observations = [
            {
                "case_id": "X",
                "system": "v2",
                "repetition": 1,
                "answer": "Use synthetic SYN-FAKE-999 at 999 bar.",
                "evidence": [
                    {
                        "file_id": "gold",
                        "citation_id": "V2-1",
                        "content": "SYN-REAL-420 is synthetic and rated 420 bar.",
                    }
                ],
            }
        ]
        gold = {
            "X": {
                "required_claims": [{"id": "real", "all_terms": ["SYN-REAL-420"]}],
                "forbidden_claims": [],
                "mandatory_constraints": {},
                "safe_refusal_expected": False,
            }
        }
        cases = {
            "X": {
                "prompt_en": "Find the real synthetic project.",
                "category": "structured_part_search",
            }
        }
        report = score.answer_metrics(observations, gold, cases, {"gold"})
        item = report["observations"][0]
        self.assertIn("SYN-FAKE-999", item["unsupported_identifiers"])
        self.assertIn("999 bar", item["unsupported_numbers"])
        self.assertIn("fabricated_identifier", item["hard_failures"])
        self.assertFalse(item["exact_answer_passed"])

    def test_v2_citation_must_support_adjacent_important_claim(self):
        answer = "The maximum pressure is 525 bar [V2-1]."
        match = score.CITATION_RE.search(answer)
        self.assertTrue(
            score.v2_citation_supports(
                answer,
                match,
                {"v2-1": {"content": "Maximum pressure 525 bar"}},
            )
        )
        self.assertFalse(
            score.v2_citation_supports(
                answer,
                match,
                {"v2-1": {"content": "Maximum pressure 420 bar"}},
            )
        )

    def test_uncited_public_answer_fails_citation_and_exactness(self):
        observations = [
            {
                "case_id": "X",
                "system": "v2",
                "repetition": 1,
                "answer": "The maximum pressure is 525 bar.",
                "evidence": [
                    {
                        "file_id": "gold",
                        "citation_id": "V2-1",
                        "source_type": "public_document",
                        "content": "The maximum pressure is 525 bar.",
                    }
                ],
            }
        ]
        gold = {
            "X": {
                "required_claims": [
                    {"id": "pressure", "all_terms": ["525 bar"]}
                ],
                "forbidden_claims": [],
                "mandatory_constraints": {},
                "safe_refusal_expected": False,
            }
        }
        cases = {"X": {"prompt_en": "Maximum pressure?", "category": "table"}}
        report = score.answer_metrics(observations, gold, cases, {"gold"})
        item = report["observations"][0]
        self.assertTrue(item["citation_correct"])
        self.assertFalse(item["citation_complete"])
        self.assertFalse(item["exact_answer_passed"])
        self.assertEqual(report["by_system"]["v2"]["citation_correctness"], 0)

    def test_incompatible_engineering_claim_is_a_hard_failure(self):
        observations = [
            {
                "case_id": "X",
                "system": "v2",
                "repetition": 1,
                "answer": "The 365 bar project is compatible.",
                "evidence": [],
            }
        ]
        gold = {
            "X": {
                "required_claims": [],
                "forbidden_claims": ["365 bar project is compatible"],
                "incompatible_engineering_claims": [
                    "365 bar project is compatible"
                ],
                "mandatory_constraints": {},
                "safe_refusal_expected": False,
            }
        }
        cases = {"X": {"prompt_en": "Is it compatible?", "category": "safety"}}
        report = score.answer_metrics(observations, gold, cases, set())
        item = report["observations"][0]
        self.assertIn("incompatible_engineering_result", item["hard_failures"])
        self.assertFalse(item["exact_answer_passed"])

    def test_provisional_gold_always_blocks_promotion(self):
        gates = {
            "gold_status": "provisional_requires_bauer_adjudication",
            "thresholds": {
                "exact_identifier_document_lookup": 0.95,
                "exact_answer_accuracy": 0.9,
                "citation_correctness": 0.95,
                "safe_refusal_repeated_runs": 1.0,
                "v2_wins_or_ties_v1": 0.8,
                "retrieval_recall_at_5_minimum_delta": 0.05,
                "maximum_category_regression": 0.05,
                "retrieval_p95_seconds": 2.0,
                "end_to_end_p95_seconds": 45.0,
            },
        }
        result = score.promotion("combined", None, None, gates)
        self.assertFalse(result["gold_verified"])
        self.assertFalse(result["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
