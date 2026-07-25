from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.auth import AuthorizationContext  # noqa: E402
from bauer_evidence_v3.config import Settings  # noqa: E402
from bauer_evidence_v3.postgres_runtime import RuntimeReadiness  # noqa: E402
from bauer_evidence_v3.object_store import MemoryObjectStore  # noqa: E402
from bauer_evidence_v3.runtime import (  # noqa: E402
    _database_readiness,
    build_api_app,
    build_compiler_worker,
    deployment_context_guard,
)


class RuntimeOcr:
    engine_id = "runtime_ocr"
    engine_version = "runtime-ocr-v1"


class RuntimeRenderer:
    renderer_id = "runtime_renderer"
    renderer_version = "runtime-renderer-v1"


def settings(**overrides) -> Settings:
    values = {
        "environment": "development",
        "service_role": "api",
        "database_url": "",
        "auth_audience": "bauer-evidence-v3",
        "auth_keyring": {"key": b"x" * 32},
        "tenant_id": "tenant",
        "knowledge_base_id": "kb",
        "allowed_agent_ids": ("bauer",),
        "allow_in_memory": True,
    }
    values.update(overrides)
    return Settings(**values)


def context(**overrides) -> AuthorizationContext:
    values = {
        "tenant_id": "tenant",
        "knowledge_base_id": "kb",
        "user_id": "user",
        "agent_id": "bauer",
        "audience": "bauer-evidence-v3",
        "issued_at": 1,
        "expires_at": 2,
        "authorized_source_ids": ("source",),
    }
    values.update(overrides)
    return AuthorizationContext(**values)


