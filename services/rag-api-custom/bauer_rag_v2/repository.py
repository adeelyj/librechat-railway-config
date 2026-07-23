from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from . import DEFAULT_INDEX_VERSION, EXTRACTOR_VERSION
from .extraction import ParsedDocument, parse_markdown_document
from .fusion import Candidate, QueryAnalysis


logger = logging.getLogger(__name__)
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
EXPECTED_EMBEDDING_DIMENSIONS = 1024
_SCHEMA_READY = False
_SCHEMA_ERROR: str | None = None


def _pool():
    from app.services.database import PSQLDatabase

    return PSQLDatabase.get_pool()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _publication_date(value: str | None) -> date | None:
    """Convert extractor ISO dates to the native type required by asyncpg DATE."""
    return date.fromisoformat(value) if value else None


def _vector_literal(values: Iterable[float]) -> str:
    vector = [float(value) for value in values]
    if len(vector) != EXPECTED_EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"V2 expects {EXPECTED_EMBEDDING_DIMENSIONS}-dimensional embeddings, got {len(vector)}"
        )
    return "[" + ",".join(format(value, ".12g") for value in vector) + "]"


async def ensure_v2_schema() -> bool:
    """Prepare V2 without allowing an additive feature failure to take down V1."""
    global _SCHEMA_ERROR, _SCHEMA_READY
    try:
        pool = await _pool()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(
                "SELECT pg_advisory_lock(hashtext('bauer_rag_v2_schema_v1'))"
            )
            try:
                await connection.execute(schema)
            finally:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtext('bauer_rag_v2_schema_v1'))"
                )
        _SCHEMA_READY = True
        _SCHEMA_ERROR = None
        logger.info("Bauer RAG V2 additive schema ensured")
    except Exception as error:
        _SCHEMA_READY = False
        _SCHEMA_ERROR = type(error).__name__
        logger.exception(
            "Bauer RAG V2 schema initialization failed; V1 remains available"
        )
    return _SCHEMA_READY


def v2_schema_state() -> dict[str, Any]:
    return {"ready": _SCHEMA_READY, "error_type": _SCHEMA_ERROR}


