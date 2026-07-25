from __future__ import annotations

import ast
import sys
import unittest
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.postgres_jobs import (  # noqa: E402
    DeadLetterAlreadyReplayedError,
    DeadLetterReplayError,
    IdempotencyConflictError,
    JobDependency,
    LeaseLostError,
    PostgresJobQueue,
)


JOB_ID = "10000000-0000-0000-0000-000000000001"
PARENT_ID = "10000000-0000-0000-0000-000000000002"
CHILD_ID = "10000000-0000-0000-0000-000000000003"
REPLAY_ID = "10000000-0000-0000-0000-000000000004"
KB_ID = "20000000-0000-0000-0000-000000000001"
LEASE_ID = "30000000-0000-0000-0000-000000000001"
RELEASE_ID = "40000000-0000-0000-0000-000000000001"


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
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.transactions = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        return self.handler(normalized, params)

    def transaction(self):
        self.transactions += 1
        return nullcontext()


def job(**overrides):
    value = {
        "job_id": JOB_ID,
        "queue_name": "compiler",
        "job_type": "compile",
        "kb_id": KB_ID,
        "release_id": None,
        "source_id": None,
        "source_version_id": None,
        "artifact_set_id": None,
        "parent_job_id": None,
        "replay_of_job_id": None,
        "idempotency_key": "source:compiler:v1",
        "payload": {"source": "sha256"},
        "state": "queued",
        "priority": 4,
        "max_attempts": 3,
        "attempt_count": 0,
    }
    value.update(overrides)
    return value


def queue(connection, uuid_values=(JOB_ID,)):
    values = iter(uuid.UUID(value) for value in uuid_values)
    return PostgresJobQueue(
        lambda: nullcontext(connection),
        jsonb_factory=FakeJsonb,
        uuid_factory=lambda: next(values),
    )


