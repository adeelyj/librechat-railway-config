from __future__ import annotations

import ast
import sys
import unittest
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.postgres_admin import (  # noqa: E402
    CompileJobSpec,
    ImmutableReleaseConflictError,
    KnowledgeBaseGrant,
    PostgresReleaseAdmin,
    PrincipalBootstrap,
    ReleaseGateError,
    ReleaseSpec,
    ReleaseTransitionError,
)


TENANT_ID = "10000000-0000-0000-0000-000000000001"
KB_ID = "20000000-0000-0000-0000-000000000001"
ADMIN_ID = "30000000-0000-0000-0000-000000000001"
READER_ID = "30000000-0000-0000-0000-000000000002"
RELEASE_ID = "40000000-0000-0000-0000-000000000001"
PREVIOUS_RELEASE_ID = "40000000-0000-0000-0000-000000000002"
SOURCE_ID = "50000000-0000-0000-0000-000000000001"
SOURCE_VERSION_ID = "60000000-0000-0000-0000-000000000001"
JOB_ID = "70000000-0000-0000-0000-000000000001"
MANIFEST_SHA = "a" * 64
COMPILER_SHA = "b" * 64


@dataclass(frozen=True)
class FakeJsonb:
    value: object


class FakeCursor:
    def __init__(self, *, row=None, rows=None, rowcount=1):
        self._row = row
        self._rows = [] if rows is None else rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, handler=None):
        self.handler = handler or (lambda _sql, _params: FakeCursor())
        self.calls = []
        self.transactions = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        return self.handler(normalized, params)

    def transaction(self):
        self.transactions += 1
        return nullcontext()


class FakeJobQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "job_id": JOB_ID,
            **kwargs,
        }

    def replay_dead_letter(self, job_id, *, idempotency_key, actor):
        self.calls.append(
            {
                "operation": "replay_dead_letter",
                "job_id": job_id,
                "idempotency_key": idempotency_key,
                "actor": actor,
            }
        )
        return {
            "job_id": "70000000-0000-0000-0000-000000000002",
            "replay_of_job_id": job_id,
            "idempotency_key": idempotency_key,
        }


def release_row(status="draft", **overrides):
    row = {
        "release_id": RELEASE_ID,
        "kb_id": KB_ID,
        "status": status,
        "based_on_release_id": None,
        "manifest_sha256": MANIFEST_SHA,
        "expected_source_count": 1,
        "compiler_fingerprint": COMPILER_SHA,
        "parser_version": "parser-v3",
        "ocr_version": "ocr-v1",
        "fact_model_version": "facts-v2",
        "embedding_model_version": "embedding-v1",
        "prompt_version": "prompt-v4",
        "metadata": {"corpus": "bauer"},
        "created_by": "test-admin",
    }
    row.update(overrides)
    return row


def release_spec(**overrides):
    values = {
        "release_id": RELEASE_ID,
        "kb_id": KB_ID,
        "manifest_sha256": MANIFEST_SHA,
        "expected_source_count": 1,
        "compiler_fingerprint": COMPILER_SHA,
        "parser_version": "parser-v3",
        "ocr_version": "ocr-v1",
        "fact_model_version": "facts-v2",
        "embedding_model_version": "embedding-v1",
        "prompt_version": "prompt-v4",
        "metadata": {"corpus": "bauer"},
        "created_by": "test-admin",
    }
    values.update(overrides)
    return ReleaseSpec(**values)


def make_admin(
    connection,
    *,
    job_queue=None,
    enforce_database_role=False,
):
    return PostgresReleaseAdmin(
        lambda: nullcontext(connection),
        tenant_id=TENANT_ID,
        principal_ids=(ADMIN_ID,),
        jsonb_factory=FakeJsonb,
        job_queue=job_queue,
        enforce_database_role=enforce_database_role,
    )


class StatefulReleaseHandler:
    def __init__(
        self,
        *,
        status,
        evidence=None,
        validation=None,
    ):
        self.status = status
        self.evidence = evidence or {
            "expected_source_count": 1,
            "actual_source_count": 1,
            "unfinished_job_count": 0,
            "invalid_artifact_count": 0,
            "missing_citable_source_count": 0,
        }
        self.validation = validation or {
            "unresolved_blocker_count": 0,
            "passing_verified_eval_count": 1,
        }

    def __call__(self, sql, params):
        if "SELECT to_jsonb(release_row)" in sql and "FOR UPDATE" in sql:
            return FakeCursor(row=(release_row(self.status),))
        if (
            "SELECT jsonb_build_object(" in sql
            and "'expected_source_count'" in sql
        ):
            return FakeCursor(row=(self.evidence,))
        if (
            "SELECT jsonb_build_object(" in sql
            and "'unresolved_blocker_count'" in sql
        ):
            return FakeCursor(row=(self.validation,))
        if "UPDATE bauer_rag_v3.knowledge_releases" in sql:
            self.status = params[0]
            return FakeCursor(row=(release_row(self.status),))
        return FakeCursor()


