from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.evaluation import (  # noqa: E402
    EvaluationManifestError,
    EvaluationObservations,
    EvaluationRequest,
    EvaluationRunner,
    EvaluationTargetResult,
    HttpEvaluationTarget,
    LockedHoldoutError,
    load_evaluation_manifest,
    parse_evaluation_manifest,
)


TENANT_ID = "10000000-0000-4000-8000-000000000001"
KB_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_ID = "30000000-0000-4000-8000-000000000001"
OTHER_RELEASE_ID = "30000000-0000-4000-8000-000000000002"
SOURCE_ID = "40000000-0000-4000-8000-000000000001"
OTHER_SOURCE_ID = "40000000-0000-4000-8000-000000000002"
EVIDENCE_ID = "50000000-0000-4000-8000-000000000001"
SOURCE_VERSION_ID = "41000000-0000-4000-8000-000000000001"
ARTIFACT_SET_ID = "42000000-0000-4000-8000-000000000001"
FACT_ID = "fact_pressure"
TABLE_ID = "table_pressure"
CELL_ID = "cell_pressure"
SOURCE_SHA256 = "a" * 64


def manifest_payload(*, split="development", cases=None):
    return {
        "schema_version": 1,
        "suite_key": "bauer-v3-development",
        "suite_version": "2026-07-25.1",
        "split": split,
        "gold_status": "interim_reviewed",
        "authorization_scope": {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": KB_ID,
            "allowed_release_ids": [RELEASE_ID],
            "allowed_source_ids": [SOURCE_ID],
        },
        "metadata": {"reviewer": "interim"},
        "extraction_targets": [],
        "table_targets": [],
        "queries": cases
        or [
            {
                "case_id": "pressure-answer",
                "category": "grounded_answer",
                "prompt": {
                    "mode": "answer",
                    "query": "What is the pressure?",
                    "mandatory_constraints": {"unit": "bar"},
                    "top_k": 5,
                },
                "retrieval": {
                    "required_evidence_ids": [EVIDENCE_ID],
                    "exact_lookup": True,
                },
                "answer": {
                    "refusal_expected": False,
                    "required_substrings": ["525 bar"],
                },
                "hard_failure_codes": ["required_evidence_missing"],
            },
            {
                "case_id": "pressure-query",
                "category": "retrieval",
                "prompt": {
                    "mode": "query",
                    "query": "K 28 pressure",
                    "top_k": 3,
                },
                "retrieval": {
                    "required_evidence_ids": [EVIDENCE_ID],
                    "exact_lookup": True,
                },
            },
        ],
    }


class RecordingStore:
    def __init__(self):
        self.prepared = []
        self.begun = []
        self.completed = []
        self.failed = []

    def prepare_suite(self, manifest):
        self.prepared.append(manifest)

    def begin_run(self, spec):
        self.begun.append(spec)

    def complete_run(self, report):
        self.completed.append(report)

    def fail_run(self, spec, *, completed_at, error):
        self.failed.append((spec, completed_at, error))


class PassingTarget:
    async def execute(self, request):
        if request.mode == "query":
            return EvaluationTargetResult(
                status="retrieved",
                release_id=RELEASE_ID,
                evidence=(
                    {
                        "evidence_id": EVIDENCE_ID,
                        "source_document_id": SOURCE_ID,
                        "tenant_id": TENANT_ID,
                        "knowledge_base_id": KB_ID,
                        "release_id": RELEASE_ID,
                    },
                ),
            )
        return EvaluationTargetResult(
            status="answered",
            answer="The pressure is 525 bar.",
            release_id=RELEASE_ID,
            evidence=(
                {
                    "evidence_id": EVIDENCE_ID,
                    "source_document_id": SOURCE_ID,
                    "tenant_id": TENANT_ID,
                    "knowledge_base_id": KB_ID,
                    "release_id": RELEASE_ID,
                },
            ),
            validation={"valid": True, "violations": []},
        )


