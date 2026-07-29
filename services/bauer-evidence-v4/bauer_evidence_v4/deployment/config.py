from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping


class V4ConfigurationError(RuntimeError):
    pass


def _json_strings(value: str | None, *, name: str) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise V4ConfigurationError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        raise V4ConfigurationError(f"{name} must be an array of strings")
    return tuple(sorted({item.strip() for item in parsed}))


def _keyring(value: str | None) -> Mapping[str, bytes]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not parsed:
        raise V4ConfigurationError("V4 authorization keyring must be an object")
    result: dict[str, bytes] = {}
    for key, secret in parsed.items():
        if not isinstance(key, str) or not isinstance(secret, str):
            raise V4ConfigurationError("V4 authorization keyring is malformed")
        encoded = secret.encode("utf-8")
        if len(encoded) < 32:
            raise V4ConfigurationError("V4 authorization keys require 32 bytes")
        result[key] = encoded
    return result


@dataclass(frozen=True, slots=True)
class V4Settings:
    database_url: str
    tenant_id: str
    knowledge_base_id: str
    principal_id: str
    candidate_release_id: str
    allowed_agent_ids: tuple[str, ...]
    auth_audience: str
    auth_keyring: Mapping[str, bytes]
    build_commit: str
    service_role: str
    worker_id: str
    worker_poll_seconds: float
    source_s3_bucket: str
    source_s3_endpoint_url: str
    source_s3_region: str
    source_s3_prefix: str
    source_s3_access_key_id: str
    source_s3_secret_access_key: str
    artifact_s3_prefix: str

    @classmethod
    def from_environment(
        cls,
        *,
        service_role: str,
    ) -> "V4Settings":
        principals = _json_strings(
            os.getenv("BAUER_V4_PRINCIPAL_IDS_JSON")
            or os.getenv("BAUER_V3_PRINCIPAL_IDS_JSON"),
            name="BAUER_V4_PRINCIPAL_IDS_JSON",
        )
        settings = cls(
            database_url=(
                os.getenv("BAUER_V4_DATABASE_URL")
                or os.getenv("BAUER_V3_DATABASE_URL", "")
            ).strip(),
            tenant_id=(
                os.getenv("BAUER_V4_TENANT_ID")
                or os.getenv("BAUER_V3_TENANT_ID", "")
            ).strip(),
            knowledge_base_id=(
                os.getenv("BAUER_V4_KB_ID")
                or os.getenv("BAUER_V3_KB_ID", "")
            ).strip(),
            principal_id=(
                os.getenv("BAUER_V4_PRINCIPAL_ID")
                or (principals[0] if principals else "")
            ).strip(),
            candidate_release_id=os.getenv(
                "BAUER_V4_CANDIDATE_RELEASE_ID", ""
            ).strip(),
            allowed_agent_ids=_json_strings(
                os.getenv("BAUER_V4_ALLOWED_AGENT_IDS_JSON"),
                name="BAUER_V4_ALLOWED_AGENT_IDS_JSON",
            ),
            auth_audience=os.getenv(
                "BAUER_V4_AUTH_AUDIENCE",
                "bauer-evidence-v4",
            ).strip(),
            auth_keyring=_keyring(
                os.getenv("BAUER_V4_AUTH_KEYRING_JSON")
                or os.getenv("BAUER_V3_AUTH_KEYRING_JSON")
            ),
            build_commit=os.getenv(
                "BAUER_V4_BUILD_COMMIT",
                os.getenv("BAUER_V3_BUILD_COMMIT", "unknown"),
            ).strip(),
            service_role=service_role,
            worker_id=os.getenv(
                "BAUER_V4_WORKER_ID",
                os.getenv("BAUER_V3_WORKER_ID", "bauer-v4-worker"),
            ).strip(),
            worker_poll_seconds=float(
                os.getenv("BAUER_V4_WORKER_POLL_SECONDS", "2")
            ),
            source_s3_bucket=os.getenv("BAUER_V3_S3_BUCKET", "").strip(),
            source_s3_endpoint_url=os.getenv(
                "BAUER_V3_S3_ENDPOINT_URL", ""
            ).strip(),
            source_s3_region=os.getenv("BAUER_V3_S3_REGION", "").strip(),
            source_s3_prefix=os.getenv(
                "BAUER_V3_S3_PREFIX", "bauer-rag-v3"
            ).strip("/"),
            source_s3_access_key_id=os.getenv(
                "BAUER_V3_S3_ACCESS_KEY_ID", ""
            ).strip(),
            source_s3_secret_access_key=os.getenv(
                "BAUER_V3_S3_SECRET_ACCESS_KEY", ""
            ).strip(),
            artifact_s3_prefix=os.getenv(
                "BAUER_V4_S3_PREFIX", "v4"
            ).strip("/"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        required = {
            "database_url": self.database_url,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "candidate_release_id": self.candidate_release_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise V4ConfigurationError(
                "missing V4 deployment settings: " + ", ".join(missing)
            )
        if self.service_role == "api":
            if not self.principal_id or not self.allowed_agent_ids:
                raise V4ConfigurationError(
                    "V4 API requires a principal and private Agent allow-list"
                )
            if not self.auth_keyring:
                raise V4ConfigurationError(
                    "V4 API requires an authorization keyring"
                )
        if self.service_role == "worker":
            if not self.worker_id or not self.source_s3_bucket:
                raise V4ConfigurationError(
                    "V4 worker requires identity and the reused source bucket"
                )
            if not self.artifact_s3_prefix.startswith("v4"):
                raise V4ConfigurationError(
                    "V4 artifact prefix must remain under v4/"
                )