class PostgresReleaseAdminTests(unittest.TestCase):
    def test_normal_release_connections_require_exact_admin_tier(self):
        connection = FakeConnection()
        instance = make_admin(
            connection,
            enforce_database_role=True,
        )
        with patch(
            "bauer_evidence_v3.postgres_admin.verify_runtime_database_role",
            return_value="bauer_v3_admin_login",
        ) as verify:
            with instance._connection() as opened:
                self.assertIs(opened, connection)
        verify.assert_called_once_with(
            connection,
            required_group_role="bauer_rag_v3_admin",
        )

    def test_psycopg_import_is_lazy(self):
        module_path = (
            SERVICE_ROOT / "bauer_evidence_v3" / "postgres_admin.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (
                    isinstance(node, ast.Import)
                    and any(
                        alias.name.startswith("psycopg")
                        for alias in node.names
                    )
                )
                or (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").startswith("psycopg")
                )
            )
        ]
        self.assertEqual(imports, [])

    def test_bootstrap_is_one_idempotent_parameterized_transaction(self):
        connection = FakeConnection()
        instance = make_admin(connection)
        result = instance.bootstrap_tenant_knowledge_base(
            external_key="bauer",
            tenant_display_name="Bauer",
            kb_id=KB_ID,
            external_namespace="bauer-manuals",
            kb_name="Bauer manuals",
            principals=(
                PrincipalBootstrap(
                    ADMIN_ID,
                    "service",
                    "bauer-v3-admin",
                    "Bauer V3 admin",
                ),
                PrincipalBootstrap(
                    READER_ID,
                    "group",
                    "bauer-readers",
                ),
            ),
            grants=(
                KnowledgeBaseGrant(ADMIN_ID, "admin", "bootstrap"),
                KnowledgeBaseGrant(READER_ID, "read", "bootstrap"),
            ),
            tenant_metadata={"region": "eu"},
        )

        self.assertEqual(result.tenant_id, TENANT_ID)
        self.assertEqual(result.kb_id, KB_ID)
        self.assertEqual(connection.transactions, 1)
        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("ON CONFLICT (tenant_id) DO UPDATE", sql)
        self.assertIn("ON CONFLICT (kb_id) DO UPDATE", sql)
        self.assertIn("ON CONFLICT (principal_id) DO UPDATE", sql)
        self.assertIn(
            "ON CONFLICT (kb_id, principal_id, permission) DO UPDATE",
            sql,
        )
        self.assertNotIn("bauer-v3-admin", sql)
        self.assertTrue(
            any("set_config('app.tenant_id'" in call[0] for call in connection.calls)
        )
        json_values = [
            value
            for _sql, params in connection.calls
            for value in params
            if isinstance(value, FakeJsonb)
        ]
        self.assertEqual(json_values[0].value, {"region": "eu"})

    def test_bootstrap_rejects_admin_not_in_context_before_database_access(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "context principal"):
            make_admin(connection).bootstrap_tenant_knowledge_base(
                external_key="bauer",
                tenant_display_name="Bauer",
                kb_id=KB_ID,
                external_namespace="bauer",
                kb_name="Bauer",
                principals=(
                    PrincipalBootstrap(
                        READER_ID,
                        "service",
                        "different-admin",
                    ),
                ),
                grants=(KnowledgeBaseGrant(READER_ID, "admin"),),
            )
        self.assertEqual(connection.calls, [])

    def test_create_release_is_insert_only_and_idempotent(self):
        inserted = release_row()

        def insert_handler(sql, _params):
            if "INSERT INTO bauer_rag_v3.knowledge_releases" in sql:
                return FakeCursor(row=(inserted,))
            return FakeCursor()

        connection = FakeConnection(insert_handler)
        created = make_admin(connection).create_release(release_spec())
        self.assertEqual(created["release_id"], RELEASE_ID)
        release_insert = next(
            sql
            for sql, _params in connection.calls
            if "INSERT INTO bauer_rag_v3.knowledge_releases" in sql
        )
        self.assertIn("ON CONFLICT (release_id) DO NOTHING", release_insert)
        self.assertNotIn("DO UPDATE", release_insert)

        def existing_handler(sql, _params):
            if "INSERT INTO bauer_rag_v3.knowledge_releases" in sql:
                return FakeCursor(row=None)
            if "FOR SHARE" in sql:
                return FakeCursor(row=(release_row(status="ready"),))
            return FakeCursor()

        existing = make_admin(
            FakeConnection(existing_handler)
        ).create_release(release_spec())
        self.assertEqual(existing["status"], "ready")

    def test_create_release_rejects_immutable_semantic_drift(self):
        def handler(sql, _params):
            if "INSERT INTO bauer_rag_v3.knowledge_releases" in sql:
                return FakeCursor(row=None)
            if "FOR SHARE" in sql:
                return FakeCursor(
                    row=(release_row(embedding_model_version="other-model"),)
                )
            return FakeCursor()

        with self.assertRaisesRegex(
            ImmutableReleaseConflictError,
            "immutable specification",
        ):
            make_admin(FakeConnection(handler)).create_release(release_spec())

    def test_start_build_enqueues_release_scoped_compile_job_without_activation(self):
        handler = StatefulReleaseHandler(status="draft")
        connection = FakeConnection(handler)
        job_queue = FakeJobQueue()
        instance = make_admin(connection, job_queue=job_queue)
        request = CompileJobSpec(
            source_id=SOURCE_ID,
            source_version_id=SOURCE_VERSION_ID,
            external_file_id="manual-1",
            source_name="manual.pdf",
            source_object_key="sha256/aa/manual.pdf",
            source_type="pdf",
            declared_media_type="application/pdf",
            category_path=("manuals", "technical"),
            priority=10,
        )

        result = instance.start_build(
            RELEASE_ID,
            compile_jobs=(request,),
        )

        self.assertEqual(result.release["status"], "building")
        self.assertEqual(len(result.jobs), 1)
        queued = job_queue.calls[0]
        self.assertEqual(queued["job_type"], "compile")
        self.assertEqual(queued["release_id"], RELEASE_ID)
        self.assertIsNone(queued["source_id"])
        self.assertIsNone(queued["source_version_id"])
        self.assertEqual(queued["payload"]["source_id"], SOURCE_ID)
        self.assertEqual(
            queued["payload"]["source_version_id"],
            SOURCE_VERSION_ID,
        )
        self.assertEqual(
            queued["payload"]["compiler_fingerprint"],
            COMPILER_SHA,
        )
        self.assertEqual(queued["payload"]["ocr_version"], "ocr-v1")
        self.assertEqual(
            queued["payload"]["fact_model_version"],
            "facts-v2",
        )
        self.assertIn(SOURCE_VERSION_ID, queued["idempotency_key"])
        self.assertFalse(
            any(
                "activate_release" in sql
                for sql, _params in connection.calls
            )
        )

    def test_default_job_queue_inherits_transaction_local_rls_context(self):
        def handler(sql, params):
            if "SELECT to_jsonb(release_row)" in sql and "FOR UPDATE" in sql:
                return FakeCursor(row=(release_row("building"),))
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql:
                return FakeCursor(
                    row=(
                        {
                            "job_id": params[0],
                            "queue_name": params[1],
                            "job_type": params[2],
                        },
                    )
                )
            return FakeCursor()

        connection = FakeConnection(handler)
        request = CompileJobSpec(
            source_id=SOURCE_ID,
            source_version_id=SOURCE_VERSION_ID,
            external_file_id="manual-1",
            source_name="manual.pdf",
            source_object_key="sha256/aa/manual.pdf",
            source_type="pdf",
        )
        jobs = make_admin(connection).enqueue_compile_jobs(
            RELEASE_ID,
            (request,),
        )

        self.assertEqual(len(jobs), 1)
        job_insert_index = next(
            index
            for index, (sql, _params) in enumerate(connection.calls)
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql
        )
        preceding_sql = [
            sql
            for sql, _params in connection.calls[:job_insert_index]
        ]
        self.assertIn(
            "SELECT set_config('app.tenant_id', %s, true)",
            preceding_sql,
        )
        self.assertIn(
            "SELECT set_config('app.principal_ids', %s, true)",
            preceding_sql,
        )

    def test_begin_validation_uses_database_evidence_gate(self):
        failing_handler = StatefulReleaseHandler(
            status="building",
            evidence={
                "expected_source_count": 2,
                "actual_source_count": 1,
                "unfinished_job_count": 1,
                "invalid_artifact_count": 1,
                "missing_citable_source_count": 1,
            },
        )
        failing_connection = FakeConnection(failing_handler)
        with self.assertRaises(ReleaseGateError) as raised:
            make_admin(failing_connection).begin_validation(RELEASE_ID)
        self.assertEqual(raised.exception.target_status, "validating")
        self.assertEqual(len(raised.exception.failures), 4)
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.knowledge_releases" in sql
                for sql, _params in failing_connection.calls
            )
        )

        passing_handler = StatefulReleaseHandler(status="building")
        result = make_admin(
            FakeConnection(passing_handler)
        ).begin_validation(RELEASE_ID)
        self.assertEqual(result["status"], "validating")

    def test_mark_ready_requires_clean_qa_and_passing_verified_eval(self):
        failing_handler = StatefulReleaseHandler(
            status="validating",
            validation={
                "unresolved_blocker_count": 2,
                "passing_verified_eval_count": 0,
            },
        )
        failing_connection = FakeConnection(failing_handler)
        with self.assertRaises(ReleaseGateError) as raised:
            make_admin(failing_connection).mark_ready(RELEASE_ID)
        self.assertEqual(
            raised.exception.failures,
            (
                "unresolved blocking QA checks",
                "no passing evaluation against verified gold",
            ),
        )
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.knowledge_releases" in sql
                for sql, _params in failing_connection.calls
            )
        )

        passing_handler = StatefulReleaseHandler(status="validating")
        passing_connection = FakeConnection(passing_handler)
        result = make_admin(passing_connection).mark_ready(RELEASE_ID)
        self.assertEqual(result["status"], "ready")
        validation_sql = next(
            sql
            for sql, _params in passing_connection.calls
            if "'passing_verified_eval_count'" in sql
        )
        self.assertIn("'independent_bauer_verified'", validation_sql)
        self.assertNotIn("'interim_reviewed'", validation_sql)

    def test_mark_failed_records_terminal_error_from_build_states(self):
        for initial_status in ("draft", "building", "validating"):
            with self.subTest(initial_status=initial_status):
                handler = StatefulReleaseHandler(status=initial_status)
                connection = FakeConnection(handler)
                failed = make_admin(connection).mark_failed(
                    RELEASE_ID,
                    error="compiler input is irrecoverably inconsistent",
                )

                self.assertEqual(failed["status"], "failed")
                update_sql, update_params = next(
                    (sql, params)
                    for sql, params in connection.calls
                    if "UPDATE bauer_rag_v3.knowledge_releases" in sql
                )
                self.assertIn(
                    "failed_at = COALESCE(failed_at, now())",
                    update_sql,
                )
                self.assertIn("error = %s", update_sql)
                self.assertEqual(
                    update_params,
                    (
                        "failed",
                        "compiler input is irrecoverably inconsistent",
                        RELEASE_ID,
                        initial_status,
                    ),
                )
                self.assertFalse(
                    any(
                        "UPDATE bauer_rag_v3.active_releases" in sql
                        for sql, _params in connection.calls
                    )
                )
                pointer_read_sql = next(
                    sql
                    for sql, _params in connection.calls
                    if "FROM bauer_rag_v3.active_releases AS active" in sql
                )
                self.assertNotIn("FOR SHARE", pointer_read_sql)

    def test_dead_letter_replay_is_scoped_to_a_context_actor(self):
        job_queue = FakeJobQueue()
        instance = make_admin(FakeConnection(), job_queue=job_queue)

        replay = instance.replay_dead_letter(
            JOB_ID,
            idempotency_key="replay:compile:attempt-2",
            actor_principal_id=ADMIN_ID,
        )

        self.assertEqual(replay["replay_of_job_id"], JOB_ID)
        self.assertEqual(
            job_queue.calls,
            [
                {
                    "operation": "replay_dead_letter",
                    "job_id": JOB_ID,
                    "idempotency_key": "replay:compile:attempt-2",
                    "actor": ADMIN_ID,
                }
            ],
        )
        with self.assertRaisesRegex(ValueError, "database context"):
            instance.replay_dead_letter(
                JOB_ID,
                idempotency_key="replay:unauthorized",
                actor_principal_id=READER_ID,
            )

    def test_mark_failed_is_idempotent_and_rejects_ready_release(self):
        failed_connection = FakeConnection(
            StatefulReleaseHandler(status="failed")
        )
        failed = make_admin(failed_connection).mark_failed(
            RELEASE_ID,
            error="same incident retry",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.knowledge_releases" in sql
                for sql, _params in failed_connection.calls
            )
        )

        ready_connection = FakeConnection(
            StatefulReleaseHandler(status="ready")
        )
        with self.assertRaisesRegex(
            ReleaseTransitionError,
            "draft, building, or validating",
        ):
            make_admin(ready_connection).mark_failed(
                RELEASE_ID,
                error="not a valid ready transition",
            )

    def test_failure_and_retirement_reject_active_release(self):
        class ActiveReleaseHandler(StatefulReleaseHandler):
            def __call__(self, sql, params):
                if "FROM bauer_rag_v3.active_releases AS active" in sql:
                    return FakeCursor(row=(1,))
                return super().__call__(sql, params)

        for operation, status in (
            ("failure", "building"),
            ("retirement", "ready"),
        ):
            with self.subTest(operation=operation):
                connection = FakeConnection(
                    ActiveReleaseHandler(status=status)
                )
                instance = make_admin(connection)
                with self.assertRaisesRegex(
                    ReleaseTransitionError,
                    "must be rolled back",
                ):
                    if operation == "failure":
                        instance.mark_failed(
                            RELEASE_ID,
                            error="operator stop",
                        )
                    else:
                        instance.retire_release(RELEASE_ID)
                self.assertFalse(
                    any(
                        "UPDATE bauer_rag_v3.knowledge_releases" in sql
                        for sql, _params in connection.calls
                    )
                )

    def test_retire_release_is_safe_and_idempotent(self):
        handler = StatefulReleaseHandler(status="ready")
        connection = FakeConnection(handler)
        retired = make_admin(connection).retire_release(RELEASE_ID)

        self.assertEqual(retired["status"], "retired")
        update_sql = next(
            sql
            for sql, _params in connection.calls
            if "UPDATE bauer_rag_v3.knowledge_releases" in sql
        )
        self.assertIn(
            "retired_at = COALESCE(retired_at, now())",
            update_sql,
        )
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.active_releases" in sql
                or "DELETE FROM bauer_rag_v3.active_releases" in sql
                for sql, _params in connection.calls
            )
        )

        retired_connection = FakeConnection(
            StatefulReleaseHandler(status="retired")
        )
        same = make_admin(retired_connection).retire_release(RELEASE_ID)
        self.assertEqual(same["status"], "retired")
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.knowledge_releases" in sql
                for sql, _params in retired_connection.calls
            )
        )

    def test_activation_and_rollback_only_call_database_function(self):
        targets = iter(
            (
                (PREVIOUS_RELEASE_ID, RELEASE_ID),
                (RELEASE_ID, PREVIOUS_RELEASE_ID),
            )
        )

        def handler(sql, _params):
            if "FROM bauer_rag_v3.activate_release(" in sql:
                return FakeCursor(row=next(targets))
            return FakeCursor()

        connection = FakeConnection(handler)
        instance = make_admin(connection)
        activated = instance.activate_release(
            kb_id=KB_ID,
            release_id=RELEASE_ID,
            actor_principal_id=ADMIN_ID,
            reason="promote validated V3 release",
            production=True,
        )
        rolled_back = instance.rollback_release(
            kb_id=KB_ID,
            target_release_id=PREVIOUS_RELEASE_ID,
            actor_principal_id=ADMIN_ID,
            reason="rollback after regression",
            production=True,
        )

        self.assertEqual(activated.active_release_id, RELEASE_ID)
        self.assertEqual(rolled_back.active_release_id, PREVIOUS_RELEASE_ID)
        calls = [
            (sql, params)
            for sql, params in connection.calls
            if "activate_release" in sql
        ]
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1][-1])
        self.assertTrue(calls[1][1][-1])
        all_sql = "\n".join(sql for sql, _params in connection.calls)
        self.assertNotIn("UPDATE bauer_rag_v3.active_releases", all_sql)
        self.assertNotIn("INSERT INTO bauer_rag_v3.active_releases", all_sql)

    def test_production_gold_gate_remains_in_database_function_contract(self):
        migration = (
            SERVICE_ROOT / "migrations" / "007_rls_roles.sql"
        ).read_text(encoding="utf-8")
        function = migration.split(
            "CREATE OR REPLACE FUNCTION bauer_rag_v3.activate_release(",
            1,
        )[1]
        self.assertIn("production_activation", function)
        self.assertIn(
            "suite.gold_status = 'independent_bauer_verified'",
            function,
        )
        self.assertIn(
            "INSERT INTO bauer_rag_v3.active_releases",
            function,
        )

    def test_invalid_uuid_is_rejected_before_database_access(self):
        connection = FakeConnection()
        instance = make_admin(connection)
        with self.assertRaisesRegex(ValueError, "release_id must be a UUID"):
            instance.start_build("not-a-uuid")
        with self.assertRaisesRegex(ValueError, "actor_principal_id"):
            instance.activate_release(
                kb_id=KB_ID,
                release_id=RELEASE_ID,
                actor_principal_id="nope",
                reason="test",
            )
        self.assertEqual(connection.calls, [])


if __name__ == "__main__":
    unittest.main()
