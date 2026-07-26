"""Transactional PostgreSQL persistence for the Bauer Evidence V3 compiler.

The compiler writes immutable source and canonical bytes to object storage
before opening a database transaction. PostgreSQL then owns registry identity,
artifact reuse, exact evidence coordinates, and release-scoped projections.

No psycopg module is imported at module-import time. Tests and alternate
connection managers can inject both a connection provider and JSON adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence

from .ids import sha256_bytes, sha256_json, stable_id
from .ingest import CompilationResult
from .ingest.canonical import Block, Cell, Document, Page, Table
from .object_store import (
    ContentAddressedObjectStore,
    StoredObject,
)
from .planner import normalize_text
from .projections import ProjectionBundle


DATABASE_ID_NAMESPACE = uuid.UUID("a33db731-3cb8-5f31-80d4-8cd6b294a764")
COMPILER_RELEASE_LOCK_SEED = 72_897_565_840_948
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TYPES = {
    "pdf",
    "html",
    "image",
    "text",
    "office_document",
    "structured_record",
    "synthetic_demo",
}
_SOURCE_TYPE_ALIASES = {
    "markdown": "text",
    "md": "text",
    "office": "office_document",
}


class PostgresCompilerError(RuntimeError):
    """Base class for persistence and database-contract failures."""


class PostgresCompilerDependencyError(PostgresCompilerError):
    """The optional psycopg 3 runtime is unavailable."""


class ReleaseStateError(PostgresCompilerError):
    """The requested release cannot accept compiler output."""


class ArtifactStateError(PostgresCompilerError):
    """An artifact is busy, corrupt, quarantined, or otherwise not reusable."""


class PersistenceInvariantError(PostgresCompilerError):
    """Compiler output cannot satisfy the normalized evidence schema."""


@dataclass(frozen=True, slots=True)
class CompilerPersistenceContext:
    tenant_id: str
    knowledge_base_id: str
    release_id: str
    manifest_sha256: str
    external_file_id: str
    compiler_fingerprint: str
    embedding_model_version: str
    source_type: str
    ordinal: int
    source_document_id: str | None = None
    source_version_id: str | None = None
    canonical_uri: str | None = None
    source_revision: str | None = None
    publication_date: str | None = None
    visibility: str = "inherited"
    worker_job_id: str | None = None
    ocr_version: str | None = None
    fact_model_version: str | None = None
    source_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "tenant_id",
            "knowledge_base_id",
            "release_id",
            "external_file_id",
            "embedding_model_version",
            "source_type",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")
        manifest_sha256 = self.manifest_sha256.casefold()
        if not _SHA256_RE.fullmatch(manifest_sha256):
            raise ValueError("manifest_sha256 must be lowercase SHA-256")
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        fingerprint = self.compiler_fingerprint.casefold()
        if not _SHA256_RE.fullmatch(fingerprint):
            raise ValueError("compiler_fingerprint must be lowercase SHA-256")
        object.__setattr__(self, "compiler_fingerprint", fingerprint)
        normalized_type = _SOURCE_TYPE_ALIASES.get(
            self.source_type.casefold(), self.source_type.casefold()
        )
        if normalized_type not in _SOURCE_TYPES:
            raise ValueError(f"unsupported source_type: {self.source_type}")
        object.__setattr__(self, "source_type", normalized_type)
        if self.visibility not in {"inherited", "restricted"}:
            raise ValueError("visibility must be inherited or restricted")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise ValueError("ordinal must be a non-negative integer")
        _json_object(self.source_metadata, "source_metadata")


@dataclass(frozen=True, slots=True)
class PersistedCompilation:
    source_id: str
    source_version_id: str
    artifact_set_id: str
    source_object: StoredObject
    canonical_object: StoredObject
    page_render_objects: tuple[tuple[int, StoredObject], ...]
    reused: bool
    artifact_counts: tuple[tuple[str, int], ...]
    projection_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class _Identity:
    canonical_source_id: str
    canonical_source_version_id: str
    tenant_id: str
    knowledge_base_id: str
    release_id: str


@dataclass(frozen=True, slots=True)
class _SectionSpec:
    canonical_id: str
    database_id: str
    parent_database_id: str | None
    path: tuple[str, ...]
    ordinal: int
    start_page: int
    end_page: int


@dataclass(frozen=True, slots=True)
class _ProvenanceSpec:
    database_id: str
    canonical_id: str
    evidence_id: str
    page_id: str | None
    block_id: str | None
    cell_id: str | None
    char_start: int | None
    char_end: int | None
    bbox: tuple[float, float, float, float] | None
    quoted_text: str
    quote_sha256: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _CorePlan:
    artifact_id: str
    artifact_canonical_id: str
    page_ids: Mapping[int, str]
    block_ids: Mapping[str, str]
    table_ids: Mapping[str, str]
    cell_ids: Mapping[str, str]
    section_ids: Mapping[tuple[str, ...], str]
    block_sections: Mapping[str, str | None]
    table_sections: Mapping[str, str | None]
    sections: tuple[_SectionSpec, ...]
    provenance: tuple[_ProvenanceSpec, ...]
    evidence_provenance: Mapping[str, tuple[str, ...]]
    expected_counts: Mapping[str, int]
    projection_fingerprint: str


def canonical_uuid(logical_id: str, *, scope: str | None = None) -> str:
    """Map readable logical IDs to stable UUIDv5 values.

    Existing UUIDs remain unchanged only when no scope is required. Artifact-
    and release-owned rows use a scope because their database primary keys are
    global while their canonical IDs are only stable within that owner.
    """

    if not isinstance(logical_id, str) or not logical_id.strip():
        raise ValueError("logical_id is required")
    clean = logical_id.strip()
    if scope is None:
        try:
            return str(uuid.UUID(clean))
        except ValueError:
            material = f"bauer-evidence-v3:{clean}"
    else:
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope must be a non-empty string")
        material = f"bauer-evidence-v3:{scope.strip()}:{clean}"
    return str(uuid.uuid5(DATABASE_ID_NAMESPACE, material))


class PostgresCompilerPersistence:
    """Persist one source compilation and rebuild its release projection."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any] | Any],
        *,
        object_store: ContentAddressedObjectStore,
        principal_ids: Sequence[str],
        jsonb_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if not callable(connection_provider):
            raise TypeError("connection_provider must be callable")
        if not hasattr(object_store, "put"):
            raise TypeError("object_store must implement put")
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
        self._connection_provider = connection_provider
        self.object_store = object_store
        self.principal_ids = normalized_principals
        self._jsonb_factory = jsonb_factory or _default_jsonb

    @classmethod
    def from_dsn(
        cls,
        database_url: str,
        *,
        object_store: ContentAddressedObjectStore,
        **kwargs: Any,
    ) -> "PostgresCompilerPersistence":
        if not isinstance(database_url, str) or not database_url.strip():
            raise ValueError("database_url must not be empty")

        def connect() -> Any:
            psycopg = _load_psycopg()
            return psycopg.connect(database_url)

        return cls(connect, object_store=object_store, **kwargs)

    def persist(
        self,
        *,
        source_bytes: bytes,
        compilation: CompilationResult,
        projections: ProjectionBundle,
        context: CompilerPersistenceContext,
    ) -> PersistedCompilation:
        if not isinstance(source_bytes, bytes):
            raise TypeError("source_bytes must be bytes")
        if sha256_bytes(source_bytes) != compilation.probe.source_sha256:
            raise PersistenceInvariantError(
                "source bytes do not match the compiler source hash"
            )
        if not compilation.quality.publishable:
            raise PersistenceInvariantError(
                f"quality status {compilation.quality.status!r} is not publishable"
            )

        identity = _resolve_identity(compilation, projections, context)
        artifact_canonical_id = stable_id(
            "artifact_set",
            identity.canonical_source_version_id,
            context.compiler_fingerprint,
        )
        proposed_artifact_id = canonical_uuid(artifact_canonical_id)
        _validate_projection_payload(compilation.document, projections)
        _build_core_plan(
            compilation.document,
            projections,
            artifact_id=proposed_artifact_id,
            artifact_canonical_id=artifact_canonical_id,
        )

        # External immutable writes deliberately precede the SQL transaction.
        source_object = self.object_store.put(
            source_bytes,
            media_type=compilation.probe.media_type,
            suffix=_suffix(compilation.probe.source_name),
        )
        canonical_bytes = compilation.document.to_json().encode("utf-8")
        canonical_object = self.object_store.put(
            canonical_bytes,
            media_type="application/json",
            suffix=".canonical.json",
        )
        if source_object.sha256 != compilation.probe.source_sha256:
            raise PersistenceInvariantError(
                "object store returned a different source digest"
            )
        if canonical_object.sha256 != sha256_bytes(canonical_bytes):
            raise PersistenceInvariantError(
                "object store returned a different canonical digest"
            )
        page_indexes = {page.index for page in compilation.document.pages}
        seen_render_indexes: set[int] = set()
        page_render_objects: list[tuple[int, StoredObject]] = []
        for render in compilation.page_renders:
            if render.page_index not in page_indexes:
                raise PersistenceInvariantError(
                    f"rendered page {render.page_index + 1} is not in the document"
                )
            if render.page_index in seen_render_indexes:
                raise PersistenceInvariantError(
                    f"rendered page {render.page_index + 1} is duplicated"
                )
            seen_render_indexes.add(render.page_index)
            stored_render = self.object_store.put(
                render.image_bytes,
                media_type=render.media_type,
                suffix=".page.png",
            )
            if (
                stored_render.sha256 != render.sha256
                or stored_render.byte_size != len(render.image_bytes)
            ):
                raise PersistenceInvariantError(
                    f"object store returned different rendered page bytes "
                    f"for page {render.page_index + 1}"
                )
            page_render_objects.append((render.page_index, stored_render))
        page_render_objects.sort(key=lambda item: item[0])
        render_object_map = dict(page_render_objects)

        tenant_id = canonical_uuid(context.tenant_id)
        kb_id = canonical_uuid(context.knowledge_base_id)
        release_id = canonical_uuid(context.release_id)
        proposed_source_id = canonical_uuid(identity.canonical_source_id)
        proposed_version_id = canonical_uuid(
            identity.canonical_source_version_id
        )

        with self._connection() as connection, connection.transaction():
            self._set_context(
                connection,
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            self._lock_release(
                connection,
                tenant_id=tenant_id,
                kb_id=kb_id,
                release_id=release_id,
                context=context,
            )
            self._upsert_object(connection, source_object)
            self._upsert_object(connection, canonical_object)
            for stored_render in {
                stored.sha256: stored
                for _, stored in page_render_objects
            }.values():
                self._upsert_object(connection, stored_render)
            source_id = self._upsert_source(
                connection,
                proposed_source_id=proposed_source_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                identity=identity,
                context=context,
                document=compilation.document,
            )
            source_version_id = self._upsert_source_version(
                connection,
                proposed_version_id=proposed_version_id,
                source_id=source_id,
                source_object=source_object,
                canonical_object=canonical_object,
                identity=identity,
                context=context,
                document=compilation.document,
            )
            artifact_id, reused, stored_summary = self._resolve_artifact(
                connection,
                proposed_artifact_id=proposed_artifact_id,
                artifact_canonical_id=artifact_canonical_id,
                source_id=source_id,
                source_version_id=source_version_id,
                context=context,
                compilation=compilation,
                canonical_object=canonical_object,
            )
            plan = _build_core_plan(
                compilation.document,
                projections,
                artifact_id=artifact_id,
                artifact_canonical_id=artifact_canonical_id,
            )
            if reused:
                self._verify_reusable_artifact(
                    stored_summary=stored_summary,
                    plan=plan,
                    compilation=compilation,
                    page_render_objects=render_object_map,
                )
            else:
                self._insert_core(
                    connection,
                    compilation=compilation,
                    projections=projections,
                    plan=plan,
                    page_render_objects=render_object_map,
                )
                self._finalize_artifact(
                    connection,
                    compilation=compilation,
                    plan=plan,
                    canonical_object=canonical_object,
                    compiler_fingerprint=context.compiler_fingerprint,
                    page_render_objects=render_object_map,
                )

            self._upsert_release_membership(
                connection,
                kb_id=kb_id,
                release_id=release_id,
                source_id=source_id,
                source_version_id=source_version_id,
                artifact_id=artifact_id,
                ordinal=context.ordinal,
                reused=reused,
            )
            projection_counts = self._rebuild_release_projection(
                connection,
                compilation=compilation,
                projections=projections,
                plan=plan,
                kb_id=kb_id,
                release_id=release_id,
                source_id=source_id,
                source_version_id=source_version_id,
                context=context,
            )
            self._upsert_quality_checks(
                connection,
                compilation=compilation,
                kb_id=kb_id,
                release_id=release_id,
                artifact_id=artifact_id,
            )

        return PersistedCompilation(
            source_id=source_id,
            source_version_id=source_version_id,
            artifact_set_id=artifact_id,
            source_object=source_object,
            canonical_object=canonical_object,
            page_render_objects=tuple(page_render_objects),
            reused=reused,
            artifact_counts=tuple(sorted(plan.expected_counts.items())),
            projection_counts=tuple(sorted(projection_counts.items())),
        )

    def persist_compilation(
        self,
        source_bytes: bytes,
        compilation: CompilationResult,
        projections: ProjectionBundle,
        context: CompilerPersistenceContext,
    ) -> PersistedCompilation:
        """Positional-friendly alias for worker adapters."""

        return self.persist(
            source_bytes=source_bytes,
            compilation=compilation,
            projections=projections,
            context=context,
        )

    def _set_context(
        self,
        connection: Any,
        *,
        tenant_id: str,
        kb_id: str,
    ) -> None:
        # Parameterized set_config(..., true) is PostgreSQL's SET LOCAL
        # equivalent and cannot leak scope through a pooled connection.
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (tenant_id,),
        )
        connection.execute(
            "SELECT set_config('app.knowledge_base_id', %s, true)",
            (kb_id,),
        )
        connection.execute(
            "SELECT set_config('app.principal_ids', %s, true)",
            (json.dumps(self.principal_ids, separators=(",", ":")),),
        )

    def _lock_release(
        self,
        connection: Any,
        *,
        tenant_id: str,
        kb_id: str,
        release_id: str,
        context: CompilerPersistenceContext,
    ) -> None:
        # A row-level FOR UPDATE lock would require granting the compiler an
        # UPDATE capability on the release-control table. Keep lifecycle
        # mutation admin-only and serialize compilation for this release with
        # a transaction-scoped, namespaced advisory lock instead.
        connection.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended(%s, %s)
            )
            """,
            (release_id, COMPILER_RELEASE_LOCK_SEED),
        )
        cursor = connection.execute(
            """
            SELECT release.status,
                   release.manifest_sha256,
                   release.compiler_fingerprint,
                   release.embedding_model_version
              FROM bauer_rag_v3.knowledge_releases AS release
              JOIN bauer_rag_v3.knowledge_bases AS kb
                ON kb.kb_id = release.kb_id
             WHERE release.release_id = %s::uuid
               AND release.kb_id = %s::uuid
               AND kb.tenant_id = %s::uuid
            """,
            (release_id, kb_id, tenant_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise ReleaseStateError("target release was not found in its tenant")
        status = str(_row_field(row, "status", 0))
        manifest_sha256 = str(_row_field(row, "manifest_sha256", 1))
        fingerprint = str(_row_field(row, "compiler_fingerprint", 2))
        embedding_version = str(
            _row_field(row, "embedding_model_version", 3)
        )
        if status != "building":
            raise ReleaseStateError(
                f"release must be building, not {status!r}"
            )
        if manifest_sha256 != context.manifest_sha256:
            raise ReleaseStateError(
                "release manifest differs from this compile request"
            )
        if fingerprint != context.compiler_fingerprint:
            raise ReleaseStateError(
                "release compiler fingerprint differs from this compiler"
            )
        if embedding_version != context.embedding_model_version:
            raise ReleaseStateError(
                "release embedding model differs from this projection"
            )

    def _upsert_object(
        self,
        connection: Any,
        stored: StoredObject,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO bauer_rag_v3.objects AS object (
                sha256, object_key, byte_size, mime_type, verified_at
            )
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (sha256) DO UPDATE
                SET verified_at = now()
              WHERE object.byte_size = EXCLUDED.byte_size
            RETURNING object.sha256,
                      object.object_key,
                      object.byte_size,
                      object.mime_type
            """,
            (
                stored.sha256,
                stored.object_key,
                stored.byte_size,
                stored.media_type,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise PersistenceInvariantError(
                f"object registry conflicts with bytes {stored.sha256}"
            )
        if (
            str(_row_field(row, "sha256", 0)) != stored.sha256
            or int(_row_field(row, "byte_size", 2)) != stored.byte_size
        ):
            raise PersistenceInvariantError(
                f"object registry failed integrity verification for {stored.sha256}"
            )

    def _upsert_source(
        self,
        connection: Any,
        *,
        proposed_source_id: str,
        tenant_id: str,
        kb_id: str,
        identity: _Identity,
        context: CompilerPersistenceContext,
        document: Document,
    ) -> str:
        metadata = {
            **_json_object(context.source_metadata, "source_metadata"),
            "canonical_source_id": identity.canonical_source_id,
            "canonical_document_id": document.document_id,
            "source_name": document.source_name,
        }
        cursor = connection.execute(
            """
            INSERT INTO bauer_rag_v3.sources AS source (
                source_id,
                tenant_id,
                kb_id,
                external_file_id,
                canonical_uri,
                source_type,
                visibility,
                metadata
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s
            )
            ON CONFLICT (kb_id, external_file_id) DO UPDATE
                -- Source registry identity is immutable after first insert.
                -- A no-op update is used only so an idempotent retry can
                -- return the existing ID without widening worker privileges.
                SET updated_at = source.updated_at
              WHERE source.tenant_id = EXCLUDED.tenant_id
                AND source.source_type = EXCLUDED.source_type
                AND source.visibility = EXCLUDED.visibility
                AND (
                    NOT (source.metadata ? 'canonical_source_id')
                    OR source.metadata ->> 'canonical_source_id' =
                       EXCLUDED.metadata ->> 'canonical_source_id'
                )
            RETURNING source.source_id::text
            """,
            (
                proposed_source_id,
                tenant_id,
                kb_id,
                context.external_file_id,
                context.canonical_uri,
                context.source_type,
                context.visibility,
                self._jsonb(metadata),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise PersistenceInvariantError(
                "external_file_id already exists with incompatible source semantics"
            )
        return str(_row_field(row, "source_id", 0))

    def _upsert_source_version(
        self,
        connection: Any,
        *,
        proposed_version_id: str,
        source_id: str,
        source_object: StoredObject,
        canonical_object: StoredObject,
        identity: _Identity,
        context: CompilerPersistenceContext,
        document: Document,
    ) -> str:
        metadata = {
            "canonical_source_version_id": identity.canonical_source_version_id,
            "canonical_document_id": document.document_id,
            "canonical_object_sha256": canonical_object.sha256,
            "canonical_object_key": canonical_object.object_key,
            "media_type": document.media_type,
            "parser_id": document.parser_id,
            "parser_version": document.parser_version,
        }
        cursor = connection.execute(
            """
            INSERT INTO bauer_rag_v3.source_versions AS version (
                source_version_id,
                source_id,
                sha256,
                filename,
                source_revision,
                publication_date,
                language,
                page_count,
                discovered_metadata
            )
            VALUES (
                %s::uuid, %s::uuid, %s, %s, %s, %s::date,
                %s, %s, %s
            )
            ON CONFLICT (source_id, sha256) DO UPDATE
                -- Immutable source bytes have one immutable registry row.
                -- Parser-specific canonical data belongs to artifact_sets;
                -- retries may return, but never rewrite, this source version.
                SET source_version_id = version.source_version_id
              WHERE
                    version.filename = EXCLUDED.filename
                AND version.source_revision IS NOT DISTINCT FROM
                    EXCLUDED.source_revision
                AND version.publication_date IS NOT DISTINCT FROM
                    EXCLUDED.publication_date
                AND (
                    version.language IS NULL
                    OR EXCLUDED.language IS NULL
                    OR version.language = EXCLUDED.language
                )
                AND (
                    version.page_count IS NULL
                    OR EXCLUDED.page_count IS NULL
                    OR version.page_count = EXCLUDED.page_count
                )
                AND (
                    NOT (
                        version.discovered_metadata
                            ? 'canonical_source_version_id'
                    )
                    OR version.discovered_metadata
                        ->> 'canonical_source_version_id' =
                       EXCLUDED.discovered_metadata
                        ->> 'canonical_source_version_id'
                )
            RETURNING version.source_version_id::text
            """,
            (
                proposed_version_id,
                source_id,
                source_object.sha256,
                document.source_name,
                context.source_revision,
                context.publication_date,
                document.language,
                len(document.pages),
                self._jsonb(metadata),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise PersistenceInvariantError("source version upsert returned no row")
        return str(_row_field(row, "source_version_id", 0))

    def _resolve_artifact(
        self,
        connection: Any,
        *,
        proposed_artifact_id: str,
        artifact_canonical_id: str,
        source_id: str,
        source_version_id: str,
        context: CompilerPersistenceContext,
        compilation: CompilationResult,
        canonical_object: StoredObject,
    ) -> tuple[str, bool, Mapping[str, Any]]:
        existing = self._select_artifact(
            connection,
            source_version_id=source_version_id,
            compiler_fingerprint=context.compiler_fingerprint,
        )
        if existing is not None:
            return self._reuse_or_raise(existing, len(compilation.document.pages))

        cursor = connection.execute(
            """
            INSERT INTO bauer_rag_v3.artifact_sets (
                artifact_set_id,
                source_id,
                source_version_id,
                compiler_fingerprint,
                status,
                parser_version,
                ocr_version,
                fact_model_version,
                worker_job_id,
                expected_page_count,
                quality_summary
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s, 'building',
                %s, %s, %s, %s::uuid, %s, %s
            )
            ON CONFLICT (source_version_id, compiler_fingerprint)
                DO NOTHING
            RETURNING artifact_set_id::text, status, quality_summary
            """,
            (
                proposed_artifact_id,
                source_id,
                source_version_id,
                context.compiler_fingerprint,
                compilation.document.parser_version,
                context.ocr_version,
                context.fact_model_version,
                (
                    canonical_uuid(context.worker_job_id)
                    if context.worker_job_id
                    else None
                ),
                len(compilation.document.pages),
                self._jsonb(
                    {
                        "phase": "building",
                        "canonical_artifact_set_id": artifact_canonical_id,
                        "canonical_object_sha256": canonical_object.sha256,
                    }
                ),
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            return str(_row_field(row, "artifact_set_id", 0)), False, {}

        raced = self._select_artifact(
            connection,
            source_version_id=source_version_id,
            compiler_fingerprint=context.compiler_fingerprint,
        )
        if raced is None:
            raise ArtifactStateError(
                "artifact uniqueness conflict disappeared during resolution"
            )
        return self._reuse_or_raise(raced, len(compilation.document.pages))

    def _select_artifact(
        self,
        connection: Any,
        *,
        source_version_id: str,
        compiler_fingerprint: str,
    ) -> Any | None:
        cursor = connection.execute(
            """
            SELECT artifact_set_id::text,
                   status,
                   expected_page_count,
                   quality_summary
              FROM bauer_rag_v3.artifact_sets
             WHERE source_version_id = %s::uuid
               AND compiler_fingerprint = %s
             FOR UPDATE
            """,
            (source_version_id, compiler_fingerprint),
        )
        return cursor.fetchone()

    @staticmethod
    def _reuse_or_raise(
        row: Any,
        expected_page_count: int,
    ) -> tuple[str, bool, Mapping[str, Any]]:
        artifact_id = str(_row_field(row, "artifact_set_id", 0))
        status = str(_row_field(row, "status", 1))
        stored_pages = _row_field(row, "expected_page_count", 2)
        summary = _row_field(row, "quality_summary", 3) or {}
        if status != "valid":
            raise ArtifactStateError(
                f"existing artifact {artifact_id} is {status!r}, not reusable"
            )
        if stored_pages is None or int(stored_pages) != expected_page_count:
            raise ArtifactStateError(
                "valid artifact page count differs from compiler output"
            )
        if not isinstance(summary, Mapping):
            raise ArtifactStateError("valid artifact has an invalid quality summary")
        return artifact_id, True, dict(summary)

    def _verify_reusable_artifact(
        self,
        *,
        stored_summary: Mapping[str, Any],
        plan: _CorePlan,
        compilation: CompilationResult,
        page_render_objects: Mapping[int, StoredObject],
    ) -> None:
        fingerprint = stored_summary.get("projection_fingerprint")
        if fingerprint is not None and fingerprint != plan.projection_fingerprint:
            raise ArtifactStateError(
                "valid artifact projection fingerprint differs from compiler output"
            )
        canonical_sha = stored_summary.get("canonical_document_sha256")
        actual_sha = sha256_bytes(
            compilation.document.to_json().encode("utf-8")
        )
        if canonical_sha is not None and canonical_sha != actual_sha:
            raise ArtifactStateError(
                "valid artifact canonical document hash differs from compiler output"
            )
        stored_renders = stored_summary.get("page_renders")
        if stored_renders is not None:
            if not isinstance(stored_renders, list):
                raise ArtifactStateError(
                    "valid artifact has invalid rendered page metadata"
                )
            try:
                stored_render_hashes = {
                    int(item["page_number"]) - 1: str(item["sha256"])
                    for item in stored_renders
                    if isinstance(item, Mapping)
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ArtifactStateError(
                    "valid artifact has invalid rendered page metadata"
                ) from exc
            current_render_hashes = {
                page_index: stored.sha256
                for page_index, stored in page_render_objects.items()
            }
            if stored_render_hashes != current_render_hashes:
                raise ArtifactStateError(
                    "valid artifact rendered page hashes differ from compiler output"
                )
        stored_counts = stored_summary.get("counts")
        if isinstance(stored_counts, Mapping):
            for name, expected in plan.expected_counts.items():
                if name in stored_counts and int(stored_counts[name]) != expected:
                    raise ArtifactStateError(
                        f"valid artifact {name} count differs from compiler output"
                    )

    def _insert_core(
        self,
        connection: Any,
        *,
        compilation: CompilationResult,
        projections: ProjectionBundle,
        plan: _CorePlan,
        page_render_objects: Mapping[int, StoredObject],
    ) -> None:
        self._insert_pages(
            connection,
            compilation.document,
            plan,
            page_render_objects=page_render_objects,
        )
        self._insert_sections(connection, plan)
        self._insert_blocks(connection, compilation.document, plan)
        self._insert_tables_and_cells(connection, compilation.document, plan)
        self._insert_provenance(connection, plan)
        self._insert_facts(connection, projections, plan)

    def _insert_pages(
        self,
        connection: Any,
        document: Document,
        plan: _CorePlan,
        *,
        page_render_objects: Mapping[int, StoredObject],
    ) -> None:
        for page in document.pages:
            signals = dict(page.signals)
            raw_confidence = signals.get("ocr_average_confidence")
            if raw_confidence is None or raw_confidence == "":
                confidence = None
            else:
                try:
                    confidence = float(raw_confidence)
                except (TypeError, ValueError) as exc:
                    raise PersistenceInvariantError(
                        f"page {page.index + 1} has invalid OCR confidence"
                    ) from exc
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise PersistenceInvariantError(
                        f"page {page.index + 1} has invalid OCR confidence"
                    )
            rendered_object = page_render_objects.get(page.index)
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.pages (
                    page_id, artifact_set_id, page_number, page_label,
                    width_points, height_points, rotation_degrees,
                    rendered_object_sha256, ocr_confidence, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    plan.page_ids[page.index],
                    plan.artifact_id,
                    page.index + 1,
                    page.printed_label,
                    page.width,
                    page.height,
                    page.rotation,
                    rendered_object.sha256 if rendered_object else None,
                    confidence,
                    self._jsonb(
                        {
                            "canonical_id": page.page_id,
                            "source_locator": page.source_locator,
                            "parser_id": page.parser_id,
                            "ocr_needed": page.ocr_needed,
                            "signals": dict(page.signals),
                        }
                    ),
                ),
            )

    def _insert_sections(
        self,
        connection: Any,
        plan: _CorePlan,
    ) -> None:
        for section in plan.sections:
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.sections (
                    section_id, artifact_set_id, parent_section_id,
                    ordinal, level, title, normalized_title,
                    start_page_number, end_page_number, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s,
                    %s, %s, %s
                )
                """,
                (
                    section.database_id,
                    plan.artifact_id,
                    section.parent_database_id,
                    section.ordinal,
                    len(section.path),
                    section.path[-1],
                    normalize_text(section.path[-1]),
                    section.start_page,
                    section.end_page,
                    self._jsonb(
                        {
                            "canonical_id": section.canonical_id,
                            "canonical_path": list(section.path),
                        }
                    ),
                ),
            )

    def _insert_blocks(
        self,
        connection: Any,
        document: Document,
        plan: _CorePlan,
    ) -> None:
        for page in document.pages:
            for block in page.blocks:
                bbox = _database_bbox(block.bbox)
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.blocks (
                        block_id, artifact_set_id, page_id, section_id,
                        reading_order, block_type, original_text,
                        normalized_text, x0, y0, x1, y1,
                        confidence, metadata
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        plan.block_ids[block.block_id],
                        plan.artifact_id,
                        plan.page_ids[page.index],
                        plan.block_sections[block.block_id],
                        block.order,
                        _block_type(block.kind),
                        block.text,
                        normalize_text(block.text),
                        *_bbox_values(bbox),
                        block.confidence,
                        self._jsonb(
                            {
                                "canonical_id": block.block_id,
                                "canonical_kind": block.kind,
                                "source_locator": block.source_locator,
                                "parser_id": block.parser_id,
                                "section_path": list(block.section_path),
                                "attributes": dict(block.attributes),
                                "discarded_degenerate_bbox": (
                                    block.bbox is not None and bbox is None
                                ),
                            }
                        ),
                    ),
                )

    def _insert_tables_and_cells(
        self,
        connection: Any,
        document: Document,
        plan: _CorePlan,
    ) -> None:
        table_ordinal = 0
        for page in document.pages:
            for table in page.tables:
                database_table_id = plan.table_ids[table.table_id]
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.tables (
                        table_id, artifact_set_id, section_id, ordinal,
                        title, caption, row_count, column_count,
                        header_row_count, confidence, metadata
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        database_table_id,
                        plan.artifact_id,
                        plan.table_sections[table.table_id],
                        table_ordinal,
                        table.caption,
                        table.caption,
                        table.row_count,
                        table.column_count,
                        _header_row_count(table),
                        table.confidence,
                        self._jsonb(
                            {
                                "canonical_id": table.table_id,
                                "canonical_order": table.order,
                                "source_locator": table.source_locator,
                                "parser_id": table.parser_id,
                                "section_path": list(table.section_path),
                                "attributes": dict(table.attributes),
                            }
                        ),
                    ),
                )
                table_ordinal += 1
                native_segment_bbox = _database_bbox(table.bbox)
                segment_bbox = native_segment_bbox or (0.0, 0.0, 1.0, 1.0)
                segment_canonical_id = stable_id(
                    "table_segment",
                    table.table_id,
                    page.page_id,
                    0,
                )
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.table_segments (
                        table_segment_id, artifact_set_id, table_id,
                        page_id, segment_ordinal, x0, y0, x1, y1, metadata
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        0, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        canonical_uuid(
                            segment_canonical_id, scope=plan.artifact_id
                        ),
                        plan.artifact_id,
                        database_table_id,
                        plan.page_ids[page.index],
                        *segment_bbox,
                        self._jsonb(
                            {
                                "canonical_id": segment_canonical_id,
                                "canonical_table_id": table.table_id,
                                "source_locator": table.source_locator,
                                "synthetic_full_page_bbox": (
                                    native_segment_bbox is None
                                ),
                            }
                        ),
                    ),
                )
                for cell in table.cells:
                    bbox = _database_bbox(cell.bbox)
                    connection.execute(
                        """
                        INSERT INTO bauer_rag_v3.table_cells (
                            cell_id, artifact_set_id, table_id, page_id,
                            row_index, column_index, row_span, column_span,
                            cell_role, raw_text, normalized_text,
                            numeric_value, unit_raw, unit_ucum,
                            x0, y0, x1, y1, confidence, metadata
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            plan.cell_ids[cell.cell_id],
                            plan.artifact_id,
                            database_table_id,
                            plan.page_ids[page.index],
                            cell.row,
                            cell.column,
                            cell.row_span,
                            cell.column_span,
                            _cell_role(cell.role),
                            cell.text,
                            normalize_text(cell.text),
                            None,
                            None,
                            None,
                            *_bbox_values(bbox),
                            cell.confidence,
                            self._jsonb(
                                {
                                    "canonical_id": cell.cell_id,
                                    "canonical_role": cell.role,
                                    "group": cell.group,
                                    "source_locator": cell.source_locator,
                                    "parser_id": cell.parser_id,
                                    "attributes": dict(cell.attributes),
                                    "discarded_degenerate_bbox": (
                                        cell.bbox is not None and bbox is None
                                    ),
                                }
                            ),
                        ),
                    )

    def _insert_provenance(
        self,
        connection: Any,
        plan: _CorePlan,
    ) -> None:
        for provenance in plan.provenance:
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.provenance_spans (
                    provenance_id, artifact_set_id, page_id, block_id,
                    cell_id, char_start, char_end, x0, y0, x1, y1,
                    quoted_text, quote_sha256, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    provenance.database_id,
                    plan.artifact_id,
                    provenance.page_id,
                    provenance.block_id,
                    provenance.cell_id,
                    provenance.char_start,
                    provenance.char_end,
                    *_bbox_values(provenance.bbox),
                    provenance.quoted_text,
                    provenance.quote_sha256,
                    self._jsonb(
                        {
                            **provenance.metadata,
                            "canonical_id": provenance.canonical_id,
                            "canonical_evidence_id": provenance.evidence_id,
                        }
                    ),
                ),
            )

    def _insert_facts(
        self,
        connection: Any,
        projections: ProjectionBundle,
        plan: _CorePlan,
    ) -> None:
        entity_ids: dict[str, str] = {}
        for fact in projections.facts:
            if fact.value_kind != "entity":
                continue
            entity_value = str(fact.entity_value or "").strip()
            normalized = normalize_text(entity_value)
            if not normalized:
                raise PersistenceInvariantError(
                    f"entity fact {fact.fact_id} has no normalized object"
                )
            if normalized in entity_ids:
                continue
            canonical_entity_id = stable_id(
                "entity",
                plan.artifact_canonical_id,
                "fact_object",
                normalized,
            )
            entity_id = canonical_uuid(
                canonical_entity_id, scope=plan.artifact_id
            )
            entity_ids[normalized] = entity_id
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.entities (
                    entity_id, artifact_set_id, entity_type,
                    canonical_name, normalized_key, metadata
                )
                VALUES (%s::uuid, %s::uuid, 'fact_object', %s, %s, %s)
                """,
                (
                    entity_id,
                    plan.artifact_id,
                    entity_value,
                    normalized,
                    self._jsonb({"canonical_id": canonical_entity_id}),
                ),
            )

        for fact in projections.facts:
            provenance_ids = _fact_provenance_ids(fact, plan)
            fact_id = canonical_uuid(fact.fact_id, scope=plan.artifact_id)
            typed = _fact_typed_values(fact, entity_ids)
            fact_sha256 = sha256_json(
                {
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "value_kind": fact.value_kind,
                    "raw_value": fact.raw_value,
                    "typed": [str(value) if value is not None else None for value in typed],
                    "unit_raw": fact.raw_unit,
                    "unit_ucum": fact.normalized_unit,
                    "qualifiers": fact.qualifiers,
                    "provenance": list(fact.provenance_evidence_ids),
                }
            )
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.facts (
                    fact_id, artifact_set_id, fact_sha256,
                    subject_entity_id, subject_key, predicate,
                    value_kind, raw_value, value_text, numeric_value,
                    date_value, boolean_value, object_entity_id,
                    unit_raw, unit_ucum, qualifiers, confidence,
                    extraction_method, verification_status,
                    primary_provenance_id, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s, NULL, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s::uuid,
                    %s, %s, %s, %s, %s, %s, %s::uuid, %s
                )
                """,
                (
                    fact_id,
                    plan.artifact_id,
                    fact_sha256,
                    fact.subject,
                    fact.predicate,
                    fact.value_kind,
                    fact.raw_value,
                    *typed,
                    fact.raw_unit,
                    fact.normalized_unit,
                    self._jsonb(fact.qualifiers),
                    fact.confidence,
                    "deterministic_projection",
                    _verification_status(fact.review_status),
                    provenance_ids[0],
                    self._jsonb(
                        {
                            "canonical_id": fact.fact_id,
                            "projected_artifact_set_id": fact.artifact_set_id,
                        }
                    ),
                ),
            )
            supporting_ordinal = 0
            for ordinal, provenance_id in enumerate(provenance_ids):
                role = "primary" if ordinal == 0 else "supporting"
                role_ordinal = 0 if ordinal == 0 else supporting_ordinal
                if ordinal > 0:
                    supporting_ordinal += 1
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.fact_provenance (
                        fact_id, artifact_set_id, provenance_id,
                        provenance_role, ordinal
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s)
                    """,
                    (
                        fact_id,
                        plan.artifact_id,
                        provenance_id,
                        role,
                        role_ordinal,
                    ),
                )

    def _finalize_artifact(
        self,
        connection: Any,
        *,
        compilation: CompilationResult,
        plan: _CorePlan,
        canonical_object: StoredObject,
        compiler_fingerprint: str,
        page_render_objects: Mapping[int, StoredObject],
    ) -> None:
        names = (
            "pages",
            "sections",
            "blocks",
            "tables",
            "table_segments",
            "table_cells",
            "provenance_spans",
            "entities",
            "facts",
            "fact_provenance",
        )
        cursor = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM bauer_rag_v3.pages
                  WHERE artifact_set_id = %s::uuid) AS pages,
                (SELECT count(*) FROM bauer_rag_v3.sections
                  WHERE artifact_set_id = %s::uuid) AS sections,
                (SELECT count(*) FROM bauer_rag_v3.blocks
                  WHERE artifact_set_id = %s::uuid) AS blocks,
                (SELECT count(*) FROM bauer_rag_v3.tables
                  WHERE artifact_set_id = %s::uuid) AS tables,
                (SELECT count(*) FROM bauer_rag_v3.table_segments
                  WHERE artifact_set_id = %s::uuid) AS table_segments,
                (SELECT count(*) FROM bauer_rag_v3.table_cells
                  WHERE artifact_set_id = %s::uuid) AS table_cells,
                (SELECT count(*) FROM bauer_rag_v3.provenance_spans
                  WHERE artifact_set_id = %s::uuid) AS provenance_spans,
                (SELECT count(*) FROM bauer_rag_v3.entities
                  WHERE artifact_set_id = %s::uuid) AS entities,
                (SELECT count(*) FROM bauer_rag_v3.facts
                  WHERE artifact_set_id = %s::uuid) AS facts,
                (SELECT count(*) FROM bauer_rag_v3.fact_provenance
                  WHERE artifact_set_id = %s::uuid) AS fact_provenance
            """,
            (plan.artifact_id,) * len(names),
        )
        row = cursor.fetchone()
        if row is None:
            raise PersistenceInvariantError("artifact count verification returned no row")
        actual = {
            name: int(_row_field(row, name, index))
            for index, name in enumerate(names)
        }
        expected = {name: int(plan.expected_counts[name]) for name in names}
        if actual != expected:
            raise PersistenceInvariantError(
                f"artifact row counts do not match compiler output: "
                f"expected={expected}, actual={actual}"
            )
        if not compilation.quality.publishable:
            raise PersistenceInvariantError(
                "artifact cannot finalize without a publishable QA report"
            )

        summary = {
            "canonical_artifact_set_id": plan.artifact_canonical_id,
            "canonical_document_id": compilation.document.document_id,
            "canonical_document_sha256": canonical_object.sha256,
            "canonical_object_key": canonical_object.object_key,
            "compiler_fingerprint": compiler_fingerprint,
            "projection_fingerprint": plan.projection_fingerprint,
            "quality_status": compilation.quality.status,
            "quality_metrics": dict(compilation.quality.metrics),
            "quality_issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "location": issue.location,
                }
                for issue in compilation.quality.issues
            ],
            "page_renders": [
                {
                    "page_number": render.page_index + 1,
                    "sha256": page_render_objects[render.page_index].sha256,
                    "object_key": page_render_objects[render.page_index].object_key,
                    "media_type": render.media_type,
                    "width_pixels": render.width_pixels,
                    "height_pixels": render.height_pixels,
                    "dpi": render.dpi,
                }
                for render in compilation.page_renders
            ],
            "counts": expected,
        }
        cursor = connection.execute(
            """
            UPDATE bauer_rag_v3.artifact_sets AS artifact
               SET status = 'valid',
                   expected_page_count = %s,
                   quality_summary = %s,
                   finalized_at = now(),
                   error = NULL
             WHERE artifact.artifact_set_id = %s::uuid
               AND artifact.status = 'building'
               AND (
                    SELECT count(*) FROM bauer_rag_v3.pages
                     WHERE artifact_set_id = artifact.artifact_set_id
               ) = %s
               AND (
                    SELECT count(*) FROM bauer_rag_v3.provenance_spans
                     WHERE artifact_set_id = artifact.artifact_set_id
               ) = %s
            RETURNING artifact.artifact_set_id::text
            """,
            (
                len(compilation.document.pages),
                self._jsonb(summary),
                plan.artifact_id,
                expected["pages"],
                expected["provenance_spans"],
            ),
        )
        if cursor.fetchone() is None:
            raise ArtifactStateError(
                "artifact changed state or counts before finalization"
            )

    def _upsert_release_membership(
        self,
        connection: Any,
        *,
        kb_id: str,
        release_id: str,
        source_id: str,
        source_version_id: str,
        artifact_id: str,
        ordinal: int,
        reused: bool,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO bauer_rag_v3.release_sources AS membership (
                kb_id, release_id, source_id, source_version_id,
                artifact_set_id, ordinal, action
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                %s::uuid, %s, %s
            )
            ON CONFLICT (release_id, source_id) DO UPDATE
                SET added_at = membership.added_at
              WHERE membership.kb_id = EXCLUDED.kb_id
                AND membership.source_version_id = EXCLUDED.source_version_id
                AND membership.artifact_set_id = EXCLUDED.artifact_set_id
                AND membership.ordinal = EXCLUDED.ordinal
            RETURNING membership.source_id::text
            """,
            (
                kb_id,
                release_id,
                source_id,
                source_version_id,
                artifact_id,
                ordinal,
                "reused" if reused else "compiled",
            ),
        )
        if cursor.fetchone() is None:
            raise PersistenceInvariantError(
                "release already contains incompatible source membership"
            )

    def _rebuild_release_projection(
        self,
        connection: Any,
        *,
        compilation: CompilationResult,
        projections: ProjectionBundle,
        plan: _CorePlan,
        kb_id: str,
        release_id: str,
        source_id: str,
        source_version_id: str,
        context: CompilerPersistenceContext,
    ) -> dict[str, int]:
        # These are release-owned materializations. Rebuilding them is required
        # even when the immutable source artifact was reused.
        connection.execute(
            """
            DELETE FROM bauer_rag_v3.nav_nodes
             WHERE release_id = %s::uuid
               AND source_id = %s::uuid
            """,
            (release_id, source_id),
        )
        connection.execute(
            """
            DELETE FROM bauer_rag_v3.search_units
             WHERE release_id = %s::uuid
               AND source_id = %s::uuid
            """,
            (release_id, source_id),
        )

        evidence_by_id = {
            evidence.evidence_id: evidence for evidence in projections.evidence
        }
        fact_units: dict[str, Any] = {}
        for fact in projections.facts:
            for evidence_id in fact.provenance_evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is not None:
                    fact_units[
                        stable_id(
                            "search_unit",
                            evidence.release_id,
                            fact.fact_id,
                        )
                    ] = fact
                    break
        evidence_units: dict[str, list[tuple[str, str]]] = {}
        database_search_unit_ids: dict[str, str] = {}
        for unit in projections.search_units:
            evidence = unit.evidence
            if evidence.evidence_id not in evidence_by_id:
                raise PersistenceInvariantError(
                    f"search unit {unit.search_unit_id} references unknown evidence"
                )
            provenance_ids = plan.evidence_provenance.get(
                evidence.evidence_id, ()
            )
            if evidence.is_citable and not provenance_ids:
                raise PersistenceInvariantError(
                    f"citable search unit {unit.search_unit_id} lacks provenance"
                )
            projected_fact = fact_units.get(unit.search_unit_id)
            primary_provenance_id = (
                _fact_provenance_ids(projected_fact, plan)[0]
                if projected_fact is not None
                else provenance_ids[0]
                if provenance_ids
                else None
            )
            coordinate = evidence.coordinate
            page_id = (
                None
                if evidence.generated_summary
                else plan.page_ids.get(coordinate.page_number - 1)
            )
            table_id = (
                plan.table_ids.get(coordinate.table_id)
                if coordinate.table_id
                else None
            )
            section_id = _projection_section_id(evidence, plan)
            database_unit_id = canonical_uuid(
                unit.search_unit_id, scope=release_id
            )
            database_search_unit_ids[unit.search_unit_id] = database_unit_id
            evidence_units.setdefault(evidence.evidence_id, []).append(
                (unit.unit_type, database_unit_id)
            )
            embedding = _vector_value(unit.embedding)
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.search_units (
                    search_unit_id, kb_id, release_id, source_id,
                    source_version_id, artifact_set_id, unit_key,
                    unit_type, page_id, section_id, table_id,
                    table_row_index, page_start, page_end, language,
                    display_text, search_text, embedding_model_version,
                    embedding, token_count, content_sha256,
                    primary_provenance_id, is_citable,
                    generated_summary, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s::uuid, %s::uuid, %s, %s, %s::uuid,
                    %s::uuid, %s::uuid, %s, %s, %s, %s,
                    %s, %s, %s, %s::vector, %s, %s, %s::uuid,
                    %s, %s, %s
                )
                """,
                (
                    database_unit_id,
                    kb_id,
                    release_id,
                    source_id,
                    source_version_id,
                    plan.artifact_id,
                    unit.search_unit_id,
                    _search_unit_type(unit.unit_type),
                    page_id,
                    section_id,
                    table_id,
                    coordinate.row_index,
                    (
                        None
                        if evidence.generated_summary
                        else coordinate.page_number
                    ),
                    (
                        None
                        if evidence.generated_summary
                        else coordinate.page_number
                    ),
                    evidence.language or compilation.document.language,
                    evidence.content,
                    unit.search_text,
                    context.embedding_model_version,
                    embedding,
                    len(unit.search_text.split()),
                    sha256_bytes(unit.search_text.encode("utf-8")),
                    primary_provenance_id,
                    evidence.is_citable,
                    evidence.generated_summary,
                    self._jsonb(
                        {
                            "canonical_id": unit.search_unit_id,
                            "canonical_evidence_id": evidence.evidence_id,
                            "canonical_fact_id": (
                                projected_fact.fact_id
                                if projected_fact is not None
                                else None
                            ),
                            "source_type": evidence.source_type,
                            "title": evidence.title,
                            "subject": unit.subject,
                            "predicate": unit.predicate,
                            "numeric_value": (
                                str(unit.numeric_value)
                                if unit.numeric_value is not None
                                else None
                            ),
                            "unit": unit.unit,
                            "exact_terms": list(unit.exact_terms),
                            "navigation_path": list(unit.navigation_path),
                            "evidence_metadata": evidence.metadata,
                        }
                    ),
                ),
            )

        for term in projections.exact_terms:
            candidates = evidence_units.get(term.evidence_id, ())
            if not candidates:
                raise PersistenceInvariantError(
                    f"exact term {term.term_id} has no search unit"
                )
            _, search_unit_id = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate[0] != "fact"
                ),
                candidates[0],
            )
            provenance_ids = plan.evidence_provenance.get(
                term.evidence_id, ()
            )
            if not provenance_ids:
                raise PersistenceInvariantError(
                    f"exact term {term.term_id} lacks provenance"
                )
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.exact_terms (
                    exact_term_id, release_id, source_id,
                    source_version_id, search_unit_id, term_type,
                    raw_term, normalized_term, provenance_id,
                    is_primary, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s::uuid, %s, %s, %s, %s::uuid, false, %s
                )
                """,
                (
                    canonical_uuid(term.term_id, scope=release_id),
                    release_id,
                    source_id,
                    source_version_id,
                    search_unit_id,
                    term.term_type,
                    term.raw_term,
                    term.normalized_term,
                    provenance_ids[0],
                    self._jsonb(
                        {
                            "canonical_id": term.term_id,
                            "canonical_evidence_id": term.evidence_id,
                        }
                    ),
                ),
            )

        description_by_node = {
            link.node_id: link.search_unit_id
            for link in projections.navigation_descriptions
        }
        navigation_ids: list[str] = []
        for index, node in enumerate(projections.navigation_nodes):
            node_id = canonical_uuid(node.node_id, scope=release_id)
            navigation_ids.append(node_id)
            node_type = (
                "artifact"
                if index == len(projections.navigation_nodes) - 1
                else "category"
            )
            description_unit_id = database_search_unit_ids[
                description_by_node[node.node_id]
            ]
            cursor = connection.execute(
                """
                INSERT INTO bauer_rag_v3.nav_nodes AS existing_node (
                    nav_node_id, release_id, source_id, node_type,
                    node_key, label, description_search_unit_id, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s, %s, %s,
                    %s::uuid, %s
                )
                ON CONFLICT (nav_node_id) DO UPDATE
                    SET label = EXCLUDED.label,
                        description_search_unit_id =
                            EXCLUDED.description_search_unit_id,
                        metadata = EXCLUDED.metadata
                  WHERE existing_node.release_id = EXCLUDED.release_id
                    AND existing_node.source_id = EXCLUDED.source_id
                    AND existing_node.node_type = EXCLUDED.node_type
                    AND existing_node.node_key = EXCLUDED.node_key
                RETURNING existing_node.nav_node_id::text
                """,
                (
                    node_id,
                    release_id,
                    source_id,
                    node_type,
                    node.node_id,
                    node.label,
                    description_unit_id,
                    self._jsonb(
                        {
                            "canonical_id": node.node_id,
                            "description_search_unit_canonical_id": (
                                description_by_node[node.node_id]
                            ),
                            "aliases": list(node.aliases),
                            "linked_evidence_ids": list(
                                node.linked_evidence_ids
                            ),
                        }
                    ),
                ),
            )
            if cursor.fetchone() is None:
                raise PersistenceInvariantError(
                    f"navigation node {node.node_id} conflicts with another meaning"
                )

        for edge_index, (from_node, to_node) in enumerate(
            zip(navigation_ids, navigation_ids[1:])
        ):
            canonical_edge_id = stable_id(
                "navigation_edge",
                context.release_id,
                projections.navigation_nodes[edge_index].node_id,
                projections.navigation_nodes[edge_index + 1].node_id,
                "contains",
            )
            target_is_document = (
                edge_index + 1 == len(projections.navigation_nodes) - 1
            )
            linked = (
                projections.navigation_nodes[
                    edge_index + 1
                ].linked_evidence_ids
                if target_is_document
                else ()
            )
            provenance_id = (
                next(
                    (
                        plan.evidence_provenance[evidence_id][0]
                        for evidence_id in linked
                        if plan.evidence_provenance.get(evidence_id)
                    ),
                    None,
                )
                if target_is_document
                else None
            )
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.nav_edges (
                    nav_edge_id, release_id, from_node_id, to_node_id,
                    relation_type, confidence, provenance_id, metadata
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    'contains', 1.0, %s::uuid, %s
                )
                ON CONFLICT (
                    release_id, from_node_id, to_node_id, relation_type
                ) DO NOTHING
                """,
                (
                    canonical_uuid(canonical_edge_id, scope=release_id),
                    release_id,
                    from_node,
                    to_node,
                    provenance_id,
                    self._jsonb({"canonical_id": canonical_edge_id}),
                ),
            )

        return {
            "search_units": len(projections.search_units),
            "exact_terms": len(projections.exact_terms),
            "navigation_nodes": len(projections.navigation_nodes),
            "navigation_edges": max(
                len(projections.navigation_nodes) - 1, 0
            ),
        }

    def _upsert_quality_checks(
        self,
        connection: Any,
        *,
        compilation: CompilationResult,
        kb_id: str,
        release_id: str,
        artifact_id: str,
    ) -> None:
        records = [
            (
                "compiler_quality_gate",
                "info",
                "pass" if compilation.quality.status == "pass" else "warn",
                {
                    "quality_status": compilation.quality.status,
                    "metrics": dict(compilation.quality.metrics),
                },
            )
        ]
        for issue in compilation.quality.issues:
            records.append(
                (
                    f"compiler_{issue.code}",
                    _qa_severity(issue.severity),
                    "warn" if issue.severity == "warning" else "fail",
                    {
                        "location": issue.location,
                        "message": issue.message,
                    },
                )
            )
        for check_code, severity, status, details in records:
            canonical_check_id = stable_id(
                "quality_check",
                release_id,
                artifact_id,
                check_code,
                compilation.document.parser_version,
            )
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.qa_checks AS check_row (
                    qa_check_id, kb_id, check_code, checker_version,
                    severity, status, release_id, artifact_set_id, details
                )
                VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s, %s,
                    %s::uuid, %s::uuid, %s
                )
                ON CONFLICT (qa_check_id) DO UPDATE
                    SET severity = EXCLUDED.severity,
                        status = EXCLUDED.status,
                        details = EXCLUDED.details
                  WHERE check_row.release_id = EXCLUDED.release_id
                    AND check_row.artifact_set_id =
                        EXCLUDED.artifact_set_id
                """,
                (
                    canonical_uuid(canonical_check_id, scope=release_id),
                    kb_id,
                    check_code,
                    compilation.document.parser_version,
                    severity,
                    status,
                    release_id,
                    artifact_id,
                    self._jsonb(details),
                ),
            )

    def _jsonb(self, value: Any) -> Any:
        return self._jsonb_factory(_json_value(value))

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_provider()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            with resource as connection:
                yield connection
        else:
            yield resource


# Backwards-friendly descriptive aliases for runtime wiring.
PostgresCompilationStore = PostgresCompilerPersistence
PostgresCompilerRepository = PostgresCompilerPersistence


def _resolve_identity(
    compilation: CompilationResult,
    projections: ProjectionBundle,
    context: CompilerPersistenceContext,
) -> _Identity:
    evidence = projections.evidence
    canonical_source_id = (
        context.source_document_id
        or (evidence[0].source_document_id if evidence else None)
        or stable_id(
            "source_document",
            context.tenant_id,
            context.knowledge_base_id,
            context.external_file_id,
        )
    )
    canonical_version_id = (
        context.source_version_id
        or (evidence[0].source_version_id if evidence else None)
        or stable_id(
            "source_version",
            canonical_source_id,
            compilation.probe.source_sha256,
        )
    )
    identity = _Identity(
        canonical_source_id=canonical_source_id,
        canonical_source_version_id=canonical_version_id,
        tenant_id=canonical_uuid(context.tenant_id),
        knowledge_base_id=canonical_uuid(context.knowledge_base_id),
        release_id=canonical_uuid(context.release_id),
    )
    for item in evidence:
        expected = (
            (item.tenant_id, identity.tenant_id, "tenant"),
            (item.knowledge_base_id, identity.knowledge_base_id, "knowledge base"),
            (item.release_id, identity.release_id, "release"),
            (item.source_document_id, canonical_uuid(canonical_source_id), "source"),
            (item.source_version_id, canonical_uuid(canonical_version_id), "version"),
        )
        for logical_id, database_id, label in expected:
            if canonical_uuid(logical_id) != database_id:
                raise PersistenceInvariantError(
                    f"projection evidence crosses {label} identity"
                )
        if item.source_sha256 != compilation.probe.source_sha256:
            raise PersistenceInvariantError(
                "projection evidence source hash differs from compilation"
            )
    for unit in projections.search_units:
        if canonical_uuid(unit.evidence.release_id) != identity.release_id:
            raise PersistenceInvariantError(
                "search unit belongs to a different release"
            )
    for node in projections.navigation_nodes:
        if canonical_uuid(node.release_id) != identity.release_id:
            raise PersistenceInvariantError(
                "navigation node belongs to a different release"
            )
    return identity


def _build_core_plan(
    document: Document,
    projections: ProjectionBundle,
    *,
    artifact_id: str,
    artifact_canonical_id: str,
) -> _CorePlan:
    page_ids = {
        page.index: canonical_uuid(page.page_id, scope=artifact_id)
        for page in document.pages
    }
    block_ids = {
        block.block_id: canonical_uuid(block.block_id, scope=artifact_id)
        for page in document.pages
        for block in page.blocks
    }
    table_ids = {
        table.table_id: canonical_uuid(table.table_id, scope=artifact_id)
        for page in document.pages
        for table in page.tables
    }
    cell_ids = {
        cell.cell_id: canonical_uuid(cell.cell_id, scope=artifact_id)
        for page in document.pages
        for table in page.tables
        for cell in table.cells
    }
    sections, section_ids = _section_plan(document, artifact_id)
    block_sections = {
        block.block_id: section_ids.get(block.section_path)
        for page in document.pages
        for block in page.blocks
    }
    table_sections = {
        table.table_id: section_ids.get(table.section_path)
        for page in document.pages
        for table in page.tables
    }
    provenance, evidence_provenance = _provenance_plan(
        document,
        projections,
        artifact_id=artifact_id,
        page_ids=page_ids,
        block_ids=block_ids,
        table_ids=table_ids,
        cell_ids=cell_ids,
    )
    entity_count = len(
        {
            normalize_text(str(fact.entity_value or ""))
            for fact in projections.facts
            if fact.value_kind == "entity"
        }
    )
    expected_counts = {
        "pages": len(page_ids),
        "sections": len(sections),
        "blocks": len(block_ids),
        "tables": len(table_ids),
        "table_segments": len(table_ids),
        "table_cells": len(cell_ids),
        "provenance_spans": len(provenance),
        "entities": entity_count,
        "facts": len(projections.facts),
        "fact_provenance": sum(
            len(_fact_provenance_ids(fact, _PlanProvenance(evidence_provenance)))
            for fact in projections.facts
        ),
    }
    return _CorePlan(
        artifact_id=artifact_id,
        artifact_canonical_id=artifact_canonical_id,
        page_ids=page_ids,
        block_ids=block_ids,
        table_ids=table_ids,
        cell_ids=cell_ids,
        section_ids=section_ids,
        block_sections=block_sections,
        table_sections=table_sections,
        sections=sections,
        provenance=provenance,
        evidence_provenance=evidence_provenance,
        expected_counts=expected_counts,
        projection_fingerprint=_artifact_projection_fingerprint(projections),
    )


@dataclass(frozen=True, slots=True)
class _PlanProvenance:
    evidence_provenance: Mapping[str, tuple[str, ...]]


def _validate_projection_payload(
    document: Document,
    projections: ProjectionBundle,
) -> None:
    _require_unique(
        (item.evidence_id for item in projections.evidence),
        "evidence ID",
    )
    _require_unique(
        (item.search_unit_id for item in projections.search_units),
        "search unit ID",
    )
    _require_unique(
        (item.fact_id for item in projections.facts),
        "fact ID",
    )
    _require_unique(
        (item.term_id for item in projections.exact_terms),
        "exact term ID",
    )
    _require_unique(
        (item.node_id for item in projections.navigation_nodes),
        "navigation node ID",
    )
    for page in document.pages:
        if page.rotation not in {0, 90, 180, 270}:
            raise PersistenceInvariantError(
                f"page {page.page_id} has an unsupported rotation"
            )
    for unit in projections.search_units:
        if not unit.search_text.strip() or not unit.evidence.content.strip():
            raise PersistenceInvariantError(
                f"search unit {unit.search_unit_id} has empty display/search text"
            )
        _search_unit_type(unit.unit_type)
        _vector_value(unit.embedding)
    units_by_id = {
        unit.search_unit_id: unit for unit in projections.search_units
    }
    description_by_node: dict[str, str] = {}
    linked_description_units: set[str] = set()
    for link in projections.navigation_descriptions:
        if link.node_id in description_by_node:
            raise PersistenceInvariantError(
                f"navigation node {link.node_id} has multiple descriptions"
            )
        if link.search_unit_id in linked_description_units:
            raise PersistenceInvariantError(
                "a navigation description search unit cannot describe "
                "multiple nodes"
            )
        description_by_node[link.node_id] = link.search_unit_id
        linked_description_units.add(link.search_unit_id)
    for term in projections.exact_terms:
        if (
            not term.term_type.strip()
            or not term.raw_term.strip()
            or not term.normalized_term.strip()
        ):
            raise PersistenceInvariantError(
                f"exact term {term.term_id} contains an empty required value"
            )
    for node in projections.navigation_nodes:
        if not node.label.strip():
            raise PersistenceInvariantError(
                f"navigation node {node.node_id} has an empty label"
            )
        description_id = description_by_node.get(node.node_id)
        if description_id is None:
            raise PersistenceInvariantError(
                f"navigation node {node.node_id} lacks a description search unit"
            )
        description_unit = units_by_id.get(description_id)
        if description_unit is None:
            raise PersistenceInvariantError(
                f"navigation node {node.node_id} references an unknown "
                "description search unit"
            )
        if (
            description_unit.unit_type != "navigation_summary"
            or description_unit.evidence.is_citable
            or not description_unit.evidence.generated_summary
        ):
            raise PersistenceInvariantError(
                f"navigation node {node.node_id} description must be a "
                "non-citable generated navigation summary"
            )
        if (
            description_unit.evidence.release_id != node.release_id
            or description_unit.evidence.source_document_id
            != next(
                (
                    item.source_document_id
                    for item in projections.evidence
                    if item.source_document_id
                ),
                "",
            )
        ):
            raise PersistenceInvariantError(
                f"navigation node {node.node_id} description crosses "
                "release or source scope"
            )
    unlinked_summaries = {
        unit.search_unit_id
        for unit in projections.search_units
        if unit.unit_type == "navigation_summary"
    }.difference(linked_description_units)
    if unlinked_summaries:
        raise PersistenceInvariantError(
            "navigation summary search units must be linked to navigation nodes"
        )
    entity_ids = {
        normalize_text(str(fact.entity_value or "")): "validated-entity"
        for fact in projections.facts
        if fact.value_kind == "entity"
        and normalize_text(str(fact.entity_value or ""))
    }
    for fact in projections.facts:
        _fact_typed_values(fact, entity_ids)


def _section_plan(
    document: Document,
    artifact_id: str,
) -> tuple[tuple[_SectionSpec, ...], dict[tuple[str, ...], str]]:
    mutable: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    for page in document.pages:
        items: list[Block | Table] = [*page.blocks, *page.tables]
        for item in sorted(items, key=lambda value: value.order):
            path = tuple(item.section_path)
            for length in range(1, len(path) + 1):
                prefix = path[:length]
                if prefix not in mutable:
                    canonical_id = stable_id(
                        "section", document.document_id, *prefix
                    )
                    mutable[prefix] = {
                        "canonical_id": canonical_id,
                        "database_id": canonical_uuid(
                            canonical_id, scope=artifact_id
                        ),
                        "start_page": page.index + 1,
                        "end_page": page.index + 1,
                    }
                    order.append(prefix)
                else:
                    mutable[prefix]["end_page"] = page.index + 1
    section_ids = {
        path: str(values["database_id"]) for path, values in mutable.items()
    }
    sections = tuple(
        _SectionSpec(
            canonical_id=str(mutable[path]["canonical_id"]),
            database_id=str(mutable[path]["database_id"]),
            parent_database_id=section_ids.get(path[:-1]),
            path=path,
            ordinal=ordinal,
            start_page=int(mutable[path]["start_page"]),
            end_page=int(mutable[path]["end_page"]),
        )
        for ordinal, path in enumerate(order)
    )
    return sections, section_ids


def _provenance_plan(
    document: Document,
    projections: ProjectionBundle,
    *,
    artifact_id: str,
    page_ids: Mapping[int, str],
    block_ids: Mapping[str, str],
    table_ids: Mapping[str, str],
    cell_ids: Mapping[str, str],
) -> tuple[tuple[_ProvenanceSpec, ...], dict[str, tuple[str, ...]]]:
    del table_ids  # Tables are represented by exact cell anchors.
    pages = {page.index: page for page in document.pages}
    blocks = {
        block.block_id: block
        for page in document.pages
        for block in page.blocks
    }
    tables = {
        table.table_id: table
        for page in document.pages
        for table in page.tables
    }
    cells = {
        cell.cell_id: cell
        for table in tables.values()
        for cell in table.cells
    }
    result: list[_ProvenanceSpec] = []
    evidence_map: dict[str, tuple[str, ...]] = {}
    for evidence in projections.evidence:
        if evidence.generated_summary:
            if evidence.is_citable:
                raise PersistenceInvariantError(
                    "generated navigation summaries cannot be citable"
                )
            evidence_map[evidence.evidence_id] = ()
            continue
        coordinate = evidence.coordinate
        page_index = coordinate.page_number - 1
        if page_index not in pages:
            raise PersistenceInvariantError(
                f"evidence {evidence.evidence_id} references an unknown page"
            )
        anchors: list[
            tuple[
                str,
                str | None,
                str | None,
                str | None,
                int | None,
                int | None,
                tuple[float, float, float, float] | None,
                str,
                dict[str, Any],
            ]
        ] = []
        if coordinate.block_id:
            block = blocks.get(coordinate.block_id)
            if block is None:
                raise PersistenceInvariantError(
                    f"evidence {evidence.evidence_id} references an unknown block"
                )
            quote = evidence.content
            start = block.text.find(quote)
            if start < 0:
                raise PersistenceInvariantError(
                    f"evidence {evidence.evidence_id} is not an exact block quote"
                )
            anchors.append(
                (
                    block.block_id,
                    None,
                    block_ids[block.block_id],
                    None,
                    start,
                    start + len(quote),
                    _database_bbox(block.bbox),
                    quote,
                    {"source_locator": block.source_locator},
                )
            )
        elif coordinate.cell_id:
            cell = cells.get(coordinate.cell_id)
            if cell is None:
                raise PersistenceInvariantError(
                    f"evidence {evidence.evidence_id} references an unknown cell"
                )
            anchors.append(
                (
                    cell.cell_id,
                    None,
                    None,
                    cell_ids[cell.cell_id],
                    None,
                    None,
                    _database_bbox(cell.bbox),
                    cell.text,
                    {"source_locator": cell.source_locator},
                )
            )
        elif coordinate.table_id and coordinate.row_index is not None:
            table = tables.get(coordinate.table_id)
            if table is None:
                raise PersistenceInvariantError(
                    f"evidence {evidence.evidence_id} references an unknown table"
                )
            row_cells = sorted(
                (
                    cell
                    for cell in table.cells
                    if cell.row == coordinate.row_index and cell.text.strip()
                ),
                key=lambda cell: cell.column,
            )
            if not row_cells:
                raise PersistenceInvariantError(
                    f"table-row evidence {evidence.evidence_id} has no source cells"
                )
            supporting_cells: list[Cell] = list(row_cells)
            covered_columns = {
                column
                for cell in row_cells
                for column in range(
                    cell.column, cell.column + cell.column_span
                )
            }
            supporting_cells.extend(
                cell
                for cell in sorted(
                    table.cells, key=lambda value: (value.row, value.column)
                )
                if cell.row < coordinate.row_index
                and cell.role in {"header", "row_header"}
                and any(
                    column in covered_columns
                    for column in range(
                        cell.column, cell.column + cell.column_span
                    )
                )
            )
            active_group = next(
                (cell.group for cell in row_cells if cell.group),
                None,
            )
            if active_group:
                group_headers = [
                    cell
                    for cell in table.cells
                    if cell.role == "group_header"
                    and cell.text == active_group
                    and cell.row < coordinate.row_index
                ]
                if group_headers:
                    supporting_cells.append(
                        max(group_headers, key=lambda cell: cell.row)
                    )
            deduplicated_cells = list(
                {
                    cell.cell_id: cell
                    for cell in supporting_cells
                }.values()
            )
            for cell in deduplicated_cells:
                anchors.append(
                    (
                        cell.cell_id,
                        None,
                        None,
                        cell_ids[cell.cell_id],
                        None,
                        None,
                        _database_bbox(cell.bbox),
                        cell.text,
                        {
                            "source_locator": cell.source_locator,
                            "canonical_table_id": table.table_id,
                            "row_index": coordinate.row_index,
                            "column_index": cell.column,
                        },
                    )
                )
        else:
            page = pages[page_index]
            anchors.append(
                (
                    page.page_id,
                    page_ids[page_index],
                    None,
                    None,
                    None,
                    None,
                    _database_bbox(
                        (
                            coordinate.bounding_box.x0,
                            coordinate.bounding_box.y0,
                            coordinate.bounding_box.x1,
                            coordinate.bounding_box.y1,
                        )
                        if coordinate.bounding_box
                        else None
                    ),
                    evidence.content,
                    {"source_locator": page.source_locator},
                )
            )

        provenance_ids: list[str] = []
        for ordinal, (
            canonical_anchor_id,
            page_id,
            block_id,
            cell_id,
            char_start,
            char_end,
            bbox,
            quote,
            metadata,
        ) in enumerate(anchors):
            quote_sha = sha256_bytes(quote.encode("utf-8"))
            canonical_id = stable_id(
                "provenance",
                evidence.evidence_id,
                canonical_anchor_id,
                ordinal,
                quote_sha,
            )
            database_id = canonical_uuid(canonical_id, scope=artifact_id)
            provenance_ids.append(database_id)
            result.append(
                _ProvenanceSpec(
                    database_id=database_id,
                    canonical_id=canonical_id,
                    evidence_id=evidence.evidence_id,
                    page_id=page_id,
                    block_id=block_id,
                    cell_id=cell_id,
                    char_start=char_start,
                    char_end=char_end,
                    bbox=bbox,
                    quoted_text=quote,
                    quote_sha256=quote_sha,
                    metadata=metadata,
                )
            )
        evidence_map[evidence.evidence_id] = tuple(provenance_ids)
    return tuple(result), evidence_map


def _artifact_projection_fingerprint(projections: ProjectionBundle) -> str:
    return sha256_json(
        {
            "evidence": [
                {
                    "id": item.evidence_id,
                    "source": item.source_document_id,
                    "version": item.source_version_id,
                    "content": item.content,
                    "coordinate": {
                        "page": item.coordinate.page_number,
                        "block": item.coordinate.block_id,
                        "table": item.coordinate.table_id,
                        "row": item.coordinate.row_index,
                        "cell": item.coordinate.cell_id,
                        "start": item.coordinate.character_start,
                        "end": item.coordinate.character_end,
                    },
                }
                for item in projections.evidence
                if not item.generated_summary
            ],
            "facts": [
                {
                    "id": fact.fact_id,
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "kind": fact.value_kind,
                    "raw": fact.raw_value,
                    "text": fact.text_value,
                    "numeric": fact.numeric_value,
                    "boolean": fact.boolean_value,
                    "date": fact.date_value,
                    "entity": fact.entity_value,
                    "unit": fact.normalized_unit,
                    "provenance": list(fact.provenance_evidence_ids),
                }
                for fact in projections.facts
            ],
        }
    )


def _fact_provenance_ids(fact: Any, plan: Any) -> tuple[str, ...]:
    result: list[str] = []
    for evidence_id in fact.provenance_evidence_ids:
        values = plan.evidence_provenance.get(evidence_id)
        if not values:
            raise PersistenceInvariantError(
                f"fact {fact.fact_id} references unknown evidence {evidence_id}"
            )
        for provenance_id in values:
            if provenance_id not in result:
                result.append(provenance_id)
    provenance = getattr(plan, "provenance", ())
    if provenance:
        quote_by_id = {
            item.database_id: item.quoted_text.strip()
            for item in provenance
        }
        exact_value = fact.raw_value.strip()
        original_order = {
            provenance_id: index
            for index, provenance_id in enumerate(result)
        }
        result.sort(
            key=lambda provenance_id: (
                quote_by_id.get(provenance_id) != exact_value,
                original_order[provenance_id],
            )
        )
    return tuple(result)


def _fact_typed_values(
    fact: Any,
    entity_ids: Mapping[str, str],
) -> tuple[Any, Any, Any, Any, Any]:
    value_text = None
    numeric_value = None
    date_value = None
    boolean_value = None
    object_entity_id = None
    if fact.value_kind == "text":
        value_text = fact.text_value
    elif fact.value_kind == "numeric":
        try:
            numeric_value = Decimal(str(fact.numeric_value))
        except (InvalidOperation, ValueError) as error:
            raise PersistenceInvariantError(
                f"fact {fact.fact_id} has an invalid numeric value"
            ) from error
        if not numeric_value.is_finite():
            raise PersistenceInvariantError(
                f"fact {fact.fact_id} has a non-finite numeric value"
            )
    elif fact.value_kind == "date":
        date_value = fact.date_value
        try:
            date.fromisoformat(str(date_value))
        except ValueError as error:
            raise PersistenceInvariantError(
                f"fact {fact.fact_id} has an invalid ISO date"
            ) from error
    elif fact.value_kind == "boolean":
        boolean_value = fact.boolean_value
    elif fact.value_kind == "entity":
        object_entity_id = entity_ids.get(
            normalize_text(str(fact.entity_value or ""))
        )
    else:
        raise PersistenceInvariantError(
            f"fact {fact.fact_id} has unsupported kind {fact.value_kind!r}"
        )
    if sum(
        value is not None
        for value in (
            value_text,
            numeric_value,
            date_value,
            boolean_value,
            object_entity_id,
        )
    ) != 1:
        raise PersistenceInvariantError(
            f"fact {fact.fact_id} does not have exactly one typed value"
        )
    return (
        value_text,
        numeric_value,
        date_value,
        boolean_value,
        object_entity_id,
    )


def _projection_section_id(evidence: Any, plan: _CorePlan) -> str | None:
    coordinate = evidence.coordinate
    if coordinate.block_id:
        return plan.block_sections.get(coordinate.block_id)
    if coordinate.table_id:
        return plan.table_sections.get(coordinate.table_id)
    return None


def _database_bbox(
    bbox: Sequence[float] | None,
) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    values = tuple(float(value) for value in bbox)
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise PersistenceInvariantError("bounding box must contain four finite values")
    x0, y0, x1, y1 = values
    if x0 < 0 or y0 < 0:
        raise PersistenceInvariantError("bounding box origins cannot be negative")
    if x1 <= x0 or y1 <= y0:
        return None
    return values  # type: ignore[return-value]


def _bbox_values(
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    return bbox if bbox is not None else (None, None, None, None)


def _block_type(kind: str) -> str:
    supported = {
        "heading",
        "paragraph",
        "list_item",
        "caption",
        "table_anchor",
        "figure",
        "header",
        "footer",
        "other",
    }
    return kind if kind in supported else "other"


def _cell_role(role: str) -> str:
    if role == "group_header":
        return "header"
    return role if role in {"header", "row_header", "body", "stub", "note"} else "body"


def _header_row_count(table: Table) -> int:
    count = 0
    for row_index in range(table.row_count):
        cells = [cell for cell in table.cells if cell.row == row_index]
        if cells and all(
            cell.role in {"header", "group_header", "row_header"}
            for cell in cells
        ):
            count += 1
        else:
            break
    return count


def _search_unit_type(value: str) -> str:
    mapping = {
        "block": "paragraph",
        "table_row": "table_row",
        "table_cell": "table_cell",
        "fact": "fact",
        "document": "document_summary",
        "navigation_summary": "navigation",
    }
    try:
        return mapping[value]
    except KeyError as error:
        raise PersistenceInvariantError(
            f"unsupported search unit type: {value}"
        ) from error


def _verification_status(value: str) -> str:
    normalized = value.casefold()
    return (
        normalized
        if normalized in {"candidate", "verified", "rejected", "superseded"}
        else "candidate"
    )


def _qa_severity(value: str) -> str:
    return {
        "warning": "warning",
        "quarantine": "error",
        "fatal": "blocker",
    }.get(value, "info")


def _vector_value(values: Sequence[float]) -> str | None:
    if not values:
        return None
    if len(values) != 1024:
        raise PersistenceInvariantError(
            "PostgreSQL embeddings must contain exactly 1024 dimensions"
        )
    normalized: list[str] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise PersistenceInvariantError(
                "PostgreSQL embeddings must contain finite values"
            )
        normalized.append(format(number, ".17g"))
    return f"[{','.join(normalized)}]"


def _suffix(source_name: str) -> str:
    if "." not in source_name:
        return ""
    suffix = "." + source_name.rsplit(".", 1)[-1].casefold()
    return (
        suffix
        if re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,31}", suffix)
        else ""
    )


def _row_field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _require_unique(values: Iterator[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise PersistenceInvariantError(f"{label} cannot be empty")
        if value in seen:
            raise PersistenceInvariantError(f"duplicate {label}: {value}")
        seen.add(value)


def _required_uuid(value: Any, field_name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error
    return str(parsed)


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    normalized = _json_value(dict(value))
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return normalized


def _json_value(value: Any) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be JSON serializable") from error


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _load_psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as error:
        raise PostgresCompilerDependencyError(
            "PostgreSQL compiler persistence requires psycopg 3"
        ) from error
    return psycopg


def _default_jsonb(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ModuleNotFoundError as error:
        raise PostgresCompilerDependencyError(
            "PostgreSQL compiler persistence requires psycopg 3"
        ) from error
    return Jsonb(value)
