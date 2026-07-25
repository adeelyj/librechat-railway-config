from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from bauer_evidence_v3.jobs import InMemoryJobQueue, JobError, LeaseLostError
from bauer_evidence_v3.models import JobState


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


class DurableJobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = InMemoryJobQueue()

    def enqueue(self, key: str = "source:parser:v1", **kwargs):
        kwargs.setdefault("available_at", NOW)
        return self.queue.enqueue(
            queue_name="compiler",
            job_type="compile",
            idempotency_key=key,
            payload={"source": "sha256"},
            **kwargs,
        )

    def test_idempotent_enqueue_returns_one_job_and_rejects_semantic_drift(self) -> None:
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first.job_id, second.job_id)
        with self.assertRaisesRegex(JobError, "different job semantics"):
            self.queue.enqueue(
                queue_name="compiler",
                job_type="compile",
                idempotency_key="source:parser:v1",
                payload={"source": "different"},
            )

    def test_only_one_worker_can_claim_a_job(self) -> None:
        self.enqueue()
        first = self.queue.claim(queue_name="compiler", worker_id="worker-a", now=NOW)
        second = self.queue.claim(queue_name="compiler", worker_id="worker-b", now=NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_stale_lease_token_cannot_heartbeat_complete_or_fail(self) -> None:
        self.enqueue()
        claimed = self.queue.claim(queue_name="compiler", worker_id="worker", now=NOW)
        assert claimed is not None
        for operation in ("heartbeat", "complete", "fail"):
            with self.subTest(operation=operation), self.assertRaises(LeaseLostError):
                kwargs = {
                    "worker_id": "worker",
                    "lease_token": "stale-token",
                    "now": NOW,
                }
                if operation == "fail":
                    kwargs["error"] = "failure"
                getattr(self.queue, operation)(claimed.job_id, **kwargs)

    def test_heartbeat_extends_lease_and_success_is_terminal(self) -> None:
        self.enqueue()
        claimed = self.queue.claim(
            queue_name="compiler",
            worker_id="worker",
            lease_seconds=10,
            now=NOW,
        )
        assert claimed is not None and claimed.lease_token
        heartbeat = self.queue.heartbeat(
            claimed.job_id,
            worker_id="worker",
            lease_token=claimed.lease_token,
            lease_seconds=20,
            now=NOW + timedelta(seconds=5),
        )
        self.assertEqual(heartbeat.lease_expires_at, NOW + timedelta(seconds=25))
        completed = self.queue.complete(
            claimed.job_id,
            worker_id="worker",
            lease_token=claimed.lease_token,
            result={"artifact_set_id": "artifact"},
            now=NOW + timedelta(seconds=6),
        )
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        self.assertIsNone(completed.lease_token)

    def test_expired_lease_retries_then_dead_letters_semantically(self) -> None:
        self.enqueue(max_attempts=2)
        first = self.queue.claim(
            queue_name="compiler",
            worker_id="worker-a",
            lease_seconds=5,
            now=NOW,
        )
        assert first is not None
        reaped = self.queue.reap_expired(now=NOW + timedelta(seconds=5))
        self.assertEqual(reaped[0].state, JobState.RETRY_WAIT)
        second = self.queue.claim(
            queue_name="compiler",
            worker_id="worker-b",
            lease_seconds=5,
            now=NOW + timedelta(seconds=6),
        )
        assert second is not None
        final = self.queue.reap_expired(now=NOW + timedelta(seconds=11))[0]
        self.assertEqual(final.state, JobState.DEAD)
        self.assertEqual(final.attempt_count, 2)
        self.assertEqual(final.last_error, "lease_expired")

    def test_dependency_must_succeed_before_claim(self) -> None:
        parent = self.enqueue("parent")
        child = self.enqueue("child", dependencies=(parent.job_id,))
        claimed_parent = self.queue.claim(
            queue_name="compiler",
            worker_id="worker",
            now=NOW,
        )
        assert claimed_parent is not None and claimed_parent.lease_token
        self.assertEqual(claimed_parent.job_id, parent.job_id)
        self.assertIsNone(
            self.queue.claim(queue_name="compiler", worker_id="another-worker", now=NOW)
        )
        self.queue.complete(
            parent.job_id,
            worker_id="worker",
            lease_token=claimed_parent.lease_token,
            now=NOW,
        )
        claimed_child = self.queue.claim(
            queue_name="compiler",
            worker_id="worker",
            now=NOW,
        )
        self.assertEqual(claimed_child.job_id, child.job_id)

    def test_dead_dependency_cancels_descendants(self) -> None:
        parent = self.enqueue("parent", max_attempts=1)
        child = self.enqueue("child", dependencies=(parent.job_id,))
        claimed = self.queue.claim(queue_name="compiler", worker_id="worker", now=NOW)
        assert claimed is not None and claimed.lease_token
        self.queue.fail(
            parent.job_id,
            worker_id="worker",
            lease_token=claimed.lease_token,
            error="parse failed",
            now=NOW,
        )
        self.queue.claim(queue_name="compiler", worker_id="another", now=NOW)
        cancelled = self.queue.get(child.job_id)
        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertIn(parent.job_id, cancelled.last_error)

    def test_dead_job_replay_requires_a_new_idempotency_key(self) -> None:
        original = self.enqueue(max_attempts=1)
        claimed = self.queue.claim(queue_name="compiler", worker_id="worker", now=NOW)
        assert claimed is not None and claimed.lease_token
        self.queue.fail(
            original.job_id,
            worker_id="worker",
            lease_token=claimed.lease_token,
            error="parser crash",
            now=NOW,
        )
        replay = self.queue.replay_dead(
            original.job_id,
            idempotency_key="source:parser:v1:replay:1",
            now=NOW,
        )
        self.assertNotEqual(replay.job_id, original.job_id)
        self.assertEqual(replay.parent_job_id, original.job_id)


if __name__ == "__main__":
    unittest.main()
