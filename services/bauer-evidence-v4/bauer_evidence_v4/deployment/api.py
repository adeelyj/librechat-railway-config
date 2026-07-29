from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from bauer_evidence_v3.auth import (
    AuthorizationContextError,
    verify_authorization_context,
)

from ..answering import AnswerService, SourceRegistry
from ..contracts.models import ReleaseContract, V4AnswerRequest, V4AnswerResponse
from ..retrieval import AuthorizedScope, CandidateGenerator, IndexedProjection
from .config import V4Settings
from .serde import document_from_data, projection_from_data


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoadedRelease:
    service: AnswerService
    external_source_ids: frozenset[str]
    source_uuid_by_external_id: dict[str, str]
    artifact_count: int
    projection_count: int


def load_release(settings: V4Settings) -> LoadedRelease:
    import psycopg

    with psycopg.connect(
        settings.database_url,
        autocommit=True,
        application_name="bauer-evidence-v4-api-loader",
    ) as connection:
        connection.execute("SET ROLE bauer_rag_v4_reader")
        connection.execute(
            "SELECT set_config('bauer_rag_v4.tenant_id', %s, false)",
            (settings.tenant_id,),
        )
        connection.execute(
            """
            SELECT set_config(
                'bauer_rag_v4.knowledge_base_id', %s, false
            )
            """,
            (settings.knowledge_base_id,),
        )
        connection.execute(
            "SELECT set_config('bauer_rag_v4.principal_id', %s, false)",
            (settings.principal_id,),
        )
        connection.execute(
            "SELECT set_config('bauer_rag_v4.release_id', %s, false)",
            (settings.candidate_release_id,),
        )
        source_rows = connection.execute(
            """
            SELECT source_id, external_source_id
            FROM bauer_rag_v4.resolve_pinned_release_sources()
            """
        ).fetchall()
        source_uuid_by_external_id = {
            str(external): str(source_id)
            for source_id, external in source_rows
        }
        connection.execute(
            "SELECT set_config('bauer_rag_v4.source_ids', %s, false)",
            (
                ",".join(
                    source_uuid_by_external_id[external]
                    for external in sorted(source_uuid_by_external_id)
                ),
            ),
        )
        release = connection.execute(
            """
            SELECT release_public_id, source_contract_sha256,
                   canonical_schema_version, compiler_identity_sha256,
                   projection_identity_sha256, embedding_identity_sha256,
                   reranker_identity_sha256, gate_manifest_sha256
            FROM bauer_rag_v4.resolve_pinned_release()
            """
        ).fetchone()
        if release is None:
            raise RuntimeError("the V4 candidate is not ready or not pinned")
        artifact_rows = connection.execute(
            """
            SELECT source.external_source_id,
                   artifact.canonical_json,
                   artifact.projections_json
            FROM bauer_rag_v4.compiled_artifacts AS artifact
            JOIN bauer_rag_v4.source_documents AS source
              ON source.source_id = artifact.source_id
            WHERE artifact.release_id = %s
            ORDER BY source.external_source_id
            """,
            (settings.candidate_release_id,),
        ).fetchall()
    if len(artifact_rows) != len(source_rows):
        raise RuntimeError(
            "V4 artifact accounting differs from release membership"
        )
    documents = {}
    external_by_sha = {}
    items = []
    for external_source_id, document_json, projections_json in artifact_rows:
        document_data = (
            document_json
            if isinstance(document_json, dict)
            else json.loads(document_json)
        )
        projection_data = (
            projections_json
            if isinstance(projections_json, list)
            else json.loads(projections_json)
        )
        document = document_from_data(document_data)
        external = str(external_source_id)
        documents[document.source_sha256] = document
        external_by_sha[document.source_sha256] = external
        items.extend(
            IndexedProjection(
                projection=projection_from_data(value),
                tenant_id=settings.tenant_id,
                knowledge_base_id=settings.knowledge_base_id,
                release_id=settings.candidate_release_id,
                authorization_source_id=external,
            )
            for value in projection_data
        )
    linked = _linked_filenames(documents)
    contract = ReleaseContract(
        public_id=str(release[0]),
        source_contract_sha256=str(release[1]),
        canonical_schema_version=str(release[2]),
        compiler_identity=str(release[3]),
        projection_identity=str(release[4]),
        embedding_identity=str(release[5]),
        reranker_identity=str(release[6]),
        gate_manifest_sha256=str(release[7]),
    )
    service = AnswerService(
        candidate_generator=CandidateGenerator(tuple(items)),
        source_registry=SourceRegistry(
            documents_by_sha256=documents,
            external_source_ids=external_by_sha,
            linked_filenames=linked,
        ),
        release=contract,
    )
    return LoadedRelease(
        service=service,
        external_source_ids=frozenset(source_uuid_by_external_id),
        source_uuid_by_external_id=source_uuid_by_external_id,
        artifact_count=len(artifact_rows),
        projection_count=len(items),
    )


