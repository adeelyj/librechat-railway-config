from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .postgres_jobs import LeaseLostError
from .telemetry import record_job_outcome


class DurableQueue(Protocol):
    def claim(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> dict[str, Any]: ...

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        result: Mapping[str, Any] | None = None,
        result_object_sha256: str | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...


JobHandler = Callable[[dict[str, Any]], Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    claimed: bool
    job_id: str | None = None
    job_type: str | None = None
    outcome: str | None = None


class _LeaseHeartbeat:
    def __init__(
        self,
        *,
        queue: DurableQueue,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> None:
        self.queue = queue
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self.interval = max(1.0, lease_seconds / 3)
        self.stop_event = threading.Event()
        self.lost_error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{job_id}",
            daemon=True,
        )

    def __enter__(self) -> "_LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, *_args) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(self.interval * 2, 2))

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.queue.heartbeat(
                    self.job_id,
                    worker_id=self.worker_id,
                    lease_token=self.lease_token,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as exc:  # preserve lease loss for the main thread
                self.lost_error = exc
                self.stop_event.set()
                return


class CompilerWorker:
    def __init__(
        self,
        *,
        queue: DurableQueue,
        handlers: Mapping[str, JobHandler],
        worker_id: str,
        queue_name: str = "compiler",
        lease_seconds: int = 120,
        poll_seconds: float = 2.0,
        maintenance_interval_seconds: float = 30.0,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds < 3:
            raise ValueError("lease_seconds must be at least three")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if maintenance_interval_seconds <= 0:
            raise ValueError("maintenance_interval_seconds must be positive")
        self.queue = queue
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.queue_name = queue_name
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.maintenance_interval_seconds = maintenance_interval_seconds
        self._next_maintenance_at = 0.0
        self.stop_event = threading.Event()

    def run_once(self) -> WorkerResult:
        self._run_maintenance_if_due()
        job = self.queue.claim(
            queue_name=self.queue_name,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return WorkerResult(claimed=False)
        job_id = str(job["job_id"])
        job_type = str(job["job_type"])
        lease_token = str(job["lease_token"])
        handler = self.handlers.get(job_type)
        if handler is None:
            self.queue.fail(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                error_message=f"no handler is registered for job type {job_type!r}",
                error_code="unknown_job_type",
                error_class="WorkerConfigurationError",
                backoff_seconds=0,
            )
            record_job_outcome(
                job_id=job_id,
                job_type=job_type,
                queue_name=self.queue_name,
                outcome="failed_unknown_job_type",
                duration_ms=0,
            )
            return WorkerResult(True, job_id, job_type, "failed")

        started = time.perf_counter()
        heartbeat = _LeaseHeartbeat(
            queue=self.queue,
            job_id=job_id,
            worker_id=self.worker_id,
            lease_token=lease_token,
            lease_seconds=self.lease_seconds,
        )
        try:
            with heartbeat:
                payload = dict(job.get("payload") or {})
                # The database job identity is execution metadata, not caller
                # supplied manifest data.  Inject it after claim so persisted
                # artifacts retain an auditable FK to the attempt that built
                # them, and overwrite any stale/tampered payload value.
                payload["worker_job_id"] = job_id
                result = handler(payload) or {}
            if heartbeat.lost_error is not None:
                raise heartbeat.lost_error
            duration_ms = round((time.perf_counter() - started) * 1000)
            self.queue.succeed(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                result=dict(result),
                metrics={"duration_ms": duration_ms},
            )
            record_job_outcome(
                job_id=job_id,
                job_type=job_type,
                queue_name=self.queue_name,
                outcome="succeeded",
                duration_ms=duration_ms,
            )
            return WorkerResult(True, job_id, job_type, "succeeded")
        except LeaseLostError:
            # A stale worker must discard its result and must not overwrite the new owner.
            duration_ms = round((time.perf_counter() - started) * 1000)
            record_job_outcome(
                job_id=job_id,
                job_type=job_type,
                queue_name=self.queue_name,
                outcome="lease_lost",
                duration_ms=duration_ms,
            )
            return WorkerResult(True, job_id, job_type, "lease_lost")
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000)
            safe_message = str(exc).strip()[:2_000] or type(exc).__name__
            try:
                self.queue.fail(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    error_message=safe_message,
                    error_code="handler_error",
                    error_class=type(exc).__name__,
                    metrics={
                        "duration_ms": duration_ms,
                        "traceback_fingerprint": _traceback_fingerprint(exc),
                    },
                )
            except LeaseLostError:
                record_job_outcome(
                    job_id=job_id,
                    job_type=job_type,
                    queue_name=self.queue_name,
                    outcome="lease_lost",
                    duration_ms=duration_ms,
                )
                return WorkerResult(True, job_id, job_type, "lease_lost")
            record_job_outcome(
                job_id=job_id,
                job_type=job_type,
                queue_name=self.queue_name,
                outcome="failed",
                duration_ms=duration_ms,
            )
            return WorkerResult(True, job_id, job_type, "failed")

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            result = self.run_once()
            if not result.claimed:
                self.stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()

    def _run_maintenance_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_maintenance_at:
            return
        self._next_maintenance_at = now + self.maintenance_interval_seconds
        lease_reaper = getattr(self.queue, "reap_expired_leases", None)
        if callable(lease_reaper):
            lease_reaper(actor=f"{self.worker_id}:lease-reaper")
        else:
            in_memory_reaper = getattr(self.queue, "reap_expired", None)
            if callable(in_memory_reaper):
                in_memory_reaper()
        dependency_reaper = getattr(self.queue, "cancel_blocked_dependents", None)
        if callable(dependency_reaper):
            dependency_reaper(actor=f"{self.worker_id}:dependency-reaper")


def _traceback_fingerprint(exc: BaseException) -> str:
    import hashlib

    stack = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__, limit=20)
    )
    return hashlib.sha256(stack.encode("utf-8", errors="replace")).hexdigest()[:16]
