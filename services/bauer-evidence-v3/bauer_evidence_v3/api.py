from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from functools import partial
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .answering import AnswerService
from .auth import (
    AuthorizationContext,
    AuthorizationContextError,
    verify_authorization_context,
)
from .releases import ReleaseError
from .telemetry import (
    AuthorizationAuditEvent,
    AuthorizationAuditSink,
    record_answer_decision,
    record_authorization_audit,
)


SERVICE_VERSION = "3.0.0-dev"


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    mandatory_constraints: dict[str, str | list[str]] = Field(default_factory=dict)
    forbidden_claim_values: list[str] = Field(default_factory=list, max_length=50)
    top_k: int = Field(default=8, ge=1, le=20)


class QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    top_k: int = Field(default=8, ge=1, le=20)


def create_app(
    *,
    answer_service: AnswerService,
    auth_keyring: Mapping[str, bytes | str],
    auth_audience: str,
    readiness: Callable[[], dict[str, Any]] | None = None,
    context_guard: Callable[[AuthorizationContext], bool] | None = None,
    authorization_audit_sink: AuthorizationAuditSink | None = None,
    build_commit: str = "unknown",
) -> FastAPI:
    app = FastAPI(
        title="Bauer Evidence V3",
        version=SERVICE_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip() or str(uuid.uuid4())
        request.state.request_id = request_id[:128]
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        response.headers["cache-control"] = "no-store"
        return response

    async def audit_authorization(
        request: Request,
        *,
        outcome: str,
        reason_code: str,
        context: AuthorizationContext | None = None,
    ) -> None:
        event = AuthorizationAuditEvent(
            request_id=request.state.request_id,
            route=request.url.path,
            outcome=outcome,
            reason_code=reason_code,
            tenant_id=context.tenant_id if context is not None else None,
            knowledge_base_id=(
                context.knowledge_base_id if context is not None else None
            ),
            user_id=context.user_id if context is not None else None,
            agent_id=context.agent_id if context is not None else None,
            authorized_source_count=(
                len(context.authorized_source_ids) if context is not None else None
            ),
            authorized_source_ids=(
                context.authorized_source_ids if context is not None else ()
            ),
        )
        # A durable sink performs PostgreSQL I/O. Keep it off the async event
        # loop while preserving fail-closed behavior if the audit append fails.
        await run_in_threadpool(
            partial(
                record_authorization_audit,
                event,
                sink=authorization_audit_sink,
            )
        )

    @app.exception_handler(AuthorizationContextError)
    async def authorization_error(request: Request, exc: AuthorizationContextError):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_authorization_context", "detail": str(exc)},
        )

    @app.exception_handler(ReleaseError)
    async def release_error(_: Request, exc: ReleaseError):
        return JSONResponse(
            status_code=503,
            content={"error": "knowledge_release_unavailable", "detail": str(exc)},
        )

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, __: PermissionError):
        context = getattr(request.state, "authorization_context", None)
        if not getattr(request.state, "authorization_denial_audited", False):
            await audit_authorization(
                request,
                outcome="denied",
                reason_code="authorized_scope_rejected",
                context=context,
            )
        return JSONResponse(
            status_code=403,
            content={"error": "authorization_scope_denied"},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "bauer-evidence-v3",
            "version": SERVICE_VERSION,
            "build_commit": build_commit,
        }

    @app.get("/version")
    async def version() -> dict[str, Any]:
        return {
            "service": "bauer-evidence-v3",
            "version": SERVICE_VERSION,
            "build_commit": build_commit,
        }

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        if readiness is None:
            return {
                "status": "ready",
                "service": "bauer-evidence-v3",
                "mode": "injected",
            }
        details = await run_in_threadpool(readiness)
        if details.get("status") != "ready":
            raise HTTPException(status_code=503, detail=details)
        return details

    async def authorize(
        request: Request,
        authorization: str | None,
    ) -> AuthorizationContext:
        if not authorization or not authorization.startswith("Bearer "):
            await audit_authorization(
                request,
                outcome="denied",
                reason_code="missing_bearer_context",
            )
            raise HTTPException(status_code=401, detail="Bearer authorization context is required")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            context = verify_authorization_context(
                token,
                keyring=auth_keyring,
                audience=auth_audience,
            )
        except AuthorizationContextError:
            await audit_authorization(
                request,
                outcome="denied",
                reason_code="invalid_signed_context",
            )
            raise
        request.state.authorization_context = context
        if context_guard is not None and not context_guard(context):
            request.state.authorization_denial_audited = True
            await audit_authorization(
                request,
                outcome="denied",
                reason_code="deployment_scope_mismatch",
                context=context,
            )
            raise PermissionError("authorization context is outside this deployment scope")
        await audit_authorization(
            request,
            outcome="allowed",
            reason_code="signed_scope_verified",
            context=context,
        )
        return context

    @app.post("/v3/answer")
    async def answer(
        request: Request,
        body: AnswerBody,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        context = await authorize(request, authorization)
        result = await answer_service.answer(
            authorization=context,
            question=body.query,
            mandatory_constraints=body.mandatory_constraints,
            forbidden_claim_values=tuple(body.forbidden_claim_values),
            top_k=body.top_k,
        )
        record_answer_decision(
            request_id=request.state.request_id,
            route=request.url.path,
            release_id=result.release_id,
            outcome=result.status,
            evidence_count=len(result.evidence.citations),
            repair_attempted=result.repair_attempted,
            validation_disposition=(
                result.validation.disposition.value
                if result.validation is not None
                else None
            ),
        )
        return {
            "status": result.status,
            "answer": result.answer,
            "release_id": result.release_id,
            "repair_attempted": result.repair_attempted,
            "query_plan": {
                "channels": [channel.value for channel in result.plan.channels],
                "identifiers": list(result.plan.identifiers),
                "table_intent": result.plan.table_intent,
                "superlative": result.plan.superlative,
                "mandatory_constraints": result.plan.mandatory_constraints,
            },
            "evidence": [asdict(item) for item in result.evidence.citations],
            "validation": (
                {
                    "valid": result.validation.valid,
                    "disposition": result.validation.disposition.value,
                    "safe_refusal_detected": result.validation.safe_refusal_detected,
                    "violations": [asdict(item) for item in result.validation.violations],
                }
                if result.validation is not None
                else None
            ),
        }

    @app.post("/v3/query")
    async def query(
        request: Request,
        body: QueryBody,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        context = await authorize(request, authorization)
        run = await run_in_threadpool(
            answer_service.retrieve_pinned,
            authorization=context,
            question=body.query,
            top_k=body.top_k,
        )
        return {
            "release_id": run.release_id,
            "answer_validation_applied": False,
            "query_plan": {
                "channels": [channel.value for channel in run.plan.channels],
                "identifiers": list(run.plan.identifiers),
                "table_intent": run.plan.table_intent,
            },
            "results": [
                {
                    "evidence_id": item.evidence.evidence_id,
                    "tenant_id": item.evidence.tenant_id,
                    "knowledge_base_id": item.evidence.knowledge_base_id,
                    "release_id": item.evidence.release_id,
                    "source_document_id": item.evidence.source_document_id,
                    "external_file_id": (
                        str(item.evidence.metadata.get("external_file_id", "")).strip()
                        or None
                    ),
                    "source_version_id": item.evidence.source_version_id,
                    "source_sha256": item.evidence.source_sha256,
                    "source_type": item.evidence.source_type,
                    "title": item.evidence.title,
                    "content": item.evidence.content,
                    "page_number": item.evidence.coordinate.page_number,
                    "printed_page_label": item.evidence.coordinate.printed_page_label,
                    "coordinate": asdict(item.evidence.coordinate),
                    "is_citable": item.evidence.is_citable,
                    "generated_summary": item.evidence.generated_summary,
                    "score": item.score,
                    "channels": item.channels,
                    "reasons": item.reasons,
                }
                for item in run.results
            ],
        }

    return app