def _linked_filenames(
    documents: dict[str, Any],
) -> dict[tuple[str, str], str]:
    by_number: dict[str, str] = {}
    for document in documents.values():
        for token in document.source_filename.replace(".", "_").split("_"):
            if token.isdigit():
                by_number.setdefault(token, document.source_filename)
    linked = {}
    for document in documents.values():
        for record in document.records:
            file_number = record.field("file_number")
            if file_number and file_number in by_number:
                linked[(document.source_sha256, file_number)] = by_number[
                    file_number
                ]
    return linked


def create_router(
    settings: V4Settings,
    loaded: LoadedRelease,
) -> APIRouter:
    router = APIRouter()
    allowed_agents = frozenset(settings.allowed_agent_ids)

    @router.get("/ready/v4")
    async def ready() -> dict[str, Any]:
        return {
            "status": "ready",
            "service": "bauer-evidence-v4",
            "build_commit": settings.build_commit,
            "candidate_release_id": settings.candidate_release_id,
            "artifact_count": loaded.artifact_count,
            "projection_count": loaded.projection_count,
            "active_release_pointer_used": False,
        }

    @router.get("/version/v4")
    async def version() -> dict[str, Any]:
        return {
            "service": "bauer-evidence-v4",
            "version": "4.0.0-private-shadow",
            "build_commit": settings.build_commit,
        }

    @router.post("/v4/answer", response_model=V4AnswerResponse)
    async def answer(
        request: Request,
        body: V4AnswerRequest,
    ) -> V4AnswerResponse:
        try:
            context = verify_authorization_context(
                body.authorization.signed_scope,
                keyring=settings.auth_keyring,
                audience=settings.auth_audience,
            )
        except AuthorizationContextError as error:
            raise HTTPException(
                status_code=401,
                detail="invalid V4 signed scope",
            ) from error
        if (
            context.tenant_id != settings.tenant_id
            or context.knowledge_base_id != settings.knowledge_base_id
            or context.agent_id not in allowed_agents
        ):
            raise HTTPException(
                status_code=403,
                detail="V4 scope is outside the private deployment",
            )
        requested_sources = frozenset(context.authorized_source_ids)
        if not requested_sources:
            raise HTTPException(
                status_code=403,
                detail="V4 source scope is empty",
            )
        unknown = requested_sources - loaded.external_source_ids
        if unknown:
            raise HTTPException(
                status_code=403,
                detail="V4 source scope contains an unknown release source",
            )
        trace_id = request.headers.get("x-request-id", "").strip()
        if not trace_id:
            trace_id = f"v4-{uuid.uuid4()}"
        result = await run_in_threadpool(
            loaded.service.answer,
            request_id=body.request_id,
            trace_id=trace_id[:128],
            question=body.question,
            search_hint=body.search_hint,
            locale=body.locale,
            scope=AuthorizedScope(
                principal_id=context.user_id,
                tenant_id=context.tenant_id,
                knowledge_base_id=context.knowledge_base_id,
                release_id=settings.candidate_release_id,
                authorized_external_source_ids=requested_sources,
            ),
        )
        await run_in_threadpool(
            _audit,
            settings,
            request_id=body.request_id,
            source_count=len(requested_sources),
        )
        return result

    return router


def _audit(
    settings: V4Settings,
    *,
    request_id: str,
    source_count: int,
) -> None:
    import psycopg

    request_sha = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    with psycopg.connect(
        settings.database_url,
        autocommit=True,
        application_name="bauer-evidence-v4-api-audit",
    ) as connection:
        connection.execute("SET ROLE bauer_rag_v4_reader")
        for name, value in (
            ("tenant_id", settings.tenant_id),
            ("knowledge_base_id", settings.knowledge_base_id),
            ("principal_id", settings.principal_id),
            ("release_id", settings.candidate_release_id),
        ):
            connection.execute(
                f"SELECT set_config('bauer_rag_v4.{name}', %s, false)",
                (value,),
            )
        connection.execute(
            """
            SELECT bauer_rag_v4.record_authorization_audit(
                'allow', 'signed_scope_verified', %s, %s
            )
            """,
            (request_sha, source_count),
        )
