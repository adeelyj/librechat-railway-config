"""Bounded PostgreSQL administration for Bauer Evidence V3.

This module owns control-plane operations only. It can bootstrap one tenant and
knowledge base, create immutable release specifications, advance a release
through its build gates, and enqueue source compilation. It never changes
``active_releases`` directly and never activates a release as a side effect of
creation or validation. Activation and rollback both go through the
``bauer_rag_v3.activate_release`` database function so that the database remains
the final authority for authorization, evidence completeness, QA, and gold
review.

The bootstrap operation must run with a database role that is allowed to create
the first tenant/knowledge-base/admin grant (normally the schema owner or a
dedicated role with ``BYPASSRLS``). Subsequent operations still establish the
transaction-local tenant and principal context required by the RLS functions.

``psycopg`` is an optional deployment dependency and is imported lazily.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence

from .db_security import verify_runtime_database_role
from .postgres_jobs import DEFAULT_MAX_ATTEMPTS, PostgresJobQueue
from .telemetry import record_release_transition


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRINCIPAL_TYPES = frozenset({"user", "group", "service"})
KB_PERMISSIONS = frozenset({"read", "ingest", "admin"})
SOURCE_TYPES = frozenset(
    {
        "pdf",
        "html",
        "image",
        "text",
        "office_document",
        "structured_record",
        "synthetic_demo",
    }
)
COMPILABLE_SOURCE_TYPES = frozenset({"pdf", "html", "text"})


class PostgresAdminError(RuntimeError):
    """Base class for V3 administrative failures."""


class PostgresAdminDependencyError(PostgresAdminError):
    """The optional PostgreSQL runtime is unavailable."""


class ImmutableReleaseConflictError(PostgresAdminError):
    """A release UUID was reused for a different immutable specification."""


class ReleaseTransitionError(PostgresAdminError):
    """The requested release lifecycle transition is invalid."""


class ReleaseGateError(ReleaseTransitionError):
    """Database evidence, QA, or evaluation state does not satisfy a gate."""

    def __init__(self, target_status: str, failures: Sequence[str]) -> None:
        self.target_status = target_status
        self.failures = tuple(failures)
        super().__init__(
            f"release cannot transition to {target_status}: "
            + "; ".join(self.failures)
        )


@dataclass(frozen=True, slots=True)
class PrincipalBootstrap:
    principal_id: str
    principal_type: str
    external_subject: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "principal_id",
            _required_uuid(self.principal_id, "principal_id"),
        )
        if self.principal_type not in PRINCIPAL_TYPES:
            raise ValueError(
                "principal_type must be one of: "
                + ", ".join(sorted(PRINCIPAL_TYPES))
            )
        object.__setattr__(
            self,
            "external_subject",
            _required_text(self.external_subject, "external_subject"),
        )
        object.__setattr__(
            self,
            "display_name",
            _optional_text(self.display_name, "display_name"),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeBaseGrant:
    principal_id: str
    permission: str
    granted_by: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "principal_id",
            _required_uuid(self.principal_id, "grant principal_id"),
        )
        if self.permission not in KB_PERMISSIONS:
            raise ValueError(
                "permission must be one of: "
                + ", ".join(sorted(KB_PERMISSIONS))
            )
        object.__setattr__(
            self,
            "granted_by",
            _optional_text(self.granted_by, "granted_by"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseSpec:
    release_id: str
    kb_id: str
    manifest_sha256: str
    expected_source_count: int
    compiler_fingerprint: str
    parser_version: str
    embedding_model_version: str
    created_by: str
    based_on_release_id: str | None = None
    ocr_version: str | None = None
    fact_model_version: str | None = None
    prompt_version: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "release_id",
            _required_uuid(self.release_id, "release_id"),
        )
        object.__setattr__(self, "kb_id", _required_uuid(self.kb_id, "kb_id"))
        object.__setattr__(
            self,
            "based_on_release_id",
            _optional_uuid(self.based_on_release_id, "based_on_release_id"),
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _required_sha256(self.manifest_sha256, "manifest_sha256"),
        )
        object.__setattr__(
            self,
            "compiler_fingerprint",
            _required_sha256(
                self.compiler_fingerprint,
                "compiler_fingerprint",
            ),
        )
        if (
            isinstance(self.expected_source_count, bool)
            or not isinstance(self.expected_source_count, int)
            or self.expected_source_count <= 0
        ):
            raise ValueError("expected_source_count must be a positive integer")
        for name in (
            "parser_version",
            "embedding_model_version",
            "created_by",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        for name in (
            "ocr_version",
            "fact_model_version",
            "prompt_version",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "metadata",
            _json_object(self.metadata, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class CompileJobSpec:
    """One source-version compilation requested for a building release."""

    source_id: str
    source_version_id: str
    external_file_id: str
    source_name: str
    source_object_key: str
    source_type: str
    declared_media_type: str | None = None
    category_path: tuple[str, ...] = ()
    priority: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _required_uuid(self.source_id, "source_id"),
        )
        object.__setattr__(
            self,
            "source_version_id",
            _required_uuid(self.source_version_id, "source_version_id"),
        )
        for name in ("external_file_id", "source_name", "source_object_key"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        if self.source_type not in COMPILABLE_SOURCE_TYPES:
            raise ValueError(
                "source_type must be one of: "
                + ", ".join(sorted(COMPILABLE_SOURCE_TYPES))
            )
        object.__setattr__(
            self,
            "declared_media_type",
            _optional_text(self.declared_media_type, "declared_media_type"),
        )
        category_path = tuple(
            _required_text(value, "category_path item")
            for value in self.category_path
        )
        object.__setattr__(self, "category_path", category_path)
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not -32768 <= self.priority <= 32767
        ):
            raise ValueError("priority must fit a PostgreSQL smallint")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 32767
        ):
            raise ValueError("max_attempts must be a positive smallint")
        object.__setattr__(
            self,
            "payload",
            _json_object(self.payload, "payload"),
        )


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    tenant_id: str
    kb_id: str
    principal_ids: tuple[str, ...]
    grants: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class BuildStartResult:
    release: Mapping[str, Any]
    jobs: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ActivationResult:
    previous_release_id: str | None
    active_release_id: str


class PostgresReleaseAdmin:
    """Transactional administrative adapter for one tenant context."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any] | Any],
        *,
        tenant_id: str,
        principal_ids: Sequence[str],
        jsonb_factory: Callable[[Any], Any] | None = None,
        job_queue: PostgresJobQueue | Any | None = None,
        enforce_database_role: bool = False,
    ) -> None:
        if not callable(connection_provider):
            raise TypeError("connection_provider must be callable")
        self.tenant_id = _required_uuid(tenant_id, "tenant_id")
        normalized_principals = tuple(
            sorted(
                {
                    _required_uuid(value, "context principal_id")
                    for value in principal_ids
                }
            )
        )
        if not normalized_principals:
            raise ValueError("at least one context principal_id is required")
        self.principal_ids = normalized_principals
        self._connection_provider = connection_provider
        self._enforce_database_role = bool(enforce_database_role)
        self._jsonb_factory = jsonb_factory or _default_jsonb
        self._job_queue = (
            job_queue
            if job_queue is not None
            else PostgresJobQueue(
                self._contextual_connection,
                jsonb_factory=self._jsonb_factory,
            )
        )

    @classmethod
    def from_dsn(
        cls,
        database_url: str,
        *,
        tenant_id: str,
        principal_ids: Sequence[str],
        **kwargs: Any,
    ) -> "PostgresReleaseAdmin":
        """Build an adapter whose psycopg connection opens per operation."""

        database_url = _required_text(database_url, "database_url")

        def connect() -> Any:
            psycopg = _load_psycopg()
            return psycopg.connect(database_url)

        return cls(
            connect,
            tenant_id=tenant_id,
            principal_ids=principal_ids,
            enforce_database_role=True,
            **kwargs,
        )

    @classmethod
    def from_bootstrap_dsn(
        cls,
        database_url: str,
        *,
        tenant_id: str,
        principal_ids: Sequence[str],
        **kwargs: Any,
    ) -> "PostgresReleaseAdmin":
        """Build the one bootstrap-only schema-owner adapter.

        The first tenant and admin grant do not yet exist for RLS
        authorization. All normal release control uses :meth:`from_dsn`,
        which enforces the exact non-owner admin tier.
        """

        database_url = _required_text(database_url, "database_url")

        def connect() -> Any:
            psycopg = _load_psycopg()
            return psycopg.connect(database_url)

        return cls(
            connect,
            tenant_id=tenant_id,
            principal_ids=principal_ids,
            enforce_database_role=False,
            **kwargs,
        )

    def bootstrap_tenant_knowledge_base(
        self,
        *,
        external_key: str,
        tenant_display_name: str,
        kb_id: str,
        external_namespace: str,
        kb_name: str,
        principals: Sequence[PrincipalBootstrap],
        grants: Sequence[KnowledgeBaseGrant],
        kb_description: str | None = None,
        tenant_metadata: Mapping[str, Any] | None = None,
        kb_metadata: Mapping[str, Any] | None = None,
    ) -> BootstrapResult:
        """Idempotently bootstrap one tenant, knowledge base, and its grants."""

        external_key = _required_text(external_key, "external_key")
        tenant_display_name = _required_text(
            tenant_display_name,
            "tenant_display_name",
        )
        kb_id = _required_uuid(kb_id, "kb_id")
        external_namespace = _required_text(
            external_namespace,
            "external_namespace",
        )
        kb_name = _required_text(kb_name, "kb_name")
        kb_description = _optional_text(kb_description, "kb_description")
        tenant_metadata = _json_object(
            tenant_metadata or {},
            "tenant_metadata",
        )
        kb_metadata = _json_object(kb_metadata or {}, "kb_metadata")
        normalized_principals = _unique_principals(principals)
        normalized_grants = _unique_grants(grants)
        supplied_principal_ids = {
            principal.principal_id for principal in normalized_principals
        }
        unknown_grants = sorted(
            {
                grant.principal_id
                for grant in normalized_grants
                if grant.principal_id not in supplied_principal_ids
            }
        )
        if unknown_grants:
            raise ValueError(
                "every bootstrap grant must reference a supplied principal: "
                + ", ".join(unknown_grants)
            )
        actor_admins = {
            grant.principal_id
            for grant in normalized_grants
            if grant.permission == "admin"
        }.intersection(self.principal_ids)
        if not actor_admins:
            raise ValueError(
                "bootstrap must grant admin to at least one context principal"
            )

        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.tenants AS tenant (
                    tenant_id,
                    external_key,
                    display_name,
                    metadata
                )
                VALUES (%s::uuid, %s, %s, %s)
                ON CONFLICT (tenant_id)
                DO UPDATE
                   SET external_key = EXCLUDED.external_key,
                       display_name = EXCLUDED.display_name,
                       metadata = EXCLUDED.metadata,
                       updated_at = now()
                """,
                (
                    self.tenant_id,
                    external_key,
                    tenant_display_name,
                    self._jsonb(tenant_metadata),
                ),
            )
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.knowledge_bases AS kb (
                    kb_id,
                    tenant_id,
                    external_namespace,
                    name,
                    description,
                    metadata
                )
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s)
                ON CONFLICT (kb_id)
                DO UPDATE
                   SET external_namespace = EXCLUDED.external_namespace,
                       name = EXCLUDED.name,
                       description = EXCLUDED.description,
                       metadata = EXCLUDED.metadata,
                       updated_at = now()
                """,
                (
                    kb_id,
                    self.tenant_id,
                    external_namespace,
                    kb_name,
                    kb_description,
                    self._jsonb(kb_metadata),
                ),
            )
            for principal in normalized_principals:
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.principals AS principal (
                        principal_id,
                        tenant_id,
                        principal_type,
                        external_subject,
                        display_name
                    )
                    VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                    ON CONFLICT (principal_id)
                    DO UPDATE
                       SET principal_type = EXCLUDED.principal_type,
                           external_subject = EXCLUDED.external_subject,
                           display_name = EXCLUDED.display_name
                    """,
                    (
                        principal.principal_id,
                        self.tenant_id,
                        principal.principal_type,
                        principal.external_subject,
                        principal.display_name,
                    ),
                )
            for grant in normalized_grants:
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.kb_grants AS grant_row (
                        tenant_id,
                        kb_id,
                        principal_id,
                        permission,
                        granted_by
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s)
                    ON CONFLICT (kb_id, principal_id, permission)
                    DO UPDATE
                       SET tenant_id = EXCLUDED.tenant_id,
                           granted_by = EXCLUDED.granted_by
                    """,
                    (
                        self.tenant_id,
                        kb_id,
                        grant.principal_id,
                        grant.permission,
                        grant.granted_by,
                    ),
                )

        return BootstrapResult(
            tenant_id=self.tenant_id,
            kb_id=kb_id,
            principal_ids=tuple(
                principal.principal_id for principal in normalized_principals
            ),
            grants=tuple(
                (grant.principal_id, grant.permission)
                for grant in normalized_grants
            ),
        )

    def create_release(self, spec: ReleaseSpec) -> dict[str, Any]:
        """Insert a draft release once and reject UUID/specification drift."""

        if not isinstance(spec, ReleaseSpec):
            raise TypeError("spec must be a ReleaseSpec")
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            inserted = _fetch_json(
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.knowledge_releases AS release_row (
                        release_id,
                        kb_id,
                        status,
                        based_on_release_id,
                        manifest_sha256,
                        expected_source_count,
                        compiler_fingerprint,
                        parser_version,
                        ocr_version,
                        fact_model_version,
                        embedding_model_version,
                        prompt_version,
                        metadata,
                        created_by
                    )
                    VALUES (
                        %s::uuid,
                        %s::uuid,
                        'draft',
                        %s::uuid,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s
                    )
                    ON CONFLICT (release_id) DO NOTHING
                    RETURNING to_jsonb(release_row)
                    """,
                    (
                        spec.release_id,
                        spec.kb_id,
                        spec.based_on_release_id,
                        spec.manifest_sha256,
                        spec.expected_source_count,
                        spec.compiler_fingerprint,
                        spec.parser_version,
                        spec.ocr_version,
                        spec.fact_model_version,
                        spec.embedding_model_version,
                        spec.prompt_version,
                        self._jsonb(spec.metadata),
                        spec.created_by,
                    ),
                )
            )
            if inserted is not None:
                return inserted
            existing = _fetch_json(
                connection.execute(
                    """
                    SELECT to_jsonb(release_row)
                    FROM bauer_rag_v3.knowledge_releases AS release_row
                    WHERE release_row.release_id = %s::uuid
                    FOR SHARE
                    """,
                    (spec.release_id,),
                )
            )
            if existing is None:
                raise PostgresAdminError(
                    "release conflict did not resolve to an existing row"
                )
            if _release_semantics(existing) != _release_spec_semantics(spec):
                raise ImmutableReleaseConflictError(
                    "release_id was reused with a different immutable specification"
                )
            return existing

    def start_build(
        self,
        release_id: str,
        *,
        compile_jobs: Sequence[CompileJobSpec] = (),
    ) -> BuildStartResult:
        """Advance draft to building, then idempotently enqueue requested work."""

        release_id = _required_uuid(release_id, "release_id")
        release = self._transition_status(
            release_id,
            expected_status="draft",
            target_status="building",
        )
        jobs = self.enqueue_compile_jobs(release_id, compile_jobs)
        return BuildStartResult(release=release, jobs=jobs)

    def enqueue_compile_jobs(
        self,
        release_id: str,
        compile_jobs: Sequence[CompileJobSpec],
    ) -> tuple[Mapping[str, Any], ...]:
        """Enqueue immutable, release-scoped source compilation requests."""

        release_id = _required_uuid(release_id, "release_id")
        normalized_jobs = _unique_compile_jobs(compile_jobs)
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release = self._load_release_for_update(connection, release_id)
            if str(release.get("status")) != "building":
                raise ReleaseTransitionError(
                    "compile jobs may only be enqueued for a building release"
                )
            compiler_fingerprint = _required_sha256(
                release.get("compiler_fingerprint"),
                "stored compiler_fingerprint",
            )
            ocr_version = _nullable_text(release.get("ocr_version"))
            fact_model_version = _nullable_text(
                release.get("fact_model_version")
            )
            kb_id = _required_uuid(release.get("kb_id"), "stored kb_id")

        enqueued: list[Mapping[str, Any]] = []
        for request in normalized_jobs:
            payload = dict(request.payload)
            payload.update(
                {
                    "tenant_id": self.tenant_id,
                    "knowledge_base_id": kb_id,
                    "release_id": release_id,
                    "source_id": request.source_id,
                    "source_version_id": request.source_version_id,
                    "external_file_id": request.external_file_id,
                    "source_name": request.source_name,
                    "source_object_key": request.source_object_key,
                    "source_type": request.source_type,
                    "declared_media_type": request.declared_media_type,
                    "category_path": list(request.category_path),
                    "compiler_fingerprint": compiler_fingerprint,
                    "ocr_version": ocr_version,
                    "fact_model_version": fact_model_version,
                }
            )
            enqueued.append(
                self._job_queue.enqueue(
                    queue_name="compiler",
                    job_type="compile",
                    kb_id=kb_id,
                    release_id=release_id,
                    # A compile job is what registers the immutable source and
                    # source-version rows.  Keep their deterministic IDs in the
                    # payload, but do not populate the optional job foreign
                    # keys before those registry rows exist.
                    source_id=None,
                    source_version_id=None,
                    idempotency_key=(
                        f"release:{release_id}:compile:"
                        f"{request.source_version_id}:{compiler_fingerprint}"
                    ),
                    payload=payload,
                    priority=request.priority,
                    max_attempts=request.max_attempts,
                )
            )
        return tuple(enqueued)

    def begin_validation(self, release_id: str) -> dict[str, Any]:
        """Advance building to validating after compilation/evidence gates pass."""

        release_id = _required_uuid(release_id, "release_id")
        transitioned = False
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release = self._load_release_for_update(connection, release_id)
            status = str(release.get("status"))
            if status == "validating":
                updated = release
            elif status != "building":
                raise ReleaseTransitionError(
                    f"expected release status building, found {status}"
                )
            else:
                gate = self._evidence_gate(connection, release_id)
                failures = _evidence_failures(gate)
                if failures:
                    raise ReleaseGateError("validating", failures)
                updated = self._update_status(
                    connection,
                    release_id,
                    expected_status="building",
                    target_status="validating",
                    timestamp_column="validation_started_at",
                )
                transitioned = True
        if transitioned:
            record_release_transition(
                release_id=release_id,
                previous_status="building",
                target_status="validating",
            )
        return updated

    def mark_ready(self, release_id: str) -> dict[str, Any]:
        """Mark validating release ready after evidence, QA, and eval gates pass."""

        release_id = _required_uuid(release_id, "release_id")
        transitioned = False
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release = self._load_release_for_update(connection, release_id)
            status = str(release.get("status"))
            if status == "ready":
                updated = release
            elif status != "validating":
                raise ReleaseTransitionError(
                    f"expected release status validating, found {status}"
                )
            else:
                evidence = self._evidence_gate(connection, release_id)
                failures = list(_evidence_failures(evidence))
                validation = self._validation_gate(connection, release_id)
                if int(validation.get("unresolved_blocker_count", 0)) != 0:
                    failures.append("unresolved blocking QA checks")
                if int(validation.get("passing_verified_eval_count", 0)) < 1:
                    failures.append("no passing evaluation against verified gold")
                if failures:
                    raise ReleaseGateError("ready", failures)
                updated = self._update_status(
                    connection,
                    release_id,
                    expected_status="validating",
                    target_status="ready",
                    timestamp_column="ready_at",
                )
                transitioned = True
        if transitioned:
            record_release_transition(
                release_id=release_id,
                previous_status="validating",
                target_status="ready",
            )
        return updated

    def mark_failed(
        self,
        release_id: str,
        *,
        error: str,
    ) -> dict[str, Any]:
        """Explicitly stop a non-ready release and record its terminal error."""

        release_id = _required_uuid(release_id, "release_id")
        error = _required_text(error, "error")
        transitioned_from: str | None = None
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release = self._load_release_for_update(connection, release_id)
            status = str(release.get("status"))
            if status == "failed":
                updated = release
            elif status not in {"draft", "building", "validating"}:
                raise ReleaseTransitionError(
                    "only a draft, building, or validating release can be "
                    f"marked failed; found {status}"
                )
            else:
                self._assert_release_not_active(connection, release_id)
                updated = self._update_status(
                    connection,
                    release_id,
                    expected_status=status,
                    target_status="failed",
                    timestamp_column="failed_at",
                    error=error,
                )
                transitioned_from = status
        if transitioned_from is not None:
            record_release_transition(
                release_id=release_id,
                previous_status=transitioned_from,
                target_status="failed",
            )
        return updated

    def replay_dead_letter(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        actor_principal_id: str,
    ) -> dict[str, Any]:
        """Replay one dead job through the contextual, audited queue path."""

        actor_principal_id = _required_uuid(
            actor_principal_id,
            "actor_principal_id",
        )
        if actor_principal_id not in self.principal_ids:
            raise ValueError(
                "actor_principal_id must be present in the database context"
            )
        return self._job_queue.replay_dead_letter(
            _required_uuid(job_id, "job_id"),
            idempotency_key=_required_text(
                idempotency_key,
                "idempotency_key",
            ),
            actor=actor_principal_id,
        )

    def retire_release(self, release_id: str) -> dict[str, Any]:
        """Retire an inactive ready release without changing the active pointer."""

        release_id = _required_uuid(release_id, "release_id")
        transitioned = False
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release = self._load_release_for_update(connection, release_id)
            status = str(release.get("status"))
            if status == "retired":
                updated = release
            elif status != "ready":
                raise ReleaseTransitionError(
                    f"only a ready release can be retired; found {status}"
                )
            else:
                self._assert_release_not_active(connection, release_id)
                updated = self._update_status(
                    connection,
                    release_id,
                    expected_status="ready",
                    target_status="retired",
                    timestamp_column="retired_at",
                )
                transitioned = True
        if transitioned:
            record_release_transition(
                release_id=release_id,
                previous_status="ready",
                target_status="retired",
            )
        return updated

    def activate_release(
        self,
        *,
        kb_id: str,
        release_id: str,
        actor_principal_id: str,
        reason: str,
        production: bool = True,
    ) -> ActivationResult:
        """Explicitly activate a ready release through the database gate."""

        return self._activate(
            kb_id=kb_id,
            release_id=release_id,
            actor_principal_id=actor_principal_id,
            reason=reason,
            production=production,
            operation="activation",
        )

    def rollback_release(
        self,
        *,
        kb_id: str,
        target_release_id: str,
        actor_principal_id: str,
        reason: str,
        production: bool = True,
    ) -> ActivationResult:
        """Explicitly repoint the active pointer to an earlier ready release."""

        return self._activate(
            kb_id=kb_id,
            release_id=target_release_id,
            actor_principal_id=actor_principal_id,
            reason=reason,
            production=production,
            operation="rollback",
        )

    def _activate(
        self,
        *,
        kb_id: str,
        release_id: str,
        actor_principal_id: str,
        reason: str,
        production: bool,
        operation: str,
    ) -> ActivationResult:
        kb_id = _required_uuid(kb_id, "kb_id")
        release_id = _required_uuid(release_id, "release_id")
        actor_principal_id = _required_uuid(
            actor_principal_id,
            "actor_principal_id",
        )
        if actor_principal_id not in self.principal_ids:
            raise ValueError(
                "actor_principal_id must be present in the database context"
            )
        reason = _required_text(reason, "reason")
        if not isinstance(production, bool):
            raise TypeError("production must be a boolean")
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            row = connection.execute(
                """
                SELECT
                    previous_release_id::text,
                    active_release_id::text
                FROM bauer_rag_v3.activate_release(
                    %s::uuid,
                    %s::uuid,
                    %s::uuid,
                    %s,
                    %s
                )
                """,
                (
                    kb_id,
                    release_id,
                    actor_principal_id,
                    reason,
                    production,
                ),
            ).fetchone()
            if row is None:
                raise PostgresAdminError(
                    "database activation function returned no result"
                )
            if isinstance(row, Mapping):
                previous = row.get("previous_release_id")
                active = row.get("active_release_id")
            else:
                previous, active = row
            active_id = _required_uuid(active, "active_release_id")
            if active_id != release_id:
                raise PostgresAdminError(
                    "database activation result did not match the target release"
                )
            result = ActivationResult(
                previous_release_id=_optional_uuid(
                    previous,
                    "previous_release_id",
                ),
                active_release_id=active_id,
            )
        record_release_transition(
            release_id=active_id,
            previous_status="ready",
            target_status="active_pointer",
            operation=operation,
            previous_release_id=result.previous_release_id,
        )
        return result

    def _transition_status(
        self,
        release_id: str,
        *,
        expected_status: str,
        target_status: str,
    ) -> dict[str, Any]:
        transitioned = False
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release = self._load_release_for_update(connection, release_id)
            status = str(release.get("status"))
            if status == target_status:
                updated = release
            elif status != expected_status:
                raise ReleaseTransitionError(
                    f"expected release status {expected_status}, found {status}"
                )
            else:
                updated = self._update_status(
                    connection,
                    release_id,
                    expected_status=expected_status,
                    target_status=target_status,
                )
                transitioned = True
        if transitioned:
            record_release_transition(
                release_id=release_id,
                previous_status=expected_status,
                target_status=target_status,
            )
        return updated

    def _load_release_for_update(
        self,
        connection: Any,
        release_id: str,
    ) -> dict[str, Any]:
        release = _fetch_json(
            connection.execute(
                """
                SELECT to_jsonb(release_row)
                FROM bauer_rag_v3.knowledge_releases AS release_row
                WHERE release_row.release_id = %s::uuid
                FOR UPDATE
                """,
                (release_id,),
            )
        )
        if release is None:
            raise ReleaseTransitionError(f"unknown release: {release_id}")
        return release

    def _assert_release_not_active(
        self,
        connection: Any,
        release_id: str,
    ) -> None:
        # The caller already holds a FOR UPDATE lock on the release row.
        # activate_release() must acquire that same row lock before it can
        # change the active pointer, so this read is race-safe without taking
        # a lock on active_releases.  Keeping it as a plain SELECT also
        # preserves the runtime admin's deliberate read-only privilege on the
        # pointer table.
        active = connection.execute(
            """
            SELECT 1
            FROM bauer_rag_v3.active_releases AS active
            WHERE active.release_id = %s::uuid
            """,
            (release_id,),
        ).fetchone()
        if active is not None:
            raise ReleaseTransitionError(
                "active release must be rolled back before failure or retirement"
            )

    def _update_status(
        self,
        connection: Any,
        release_id: str,
        *,
        expected_status: str,
        target_status: str,
        timestamp_column: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        assignments = ["status = %s"]
        parameters: list[Any] = [target_status]
        if timestamp_column is not None:
            if timestamp_column not in {
                "validation_started_at",
                "ready_at",
                "failed_at",
                "retired_at",
            }:
                raise ValueError("unsupported release timestamp column")
            assignments.append(
                f"{timestamp_column} = COALESCE({timestamp_column}, now())"
            )
        if error is not None:
            assignments.append("error = %s")
            parameters.append(error)
        parameters.extend((release_id, expected_status))
        updated = _fetch_json(
            connection.execute(
                f"""
                UPDATE bauer_rag_v3.knowledge_releases AS release_row
                SET {", ".join(assignments)}
                WHERE release_row.release_id = %s::uuid
                  AND release_row.status = %s
                RETURNING to_jsonb(release_row)
                """,
                tuple(parameters),
            )
        )
        if updated is None:
            raise ReleaseTransitionError(
                f"concurrent release transition prevented {target_status}"
            )
        return updated

    def _evidence_gate(
        self,
        connection: Any,
        release_id: str,
    ) -> dict[str, Any]:
        gate = _fetch_json(
            connection.execute(
                """
                SELECT jsonb_build_object(
                    'expected_source_count',
                    release_row.expected_source_count,
                    'actual_source_count',
                    (
                        SELECT count(*)
                        FROM bauer_rag_v3.release_sources AS member
                        WHERE member.release_id = release_row.release_id
                    ),
                    'unfinished_job_count',
                    (
                        SELECT count(*)
                        FROM bauer_rag_v3.jobs AS job_row
                        WHERE job_row.release_id = release_row.release_id
                          AND job_row.state <> 'succeeded'
                    ),
                    'invalid_artifact_count',
                    (
                        SELECT count(*)
                        FROM bauer_rag_v3.release_sources AS member
                        JOIN bauer_rag_v3.artifact_sets AS artifact
                          ON artifact.artifact_set_id = member.artifact_set_id
                        WHERE member.release_id = release_row.release_id
                          AND artifact.status <> 'valid'
                    ),
                    'missing_citable_source_count',
                    (
                        SELECT count(*)
                        FROM bauer_rag_v3.release_sources AS member
                        WHERE member.release_id = release_row.release_id
                          AND NOT EXISTS (
                              SELECT 1
                              FROM bauer_rag_v3.search_units AS unit
                              WHERE unit.release_id = member.release_id
                                AND unit.source_id = member.source_id
                                AND unit.is_citable
                          )
                    )
                )
                FROM bauer_rag_v3.knowledge_releases AS release_row
                WHERE release_row.release_id = %s::uuid
                """,
                (release_id,),
            )
        )
        if gate is None:
            raise ReleaseTransitionError(f"unknown release: {release_id}")
        return gate

    def _validation_gate(
        self,
        connection: Any,
        release_id: str,
    ) -> dict[str, Any]:
        gate = _fetch_json(
            connection.execute(
                """
                SELECT jsonb_build_object(
                    'unresolved_blocker_count',
                    (
                        SELECT count(*)
                        FROM bauer_rag_v3.qa_checks AS check_row
                        WHERE check_row.release_id = release_row.release_id
                          AND check_row.severity = 'blocker'
                          AND check_row.status = 'fail'
                          AND check_row.resolved_at IS NULL
                    ),
                    'passing_verified_eval_count',
                    (
                        SELECT count(*)
                        FROM bauer_rag_v3.eval_runs AS eval_run
                        JOIN bauer_rag_v3.eval_suites AS suite
                          ON suite.eval_suite_id = eval_run.eval_suite_id
                        WHERE eval_run.release_id = release_row.release_id
                          AND eval_run.status = 'succeeded'
                          AND eval_run.passed
                          AND cardinality(eval_run.hard_failures) = 0
                          AND suite.gold_status IN (
                              'verified',
                              'independent_bauer_verified'
                          )
                    )
                )
                FROM bauer_rag_v3.knowledge_releases AS release_row
                WHERE release_row.release_id = %s::uuid
                """,
                (release_id,),
            )
        )
        if gate is None:
            raise ReleaseTransitionError(f"unknown release: {release_id}")
        return gate

    def _set_context(self, connection: Any) -> None:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (self.tenant_id,),
        )
        connection.execute(
            "SELECT set_config('app.principal_ids', %s, true)",
            (json.dumps(self.principal_ids, separators=(",", ":")),),
        )

    def _jsonb(self, value: Any) -> Any:
        return self._jsonb_factory(value)

    @contextmanager
    def _contextual_connection(self) -> Iterator[Any]:
        """Provide the job queue an outer transaction with the same RLS context."""

        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            yield connection

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_provider()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            with resource as connection:
                if self._enforce_database_role:
                    verify_runtime_database_role(
                        connection,
                        required_group_role="bauer_rag_v3_admin",
                    )
                yield connection
        else:
            if self._enforce_database_role:
                verify_runtime_database_role(
                    resource,
                    required_group_role="bauer_rag_v3_admin",
                )
            yield resource


def _load_psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as error:
        raise PostgresAdminDependencyError(
            "PostgreSQL V3 administration requires psycopg 3"
        ) from error
    return psycopg


def _default_jsonb(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ModuleNotFoundError as error:
        raise PostgresAdminDependencyError(
            "PostgreSQL V3 administration requires psycopg 3"
        ) from error
    return Jsonb(value)


def _fetch_json(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        value: Any = next(iter(row.values())) if len(row) == 1 else dict(row)
    else:
        value = row[0]
    if not isinstance(value, Mapping):
        raise PostgresAdminError("database did not return a JSON object")
    return dict(value)


def _release_spec_semantics(spec: ReleaseSpec) -> dict[str, Any]:
    return {
        "release_id": spec.release_id,
        "kb_id": spec.kb_id,
        "based_on_release_id": spec.based_on_release_id,
        "manifest_sha256": spec.manifest_sha256,
        "expected_source_count": spec.expected_source_count,
        "compiler_fingerprint": spec.compiler_fingerprint,
        "parser_version": spec.parser_version,
        "ocr_version": spec.ocr_version,
        "fact_model_version": spec.fact_model_version,
        "embedding_model_version": spec.embedding_model_version,
        "prompt_version": spec.prompt_version,
        "metadata": dict(spec.metadata),
        "created_by": spec.created_by,
    }


def _release_semantics(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "release_id": _required_uuid(row.get("release_id"), "stored release_id"),
        "kb_id": _required_uuid(row.get("kb_id"), "stored kb_id"),
        "based_on_release_id": _optional_uuid(
            row.get("based_on_release_id"),
            "stored based_on_release_id",
        ),
        "manifest_sha256": str(row.get("manifest_sha256")),
        "expected_source_count": int(row.get("expected_source_count")),
        "compiler_fingerprint": str(row.get("compiler_fingerprint")),
        "parser_version": str(row.get("parser_version")),
        "ocr_version": _nullable_text(row.get("ocr_version")),
        "fact_model_version": _nullable_text(row.get("fact_model_version")),
        "embedding_model_version": str(row.get("embedding_model_version")),
        "prompt_version": _nullable_text(row.get("prompt_version")),
        "metadata": dict(row.get("metadata") or {}),
        "created_by": str(row.get("created_by")),
    }


def _evidence_failures(gate: Mapping[str, Any]) -> tuple[str, ...]:
    failures: list[str] = []
    expected = int(gate.get("expected_source_count", 0))
    actual = int(gate.get("actual_source_count", 0))
    if actual != expected:
        failures.append(f"release has {actual} sources; expected {expected}")
    if int(gate.get("unfinished_job_count", 0)) != 0:
        failures.append("release has unfinished or failed jobs")
    if int(gate.get("invalid_artifact_count", 0)) != 0:
        failures.append("release contains a non-valid artifact set")
    if int(gate.get("missing_citable_source_count", 0)) != 0:
        failures.append("release contains a source without citable evidence")
    return tuple(failures)


def _unique_principals(
    principals: Sequence[PrincipalBootstrap],
) -> tuple[PrincipalBootstrap, ...]:
    values: dict[str, PrincipalBootstrap] = {}
    for principal in principals:
        if not isinstance(principal, PrincipalBootstrap):
            raise TypeError("principals must contain PrincipalBootstrap values")
        existing = values.get(principal.principal_id)
        if existing is not None and existing != principal:
            raise ValueError("principal_id has conflicting bootstrap definitions")
        values[principal.principal_id] = principal
    if not values:
        raise ValueError("at least one principal is required")
    return tuple(values[key] for key in sorted(values))


def _unique_grants(
    grants: Sequence[KnowledgeBaseGrant],
) -> tuple[KnowledgeBaseGrant, ...]:
    values: dict[tuple[str, str], KnowledgeBaseGrant] = {}
    for grant in grants:
        if not isinstance(grant, KnowledgeBaseGrant):
            raise TypeError("grants must contain KnowledgeBaseGrant values")
        key = (grant.principal_id, grant.permission)
        existing = values.get(key)
        if existing is not None and existing != grant:
            raise ValueError("knowledge-base grant has conflicting definitions")
        values[key] = grant
    if not values:
        raise ValueError("at least one knowledge-base grant is required")
    return tuple(values[key] for key in sorted(values))


def _unique_compile_jobs(
    jobs: Sequence[CompileJobSpec],
) -> tuple[CompileJobSpec, ...]:
    values: dict[str, CompileJobSpec] = {}
    for job in jobs:
        if not isinstance(job, CompileJobSpec):
            raise TypeError("compile_jobs must contain CompileJobSpec values")
        existing = values.get(job.source_version_id)
        if existing is not None and existing != job:
            raise ValueError("source_version_id has conflicting compile requests")
        values[job.source_version_id] = job
    return tuple(values[key] for key in sorted(values))


def _required_uuid(value: Any, field_name: str) -> str:
    if value is None:
        raise ValueError(f"{field_name} must be a UUID")
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error
    return str(parsed)


def _optional_uuid(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_uuid(value, field_name)


def _required_sha256(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return normalized


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _nullable_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    normalized = dict(value)
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be JSON serializable") from error
    return normalized
