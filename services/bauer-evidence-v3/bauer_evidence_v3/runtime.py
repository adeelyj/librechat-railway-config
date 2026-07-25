from __future__ import annotations

import logging
import json
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Callable

from fastapi import FastAPI

from .answering import AnswerService
from .api import SERVICE_VERSION, create_app
from .auth import AuthorizationContext
from .config import ConfigurationError, Settings
from .db_security import verify_runtime_database_role
from .compile_handler import CompilationJobHandler
from .embeddings import OpenAICompatibleEmbeddingProvider
from .generation import ExtractiveModelGateway, OpenAICompatibleModelGateway
from .ingest import Compiler
from .ingest.ocr import RapidOcrEngine
from .ingest.render import PdfPlumberPageRenderer
from .object_store import (
    ContentAddressedObjectStore,
    LocalObjectStore,
    MirroredObjectStore,
    S3ObjectStore,
)
from .postgres_runtime import (
    PostgresCandidateReleaseRegistry,
    PostgresEvidenceIndex,
    PostgresReleaseRegistry,
)
from .postgres_audit import PostgresAuthorizationAuditSink
from .postgres_compiler import PostgresCompilerPersistence
from .postgres_jobs import PostgresJobQueue
from .releases import InMemoryReleaseRegistry
from .retrieval import InMemoryEvidenceIndex
from .telemetry import configure_telemetry
from .worker import CompilerWorker


LOGGER = logging.getLogger(__name__)


def deployment_context_guard(
    settings: Settings,
) -> Callable[[AuthorizationContext], bool]:
    allowed_agents = frozenset(settings.allowed_agent_ids)

    def guard(context: AuthorizationContext) -> bool:
        tenant_matches = (
            not settings.tenant_id or context.tenant_id == settings.tenant_id
        )
        knowledge_base_matches = (
            not settings.knowledge_base_id
            or context.knowledge_base_id == settings.knowledge_base_id
        )
        agent_matches = not allowed_agents or context.agent_id in allowed_agents
        return tenant_matches and knowledge_base_matches and agent_matches

    return guard


def build_api_app(settings: Settings) -> FastAPI:
    if settings.service_role != "api":
        raise ConfigurationError("API runtime requires BAUER_V3_SERVICE_ROLE=api")
    if settings.candidate_release_id and settings.allow_in_memory:
        raise ConfigurationError(
            "candidate-release mode requires durable PostgreSQL"
        )

    if settings.allow_in_memory:
        releases = InMemoryReleaseRegistry()
        index = InMemoryEvidenceIndex()
        model = ExtractiveModelGateway()
        authorization_audit_sink = None

        def readiness() -> dict[str, Any]:
            return {
                "status": "not_ready",
                "service": "bauer-evidence-v3",
                "mode": "in_memory_empty",
                "reason": "no_active_release",
            }

    else:
        embedding = OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        active_releases = PostgresReleaseRegistry(
            settings.database_url,
            tenant_id=settings.tenant_id,
            knowledge_base_id=settings.knowledge_base_id,
            principal_ids=settings.principal_ids,
            enforce_least_privilege=settings.production,
        )
        releases = (
            PostgresCandidateReleaseRegistry(
                active_releases,
                candidate_release_id=settings.candidate_release_id,
            )
            if settings.candidate_release_id
            else active_releases
        )
        index = PostgresEvidenceIndex(
            settings.database_url,
            tenant_id=settings.tenant_id,
            knowledge_base_id=settings.knowledge_base_id,
            principal_ids=settings.principal_ids,
            embedding_provider=embedding,
            embedding_dimensions=settings.embedding_dimensions,
            enforce_least_privilege=settings.production,
        )
        model = OpenAICompatibleModelGateway(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            model=settings.model_name,
        )
        readiness = _database_readiness(
            releases,
            expected_version=settings.expected_migration_version,
        )
        authorization_audit_sink = PostgresAuthorizationAuditSink(
            settings.database_url,
            tenant_id=settings.tenant_id,
            knowledge_base_id=settings.knowledge_base_id,
            principal_ids=settings.principal_ids,
            enforce_least_privilege=settings.production,
        )

    service = AnswerService(releases=releases, index=index, model=model)
    app = create_app(
        answer_service=service,
        auth_keyring=settings.auth_keyring,
        auth_audience=settings.auth_audience,
        readiness=readiness,
        context_guard=deployment_context_guard(settings),
        authorization_audit_sink=authorization_audit_sink,
        build_commit=settings.build_commit,
    )
    configure_telemetry(
        service_name="bauer-evidence-v3-api",
        service_version=SERVICE_VERSION,
        environment=settings.environment,
        fastapi_app=app,
    )
    return app


def _database_readiness(
    registry: PostgresReleaseRegistry | PostgresCandidateReleaseRegistry,
    *,
    expected_version: int,
) -> Callable[[], dict[str, Any]]:
    def readiness() -> dict[str, Any]:
        try:
            state = registry.readiness(expected_version=expected_version)
        except Exception:
            LOGGER.warning("V3 database readiness check failed")
            return {
                "status": "not_ready",
                "service": "bauer-evidence-v3",
                "reason": "database_unavailable",
            }
        details = asdict(state)
        details["status"] = "ready" if state.ready else "not_ready"
        details["service"] = "bauer-evidence-v3"
        details["mode"] = f"postgres_{state.release_selection}"
        return details

    return readiness


