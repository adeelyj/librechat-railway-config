from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from bauer_evidence_v3.answering import AnswerService  # noqa: E402
from bauer_evidence_v3.api import create_app  # noqa: E402
from bauer_evidence_v3.auth import (  # noqa: E402
    AuthorizationContext,
    sign_authorization_context,
)
from bauer_evidence_v3.config import ConfigurationError, Settings  # noqa: E402
from bauer_evidence_v3.generation import SequenceModelGateway  # noqa: E402
from bauer_evidence_v3.models import ArtifactStatus, EvidenceItem, SourceCoordinate  # noqa: E402
from bauer_evidence_v3.releases import (  # noqa: E402
    InMemoryReleaseRegistry,
    ReleaseGateReport,
    ReleaseSource,
)
from bauer_evidence_v3.retrieval import InMemoryEvidenceIndex, SearchUnit  # noqa: E402


KEYRING = {"current": b"bauer-v3-api-test-key-material-00001"}


@contextmanager
def environment(values: dict[str, str]):
    with patch.dict(os.environ, values, clear=True):
        yield


def durable_api_environment(**overrides: str) -> dict[str, str]:
    values = {
        "BAUER_V3_ENVIRONMENT": "staging",
        "BAUER_V3_SERVICE_ROLE": "api",
        "BAUER_V3_DATABASE_URL": "postgresql://unused",
        "BAUER_V3_AUTH_KEYRING_JSON": (
            '{"current":"abcdefghijklmnopqrstuvwxyz123456"}'
        ),
        "BAUER_V3_TENANT_ID": "10000000-0000-0000-0000-000000000001",
        "BAUER_V3_KB_ID": "20000000-0000-0000-0000-000000000001",
        "BAUER_V3_PRINCIPAL_IDS_JSON": (
            '["30000000-0000-0000-0000-000000000001"]'
        ),
        "BAUER_V3_ALLOWED_AGENT_IDS_JSON": '["bauer-v3-shadow"]',
        "BAUER_V3_MODEL_BASE_URL": "https://models.invalid/v1",
        "BAUER_V3_MODEL_API_KEY": "unused",
        "BAUER_V3_MODEL_NAME": "answer-test",
        "BAUER_V3_EMBEDDING_BASE_URL": "https://models.invalid/v1",
        "BAUER_V3_EMBEDDING_API_KEY": "unused",
        "BAUER_V3_EMBEDDING_MODEL": "embedding-test",
    }
    values.update(overrides)
    return values