async def start_index_run(
    *,
    namespace: str,
    index_version: str,
    extractor_version: str,
    embedding_version: str,
    expected_file_count: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    run_id = uuid.uuid4()
    pool = await _pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO bauer_rag_v2.index_runs (
                run_id, namespace, index_version, extractor_version,
                embedding_version, expected_file_count, status, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'running', $7::jsonb)
            RETURNING *
            """,
            run_id,
            namespace,
            index_version,
            extractor_version,
            embedding_version,
            expected_file_count,
            _json(metadata),
        )
    return dict(row)


async def get_index_run(run_id: uuid.UUID) -> dict[str, Any] | None:
    pool = await _pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT * FROM bauer_rag_v2.index_runs WHERE run_id = $1", run_id
        )
    return dict(row) if row else None


async def _embed_documents(contents: list[str], executor=None) -> list[list[float]]:
    from app.config import vector_store

    loop = asyncio.get_running_loop()
    batch_size = min(
        max(int(os.getenv("BAUER_RAG_V2_EMBED_BATCH_SIZE", "64")), 1),
        128,
    )
    embeddings: list[list[float]] = []
    for start in range(0, len(contents), batch_size):
        batch = contents[start : start + batch_size]
        values = await loop.run_in_executor(
            executor,
            lambda batch=batch: vector_store.embedding_function.embed_documents(batch),
        )
        if len(values) != len(batch):
            raise ValueError("Embedding provider returned an unexpected batch size")
        embeddings.extend(values)
    return embeddings


async def _existing_reusable_document(
    *,
    namespace: str,
    index_version: str,
    file_id: str,
    checksum: str,
    extractor_version: str,
    embedding_version: str,
) -> uuid.UUID | None:
    pool = await _pool()
    async with pool.acquire() as connection:
        return await connection.fetchval(
            """
            SELECT document_id
            FROM bauer_rag_v2.documents
            WHERE namespace = $1
              AND index_version = $2
              AND file_id = $3
              AND checksum = $4
              AND extractor_version = $5
              AND embedding_version = $6
            ORDER BY active DESC, created_at DESC
            LIMIT 1
            """,
            namespace,
            index_version,
            file_id,
            checksum,
            extractor_version,
            embedding_version,
        )


async def stage_document(
    *,
    run_id: uuid.UUID,
    file_id: str,
    checksum: str,
    filename: str,
    content: str,
    source_type: str,
    executor=None,
) -> dict[str, Any]:
    run = await get_index_run(run_id)
    if not run:
        raise ValueError("Unknown V2 index run")
    if run["status"] != "running":
        raise ValueError(f"V2 index run is {run['status']}, not running")

    pool = await _pool()
    async with pool.acquire() as connection:
        prior = await connection.fetchrow(
            """
            SELECT document_id, action
            FROM bauer_rag_v2.index_run_documents
            WHERE run_id = $1 AND file_id = $2
            """,
            run_id,
            file_id,
        )
        if prior:
            return {
                "file_id": file_id,
                "document_id": str(prior["document_id"]),
                "action": prior["action"],
                "idempotent": True,
            }

    reusable = await _existing_reusable_document(
        namespace=run["namespace"],
        index_version=run["index_version"],
        file_id=file_id,
        checksum=checksum,
        extractor_version=run["extractor_version"],
        embedding_version=run["embedding_version"],
    )
    if reusable:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO bauer_rag_v2.index_run_documents
                    (run_id, document_id, file_id, action)
                VALUES ($1, $2, $3, 'reused')
                ON CONFLICT (run_id, file_id) DO NOTHING
                """,
                run_id,
                reusable,
                file_id,
            )
        return {
            "file_id": file_id,
            "document_id": str(reusable),
            "action": "reused",
            "chunk_count": 0,
        }

    parsed = parse_markdown_document(
        text=content,
        file_id=file_id,
        checksum=checksum,
        filename=filename,
        namespace=run["namespace"],
        source_type=source_type,
    )
    embeddings = await _embed_documents(
        [chunk.content for chunk in parsed.chunks],
        executor=executor,
    )
    if len(embeddings) != len(parsed.chunks):
        raise ValueError("Embedding provider returned an unexpected number of vectors")
    vector_literals = [_vector_literal(values) for values in embeddings]
    document_id = uuid.uuid4()

    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO bauer_rag_v2.documents (
                    document_id, namespace, index_version, file_id, checksum,
                    filename, title, language, source_type, source_path,
                    document_number, certificate, certificates, revision, organization,
                    address, publication_date, product_families, media, component_categories,
                    standards, extractor_version, embedding_version,
                    active, metadata
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13::text[], $14, $15, $16, $17::date,
                    $18::text[], $19::text[], $20::text[], $21::text[],
                    $22, $23, false, $24::jsonb
                )
                """,
                document_id,
                parsed.namespace,
                run["index_version"],
                parsed.file_id,
                parsed.checksum,
                parsed.filename,
                parsed.title,
                parsed.language,
                parsed.source_type,
                parsed.source_path,
                parsed.document_number,
                parsed.certificate,
                list(parsed.certificates),
                parsed.revision,
                parsed.organization,
                parsed.address,
                _publication_date(parsed.publication_date),
                list(parsed.product_families),
                list(parsed.media),
                list(parsed.component_categories),
                list(parsed.standards),
                run["extractor_version"],
                run["embedding_version"],
                _json(parsed.metadata),
            )
            for chunk, vector in zip(parsed.chunks, vector_literals):
                await connection.execute(
                    """
                    INSERT INTO bauer_rag_v2.chunks (
                        chunk_id, document_id, namespace, index_version, file_id,
                        checksum, ordinal, chunk_kind, page, section_path,
                        table_title, row_label, headers, row_values, units,
                        footnotes, content, search_text, embedding, metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::text[],
                        $11, $12, $13::jsonb, $14::jsonb, $15::text[],
                        $16, $17, $18, $19::vector, $20::jsonb
                    )
                    """,
                    chunk.chunk_id,
                    document_id,
                    parsed.namespace,
                    run["index_version"],
                    parsed.file_id,
                    parsed.checksum,
                    chunk.ordinal,
                    chunk.chunk_kind,
                    chunk.page,
                    list(chunk.section_path),
                    chunk.table_title,
                    chunk.row_label,
                    _json(list(chunk.headers)),
                    _json(list(chunk.row_values)),
                    list(chunk.units),
                    chunk.footnotes,
                    chunk.content,
                    chunk.search_text,
                    vector,
                    _json(chunk.metadata),
                )
            for entity in parsed.entities:
                chunk_id = entity.source_span.get("chunk_id")
                await connection.execute(
                    """
                    INSERT INTO bauer_rag_v2.entities (
                        document_id, chunk_id, kind, raw_value, normalized_value,
                        numeric_value, unit, source_span
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    document_id,
                    chunk_id,
                    entity.kind,
                    entity.raw_value,
                    entity.normalized_value,
                    entity.numeric_value,
                    entity.unit,
                    _json(entity.source_span),
                )
            await connection.execute(
                """
                INSERT INTO bauer_rag_v2.index_run_documents
                    (run_id, document_id, file_id, action)
                VALUES ($1, $2, $3, 'indexed')
                """,
                run_id,
                document_id,
                file_id,
            )
    return {
        "file_id": file_id,
        "document_id": str(document_id),
        "action": "indexed",
        "chunk_count": len(parsed.chunks),
        "table_row_count": parsed.metadata["table_row_count"],
        "entity_count": len(parsed.entities),
    }


