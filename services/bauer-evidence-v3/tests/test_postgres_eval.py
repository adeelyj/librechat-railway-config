from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.evaluation import (  # noqa: E402
    EvaluationRunner,
    EvaluationTargetResult,
    parse_evaluation_manifest,
)
from bauer_evidence_v3.postgres_eval import (  # noqa: E402
    EvaluationObservationScopeError,
    ImmutableEvaluationConflictError,
    PostgresEvaluationObservationSource,
    PostgresEvaluationStore,
)


TENANT_ID = "10000000-0000-4000-8000-000000000001"
KB_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_ID = "30000000-0000-4000-8000-000000000001"
SOURCE_ID = "40000000-0000-4000-8000-000000000001"
EVIDENCE_ID = "50000000-0000-4000-8000-000000000001"
PRINCIPAL_ID = "60000000-0000-4000-8000-000000000001"
SOURCE_VERSION_ID = "41000000-0000-4000-8000-000000000001"
ARTIFACT_SET_ID = "42000000-0000-4000-8000-000000000001"
PHYSICAL_TABLE_ID = "43000000-0000-4000-8000-000000000001"
PHYSICAL_CELL_ID = "44000000-0000-4000-8000-000000000001"
PHYSICAL_FACT_ID = "45000000-0000-4000-8000-000000000001"
PROVENANCE_ID = "46000000-0000-4000-8000-000000000001"
PAGE_ID = "47000000-0000-4000-8000-000000000001"
TABLE_ID = "table_pressure"
CELL_ID = "cell_pressure"
FACT_ID = "fact_pressure"
SOURCE_SHA256 = "a" * 64


def manifest_payload():
    return {
        "schema_version": 1,
        "suite_key": "bauer-v3-dev",
        "suite_version": "2026-07-25.1",
        "split": "development",
        "gold_status": "interim_reviewed",
        "authorization_scope": {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": KB_ID,
            "allowed_release_ids": [RELEASE_ID],
            "allowed_source_ids": [SOURCE_ID],
        },
        "metadata": {"owner": "interim-reviewer"},
        "queries": [
            {
                "case_id": "query-pressure",
                "category": "retrieval",
                "prompt": {
                    "mode": "query",
                    "query": "K 28 pressure",
                    "top_k": 5,
                },
                "retrieval": {
                    "required_evidence_ids": [EVIDENCE_ID],
                    "exact_lookup": True,
                },
                "hard_failure_codes": ["required_evidence_missing"],
            }
        ],
    }


def structured_manifest_payload():
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
    return payload


@dataclass(frozen=True)
class FakeJsonb:
    value: object


class FakeCursor:
    def __init__(self, *, row=None, rows=None, rowcount=1):
        self._row = row
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(
        self,
        manifest,
        *,
        conflicting_suite=False,
        release_status="validating",
    ):
        self.manifest = manifest
        self.conflicting_suite = conflicting_suite
        self.release_status = release_status
        self.calls = []
        self.transactions = 0
        self.run_status = "running"

    def transaction(self):
        self.transactions += 1
        return nullcontext()

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.calls.append((normalized, params))
        if "FROM bauer_rag_v3.eval_suites" in normalized:
            return FakeCursor(
                row={
                    "eval_suite_id": self.manifest.eval_suite_id,
                    "tenant_id": TENANT_ID,
                    "suite_key": self.manifest.suite_key,
                    "suite_version": self.manifest.suite_version,
                    "manifest_sha256": (
                        "f" * 64
                        if self.conflicting_suite
                        else self.manifest.manifest_sha256
                    ),
                    "gold_status": self.manifest.gold_status,
                    "metadata": {
                        **self.manifest.metadata,
                        "split": "development",
                    },
                    "verified_at": None,
                    "verified_by": None,
                }
            )
        if (
            "FROM bauer_rag_v3.eval_cases" in normalized
            and "WHERE eval_case_id" in normalized
        ):
            case = self.manifest.cases[0]
            return FakeCursor(
                row={
                    "eval_case_id": case.eval_case_id,
                    "eval_suite_id": self.manifest.eval_suite_id,
                    "case_key": case.case_key,
                    "split": case.split,
                    "category": case.category,
                    "prompt": case.prompt_payload,
                    "expected": case.expected_payload,
                    "hard_failure_codes": list(case.hard_failure_codes),
                    "metadata": dict(case.metadata),
                }
            )
        if (
            "FROM bauer_rag_v3.eval_cases" in normalized
            and "ORDER BY case_key" in normalized
        ):
            if not self.manifest.cases:
                return FakeCursor(rows=[])
            case = self.manifest.cases[0]
            return FakeCursor(
                rows=[
                    {
                        "eval_case_id": case.eval_case_id,
                        "case_key": case.case_key,
                    }
                ]
            )
        if (
            "FROM bauer_rag_v3.eval_cases" in normalized
            and "eval_case_id = ANY" in normalized
        ):
            return FakeCursor(
                rows=[
                    {
                        "eval_case_id": self.manifest.cases[0].eval_case_id,
                    }
                ]
            )
        if "FROM bauer_rag_v3.knowledge_releases" in normalized:
            return FakeCursor(
                row={
                    "release_id": RELEASE_ID,
                    "kb_id": KB_ID,
                    "tenant_id": TENANT_ID,
                    "status": self.release_status,
                }
            )
        if (
            "FROM bauer_rag_v3.eval_runs" in normalized
            and "FOR UPDATE" in normalized
        ):
            return FakeCursor(
                row={
                    "status": self.run_status,
                    "tenant_id": TENANT_ID,
                    "kb_id": KB_ID,
                    "release_id": RELEASE_ID,
                    "eval_suite_id": self.manifest.eval_suite_id,
                    "repetitions": 1,
                }
            )
        if (
            normalized.startswith("UPDATE bauer_rag_v3.eval_runs")
            and "status = 'succeeded'" in normalized
        ):
            self.run_status = "succeeded"
            return FakeCursor(rowcount=1)
        return FakeCursor(rowcount=1)


