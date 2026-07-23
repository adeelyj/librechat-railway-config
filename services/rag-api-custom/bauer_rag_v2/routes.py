from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from . import DEFAULT_INDEX_VERSION, EXTRACTOR_VERSION
from .extraction import normalize_for_search
from .fusion import (
    analyze_query,
    filter_candidates_by_score,
    limit_candidates_per_file,
    reciprocal_rank_fusion,
    rerank_candidates,
)
from .models import QueryV2Body, StartIndexRunBody, ValidateV2Body
from .repository import (
    activate_index_run,
    exact_search,
    fail_index_run,
    get_index_run,
    lexical_search,
    resolve_index_version,
    stage_document,
    start_index_run,
    vector_search,
    v2_schema_state,
)
from .validator import validate_answer


logger = logging.getLogger(__name__)
router = APIRouter(tags=["bauer-rag-v2"])
MAX_BATCH_FILES = 1000
# LibreChat requests eight compact items. Evaluation may request ten so Recall@10
# remains measurable without changing the model-visible production payload.
MAX_EVIDENCE_ITEMS = 10
MAX_EVIDENCE_PER_FILE = 2
MAX_INDEX_FILE_BYTES = int(os.getenv("BAUER_RAG_V2_MAX_INDEX_FILE_BYTES", str(25 * 1024 * 1024)))


def _csv_env(name: str) -> set[str]:
    return {
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    }


def _request_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bauer RAG V2 requires an authenticated request",
        )
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    configured = _csv_env("BAUER_RAG_V2_ADMIN_USER_IDS")
    role = str(user.get("role", "")).upper()
    if role != "ADMIN" and str(user["id"]) not in configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bauer RAG V2 index administration is not authorized",
        )
    return user


def _authorized_namespace(request: Request, entity_id: str | None) -> str:
    user = _request_user(request)
    namespace = entity_id or str(user["id"])
    allowlist = _csv_env("BAUER_RAG_V2_NAMESPACE_IDS")
    if not allowlist:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bauer RAG V2 namespace allow-list is not configured",
        )
    if namespace not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bauer RAG V2 namespace is not authorized",
        )
    return namespace


def _debug_allowed(request: Request) -> bool:
    user = _request_user(request)
    return str(user.get("role", "")).upper() == "ADMIN" or str(user["id"]) in _csv_env(
        "BAUER_RAG_V2_DEBUG_USER_IDS"
    )


def _validate_file_ids(file_ids: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in file_ids:
        clean = str(value).strip()
        if not clean or len(clean) > 200:
            raise HTTPException(status_code=400, detail="file_ids contains an invalid value")
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)
    if not unique:
        raise HTTPException(status_code=400, detail="file_ids must not be empty")
    if len(unique) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_BATCH_FILES} file_ids are allowed",
        )
    return unique


def _require_v2_schema() -> None:
    if not v2_schema_state()["ready"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bauer RAG V2 schema is not ready; V1 remains available",
        )


def _minimum_evidence_score() -> float:
    try:
        configured = float(os.getenv("BAUER_RAG_V2_MIN_EVIDENCE_SCORE", "0.08"))
    except ValueError:
        logger.error("Invalid BAUER_RAG_V2_MIN_EVIDENCE_SCORE; using 0.08")
        configured = 0.08
    return min(max(configured, 0.0), 1.0)


