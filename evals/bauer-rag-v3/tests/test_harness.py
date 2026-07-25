from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT))

from v3eval import io  # noqa: E402
from v3eval.io import EvaluationContractError, LockedHoldoutError  # noqa: E402
from v3eval.metrics import (  # noqa: E402
    score_answers,
    score_authorization,
    score_extraction,
    score_latency,
    score_retrieval,
    score_tables,
)
from v3eval.report import score_run  # noqa: E402


def synthetic_suite(split: str = "development") -> dict:
    return {
        "schema_version": 1,
        "split": split,
        "gold_status": "synthetic_reviewed",
        "authorization_scope": {
            "tenant_id": "tenant-synthetic",
            "knowledge_base_id": "kb-synthetic",
            "allowed_release_ids": ["release-synthetic"],
            "allowed_source_ids": ["source-a", "source-b"],
        },
        "extraction_targets": [],
        "table_targets": [],
        "queries": [],
    }


def synthetic_run(split: str = "development") -> dict:
    return {
        "schema_version": 1,
        "run_id": "synthetic-run",
        "split": split,
        "release_id": "release-synthetic",
        "observations": {
            "extraction": [],
            "tables": [],
            "retrieval": [],
            "answers": [],
            "latency": [],
            "authorization": [],
        },
    }


def scoped_result(
    evidence_id: str = "evidence-a",
    *,
    source_id: str = "source-a",
    tenant_id: str = "tenant-synthetic",
    knowledge_base_id: str = "kb-synthetic",
    release_id: str = "release-synthetic",
) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "tenant_id": tenant_id,
        "knowledge_base_id": knowledge_base_id,
        "release_id": release_id,
    }


