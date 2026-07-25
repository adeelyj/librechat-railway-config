"""Production-safe orchestration for Bauer Evidence V3 releases.

This module is intentionally independent of the API ``Settings`` object.  A
release operator needs database scope, immutable source storage, and compiler
identity, but must not need answer-model credentials or API configuration.

The controller delegates every lifecycle transition to
``PostgresReleaseAdmin`` and its contextual ``PostgresJobQueue``.  In
particular, it does not reproduce the database evidence, QA, verified-gold,
activation, or rollback gates in Python.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, Protocol

from .db_security import verify_runtime_database_role
from .ingest import Compiler, PdfPlumberPageRenderer
from .ingest.ocr import RapidOcrEngine
from .object_store import (
    ContentAddressedObjectStore,
    LocalObjectStore,
    MirroredObjectStore,
    S3ObjectStore,
)
from .postgres_admin import (
    ActivationResult,
    BuildStartResult,
    KnowledgeBaseGrant,
    PostgresReleaseAdmin,
    PrincipalBootstrap,
    ReleaseSpec,
)
from .source_manifest import (
    SourceManifest,
    SourceSpec,
    create_source_manifest,
    load_source_manifest,
    stage_manifest_sources,
    verify_source_manifest,
)
from .toolchain import CompilerToolchainIdentity, derive_compiler_toolchain


CONFIRM_ACTIVE_POINTER_CHANGE = "CONFIRM_ACTIVE_POINTER_CHANGE"
RELEASE_EMBEDDING_DIMENSIONS = 1_024
_SUPPORTED_ENVIRONMENTS = frozenset(
    {"development", "test", "staging", "production"}
)
_SOURCE_SPEC_KEYS = frozenset(
    {
        "external_file_id",
        "logical_path",
        "source_type",
        "declared_media_type",
        "visibility",
        "category_path",
        "metadata",
        "is_original",
        "is_derivative",
    }
)
_MANIFEST_SPEC_KEYS = frozenset(
    {
        "tenant_id",
        "knowledge_base_id",
        "release_id",
        "created_at",
        "sources",
    }
)
_BOOTSTRAP_KEYS = frozenset(
    {
        "external_key",
        "tenant_display_name",
        "kb_id",
        "external_namespace",
        "kb_name",
        "kb_description",
        "principals",
        "grants",
        "tenant_metadata",
        "kb_metadata",
    }
)
_PRINCIPAL_KEYS = frozenset(
    {
        "principal_id",
        "principal_type",
        "external_subject",
        "display_name",
    }
)
_GRANT_KEYS = frozenset({"principal_id", "permission", "granted_by"})


class ReleaseControlError(RuntimeError):
    """A release-control input or safety invariant was violated."""


class ReleaseAdmin(Protocol):
    def bootstrap_tenant_knowledge_base(self, **kwargs: Any) -> Any: ...

    def create_release(self, spec: ReleaseSpec) -> Mapping[str, Any]: ...

    def start_build(
        self,
        release_id: str,
        *,
        compile_jobs: Sequence[Any] = (),
    ) -> BuildStartResult: ...

    def begin_validation(self, release_id: str) -> Mapping[str, Any]: ...

    def mark_ready(self, release_id: str) -> Mapping[str, Any]: ...

    def mark_failed(
        self,
        release_id: str,
        *,
        error: str,
    ) -> Mapping[str, Any]: ...

    def replay_dead_letter(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        actor_principal_id: str,
    ) -> Mapping[str, Any]: ...

    def retire_release(self, release_id: str) -> Mapping[str, Any]: ...

    def activate_release(self, **kwargs: Any) -> ActivationResult: ...

    def rollback_release(self, **kwargs: Any) -> ActivationResult: ...


class ReleaseStatusReader(Protocol):
    def inspect(
        self,
        *,
        kb_id: str,
        release_id: str | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ControlScope:
    """The one tenant, knowledge base, and database principal context."""

    tenant_id: str
    knowledge_base_id: str
    principal_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tenant_id",
            _uuid(self.tenant_id, "tenant_id"),
        )
        object.__setattr__(
            self,
            "knowledge_base_id",
            _uuid(self.knowledge_base_id, "knowledge_base_id"),
        )
        principals = tuple(
            sorted({_uuid(value, "principal_id") for value in self.principal_ids})
        )
        if not principals:
            raise ReleaseControlError("at least one principal_id is required")
        object.__setattr__(self, "principal_ids", principals)


@dataclass(frozen=True, slots=True)
class DatabaseControlSettings:
    """Minimal database configuration for release-control commands."""

    database_url: str
    scope: ControlScope

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "database_url",
            _required_text(self.database_url, "database_url"),
        )
        if not isinstance(self.scope, ControlScope):
            raise TypeError("scope must be a ControlScope")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "DatabaseControlSettings":
        values = os.environ if environment is None else environment
        database_url = _required_text(
            values.get("BAUER_V3_DATABASE_URL"),
            "BAUER_V3_DATABASE_URL",
        )
        tenant_id = _required_text(
            values.get("BAUER_V3_TENANT_ID"),
            "BAUER_V3_TENANT_ID",
        )
        knowledge_base_id = _required_text(
            values.get("BAUER_V3_KNOWLEDGE_BASE_ID"),
            "BAUER_V3_KNOWLEDGE_BASE_ID",
        )
        principal_ids = _json_string_tuple(
            values.get("BAUER_V3_PRINCIPAL_IDS_JSON"),
            "BAUER_V3_PRINCIPAL_IDS_JSON",
        )
        return cls(
            database_url=database_url,
            scope=ControlScope(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                principal_ids=principal_ids,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseBuildResult:
    release: Mapping[str, Any]
    build: BuildStartResult
    manifest_sha256: str
    compiler_fingerprint: str
    parser_version: str
    compile_job_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "release": dict(self.release),
            "build_release": dict(self.build.release),
            "jobs": [dict(value) for value in self.build.jobs],
            "manifest_sha256": self.manifest_sha256,
            "compiler_fingerprint": self.compiler_fingerprint,
            "parser_version": self.parser_version,
            "compile_job_count": self.compile_job_count,
        }


class ReleaseController:
    """Bounded release workflow over an injected administrative adapter."""

    def __init__(
        self,
        *,
        scope: ControlScope,
        admin: ReleaseAdmin,
        status_reader: ReleaseStatusReader | None = None,
    ) -> None:
        if not isinstance(scope, ControlScope):
            raise TypeError("scope must be a ControlScope")
        self.scope = scope
        self.admin = admin
        self.status_reader = status_reader

    def bootstrap(self, specification: Mapping[str, Any]) -> Any:
        """Bootstrap exactly the configured tenant and knowledge base."""

        payload = _strict_object(
            specification,
            allowed=_BOOTSTRAP_KEYS,
            required={
                "external_key",
                "tenant_display_name",
                "external_namespace",
                "kb_name",
                "principals",
                "grants",
            },
            name="bootstrap specification",
        )
        supplied_kb_id = payload.get("kb_id", self.scope.knowledge_base_id)
        kb_id = _uuid(supplied_kb_id, "bootstrap kb_id")
        if kb_id != self.scope.knowledge_base_id:
            raise ReleaseControlError(
                "bootstrap kb_id does not match BAUER_V3_KNOWLEDGE_BASE_ID"
            )
        principals = tuple(
            _principal_from_mapping(value)
            for value in _json_array(payload["principals"], "principals")
        )
        grants = tuple(
            _grant_from_mapping(value)
            for value in _json_array(payload["grants"], "grants")
        )
        return self.admin.bootstrap_tenant_knowledge_base(
            external_key=payload["external_key"],
            tenant_display_name=payload["tenant_display_name"],
            kb_id=kb_id,
            external_namespace=payload["external_namespace"],
            kb_name=payload["kb_name"],
            principals=principals,
            grants=grants,
            kb_description=payload.get("kb_description"),
            tenant_metadata=payload.get("tenant_metadata"),
            kb_metadata=payload.get("kb_metadata"),
        )

    def build_release(
        self,
        *,
        manifest: SourceManifest,
        source_root: str | Path,
        object_store: ContentAddressedObjectStore,
        toolchain: CompilerToolchainIdentity,
        embedding_model_version: str,
        created_by: str,
        based_on_release_id: str | None = None,
        fact_model_version: str | None = None,
        prompt_version: str | None = None,
        priority: int = 0,
        max_attempts: int = 5,
    ) -> ReleaseBuildResult:
        """Verify, stage, create, and start one immutable release build."""

        self._assert_manifest_scope(manifest)
        _assert_release_toolchain(
            toolchain,
            embedding_model_version=embedding_model_version,
            fact_model_version=fact_model_version,
        )
        verified = verify_source_manifest(manifest, source_root)
        compile_jobs = stage_manifest_sources(
            verified,
            source_root,
            object_store,
            priority=priority,
            max_attempts=max_attempts,
        )
        metadata = {
            "toolchain": toolchain.manifest,
            "source_manifest_schema_version": manifest.schema_version,
        }
        release_spec = ReleaseSpec(
            release_id=manifest.release_id,
            kb_id=self.scope.knowledge_base_id,
            based_on_release_id=based_on_release_id,
            manifest_sha256=manifest.manifest_sha256,
            expected_source_count=manifest.source_count,
            compiler_fingerprint=toolchain.fingerprint,
            parser_version=toolchain.parser_version,
            ocr_version=_toolchain_ocr_version(toolchain),
            fact_model_version=fact_model_version,
            embedding_model_version=embedding_model_version,
            prompt_version=prompt_version,
            metadata=metadata,
            created_by=created_by,
        )
        release = self.admin.create_release(release_spec)
        build = self.admin.start_build(
            manifest.release_id,
            compile_jobs=compile_jobs,
        )
        return ReleaseBuildResult(
            release=release,
            build=build,
            manifest_sha256=manifest.manifest_sha256,
            compiler_fingerprint=toolchain.fingerprint,
            parser_version=toolchain.parser_version,
            compile_job_count=len(compile_jobs),
        )

    def begin_validation(self, release_id: str) -> Mapping[str, Any]:
        return self.admin.begin_validation(_uuid(release_id, "release_id"))

    def mark_ready(self, release_id: str) -> Mapping[str, Any]:
        # The PostgreSQL adapter owns the evidence, QA, and independently
        # verified-gold gates.  There is deliberately no force option here.
        return self.admin.mark_ready(_uuid(release_id, "release_id"))

    def mark_failed(
        self,
        release_id: str,
        *,
        error: str,
    ) -> Mapping[str, Any]:
        return self.admin.mark_failed(
            _uuid(release_id, "release_id"),
            error=_required_text(error, "error"),
        )

    def replay_dead_letter(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        actor_principal_id: str,
    ) -> Mapping[str, Any]:
        return self.admin.replay_dead_letter(
            _uuid(job_id, "job_id"),
            idempotency_key=_required_text(
                idempotency_key,
                "idempotency_key",
            ),
            actor_principal_id=self._actor(actor_principal_id),
        )

    def retire(self, release_id: str) -> Mapping[str, Any]:
        return self.admin.retire_release(_uuid(release_id, "release_id"))

    def inspect(
        self,
        *,
        release_id: str | None = None,
    ) -> Mapping[str, Any]:
        if self.status_reader is None:
            raise ReleaseControlError("release status reader is not configured")
        return self.status_reader.inspect(
            kb_id=self.scope.knowledge_base_id,
            release_id=(
                _uuid(release_id, "release_id")
                if release_id is not None
                else None
            ),
        )

    def activate(
        self,
        *,
        release_id: str,
        actor_principal_id: str,
        reason: str,
        confirmation: str,
    ) -> ActivationResult:
        self._require_pointer_confirmation(confirmation)
        actor = self._actor(actor_principal_id)
        return self.admin.activate_release(
            kb_id=self.scope.knowledge_base_id,
            release_id=_uuid(release_id, "release_id"),
            actor_principal_id=actor,
            reason=_required_text(reason, "reason"),
            production=True,
        )

    def rollback(
        self,
        *,
        target_release_id: str,
        actor_principal_id: str,
        reason: str,
        confirmation: str,
    ) -> ActivationResult:
        self._require_pointer_confirmation(confirmation)
        actor = self._actor(actor_principal_id)
        return self.admin.rollback_release(
            kb_id=self.scope.knowledge_base_id,
            target_release_id=_uuid(
                target_release_id,
                "target_release_id",
            ),
            actor_principal_id=actor,
            reason=_required_text(reason, "reason"),
            production=True,
        )

    def _assert_manifest_scope(self, manifest: SourceManifest) -> None:
        if not isinstance(manifest, SourceManifest):
            raise TypeError("manifest must be a SourceManifest")
        if manifest.tenant_id != self.scope.tenant_id:
            raise ReleaseControlError(
                "manifest tenant_id does not match BAUER_V3_TENANT_ID"
            )
        if manifest.knowledge_base_id != self.scope.knowledge_base_id:
            raise ReleaseControlError(
                "manifest knowledge_base_id does not match "
                "BAUER_V3_KNOWLEDGE_BASE_ID"
            )

    def _actor(self, principal_id: str) -> str:
        actor = _uuid(principal_id, "actor_principal_id")
        if actor not in self.scope.principal_ids:
            raise ReleaseControlError(
                "actor_principal_id is not in BAUER_V3_PRINCIPAL_IDS_JSON"
            )
        return actor

    @staticmethod
    def _require_pointer_confirmation(confirmation: str) -> None:
        if confirmation != CONFIRM_ACTIVE_POINTER_CHANGE:
            raise ReleaseControlError(
                "active-pointer change requires the exact confirmation "
                f"{CONFIRM_ACTIVE_POINTER_CHANGE}"
            )


class PostgresReleaseStatusReader:
    """Read-only release status inspection under the configured RLS context."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any] | Any],
        *,
        tenant_id: str,
        principal_ids: Sequence[str],
        enforce_database_role: bool = False,
    ) -> None:
        if not callable(connection_provider):
            raise TypeError("connection_provider must be callable")
        self._connection_provider = connection_provider
        self.tenant_id = _uuid(tenant_id, "tenant_id")
        self.principal_ids = tuple(
            sorted({_uuid(value, "principal_id") for value in principal_ids})
        )
        if not self.principal_ids:
            raise ReleaseControlError("at least one principal_id is required")
        self._enforce_database_role = bool(enforce_database_role)

    @classmethod
    def from_dsn(
        cls,
        database_url: str,
        *,
        tenant_id: str,
        principal_ids: Sequence[str],
    ) -> "PostgresReleaseStatusReader":
        dsn = _required_text(database_url, "database_url")

        def connect() -> Any:
            try:
                import psycopg
            except ModuleNotFoundError as exc:
                raise ReleaseControlError(
                    "PostgreSQL release control requires psycopg 3"
                ) from exc
            return psycopg.connect(
                dsn,
                application_name="bauer-evidence-v3-release-control",
            )

        return cls(
            connect,
            tenant_id=tenant_id,
            principal_ids=principal_ids,
            enforce_database_role=True,
        )

    def inspect(
        self,
        *,
        kb_id: str,
        release_id: str | None = None,
    ) -> Mapping[str, Any]:
        knowledge_base_id = _uuid(kb_id, "kb_id")
        selected_release = (
            _uuid(release_id, "release_id")
            if release_id is not None
            else None
        )
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                (self.tenant_id,),
            )
            connection.execute(
                "SELECT set_config('app.principal_ids', %s, true)",
                (
                    json.dumps(
                        self.principal_ids,
                        separators=(",", ":"),
                    ),
                ),
            )
            row = connection.execute(
                """
                WITH selected AS (
                    SELECT release_row.*
                    FROM bauer_rag_v3.knowledge_releases AS release_row
                    WHERE release_row.kb_id = %s::uuid
                      AND (
                          %s::uuid IS NULL
                          OR release_row.release_id = %s::uuid
                      )
                    ORDER BY release_row.created_at DESC
                    LIMIT 1
                )
                SELECT jsonb_build_object(
                    'release',
                    (SELECT to_jsonb(selected) FROM selected),
                    'active_release_id',
                    (
                        SELECT active.release_id::text
                        FROM bauer_rag_v3.active_releases AS active
                        WHERE active.kb_id = %s::uuid
                    ),
                    'job_counts',
                    COALESCE(
                        (
                            SELECT jsonb_object_agg(counts.state, counts.total)
                            FROM (
                                SELECT job.state, count(*)::integer AS total
                                FROM bauer_rag_v3.jobs AS job
                                WHERE job.kb_id = %s::uuid
                                  AND job.release_id = (
                                      SELECT selected.release_id FROM selected
                                  )
                                GROUP BY job.state
                            ) AS counts
                        ),
                        '{}'::jsonb
                    ),
                    'unresolved_blocker_count',
                    (
                        SELECT count(*)::integer
                        FROM bauer_rag_v3.qa_checks AS qa
                        WHERE qa.kb_id = %s::uuid
                          AND qa.release_id = (
                              SELECT selected.release_id FROM selected
                          )
                          AND qa.severity = 'blocker'
                          AND qa.status = 'fail'
                          AND qa.resolved_at IS NULL
                    ),
                    'latest_eval',
                    (
                        SELECT to_jsonb(eval_run)
                        FROM bauer_rag_v3.eval_runs AS eval_run
                        WHERE eval_run.kb_id = %s::uuid
                          AND eval_run.release_id = (
                              SELECT selected.release_id FROM selected
                          )
                        ORDER BY eval_run.started_at DESC
                        LIMIT 1
                    )
                )
                """,
                (
                    knowledge_base_id,
                    selected_release,
                    selected_release,
                    knowledge_base_id,
                    knowledge_base_id,
                    knowledge_base_id,
                    knowledge_base_id,
                ),
            ).fetchone()
        payload = _single_json_value(row)
        if payload.get("release") is None:
            target = selected_release or f"latest release for {knowledge_base_id}"
            raise ReleaseControlError(f"release not found: {target}")
        payload["is_active"] = (
            payload.get("active_release_id")
            == str(payload["release"].get("release_id"))
        )
        return payload

    @contextmanager
    def _connection(self):
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
            try:
                if self._enforce_database_role:
                    verify_runtime_database_role(
                        resource,
                        required_group_role="bauer_rag_v3_admin",
                    )
                yield resource
            finally:
                close = getattr(resource, "close", None)
                if callable(close):
                    close()