class PostgresDurableJobTests(unittest.TestCase):
    def test_psycopg_import_is_lazy(self):
        module_path = (
            SERVICE_ROOT / "bauer_evidence_v3" / "postgres_jobs.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        top_level_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (
                    isinstance(node, ast.Import)
                    and any(alias.name.startswith("psycopg") for alias in node.names)
                )
                or (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").startswith("psycopg")
                )
            )
        ]
        self.assertEqual(top_level_imports, [])

    def test_enqueue_uses_jsonb_and_returns_idempotent_match(self):
        inserted_job = job()

        def inserted_handler(sql, params):
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql:
                self.assertIsInstance(params[11], FakeJsonb)
                return FakeCursor(row=(inserted_job,))
            return FakeCursor()

        connection = FakeConnection(inserted_handler)
        result = queue(connection).enqueue(
            queue_name="compiler",
            job_type="compile",
            kb_id=KB_ID,
            idempotency_key="source:compiler:v1",
            payload={"source": "sha256"},
            priority=4,
            max_attempts=3,
        )
        self.assertEqual(result["job_id"], JOB_ID)
        self.assertTrue(any("job_events" in sql for sql, _ in connection.calls))
        self.assertEqual(connection.transactions, 1)

        existing_bundle = {"job": inserted_job, "dependencies": []}

        def existing_handler(sql, _params):
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql:
                return FakeCursor(row=None)
            if "FOR SHARE" in sql:
                return FakeCursor(row=(existing_bundle,))
            return FakeCursor()

        existing_connection = FakeConnection(existing_handler)
        existing = queue(existing_connection).enqueue(
            queue_name="compiler",
            job_type="compile",
            kb_id=KB_ID,
            idempotency_key="source:compiler:v1",
            payload={"source": "sha256"},
            priority=4,
            max_attempts=3,
        )
        self.assertEqual(existing["job_id"], JOB_ID)
        self.assertFalse(any("job_events" in sql for sql, _ in existing_connection.calls))

    def test_enqueue_rejects_idempotency_semantic_drift(self):
        stored = job(payload={"source": "other"})

        def handler(sql, _params):
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql:
                return FakeCursor(row=None)
            if "FOR SHARE" in sql:
                return FakeCursor(row=({"job": stored, "dependencies": []},))
            return FakeCursor()

        with self.assertRaisesRegex(IdempotencyConflictError, "different job semantics"):
            queue(FakeConnection(handler)).enqueue(
                queue_name="compiler",
                job_type="compile",
                kb_id=KB_ID,
                idempotency_key="source:compiler:v1",
                payload={"source": "sha256"},
                priority=4,
                max_attempts=3,
            )

    def test_enqueue_rejects_conflicting_dependency_requirements(self):
        connection = FakeConnection(lambda _sql, _params: FakeCursor())
        with self.assertRaisesRegex(ValueError, "conflicting completion requirements"):
            queue(connection).enqueue(
                queue_name="compiler",
                job_type="compile",
                kb_id=KB_ID,
                idempotency_key="source:compiler:v1",
                payload={"source": "sha256"},
                dependencies=(
                    JobDependency(PARENT_ID, "succeeded"),
                    JobDependency(PARENT_ID, "completed"),
                ),
            )
        self.assertEqual(connection.calls, [])

    def test_claim_is_skip_locked_dependency_gated_and_creates_attempt(self):
        claimed = job(
            state="running",
            attempt_count=1,
            leased_by="worker-a",
            lease_token=LEASE_ID,
        )

        def handler(sql, params):
            if "WITH RECURSIVE blocked" in sql:
                return FakeCursor(rows=[])
            if "WITH candidate AS" in sql:
                self.assertIn("FOR UPDATE SKIP LOCKED", sql)
                self.assertIn("dependency.requirement = 'succeeded'", sql)
                self.assertIn("job.priority DESC", sql)
                self.assertEqual(params[2], LEASE_ID)
                return FakeCursor(row=(claimed,))
            return FakeCursor()

        connection = FakeConnection(handler)
        result = queue(connection, (LEASE_ID,)).claim(
            queue_name="compiler",
            worker_id="worker-a",
            lease_seconds=45,
        )
        self.assertEqual(result["lease_token"], LEASE_ID)
        attempt_calls = [
            params
            for sql, params in connection.calls
            if "INSERT INTO bauer_rag_v3.job_attempts" in sql
        ]
        self.assertEqual(attempt_calls[0][1:], (1, "worker-a", LEASE_ID))

    def test_stale_owner_updates_zero_rows_and_gets_lease_lost(self):
        def handler(sql, _params):
            if "UPDATE bauer_rag_v3.jobs AS job" in sql:
                return FakeCursor(row=None, rowcount=0)
            return FakeCursor()

        for operation in ("heartbeat", "succeed"):
            with self.subTest(operation=operation):
                instance = queue(FakeConnection(handler))
                with self.assertRaises(LeaseLostError):
                    getattr(instance, operation)(
                        JOB_ID,
                        worker_id="stale-worker",
                        lease_token=LEASE_ID,
                    )

        def fail_handler(sql, _params):
            if "FOR UPDATE" in sql and "lease_expires_at > now()" in sql:
                return FakeCursor(row=None)
            return FakeCursor()

        with self.assertRaises(LeaseLostError):
            queue(FakeConnection(fail_handler)).fail(
                JOB_ID,
                worker_id="stale-worker",
                lease_token=LEASE_ID,
                error_message="late failure",
            )

    def test_heartbeat_and_success_guard_attempt_and_serialize_json(self):
        running = job(state="running", attempt_count=1, lease_token=LEASE_ID)
        succeeded = job(state="succeeded", attempt_count=1, result={"ok": True})

        def heartbeat_handler(sql, _params):
            if "SET heartbeat_at = now()" in sql and "UPDATE bauer_rag_v3.jobs" in sql:
                self.assertIn("job.lease_expires_at > now()", sql)
                return FakeCursor(row=(running,))
            return FakeCursor(rowcount=1)

        heartbeat_connection = FakeConnection(heartbeat_handler)
        heartbeat = queue(heartbeat_connection).heartbeat(
            JOB_ID,
            worker_id="worker-a",
            lease_token=LEASE_ID,
        )
        self.assertEqual(heartbeat["state"], "running")

        def success_handler(sql, params):
            if "SET state = 'succeeded'" in sql:
                self.assertIsInstance(params[0], FakeJsonb)
                return FakeCursor(row=(succeeded,))
            if "SET outcome = 'succeeded'" in sql:
                self.assertIsInstance(params[0], FakeJsonb)
                return FakeCursor(rowcount=1)
            return FakeCursor()

        result = queue(FakeConnection(success_handler)).succeed(
            JOB_ID,
            worker_id="worker-a",
            lease_token=LEASE_ID,
            result={"ok": True},
            metrics={"seconds": 2},
        )
        self.assertEqual(result["state"], "succeeded")

    def test_failure_retries_with_backoff_or_writes_dead_letter(self):
        def retry_handler(sql, params):
            if "FOR UPDATE" in sql and "lease_expires_at > now()" in sql:
                return FakeCursor(
                    row=(
                        {
                            "job_id": JOB_ID,
                            "attempt_count": 1,
                            "max_attempts": 3,
                        },
                    )
                )
            if "SET state = 'retry_wait'" in sql:
                self.assertEqual(params[0], 1)
                return FakeCursor(row=(job(state="retry_wait", attempt_count=1),))
            return FakeCursor()

        retry_connection = FakeConnection(retry_handler)
        retried = queue(retry_connection).fail(
            JOB_ID,
            worker_id="worker-a",
            lease_token=LEASE_ID,
            error_message="parser unavailable",
            error_code="parser_timeout",
        )
        self.assertEqual(retried["state"], "retry_wait")
        self.assertFalse(any("dead_letters" in sql for sql, _ in retry_connection.calls))

        def dead_handler(sql, _params):
            if "FOR UPDATE" in sql and "lease_expires_at > now()" in sql:
                return FakeCursor(
                    row=(
                        {
                            "job_id": JOB_ID,
                            "attempt_count": 3,
                            "max_attempts": 3,
                        },
                    )
                )
            if "SET state = 'dead'" in sql:
                return FakeCursor(row=(job(state="dead", attempt_count=3),))
            if "WITH RECURSIVE blocked" in sql:
                return FakeCursor(rows=[])
            return FakeCursor()

        dead_connection = FakeConnection(dead_handler)
        dead = queue(dead_connection).fail(
            JOB_ID,
            worker_id="worker-a",
            lease_token=LEASE_ID,
            error_message="permanent parse failure",
            error_class="ParseError",
        )
        self.assertEqual(dead["state"], "dead")
        self.assertTrue(any("dead_letters" in sql for sql, _ in dead_connection.calls))

    def test_expired_lease_reaper_retries_and_dead_letters(self):
        expired = [
            {
                "job_id": JOB_ID,
                "lease_token": LEASE_ID,
                "leased_by": "worker-a",
                "attempt_count": 1,
                "max_attempts": 3,
            },
            {
                "job_id": PARENT_ID,
                "lease_token": "30000000-0000-0000-0000-000000000002",
                "leased_by": "worker-b",
                "attempt_count": 2,
                "max_attempts": 2,
            },
        ]

        def handler(sql, params):
            if "WHERE job.state = 'running'" in sql and "FOR UPDATE SKIP LOCKED" in sql:
                return FakeCursor(rows=[(item,) for item in expired])
            if "SET state = 'retry_wait'" in sql:
                return FakeCursor(row=(job(state="retry_wait", attempt_count=1),))
            if "SET state = 'dead'" in sql:
                return FakeCursor(
                    row=(
                        job(
                            job_id=PARENT_ID,
                            state="dead",
                            attempt_count=2,
                        ),
                    )
                )
            if "WITH RECURSIVE blocked" in sql:
                return FakeCursor(rows=[])
            return FakeCursor()

        connection = FakeConnection(handler)
        changed = queue(connection).reap_expired_leases()
        self.assertEqual([item["state"] for item in changed], ["retry_wait", "dead"])
        self.assertTrue(any("dead_letters" in sql for sql, _ in connection.calls))
        expired_attempts = [
            params
            for sql, params in connection.calls
            if "SET outcome = %s" in sql
        ]
        self.assertTrue(all(params[0] == "lease_expired" for params in expired_attempts))

    def test_recursive_dependent_cancellation_only_targets_required_success(self):
        cancelled = {
            "job_id": CHILD_ID,
            "state": "cancelled",
            "last_error_code": "dependency_failed",
            "last_error_message": f"dependency_failed:{PARENT_ID}",
        }

        def handler(sql, _params):
            if "WITH RECURSIVE blocked" in sql:
                self.assertIn("dependency.requirement = 'succeeded'", sql)
                self.assertIn("child.state IN ('queued', 'retry_wait')", sql)
                return FakeCursor(rows=[(cancelled,)])
            return FakeCursor()

        connection = FakeConnection(handler)
        result = queue(connection).cancel_blocked_dependents(actor="dependency-reaper")
        self.assertEqual(result[0]["state"], "cancelled")
        self.assertTrue(any("job_events" in sql for sql, _ in connection.calls))

    def test_dead_letter_replay_creates_new_linked_job_atomically(self):
        source_job = job(state="dead", attempt_count=3)
        dead_letter = {
            "job_id": JOB_ID,
            "payload_snapshot": {"source": "sha256"},
            "replayed_by_job_id": None,
            "replayed_at": None,
        }
        replayed_job = job(
            job_id=REPLAY_ID,
            state="queued",
            replay_of_job_id=JOB_ID,
            idempotency_key="source:compiler:v1:replay:incident-42",
        )

        def handler(sql, params):
            if "FROM bauer_rag_v3.dead_letters AS dead_letter" in sql:
                self.assertIn("FOR UPDATE OF dead_letter, job", sql)
                return FakeCursor(
                    row=(
                        {
                            "dead_letter": dead_letter,
                            "job": source_job,
                            "dependencies": [
                                {
                                    "job_id": PARENT_ID,
                                    "requirement": "succeeded",
                                }
                            ],
                        },
                    )
                )
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql:
                self.assertEqual(params[0], REPLAY_ID)
                self.assertEqual(params[9], JOB_ID)
                self.assertEqual(
                    params[10],
                    "source:compiler:v1:replay:incident-42",
                )
                self.assertIsInstance(params[11], FakeJsonb)
                return FakeCursor(row=(replayed_job,))
            if "UPDATE bauer_rag_v3.dead_letters" in sql:
                self.assertEqual(params, (REPLAY_ID, JOB_ID))
                return FakeCursor(rowcount=1)
            return FakeCursor()

        connection = FakeConnection(handler)
        replay = queue(connection, (REPLAY_ID,)).replay_dead_letter(
            JOB_ID,
            idempotency_key="source:compiler:v1:replay:incident-42",
            actor="operator-a",
        )

        self.assertEqual(replay["job_id"], REPLAY_ID)
        dependency_params = next(
            params
            for sql, params in connection.calls
            if "INSERT INTO bauer_rag_v3.job_dependencies" in sql
        )
        self.assertEqual(
            dependency_params,
            (REPLAY_ID, PARENT_ID, "succeeded"),
        )
        event_params = [
            params
            for sql, params in connection.calls
            if "INSERT INTO bauer_rag_v3.job_events" in sql
        ]
        self.assertEqual(
            [(params[0], params[1]) for params in event_params],
            [(REPLAY_ID, "queued"), (JOB_ID, "replayed")],
        )
        self.assertEqual(connection.transactions, 1)

    def test_dead_letter_replay_is_idempotent_only_for_the_same_key(self):
        dead_letter = {
            "job_id": JOB_ID,
            "payload_snapshot": {"source": "sha256"},
            "replayed_by_job_id": REPLAY_ID,
            "replayed_at": "2026-07-25T00:00:00Z",
        }
        replayed_job = job(
            job_id=REPLAY_ID,
            replay_of_job_id=JOB_ID,
            idempotency_key="source:compiler:v1:replay:incident-42",
        )

        def handler(sql, _params):
            if "FROM bauer_rag_v3.dead_letters AS dead_letter" in sql:
                return FakeCursor(
                    row=(
                        {
                            "dead_letter": dead_letter,
                            "job": job(state="dead", attempt_count=3),
                            "dependencies": [],
                        },
                    )
                )
            if "WHERE job.job_id = %s::uuid FOR SHARE" in sql:
                return FakeCursor(row=(replayed_job,))
            return FakeCursor()

        same_connection = FakeConnection(handler)
        same = queue(same_connection).replay_dead_letter(
            JOB_ID,
            idempotency_key="source:compiler:v1:replay:incident-42",
            actor="operator-a",
        )
        self.assertEqual(same["job_id"], REPLAY_ID)
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.dead_letters" in sql
                for sql, _params in same_connection.calls
            )
        )

        with self.assertRaises(DeadLetterAlreadyReplayedError):
            queue(FakeConnection(handler)).replay_dead_letter(
                JOB_ID,
                idempotency_key="source:compiler:v1:replay:incident-43",
                actor="operator-a",
            )

    def test_dead_letter_replay_rejects_original_idempotency_key(self):
        def handler(sql, _params):
            if "FROM bauer_rag_v3.dead_letters AS dead_letter" in sql:
                return FakeCursor(
                    row=(
                        {
                            "dead_letter": {
                                "job_id": JOB_ID,
                                "payload_snapshot": {"source": "sha256"},
                                "replayed_by_job_id": None,
                                "replayed_at": None,
                            },
                            "job": job(state="dead", attempt_count=3),
                            "dependencies": [],
                        },
                    )
                )
            return FakeCursor()

        connection = FakeConnection(handler)
        with self.assertRaisesRegex(
            DeadLetterReplayError,
            "must differ from the original",
        ):
            queue(connection).replay_dead_letter(
                JOB_ID,
                idempotency_key="source:compiler:v1",
                actor="operator-a",
            )
        self.assertFalse(
            any(
                "INSERT INTO bauer_rag_v3.jobs AS job" in sql
                for sql, _params in connection.calls
            )
        )

    def test_dead_letter_replay_rejects_terminal_release(self):
        def handler(sql, _params):
            if "FROM bauer_rag_v3.dead_letters AS dead_letter" in sql:
                return FakeCursor(
                    row=(
                        {
                            "dead_letter": {
                                "job_id": JOB_ID,
                                "payload_snapshot": {"source": "sha256"},
                                "replayed_by_job_id": None,
                                "replayed_at": None,
                            },
                            "job": job(
                                state="dead",
                                attempt_count=3,
                                release_id=RELEASE_ID,
                            ),
                            "release_status": "failed",
                            "dependencies": [],
                        },
                    )
                )
            return FakeCursor()

        connection = FakeConnection(handler)
        with self.assertRaisesRegex(
            DeadLetterReplayError,
            "building or validating",
        ):
            queue(connection).replay_dead_letter(
                JOB_ID,
                idempotency_key="source:compiler:v1:replay:incident-42",
                actor="operator-a",
            )
        self.assertFalse(
            any(
                "INSERT INTO bauer_rag_v3.jobs AS job" in sql
                for sql, _params in connection.calls
            )
        )

    def test_dead_letter_replay_rejects_an_occupied_new_key(self):
        def handler(sql, _params):
            if "FROM bauer_rag_v3.dead_letters AS dead_letter" in sql:
                return FakeCursor(
                    row=(
                        {
                            "dead_letter": {
                                "job_id": JOB_ID,
                                "payload_snapshot": {"source": "sha256"},
                                "replayed_by_job_id": None,
                                "replayed_at": None,
                            },
                            "job": job(state="dead", attempt_count=3),
                            "dependencies": [],
                        },
                    )
                )
            if "INSERT INTO bauer_rag_v3.jobs AS job" in sql:
                return FakeCursor(row=None)
            return FakeCursor()

        connection = FakeConnection(handler)
        with self.assertRaisesRegex(
            IdempotencyConflictError,
            "already in use",
        ):
            queue(connection, (REPLAY_ID,)).replay_dead_letter(
                JOB_ID,
                idempotency_key="source:compiler:v1:replay:occupied",
                actor="operator-a",
            )
        self.assertFalse(
            any(
                "UPDATE bauer_rag_v3.dead_letters" in sql
                for sql, _params in connection.calls
            )
        )


if __name__ == "__main__":
    unittest.main()