def _display_label(value: str | None) -> str | None:
    if value is None:
        return None
    clean = re.sub(r"^#+\s*", "", value.strip())
    clean = clean.replace("_", " ")
    clean = re.sub(r"\s*\|\s*", " / ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _candidate_evidence(candidate: Any, rank: int) -> dict[str, Any]:
    normalized_content = normalize_for_search(candidate.content)

    def relevant(values: list[str], limit: int = 8) -> list[str]:
        return [
            value
            for value in values
            if normalize_for_search(value) in normalized_content
        ][:limit]

    return {
        "citation_id": f"V2-{rank}",
        "file_id": candidate.file_id,
        "filename": candidate.filename,
        "title": candidate.title,
        "language": candidate.language,
        "publication_date": candidate.publication_date,
        "certificates": relevant(candidate.certificates, 4),
        "product_families": relevant(candidate.product_families),
        "media": relevant(candidate.media),
        "component_categories": relevant(candidate.component_categories),
        "standards": relevant(candidate.standards),
        "content": candidate.content,
        "page": candidate.page,
        "section": [_display_label(value) for value in candidate.section_path],
        "chunk_kind": candidate.chunk_kind,
        "table_title": _display_label(candidate.table_title),
        "row_label": _display_label(candidate.row_label),
        "headers": candidate.headers,
        "row_values": candidate.row_values,
        "units": candidate.units,
        "footnotes": candidate.footnotes,
        "source_type": candidate.source_type,
        "channels": sorted(candidate.channels),
        "score": round(candidate.final_score, 8),
        "metadata": {
            key: value
            for key, value in candidate.metadata.items()
            if key in {"parser", "reranker", "extractor_version"}
        },
    }


@router.post("/query_v2")
async def query_v2(request: Request, body: QueryV2Body):
    started = time.perf_counter()
    namespace = _authorized_namespace(request, body.entity_id)
    _require_v2_schema()
    file_ids = _validate_file_ids(body.file_ids)
    if not 1 <= body.k <= MAX_EVIDENCE_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"k must be between 1 and {MAX_EVIDENCE_ITEMS}",
        )
    if body.debug and not _debug_allowed(request):
        raise HTTPException(status_code=403, detail="V2 debug output is not authorized")

    try:
        index_version = await resolve_index_version(namespace, body.index_version)
    except LookupError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    analysis = analyze_query(body.query)
    channel_tasks = {
        "exact": asyncio.create_task(
            exact_search(
                namespace=namespace,
                index_version=index_version,
                file_ids=file_ids,
                analysis=analysis,
                limit=20,
            )
        ),
        "lexical": asyncio.create_task(
            lexical_search(
                namespace=namespace,
                index_version=index_version,
                file_ids=file_ids,
                analysis=analysis,
                limit=30,
            )
        ),
        "vector": asyncio.create_task(
            vector_search(
                namespace=namespace,
                index_version=index_version,
                file_ids=file_ids,
                query=body.query,
                executor=request.app.state.thread_pool,
                limit=30,
            )
        ),
    }
    channel_results: dict[str, list[Any]] = {}
    channel_errors: dict[str, str] = {}
    completed = await asyncio.gather(*channel_tasks.values(), return_exceptions=True)
    for channel, result in zip(channel_tasks, completed):
        if isinstance(result, Exception):
            channel_errors[channel] = type(result).__name__
            logger.error(
                "Bauer RAG V2 %s channel failed: %s",
                channel,
                type(result).__name__,
                exc_info=(type(result), result, result.__traceback__),
            )
        else:
            channel_results[channel] = result
    if not channel_results:
        raise HTTPException(status_code=500, detail="All Bauer RAG V2 search channels failed")

    fused = reciprocal_rank_fusion(channel_results)[:30]
    reranked, reranker_info = await rerank_candidates(
        analysis,
        fused,
        top_n=min(30, max(body.k * 3, body.k)),
    )
    allowed = set(file_ids)
    authorized = [candidate for candidate in reranked if candidate.file_id in allowed]
    if len(authorized) != len(reranked):
        logger.error(
            "Bauer RAG V2 response filter dropped %d unauthorized candidate(s)",
            len(reranked) - len(authorized),
        )
    minimum_score = _minimum_evidence_score()
    filtered = filter_candidates_by_score(
        authorized,
        minimum_score=minimum_score,
    )
    selected = limit_candidates_per_file(
        filtered,
        top_n=body.k,
        max_per_file=(
            max(MAX_EVIDENCE_PER_FILE, 3)
            if analysis.pressure_extremum
            else MAX_EVIDENCE_PER_FILE
        ),
        max_per_page=1 if analysis.pressure_extremum else None,
    )
    evidence = [
        _candidate_evidence(candidate, rank)
        for rank, candidate in enumerate(selected, start=1)
    ]
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "Bauer RAG V2 query namespace=%s index_version=%s files=%d results=%d elapsed_ms=%.2f degraded=%s",
        namespace,
        index_version,
        len(file_ids),
        len(evidence),
        elapsed_ms,
        bool(channel_errors),
    )
    response: dict[str, Any] = {
        "route": "v2",
        "index_version": index_version,
        "results": evidence,
        "degraded": bool(channel_errors),
        "safe_refusal": (
            "The answer was not established in the authorized indexed Bauer sources."
            if not evidence
            else None
        ),
    }
    if body.debug:
        response["debug"] = {
            "analysis": {
                "identifiers": analysis.identifiers,
                "standards": analysis.standards,
                "number_units": analysis.number_units,
                "quoted_phrases": analysis.quoted_phrases,
                "table_intent": analysis.table_intent,
                "document_lookup": analysis.document_lookup,
                "pressure_extremum": analysis.pressure_extremum,
                "citation_intent": analysis.citation_intent,
            },
            "channel_counts": {
                channel: len(results) for channel, results in channel_results.items()
            },
            "channel_errors": channel_errors,
            "fused_count": len(fused),
            "reranker": reranker_info,
            "minimum_evidence_score": minimum_score,
            "below_threshold_count": len(authorized) - len(filtered),
            "elapsed_ms": elapsed_ms,
        }
    return response