class ManifestContractTests(unittest.TestCase):
    def test_manifest_derives_stable_suite_and_case_ids(self):
        first = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        second = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        self.assertEqual(first.eval_suite_id, second.eval_suite_id)
        self.assertEqual(
            [case.eval_case_id for case in first.cases],
            [case.eval_case_id for case in second.cases],
        )
        self.assertEqual(first.scope.tenant_id, TENANT_ID)
        self.assertEqual(first.cases[0].mode, "answer")
        self.assertEqual(first.cases[1].mode, "query")

    def test_selected_release_must_be_declared_in_authorization_scope(self):
        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        runner = EvaluationRunner(
            target=PassingTarget(),
            store=RecordingStore(),
        )
        with self.assertRaisesRegex(
            EvaluationManifestError,
            "outside manifest allowed_release_ids",
        ):
            asyncio.run(
                runner.run(
                    manifest,
                    release_id=OTHER_RELEASE_ID,
                    code_version="test-code",
                )
            )

    def test_holdout_is_refused_before_manifest_file_access(self):
        with patch.object(Path, "read_text") as read_text:
            with self.assertRaisesRegex(LockedHoldoutError, "locked holdout"):
                load_evaluation_manifest(
                    Path("never-opened.yaml"),
                    expected_split="holdout",
                )
        read_text.assert_not_called()

    def test_verified_gold_requires_verifier_provenance(self):
        payload = manifest_payload()
        payload["gold_status"] = "independent_bauer_verified"
        with self.assertRaisesRegex(
            EvaluationManifestError,
            "verified_at and verified_by",
        ):
            parse_evaluation_manifest(
                payload,
                expected_split="development",
            )

    def test_manifest_accepts_scoped_extraction_and_table_targets(self):
        payload = manifest_payload()
        payload["extraction_targets"] = [
            {
                "source_id": SOURCE_ID,
                "expected_page_count": 1,
                "facts": [
                    {
                        "fact_id": FACT_ID,
                        "value": "525",
                        "unit": "bar",
                    }
                ],
            }
        ]
        payload["table_targets"] = [
            {
                "source_id": SOURCE_ID,
                "table_id": TABLE_ID,
                "row_count": 1,
                "column_count": 1,
                "cells": [
                    {
                        "cell_id": CELL_ID,
                        "value": "525",
                        "unit": "bar",
                    }
                ],
            }
        ]
        manifest = parse_evaluation_manifest(
            payload,
            expected_split="development",
        )
        self.assertEqual(
            manifest.extraction_targets[0]["source_id"],
            SOURCE_ID,
        )
        self.assertEqual(
            manifest.table_targets[0]["table_id"],
            TABLE_ID,
        )

    def test_structured_target_source_must_be_authorized(self):
        payload = manifest_payload()
        payload["extraction_targets"] = [
            {
                "source_id": OTHER_SOURCE_ID,
                "expected_page_count": 1,
            }
        ]
        with self.assertRaisesRegex(
            EvaluationManifestError,
            "outside allowed_source_ids",
        ):
            parse_evaluation_manifest(
                payload,
                expected_split="development",
            )