async def activate_index_run(run_id: uuid.UUID) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            run = await connection.fetchrow(
                """
                SELECT *
                FROM bauer_rag_v2.index_runs
                WHERE run_id = $1
                FOR UPDATE
                """,
                run_id,
            )
            if not run:
                raise ValueError("Unknown V2 index run")
            if run["status"] != "running":
                raise ValueError(f"V2 index run is {run['status']}, not running")
            actual_count = await connection.fetchval(
                "SELECT count(*) FROM bauer_rag_v2.index_run_documents WHERE run_id = $1",
                run_id,
            )
            if actual_count != run["expected_file_count"]:
                raise ValueError(
                    f"V2 index run has {actual_count} files; expected {run['expected_file_count']}"
                )

            await connection.execute(
                """
                UPDATE bauer_rag_v2.documents
                SET active = false
                WHERE namespace = $1 AND index_version = $2 AND active
                """,
                run["namespace"],
                run["index_version"],
            )
            await connection.execute(
                """
                UPDATE bauer_rag_v2.documents AS document
                SET active = true, activated_at = now()
                FROM bauer_rag_v2.index_run_documents AS member
                WHERE member.run_id = $1
                  AND document.document_id = member.document_id
                """,
                run_id,
            )
            await connection.execute(
                """
                UPDATE bauer_rag_v2.index_runs
                SET status = 'abandoned', completed_at = now()
                WHERE namespace = $1
                  AND index_version = $2
                  AND status = 'active'
                  AND run_id <> $3
                """,
                run["namespace"],
                run["index_version"],
                run_id,
            )
            await connection.execute(
                """
                UPDATE bauer_rag_v2.index_runs
                SET status = 'active', completed_at = now(), error = NULL
                WHERE run_id = $1
                """,
                run_id,
            )
    return {
        "run_id": str(run_id),
        "namespace": run["namespace"],
        "index_version": run["index_version"],
        "file_count": actual_count,
        "status": "active",
    }


async def fail_index_run(run_id: uuid.UUID, error: str) -> None:
    pool = await _pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE bauer_rag_v2.index_runs
            SET status = 'failed', completed_at = now(), error = left($2, 4000)
            WHERE run_id = $1 AND status = 'running'
            """,
            run_id,
            error,
        )


async def _active_index_version(namespace: str, requested: str | None) -> str | None:
    pool = await _pool()
    async with pool.acquire() as connection:
        if requested:
            return await connection.fetchval(
                """
                SELECT index_version
                FROM bauer_rag_v2.index_runs
                WHERE namespace = $1 AND index_version = $2 AND status = 'active'
                """,
                namespace,
                requested,
            )
        return await connection.fetchval(
            """
            SELECT index_version
            FROM bauer_rag_v2.index_runs
            WHERE namespace = $1 AND status = 'active'
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            namespace,
        )


