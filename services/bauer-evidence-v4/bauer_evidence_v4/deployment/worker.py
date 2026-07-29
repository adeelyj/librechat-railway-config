from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from bauer_evidence_v3.object_store import S3ObjectStore

from ..canonical.models import canonical_data
from ..compilation import CanonicalCompiler
from ..indexing import ProjectionBuilder
from ..retrieval.candidate_generation import HashingDenseEncoder
from .config import V4Settings


LOGGER = logging.getLogger(__name__)


def _fingerprint(error: BaseException) -> str:
    return hashlib.sha256(
        f"{type(error).__name__}:{error}".encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()


def _s3_client(settings: V4Settings):
    import boto3

    options: dict[str, str] = {}
    if settings.source_s3_endpoint_url:
        options["endpoint_url"] = settings.source_s3_endpoint_url
    if settings.source_s3_region:
        options["region_name"] = settings.source_s3_region
    if settings.source_s3_access_key_id:
        options["aws_access_key_id"] = settings.source_s3_access_key_id
        options["aws_secret_access_key"] = (
            settings.source_s3_secret_access_key
        )
    return boto3.client("s3", **options)


@dataclass(slots=True)
class V4CompilerWorker:
    settings: V4Settings
    source_store: Any = field(init=False, repr=False)
    artifact_store: Any = field(init=False, repr=False)
    compiler: CanonicalCompiler = field(init=False, repr=False)
    projections: ProjectionBuilder = field(init=False, repr=False)
    encoder: HashingDenseEncoder = field(init=False, repr=False)
    stop_event: threading.Event = field(init=False, repr=False)

    def __post_init__(self) -> None:
        client = _s3_client(self.settings)
        self.source_store = S3ObjectStore(
            bucket=self.settings.source_s3_bucket,
            client=client,
            prefix=self.settings.source_s3_prefix,
        )
        self.artifact_store = S3ObjectStore(
            bucket=self.settings.source_s3_bucket,
            client=client,
            prefix=self.settings.artifact_s3_prefix,
        )
        self.compiler = CanonicalCompiler()
        self.projections = ProjectionBuilder()
        self.encoder = HashingDenseEncoder()
        self.stop_event = threading.Event()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            claimed = self.run_once()
            if not claimed:
                self.stop_event.wait(self.settings.worker_poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()

    def run_once(self) -> bool:
        import psycopg

        with psycopg.connect(
            self.settings.database_url,
            autocommit=True,
            application_name="bauer-evidence-v4-worker",
        ) as connection:
            self._scope(connection)
            row = connection.execute(
                """
                SELECT job_id, source_id
                FROM bauer_rag_v4.claim_compilation_job(%s, 300)
                """,
                (self.settings.worker_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "SELECT bauer_rag_v4.requeue_expired_jobs()"
                )
                return False
            job_id, source_id = (str(row[0]), str(row[1]))
            try:
                self._compile(connection, job_id, source_id)
            except Exception as error:
                LOGGER.exception(
                    "V4 compilation failed",
                    extra={
                        "job_id": job_id,
                        "source_id": source_id,
                        "error_type": type(error).__name__,
                    },
                )
                connection.execute(
                    """
                    SELECT bauer_rag_v4.fail_compilation_job(
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        job_id,
                        self.settings.worker_id,
                        "compiler_error",
                        _fingerprint(error),
                    ),
                )
            return True

    def _scope(self, connection: Any) -> None:
        connection.execute("SET ROLE bauer_rag_v4_worker")
        connection.execute(
            "SELECT set_config('bauer_rag_v4.tenant_id', %s, false)",
            (self.settings.tenant_id,),
        )
        connection.execute(
            """
            SELECT set_config(
                'bauer_rag_v4.knowledge_base_id', %s, false
            )
            """,
            (self.settings.knowledge_base_id,),
        )
        connection.execute(
            "SELECT set_config('bauer_rag_v4.release_id', %s, false)",
            (self.settings.candidate_release_id,),
        )

    def _compile(
        self,
        connection: Any,
        job_id: str,
        source_id: str,
    ) -> None:
        source = connection.execute(
            """
            SELECT version.source_version_id,
                   version.content_sha256,
                   version.media_type,
                   version.object_key,
                   document.external_source_id,
                   document.original_filename
            FROM bauer_rag_v4.source_versions AS version
            JOIN bauer_rag_v4.source_documents AS document
              ON document.source_id = version.source_id
            JOIN bauer_rag_v4.release_sources AS member
              ON member.source_id = version.source_id
             AND member.source_version_id = version.source_version_id
            WHERE member.release_id = %s
              AND version.source_id = %s
            """,
            (self.settings.candidate_release_id, source_id),
        ).fetchone()
        if source is None:
            raise RuntimeError("release source is unavailable")
        (
            source_version_id,
            content_sha256,
            media_type,
            object_key,
            _external_source_id,
            original_filename,
        ) = source
        payload = self.source_store.get(str(object_key))
        if hashlib.sha256(payload).hexdigest() != str(content_sha256):
            raise RuntimeError("source object does not match registry identity")
        result = self.compiler.compile(
            payload,
            source_path=str(original_filename),
            declared_media_type=str(media_type),
            enforce_gate=False,
        )
        if result.document is None or result.status != "published":
            codes = sorted(
                {
                    issue.code
                    for candidate in result.candidates
                    for issue in candidate.quality.issues
                }
            )
            raise RuntimeError(
                "canonical compiler quarantined source: "
                + ",".join(codes[:12])
            )
        document = result.document
        projections = self.projections.build(document)
        selected = next(
            candidate
            for candidate in result.candidates
            if candidate.parser_id == result.selected_parser_id
        )
        artifact = {
            "schema_version": 1,
            "release_id": self.settings.candidate_release_id,
            "source_id": source_id,
            "source_version_id": str(source_version_id),
            "document": canonical_data(document),
            "projections": [
                canonical_data(projection) for projection in projections
            ],
            "quality": {
                "status": selected.quality.status,
                "semantic_score": selected.quality.semantic_score,
                "metrics": dict(selected.quality.metrics),
                "issues": [
                    canonical_data(issue)
                    for issue in selected.quality.issues
                ],
            },
        }
        encoded = json.dumps(
            artifact,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        stored = self.artifact_store.put(
            encoded,
            media_type="application/json",
            suffix=".json",
        )
        with connection.transaction():
            self._persist_canonical(
                connection,
                source_id=source_id,
                source_version_id=str(source_version_id),
                document=document,
                projections=projections,
            )
            connection.execute(
                """
                INSERT INTO bauer_rag_v4.compiled_artifacts (
                    tenant_id, knowledge_base_id, release_id,
                    source_id, source_version_id, artifact_object_key,
                    artifact_sha256, canonical_json, projections_json,
                    parser_identity, quality_status, quality_metrics
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s::jsonb
                )
                """,
                (
                    self.settings.tenant_id,
                    self.settings.knowledge_base_id,
                    self.settings.candidate_release_id,
                    source_id,
                    str(source_version_id),
                    stored.object_key,
                    stored.sha256,
                    json.dumps(canonical_data(document)),
                    json.dumps(
                        [canonical_data(item) for item in projections]
                    ),
                    f"{document.parser_id}:{document.parser_version}",
                    selected.quality.status,
                    json.dumps(dict(selected.quality.metrics)),
                ),
            )
            connection.execute(
                """
                SELECT bauer_rag_v4.succeed_compilation_job(%s, %s)
                """,
                (job_id, self.settings.worker_id),
            )

    def _persist_canonical(
        self,
        connection: Any,
        *,
        source_id: str,
        source_version_id: str,
        document: Any,
        projections: Any,
    ) -> None:
        scope = (
            self.settings.tenant_id,
            self.settings.knowledge_base_id,
            self.settings.candidate_release_id,
            source_id,
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.canonical_documents (
                canonical_document_id, tenant_id, knowledge_base_id,
                release_id, source_id, source_version_id,
                canonical_sha256, canonical_schema_version,
                parser_identity, block_count, table_count,
                cell_count, fact_count
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                document.document_id,
                *scope,
                source_version_id,
                hashlib.sha256(document.to_json().encode("utf-8")).hexdigest(),
                f"v4.{document.schema_version}",
                f"{document.parser_id}:{document.parser_version}",
                len(document.blocks),
                len(document.tables),
                sum(len(table.cells) for table in document.tables),
                len(document.facts),
            ),
        )
        for block in document.blocks:
            connection.execute(
                """
                INSERT INTO bauer_rag_v4.canonical_blocks (
                    block_id, canonical_document_id, tenant_id,
                    knowledge_base_id, release_id, source_id,
                    block_kind, physical_page, printed_page,
                    section_path, source_locator, text_sha256,
                    text_content
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    block.block_id,
                    document.document_id,
                    *scope,
                    block.kind,
                    block.provenance.physical_page,
                    block.provenance.printed_page,
                    list(block.section_path),
                    block.provenance.locator,
                    hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
                    block.text,
                ),
            )
        for table in document.tables:
            connection.execute(
                """
                INSERT INTO bauer_rag_v4.canonical_tables (
                    table_id, canonical_document_id, tenant_id,
                    knowledge_base_id, release_id, source_id,
                    caption, section_path, physical_page,
                    printed_page, source_locator, row_count,
                    column_count
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    table.table_id,
                    document.document_id,
                    *scope,
                    table.caption,
                    list(table.section_path),
                    table.provenance.physical_page,
                    table.provenance.printed_page,
                    table.provenance.locator,
                    table.row_count,
                    table.column_count,
                ),
            )
            for cell in table.cells:
                qualifiers = {
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "qualifier_markers": list(cell.qualifier_markers),
                    "group": cell.group,
                    "value_kind": cell.value_kind,
                }
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v4.canonical_cells (
                        cell_id, table_id, canonical_document_id,
                        tenant_id, knowledge_base_id, release_id,
                        source_id, row_index, column_index, role,
                        header_path, raw_text, normalized_value,
                        raw_unit, normalized_unit, qualifiers,
                        source_locator
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                    )
                    """,
                    (
                        cell.cell_id,
                        table.table_id,
                        document.document_id,
                        *scope,
                        cell.row,
                        cell.column,
                        cell.role,
                        list(cell.header_path),
                        cell.text,
                        (
                            str(cell.numeric_value)
                            if cell.numeric_value is not None
                            else None
                        ),
                        cell.unit_raw,
                        cell.unit_ucum,
                        json.dumps(qualifiers),
                        cell.provenance.locator,
                    ),
                )
        for fact in document.facts:
            connection.execute(
                """
                INSERT INTO bauer_rag_v4.canonical_facts (
                    fact_id, canonical_document_id, tenant_id,
                    knowledge_base_id, release_id, source_id,
                    subject, predicate, raw_value, numeric_value,
                    minimum_value, maximum_value, raw_unit,
                    normalized_unit, qualifiers, provenance_ids,
                    confidence, review_status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s
                )
                """,
                (
                    fact.fact_id,
                    document.document_id,
                    *scope,
                    fact.subject,
                    fact.predicate,
                    fact.raw_value,
                    fact.numeric_value,
                    fact.minimum_value,
                    fact.maximum_value,
                    fact.unit_raw,
                    fact.unit_ucum,
                    json.dumps(dict(fact.qualifiers)),
                    list(fact.provenance_ids),
                    fact.confidence,
                    fact.review_status,
                ),
            )
        for projection in projections:
            vector = self.encoder.encode(projection.search_text)
            vector_text = "[" + ",".join(f"{item:.9g}" for item in vector) + "]"
            embedding_identity = hashlib.sha256(
                (
                    "local|hashing-dense-v1|1|256|l2|"
                    + projection.search_text_sha256
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO bauer_rag_v4.embedding_cache (
                    tenant_id, knowledge_base_id, provider, model,
                    revision, dimensions, normalization,
                    search_text_sha256, embedding_identity_sha256,
                    embedding
                )
                VALUES (
                    %s, %s, 'local', 'hashing-dense', '1', 256,
                    'l2', %s, %s, %s::vector
                )
                ON CONFLICT (
                    tenant_id, knowledge_base_id, provider, model,
                    revision, dimensions, normalization,
                    search_text_sha256
                ) DO NOTHING
                """,
                (
                    self.settings.tenant_id,
                    self.settings.knowledge_base_id,
                    projection.search_text_sha256,
                    embedding_identity,
                    vector_text,
                ),
            )
            connection.execute(
                """
                INSERT INTO bauer_rag_v4.search_projections (
                    projection_id, canonical_document_id,
                    tenant_id, knowledge_base_id, release_id,
                    source_id, projection_type, projection_schema,
                    search_text, search_text_sha256, exact_terms,
                    canonical_evidence_ids, subject, predicate,
                    qualifiers, embedding_identity_sha256, embedding
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s::vector
                )
                """,
                (
                    projection.projection_id,
                    document.document_id,
                    *scope,
                    projection.projection_type,
                    projection.projection_schema,
                    projection.search_text,
                    projection.search_text_sha256,
                    list(projection.exact_terms),
                    list(projection.canonical_evidence_ids),
                    projection.subject,
                    projection.predicate,
                    json.dumps(dict(projection.qualifiers)),
                    embedding_identity,
                    vector_text,
                ),
            )