class RunnerTests(unittest.TestCase):
    @staticmethod
    def structured_manifest():
        payload = manifest_payload()
        payload["extraction_targets"] = [
            {
                "source_id": SOURCE_ID,
                "expected_page_count": 1,
                "facts": [
                    {
                        "fact_id": FACT_ID,
                        "value": "525",
                        "unit": "bar",
                    }
                ],
            }
        ]
        payload["table_targets"] = [
            {
                "source_id": SOURCE_ID,
                "table_id": TABLE_ID,
                "row_count": 1,
                "column_count": 1,
                "cells": [
                    {
                        "cell_id": CELL_ID,
                        "value": "525",
                        "unit": "bar",
                    }
                ],
            }
        ]
        return parse_evaluation_manifest(
            payload,
            expected_split="development",
        )

    @staticmethod
    def structured_only_manifest():
        payload = manifest_payload()
        payload["extraction_targets"] = [
            {
                "source_id": SOURCE_ID,
                "expected_page_count": 1,
                "facts": [
                    {
                        "fact_id": FACT_ID,
                        "value": "525",
                        "unit": "bar",
                    }
                ],
            }
        ]
        payload["table_targets"] = [
            {
                "source_id": SOURCE_ID,
                "table_id": TABLE_ID,
                "row_count": 1,
                "column_count": 1,
                "cells": [
                    {
                        "cell_id": CELL_ID,
                        "value": "525",
                        "unit": "bar",
                    }
                ],
            }
        ]
        payload["queries"] = []
        return parse_evaluation_manifest(
            payload,
            expected_split="development",
        )

    @staticmethod
    def passing_observations():
        scope = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": KB_ID,
            "release_id": RELEASE_ID,
            "source_id": SOURCE_ID,
            "source_version_id": SOURCE_VERSION_ID,
            "artifact_set_id": ARTIFACT_SET_ID,
            "source_sha256": SOURCE_SHA256,
        }
        return EvaluationObservations(
            extraction=(
                {
                    **scope,
                    "status": "published",
                    "page_count": 1,
                    "evidence_count": 2,
                    "resolvable_evidence_count": 2,
                    "reading_order_checks": 1,
                    "reading_order_passes": 1,
                    "stable_id_checks": 3,
                    "stable_id_matches": 3,
                    "facts": [
                        {
                            "fact_id": FACT_ID,
                            "value": "525",
                            "unit": "bar",
                            "source_coordinate_resolvable": True,
                        }
                    ],
                },
            ),
            tables=(
                {
                    **scope,
                    "table_id": TABLE_ID,
                    "row_count": 1,
                    "column_count": 1,
                    "cells": [
                        {
                            "cell_id": CELL_ID,
                            "value": "525",
                            "unit": "bar",
                            "row_span": 1,
                            "column_span": 1,
                            "source_coordinate_resolvable": True,
                        }
                    ],
                },
            ),
        )

    def test_runner_captures_and_scores_nonempty_structured_targets(self):
        class ObservationSource:
            def capture(self, *, manifest, release_id):
                self.manifest = manifest
                self.release_id = release_id
                return RunnerTests.passing_observations()

        source = ObservationSource()
        report = asyncio.run(
            EvaluationRunner(
                target=PassingTarget(),
                store=RecordingStore(),
                observation_source=source,
            ).run(
                self.structured_manifest(),
                release_id=RELEASE_ID,
                code_version="commit-structured",
            )
        )
        self.assertTrue(report.passed)
        self.assertEqual(source.release_id, RELEASE_ID)
        self.assertTrue(report.aggregate_metrics["extraction_qa"]["passed"])
        self.assertTrue(report.aggregate_metrics["table_integrity"]["passed"])
        payload = report.to_json()
        self.assertEqual(payload["split"], "development")
        self.assertEqual(payload["run_id"], payload["eval_run_id"])
        self.assertEqual(
            payload["observations"]["extraction"][0]["source_id"],
            SOURCE_ID,
        )
        self.assertEqual(
            payload["observations"]["tables"][0]["table_id"],
            TABLE_ID,
        )

    def test_structured_only_runner_needs_no_answer_or_query_target(self):
        class ObservationSource:
            def capture(self, *, manifest, release_id):
                return RunnerTests.passing_observations()

        report = asyncio.run(
            EvaluationRunner(
                target=None,
                store=RecordingStore(),
                observation_source=ObservationSource(),
            ).run(
                self.structured_only_manifest(),
                release_id=RELEASE_ID,
                code_version="commit-canonical-only",
            )
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.results, ())
        self.assertTrue(report.aggregate_metrics["extraction_qa"]["passed"])
        self.assertTrue(report.aggregate_metrics["table_integrity"]["passed"])

    def test_runner_never_silently_skips_structured_targets(self):
        with self.assertRaisesRegex(
            EvaluationManifestError,
            "cannot be silently skipped",
        ):
            asyncio.run(
                EvaluationRunner(
                    target=PassingTarget(),
                    store=RecordingStore(),
                ).run(
                    self.structured_manifest(),
                    release_id=RELEASE_ID,
                    code_version="commit-structured",
                )
            )

    def test_structured_mismatch_is_a_persisted_hard_failure(self):
        class ObservationSource:
            def capture(self, *, manifest, release_id):
                observations = RunnerTests.passing_observations()
                extraction = dict(observations.extraction[0])
                extraction["page_count"] = 0
                return EvaluationObservations(
                    extraction=(extraction,),
                    tables=observations.tables,
                )

        store = RecordingStore()
        report = asyncio.run(
            EvaluationRunner(
                target=PassingTarget(),
                store=store,
                observation_source=ObservationSource(),
            ).run(
                self.structured_manifest(),
                release_id=RELEASE_ID,
                code_version="commit-structured-failure",
            )
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            report.observation_hard_failures,
            ("extraction_target_failed",),
        )
        self.assertEqual(
            report.hard_failures,
            ("extraction_target_failed",),
        )
        self.assertEqual(store.completed, [report])
        self.assertEqual(store.failed, [])

    def test_runner_executes_answer_and_query_cases_and_completes_pass(self):
        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        store = RecordingStore()
        report = asyncio.run(
            EvaluationRunner(
                target=PassingTarget(),
                store=store,
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-abc",
                repetitions=2,
            )
        )
        self.assertTrue(report.passed)
        self.assertEqual(len(report.results), 4)
        self.assertEqual(report.aggregate_metrics["pass_rate"], 1.0)
        self.assertEqual(report.hard_failures, ())
        self.assertEqual(len(store.prepared), 1)
        self.assertEqual(len(store.begun), 1)
        self.assertEqual(store.completed, [report])
        self.assertEqual(store.failed, [])
        self.assertEqual(
            {
                result.evidence_search_unit_ids
                for result in report.results
            },
            {(EVIDENCE_ID,)},
        )

    def test_release_and_authorization_violations_are_hard_failures(self):
        class LeakingTarget:
            async def execute(self, _request):
                return EvaluationTargetResult(
                    status="answered",
                    answer="The pressure is 525 bar.",
                    release_id=OTHER_RELEASE_ID,
                    evidence=(
                        {
                            "evidence_id": "not-a-uuid",
                            "source_document_id": OTHER_SOURCE_ID,
                            "tenant_id": TENANT_ID,
                            "knowledge_base_id": KB_ID,
                            "release_id": OTHER_RELEASE_ID,
                        },
                    ),
                    validation={"valid": False, "violations": []},
                )

        payload = manifest_payload(cases=[manifest_payload()["queries"][0]])
        manifest = parse_evaluation_manifest(
            payload,
            expected_split="development",
        )
        store = RecordingStore()
        report = asyncio.run(
            EvaluationRunner(
                target=LeakingTarget(),
                store=store,
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-abc",
            )
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            set(report.hard_failures),
            {
                "authorization_leakage",
                "invalid_answer_accepted",
                "malformed_evidence_id",
                "release_mismatch",
                "required_evidence_missing",
            },
        )
        self.assertEqual(len(store.completed), 1)
        self.assertEqual(store.failed, [])

    def test_target_exception_becomes_persisted_case_failure(self):
        class BrokenTarget:
            async def execute(self, _request):
                raise TimeoutError("private API timed out")

        payload = manifest_payload(cases=[manifest_payload()["queries"][1]])
        manifest = parse_evaluation_manifest(
            payload,
            expected_split="development",
        )
        store = RecordingStore()
        report = asyncio.run(
            EvaluationRunner(
                target=BrokenTarget(),
                store=store,
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-abc",
            )
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.hard_failures, ("target_error",))
        self.assertIn(
            "TimeoutError",
            report.results[0].details["failure_details"]["target_error"],
        )
        self.assertEqual(len(store.completed), 1)

    def test_internal_store_failure_marks_running_run_failed(self):
        class FailingStore(RecordingStore):
            def complete_run(self, report):
                super().complete_run(report)
                raise RuntimeError("database unavailable")

        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        store = FailingStore()
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            asyncio.run(
                EvaluationRunner(
                    target=PassingTarget(),
                    store=store,
                ).run(
                    manifest,
                    release_id=RELEASE_ID,
                    code_version="commit-abc",
                )
            )
        self.assertEqual(len(store.failed), 1)