@router.post("/v2/index-runs")
async def create_index_run(request: Request, body: StartIndexRunBody):
    _require_admin(request)
    _require_v2_schema()
    allowlist = _csv_env("BAUER_RAG_V2_NAMESPACE_IDS")
    if body.namespace not in allowlist:
        raise HTTPException(status_code=403, detail="Index namespace is not allow-listed")
    run = await start_index_run(
        namespace=body.namespace,
        index_version=body.index_version,
        extractor_version=body.extractor_version or EXTRACTOR_VERSION,
        embedding_version=body.embedding_version,
        expected_file_count=body.expected_file_count,
        metadata=body.metadata,
    )
    return {
        "run_id": str(run["run_id"]),
        "status": run["status"],
        "namespace": run["namespace"],
        "index_version": run["index_version"],
    }


@router.post("/v2/index-runs/{run_id}/documents")
async def upload_index_document(
    request: Request,
    run_id: uuid.UUID,
    file_id: str = Form(...),
    checksum: str = Form(...),
    filename: str = Form(...),
    source_type: str = Form("public_document"),
    file: UploadFile = File(...),
):
    _require_admin(request)
    _require_v2_schema()
    if not file_id.strip() or len(file_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid file_id")
    if not filename.strip() or len(filename) > 500:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if source_type not in {"public_document", "synthetic_demo"}:
        raise HTTPException(status_code=400, detail="Invalid source_type")
    if not filename.casefold().endswith((".md", ".markdown", ".txt")):
        raise HTTPException(
            status_code=400,
            detail="The initial V2 indexer accepts reviewed Markdown/text derivatives only",
        )

    payload = await file.read(MAX_INDEX_FILE_BYTES + 1)
    if len(payload) > MAX_INDEX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="V2 index document exceeds the size limit")
    actual_checksum = hashlib.sha256(payload).hexdigest()
    if actual_checksum.casefold() != checksum.casefold():
        raise HTTPException(status_code=400, detail="V2 index document checksum mismatch")
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=400, detail="V2 index document must be UTF-8") from error

    try:
        return await stage_document(
            run_id=run_id,
            file_id=file_id.strip(),
            checksum=actual_checksum,
            filename=filename.strip(),
            content=content,
            source_type=source_type,
            executor=request.app.state.thread_pool,
        )
    except Exception as error:
        await fail_index_run(run_id, f"{type(error).__name__}: {error}")
        logger.exception("Bauer RAG V2 index staging failed for file_id=%s", file_id)
        raise HTTPException(status_code=500, detail="V2 document indexing failed") from error


@router.post("/v2/index-runs/{run_id}/activate")
async def activate_run(request: Request, run_id: uuid.UUID):
    _require_admin(request)
    _require_v2_schema()
    try:
        return await activate_index_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/v2/index-runs/{run_id}/fail")
async def mark_run_failed(request: Request, run_id: uuid.UUID):
    _require_admin(request)
    _require_v2_schema()
    await fail_index_run(run_id, "Marked failed by an authorized operator")
    return {"run_id": str(run_id), "status": "failed"}


@router.get("/v2/index-runs/{run_id}")
async def index_run_status(request: Request, run_id: uuid.UUID):
    _require_admin(request)
    _require_v2_schema()
    run = await get_index_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown V2 index run")
    run["run_id"] = str(run["run_id"])
    return run


@router.post("/validate_v2")
async def validate_v2(request: Request, body: ValidateV2Body):
    _require_admin(request)
    return validate_answer(
        answer=body.answer,
        evidence=body.evidence,
        mandatory_constraints=body.mandatory_constraints,
        safe_refusal_expected=body.safe_refusal_expected,
    )


@router.get("/v2/status")
async def v2_status(request: Request):
    _require_admin(request)
    schema = v2_schema_state()
    return {
        "enabled": schema["ready"],
        "schema": schema,
        "default_index_version": DEFAULT_INDEX_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "namespace_allowlist_count": len(_csv_env("BAUER_RAG_V2_NAMESPACE_IDS")),
        "reranker_configured": bool(
            os.getenv("BAUER_RAG_V2_RERANK_URL") and os.getenv("BAUER_RAG_V2_RERANK_MODEL")
        ),
    }