class PassingTarget:
    async def execute(self, _request):
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


class ObservationConnection:
    def __init__(self, *, visible_sources=(SOURCE_ID,)):
        self.visible_sources = visible_sources
        self.calls = []
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return nullcontext()

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.calls.append((normalized, params))
        if (
            "FROM bauer_rag_v3.knowledge_releases AS release_row"
            in normalized
        ):
            return FakeCursor(
                row={
                    "release_id": RELEASE_ID,
                    "knowledge_base_id": KB_ID,
                    "tenant_id": TENANT_ID,
                    "status": "validating",
                }
            )
        if (
            "SELECT member.source_id::text AS source_id"
            in normalized
        ):
            return FakeCursor(
                rows=[
                    {"source_id": source_id}
                    for source_id in self.visible_sources
                ]
            )
        if "artifact.quality_summary" in normalized:
            return FakeCursor(
                row={
                    "tenant_id": TENANT_ID,
                    "knowledge_base_id": KB_ID,
                    "release_id": RELEASE_ID,
                    "source_id": SOURCE_ID,
                    "source_version_id": SOURCE_VERSION_ID,
                    "artifact_set_id": ARTIFACT_SET_ID,
                    "source_sha256": SOURCE_SHA256,
                    "artifact_status": "valid",
                    "quality_summary": {"quality_issues": []},
                    "page_count": 1,
                    "block_count": 1,
                    "cell_count": 1,
                    "evidence_count": 2,
                    "resolvable_evidence_count": 2,
                    "reading_order_checks": 1,
                    "reading_order_passes": 1,
                }
            )
        if "AS physical_fact_id" in normalized:
            return FakeCursor(
                rows=[
                    {
                        "physical_fact_id": PHYSICAL_FACT_ID,
                        "canonical_fact_id": FACT_ID,
                        "value_kind": "numeric",
                        "raw_value": "525",
                        "value_text": None,
                        "numeric_value": "525",
                        "date_value": None,
                        "boolean_value": None,
                        "unit_raw": "bar",
                        "unit_ucum": "bar",
                        "provenance_id": PROVENANCE_ID,
                        "page_id": PAGE_ID,
                    }
                ]
            )
        if "AS canonical_table_id" in normalized:
            return FakeCursor(
                rows=[
                    {
                        "tenant_id": TENANT_ID,
                        "knowledge_base_id": KB_ID,
                        "release_id": RELEASE_ID,
                        "source_id": SOURCE_ID,
                        "source_version_id": SOURCE_VERSION_ID,
                        "artifact_set_id": ARTIFACT_SET_ID,
                        "source_sha256": SOURCE_SHA256,
                        "physical_table_id": PHYSICAL_TABLE_ID,
                        "canonical_table_id": TABLE_ID,
                        "row_count": 1,
                        "column_count": 1,
                    }
                ]
            )
        if "AS physical_cell_id" in normalized:
            return FakeCursor(
                rows=[
                    {
                        "physical_cell_id": PHYSICAL_CELL_ID,
                        "canonical_cell_id": CELL_ID,
                        "page_id": PAGE_ID,
                        "page_number": 1,
                        "row_index": 0,
                        "column_index": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "raw_text": "525",
                        "numeric_value": "525",
                        "unit_raw": "bar",
                        "unit_ucum": "bar",
                        "x0": 0.1,
                        "y0": 0.2,
                        "x1": 0.3,
                        "y1": 0.4,
                    }
                ]
            )
        return FakeCursor()