class HttpTargetTests(unittest.TestCase):
    def test_http_target_calls_retrieval_route_and_normalizes_response(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "release_id": RELEASE_ID,
                    "results": [
                        {
                            "evidence_id": EVIDENCE_ID,
                            "source_document_id": SOURCE_ID,
                        }
                    ],
                }

        class Client:
            def __init__(self):
                self.calls = []

            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        client = Client()
        target = HttpEvaluationTarget(
            api_url="https://private-v3.example/",
            authorization_token=lambda: "rotated-token",
            client=client,
        )
        result = asyncio.run(
            target.execute(
                EvaluationRequest(
                    case_key="query",
                    mode="query",
                    question="K 28 pressure",
                    mandatory_constraints={},
                    forbidden_claim_values=(),
                    top_k=3,
                )
            )
        )
        self.assertEqual(result.status, "retrieved")
        self.assertEqual(result.release_id, RELEASE_ID)
        self.assertEqual(result.evidence[0]["evidence_id"], EVIDENCE_ID)
        url, request = client.calls[0]
        self.assertEqual(url, "https://private-v3.example/v3/query")
        self.assertEqual(request["json"], {"query": "K 28 pressure", "top_k": 3})
        self.assertEqual(
            request["headers"]["authorization"],
            "Bearer rotated-token",
        )


if __name__ == "__main__":
    unittest.main()