def build_object_store(settings: Settings) -> ContentAddressedObjectStore:
    if settings.object_store_backend == "local":
        return LocalObjectStore(settings.object_store_root)
    primary = S3ObjectStore(
        bucket=settings.s3_bucket,
        client=_s3_client(
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        ),
        prefix=settings.s3_prefix,
    )
    if not settings.mirror_s3_bucket:
        return primary
    mirror = S3ObjectStore(
        bucket=settings.mirror_s3_bucket,
        client=_s3_client(
            endpoint_url=settings.mirror_s3_endpoint_url,
            region_name=settings.mirror_s3_region,
            access_key_id=settings.mirror_s3_access_key_id,
            secret_access_key=settings.mirror_s3_secret_access_key,
        ),
        prefix=settings.s3_prefix,
    )
    return MirroredObjectStore(primary=primary, mirror=mirror)


def build_compiler_worker(settings: Settings) -> CompilerWorker:
    if settings.service_role != "worker":
        raise ConfigurationError(
            "compiler runtime requires BAUER_V3_SERVICE_ROLE=worker"
        )
    if settings.allow_in_memory:
        raise ConfigurationError("compiler runtime requires durable PostgreSQL mode")
    object_store = build_object_store(settings)
    connection_provider = _scoped_connection_provider(settings)
    queue = PostgresJobQueue(connection_provider)
    persistence = PostgresCompilerPersistence(
        connection_provider,
        object_store=object_store,
        principal_ids=settings.principal_ids,
    )
    embedding = OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    compiler = Compiler()
    ocr_version: str | None = None
    if settings.ocr_enabled:
        if (
            settings.ocr_detection_model is None
            or settings.ocr_recognition_model is None
            or settings.ocr_classification_model is None
        ):
            raise ConfigurationError(
                "enabled OCR requires all three model paths"
            )
        ocr_engine = RapidOcrEngine(
            detection_model=settings.ocr_detection_model,
            detection_model_sha256=settings.ocr_detection_model_sha256,
            recognition_model=settings.ocr_recognition_model,
            recognition_model_sha256=(
                settings.ocr_recognition_model_sha256
            ),
            classification_model=settings.ocr_classification_model,
            classification_model_sha256=(
                settings.ocr_classification_model_sha256
            ),
        )
        compiler = Compiler(
            ocr_engine=ocr_engine,
            page_renderer=PdfPlumberPageRenderer(),
            ocr_languages=settings.ocr_languages,
            ocr_minimum_confidence=settings.ocr_minimum_confidence,
            ocr_render_dpi=settings.ocr_render_dpi,
        )
        ocr_version = ocr_engine.engine_version
    handler = CompilationJobHandler(
        object_store=object_store,
        persistence=persistence,
        embedding_provider=embedding,
        embedding_model_version=settings.embedding_model,
        embedding_batch_size=settings.embedding_batch_size,
        embedding_dimensions=settings.embedding_dimensions,
        tenant_id=settings.tenant_id,
        knowledge_base_id=settings.knowledge_base_id,
        compiler=compiler,
        ocr_version=ocr_version,
    )
    configure_telemetry(
        service_name="bauer-evidence-v3-worker",
        service_version=SERVICE_VERSION,
        environment=settings.environment,
    )
    return CompilerWorker(
        queue=queue,
        handlers={"compile": handler},
        worker_id=settings.worker_id,
        lease_seconds=settings.job_lease_seconds,
        poll_seconds=settings.worker_poll_seconds,
    )


def _scoped_connection_provider(settings: Settings):
    @contextmanager
    def connect():
        try:
            import psycopg
        except ModuleNotFoundError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("PostgreSQL worker runtime requires psycopg 3") from exc
        with psycopg.connect(
            settings.database_url,
            autocommit=True,
            application_name="bauer-evidence-v3-worker",
        ) as connection:
            if settings.production:
                verify_runtime_database_role(
                    connection,
                    required_group_role="bauer_rag_v3_ingester",
                )
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (settings.tenant_id,),
            )
            connection.execute(
                "SELECT set_config('app.knowledge_base_id', %s, false)",
                (settings.knowledge_base_id,),
            )
            connection.execute(
                "SELECT set_config('app.principal_ids', %s, false)",
                (
                    json.dumps(
                        settings.principal_ids,
                        separators=(",", ":"),
                    ),
                ),
            )
            yield connection

    return connect


def _s3_client(
    *,
    endpoint_url: str,
    region_name: str,
    access_key_id: str,
    secret_access_key: str,
):
    if bool(access_key_id) != bool(secret_access_key):
        raise ConfigurationError(
            "S3 access key ID and secret access key must be configured together"
        )
    try:
        import boto3
    except ModuleNotFoundError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("S3 object storage requires boto3") from exc
    options: dict[str, str] = {}
    if endpoint_url:
        options["endpoint_url"] = endpoint_url
    if region_name:
        options["region_name"] = region_name
    if access_key_id:
        options["aws_access_key_id"] = access_key_id
        options["aws_secret_access_key"] = secret_access_key
    return boto3.client("s3", **options)