def load_manifest_creation_spec(
    path: str | Path,
) -> tuple[dict[str, Any], tuple[SourceSpec, ...]]:
    """Load the strict JSON input used by the offline manifest command."""

    payload = _strict_object(
        load_json_file(path),
        allowed=_MANIFEST_SPEC_KEYS,
        required={
            "tenant_id",
            "knowledge_base_id",
            "release_id",
            "sources",
        },
        name="manifest creation specification",
    )
    sources = tuple(
        _source_spec_from_mapping(value)
        for value in _json_array(payload["sources"], "sources")
    )
    return payload, sources


def create_manifest_from_spec(
    specification_path: str | Path,
    *,
    source_root: str | Path,
) -> SourceManifest:
    payload, sources = load_manifest_creation_spec(specification_path)
    return create_source_manifest(
        source_root,
        tenant_id=payload["tenant_id"],
        knowledge_base_id=payload["knowledge_base_id"],
        release_id=payload["release_id"],
        created_at=payload.get("created_at"),
        sources=sources,
    )


def load_verified_manifest(
    manifest_path: str | Path,
    *,
    source_root: str | Path,
) -> SourceManifest:
    content = Path(manifest_path).read_bytes()
    manifest = load_source_manifest(content)
    return verify_source_manifest(manifest, source_root)