class PostgresEvaluationStoreTests(unittest.TestCase):
    def make_store(self, connection):
        return PostgresEvaluationStore(
            lambda: nullcontext(connection),
            tenant_id=TENANT_ID,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )

    def test_full_runner_lifecycle_sets_rls_and_persists_results_atomically(self):
        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        connection = FakeConnection(manifest)
        store = self.make_store(connection)
        report = asyncio.run(
            EvaluationRunner(
                target=PassingTarget(),
                store=store,
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-abc",
            )
        )
        self.assertTrue(report.passed)
        self.assertEqual(connection.run_status, "succeeded")
        sql = [statement for statement, _params in connection.calls]
        self.assertTrue(
            any(
                "INSERT INTO bauer_rag_v3.eval_suites" in statement
                for statement in sql
            )
        )
        self.assertTrue(
            any(
                "INSERT INTO bauer_rag_v3.eval_cases" in statement
                for statement in sql
            )
        )
        self.assertTrue(
            any(
                "INSERT INTO bauer_rag_v3.eval_runs" in statement
                for statement in sql
            )
        )
        self.assertTrue(
            any(
                "INSERT INTO bauer_rag_v3.eval_results" in statement
                for statement in sql
            )
        )
        tenant_context_count = sum(
            "set_config('app.tenant_id'" in statement for statement in sql
        )
        principal_context_count = sum(
            "set_config('app.principal_ids'" in statement for statement in sql
        )
        self.assertEqual(tenant_context_count, 3)
        self.assertEqual(principal_context_count, 3)
        self.assertEqual(connection.transactions, 3)

        result_insert = next(
            params
            for statement, params in connection.calls
            if "INSERT INTO bauer_rag_v3.eval_results" in statement
        )
        self.assertEqual(result_insert[7], [EVIDENCE_ID])
        self.assertIsInstance(result_insert[5], FakeJsonb)
        self.assertIsInstance(result_insert[9], FakeJsonb)

    def test_existing_suite_content_is_verified_not_overwritten(self):
        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        connection = FakeConnection(manifest, conflicting_suite=True)
        store = self.make_store(connection)
        with self.assertRaisesRegex(
            ImmutableEvaluationConflictError,
            "suite UUID has conflicting content",
        ):
            store.prepare_suite(manifest)
        self.assertFalse(
            any(
                statement.startswith("UPDATE bauer_rag_v3.eval_suites")
                for statement, _params in connection.calls
            )
        )

    def test_ready_release_accepts_post_ready_http_shadow_evaluation(self):
        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        connection = FakeConnection(manifest, release_status="ready")
        report = asyncio.run(
            EvaluationRunner(
                target=PassingTarget(),
                store=self.make_store(connection),
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-ready-shadow",
            )
        )

        self.assertTrue(report.passed)
        self.assertEqual(connection.run_status, "succeeded")

    def test_store_requires_non_empty_evaluation_principal_context(self):
        manifest = parse_evaluation_manifest(
            manifest_payload(),
            expected_split="development",
        )
        with self.assertRaisesRegex(ValueError, "evaluation principal"):
            PostgresEvaluationStore(
                lambda: nullcontext(FakeConnection(manifest)),
                tenant_id=TENANT_ID,
                principal_ids=(),
                jsonb_factory=FakeJsonb,
            )


