from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class ConfigurationError(RuntimeError):
    pass


_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILD_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean environment value: {value!r}")


def _positive_int(value: str | None, *, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ConfigurationError(f"{name} must be positive")
    return parsed


def _bounded_float(
    value: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return parsed


def _keyring(value: str | None) -> dict[str, bytes]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("BAUER_V3_AUTH_KEYRING_JSON must contain valid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ConfigurationError("BAUER_V3_AUTH_KEYRING_JSON must be a non-empty object")
    result: dict[str, bytes] = {}
    for key_id, secret in parsed.items():
        if not isinstance(key_id, str) or not key_id.strip():
            raise ConfigurationError("authorization key IDs must be non-empty strings")
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
            raise ConfigurationError("authorization signing keys must contain at least 32 bytes")
        result[key_id.strip()] = secret.encode("utf-8")
    return result


def _string_tuple(value: str | None, *, name: str) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must contain valid JSON") from exc
    if not isinstance(parsed, list):
        raise ConfigurationError(f"{name} must be a JSON array")
    normalized: set[str] = set()
    for item in parsed:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{name} items must be non-empty strings")
        normalized.add(item.strip())
    return tuple(sorted(normalized))


def _uuid(value: str, *, name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ConfigurationError(f"{name} must be a UUID") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    service_role: str
    database_url: str
    auth_audience: str
    auth_keyring: Mapping[str, bytes] = field(repr=False)
    tenant_id: str = ""
    knowledge_base_id: str = ""
    principal_ids: tuple[str, ...] = ()
    allowed_agent_ids: tuple[str, ...] = ()
    candidate_release_id: str = ""
    model_base_url: str = ""
    model_api_key: str = field(default="", repr=False)
    model_name: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = ""
    embedding_dimensions: int = 1_024
    embedding_batch_size: int = 32
    ocr_enabled: bool = False
    ocr_detection_model: Path | None = None
    ocr_detection_model_sha256: str = ""
    ocr_recognition_model: Path | None = None
    ocr_recognition_model_sha256: str = ""
    ocr_classification_model: Path | None = None
    ocr_classification_model_sha256: str = ""
    ocr_languages: tuple[str, ...] = ("de", "en")
    ocr_minimum_confidence: float = 0.80
    ocr_render_dpi: int = 150
    object_store_backend: str = "local"
    object_store_root: Path = Path("./data/v3-objects")
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = ""
    s3_prefix: str = "bauer-rag-v3"
    s3_access_key_id: str = field(default="", repr=False)
    s3_secret_access_key: str = field(default="", repr=False)
    mirror_s3_bucket: str = ""
    mirror_s3_endpoint_url: str = ""
    mirror_s3_region: str = ""
    mirror_s3_access_key_id: str = field(default="", repr=False)
    mirror_s3_secret_access_key: str = field(default="", repr=False)
    allow_in_memory: bool = False
    expected_migration_version: int = 16
    build_commit: str = "unknown"
    port: int = 8000
    worker_id: str = ""
    worker_poll_seconds: int = 2
    job_lease_seconds: int = 120

    @property
    def production(self) -> bool:
        return self.environment.strip().casefold() == "production"

    def validate(self) -> "Settings":
        if self.environment.strip().casefold() not in _ENVIRONMENTS:
            raise ConfigurationError(
                "BAUER_V3_ENVIRONMENT must be development, test, staging, or production"
            )
        if self.service_role not in {"api", "worker", "migrate"}:
            raise ConfigurationError("BAUER_V3_SERVICE_ROLE must be api, worker, or migrate")
        if (
            self.build_commit != "unknown"
            and not _BUILD_COMMIT_RE.fullmatch(self.build_commit)
        ):
            raise ConfigurationError(
                "BAUER_V3_BUILD_COMMIT must be a lowercase hexadecimal Git commit"
            )
        if self.candidate_release_id:
            _uuid(
                self.candidate_release_id,
                name="BAUER_V3_CANDIDATE_RELEASE_ID",
            )
            if self.service_role != "api":
                raise ConfigurationError(
                    "BAUER_V3_CANDIDATE_RELEASE_ID is valid only for the API"
                )
            if self.allow_in_memory:
                raise ConfigurationError(
                    "candidate-release mode requires durable PostgreSQL"
                )
        if self.service_role == "api":
            if not self.auth_audience:
                raise ConfigurationError("BAUER_V3_AUTH_AUDIENCE is required")
            if not self.auth_keyring:
                raise ConfigurationError("BAUER_V3_AUTH_KEYRING_JSON is required")
        if not self.allow_in_memory and not self.database_url:
            raise ConfigurationError(
                "BAUER_V3_DATABASE_URL is required unless in-memory development is explicit"
            )
        if self.production and self.allow_in_memory:
            raise ConfigurationError("in-memory mode is forbidden in production")
        if self.service_role in {"worker", "migrate"} and self.allow_in_memory:
            raise ConfigurationError(
                f"{self.service_role} requires durable PostgreSQL mode"
            )
        if self.service_role in {"api", "worker"} and not self.allow_in_memory:
            if not self.tenant_id or not self.knowledge_base_id:
                raise ConfigurationError(
                    "BAUER_V3_TENANT_ID and BAUER_V3_KB_ID are required"
                )
            _uuid(self.tenant_id, name="BAUER_V3_TENANT_ID")
            _uuid(self.knowledge_base_id, name="BAUER_V3_KB_ID")
            if not self.principal_ids:
                raise ConfigurationError(
                    "BAUER_V3_PRINCIPAL_IDS_JSON is required"
                )
            for principal_id in self.principal_ids:
                _uuid(principal_id, name="BAUER_V3_PRINCIPAL_IDS_JSON item")
            if self.embedding_dimensions != 1_024:
                raise ConfigurationError(
                    "BAUER_V3_EMBEDDING_DIMENSIONS must be 1024 for schema version 16"
                )
        if self.service_role == "api" and not self.allow_in_memory:
            if not self.allowed_agent_ids:
                raise ConfigurationError(
                    "BAUER_V3_ALLOWED_AGENT_IDS_JSON is required for the API"
                )
            missing_model = [
                name
                for name, value in (
                    ("BAUER_V3_MODEL_BASE_URL", self.model_base_url),
                    ("BAUER_V3_MODEL_API_KEY", self.model_api_key),
                    ("BAUER_V3_MODEL_NAME", self.model_name),
                )
                if not value
            ]
            if missing_model:
                raise ConfigurationError(
                    f"API model configuration is incomplete: {', '.join(missing_model)}"
                )
            missing_embedding = [
                name
                for name, value in (
                    ("BAUER_V3_EMBEDDING_BASE_URL", self.embedding_base_url),
                    ("BAUER_V3_EMBEDDING_API_KEY", self.embedding_api_key),
                    ("BAUER_V3_EMBEDDING_MODEL", self.embedding_model),
                )
                if not value
            ]
            if missing_embedding:
                raise ConfigurationError(
                    "API embedding configuration is incomplete: "
                    f"{', '.join(missing_embedding)}"
                )
        if self.service_role == "worker" and not self.allow_in_memory:
            if not self.worker_id:
                raise ConfigurationError("BAUER_V3_WORKER_ID is required for the worker")
            missing_embedding = [
                name
                for name, value in (
                    ("BAUER_V3_EMBEDDING_BASE_URL", self.embedding_base_url),
                    ("BAUER_V3_EMBEDDING_API_KEY", self.embedding_api_key),
                    ("BAUER_V3_EMBEDDING_MODEL", self.embedding_model),
                )
                if not value
            ]
            if missing_embedding:
                raise ConfigurationError(
                    "worker embedding configuration is incomplete: "
                    f"{', '.join(missing_embedding)}"
                )
            if self.job_lease_seconds < 3:
                raise ConfigurationError(
                    "BAUER_V3_JOB_LEASE_SECONDS must be at least 3"
                )
            if self.embedding_batch_size > 128:
                raise ConfigurationError(
                    "BAUER_V3_EMBEDDING_BATCH_SIZE must not exceed 128"
                )
            if self.production and not self.ocr_enabled:
                raise ConfigurationError(
                    "production workers require BAUER_V3_OCR_ENABLED=true"
                )
            if self.ocr_enabled:
                missing_ocr = [
                    name
                    for name, value in (
                        (
                            "BAUER_V3_OCR_DETECTION_MODEL",
                            self.ocr_detection_model,
                        ),
                        (
                            "BAUER_V3_OCR_DETECTION_MODEL_SHA256",
                            self.ocr_detection_model_sha256,
                        ),
                        (
                            "BAUER_V3_OCR_RECOGNITION_MODEL",
                            self.ocr_recognition_model,
                        ),
                        (
                            "BAUER_V3_OCR_RECOGNITION_MODEL_SHA256",
                            self.ocr_recognition_model_sha256,
                        ),
                        (
                            "BAUER_V3_OCR_CLASSIFICATION_MODEL",
                            self.ocr_classification_model,
                        ),
                        (
                            "BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256",
                            self.ocr_classification_model_sha256,
                        ),
                    )
                    if not value
                ]
                if missing_ocr:
                    raise ConfigurationError(
                        "worker OCR configuration is incomplete: "
                        + ", ".join(missing_ocr)
                    )
                for name, value in (
                    (
                        "BAUER_V3_OCR_DETECTION_MODEL_SHA256",
                        self.ocr_detection_model_sha256,
                    ),
                    (
                        "BAUER_V3_OCR_RECOGNITION_MODEL_SHA256",
                        self.ocr_recognition_model_sha256,
                    ),
                    (
                        "BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256",
                        self.ocr_classification_model_sha256,
                    ),
                ):
                    if not _SHA256_RE.fullmatch(value):
                        raise ConfigurationError(
                            f"{name} must be 64 lowercase hexadecimal characters"
                        )
                if not self.ocr_languages:
                    raise ConfigurationError(
                        "BAUER_V3_OCR_LANGUAGES_JSON must not be empty"
                    )
                if not 0 <= self.ocr_minimum_confidence <= 1:
                    raise ConfigurationError(
                        "BAUER_V3_OCR_MINIMUM_CONFIDENCE must be between 0 and 1"
                    )
                if not 72 <= self.ocr_render_dpi <= 300:
                    raise ConfigurationError(
                        "BAUER_V3_OCR_RENDER_DPI must be between 72 and 300"
                    )
        if self.object_store_backend not in {"local", "s3"}:
            raise ConfigurationError("BAUER_V3_OBJECT_STORE_BACKEND must be local or s3")
        if (
            self.production
            and self.service_role == "worker"
            and self.object_store_backend != "s3"
        ):
            raise ConfigurationError("production requires the S3-compatible object-store adapter")
        if (
            self.service_role == "worker"
            and self.object_store_backend == "s3"
            and not self.s3_bucket
        ):
            raise ConfigurationError("BAUER_V3_S3_BUCKET is required for the S3 backend")
        if self.production and self.service_role == "worker":
            if not self.mirror_s3_bucket:
                raise ConfigurationError(
                    "production workers require BAUER_V3_MIRROR_S3_BUCKET"
                )
            if (
                self.mirror_s3_bucket == self.s3_bucket
                and self.mirror_s3_endpoint_url == self.s3_endpoint_url
            ):
                raise ConfigurationError(
                    "the production object mirror must be independent of the primary bucket"
                )
        return self

    def public_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "service_role": self.service_role,
            "database_configured": bool(self.database_url),
            "model_configured": bool(
                self.model_base_url and self.model_api_key and self.model_name
            ),
            "embedding_configured": bool(
                self.embedding_base_url
                and self.embedding_api_key
                and self.embedding_model
            ),
            "ocr_configured": bool(
                self.ocr_enabled
                and self.ocr_detection_model
                and self.ocr_recognition_model
                and self.ocr_classification_model
            ),
            "scope_configured": bool(
                self.tenant_id
                and self.knowledge_base_id
                and self.principal_ids
                and self.allowed_agent_ids
            ),
            "candidate_release_configured": bool(self.candidate_release_id),
            "object_store_backend": self.object_store_backend,
            "object_mirror_configured": bool(self.mirror_s3_bucket),
            "allow_in_memory": self.allow_in_memory,
            "expected_migration_version": self.expected_migration_version,
            "build_commit": self.build_commit,
        }

    @classmethod
    def from_environment(
        cls,
        *,
        service_role_override: str | None = None,
    ) -> "Settings":
        candidate_release_id = os.getenv(
            "BAUER_V3_CANDIDATE_RELEASE_ID",
            "",
        ).strip()
        settings = cls(
            environment=os.getenv(
                "BAUER_V3_ENVIRONMENT",
                "development",
            ).strip().casefold(),
            service_role=(
                service_role_override
                if service_role_override is not None
                else os.getenv("BAUER_V3_SERVICE_ROLE", "api")
            ).strip().casefold(),
            database_url=os.getenv("BAUER_V3_DATABASE_URL", "").strip(),
            auth_audience=os.getenv(
                "BAUER_V3_AUTH_AUDIENCE",
                "bauer-evidence-v3",
            ).strip(),
            auth_keyring=_keyring(os.getenv("BAUER_V3_AUTH_KEYRING_JSON")),
            tenant_id=os.getenv("BAUER_V3_TENANT_ID", "").strip(),
            knowledge_base_id=os.getenv("BAUER_V3_KB_ID", "").strip(),
            principal_ids=_string_tuple(
                os.getenv("BAUER_V3_PRINCIPAL_IDS_JSON"),
                name="BAUER_V3_PRINCIPAL_IDS_JSON",
            ),
            allowed_agent_ids=_string_tuple(
                os.getenv("BAUER_V3_ALLOWED_AGENT_IDS_JSON"),
                name="BAUER_V3_ALLOWED_AGENT_IDS_JSON",
            ),
            candidate_release_id=(
                _uuid(
                    candidate_release_id,
                    name="BAUER_V3_CANDIDATE_RELEASE_ID",
                )
                if candidate_release_id
                else ""
            ),
            model_base_url=os.getenv("BAUER_V3_MODEL_BASE_URL", "").strip(),
            model_api_key=os.getenv("BAUER_V3_MODEL_API_KEY", "").strip(),
            model_name=os.getenv("BAUER_V3_MODEL_NAME", "").strip(),
            embedding_base_url=os.getenv(
                "BAUER_V3_EMBEDDING_BASE_URL",
                os.getenv("BAUER_V3_MODEL_BASE_URL", ""),
            ).strip(),
            embedding_api_key=os.getenv(
                "BAUER_V3_EMBEDDING_API_KEY",
                os.getenv("BAUER_V3_MODEL_API_KEY", ""),
            ).strip(),
            embedding_model=os.getenv("BAUER_V3_EMBEDDING_MODEL", "").strip(),
            embedding_dimensions=_positive_int(
                os.getenv("BAUER_V3_EMBEDDING_DIMENSIONS"),
                default=1_024,
                name="BAUER_V3_EMBEDDING_DIMENSIONS",
            ),
            embedding_batch_size=_positive_int(
                os.getenv("BAUER_V3_EMBEDDING_BATCH_SIZE"),
                default=32,
                name="BAUER_V3_EMBEDDING_BATCH_SIZE",
            ),
            ocr_enabled=_boolean(os.getenv("BAUER_V3_OCR_ENABLED")),
            ocr_detection_model=(
                Path(value).resolve()
                if (
                    value := os.getenv(
                        "BAUER_V3_OCR_DETECTION_MODEL",
                        "",
                    ).strip()
                )
                else None
            ),
            ocr_detection_model_sha256=os.getenv(
                "BAUER_V3_OCR_DETECTION_MODEL_SHA256",
                "",
            ).strip(),
            ocr_recognition_model=(
                Path(value).resolve()
                if (
                    value := os.getenv(
                        "BAUER_V3_OCR_RECOGNITION_MODEL",
                        "",
                    ).strip()
                )
                else None
            ),
            ocr_recognition_model_sha256=os.getenv(
                "BAUER_V3_OCR_RECOGNITION_MODEL_SHA256",
                "",
            ).strip(),
            ocr_classification_model=(
                Path(value).resolve()
                if (
                    value := os.getenv(
                        "BAUER_V3_OCR_CLASSIFICATION_MODEL",
                        "",
                    ).strip()
                )
                else None
            ),
            ocr_classification_model_sha256=os.getenv(
                "BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256",
                "",
            ).strip(),
            ocr_languages=(
                _string_tuple(
                    os.getenv("BAUER_V3_OCR_LANGUAGES_JSON"),
                    name="BAUER_V3_OCR_LANGUAGES_JSON",
                )
                or ("de", "en")
            ),
            ocr_minimum_confidence=_bounded_float(
                os.getenv("BAUER_V3_OCR_MINIMUM_CONFIDENCE"),
                default=0.80,
                minimum=0,
                maximum=1,
                name="BAUER_V3_OCR_MINIMUM_CONFIDENCE",
            ),
            ocr_render_dpi=_positive_int(
                os.getenv("BAUER_V3_OCR_RENDER_DPI"),
                default=150,
                name="BAUER_V3_OCR_RENDER_DPI",
            ),
            object_store_backend=os.getenv(
                "BAUER_V3_OBJECT_STORE_BACKEND",
                "local",
            ).strip().casefold(),
            object_store_root=Path(
                os.getenv("BAUER_V3_OBJECT_STORE_ROOT", "./data/v3-objects")
            ),
            s3_bucket=os.getenv("BAUER_V3_S3_BUCKET", "").strip(),
            s3_endpoint_url=os.getenv("BAUER_V3_S3_ENDPOINT_URL", "").strip(),
            s3_region=os.getenv("BAUER_V3_S3_REGION", "").strip(),
            s3_prefix=os.getenv("BAUER_V3_S3_PREFIX", "bauer-rag-v3").strip(),
            s3_access_key_id=os.getenv("BAUER_V3_S3_ACCESS_KEY_ID", "").strip(),
            s3_secret_access_key=os.getenv(
                "BAUER_V3_S3_SECRET_ACCESS_KEY",
                "",
            ).strip(),
            mirror_s3_bucket=os.getenv("BAUER_V3_MIRROR_S3_BUCKET", "").strip(),
            mirror_s3_endpoint_url=os.getenv(
                "BAUER_V3_MIRROR_S3_ENDPOINT_URL",
                "",
            ).strip(),
            mirror_s3_region=os.getenv("BAUER_V3_MIRROR_S3_REGION", "").strip(),
            mirror_s3_access_key_id=os.getenv(
                "BAUER_V3_MIRROR_S3_ACCESS_KEY_ID",
                "",
            ).strip(),
            mirror_s3_secret_access_key=os.getenv(
                "BAUER_V3_MIRROR_S3_SECRET_ACCESS_KEY",
                "",
            ).strip(),
            allow_in_memory=_boolean(os.getenv("BAUER_V3_ALLOW_IN_MEMORY")),
            expected_migration_version=_positive_int(
                os.getenv("BAUER_V3_EXPECTED_MIGRATION_VERSION"),
                default=16,
                name="BAUER_V3_EXPECTED_MIGRATION_VERSION",
            ),
            build_commit=(
                os.getenv("BAUER_V3_BUILD_COMMIT")
                or os.getenv("RAILWAY_GIT_COMMIT_SHA")
                or "unknown"
            ).strip().casefold(),
            port=_positive_int(os.getenv("PORT"), default=8000, name="PORT"),
            worker_id=os.getenv("BAUER_V3_WORKER_ID", "").strip(),
            worker_poll_seconds=_positive_int(
                os.getenv("BAUER_V3_WORKER_POLL_SECONDS"),
                default=2,
                name="BAUER_V3_WORKER_POLL_SECONDS",
            ),
            job_lease_seconds=_positive_int(
                os.getenv("BAUER_V3_JOB_LEASE_SECONDS"),
                default=120,
                name="BAUER_V3_JOB_LEASE_SECONDS",
            ),
        )
        return settings.validate()
