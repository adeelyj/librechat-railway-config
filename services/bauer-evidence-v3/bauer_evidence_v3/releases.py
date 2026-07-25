from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .ids import sha256_json
from .models import ArtifactStatus, ReleaseStatus


INDEPENDENT_GOLD_STATUS = "independent_bauer_verified"


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseSource:
    source_id: str
    source_version_id: str
    artifact_set_id: str
    artifact_status: ArtifactStatus
    action: str = "compiled"

    def __post_init__(self) -> None:
        if self.action not in {"compiled", "reused"}:
            raise ValueError("release source action must be compiled or reused")


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    manifest_sha256: str
    source_complete: bool
    citations_resolvable: bool
    projections_complete: bool
    blocking_qa_codes: tuple[str, ...] = ()
    hard_eval_failures: tuple[str, ...] = ()
    gold_status: str = "interim_codex_reviewed_requires_bauer_signoff"
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (
            self.source_complete
            and self.citations_resolvable
            and self.projections_complete
            and not self.blocking_qa_codes
            and not self.hard_eval_failures
        )


@dataclass(slots=True)
class ReleaseRecord:
    release_id: str
    tenant_id: str
    knowledge_base_id: str
    label: str
    expected_source_count: int
    compiler_fingerprint: str
    versions: dict[str, str]
    status: ReleaseStatus = ReleaseStatus.DRAFT
    sources: dict[str, ReleaseSource] = field(default_factory=dict)
    gate_report: ReleaseGateReport | None = None
    base_release_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    knowledge_base_id: str
    new_release_id: str
    previous_release_id: str | None
    actor: str
    reason: str
    production: bool
    activated_at: datetime