class PostgresEvaluationObservationSourceTests(unittest.TestCase):
    def make_source(self, connection):
        return PostgresEvaluationObservationSource(
            lambda: nullcontext(connection),
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            enforce_least_privilege=False,
        )

    def test_capture_is_read_only_release_and_source_pinned(self):
        manifest = parse_evaluation_manifest(
            structured_manifest_payload(),
            expected_split="development",
        )
        connection = ObservationConnection()
        observations = self.make_source(connection).capture(
            manifest=manifest,
            release_id=RELEASE_ID,
        )

        self.assertEqual(connection.transactions, 1)
        self.assertTrue(
            connection.calls[0][0].startswith(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
        )
        statements = [statement for statement, _ in connection.calls]
        self.assertFalse(
            any(
                statement.startswith(("INSERT ", "UPDATE ", "DELETE "))
                for statement in statements
            )
        )
        self.assertTrue(
            all(
                "bauer_rag_v3.can_read_release" in statement
                for statement in statements
                if "FROM bauer_rag_v3." in statement
                and "knowledge_bases" not in statement
            )
        )
        extraction = observations.extraction[0]
        self.assertEqual(extraction["source_id"], SOURCE_ID)
        self.assertEqual(extraction["source_version_id"], SOURCE_VERSION_ID)
        extraction_statement = next(
            statement
            for statement, _ in connection.calls
            if "artifact.quality_summary" in statement
        )
        self.assertIn(
            "JOIN bauer_rag_v3.table_segments AS segment_row",
            extraction_statement,
        )
        self.assertIn(
            "table_row.metadata ->> 'canonical_order'",
            extraction_statement,
        )
        self.assertIn(
            "table_row.metadata ->> 'parser_id' = 'html_source'",
            extraction_statement,
        )
        self.assertIn("UNION ALL", extraction_statement)
        self.assertEqual(extraction["facts"][0]["fact_id"], FACT_ID)
        self.assertTrue(
            extraction["facts"][0]["source_coordinate_resolvable"]
        )
        table = observations.tables[0]
        self.assertEqual(table["table_id"], TABLE_ID)
        self.assertEqual(table["cells"][0]["cell_id"], CELL_ID)
        self.assertTrue(
            table["cells"][0]["source_coordinate_resolvable"]
        )
        source_context = next(
            params
            for statement, params in connection.calls
            if "set_config('app.authorized_source_ids'" in statement
        )
        self.assertIn(SOURCE_ID, source_context[0])

    def test_runner_persists_a_full_structured_capture_lifecycle(self):
        manifest = parse_evaluation_manifest(
            structured_manifest_payload(),
            expected_split="development",
        )
        store_connection = FakeConnection(manifest)
        store = PostgresEvaluationStore(
            lambda: nullcontext(store_connection),
            tenant_id=TENANT_ID,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )
        report = asyncio.run(
            EvaluationRunner(
                target=PassingTarget(),
                store=store,
                observation_source=self.make_source(
                    ObservationConnection()
                ),
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-structured-postgres",
            )
        )

        self.assertTrue(report.passed)
        self.assertEqual(store_connection.run_status, "succeeded")
        self.assertTrue(report.aggregate_metrics["extraction_qa"]["passed"])
        self.assertTrue(report.aggregate_metrics["table_integrity"]["passed"])
        finalized = next(
            params
            for statement, params in store_connection.calls
            if statement.startswith("UPDATE bauer_rag_v3.eval_runs")
            and "status = 'succeeded'" in statement
        )
        self.assertTrue(finalized[0])
        self.assertIsInstance(finalized[1], FakeJsonb)

    def test_structured_only_capture_persists_without_fake_query_cases(self):
        payload = structured_manifest_payload()
        payload["queries"] = []
        manifest = parse_evaluation_manifest(
            payload,
            expected_split="development",
        )
        store_connection = FakeConnection(manifest)
        report = asyncio.run(
            EvaluationRunner(
                target=None,
                store=PostgresEvaluationStore(
                    lambda: nullcontext(store_connection),
                    tenant_id=TENANT_ID,
                    principal_ids=(PRINCIPAL_ID,),
                    jsonb_factory=FakeJsonb,
                ),
                observation_source=self.make_source(
                    ObservationConnection()
                ),
            ).run(
                manifest,
                release_id=RELEASE_ID,
                code_version="commit-canonical-only-postgres",
            )
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.results, ())
        self.assertEqual(store_connection.run_status, "succeeded")
        self.assertFalse(
            any(
                "INSERT INTO bauer_rag_v3.eval_results" in statement
                for statement, _ in store_connection.calls
            )
        )

    def test_capture_fails_closed_when_target_source_is_not_visible(self):
        manifest = parse_evaluation_manifest(
            structured_manifest_payload(),
            expected_split="development",
        )
        with self.assertRaisesRegex(
            EvaluationObservationScopeError,
            "absent or unauthorized",
        ):
            self.make_source(
                ObservationConnection(visible_sources=())
            ).capture(
                manifest=manifest,
                release_id=RELEASE_ID,
            )

    def test_capture_rejects_a_write_capable_reader_login(self):
        class Connection:
            def execute(self, _sql, _params=()):
                return FakeCursor(
                    row={
                        "has_ingester_membership": True,
                        "has_evaluator_membership": False,
                        "has_admin_membership": False,
                    }
                )

        with (
            patch(
                "bauer_evidence_v3.postgres_eval."
                "verify_runtime_database_role",
                return_value="overprivileged-login",
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "reader-only database login",
            ),
        ):
            PostgresEvaluationObservationSource._verify_reader_only_role(
                Connection()
            )


if __name__ == "__main__":
    unittest.main()