class ConfigurationTests(unittest.TestCase):
    def test_production_rejects_in_memory_and_missing_s3(self) -> None:
        with environment(
            {
                "BAUER_V3_ENVIRONMENT": "production",
                "BAUER_V3_SERVICE_ROLE": "api",
                "BAUER_V3_AUTH_KEYRING_JSON": '{"current":"abcdefghijklmnopqrstuvwxyz123456"}',
                "BAUER_V3_ALLOW_IN_MEMORY": "true",
            }
        ):
            with self.assertRaisesRegex(ConfigurationError, "forbidden"):
                Settings.from_environment()

    def test_public_summary_never_contains_secrets(self) -> None:
        with environment(
            {
                "BAUER_V3_ENVIRONMENT": "development",
                "BAUER_V3_SERVICE_ROLE": "api",
                "BAUER_V3_AUTH_KEYRING_JSON": '{"current":"abcdefghijklmnopqrstuvwxyz123456"}',
                "BAUER_V3_ALLOW_IN_MEMORY": "true",
            }
        ):
            settings = Settings.from_environment()
        summary = str(settings.public_summary())
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", summary)
        self.assertNotIn("auth_keyring", summary)
        self.assertEqual(settings.expected_migration_version, 12)
        self.assertEqual(settings.build_commit, "unknown")

    def test_build_commit_is_validated_and_railway_metadata_is_supported(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        with environment(
            durable_api_environment(RAILWAY_GIT_COMMIT_SHA=commit)
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.build_commit, commit)
        self.assertEqual(settings.public_summary()["build_commit"], commit)

        with environment(
            durable_api_environment(BAUER_V3_BUILD_COMMIT="not-a-commit")
        ):
            with self.assertRaisesRegex(ConfigurationError, "Git commit"):
                Settings.from_environment()

    def test_durable_runtime_rejects_dimension_that_schema_cannot_store(self) -> None:
        with environment(
            {
                "BAUER_V3_SERVICE_ROLE": "worker",
                "BAUER_V3_DATABASE_URL": "postgresql://unused",
                "BAUER_V3_TENANT_ID": "10000000-0000-0000-0000-000000000001",
                "BAUER_V3_KB_ID": "20000000-0000-0000-0000-000000000001",
                "BAUER_V3_PRINCIPAL_IDS_JSON": '["30000000-0000-0000-0000-000000000001"]',
                "BAUER_V3_WORKER_ID": "test-worker",
                "BAUER_V3_EMBEDDING_BASE_URL": "https://models.invalid/v1",
                "BAUER_V3_EMBEDDING_API_KEY": "unused",
                "BAUER_V3_EMBEDDING_MODEL": "test",
                "BAUER_V3_EMBEDDING_DIMENSIONS": "768",
            }
        ):
            with self.assertRaisesRegex(
                ConfigurationError,
                "must be 1024 for schema version 12",
            ):
                Settings.from_environment()

    def test_worker_ocr_requires_pinned_models_and_production_enables_it(self) -> None:
        base = {
            "BAUER_V3_ENVIRONMENT": "staging",
            "BAUER_V3_SERVICE_ROLE": "worker",
            "BAUER_V3_DATABASE_URL": "postgresql://unused",
            "BAUER_V3_TENANT_ID": "10000000-0000-0000-0000-000000000001",
            "BAUER_V3_KB_ID": "20000000-0000-0000-0000-000000000001",
            "BAUER_V3_PRINCIPAL_IDS_JSON": (
                '["30000000-0000-0000-0000-000000000001"]'
            ),
            "BAUER_V3_WORKER_ID": "test-worker",
            "BAUER_V3_EMBEDDING_BASE_URL": "https://models.invalid/v1",
            "BAUER_V3_EMBEDDING_API_KEY": "unused",
            "BAUER_V3_EMBEDDING_MODEL": "test",
            "BAUER_V3_OCR_ENABLED": "true",
        }
        with environment(base):
            with self.assertRaisesRegex(
                ConfigurationError,
                "OCR configuration is incomplete",
            ):
                Settings.from_environment()

        pinned = {
            **base,
            "BAUER_V3_OCR_DETECTION_MODEL": "/models/detect.onnx",
            "BAUER_V3_OCR_DETECTION_MODEL_SHA256": "a" * 64,
            "BAUER_V3_OCR_RECOGNITION_MODEL": "/models/recognize.onnx",
            "BAUER_V3_OCR_RECOGNITION_MODEL_SHA256": "b" * 64,
            "BAUER_V3_OCR_CLASSIFICATION_MODEL": "/models/classify.onnx",
            "BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256": "c" * 64,
            "BAUER_V3_OCR_LANGUAGES_JSON": '["de","en"]',
            "BAUER_V3_OCR_MINIMUM_CONFIDENCE": "0.85",
            "BAUER_V3_OCR_RENDER_DPI": "180",
        }
        with environment(pinned):
            loaded = Settings.from_environment()
        self.assertTrue(loaded.ocr_enabled)
        self.assertEqual(loaded.ocr_minimum_confidence, 0.85)
        self.assertEqual(loaded.ocr_render_dpi, 180)
        self.assertTrue(loaded.public_summary()["ocr_configured"])

        production = {
            **base,
            "BAUER_V3_ENVIRONMENT": "production",
            "BAUER_V3_OCR_ENABLED": "false",
            "BAUER_V3_OBJECT_STORE_BACKEND": "s3",
            "BAUER_V3_S3_BUCKET": "primary",
            "BAUER_V3_S3_ENDPOINT_URL": "https://primary.invalid",
            "BAUER_V3_MIRROR_S3_BUCKET": "mirror",
            "BAUER_V3_MIRROR_S3_ENDPOINT_URL": "https://mirror.invalid",
        }
        with environment(production):
            with self.assertRaisesRegex(
                ConfigurationError,
                "production workers require BAUER_V3_OCR_ENABLED=true",
            ):
                Settings.from_environment()

    def test_environment_is_closed_to_known_values(self) -> None:
        for invalid in ("prod", "productionn", "local", ""):
            with self.subTest(invalid=invalid), environment(
                {
                    "BAUER_V3_ENVIRONMENT": invalid,
                    "BAUER_V3_SERVICE_ROLE": "api",
                    "BAUER_V3_AUTH_KEYRING_JSON": (
                        '{"current":"abcdefghijklmnopqrstuvwxyz123456"}'
                    ),
                    "BAUER_V3_ALLOW_IN_MEMORY": "true",
                }
            ):
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "must be development, test, staging, or production",
                ):
                    Settings.from_environment()

    def test_known_environment_is_normalized(self) -> None:
        with environment(
            {
                "BAUER_V3_ENVIRONMENT": " StAgInG ",
                "BAUER_V3_SERVICE_ROLE": "api",
                "BAUER_V3_AUTH_KEYRING_JSON": (
                    '{"current":"abcdefghijklmnopqrstuvwxyz123456"}'
                ),
                "BAUER_V3_ALLOW_IN_MEMORY": "true",
            }
        ):
            loaded = Settings.from_environment()
        self.assertEqual(loaded.environment, "staging")

    def test_candidate_release_is_uuid_validated_api_only_and_durable(self) -> None:
        candidate = "40000000-0000-4000-8000-000000000001"
        with environment(
            durable_api_environment(
                BAUER_V3_CANDIDATE_RELEASE_ID=candidate,
            )
        ):
            loaded = Settings.from_environment()
        self.assertEqual(loaded.candidate_release_id, candidate)
        self.assertTrue(loaded.public_summary()["candidate_release_configured"])
        self.assertNotIn(candidate, str(loaded.public_summary()))

        with environment(
            durable_api_environment(
                BAUER_V3_CANDIDATE_RELEASE_ID="not-a-release-id",
            )
        ):
            with self.assertRaisesRegex(
                ConfigurationError,
                "BAUER_V3_CANDIDATE_RELEASE_ID must be a UUID",
            ):
                Settings.from_environment()

        worker_values = durable_api_environment(
            BAUER_V3_SERVICE_ROLE="worker",
            BAUER_V3_WORKER_ID="worker",
            BAUER_V3_CANDIDATE_RELEASE_ID=candidate,
        )
        worker_values.pop("BAUER_V3_ALLOWED_AGENT_IDS_JSON")
        with environment(worker_values):
            with self.assertRaisesRegex(ConfigurationError, "only for the API"):
                Settings.from_environment()

        with environment(
            {
                "BAUER_V3_ENVIRONMENT": "test",
                "BAUER_V3_SERVICE_ROLE": "api",
                "BAUER_V3_AUTH_KEYRING_JSON": (
                    '{"current":"abcdefghijklmnopqrstuvwxyz123456"}'
                ),
                "BAUER_V3_ALLOW_IN_MEMORY": "true",
                "BAUER_V3_CANDIDATE_RELEASE_ID": candidate,
            }
        ):
            with self.assertRaisesRegex(
                ConfigurationError,
                "requires durable PostgreSQL",
            ):
                Settings.from_environment()


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        releases = InMemoryReleaseRegistry()
        releases.create(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label="api-test",
            expected_source_count=1,
            compiler_fingerprint="test",
            versions={
                "compiler": "test",
                "parser": "test",
                "embedding": "test",
                "prompt": "test",
            },
            release_id="release",
        )
        releases.start_build("release")
        releases.record_source(
            "release",
            ReleaseSource("source", "version", "artifact", ArtifactStatus.VALID),
        )
        releases.begin_validation("release")
        releases.mark_ready(
            "release",
            ReleaseGateReport(
                manifest_sha256=releases.computed_manifest_sha256("release"),
                source_complete=True,
                citations_resolvable=True,
                projections_complete=True,
            ),
        )
        releases.activate("release", actor="test", reason="API fixture")
        evidence = EvidenceItem(
            evidence_id="evidence",
            release_id="release",
            tenant_id="tenant",
            knowledge_base_id="kb",
            source_document_id="source",
            source_version_id="version",
            source_sha256="ab" * 32,
            source_type="public_document",
            title="Technical data",
            content="BM 40 has a maximum pressure of 350 bar.",
            coordinate=SourceCoordinate(page_number=4),
        )
        index = InMemoryEvidenceIndex()
        index.add(
            SearchUnit(
                search_unit_id="unit",
                evidence=evidence,
                unit_type="table_row",
                search_text=evidence.content,
                exact_terms=("BM 40",),
            )
        )
        from bauer_evidence_v3.ids import sha256_json

        citation = f"E-{sha256_json('evidence')[:12].upper()}"
        model = SequenceModelGateway(
            [f"BM 40 has a maximum pressure of 350 bar [{citation}]."]
        )
        service = AnswerService(releases=releases, index=index, model=model)
        self.service = service
        self.authorization_audit = []
        app = create_app(
            answer_service=service,
            auth_keyring=KEYRING,
            auth_audience="bauer-evidence-v3",
            authorization_audit_sink=self.authorization_audit.append,
            build_commit="0123456789abcdef0123456789abcdef01234567",
        )
        self.client = TestClient(app)
        now = int(datetime.now(UTC).timestamp())
        context = AuthorizationContext(
            tenant_id="tenant",
            knowledge_base_id="kb",
            user_id="user",
            agent_id="agent",
            audience="bauer-evidence-v3",
            issued_at=now,
            expires_at=now + 120,
            authorized_source_ids=("source",),
        )
        self.context = context
        self.token = sign_authorization_context(
            context,
            key_id="current",
            keyring=KEYRING,
        )

    def test_health_is_public_but_answer_fails_closed_without_context(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.json()["build_commit"],
            "0123456789abcdef0123456789abcdef01234567",
        )
        self.assertEqual(self.client.get("/version").json(), {
            "service": "bauer-evidence-v3",
            "version": "3.0.0-dev",
            "build_commit": "0123456789abcdef0123456789abcdef01234567",
        })
        denied = self.client.post("/v3/answer", json={"query": "BM 40 pressure"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            self.authorization_audit[-1].reason_code,
            "missing_bearer_context",
        )

    def test_answer_returns_validated_final_text_and_stable_evidence(self) -> None:
        response = self.client.post(
            "/v3/answer",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"query": "What is the maximum pressure for BM 40?"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "answered")
        self.assertTrue(body["validation"]["valid"])
        self.assertEqual(body["release_id"], "release")
        self.assertEqual(body["evidence"][0]["source_version_id"], "version")
        self.assertIsNone(body["evidence"][0]["external_file_id"])
        self.assertIn("x-request-id", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.authorization_audit[-1].outcome, "allowed")
        self.assertEqual(
            self.authorization_audit[-1].authorized_source_count,
            1,
        )

    def test_query_endpoint_is_explicitly_retrieval_only(self) -> None:
        delegate = self.service.releases

        class CountingRegistry:
            calls = 0

            def pin_active(self, knowledge_base_id):
                self.calls += 1
                return delegate.pin_active(knowledge_base_id)

        counting = CountingRegistry()
        self.service.releases = counting
        response = self.client.post(
            "/v3/query",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"query": "BM 40"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["answer_validation_applied"])
        result = body["results"][0]
        self.assertEqual(result["tenant_id"], "tenant")
        self.assertEqual(result["knowledge_base_id"], "kb")
        self.assertEqual(result["release_id"], "release")
        self.assertEqual(result["source_sha256"], "ab" * 32)
        self.assertEqual(result["source_type"], "public_document")
        self.assertEqual(result["coordinate"]["page_number"], 4)
        self.assertIsNone(result["external_file_id"])
        self.assertTrue(result["is_citable"])
        self.assertFalse(result["generated_summary"])
        self.assertEqual(counting.calls, 1)

    def test_unknown_body_fields_are_rejected(self) -> None:
        response = self.client.post(
            "/v3/answer",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"query": "BM 40", "debug": True},
        )
        self.assertEqual(response.status_code, 422)

    def test_request_cannot_select_a_candidate_release(self) -> None:
        response = self.client.post(
            "/v3/query",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "query": "BM 40",
                "release_id": "40000000-0000-4000-8000-000000000001",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_deployment_scope_guard_rejects_other_agent(self) -> None:
        guarded = TestClient(
            create_app(
                answer_service=self.service,
                auth_keyring=KEYRING,
                auth_audience="bauer-evidence-v3",
                context_guard=lambda context: (
                    context.tenant_id == "tenant"
                    and context.knowledge_base_id == "kb"
                    and context.agent_id == "bauer-v3-agent"
                ),
            )
        )
        response = guarded.post(
            "/v3/query",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"query": "BM 40"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"error": "authorization_scope_denied"},
        )


if __name__ == "__main__":
    unittest.main()