CANDIDATE_COLUMNS = """
    chunk.chunk_id,
    chunk.file_id,
    document.filename,
    document.title,
    document.language,
    document.publication_date,
    document.certificates,
    document.product_families,
    document.media,
    document.component_categories,
    document.standards,
    chunk.content,
    chunk.page,
    chunk.section_path,
    chunk.chunk_kind,
    chunk.table_title,
    chunk.row_label,
    chunk.headers,
    chunk.row_values,
    chunk.units,
    chunk.footnotes,
    document.source_type,
    chunk.metadata
"""


def _candidate_from_row(row: Any, channel: str, raw_score: float) -> Candidate:
    return Candidate(
        chunk_id=row["chunk_id"],
        file_id=row["file_id"],
        filename=row["filename"],
        title=row["title"],
        language=row["language"],
        publication_date=(
            row["publication_date"].isoformat() if row["publication_date"] else None
        ),
        certificates=list(row["certificates"] or []),
        product_families=list(row["product_families"] or []),
        media=list(row["media"] or []),
        component_categories=list(row["component_categories"] or []),
        standards=list(row["standards"] or []),
        content=row["content"],
        page=row["page"],
        section_path=list(row["section_path"] or []),
        chunk_kind=row["chunk_kind"],
        table_title=row["table_title"],
        row_label=row["row_label"],
        headers=list(_decode_json(row["headers"], [])),
        row_values=list(_decode_json(row["row_values"], [])),
        units=list(row["units"] or []),
        footnotes=row["footnotes"],
        source_type=row["source_type"],
        metadata=dict(_decode_json(row["metadata"], {})),
        channels={channel: {"raw_score": float(raw_score)}},
    )


def _maximum_pressure_bar(content: str) -> float:
    normalized = re.sub(r"\s+", " ", content.casefold())
    ranges = [
        float(upper.replace(",", "."))
        for _, upper in re.findall(
            r"(?<!\d)(\d{2,4}(?:[.,]\d+)?)\s*[-–—]\s*"
            r"(\d{2,4}(?:[.,]\d+)?)\s*bar\b",
            normalized,
        )
    ]
    singles = [
        float(value.replace(",", "."))
        for value in re.findall(
            r"(?<!\d)(\d{2,4}(?:[.,]\d+)?)\s*bar\b",
            normalized,
        )
    ]
    return max((*ranges, *singles), default=0.0)


def _pressure_evidence_sort_key(candidate: Candidate) -> tuple[Any, ...]:
    publication = int((candidate.publication_date or "0000-00-00").replace("-", ""))
    return (
        -float(candidate.metadata.get("pressure_extremum_bar") or 0.0),
        candidate.page is None,
        -publication,
        candidate.filename,
        candidate.page or 0,
        candidate.chunk_id,
    )


def _is_pressure_evidence_candidate(candidate: Candidate) -> bool:
    normalized = re.sub(r"\s+", " ", candidate.content.casefold())
    if candidate.chunk_kind == "table_row":
        return bool(
            re.match(
                r"^(?:BM|I|K|GIB|GI|PE)[\s-]*[0-9]",
                candidate.row_label or "",
                re.IGNORECASE,
            )
            and (
                "operating pressure" in normalized
                or "working pressure" in normalized
                or "operating pres-" in normalized
            )
        )
    if any(
        marker in normalized
        for marker in (
            "performance overview",
            "compressors air cooled",
            "compressors water cooled",
            "booster air cooled",
            "booster water cooled",
        )
    ):
        return True
    return "pressure range" in normalized and any(
        marker in normalized
        for marker in (
            "compressor units",
            "compressors are",
            "compressor blocks",
            "series compressors",
            "booster",
        )
    )


