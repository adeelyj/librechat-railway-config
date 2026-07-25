"""Production PostgreSQL durable-job operations for Bauer Evidence V3.

The queue is intentionally at-least-once. A lease may expire after a worker has
performed an external side effect, so handlers must remain idempotent. Database
effects are protected by the current worker ID and lease token; a stale owner
always updates zero rows and receives :class:`LeaseLostError`.

No psycopg module is imported at module-import time. Tests and alternate
connection managers can inject both the connection provider and JSON adapter.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 60
DEFAULT_REAPER_BATCH_SIZE = 100
MAX_BACKOFF_SECONDS = 3_600
MAX_NAME_LENGTH = 200
MAX_ERROR_MESSAGE_LENGTH = 4_000


class PostgresJobError(RuntimeError):
    """Base class for durable queue failures."""


class PostgresJobDependencyError(PostgresJobError):
    """The optional psycopg 3 runtime is unavailable."""


class IdempotencyConflictError(PostgresJobError):
    """An idempotency key was reused for different work."""


class LeaseLostError(PostgresJobError):
    """The caller no longer owns a live lease for the job."""


class DeadLetterReplayError(PostgresJobError):
    """A dead letter cannot be safely replayed."""


class DeadLetterAlreadyReplayedError(DeadLetterReplayError):
    """A dead letter is already linked to a different replay request."""


@dataclass(frozen=True, slots=True, order=True)
class JobDependency:
    job_id: str
    requirement: str = "succeeded"

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _required_uuid(self.job_id, "dependency job_id"))
        if self.requirement not in {"succeeded", "completed"}:
            raise ValueError("dependency requirement must be 'succeeded' or 'completed'")


class PostgresJobQueue:
    """Transactional operations over the tables created by migration 005."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any] | Any],
        *,
        jsonb_factory: Callable[[Any], Any] | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if not callable(connection_provider):
            raise TypeError("connection_provider must be callable")
        self._connection_provider = connection_provider
        self._jsonb_factory = jsonb_factory or _default_jsonb
        self._uuid_factory = uuid_factory

    @classmethod
    def from_dsn(cls, database_url: str, **kwargs: Any) -> "PostgresJobQueue":
        """Create a queue whose psycopg connection is opened lazily per operation."""

        if not isinstance(database_url, str) or not database_url.strip():
            raise ValueError("database_url must not be empty")

        def connect() -> Any:
            psycopg = _load_psycopg()
            return psycopg.connect(database_url)

        return cls(connect, **kwargs)

    def enqueue(
        self,
        *,
        queue_name: str,
        job_type: str,
        kb_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        release_id: str | None = None,
        source_id: str | None = None,
        source_version_id: str | None = None,
        artifact_set_id: str | None = None,
        parent_job_id: str | None = None,
        replay_of_job_id: str | None = None,
        priority: int = 0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        available_at: datetime | None = None,
        dependencies: Sequence[JobDependency | str | tuple[str, str]] = (),
    ) -> dict[str, Any]:
        """Insert once, returning the existing row only when semantics match."""

        queue_name = _required_name(queue_name, "queue_name")
        job_type = _required_name(job_type, "job_type")
        idempotency_key = _required_name(idempotency_key, "idempotency_key")
        kb_id = _required_uuid(kb_id, "kb_id")
        release_id = _optional_uuid(release_id, "release_id")
        source_id = _optional_uuid(source_id, "source_id")
        source_version_id = _optional_uuid(source_version_id, "source_version_id")
        artifact_set_id = _optional_uuid(artifact_set_id, "artifact_set_id")
        parent_job_id = _optional_uuid(parent_job_id, "parent_job_id")
        replay_of_job_id = _optional_uuid(replay_of_job_id, "replay_of_job_id")
        if source_version_id is not None and source_id is None:
            raise ValueError("source_version_id requires source_id")
        if artifact_set_id is not None and source_version_id is None:
            raise ValueError("artifact_set_id requires source_version_id")
        priority = _smallint(priority, "priority")
        max_attempts = _positive_smallint(max_attempts, "max_attempts")
        normalized_payload = _json_object(payload, "payload")
        normalized_dependencies = _dependencies(dependencies)
        job_id = _required_uuid(self._uuid_factory(), "generated job_id")
        if any(item.job_id == job_id for item in normalized_dependencies):
            raise ValueError("a job cannot depend on itself")

        requested_semantics = {
            "job_type": job_type,
            "kb_id": kb_id,
            "release_id": release_id,
            "source_id": source_id,
            "source_version_id": source_version_id,
            "artifact_set_id": artifact_set_id,
            "parent_job_id": parent_job_id,
            "replay_of_job_id": replay_of_job_id,
            "payload": normalized_payload,
            "priority": priority,
            "max_attempts": max_attempts,
            "dependencies": [
                {"job_id": item.job_id, "requirement": item.requirement}
                for item in normalized_dependencies
            ],
        }

        with self._connection() as connection, connection.transaction():
            cursor = connection.execute(
                """
                INSERT INTO bauer_rag_v3.jobs AS job (
                    job_id,
                    queue_name,
                    job_type,
                    kb_id,
                    release_id,
                    source_id,
                    source_version_id,
                    artifact_set_id,
                    parent_job_id,
                    replay_of_job_id,
                    idempotency_key,
                    payload,
                    priority,
                    available_at,
                    max_attempts
                )
                VALUES (
                    %s::uuid, %s, %s, %s::uuid, %s::uuid, %s::uuid,
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s,
                    %s, COALESCE(%s::timestamptz, now()), %s
                )
                ON CONFLICT (queue_name, idempotency_key) DO NOTHING
                RETURNING to_jsonb(job)
                """,
                (
                    job_id,
                    queue_name,
                    job_type,
                    kb_id,
                    release_id,
                    source_id,
                    source_version_id,
                    artifact_set_id,
                    parent_job_id,
                    replay_of_job_id,
                    idempotency_key,
                    self._jsonb(normalized_payload),
                    priority,
                    available_at,
                    max_attempts,
                ),
            )
            inserted = _fetch_json(cursor)
            if inserted is None:
                existing = _fetch_json(
                    connection.execute(
                        """
                        SELECT jsonb_build_object(
                            'job', to_jsonb(job),
                            'dependencies',
                            COALESCE(
                                (
                                    SELECT jsonb_agg(
                                        jsonb_build_object(
                                            'job_id', dependency.depends_on_job_id,
                                            'requirement', dependency.requirement
                                        )
                                        ORDER BY
                                            dependency.depends_on_job_id,
                                            dependency.requirement
                                    )
                                    FROM bauer_rag_v3.job_dependencies AS dependency
                                    WHERE dependency.job_id = job.job_id
                                ),
                                '[]'::jsonb
                            )
                        )
                        FROM bauer_rag_v3.jobs AS job
                        WHERE job.queue_name = %s
                          AND job.idempotency_key = %s
                        FOR SHARE
                        """,
                        (queue_name, idempotency_key),
                    )
                )
                if existing is None:
                    raise PostgresJobError(
                        "idempotency conflict did not resolve to an existing job"
                    )
                if _stored_semantics(existing) != requested_semantics:
                    raise IdempotencyConflictError(
                        "idempotency key was reused with different job semantics"
                    )
                return dict(existing["job"])

            for dependency in normalized_dependencies:
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.job_dependencies (
                        job_id,
                        depends_on_job_id,
                        requirement
                    )
                    VALUES (%s::uuid, %s::uuid, %s)
                    """,
                    (job_id, dependency.job_id, dependency.requirement),
                )
            self._event(
                connection,
                job_id,
                "queued",
                actor=None,
                details={"idempotency_key": idempotency_key},
            )
            return inserted

    def claim(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Claim one dependency-ready job using ``FOR UPDATE SKIP LOCKED``."""

        queue_name = _required_name(queue_name, "queue_name")
        worker_id = _required_name(worker_id, "worker_id")
        lease_seconds = _positive_seconds(lease_seconds, "lease_seconds")
        lease_token = _required_uuid(self._uuid_factory(), "generated lease_token")

        with self._connection() as connection, connection.transaction():
            self._cancel_blocked_dependents(connection, actor=worker_id)
            claimed = _fetch_json(
                connection.execute(
                    """
                    WITH candidate AS (
                        SELECT job.job_id
                        FROM bauer_rag_v3.jobs AS job
                        WHERE job.queue_name = %s
                          AND job.state IN ('queued', 'retry_wait')
                          AND job.available_at <= now()
                          AND job.attempt_count < job.max_attempts
                          AND NOT EXISTS (
                              SELECT 1
                              FROM bauer_rag_v3.job_dependencies AS dependency
                              JOIN bauer_rag_v3.jobs AS prerequisite
                                ON prerequisite.job_id = dependency.depends_on_job_id
                              WHERE dependency.job_id = job.job_id
                                AND (
                                    (
                                        dependency.requirement = 'succeeded'
                                        AND prerequisite.state <> 'succeeded'
                                    )
                                    OR
                                    (
                                        dependency.requirement = 'completed'
                                        AND prerequisite.state NOT IN (
                                            'succeeded', 'dead', 'cancelled'
                                        )
                                    )
                                )
                          )
                        ORDER BY
                            job.priority DESC,
                            job.available_at,
                            job.created_at,
                            job.job_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE bauer_rag_v3.jobs AS job
                    SET state = 'running',
                        attempt_count = job.attempt_count + 1,
                        leased_by = %s,
                        lease_token = %s::uuid,
                        lease_expires_at =
                            now() + (%s * interval '1 second'),
                        heartbeat_at = now(),
                        started_at = COALESCE(job.started_at, now()),
                        updated_at = now()
                    FROM candidate
                    WHERE job.job_id = candidate.job_id
                    RETURNING to_jsonb(job)
                    """,
                    (queue_name, worker_id, lease_token, lease_seconds),
                )
            )
            if claimed is None:
                return None
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.job_attempts (
                    job_id,
                    attempt_number,
                    worker_id,
                    lease_token,
                    heartbeat_at
                )
                VALUES (%s::uuid, %s, %s, %s::uuid, now())
                """,
                (
                    claimed["job_id"],
                    int(claimed["attempt_count"]),
                    worker_id,
                    lease_token,
                ),
            )
            self._event(
                connection,
                str(claimed["job_id"]),
                "claimed",
                actor=worker_id,
                details={
                    "attempt_number": int(claimed["attempt_count"]),
                    "lease_token": lease_token,
                },
            )
            return claimed

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Extend a live lease, rejecting every stale owner."""

        job_id, worker_id, lease_token = _lease_identity(job_id, worker_id, lease_token)
        lease_seconds = _positive_seconds(lease_seconds, "lease_seconds")
        with self._connection() as connection, connection.transaction():
            updated = _fetch_json(
                connection.execute(
                    """
                    UPDATE bauer_rag_v3.jobs AS job
                    SET heartbeat_at = now(),
                        lease_expires_at =
                            now() + (%s * interval '1 second'),
                        updated_at = now()
                    WHERE job.job_id = %s::uuid
                      AND job.state = 'running'
                      AND job.leased_by = %s
                      AND job.lease_token = %s::uuid
                      AND job.lease_expires_at > now()
                    RETURNING to_jsonb(job)
                    """,
                    (lease_seconds, job_id, worker_id, lease_token),
                )
            )
            if updated is None:
                raise LeaseLostError("worker does not own a live job lease")
            attempt = connection.execute(
                """
                UPDATE bauer_rag_v3.job_attempts
                SET heartbeat_at = now()
                WHERE job_id = %s::uuid
                  AND lease_token = %s::uuid
                  AND worker_id = %s
                  AND outcome = 'running'
                """,
                (job_id, lease_token, worker_id),
            )
            if attempt.rowcount != 1:
                raise PostgresJobError("live job lease has no matching running attempt")
            return updated

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        result: Mapping[str, Any] | None = None,
        result_object_sha256: str | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit successful completion only for the current live lease."""

        job_id, worker_id, lease_token = _lease_identity(job_id, worker_id, lease_token)
        normalized_result = _json_object(result or {}, "result")
        normalized_metrics = _json_object(metrics or {}, "metrics")
        result_object_sha256 = _optional_sha256(result_object_sha256)
        with self._connection() as connection, connection.transaction():
            updated = _fetch_json(
                connection.execute(
                    """
                    UPDATE bauer_rag_v3.jobs AS job
                    SET state = 'succeeded',
                        result = %s,
                        result_object_sha256 = %s,
                        leased_by = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL,
                        last_error_code = NULL,
                        last_error_class = NULL,
                        last_error_message = NULL,
                        finished_at = now(),
                        updated_at = now()
                    WHERE job.job_id = %s::uuid
                      AND job.state = 'running'
                      AND job.leased_by = %s
                      AND job.lease_token = %s::uuid
                      AND job.lease_expires_at > now()
                    RETURNING to_jsonb(job)
                    """,
                    (
                        self._jsonb(normalized_result),
                        result_object_sha256,
                        job_id,
                        worker_id,
                        lease_token,
                    ),
                )
            )
            if updated is None:
                raise LeaseLostError("worker does not own a live job lease")
            attempt = connection.execute(
                """
                UPDATE bauer_rag_v3.job_attempts
                SET outcome = 'succeeded',
                    heartbeat_at = now(),
                    finished_at = now(),
                    metrics = %s
                WHERE job_id = %s::uuid
                  AND lease_token = %s::uuid
                  AND worker_id = %s
                  AND outcome = 'running'
                """,
                (
                    self._jsonb(normalized_metrics),
                    job_id,
                    lease_token,
                    worker_id,
                ),
            )
            if attempt.rowcount != 1:
                raise PostgresJobError("successful job has no matching running attempt")
            self._event(
                connection,
                job_id,
                "succeeded",
                actor=worker_id,
                details={"result_object_sha256": result_object_sha256},
            )
            return updated

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        error_message: str,
        error_code: str | None = None,
        error_class: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        backoff_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Fail the current attempt and schedule a retry or durable dead letter."""

        job_id, worker_id, lease_token = _lease_identity(job_id, worker_id, lease_token)
        error_message = _error_message(error_message)
        error_code = _optional_name(error_code, "error_code")
        error_class = _optional_name(error_class, "error_class")
        normalized_metrics = _json_object(metrics or {}, "metrics")

        with self._connection() as connection, connection.transaction():
            owned = _fetch_json(
                connection.execute(
                    """
                    SELECT jsonb_build_object(
                        'job_id', job.job_id,
                        'attempt_count', job.attempt_count,
                        'max_attempts', job.max_attempts
                    )
                    FROM bauer_rag_v3.jobs AS job
                    WHERE job.job_id = %s::uuid
                      AND job.state = 'running'
                      AND job.leased_by = %s
                      AND job.lease_token = %s::uuid
                      AND job.lease_expires_at > now()
                    FOR UPDATE
                    """,
                    (job_id, worker_id, lease_token),
                )
            )
            if owned is None:
                raise LeaseLostError("worker does not own a live job lease")
            dead = int(owned["attempt_count"]) >= int(owned["max_attempts"])
            delay = _retry_delay(
                int(owned["attempt_count"]),
                backoff_seconds=backoff_seconds,
            )
            updated = self._transition_failure(
                connection,
                job_id=job_id,
                lease_token=lease_token,
                worker_id=worker_id,
                dead=dead,
                delay=delay,
                attempt_outcome="failed",
                error_code=error_code,
                error_class=error_class,
                error_message=error_message,
                metrics=normalized_metrics,
            )
            if dead:
                self._dead_letter(
                    connection,
                    job_id,
                    error_code=error_code,
                    error_class=error_class,
                    error_message=error_message,
                )
                self._cancel_blocked_dependents(connection, actor=worker_id)
            return updated

    def reap_expired_leases(
        self,
        *,
        actor: str = "lease-reaper",
        batch_size: int = DEFAULT_REAPER_BATCH_SIZE,
    ) -> tuple[dict[str, Any], ...]:
        """Requeue or dead-letter expired claims under row locks."""

        actor = _required_name(actor, "actor")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise ValueError("batch_size must be an integer")
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")

        with self._connection() as connection, connection.transaction():
            expired = _fetch_json_rows(
                connection.execute(
                    """
                    SELECT jsonb_build_object(
                        'job_id', job.job_id,
                        'lease_token', job.lease_token,
                        'leased_by', job.leased_by,
                        'attempt_count', job.attempt_count,
                        'max_attempts', job.max_attempts
                    )
                    FROM bauer_rag_v3.jobs AS job
                    WHERE job.state = 'running'
                      AND job.lease_expires_at <= now()
                    ORDER BY job.lease_expires_at, job.job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (batch_size,),
                )
            )
            changed: list[dict[str, Any]] = []
            for item in expired:
                job_id = str(item["job_id"])
                lease_token = str(item["lease_token"])
                worker_id = str(item["leased_by"])
                dead = int(item["attempt_count"]) >= int(item["max_attempts"])
                updated = self._transition_failure(
                    connection,
                    job_id=job_id,
                    lease_token=lease_token,
                    worker_id=worker_id,
                    dead=dead,
                    delay=_retry_delay(int(item["attempt_count"])),
                    attempt_outcome="lease_expired",
                    error_code="lease_expired",
                    error_class="LeaseExpired",
                    error_message="Worker lease expired before completion",
                    metrics={},
                    require_expired=True,
                    actor=actor,
                )
                if updated is None:
                    continue
                changed.append(updated)
                if dead:
                    self._dead_letter(
                        connection,
                        job_id,
                        error_code="lease_expired",
                        error_class="LeaseExpired",
                        error_message="Worker lease expired before completion",
                    )
            if any(item.get("state") == "dead" for item in changed):
                self._cancel_blocked_dependents(connection, actor=actor)
            return tuple(changed)

    def cancel_blocked_dependents(
        self,
        *,
        actor: str = "dependency-reaper",
    ) -> tuple[dict[str, Any], ...]:
        """Cancel queued descendants whose required-success ancestor failed."""

        actor = _required_name(actor, "actor")
        with self._connection() as connection, connection.transaction():
            return tuple(self._cancel_blocked_dependents(connection, actor=actor))

    def replay_dead_letter(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        """Create one fresh queued job from an unreplayed dead letter.

        The failed job remains immutable.  Its payload snapshot, routing
        fields, retry policy, and dependencies are copied to a newly generated
        job whose ``replay_of_job_id`` points at the dead job.  The caller must
        supply a new idempotency key so an operator retry is distinguishable
        from the original work while repeated calls with the same key remain
        idempotent.
        """

        job_id = _required_uuid(job_id, "job_id")
        idempotency_key = _required_name(idempotency_key, "idempotency_key")
        actor = _required_name(actor, "actor")

        with self._connection() as connection, connection.transaction():
            bundle = _fetch_json(
                connection.execute(
                    """
                    SELECT jsonb_build_object(
                        'dead_letter', to_jsonb(dead_letter),
                        'job', to_jsonb(job),
                        'release_status',
                        (
                            SELECT release.status
                            FROM bauer_rag_v3.knowledge_releases AS release
                            WHERE release.release_id = job.release_id
                        ),
                        'dependencies',
                        COALESCE(
                            (
                                SELECT jsonb_agg(
                                    jsonb_build_object(
                                        'job_id',
                                        dependency.depends_on_job_id,
                                        'requirement',
                                        dependency.requirement
                                    )
                                    ORDER BY
                                        dependency.depends_on_job_id,
                                        dependency.requirement
                                )
                                FROM bauer_rag_v3.job_dependencies AS dependency
                                WHERE dependency.job_id = job.job_id
                            ),
                            '[]'::jsonb
                        )
                    )
                    FROM bauer_rag_v3.dead_letters AS dead_letter
                    JOIN bauer_rag_v3.jobs AS job
                      ON job.job_id = dead_letter.job_id
                    WHERE dead_letter.job_id = %s::uuid
                    FOR UPDATE OF dead_letter, job
                    """,
                    (job_id,),
                )
            )
            if bundle is None:
                raise DeadLetterReplayError(
                    f"unknown or inaccessible dead letter: {job_id}"
                )

            dead_letter = bundle.get("dead_letter")
            source_job = bundle.get("job")
            raw_dependencies = bundle.get("dependencies", [])
            if (
                not isinstance(dead_letter, Mapping)
                or not isinstance(source_job, Mapping)
                or not isinstance(raw_dependencies, list)
            ):
                raise PostgresJobError(
                    "stored dead-letter replay record has an invalid shape"
                )

            replayed_by_job_id = _nullable_text(
                dead_letter.get("replayed_by_job_id")
            )
            if replayed_by_job_id is not None:
                replayed = _fetch_json(
                    connection.execute(
                        """
                        SELECT to_jsonb(job)
                        FROM bauer_rag_v3.jobs AS job
                        WHERE job.job_id = %s::uuid
                        FOR SHARE
                        """,
                        (replayed_by_job_id,),
                    )
                )
                if replayed is None:
                    raise PostgresJobError(
                        "dead letter points to a missing replay job"
                    )
                if str(replayed.get("idempotency_key")) != idempotency_key:
                    raise DeadLetterAlreadyReplayedError(
                        "dead letter was already replayed with a different "
                        "idempotency key"
                    )
                return replayed

            if str(source_job.get("state")) != "dead":
                raise DeadLetterReplayError(
                    "only a job in the dead state can be replayed"
                )
            if (
                source_job.get("release_id") is not None
                and bundle.get("release_status") not in {
                    "building",
                    "validating",
                }
            ):
                raise DeadLetterReplayError(
                    "release-scoped jobs can only be replayed while their "
                    "release is building or validating"
                )
            original_idempotency_key = _required_name(
                source_job.get("idempotency_key"),
                "stored idempotency_key",
            )
            if idempotency_key == original_idempotency_key:
                raise DeadLetterReplayError(
                    "replay idempotency_key must differ from the original"
                )

            dependencies = _stored_dependencies(raw_dependencies)
            replay_job_id = _required_uuid(
                self._uuid_factory(),
                "generated replay job_id",
            )
            if any(item.job_id == replay_job_id for item in dependencies):
                raise PostgresJobError(
                    "generated replay job_id conflicts with a dependency"
                )

            replay_payload = _json_object(
                dead_letter.get("payload_snapshot"),
                "stored payload_snapshot",
            )
            requested_semantics = _replay_semantics(
                source_job,
                replay_of_job_id=job_id,
                payload=replay_payload,
                dependencies=dependencies,
            )
            inserted = _fetch_json(
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.jobs AS job (
                        job_id,
                        queue_name,
                        job_type,
                        kb_id,
                        release_id,
                        source_id,
                        source_version_id,
                        artifact_set_id,
                        parent_job_id,
                        replay_of_job_id,
                        idempotency_key,
                        payload,
                        priority,
                        available_at,
                        max_attempts
                    )
                    VALUES (
                        %s::uuid, %s, %s, %s::uuid, %s::uuid, %s::uuid,
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s,
                        %s, now(), %s
                    )
                    ON CONFLICT (queue_name, idempotency_key) DO NOTHING
                    RETURNING to_jsonb(job)
                    """,
                    (
                        replay_job_id,
                        requested_semantics["queue_name"],
                        requested_semantics["job_type"],
                        requested_semantics["kb_id"],
                        requested_semantics["release_id"],
                        requested_semantics["source_id"],
                        requested_semantics["source_version_id"],
                        requested_semantics["artifact_set_id"],
                        requested_semantics["parent_job_id"],
                        job_id,
                        idempotency_key,
                        self._jsonb(replay_payload),
                        requested_semantics["priority"],
                        requested_semantics["max_attempts"],
                    ),
                )
            )
            if inserted is None:
                # A replay created by this method links the dead letter in the
                # same transaction.  Therefore an unlinked dead letter plus an
                # occupied key is never an idempotent partial success; it is a
                # collision with unrelated or manually inserted work.
                raise IdempotencyConflictError(
                    "replay idempotency key is already in use"
                )
            for dependency in dependencies:
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.job_dependencies (
                        job_id,
                        depends_on_job_id,
                        requirement
                    )
                    VALUES (%s::uuid, %s::uuid, %s)
                    """,
                    (
                        replay_job_id,
                        dependency.job_id,
                        dependency.requirement,
                    ),
                )
            self._event(
                connection,
                replay_job_id,
                "queued",
                actor=actor,
                details={
                    "idempotency_key": idempotency_key,
                    "replay_of_job_id": job_id,
                },
            )

            linked = connection.execute(
                """
                UPDATE bauer_rag_v3.dead_letters
                SET replayed_by_job_id = %s::uuid,
                    replayed_at = now()
                WHERE job_id = %s::uuid
                  AND replayed_by_job_id IS NULL
                  AND replayed_at IS NULL
                """,
                (replay_job_id, job_id),
            )
            if linked.rowcount != 1:
                raise PostgresJobError(
                    "dead letter changed while its replay was being linked"
                )
            self._event(
                connection,
                job_id,
                "replayed",
                actor=actor,
                details={
                    "replayed_by_job_id": replay_job_id,
                    "idempotency_key": idempotency_key,
                },
            )
            return inserted

    def _transition_failure(
        self,
        connection: Any,
        *,
        job_id: str,
        lease_token: str,
        worker_id: str,
        dead: bool,
        delay: int,
        attempt_outcome: str,
        error_code: str | None,
        error_class: str | None,
        error_message: str,
        metrics: Mapping[str, Any],
        require_expired: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any] | None:
        state = "dead" if dead else "retry_wait"
        expiry_guard = (
            "AND job.lease_expires_at <= now()"
            if require_expired
            else "AND job.lease_expires_at > now()"
        )
        updated = _fetch_json(
            connection.execute(
                f"""
                UPDATE bauer_rag_v3.jobs AS job
                SET state = '{state}',
                    available_at = CASE
                        WHEN '{state}' = 'retry_wait'
                        THEN now() + (%s * interval '1 second')
                        ELSE job.available_at
                    END,
                    leased_by = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    last_error_code = %s,
                    last_error_class = %s,
                    last_error_message = %s,
                    finished_at = CASE
                        WHEN '{state}' = 'dead' THEN now()
                        ELSE NULL
                    END,
                    updated_at = now()
                WHERE job.job_id = %s::uuid
                  AND job.state = 'running'
                  AND job.leased_by = %s
                  AND job.lease_token = %s::uuid
                  {expiry_guard}
                RETURNING to_jsonb(job)
                """,
                (
                    delay,
                    error_code,
                    error_class,
                    error_message,
                    job_id,
                    worker_id,
                    lease_token,
                ),
            )
        )
        if updated is None:
            if require_expired:
                return None
            raise LeaseLostError("worker does not own a live job lease")
        attempt = connection.execute(
            """
            UPDATE bauer_rag_v3.job_attempts
            SET outcome = %s,
                heartbeat_at = now(),
                finished_at = now(),
                error_code = %s,
                error_class = %s,
                error_message = %s,
                metrics = %s
            WHERE job_id = %s::uuid
              AND lease_token = %s::uuid
              AND worker_id = %s
              AND outcome = 'running'
            """,
            (
                attempt_outcome,
                error_code,
                error_class,
                error_message,
                self._jsonb(dict(metrics)),
                job_id,
                lease_token,
                worker_id,
            ),
        )
        if attempt.rowcount != 1:
            raise PostgresJobError("failed job has no matching running attempt")
        self._event(
            connection,
            job_id,
            state,
            actor=actor or worker_id,
            details={
                "attempt_outcome": attempt_outcome,
                "error_code": error_code,
                "backoff_seconds": None if dead else delay,
            },
        )
        return updated

    def _dead_letter(
        self,
        connection: Any,
        job_id: str,
        *,
        error_code: str | None,
        error_class: str | None,
        error_message: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO bauer_rag_v3.dead_letters (
                job_id,
                final_error_code,
                final_error_class,
                final_error_message,
                payload_snapshot
            )
            SELECT
                job.job_id,
                %s,
                %s,
                %s,
                job.payload
            FROM bauer_rag_v3.jobs AS job
            WHERE job.job_id = %s::uuid
              AND job.state = 'dead'
            ON CONFLICT (job_id) DO NOTHING
            """,
            (error_code, error_class, error_message, job_id),
        )

    def _cancel_blocked_dependents(
        self,
        connection: Any,
        *,
        actor: str,
    ) -> list[dict[str, Any]]:
        cancelled = _fetch_json_rows(
            connection.execute(
                """
                WITH RECURSIVE blocked(job_id, root_dependency_id) AS (
                    SELECT child.job_id, prerequisite.job_id
                    FROM bauer_rag_v3.job_dependencies AS dependency
                    JOIN bauer_rag_v3.jobs AS prerequisite
                      ON prerequisite.job_id = dependency.depends_on_job_id
                    JOIN bauer_rag_v3.jobs AS child
                      ON child.job_id = dependency.job_id
                    WHERE dependency.requirement = 'succeeded'
                      AND prerequisite.state IN ('dead', 'cancelled')
                      AND child.state IN ('queued', 'retry_wait')

                    UNION

                    SELECT child.job_id, blocked.root_dependency_id
                    FROM blocked
                    JOIN bauer_rag_v3.job_dependencies AS dependency
                      ON dependency.depends_on_job_id = blocked.job_id
                     AND dependency.requirement = 'succeeded'
                    JOIN bauer_rag_v3.jobs AS child
                      ON child.job_id = dependency.job_id
                    WHERE child.state IN ('queued', 'retry_wait')
                ),
                unique_blocked AS (
                    SELECT
                        job_id,
                        min(root_dependency_id::text)::uuid AS root_dependency_id
                    FROM blocked
                    GROUP BY job_id
                )
                UPDATE bauer_rag_v3.jobs AS job
                SET state = 'cancelled',
                    leased_by = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    last_error_code = 'dependency_failed',
                    last_error_class = 'DependencyFailed',
                    last_error_message =
                        'dependency_failed:' || blocked.root_dependency_id::text,
                    finished_at = now(),
                    updated_at = now()
                FROM unique_blocked AS blocked
                WHERE job.job_id = blocked.job_id
                  AND job.state IN ('queued', 'retry_wait')
                RETURNING jsonb_build_object(
                    'job_id', job.job_id,
                    'state', job.state,
                    'last_error_code', job.last_error_code,
                    'last_error_message', job.last_error_message
                )
                """
            )
        )
        for job in cancelled:
            self._event(
                connection,
                str(job["job_id"]),
                "cancelled",
                actor=actor,
                details={"reason": job.get("last_error_message")},
            )
        return cancelled

    def _event(
        self,
        connection: Any,
        job_id: str,
        event_type: str,
        *,
        actor: str | None,
        details: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO bauer_rag_v3.job_events (
                job_id,
                event_type,
                actor,
                details
            )
            VALUES (%s::uuid, %s, %s, %s)
            """,
            (job_id, event_type, actor, self._jsonb(dict(details))),
        )

    def _jsonb(self, value: Any) -> Any:
        return self._jsonb_factory(value)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_provider()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            with resource as connection:
                yield connection
        else:
            yield resource


def _load_psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as error:
        raise PostgresJobDependencyError(
            "PostgreSQL durable jobs require psycopg 3"
        ) from error
    return psycopg


def _default_jsonb(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ModuleNotFoundError as error:
        raise PostgresJobDependencyError(
            "PostgreSQL durable jobs require psycopg 3"
        ) from error
    return Jsonb(value)


def _fetch_json(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    value: Any
    if isinstance(row, Mapping):
        value = next(iter(row.values())) if len(row) == 1 else dict(row)
    else:
        value = row[0]
    if not isinstance(value, Mapping):
        raise PostgresJobError("database did not return a JSON object")
    return dict(value)


def _fetch_json_rows(cursor: Any) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        if isinstance(row, Mapping):
            value = next(iter(row.values())) if len(row) == 1 else dict(row)
        else:
            value = row[0]
        if not isinstance(value, Mapping):
            raise PostgresJobError("database did not return a JSON object")
        values.append(dict(value))
    return values


def _stored_semantics(bundle: Mapping[str, Any]) -> dict[str, Any]:
    job = bundle.get("job")
    dependencies = bundle.get("dependencies", [])
    if not isinstance(job, Mapping) or not isinstance(dependencies, list):
        raise PostgresJobError("stored idempotency record has an invalid shape")
    return {
        "job_type": str(job.get("job_type")),
        "kb_id": _nullable_text(job.get("kb_id")),
        "release_id": _nullable_text(job.get("release_id")),
        "source_id": _nullable_text(job.get("source_id")),
        "source_version_id": _nullable_text(job.get("source_version_id")),
        "artifact_set_id": _nullable_text(job.get("artifact_set_id")),
        "parent_job_id": _nullable_text(job.get("parent_job_id")),
        "replay_of_job_id": _nullable_text(job.get("replay_of_job_id")),
        "payload": job.get("payload"),
        "priority": int(job.get("priority", 0)),
        "max_attempts": int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
        "dependencies": sorted(
            (
                {
                    "job_id": str(item["job_id"]),
                    "requirement": str(item["requirement"]),
                }
                for item in dependencies
            ),
            key=lambda item: (item["job_id"], item["requirement"]),
        ),
    }


def _stored_dependencies(values: Sequence[Any]) -> tuple[JobDependency, ...]:
    dependencies: list[JobDependency] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise PostgresJobError(
                "stored dead-letter dependency has an invalid shape"
            )
        try:
            dependencies.append(
                JobDependency(
                    value.get("job_id"),
                    str(value.get("requirement")),
                )
            )
        except (TypeError, ValueError) as error:
            raise PostgresJobError(
                "stored dead-letter dependency is invalid"
            ) from error
    return _dependencies(tuple(dependencies))


def _replay_semantics(
    source_job: Mapping[str, Any],
    *,
    replay_of_job_id: str,
    payload: Mapping[str, Any],
    dependencies: Sequence[JobDependency],
) -> dict[str, Any]:
    source_id = _optional_uuid(source_job.get("source_id"), "stored source_id")
    source_version_id = _optional_uuid(
        source_job.get("source_version_id"),
        "stored source_version_id",
    )
    artifact_set_id = _optional_uuid(
        source_job.get("artifact_set_id"),
        "stored artifact_set_id",
    )
    if source_version_id is not None and source_id is None:
        raise PostgresJobError(
            "stored replay source_version_id has no source_id"
        )
    if artifact_set_id is not None and source_version_id is None:
        raise PostgresJobError(
            "stored replay artifact_set_id has no source_version_id"
        )
    normalized_dependencies = tuple(dependencies)
    return {
        "queue_name": _required_name(
            source_job.get("queue_name"),
            "stored queue_name",
        ),
        "job_type": _required_name(
            source_job.get("job_type"),
            "stored job_type",
        ),
        "kb_id": _required_uuid(source_job.get("kb_id"), "stored kb_id"),
        "release_id": _optional_uuid(
            source_job.get("release_id"),
            "stored release_id",
        ),
        "source_id": source_id,
        "source_version_id": source_version_id,
        "artifact_set_id": artifact_set_id,
        "parent_job_id": _optional_uuid(
            source_job.get("parent_job_id"),
            "stored parent_job_id",
        ),
        "replay_of_job_id": _required_uuid(
            replay_of_job_id,
            "replay_of_job_id",
        ),
        "payload": dict(payload),
        "priority": _smallint(
            source_job.get("priority"),
            "stored priority",
        ),
        "max_attempts": _positive_smallint(
            source_job.get("max_attempts"),
            "stored max_attempts",
        ),
        "dependencies": [
            {"job_id": item.job_id, "requirement": item.requirement}
            for item in normalized_dependencies
        ],
    }


def _dependencies(
    values: Sequence[JobDependency | str | tuple[str, str]],
) -> tuple[JobDependency, ...]:
    normalized: dict[str, JobDependency] = {}
    for value in values:
        if isinstance(value, JobDependency):
            dependency = value
        elif isinstance(value, str):
            dependency = JobDependency(value)
        elif isinstance(value, tuple) and len(value) == 2:
            dependency = JobDependency(str(value[0]), str(value[1]))
        else:
            raise TypeError("dependencies must contain JobDependency, UUID, or (UUID, requirement)")
        existing = normalized.get(dependency.job_id)
        if existing is not None and existing.requirement != dependency.requirement:
            raise ValueError(
                "a dependency job cannot have conflicting completion requirements"
            )
        normalized[dependency.job_id] = dependency
    return tuple(sorted(normalized.values()))


def _lease_identity(job_id: str, worker_id: str, lease_token: str) -> tuple[str, str, str]:
    return (
        _required_uuid(job_id, "job_id"),
        _required_name(worker_id, "worker_id"),
        _required_uuid(lease_token, "lease_token"),
    )


def _required_uuid(value: Any, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{field} must be a UUID") from error


def _optional_uuid(value: Any, field: str) -> str | None:
    return None if value is None else _required_uuid(value, field)


def _required_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    clean = value.strip()
    if len(clean) > MAX_NAME_LENGTH:
        raise ValueError(f"{field} exceeds {MAX_NAME_LENGTH} characters")
    return clean


def _optional_name(value: Any, field: str) -> str | None:
    return None if value is None else _required_name(value, field)


def _smallint(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -32_768 <= value <= 32_767:
        raise ValueError(f"{field} must fit PostgreSQL smallint")
    return value


def _positive_smallint(value: Any, field: str) -> int:
    result = _smallint(value, field)
    if result < 1:
        raise ValueError(f"{field} must be positive")
    return result


def _positive_seconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86_400:
        raise ValueError(f"{field} must be between 1 and 86400")
    return value


def _retry_delay(attempt_count: int, *, backoff_seconds: int | None = None) -> int:
    if backoff_seconds is not None:
        if (
            isinstance(backoff_seconds, bool)
            or not isinstance(backoff_seconds, int)
            or not 0 <= backoff_seconds <= MAX_BACKOFF_SECONDS
        ):
            raise ValueError(
                f"backoff_seconds must be between 0 and {MAX_BACKOFF_SECONDS}"
            )
        return backoff_seconds
    return min(MAX_BACKOFF_SECONDS, 2 ** max(attempt_count - 1, 0))


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        result = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be JSON serializable") from error
    if not isinstance(result, dict):
        raise ValueError(f"{field} must be a JSON object")
    return result


def _error_message(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("error_message is required")
    return value.strip()[:MAX_ERROR_MESSAGE_LENGTH]


def _optional_sha256(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise ValueError("result_object_sha256 must be a 64-character hexadecimal digest")
    return value.lower()


def _nullable_text(value: Any) -> str | None:
    return None if value is None else str(value)