class InMemoryReleaseRegistry:
    """Thread-safe reference implementation of V3's release invariants.

    PostgreSQL is the production authority. This store makes the state machine independently
    testable and powers the local golden vertical slice without weakening publication rules.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._releases: dict[str, ReleaseRecord] = {}
        self._active_by_kb: dict[str, str] = {}
        self._activations: list[ActivationRecord] = []

    def create(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        label: str,
        expected_source_count: int,
        compiler_fingerprint: str,
        versions: dict[str, str],
        base_release_id: str | None = None,
        release_id: str | None = None,
    ) -> ReleaseRecord:
        if expected_source_count < 1:
            raise ValueError("expected_source_count must be positive")
        required_versions = {"compiler", "parser", "embedding", "prompt"}
        missing = required_versions.difference(versions)
        if missing:
            raise ValueError(f"missing release versions: {', '.join(sorted(missing))}")
        record = ReleaseRecord(
            release_id=release_id or str(uuid.uuid4()),
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            label=label,
            expected_source_count=expected_source_count,
            compiler_fingerprint=compiler_fingerprint,
            versions=dict(versions),
            base_release_id=base_release_id,
        )
        with self._lock:
            if record.release_id in self._releases:
                raise ReleaseError(f"release already exists: {record.release_id}")
            if base_release_id:
                base = self._require(base_release_id)
                if base.knowledge_base_id != knowledge_base_id:
                    raise ReleaseError("base release belongs to a different knowledge base")
            self._releases[record.release_id] = record
            return copy.deepcopy(record)

    def start_build(self, release_id: str) -> ReleaseRecord:
        with self._lock:
            record = self._require(release_id)
            self._require_status(record, ReleaseStatus.DRAFT)
            record.status = ReleaseStatus.BUILDING
            record.updated_at = datetime.now(UTC)
            return copy.deepcopy(record)

    def record_source(self, release_id: str, source: ReleaseSource) -> ReleaseRecord:
        with self._lock:
            record = self._require(release_id)
            self._require_status(record, ReleaseStatus.BUILDING)
            existing = record.sources.get(source.source_id)
            if existing and existing != source:
                raise ReleaseError(f"source membership changed during immutable build: {source.source_id}")
            record.sources[source.source_id] = source
            if len(record.sources) > record.expected_source_count:
                raise ReleaseError("release contains more sources than its immutable manifest")
            record.updated_at = datetime.now(UTC)
            return copy.deepcopy(record)

    def computed_manifest_sha256(self, release_id: str) -> str:
        with self._lock:
            record = self._require(release_id)
            manifest = {
                "release_id": record.release_id,
                "tenant_id": record.tenant_id,
                "knowledge_base_id": record.knowledge_base_id,
                "compiler_fingerprint": record.compiler_fingerprint,
                "versions": record.versions,
                "sources": [
                    {
                        "source_id": item.source_id,
                        "source_version_id": item.source_version_id,
                        "artifact_set_id": item.artifact_set_id,
                        "artifact_status": item.artifact_status.value,
                        "action": item.action,
                    }
                    for item in sorted(record.sources.values(), key=lambda value: value.source_id)
                ],
            }
            return sha256_json(manifest)

    def begin_validation(self, release_id: str) -> ReleaseRecord:
        with self._lock:
            record = self._require(release_id)
            self._require_status(record, ReleaseStatus.BUILDING)
            if len(record.sources) != record.expected_source_count:
                raise ReleaseError(
                    f"release is incomplete: {len(record.sources)}/{record.expected_source_count} sources"
                )
            invalid = [
                item.source_id
                for item in record.sources.values()
                if item.artifact_status is not ArtifactStatus.VALID
            ]
            if invalid:
                raise ReleaseError(f"release contains non-valid artifacts: {', '.join(sorted(invalid))}")
            record.status = ReleaseStatus.VALIDATING
            record.updated_at = datetime.now(UTC)
            return copy.deepcopy(record)

    def mark_ready(self, release_id: str, report: ReleaseGateReport) -> ReleaseRecord:
        with self._lock:
            record = self._require(release_id)
            self._require_status(record, ReleaseStatus.VALIDATING)
            expected_manifest = self.computed_manifest_sha256(release_id)
            if report.manifest_sha256 != expected_manifest:
                raise ReleaseError("release manifest checksum does not match canonical membership")
            if not report.passed:
                failures = [
                    *report.blocking_qa_codes,
                    *report.hard_eval_failures,
                ]
                raise ReleaseError(
                    "release validation failed"
                    + (f": {', '.join(failures)}" if failures else "")
                )
            record.gate_report = report
            record.status = ReleaseStatus.READY
            record.updated_at = datetime.now(UTC)
            return copy.deepcopy(record)

    def activate(
        self,
        release_id: str,
        *,
        actor: str,
        reason: str,
        production: bool = False,
    ) -> ActivationRecord:
        if not actor.strip() or not reason.strip():
            raise ValueError("activation requires an actor and reason")
        with self._lock:
            target = self._require(release_id)
            self._require_status(target, ReleaseStatus.READY)
            if target.gate_report is None:
                raise ReleaseError("ready release is missing its immutable gate report")
            if production and target.gate_report.gold_status != INDEPENDENT_GOLD_STATUS:
                raise ReleaseError("production activation requires independent Bauer gold verification")
            previous = self._active_by_kb.get(target.knowledge_base_id)
            if previous == release_id:
                raise ReleaseError("release is already active")
            self._active_by_kb[target.knowledge_base_id] = release_id
            activation = ActivationRecord(
                knowledge_base_id=target.knowledge_base_id,
                new_release_id=release_id,
                previous_release_id=previous,
                actor=actor,
                reason=reason,
                production=production,
                activated_at=datetime.now(UTC),
            )
            self._activations.append(activation)
            return activation

    def rollback(
        self,
        knowledge_base_id: str,
        target_release_id: str,
        *,
        actor: str,
        reason: str,
        production: bool = False,
    ) -> ActivationRecord:
        with self._lock:
            target = self._require(target_release_id)
            if target.knowledge_base_id != knowledge_base_id:
                raise ReleaseError("rollback target belongs to a different knowledge base")
            return self.activate(
                target_release_id,
                actor=actor,
                reason=reason,
                production=production,
            )
    def fail(self, release_id: str, error: str) -> ReleaseRecord:
        with self._lock:
            record = self._require(release_id)
            if self._active_by_kb.get(record.knowledge_base_id) == release_id:
                raise ReleaseError("an active release cannot be failed")
            if record.status in {ReleaseStatus.READY, ReleaseStatus.RETIRED}:
                raise ReleaseError(f"cannot fail release in state {record.status.value}")
            record.status = ReleaseStatus.FAILED
            record.error = error
            record.updated_at = datetime.now(UTC)
            return copy.deepcopy(record)

    def retire(self, release_id: str) -> ReleaseRecord:
        with self._lock:
            record = self._require(release_id)
            self._require_status(record, ReleaseStatus.READY)
            if self._active_by_kb.get(record.knowledge_base_id) == release_id:
                raise ReleaseError("active release must be rolled back before retirement")
            record.status = ReleaseStatus.RETIRED
            record.updated_at = datetime.now(UTC)
            return copy.deepcopy(record)

    def pin_active(self, knowledge_base_id: str) -> ReleaseRecord:
        with self._lock:
            release_id = self._active_by_kb.get(knowledge_base_id)
            if not release_id:
                raise ReleaseError(f"knowledge base has no active release: {knowledge_base_id}")
            record = self._require(release_id)
            if record.status is not ReleaseStatus.READY:
                raise ReleaseError("active-release pointer does not resolve to a ready release")
            return copy.deepcopy(record)

    def get(self, release_id: str) -> ReleaseRecord:
        with self._lock:
            return copy.deepcopy(self._require(release_id))

    def activation_history(self, knowledge_base_id: str) -> tuple[ActivationRecord, ...]:
        with self._lock:
            return tuple(
                item for item in self._activations if item.knowledge_base_id == knowledge_base_id
            )

    def _require(self, release_id: str) -> ReleaseRecord:
        try:
            return self._releases[release_id]
        except KeyError as exc:
            raise ReleaseError(f"unknown release: {release_id}") from exc

    @staticmethod
    def _require_status(record: ReleaseRecord, expected: ReleaseStatus) -> None:
        if record.status is not expected:
            raise ReleaseError(
                f"release {record.release_id} is {record.status.value}; expected {expected.value}"
            )