def _select_pressure_evidence(
    candidates: list[Candidate],
    *,
    limit: int,
) -> list[Candidate]:
    if not candidates or limit <= 0:
        return []
    global_maximum = max(
        float(candidate.metadata.get("pressure_extremum_bar") or 0.0)
        for candidate in candidates
    )
    compressor_overviews = sorted(
        (
            candidate
            for candidate in candidates
            if "compressors air cooled" in candidate.content.casefold()
            or "compressors water cooled" in candidate.content.casefold()
        ),
        key=_pressure_evidence_sort_key,
    )
    booster_overviews = sorted(
        (
            candidate
            for candidate in candidates
            if "booster air cooled" in candidate.content.casefold()
            or "booster water cooled" in candidate.content.casefold()
        ),
        key=_pressure_evidence_sort_key,
    )
    primary = sorted(
        (
            candidate
            for candidate in candidates
            if float(candidate.metadata.get("pressure_extremum_bar") or 0.0)
            == global_maximum
        ),
        key=_pressure_evidence_sort_key,
    )
    boosters = sorted(
        (
            candidate
            for candidate in candidates
            if "booster" in candidate.content.casefold()
        ),
        key=_pressure_evidence_sort_key,
    )
    qualifications = sorted(
        (
            candidate
            for candidate in candidates
            if "safety valve" in candidate.content.casefold()
            and any(
                term in candidate.content.casefold()
                for term in ("shutdown pressure", "shut-down pressure", "final pressure")
            )
        ),
        key=_pressure_evidence_sort_key,
    )
    all_candidates = sorted(candidates, key=_pressure_evidence_sort_key)
    selected: list[Candidate] = []
    seen: set[str] = set()
    pools = (
        compressor_overviews[: min(2, limit)],
        booster_overviews[: min(2, limit)],
        qualifications[: min(5, limit)],
        primary[: min(8, limit)],
        boosters[: min(4, limit)],
        all_candidates,
    )
    for pool in pools:
        for candidate in pool:
            if candidate.location_key in seen:
                continue
            seen.add(candidate.location_key)
            selected.append(candidate)
            if len(selected) == limit:
                return selected
    return selected


