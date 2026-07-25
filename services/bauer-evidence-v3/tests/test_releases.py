from __future__ import annotations

import threading
import unittest

from bauer_evidence_v3.models import ArtifactStatus, ReleaseStatus
from bauer_evidence_v3.releases import (
    INDEPENDENT_GOLD_STATUS,
    InMemoryReleaseRegistry,
    ReleaseError,
    ReleaseGateReport,
    ReleaseSource,
)


VERSIONS = {
    "compiler": "v3-test",
    "parser": "deterministic-test",
    "embedding": "none",
    "prompt": "v3-test",
}


class ReleaseRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = InMemoryReleaseRegistry()

    def build_ready(self, *, release_id: str, gold_status: str = "interim") -> str:
        self.registry.create(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label=release_id,
            expected_source_count=1,
            compiler_fingerprint="compiler:test",
            versions=VERSIONS,
            release_id=release_id,
        )
        self.registry.start_build(release_id)
        self.registry.record_source(
            release_id,
            ReleaseSource(
                source_id="source",
                source_version_id=f"{release_id}:source-version",
                artifact_set_id=f"{release_id}:artifact",
                artifact_status=ArtifactStatus.VALID,
            ),
        )
        self.registry.begin_validation(release_id)
        self.registry.mark_ready(
            release_id,
            ReleaseGateReport(
                manifest_sha256=self.registry.computed_manifest_sha256(release_id),
                source_complete=True,
                citations_resolvable=True,
                projections_complete=True,
                gold_status=gold_status,
            ),
        )
        return release_id

    def test_incomplete_release_cannot_begin_validation(self) -> None:
        self.registry.create(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label="candidate",
            expected_source_count=1,
            compiler_fingerprint="compiler:test",
            versions=VERSIONS,
            release_id="candidate",
        )
        self.registry.start_build("candidate")
        with self.assertRaisesRegex(ReleaseError, "incomplete"):
            self.registry.begin_validation("candidate")

    def test_quarantined_artifact_blocks_validation(self) -> None:
        self.registry.create(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label="candidate",
            expected_source_count=1,
            compiler_fingerprint="compiler:test",
            versions=VERSIONS,
            release_id="candidate",
        )
        self.registry.start_build("candidate")
        self.registry.record_source(
            "candidate",
            ReleaseSource("source", "version", "artifact", ArtifactStatus.QUARANTINED),
        )
        with self.assertRaisesRegex(ReleaseError, "non-valid"):
            self.registry.begin_validation("candidate")

    def test_manifest_drift_and_blocking_qa_reject_ready_state(self) -> None:
        release_id = "candidate"
        self.registry.create(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label=release_id,
            expected_source_count=1,
            compiler_fingerprint="compiler:test",
            versions=VERSIONS,
            release_id=release_id,
        )
        self.registry.start_build(release_id)
        self.registry.record_source(
            release_id,
            ReleaseSource("source", "version", "artifact", ArtifactStatus.VALID),
        )
        self.registry.begin_validation(release_id)
        with self.assertRaisesRegex(ReleaseError, "checksum"):
            self.registry.mark_ready(
                release_id,
                ReleaseGateReport("0" * 64, True, True, True),
            )
        with self.assertRaisesRegex(ReleaseError, "TABLE_GRID"):
            self.registry.mark_ready(
                release_id,
                ReleaseGateReport(
                    self.registry.computed_manifest_sha256(release_id),
                    True,
                    True,
                    True,
                    blocking_qa_codes=("TABLE_GRID",),
                ),
            )
        self.assertEqual(self.registry.get(release_id).status, ReleaseStatus.VALIDATING)

    def test_candidate_is_invisible_until_atomic_activation_and_can_roll_back(self) -> None:
        first = self.build_ready(release_id="r1")
        second = self.build_ready(release_id="r2")
        with self.assertRaises(ReleaseError):
            self.registry.pin_active("kb")
        self.registry.activate(first, actor="test", reason="initial development release")
        pinned = self.registry.pin_active("kb")
        self.assertEqual(pinned.release_id, first)
        self.registry.activate(second, actor="test", reason="blue-green candidate")
        self.assertEqual(self.registry.pin_active("kb").release_id, second)
        rollback = self.registry.rollback(
            "kb",
            first,
            actor="test",
            reason="regression detected",
        )
        self.assertEqual(rollback.previous_release_id, second)
        self.assertEqual(self.registry.pin_active("kb").release_id, first)
        self.assertEqual(len(self.registry.activation_history("kb")), 3)

    def test_production_activation_requires_independent_bauer_gold(self) -> None:
        release_id = self.build_ready(release_id="candidate", gold_status="interim")
        with self.assertRaisesRegex(ReleaseError, "independent Bauer"):
            self.registry.activate(
                release_id,
                actor="release-manager",
                reason="production",
                production=True,
            )
        verified = self.build_ready(
            release_id="verified",
            gold_status=INDEPENDENT_GOLD_STATUS,
        )
        self.registry.activate(
            verified,
            actor="release-manager",
            reason="signed promotion",
            production=True,
        )
        self.assertEqual(self.registry.pin_active("kb").release_id, verified)

    def test_concurrent_activation_never_corrupts_pointer(self) -> None:
        releases = [self.build_ready(release_id=f"r{index}") for index in range(4)]
        failures: list[Exception] = []

        def activate(release_id: str) -> None:
            try:
                self.registry.activate(
                    release_id,
                    actor="thread",
                    reason=f"activate {release_id}",
                )
            except Exception as exc:  # pragma: no cover - diagnostic collection
                failures.append(exc)

        threads = [threading.Thread(target=activate, args=(release_id,)) for release_id in releases]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(failures)
        self.assertIn(self.registry.pin_active("kb").release_id, releases)
        self.assertEqual(len(self.registry.activation_history("kb")), 4)

    def test_active_release_cannot_be_failed_or_retired(self) -> None:
        release_id = self.build_ready(release_id="active")
        self.registry.activate(release_id, actor="test", reason="activate")
        with self.assertRaises(ReleaseError):
            self.registry.fail(release_id, "should not happen")
        with self.assertRaises(ReleaseError):
            self.registry.retire(release_id)


if __name__ == "__main__":
    unittest.main()
