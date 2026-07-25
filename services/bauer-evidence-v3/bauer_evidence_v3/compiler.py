from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .ids import sha256_json, stable_id
from .ingest import CompilationResult, Compiler
from .models import ArtifactStatus
from .object_store import ContentAddressedObjectStore, StoredObject
from .projections import ProjectionBundle, project_document
from .releases import (
    InMemoryReleaseRegistry,
    ReleaseGateReport,
    ReleaseRecord,
    ReleaseSource,
)
from .retrieval import InMemoryEvidenceIndex


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    tenant_id: str
    knowledge_base_id: str
    external_file_id: str
    source_name: str
    declared_media_type: str | None
    source_type: str
    category_path: tuple[str, ...] = ()
    source_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompiledArtifact:
    source_document_id: str
    source_version_id: str
    artifact_set_id: str
    source_object: StoredObject
    canonical_object: StoredObject
    compilation: CompilationResult
    reused: bool


@dataclass(frozen=True, slots=True)
class CompiledReleaseSource:
    artifact: CompiledArtifact
    projections: ProjectionBundle


class InMemoryKnowledgeCompiler:
    """Reference compiler that exercises the complete release contract locally."""

    def __init__(
        self,
        *,
        releases: InMemoryReleaseRegistry,
        index: InMemoryEvidenceIndex,
        object_store: ContentAddressedObjectStore,
        compiler: Compiler | None = None,
        compiler_versions: dict[str, str] | None = None,
    ) -> None:
        self.releases = releases
        self.index = index
        self.object_store = object_store
        self.compiler = compiler or Compiler()
        self.compiler_versions = compiler_versions or {
            "compiler": "bauer-evidence-v3-core-1",
            "parser": "source-native-core-1",
            "embedding": "hashing-local-only",
            "prompt": "bauer-v3-answer-1",
        }
        self.compiler_fingerprint = sha256_json(self.compiler_versions)
        self._artifacts: dict[tuple[str, str], CompiledArtifact] = {}
        self._projection_counts: dict[str, int] = {}

    def create_release(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        label: str,
        expected_source_count: int,
        base_release_id: str | None = None,
        release_id: str | None = None,
    ) -> ReleaseRecord:
        release = self.releases.create(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            label=label,
            expected_source_count=expected_source_count,
            compiler_fingerprint=self.compiler_fingerprint,
            versions=self.compiler_versions,
            base_release_id=base_release_id,
            release_id=release_id,
        )
        return self.releases.start_build(release.release_id)

    def compile_source(
        self,
        *,
        release_id: str,
        descriptor: SourceDescriptor,
        content: bytes,
    ) -> CompiledReleaseSource:
        release = self.releases.get(release_id)
        if (
            release.tenant_id != descriptor.tenant_id
            or release.knowledge_base_id != descriptor.knowledge_base_id
        ):
            raise ValueError("source descriptor does not match its target release")
        source_document_id = stable_id(
            "source_document",
            descriptor.tenant_id,
            descriptor.knowledge_base_id,
            descriptor.external_file_id,
        )
        source_object = self.object_store.put(
            content,
            media_type=descriptor.declared_media_type or "application/octet-stream",
            suffix=_suffix(descriptor.source_name),
        )
        source_version_id = stable_id(
            "source_version",
            source_document_id,
            source_object.sha256,
        )
        artifact_key = (source_version_id, self.compiler_fingerprint)
        cached = self._artifacts.get(artifact_key)
        if cached is None:
            compilation = self.compiler.compile(
                content,
                source_name=descriptor.source_name,
                declared_media_type=descriptor.declared_media_type,
                enforce_gate=True,
            )
            canonical_content = compilation.document.to_json().encode("utf-8")
            canonical_object = self.object_store.put(
                canonical_content,
                media_type="application/json",
                suffix=".canonical.json",
            )
            artifact = CompiledArtifact(
                source_document_id=source_document_id,
                source_version_id=source_version_id,
                artifact_set_id=stable_id(
                    "artifact_set",
                    source_version_id,
                    self.compiler_fingerprint,
                ),
                source_object=source_object,
                canonical_object=canonical_object,
                compilation=compilation,
                reused=False,
            )
            self._artifacts[artifact_key] = artifact
        else:
            artifact = CompiledArtifact(
                source_document_id=cached.source_document_id,
                source_version_id=cached.source_version_id,
                artifact_set_id=cached.artifact_set_id,
                source_object=cached.source_object,
                canonical_object=cached.canonical_object,
                compilation=cached.compilation,
                reused=True,
            )
        projections = project_document(
            artifact.compilation.document,
            release_id=release_id,
            tenant_id=descriptor.tenant_id,
            knowledge_base_id=descriptor.knowledge_base_id,
            source_document_id=source_document_id,
            source_version_id=source_version_id,
            source_type=descriptor.source_type,
            external_file_id=descriptor.external_file_id,
            category_path=descriptor.category_path,
            source_metadata=descriptor.source_metadata,
        )
        for unit in projections.search_units:
            self.index.add(unit)
        for node in projections.navigation_nodes:
            self.index.add_navigation(node)
        self._projection_counts[release_id] = (
            self._projection_counts.get(release_id, 0) + len(projections.search_units)
        )
        self.releases.record_source(
            release_id,
            ReleaseSource(
                source_id=source_document_id,
                source_version_id=source_version_id,
                artifact_set_id=artifact.artifact_set_id,
                artifact_status=ArtifactStatus.VALID,
                action="reused" if artifact.reused else "compiled",
            ),
        )
        return CompiledReleaseSource(artifact=artifact, projections=projections)

    def finalize_release(
        self,
        release_id: str,
        *,
        gold_status: str = "interim_codex_reviewed_requires_bauer_signoff",
        metrics: dict[str, float] | None = None,
        blocking_qa_codes: tuple[str, ...] = (),
        hard_eval_failures: tuple[str, ...] = (),
    ) -> ReleaseRecord:
        self.releases.begin_validation(release_id)
        report = ReleaseGateReport(
            manifest_sha256=self.releases.computed_manifest_sha256(release_id),
            source_complete=True,
            citations_resolvable=self._projection_counts.get(release_id, 0) > 0,
            projections_complete=self._projection_counts.get(release_id, 0) > 0,
            blocking_qa_codes=blocking_qa_codes,
            hard_eval_failures=hard_eval_failures,
            gold_status=gold_status,
            metrics=metrics or {},
        )
        return self.releases.mark_ready(release_id, report)

    def compile_job(
        self,
        payload: dict[str, Any],
        *,
        source_loader,
    ) -> dict[str, Any]:
        """Job-handler contract: large bytes stay in object storage, never in queue payloads."""

        required = {
            "release_id",
            "tenant_id",
            "knowledge_base_id",
            "external_file_id",
            "source_name",
            "source_object_key",
            "source_type",
        }
        missing = required.difference(payload)
        if missing:
            raise ValueError(f"compile job is missing: {', '.join(sorted(missing))}")
        content = source_loader(payload["source_object_key"])
        result = self.compile_source(
            release_id=str(payload["release_id"]),
            descriptor=SourceDescriptor(
                tenant_id=str(payload["tenant_id"]),
                knowledge_base_id=str(payload["knowledge_base_id"]),
                external_file_id=str(payload["external_file_id"]),
                source_name=str(payload["source_name"]),
                declared_media_type=(
                    str(payload["declared_media_type"])
                    if payload.get("declared_media_type")
                    else None
                ),
                source_type=str(payload["source_type"]),
                category_path=tuple(
                    str(value) for value in payload.get("category_path", ())
                ),
                source_metadata=(
                    {
                        str(key): value
                        for key, value in payload["source_metadata"].items()
                    }
                    if isinstance(payload.get("source_metadata"), Mapping)
                    else {}
                ),
            ),
            content=content,
        )
        return {
            "source_version_id": result.artifact.source_version_id,
            "artifact_set_id": result.artifact.artifact_set_id,
            "canonical_object_key": result.artifact.canonical_object.object_key,
            "reused": result.artifact.reused,
            "quality_status": result.artifact.compilation.quality.status,
            "search_unit_count": len(result.projections.search_units),
            "fact_count": len(result.projections.facts),
        }


def _suffix(source_name: str) -> str:
    name = source_name.casefold()
    if "." not in name:
        return ""
    extension = "." + name.rsplit(".", 1)[-1]
    if not 2 <= len(extension) <= 16 or not extension[1:].replace("-", "").isalnum():
        return ""
    return extension