async def _pressure_extremum_search(
    *,
    namespace: str,
    index_version: str,
    file_ids: list[str],
    limit: int,
) -> list[Candidate]:
    pool = await _pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""
            SELECT
                {CANDIDATE_COLUMNS},
                0.0 AS channel_score
            FROM bauer_rag_v2.chunks AS chunk
            JOIN bauer_rag_v2.documents AS document
              ON document.document_id = chunk.document_id
            WHERE document.active
              AND document.namespace = $1
              AND document.index_version = $2
              AND document.file_id = ANY($3::text[])
              AND chunk.search_text ILIKE '%bar%'
              AND (
                    'compressor' = ANY(document.component_categories)
                 OR 'booster' = ANY(document.component_categories)
              )
            ORDER BY
                document.publication_date DESC NULLS LAST,
                CASE WHEN chunk.page IS NULL THEN 1 ELSE 0 END,
                chunk.page NULLS LAST,
                chunk.ordinal
            LIMIT 2000
            """,
            namespace,
            index_version,
            file_ids,
        )
    candidates = []
    for row in rows:
        maximum = _maximum_pressure_bar(row["content"])
        if maximum < 400:
            continue
        candidate = _candidate_from_row(
            row,
            "exact",
            1.0 + min(maximum / 10_000.0, 0.1),
        )
        candidate.metadata["pressure_extremum_bar"] = maximum
        if _is_pressure_evidence_candidate(candidate):
            candidates.append(candidate)
    return _select_pressure_evidence(candidates, limit=limit)


async def exact_search(
    *,
    namespace: str,
    index_version: str,
    file_ids: list[str],
    analysis: QueryAnalysis,
    limit: int = 20,
) -> list[Candidate]:
    if analysis.pressure_extremum and not analysis.exact_terms:
        return await _pressure_extremum_search(
            namespace=namespace,
            index_version=index_version,
            file_ids=file_ids,
            limit=limit,
        )
    terms = [value for value in analysis.exact_terms if len(value) >= 2][:40]
    if not terms:
        return []
    primary_terms = [
        value for value in analysis.primary_exact_terms if len(value) >= 2
    ][:30]
    patterns = [f"%{value}%" for value in terms if len(value) >= 3]
    pool = await _pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""
            SELECT
                {CANDIDATE_COLUMNS},
                GREATEST(
                    CASE WHEN btrim(regexp_replace(
                        lower(COALESCE(chunk.row_label, '')),
                        '[^[:alnum:]%]+', ' ', 'g'
                    )) = ANY($8::text[]) THEN 1.12 ELSE 0.0 END,
                    CASE WHEN btrim(regexp_replace(
                        lower(COALESCE(chunk.row_label, '')),
                        '[^[:alnum:]%]+', ' ', 'g'
                    )) = ANY($4::text[]) THEN 0.98 ELSE 0.0 END,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM bauer_rag_v2.entities AS entity
                        WHERE entity.document_id = document.document_id
                          AND entity.chunk_id = chunk.chunk_id
                          AND entity.normalized_value = ANY($8::text[])
                    ) THEN 1.08 ELSE 0.0 END,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM bauer_rag_v2.entities AS entity
                        WHERE entity.document_id = document.document_id
                          AND entity.chunk_id = chunk.chunk_id
                          AND entity.normalized_value = ANY($4::text[])
                    ) THEN 1.0 ELSE 0.0 END,
                    CASE WHEN document.filename ILIKE ANY($5::text[])
                        THEN CASE WHEN $7::boolean THEN 1.06 ELSE 0.95 END
                        ELSE 0.0 END,
                    CASE WHEN document.title ILIKE ANY($5::text[]) THEN 0.92 ELSE 0.0 END,
                    CASE WHEN document.certificate ILIKE ANY($5::text[]) THEN 0.98 ELSE 0.0 END,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM unnest(document.certificates) AS certificate(value)
                        WHERE certificate.value ILIKE ANY($5::text[])
                    ) THEN 0.98 ELSE 0.0 END,
                    CASE WHEN document.document_number ILIKE ANY($5::text[]) THEN 0.98 ELSE 0.0 END,
                    CASE WHEN document.publication_date::text ILIKE ANY($5::text[]) THEN 0.98 ELSE 0.0 END,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM bauer_rag_v2.entities AS entity
                        WHERE entity.document_id = document.document_id
                          AND entity.chunk_id = chunk.chunk_id
                          AND entity.normalized_value ILIKE ANY($5::text[])
                    ) THEN 0.90 ELSE 0.0 END,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM bauer_rag_v2.entities AS entity
                        WHERE entity.document_id = document.document_id
                          AND entity.normalized_value = ANY($4::text[])
                    ) THEN 0.86 ELSE 0.0 END,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM bauer_rag_v2.entities AS entity
                        WHERE entity.document_id = document.document_id
                          AND entity.normalized_value ILIKE ANY($5::text[])
                    ) THEN 0.78 ELSE 0.0 END
                ) + LEAST(GREATEST(similarity(chunk.search_text, $6), 0.0) * 0.20, 0.12)
                  AS channel_score
            FROM bauer_rag_v2.chunks AS chunk
            JOIN bauer_rag_v2.documents AS document
              ON document.document_id = chunk.document_id
            WHERE document.active
              AND document.namespace = $1
              AND document.index_version = $2
              AND document.file_id = ANY($3::text[])
              AND (
                    btrim(regexp_replace(
                        lower(COALESCE(chunk.row_label, '')),
                        '[^[:alnum:]%]+', ' ', 'g'
                    )) = ANY($4::text[])
                 OR document.filename ILIKE ANY($5::text[])
                 OR document.title ILIKE ANY($5::text[])
                 OR document.certificate ILIKE ANY($5::text[])
                 OR EXISTS (
                    SELECT 1 FROM unnest(document.certificates) AS certificate(value)
                    WHERE certificate.value ILIKE ANY($5::text[])
                 )
                 OR document.document_number ILIKE ANY($5::text[])
                 OR document.publication_date::text ILIKE ANY($5::text[])
                 OR chunk.row_label ILIKE ANY($5::text[])
                 OR EXISTS (
                    SELECT 1 FROM bauer_rag_v2.entities AS entity
                    WHERE entity.document_id = document.document_id
                      AND (
                           entity.normalized_value = ANY($4::text[])
                        OR entity.normalized_value ILIKE ANY($5::text[])
                      )
                 )
              )
            ORDER BY
                channel_score DESC,
                CASE WHEN $7::boolean AND chunk.page IS NULL THEN 1 ELSE 0 END,
                chunk.page NULLS LAST,
                chunk.ordinal,
                chunk.chunk_id
            LIMIT $9
            """,
            namespace,
            index_version,
            file_ids,
            terms,
            patterns,
            analysis.original,
            analysis.document_lookup,
            primary_terms,
            min(limit * 5, 100),
        )
    candidates = [_candidate_from_row(row, "exact", row["channel_score"]) for row in rows]
    candidates.sort(key=lambda item: -item.channels["exact"]["raw_score"])
    return candidates[:limit]


