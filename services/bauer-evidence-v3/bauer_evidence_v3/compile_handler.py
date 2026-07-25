from __future__ import annotations

import hmac
from dataclasses import replace
from typing import Any, Mapping, Protocol

from .ids import sha256_bytes
from .ingest import Compiler
from .object_store import ContentAddressedObjectStore
from .postgres_compiler import (
    CompilerPersistenceContext,
    PostgresCompilerPersistence,
)
from .projections import ProjectionBundle, project_document
from .toolchain import CompilerToolchainIdentity, derive_compiler_toolchain


_COMPILED_SOURCE_FORMATS = {
    "pdf": frozenset({"pdf"}),
    "html": frozenset({"html"}),
    "text": frozenset({"text", "markdown"}),
}


class BatchEmbeddingProvider(Protocol):
    def embed(self, text: str) -> tuple[float, ...]: ...

    def embed_many(
        self,
        texts: tuple[str, ...] | list[str],
    ) -> tuple[tuple[float, ...], ...]: ...


class CompilationJobHandler:
    """Compile one immutable manifest entry and persist its V3 evidence graph."""

    def __init__(
        self,
        *,
        object_store: ContentAddressedObjectStore,
        persistence: PostgresCompilerPersistence,
        embedding_provider: BatchEmbeddingProvider,
        embedding_model_version: str,
        tenant_id: str,
        knowledge_base_id: str,
        compiler: Compiler | None = None,
        embedding_batch_size: int = 32,
        embedding_dimensions: int | None = None,
        ocr_version: str | None = None,
        fact_model_version: str | None = None,
    ) -> None:
        if not embedding_model_version.strip():
            raise ValueError("embedding_model_version is required")
        if not tenant_id.strip() or not knowledge_base_id.strip():
            raise ValueError("tenant_id and knowledge_base_id are required")
        if not 1 <= embedding_batch_size <= 128:
            raise ValueError("embedding_batch_size must be between 1 and 128")
        self.object_store = object_store
        self.persistence = persistence
        self.embedding_provider = embedding_provider
        self.embedding_model_version = embedding_model_version.strip()
        self.tenant_id = tenant_id.strip()
        self.knowledge_base_id = knowledge_base_id.strip()
        self.compiler = compiler or Compiler()
        self.embedding_batch_size = embedding_batch_size
        provider_dimensions = getattr(embedding_provider, "dimensions", None)
        if embedding_dimensions is None:
            embedding_dimensions = provider_dimensions
        if (
            isinstance(embedding_dimensions, bool)
            or not isinstance(embedding_dimensions, int)
            or embedding_dimensions < 1
        ):
            raise ValueError(
                "embedding_dimensions is required when the provider does not "
                "declare positive dimensions"
            )
        if (
            provider_dimensions is not None
            and provider_dimensions != embedding_dimensions
        ):
            raise ValueError(
                "embedding_dimensions does not match the embedding provider"
            )
        self.embedding_dimensions = embedding_dimensions
        configured_ocr_version = _optional_text(ocr_version)
        actual_ocr_version = _optional_text(
            getattr(self.compiler.ocr_engine, "engine_version", None)
        )
        if configured_ocr_version != actual_ocr_version:
            raise ValueError(
                "ocr_version must match the configured compiler OCR engine"
            )
        self.ocr_version = actual_ocr_version
        self.fact_model_version = _optional_text(fact_model_version)
        self.toolchain: CompilerToolchainIdentity = derive_compiler_toolchain(
            compiler=self.compiler,
            embedding_model_version=self.embedding_model_version,
            embedding_dimensions=self.embedding_dimensions,
            ocr_version=self.ocr_version,
            fact_model_version=self.fact_model_version,
        )
        self.compiler_fingerprint = self.toolchain.fingerprint

    def __call__(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        required = {
            "release_id",
            "tenant_id",
            "knowledge_base_id",
            "source_id",
            "source_version_id",
            "external_file_id",
            "source_name",
            "source_object_key",
            "source_type",
            "source_sha256",
            "source_byte_size",
            "manifest_sha256",
            "compiler_fingerprint",
            "ordinal",
        }
        missing = sorted(required.difference(payload))
        if missing:
            raise ValueError(f"compile job is missing: {', '.join(missing)}")
        if (
            str(payload["tenant_id"]) != self.tenant_id
            or str(payload["knowledge_base_id"]) != self.knowledge_base_id
        ):
            raise PermissionError("compile job is outside this worker deployment scope")
        source_type = str(payload["source_type"]).strip().casefold()
        if source_type not in _COMPILED_SOURCE_FORMATS:
            raise ValueError(
                "compile job source_type must be pdf, html, or text"
            )
        claimed_fingerprint = str(payload["compiler_fingerprint"]).strip()
        if not hmac.compare_digest(
            claimed_fingerprint,
            self.compiler_fingerprint,
        ):
            raise ValueError(
                "compile job compiler_fingerprint does not match this worker "
                "toolchain"
            )
        if _optional_text(payload.get("ocr_version")) != self.ocr_version:
            raise ValueError("compile job OCR version does not match this worker")
        if (
            _optional_text(payload.get("fact_model_version"))
            != self.fact_model_version
        ):
            raise ValueError(
                "compile job fact model version does not match this worker"
            )

        source_bytes = self.object_store.get(str(payload["source_object_key"]))
        expected_size = _non_negative_int(payload["source_byte_size"], "source_byte_size")
        if len(source_bytes) != expected_size:
            raise ValueError("source object byte size does not match its manifest")
        expected_sha256 = str(payload["source_sha256"]).strip().casefold()
        if sha256_bytes(source_bytes) != expected_sha256:
            raise ValueError("source object SHA-256 does not match its manifest")

        compilation = self.compiler.compile(
            source_bytes,
            source_name=str(payload["source_name"]),
            declared_media_type=_optional_text(payload.get("declared_media_type")),
            enforce_gate=True,
        )
        if compilation.probe.format not in _COMPILED_SOURCE_FORMATS[source_type]:
            raise ValueError(
                "compiled source format does not match the manifest source_type"
            )
        projections = project_document(
            compilation.document,
            release_id=str(payload["release_id"]),
            tenant_id=str(payload["tenant_id"]),
            knowledge_base_id=str(payload["knowledge_base_id"]),
            source_document_id=str(payload["source_id"]),
            source_version_id=str(payload["source_version_id"]),
            source_type=source_type,
            external_file_id=str(payload["external_file_id"]),
            category_path=tuple(
                str(value) for value in payload.get("category_path", ())
            ),
            source_metadata=_mapping(payload.get("source_metadata")),
        )
        projections = embed_projection_bundle(
            projections,
            provider=self.embedding_provider,
            batch_size=self.embedding_batch_size,
        )
        persisted = self.persistence.persist(
            source_bytes=source_bytes,
            compilation=compilation,
            projections=projections,
            context=CompilerPersistenceContext(
                tenant_id=str(payload["tenant_id"]),
                knowledge_base_id=str(payload["knowledge_base_id"]),
                release_id=str(payload["release_id"]),
                manifest_sha256=str(payload["manifest_sha256"]),
                external_file_id=str(payload["external_file_id"]),
                compiler_fingerprint=self.compiler_fingerprint,
                embedding_model_version=self.embedding_model_version,
                source_type=source_type,
                ordinal=_non_negative_int(payload["ordinal"], "ordinal"),
                source_document_id=str(payload["source_id"]),
                source_version_id=str(payload["source_version_id"]),
                canonical_uri=_optional_text(payload.get("canonical_uri")),
                source_revision=_optional_text(payload.get("source_revision")),
                publication_date=_optional_text(payload.get("publication_date")),
                visibility=str(payload.get("visibility") or "inherited"),
                worker_job_id=_optional_text(payload.get("worker_job_id")),
                ocr_version=self.ocr_version,
                fact_model_version=self.fact_model_version,
                source_metadata=_mapping(payload.get("source_metadata")),
            ),
        )
        return {
            "source_id": persisted.source_id,
            "source_version_id": persisted.source_version_id,
            "artifact_set_id": persisted.artifact_set_id,
            "source_object_sha256": persisted.source_object.sha256,
            "canonical_object_sha256": persisted.canonical_object.sha256,
            "reused": persisted.reused,
            "quality_status": compilation.quality.status,
            "artifact_counts": dict(persisted.artifact_counts),
            "projection_counts": dict(persisted.projection_counts),
        }


def embed_projection_bundle(
    bundle: ProjectionBundle,
    *,
    provider: BatchEmbeddingProvider,
    batch_size: int,
) -> ProjectionBundle:
    if not 1 <= batch_size <= 128:
        raise ValueError("batch_size must be between 1 and 128")
    units = list(bundle.search_units)
    embedded = []
    for start in range(0, len(units), batch_size):
        batch = units[start : start + batch_size]
        texts = tuple(unit.search_text for unit in batch)
        if hasattr(provider, "embed_many"):
            vectors = provider.embed_many(texts)
        else:  # pragma: no cover - compatibility with simple injected providers
            vectors = tuple(provider.embed(text) for text in texts)
        if len(vectors) != len(batch):
            raise ValueError("embedding provider returned the wrong batch size")
        embedded.extend(
            replace(unit, embedding=tuple(vector))
            for unit, vector in zip(batch, vectors)
        )
    return replace(bundle, search_units=tuple(embedded))


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("source_metadata must be an object")
    return {str(key): item for key, item in value.items()}