class SplitSafetyTests(unittest.TestCase):
    def write_suite(self, root: Path, payload: dict) -> None:
        cases = root / "cases"
        cases.mkdir(parents=True)
        (cases / f"{payload['split']}.yaml").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_development_is_the_default_and_only_implicitly_loadable_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_suite(root, synthetic_suite())
            loaded = io.load_suite(root)
        self.assertEqual(loaded["split"], "development")

    def test_holdout_is_rejected_before_any_file_read(self):
        with patch.object(io, "_read_json") as read_json:
            with self.assertRaisesRegex(LockedHoldoutError, "locked holdout"):
                io.load_suite(Path("does-not-matter"), split="holdout")
        read_json.assert_not_called()

    def test_holdout_requires_both_explicit_split_and_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_suite(root, synthetic_suite("holdout"))
            loaded = io.load_suite(
                root,
                split="holdout",
                acknowledge_locked_holdout=True,
            )
        self.assertEqual(loaded["split"], "holdout")

    def test_combined_split_is_forbidden(self):
        with self.assertRaisesRegex(EvaluationContractError, "combined loading"):
            io.load_suite(Path("unused"), split="all")

    def test_run_must_match_the_explicitly_selected_split(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text(json.dumps(synthetic_run("holdout")), encoding="utf-8")
            with self.assertRaisesRegex(EvaluationContractError, "expected 'development'"):
                io.load_run(path, expected_split="development")


class MetricFamilyTests(unittest.TestCase):
    def test_extraction_qa_keeps_completeness_and_quality_signals_separate(self):
        targets = [
            {
                "source_id": "source-a",
                "expected_page_count": 2,
                "facts": [
                    {"fact_id": "fact-pressure", "value": "525", "unit": "bar"}
                ],
            },
            {"source_id": "source-b", "expected_page_count": 1},
        ]
        observations = [
            {
                "source_id": "source-a",
                "status": "published",
                "page_count": 2,
                "evidence_count": 4,
                "resolvable_evidence_count": 3,
                "reading_order_checks": 2,
                "reading_order_passes": 1,
                "stable_id_checks": 2,
                "stable_id_matches": 2,
                "facts": [
                    {
                        "fact_id": "fact-pressure",
                        "value": "525",
                        "unit": "bar",
                        "source_coordinate_resolvable": True,
                    }
                ],
            }
        ]
        result = score_extraction(targets, observations)
        self.assertEqual(result["source_accounting_rate"], 0.5)
        self.assertEqual(result["page_completeness"], 0.666667)
        self.assertEqual(result["citation_resolvability"], 0.75)
        self.assertEqual(result["reading_order_accuracy"], 0.5)
        self.assertEqual(result["deterministic_id_rate"], 1.0)
        self.assertEqual(result["fact_value_accuracy"], 1.0)
        self.assertEqual(result["fact_unit_accuracy"], 1.0)
        self.assertEqual(result["fact_coordinate_resolvability"], 1.0)
        self.assertEqual(result["missing_source_ids"], ["source-b"])

    def test_retrieval_reports_recall_mrr_exactness_and_duplicates(self):
        queries = [
            {
                "case_id": "query-a",
                "retrieval": {
                    "required_evidence_ids": ["evidence-gold"],
                    "exact_lookup": True,
                },
            }
        ]
        observations = [
            {
                "case_id": "query-a",
                "results": [
                    {"evidence_id": "evidence-other"},
                    {"evidence_id": "evidence-gold"},
                    {"evidence_id": "evidence-gold"},
                ],
            }
        ]
        result = score_retrieval(queries, observations)
        self.assertEqual(result["recall_at_1"], 0.0)
        self.assertEqual(result["recall_at_3"], 1.0)
        self.assertEqual(result["recall_at_5"], 1.0)
        self.assertEqual(result["mean_reciprocal_rank"], 0.5)
        self.assertEqual(result["exact_lookup_accuracy"], 0.0)
        self.assertEqual(result["duplicate_rate"], 0.333333)

    def test_table_integrity_scores_grid_values_units_spans_and_coordinates(self):
        targets = [
            {
                "table_id": "table-a",
                "row_count": 1,
                "column_count": 2,
                "cells": [
                    {
                        "cell_id": "cell-model",
                        "value": "K 28",
                        "unit": None,
                        "row_span": 1,
                        "column_span": 1,
                    },
                    {
                        "cell_id": "cell-pressure",
                        "value": "525",
                        "unit": "bar",
                        "row_span": 1,
                        "column_span": 1,
                    },
                ],
            }
        ]
        observations = [
            {
                "table_id": "table-a",
                "row_count": 1,
                "column_count": 2,
                "cells": [
                    {
                        "cell_id": "cell-model",
                        "value": "K 28",
                        "unit": None,
                        "row_span": 1,
                        "column_span": 1,
                        "source_coordinate_resolvable": True,
                    },
                    {
                        "cell_id": "cell-pressure",
                        "value": "525",
                        "unit": "psi",
                        "row_span": 1,
                        "column_span": 1,
                        "source_coordinate_resolvable": False,
                    },
                ],
            }
        ]
        result = score_tables(targets, observations)
        self.assertEqual(result["grid_exact_rate"], 1.0)
        self.assertEqual(result["cell_value_accuracy"], 1.0)
        self.assertEqual(result["unit_accuracy"], 0.0)
        self.assertEqual(result["span_accuracy"], 1.0)
        self.assertEqual(result["cell_coordinate_resolvability"], 0.5)
        self.assertEqual(result["overall_cell_integrity"], 0.5)

    def test_answer_grounding_uses_structured_validator_records(self):
        queries = [
            {
                "case_id": "answer-a",
                "answer": {
                    "required_constraints": ["pressure", "material"],
                    "refusal_expected": False,
                },
            },
            {
                "case_id": "answer-b",
                "answer": {
                    "required_constraints": [],
                    "refusal_expected": True,
                },
            },
        ]
        observations = [
            {
                "case_id": "answer-a",
                "validator_decision": "accepted",
                "refused": False,
                "claims": [
                    {
                        "important": True,
                        "supported": True,
                        "citation_valid": True,
                        "high_risk": False,
                    },
                    {
                        "important": True,
                        "supported": False,
                        "citation_valid": False,
                        "high_risk": True,
                    },
                ],
                "constraints": [
                    {"id": "pressure", "preserved": True},
                    {"id": "material", "preserved": False},
                ],
            },
            {
                "case_id": "answer-b",
                "validator_decision": "refused",
                "refused": True,
                "claims": [],
                "constraints": [],
            },
        ]
        result = score_answers(queries, observations)
        self.assertEqual(result["important_claim_support_rate"], 0.5)
        self.assertEqual(result["important_claim_citation_correctness"], 0.5)
        self.assertEqual(result["constraint_preservation_rate"], 0.5)
        self.assertEqual(result["safe_abstention_accuracy"], 1.0)
        self.assertEqual(result["unsupported_high_risk_claims_accepted"], 1)

    def test_latency_is_partitioned_by_stage_and_route(self):
        observations = [
            {
                "stage": "retrieval",
                "route": "exact",
                "elapsed_ms": 10,
                "success": True,
            },
            {
                "stage": "retrieval",
                "route": "exact",
                "elapsed_ms": 20,
                "success": True,
            },
            {
                "stage": "retrieval",
                "route": "visual",
                "elapsed_ms": 100,
                "success": True,
            },
        ]
        result = score_latency(observations)["by_route"]
        self.assertEqual(set(result), {"retrieval:exact", "retrieval:visual"})
        self.assertEqual(result["retrieval:exact"]["p50_ms"], 15.0)
        self.assertEqual(result["retrieval:visual"]["p95_ms"], 100.0)

    def test_authorization_passes_only_fully_scoped_results(self):
        scope = synthetic_suite()["authorization_scope"]
        result = score_authorization(
            scope,
            retrieval_observations=[
                {"results": [scoped_result("evidence-retrieval")]}
            ],
            answer_observations=[
                {"evidence": [scoped_result("evidence-answer", source_id="source-b")]}
            ],
            authorization_observations=[
                {"unauthorized_result_count": 0, "authorized": True}
            ],
        )
        self.assertTrue(result["scope_configured"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["violation_count"], 0)

    def test_authorization_detects_source_tenant_release_and_explicit_leakage(self):
        scope = synthetic_suite()["authorization_scope"]
        result = score_authorization(
            scope,
            retrieval_observations=[
                {
                    "results": [
                        scoped_result(
                            source_id="source-forbidden",
                            tenant_id="tenant-other",
                            release_id="release-other",
                        )
                    ]
                }
            ],
            answer_observations=[],
            authorization_observations=[
                {
                    "unauthorized_result_count": 1,
                    "authorized": False,
                    "delivered": True,
                }
            ],
        )
        kinds = {item["kind"] for item in result["violations"]}
        self.assertFalse(result["passed"])
        self.assertIn("tenant_id_mismatch", kinds)
        self.assertIn("release_scope_mismatch", kinds)
        self.assertIn("source_scope_mismatch", kinds)
        self.assertIn("explicit_unauthorized_result_count", kinds)
        self.assertIn("explicit_denied_item_delivered", kinds)

    def test_report_keeps_six_metric_families_and_surfaces_hard_failures(self):
        suite = synthetic_suite()
        suite["queries"] = [
            {
                "case_id": "answer-a",
                "answer": {
                    "required_constraints": [],
                    "refusal_expected": False,
                },
            }
        ]
        run = synthetic_run()
        run["observations"]["answers"] = [
            {
                "case_id": "answer-a",
                "validator_decision": "accepted",
                "claims": [
                    {
                        "important": True,
                        "supported": False,
                        "citation_valid": False,
                        "high_risk": True,
                    }
                ],
                "constraints": [],
                "evidence": [],
            }
        ]
        report = score_run(suite, run)
        self.assertEqual(
            set(report["metrics"]),
            {
                "extraction_qa",
                "retrieval",
                "answer_grounding",
                "table_integrity",
                "latency",
                "authorization_leakage",
            },
        )
        self.assertEqual(
            report["hard_failures"],
            [{"kind": "unsupported_high_risk_claim_accepted", "count": 1}],
        )
        self.assertFalse(report["promotion"]["evaluated"])
        self.assertFalse(report["promotion"]["allowed"])


if __name__ == "__main__":
    unittest.main()