def write_manifest_once(
    manifest: SourceManifest,
    output_path: str | Path,
) -> Path:
    """Write an immutable manifest without silently replacing prior bytes."""

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = manifest.to_bytes()
    if target.exists():
        if target.read_bytes() == content:
            return target
        raise ReleaseControlError(
            f"refusing to overwrite a different manifest: {target}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    created_target = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Opening with x mode makes the non-overwrite promise explicit.  Copy
        # from the fsynced temporary while holding the newly created target.
        with target.open("xb") as handle:
            created_target = True
            handle.write(temporary.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if created_target:
            target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return target


def build_compiler_from_environment(
    environment: Mapping[str, str] | None = None,
) -> Compiler:
    """Build the compiler components that contribute to toolchain identity."""

    values = os.environ if environment is None else environment
    enabled = _boolean(
        values.get("BAUER_V3_OCR_ENABLED"),
        default=False,
        name="BAUER_V3_OCR_ENABLED",
    )
    if not enabled:
        return Compiler()
    engine = RapidOcrEngine(
        detection_model=_required_text(
            values.get("BAUER_V3_OCR_DETECTION_MODEL"),
            "BAUER_V3_OCR_DETECTION_MODEL",
        ),
        detection_model_sha256=_required_text(
            values.get("BAUER_V3_OCR_DETECTION_MODEL_SHA256"),
            "BAUER_V3_OCR_DETECTION_MODEL_SHA256",
        ),
        recognition_model=_required_text(
            values.get("BAUER_V3_OCR_RECOGNITION_MODEL"),
            "BAUER_V3_OCR_RECOGNITION_MODEL",
        ),
        recognition_model_sha256=_required_text(
            values.get("BAUER_V3_OCR_RECOGNITION_MODEL_SHA256"),
            "BAUER_V3_OCR_RECOGNITION_MODEL_SHA256",
        ),
        classification_model=_required_text(
            values.get("BAUER_V3_OCR_CLASSIFICATION_MODEL"),
            "BAUER_V3_OCR_CLASSIFICATION_MODEL",
        ),
        classification_model_sha256=_required_text(
            values.get("BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256"),
            "BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256",
        ),
    )
    return Compiler(
        ocr_engine=engine,
        page_renderer=PdfPlumberPageRenderer(),
        ocr_languages=(
            _json_string_tuple(
                values.get("BAUER_V3_OCR_LANGUAGES_JSON"),
                "BAUER_V3_OCR_LANGUAGES_JSON",
            )
            if values.get("BAUER_V3_OCR_LANGUAGES_JSON")
            else ("de", "en")
        ),
        ocr_minimum_confidence=_bounded_float(
            values.get("BAUER_V3_OCR_MINIMUM_CONFIDENCE"),
            default=0.80,
            minimum=0.0,
            maximum=1.0,
            name="BAUER_V3_OCR_MINIMUM_CONFIDENCE",
        ),
        ocr_render_dpi=_positive_int(
            values.get("BAUER_V3_OCR_RENDER_DPI"),
            default=150,
            name="BAUER_V3_OCR_RENDER_DPI",
        ),
    )


def derive_toolchain_from_environment(
    *,
    embedding_model_version: str,
    embedding_dimensions: int,
    fact_model_version: str | None = None,
    environment: Mapping[str, str] | None = None,
    compiler_factory: Callable[[Mapping[str, str] | None], Compiler] = (
        build_compiler_from_environment
    ),
) -> CompilerToolchainIdentity:
    compiler = compiler_factory(environment)
    return derive_compiler_toolchain(
        embedding_model_version=embedding_model_version,
        embedding_dimensions=embedding_dimensions,
        compiler=compiler,
        fact_model_version=fact_model_version,
    )


def toolchain_to_dict(
    identity: CompilerToolchainIdentity,
) -> dict[str, Any]:
    return {
        "fingerprint": identity.fingerprint,
        "parser_version": identity.parser_version,
        "manifest": identity.manifest,
    }


def build_object_store_from_environment(
    environment: Mapping[str, str] | None = None,
) -> ContentAddressedObjectStore:
    """Build only the source object store needed by release staging."""

    values = os.environ if environment is None else environment
    deployment_environment = (
        values.get("BAUER_V3_ENVIRONMENT", "development").strip().casefold()
    )
    if deployment_environment not in _SUPPORTED_ENVIRONMENTS:
        raise ReleaseControlError(
            "BAUER_V3_ENVIRONMENT must be one of: "
            + ", ".join(sorted(_SUPPORTED_ENVIRONMENTS))
        )
    backend = (
        values.get("BAUER_V3_OBJECT_STORE_BACKEND", "local")
        .strip()
        .casefold()
    )
    if backend == "local":
        if deployment_environment == "production":
            raise ReleaseControlError(
                "production release staging requires the mirrored S3 backend"
            )
        root = values.get(
            "BAUER_V3_OBJECT_STORE_ROOT",
            "./data/v3-objects",
        )
        return LocalObjectStore(root)
    if backend != "s3":
        raise ReleaseControlError(
            "BAUER_V3_OBJECT_STORE_BACKEND must be local or s3"
        )
    prefix = values.get("BAUER_V3_S3_PREFIX", "bauer-rag-v3").strip()
    mirror_bucket = values.get("BAUER_V3_MIRROR_S3_BUCKET", "").strip()
    if deployment_environment == "production" and not mirror_bucket:
        raise ReleaseControlError(
            "production release staging requires "
            "BAUER_V3_MIRROR_S3_BUCKET"
        )
    primary = S3ObjectStore(
        bucket=_required_text(
            values.get("BAUER_V3_S3_BUCKET"),
            "BAUER_V3_S3_BUCKET",
        ),
        client=_s3_client(
            endpoint_url=values.get("BAUER_V3_S3_ENDPOINT_URL", "").strip(),
            region_name=values.get("BAUER_V3_S3_REGION", "").strip(),
            access_key_id=values.get(
                "BAUER_V3_S3_ACCESS_KEY_ID",
                "",
            ).strip(),
            secret_access_key=values.get(
                "BAUER_V3_S3_SECRET_ACCESS_KEY",
                "",
            ).strip(),
        ),
        prefix=prefix,
    )
    if not mirror_bucket:
        return primary
    mirror = S3ObjectStore(
        bucket=mirror_bucket,
        client=_s3_client(
            endpoint_url=values.get(
                "BAUER_V3_MIRROR_S3_ENDPOINT_URL",
                "",
            ).strip(),
            region_name=values.get(
                "BAUER_V3_MIRROR_S3_REGION",
                "",
            ).strip(),
            access_key_id=values.get(
                "BAUER_V3_MIRROR_S3_ACCESS_KEY_ID",
                "",
            ).strip(),
            secret_access_key=values.get(
                "BAUER_V3_MIRROR_S3_SECRET_ACCESS_KEY",
                "",
            ).strip(),
        ),
        prefix=prefix,
    )
    return MirroredObjectStore(primary=primary, mirror=mirror)


def default_admin(settings: DatabaseControlSettings) -> PostgresReleaseAdmin:
    return PostgresReleaseAdmin.from_dsn(
        settings.database_url,
        tenant_id=settings.scope.tenant_id,
        principal_ids=settings.scope.principal_ids,
    )


def default_bootstrap_admin(
    settings: DatabaseControlSettings,
) -> PostgresReleaseAdmin:
    return PostgresReleaseAdmin.from_bootstrap_dsn(
        settings.database_url,
        tenant_id=settings.scope.tenant_id,
        principal_ids=settings.scope.principal_ids,
    )


def default_status_reader(
    settings: DatabaseControlSettings,
) -> PostgresReleaseStatusReader:
    return PostgresReleaseStatusReader.from_dsn(
        settings.database_url,
        tenant_id=settings.scope.tenant_id,
        principal_ids=settings.scope.principal_ids,
    )


def load_json_file(path: str | Path) -> dict[str, Any]:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseControlError(f"cannot read JSON file: {path}") from exc
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ReleaseControlError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseControlError("JSON input must be an object")
    return value


def _source_spec_from_mapping(value: Any) -> SourceSpec:
    payload = _strict_object(
        value,
        allowed=_SOURCE_SPEC_KEYS,
        required={"external_file_id", "logical_path", "source_type"},
        name="source specification",
    )
    return SourceSpec(
        external_file_id=payload["external_file_id"],
        logical_path=payload["logical_path"],
        source_type=payload["source_type"],
        declared_media_type=payload.get("declared_media_type"),
        visibility=payload.get("visibility", "inherited"),
        category_path=tuple(payload.get("category_path", ())),
        metadata=payload.get("metadata", {}),
        is_original=payload.get("is_original", True),
        is_derivative=payload.get("is_derivative", False),
    )


def _principal_from_mapping(value: Any) -> PrincipalBootstrap:
    payload = _strict_object(
        value,
        allowed=_PRINCIPAL_KEYS,
        required={"principal_id", "principal_type", "external_subject"},
        name="principal",
    )
    return PrincipalBootstrap(
        principal_id=payload["principal_id"],
        principal_type=payload["principal_type"],
        external_subject=payload["external_subject"],
        display_name=payload.get("display_name"),
    )


def _grant_from_mapping(value: Any) -> KnowledgeBaseGrant:
    payload = _strict_object(
        value,
        allowed=_GRANT_KEYS,
        required={"principal_id", "permission"},
        name="grant",
    )
    return KnowledgeBaseGrant(
        principal_id=payload["principal_id"],
        permission=payload["permission"],
        granted_by=payload.get("granted_by"),
    )


def _strict_object(
    value: Any,
    *,
    allowed: frozenset[str],
    required: set[str],
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseControlError(f"{name} must be a JSON object")
    payload = dict(value)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ReleaseControlError(
            f"{name} has unknown fields: {', '.join(unknown)}"
        )
    missing = sorted(required - set(payload))
    if missing:
        raise ReleaseControlError(
            f"{name} is missing fields: {', '.join(missing)}"
        )
    return payload


def _json_array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReleaseControlError(f"{name} must be a JSON array")
    return value


def _single_json_value(row: Any) -> dict[str, Any]:
    if row is None:
        raise ReleaseControlError("database status query returned no row")
    if isinstance(row, Mapping):
        value = next(iter(row.values())) if len(row) == 1 else dict(row)
    else:
        value = row[0]
    if not isinstance(value, Mapping):
        raise ReleaseControlError(
            "database status query did not return a JSON object"
        )
    return dict(value)


def _toolchain_ocr_version(
    identity: CompilerToolchainIdentity,
) -> str | None:
    ocr = identity.manifest.get("ocr")
    if ocr is None:
        return None
    if not isinstance(ocr, Mapping):
        raise ReleaseControlError("toolchain OCR identity is invalid")
    return _required_text(ocr.get("version"), "toolchain OCR version")


def _assert_release_toolchain(
    identity: CompilerToolchainIdentity,
    *,
    embedding_model_version: str,
    fact_model_version: str | None,
) -> None:
    if not isinstance(identity, CompilerToolchainIdentity):
        raise TypeError("toolchain must be a CompilerToolchainIdentity")
    manifest = identity.manifest
    embedding = manifest.get("embedding")
    if not isinstance(embedding, Mapping):
        raise ReleaseControlError("toolchain embedding identity is invalid")
    expected_model = _required_text(
        embedding_model_version,
        "embedding_model_version",
    )
    if embedding.get("model_version") != expected_model:
        raise ReleaseControlError(
            "release embedding model does not match the derived toolchain"
        )
    if embedding.get("dimensions") != RELEASE_EMBEDDING_DIMENSIONS:
        raise ReleaseControlError(
            "release toolchain must use the schema-fixed 1024 embedding "
            "dimensions"
        )
    stored_fact_model = manifest.get("fact_model")
    if fact_model_version is None:
        if stored_fact_model is not None:
            raise ReleaseControlError(
                "release fact model does not match the derived toolchain"
            )
    elif (
        not isinstance(stored_fact_model, Mapping)
        or stored_fact_model.get("version") != fact_model_version
    ):
        raise ReleaseControlError(
            "release fact model does not match the derived toolchain"
        )


def _s3_client(
    *,
    endpoint_url: str,
    region_name: str,
    access_key_id: str,
    secret_access_key: str,
) -> Any:
    if bool(access_key_id) != bool(secret_access_key):
        raise ReleaseControlError(
            "S3 access key ID and secret access key must be configured together"
        )
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise ReleaseControlError(
            "S3 release staging requires boto3"
        ) from exc
    options: dict[str, str] = {}
    if endpoint_url:
        options["endpoint_url"] = endpoint_url
    if region_name:
        options["region_name"] = region_name
    if access_key_id:
        options["aws_access_key_id"] = access_key_id
        options["aws_secret_access_key"] = secret_access_key
    return boto3.client("s3", **options)


def _json_string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseControlError(f"{name} is required")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ReleaseControlError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ReleaseControlError(f"{name} must be a non-empty JSON array")
    normalized: list[str] = []
    for item in parsed:
        normalized.append(_required_text(item, f"{name} item"))
    return tuple(normalized)


def _boolean(
    value: Any,
    *,
    default: bool,
    name: str,
) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ReleaseControlError(f"{name} must be true or false")


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseControlError(f"{name} must be numeric") from exc
    if not minimum <= result <= maximum:
        raise ReleaseControlError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return result


def _positive_int(
    value: Any,
    *,
    default: int,
    name: str,
) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseControlError(f"{name} must be an integer") from exc
    if isinstance(value, bool) or result < 1:
        raise ReleaseControlError(f"{name} must be a positive integer")
    return result


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseControlError(f"{name} is required")
    return value.strip()


def _uuid(value: Any, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReleaseControlError(f"{name} must be a UUID") from exc


def _reject_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
