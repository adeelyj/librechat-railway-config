from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import JobState


class JobError(RuntimeError):
    pass


class LeaseLostError(JobError):
    pass


@dataclass(slots=True)
class JobRecord:
    job_id: str
    queue_name: str
    job_type: str
    idempotency_key: str
    payload: dict[str, Any]
    knowledge_base_id: str | None
    release_id: str | None
    source_version_id: str | None
    artifact_set_id: str | None
    priority: int
    max_attempts: int
    dependencies: tuple[str, ...]
    parent_job_id: str | None = None
    state: JobState = JobState.QUEUED
    attempt_count: int = 0
    available_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    leased_by: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result: dict[str, Any] | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InMemoryJobQueue:
    """At-least-once reference queue with lease-token guarded effects."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def enqueue(
        self,
        *,
        queue_name: str,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        knowledge_base_id: str | None = None,
        release_id: str | None = None,
        source_version_id: str | None = None,
        artifact_set_id: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        dependencies: tuple[str, ...] = (),
        available_at: datetime | None = None,
        parent_job_id: str | None = None,
    ) -> JobRecord:
        if not queue_name or not job_type or not idempotency_key:
            raise ValueError("queue_name, job_type, and idempotency_key are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        with self._lock:
            idempotency = (queue_name, idempotency_key)
            existing_id = self._idempotency.get(idempotency)
            if existing_id:
                existing = self._jobs[existing_id]
                comparable = (
                    existing.job_type,
                    existing.payload,
                    existing.knowledge_base_id,
                    existing.release_id,
                    existing.source_version_id,
                    existing.artifact_set_id,
                    existing.dependencies,
                )
                requested = (
                    job_type,
                    payload,
                    knowledge_base_id,
                    release_id,
                    source_version_id,
                    artifact_set_id,
                    dependencies,
                )
                if comparable != requested:
                    raise JobError("idempotency key was reused with different job semantics")
                return copy.deepcopy(existing)
            missing_dependencies = [job_id for job_id in dependencies if job_id not in self._jobs]
            if missing_dependencies:
                raise JobError(f"unknown dependencies: {', '.join(missing_dependencies)}")
            now = datetime.now(UTC)
            record = JobRecord(
                job_id=str(uuid.uuid4()),
                queue_name=queue_name,
                job_type=job_type,
                idempotency_key=idempotency_key,
                payload=copy.deepcopy(payload),
                knowledge_base_id=knowledge_base_id,
                release_id=release_id,
                source_version_id=source_version_id,
                artifact_set_id=artifact_set_id,
                priority=priority,
                max_attempts=max_attempts,
                dependencies=tuple(dependencies),
                parent_job_id=parent_job_id,
                available_at=available_at or now,
                created_at=now,
                updated_at=now,
            )
            self._jobs[record.job_id] = record
            self._idempotency[idempotency] = record.job_id
            return copy.deepcopy(record)

    def claim(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobRecord | None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = now or datetime.now(UTC)
        with self._lock:
            self._reap_expired_locked(now)
            self._cancel_impossible_dependencies_locked(now)
            candidates = [
                job
                for job in self._jobs.values()
                if job.queue_name == queue_name
                and job.state in {JobState.QUEUED, JobState.RETRY_WAIT}
                and job.available_at <= now
                and all(
                    self._jobs[parent].state is JobState.SUCCEEDED
                    for parent in job.dependencies
                )
            ]
            if not candidates:
                return None
            candidates.sort(
                key=lambda item: (-item.priority, item.available_at, item.created_at, item.job_id)
            )
            job = candidates[0]
            job.state = JobState.RUNNING
            job.attempt_count += 1
            job.leased_by = worker_id
            job.lease_token = str(uuid.uuid4())
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            job.updated_at = now
            return copy.deepcopy(job)

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobRecord:
        now = now or datetime.now(UTC)
        with self._lock:
            job = self._owned_live_job(job_id, worker_id, lease_token, now)
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return copy.deepcopy(job)

    def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        now = now or datetime.now(UTC)
        with self._lock:
            job = self._owned_live_job(job_id, worker_id, lease_token, now)
            job.state = JobState.SUCCEEDED
            job.result = copy.deepcopy(result or {})
            job.finished_at = now
            job.updated_at = now
            self._clear_lease(job)
            return copy.deepcopy(job)

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        error: str,
        now: datetime | None = None,
        backoff_seconds: int | None = None,
    ) -> JobRecord:
        now = now or datetime.now(UTC)
        with self._lock:
            job = self._owned_live_job(job_id, worker_id, lease_token, now)
            self._transition_failure(job, error=error, now=now, backoff_seconds=backoff_seconds)
            return copy.deepcopy(job)

    def cancel(self, job_id: str, *, reason: str, now: datetime | None = None) -> JobRecord:
        now = now or datetime.now(UTC)
        with self._lock:
            job = self._require(job_id)
            if job.state in {JobState.SUCCEEDED, JobState.DEAD, JobState.CANCELLED}:
                raise JobError(f"terminal job cannot be cancelled: {job.state.value}")
            job.state = JobState.CANCELLED
            job.last_error = reason
            job.finished_at = now
            job.updated_at = now
            self._clear_lease(job)
            self._cancel_impossible_dependencies_locked(now)
            return copy.deepcopy(job)

    def reap_expired(self, *, now: datetime | None = None) -> tuple[JobRecord, ...]:
        now = now or datetime.now(UTC)
        with self._lock:
            changed = self._reap_expired_locked(now)
            self._cancel_impossible_dependencies_locked(now)
            return tuple(copy.deepcopy(item) for item in changed)

    def replay_dead(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> JobRecord:
        with self._lock:
            original = self._require(job_id)
            if original.state is not JobState.DEAD:
                raise JobError("only dead jobs can be replayed")
            return self.enqueue(
                queue_name=original.queue_name,
                job_type=original.job_type,
                idempotency_key=idempotency_key,
                payload=original.payload,
                knowledge_base_id=original.knowledge_base_id,
                release_id=original.release_id,
                source_version_id=original.source_version_id,
                artifact_set_id=original.artifact_set_id,
                priority=original.priority,
                max_attempts=original.max_attempts,
                dependencies=original.dependencies,
                available_at=now,
                parent_job_id=original.job_id,
            )

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            return copy.deepcopy(self._require(job_id))

    def list(self, *, queue_name: str | None = None) -> tuple[JobRecord, ...]:
        with self._lock:
            jobs = [
                copy.deepcopy(item)
                for item in self._jobs.values()
                if queue_name is None or item.queue_name == queue_name
            ]
            return tuple(sorted(jobs, key=lambda item: (item.created_at, item.job_id)))

    def _owned_live_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
    ) -> JobRecord:
        job = self._require(job_id)
        if (
            job.state is not JobState.RUNNING
            or job.leased_by != worker_id
            or job.lease_token != lease_token
        ):
            raise LeaseLostError("worker does not own the current job lease")
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            raise LeaseLostError("job lease has expired")
        return job

    def _reap_expired_locked(self, now: datetime) -> list[JobRecord]:
        changed: list[JobRecord] = []
        for job in self._jobs.values():
            if (
                job.state is JobState.RUNNING
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
            ):
                self._transition_failure(job, error="lease_expired", now=now)
                changed.append(job)
        return changed

    def _transition_failure(
        self,
        job: JobRecord,
        *,
        error: str,
        now: datetime,
        backoff_seconds: int | None = None,
    ) -> None:
        job.last_error = error
        job.updated_at = now
        self._clear_lease(job)
        if job.attempt_count >= job.max_attempts:
            job.state = JobState.DEAD
            job.finished_at = now
            return
        job.state = JobState.RETRY_WAIT
        delay = backoff_seconds
        if delay is None:
            delay = min(3600, 2 ** max(job.attempt_count - 1, 0))
        job.available_at = now + timedelta(seconds=max(delay, 0))

    def _cancel_impossible_dependencies_locked(self, now: datetime) -> None:
        changed = True
        while changed:
            changed = False
            for job in self._jobs.values():
                if job.state not in {JobState.QUEUED, JobState.RETRY_WAIT}:
                    continue
                failed = [
                    parent
                    for parent in job.dependencies
                    if self._jobs[parent].state in {JobState.DEAD, JobState.CANCELLED}
                ]
                if failed:
                    job.state = JobState.CANCELLED
                    job.last_error = f"dependency_failed:{','.join(sorted(failed))}"
                    job.finished_at = now
                    job.updated_at = now
                    changed = True

    @staticmethod
    def _clear_lease(job: JobRecord) -> None:
        job.leased_by = None
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None

    def _require(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobError(f"unknown job: {job_id}") from exc