class RuntimeTests(unittest.TestCase):
    def test_context_guard_binds_tenant_kb_and_agent(self) -> None:
        guard = deployment_context_guard(settings())
        self.assertTrue(guard(context()))
        self.assertFalse(guard(context(tenant_id="other")))
        self.assertFalse(guard(context(knowledge_base_id="other")))
        self.assertFalse(guard(context(agent_id="other")))

    def test_readiness_exposes_counts_without_exception_details(self) -> None:
        class Registry:
            def readiness(self, *, expected_version):
                self.expected_version = expected_version
                return RuntimeReadiness(
                    True,
                    9,
                    9,
                    9,
                    0,
                    1,
                    "fixed_candidate",
                )

        registry = Registry()
        result = _database_readiness(registry, expected_version=9)()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["active_release_count"], 0)
        self.assertEqual(result["selected_release_count"], 1)
        self.assertEqual(result["release_selection"], "fixed_candidate")
        self.assertEqual(result["mode"], "postgres_fixed_candidate")
        self.assertEqual(registry.expected_version, 9)

    def test_readiness_failure_is_safe_and_fail_closed(self) -> None:
        class Registry:
            def readiness(self, *, expected_version):
                raise RuntimeError("postgres://secret@database")

        result = _database_readiness(Registry(), expected_version=9)()
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["reason"], "database_unavailable")
        self.assertNotIn("secret", str(result))

    def test_api_runtime_uses_only_deployment_fixed_candidate_registry(self) -> None:
        candidate_id = "40000000-0000-4000-8000-000000000001"
        api_settings = settings(
            allow_in_memory=False,
            database_url="postgresql://database.invalid/v3",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            principal_ids=("30000000-0000-0000-0000-000000000001",),
            candidate_release_id=candidate_id,
            model_base_url="https://model.invalid/v1",
            model_api_key="secret",
            model_name="answer-v3",
            embedding_base_url="https://model.invalid/v1",
            embedding_api_key="secret",
            embedding_model="embedding-v3",
        )
        active_registry = Mock()
        candidate_registry = Mock()
        app = Mock()
        with (
            patch(
                "bauer_evidence_v3.runtime.PostgresReleaseRegistry",
                return_value=active_registry,
            ),
            patch(
                "bauer_evidence_v3.runtime.PostgresCandidateReleaseRegistry",
                return_value=candidate_registry,
            ) as candidate_type,
            patch("bauer_evidence_v3.runtime.PostgresEvidenceIndex"),
            patch("bauer_evidence_v3.runtime.OpenAICompatibleEmbeddingProvider"),
            patch("bauer_evidence_v3.runtime.OpenAICompatibleModelGateway"),
            patch(
                "bauer_evidence_v3.runtime.create_app",
                return_value=app,
            ) as create,
            patch("bauer_evidence_v3.runtime.configure_telemetry"),
        ):
            result = build_api_app(api_settings)

        self.assertIs(result, app)
        candidate_type.assert_called_once_with(
            active_registry,
            candidate_release_id=candidate_id,
        )
        self.assertIs(
            create.call_args.kwargs["answer_service"].releases,
            candidate_registry,
        )

    def test_api_runtime_without_candidate_uses_active_registry_directly(self) -> None:
        api_settings = settings(
            allow_in_memory=False,
            database_url="postgresql://database.invalid/v3",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            principal_ids=("30000000-0000-0000-0000-000000000001",),
            model_base_url="https://model.invalid/v1",
            model_api_key="secret",
            model_name="answer-v3",
            embedding_base_url="https://model.invalid/v1",
            embedding_api_key="secret",
            embedding_model="embedding-v3",
        )
        active_registry = Mock()
        with (
            patch(
                "bauer_evidence_v3.runtime.PostgresReleaseRegistry",
                return_value=active_registry,
            ),
            patch(
                "bauer_evidence_v3.runtime.PostgresCandidateReleaseRegistry",
            ) as candidate_type,
            patch("bauer_evidence_v3.runtime.PostgresEvidenceIndex"),
            patch("bauer_evidence_v3.runtime.OpenAICompatibleEmbeddingProvider"),
            patch("bauer_evidence_v3.runtime.OpenAICompatibleModelGateway"),
            patch(
                "bauer_evidence_v3.runtime.create_app",
                return_value=Mock(),
            ) as create,
            patch("bauer_evidence_v3.runtime.configure_telemetry"),
        ):
            build_api_app(api_settings)

        candidate_type.assert_not_called()
        self.assertIs(
            create.call_args.kwargs["answer_service"].releases,
            active_registry,
        )

    def test_worker_runtime_wires_durable_queue_compiler_and_scope(self) -> None:
        worker_settings = settings(
            service_role="worker",
            allow_in_memory=False,
            database_url="postgresql://database.invalid/v3",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            principal_ids=("30000000-0000-0000-0000-000000000001",),
            allowed_agent_ids=(),
            worker_id="worker-test",
            embedding_base_url="https://model.invalid/v1",
            embedding_api_key="secret",
            embedding_model="embedding-v3",
        )
        queue = Mock()
        persistence = Mock()
        embedding = Mock()
        embedding.dimensions = worker_settings.embedding_dimensions
        with (
            patch(
                "bauer_evidence_v3.runtime.build_object_store",
                return_value=MemoryObjectStore(),
            ),
            patch(
                "bauer_evidence_v3.runtime.PostgresJobQueue",
                return_value=queue,
            ),
            patch(
                "bauer_evidence_v3.runtime.PostgresCompilerPersistence",
                return_value=persistence,
            ) as persistence_type,
            patch(
                "bauer_evidence_v3.runtime.OpenAICompatibleEmbeddingProvider",
                return_value=embedding,
            ),
            patch("bauer_evidence_v3.runtime.configure_telemetry"),
        ):
            worker = build_compiler_worker(worker_settings)

        self.assertIs(worker.queue, queue)
        self.assertEqual(worker.worker_id, "worker-test")
        handler = worker.handlers["compile"]
        self.assertEqual(handler.tenant_id, worker_settings.tenant_id)
        self.assertEqual(
            handler.knowledge_base_id,
            worker_settings.knowledge_base_id,
        )
        persistence_type.assert_called_once()
        self.assertEqual(
            persistence_type.call_args.kwargs["principal_ids"],
            worker_settings.principal_ids,
        )

    def test_worker_runtime_wires_checksum_pinned_ocr_into_toolchain(self) -> None:
        worker_settings = settings(
            service_role="worker",
            allow_in_memory=False,
            database_url="postgresql://database.invalid/v3",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            principal_ids=("30000000-0000-0000-0000-000000000001",),
            allowed_agent_ids=(),
            worker_id="worker-test",
            embedding_base_url="https://model.invalid/v1",
            embedding_api_key="secret",
            embedding_model="embedding-v3",
            ocr_enabled=True,
            ocr_detection_model=Path("/models/detect.onnx"),
            ocr_detection_model_sha256="a" * 64,
            ocr_recognition_model=Path("/models/recognize.onnx"),
            ocr_recognition_model_sha256="b" * 64,
            ocr_classification_model=Path("/models/classify.onnx"),
            ocr_classification_model_sha256="c" * 64,
            ocr_languages=("de", "en"),
            ocr_minimum_confidence=0.85,
            ocr_render_dpi=180,
        )
        embedding = Mock()
        embedding.dimensions = worker_settings.embedding_dimensions
        ocr = RuntimeOcr()
        renderer = RuntimeRenderer()
        with (
            patch(
                "bauer_evidence_v3.runtime.build_object_store",
                return_value=MemoryObjectStore(),
            ),
            patch("bauer_evidence_v3.runtime.PostgresJobQueue"),
            patch("bauer_evidence_v3.runtime.PostgresCompilerPersistence"),
            patch(
                "bauer_evidence_v3.runtime.OpenAICompatibleEmbeddingProvider",
                return_value=embedding,
            ),
            patch(
                "bauer_evidence_v3.runtime.RapidOcrEngine",
                return_value=ocr,
            ) as ocr_type,
            patch(
                "bauer_evidence_v3.runtime.PdfPlumberPageRenderer",
                return_value=renderer,
            ),
            patch("bauer_evidence_v3.runtime.configure_telemetry"),
        ):
            worker = build_compiler_worker(worker_settings)

        handler = worker.handlers["compile"]
        self.assertIs(handler.compiler.ocr_engine, ocr)
        self.assertIs(handler.compiler.page_renderer, renderer)
        self.assertEqual(handler.ocr_version, ocr.engine_version)
        self.assertEqual(handler.compiler.ocr_languages, ("de", "en"))
        self.assertEqual(handler.compiler.ocr_minimum_confidence, 0.85)
        self.assertEqual(handler.compiler.ocr_render_dpi, 180)
        self.assertEqual(
            handler.toolchain.manifest["ocr"]["renderer"]["id"],
            renderer.renderer_id,
        )
        ocr_type.assert_called_once_with(
            detection_model=worker_settings.ocr_detection_model,
            detection_model_sha256=(
                worker_settings.ocr_detection_model_sha256
            ),
            recognition_model=worker_settings.ocr_recognition_model,
            recognition_model_sha256=(
                worker_settings.ocr_recognition_model_sha256
            ),
            classification_model=worker_settings.ocr_classification_model,
            classification_model_sha256=(
                worker_settings.ocr_classification_model_sha256
            ),
        )


if __name__ == "__main__":
    unittest.main()