def _lexical_tsquery(analysis: QueryAnalysis) -> str:
    lexemes = []
    for token in analysis.tokens:
        lexemes.extend(re.findall(r"[^\W_]+", token, flags=re.UNICODE))
    unique = list(dict.fromkeys(lexeme for lexeme in lexemes if len(lexeme) >= 2))
    return " | ".join(unique[:40])


async def lexical_search(
    *,
    namespace: str,
    index_version: str,
    file_ids: list[str],
    analysis: QueryAnalysis,
    limit: int = 30,
) -> list[Candidate]:
    tsquery = _lexical_tsquery(analysis)
    if not tsquery:
        return []
    pool = await _pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""
            WITH request AS (
                SELECT to_tsquery('simple', $4) AS ts_query
            )
            SELECT
                {CANDIDATE_COLUMNS},
                (
                    ts_rank_cd(chunk.search_vector, request.ts_query) * 0.75
                    + similarity(chunk.search_text, $5) * 0.25
                ) AS channel_score
            FROM bauer_rag_v2.chunks AS chunk
            JOIN bauer_rag_v2.documents AS document
              ON document.document_id = chunk.document_id
            CROSS JOIN request
            WHERE document.active
              AND document.namespace = $1
              AND document.index_version = $2
              AND document.file_id = ANY($3::text[])
              AND (
                    chunk.search_vector @@ request.ts_query
                 OR chunk.search_text % $5
              )
            ORDER BY channel_score DESC, chunk.chunk_id
            LIMIT $6
            """,
            namespace,
            index_version,
            file_ids,
            tsquery,
            analysis.original,
            limit,
        )
    return [_candidate_from_row(row, "lexical", row["channel_score"]) for row in rows]


async def vector_search(
    *,
    namespace: str,
    index_version: str,
    file_ids: list[str],
    query: str,
    executor=None,
    limit: int = 30,
) -> list[Candidate]:
    from app.config import vector_store

    loop = asyncio.get_running_loop()
    embedding = await loop.run_in_executor(
        executor,
        lambda: vector_store.embedding_function.embed_query(query),
    )
    vector = _vector_literal(embedding)
    pool = await _pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""
            SELECT
                {CANDIDATE_COLUMNS},
                (chunk.embedding <=> $4::vector) AS distance
            FROM bauer_rag_v2.chunks AS chunk
            JOIN bauer_rag_v2.documents AS document
              ON document.document_id = chunk.document_id
            WHERE document.active
              AND document.namespace = $1
              AND document.index_version = $2
              AND document.file_id = ANY($3::text[])
              AND chunk.embedding IS NOT NULL
            ORDER BY chunk.embedding <=> $4::vector
            LIMIT $5
            """,
            namespace,
            index_version,
            file_ids,
            vector,
            limit,
        )
    return [
        _candidate_from_row(row, "vector", 1.0 - float(row["distance"]))
        for row in rows
    ]


async def resolve_index_version(namespace: str, requested: str | None = None) -> str:
    active = await _active_index_version(namespace, requested)
    if not active:
        target = requested or DEFAULT_INDEX_VERSION
        raise LookupError(
            f"No active Bauer RAG V2 index for namespace '{namespace}' and version '{target}'"
        )
    return active
