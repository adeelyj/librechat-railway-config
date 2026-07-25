from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.postgres_jobs import LeaseLostError  # noqa: E402
from bauer_evidence_v3.worker import CompilerWorker  # noqa: E402


class FakeQueue:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.succeeded = []
        self.failed = []
        self.heartbeats = []
        self.lose_on_success = False

    def claim(self, **_kwargs):
        return self.jobs.pop(0) if self.jobs else None

    def heartbeat(self, job_id, **kwargs):
        self.heartbeats.append((job_id, kwargs))
        return {}

    def succeed(self, job_id, **kwargs):
        if self.lose_on_success:
            raise LeaseLostError("stale")
        self.succeeded.append((job_id, kwargs))
        return {}

    def fail(self, job_id, **kwargs):
        self.failed.append((job_id, kwargs))
        return {}


def job(job_type="compile"):
    return {
        "job_id": "job-1",
        "job_type": job_type,
        "lease_token": "lease-1",
        "payload": {"source_object_key": "sha256/key"},
    }


class WorkerTests(unittest.TestCase):
    def worker(self, queue, handlers):
        return CompilerWorker(
            queue=queue,
            handlers=handlers,
            worker_id="worker-a",
            lease_seconds=3,
            poll_seconds=0.01,
        )

    def test_success_commits_result_with_metrics(self) -> None:
        queue = FakeQueue([job()])
        worker = self.worker(queue, {"compile": lambda payload: {"seen": payload}})
        result = worker.run_once()
        self.assertEqual(result.outcome, "succeeded")
        self.assertEqual(queue.succeeded[0][1]["result"]["seen"]["source_object_key"], "sha256/key")
        self.assertEqual(
            queue.succeeded[0][1]["result"]["seen"]["worker_job_id"],
            "job-1",
        )
        self.assertIn("duration_ms", queue.succeeded[0][1]["metrics"])

    def test_handler_failure_is_retried_by_durable_queue_without_payload_leak(self) -> None:
        queue = FakeQueue([job()])

        def fail(_payload):
            raise ValueError("parser rejected source")

        result = self.worker(queue, {"compile": fail}).run_once()
        self.assertEqual(result.outcome, "failed")
        failure = queue.failed[0][1]
        self.assertEqual(failure["error_class"], "ValueError")
        self.assertEqual(failure["error_message"], "parser rejected source")
        self.assertNotIn("source_object_key", str(failure))

    def test_unknown_job_type_is_failed_explicitly(self) -> None:
        queue = FakeQueue([job("unknown")])
        result = self.worker(queue, {}).run_once()
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(queue.failed[0][1]["error_code"], "unknown_job_type")

    def test_stale_worker_discards_result(self) -> None:
        queue = FakeQueue([job()])
        queue.lose_on_success = True
        result = self.worker(queue, {"compile": lambda _payload: {"artifact": "new"}}).run_once()
        self.assertEqual(result.outcome, "lease_lost")
        self.assertFalse(queue.failed)

    def test_empty_queue_does_not_run_handlers(self) -> None:
        queue = FakeQueue([])
        result = self.worker(queue, {"compile": lambda _payload: self.fail("not called")}).run_once()
        self.assertFalse(result.claimed)

    def test_worker_runs_lease_and_dependency_maintenance(self) -> None:
        class MaintenanceQueue(FakeQueue):
            def __init__(self):
                super().__init__([])
                self.maintenance = []

            def reap_expired_leases(self, **kwargs):
                self.maintenance.append(("leases", kwargs))
                return ()

            def cancel_blocked_dependents(self, **kwargs):
                self.maintenance.append(("dependencies", kwargs))
                return ()

        queue = MaintenanceQueue()
        worker = self.worker(queue, {})
        worker.run_once()
        worker.run_once()
        self.assertEqual([item[0] for item in queue.maintenance], ["leases", "dependencies"])
        self.assertEqual(
            queue.maintenance[0][1]["actor"],
            "worker-a:lease-reaper",
        )


if __name__ == "__main__":
    unittest.main()
