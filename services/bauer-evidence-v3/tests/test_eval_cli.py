from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3 import eval_cli  # noqa: E402
from bauer_evidence_v3.evaluation import EvaluationError  # noqa: E402
from bauer_evidence_v3.models import ReleaseStatus  # noqa: E402


TENANT_ID = "10000000-0000-4000-8000-000000000001"
KB_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_ID = "30000000-0000-4000-8000-000000000001"
SOURCE_ID = "40000000-0000-4000-8000-000000000001"
ADMIN_PRINCIPAL_ID = "50000000-0000-4000-8000-000000000001"
QUERY_PRINCIPAL_ID = "60000000-0000-4000-8000-000000000001"


def manifest():
    return SimpleNamespace(
        scope=SimpleNamespace(
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            allowed_release_ids=(RELEASE_ID,),
            allowed_source_ids=(SOURCE_ID,),
        )
    )


def structured_manifest():
    value = manifest()
    value.extraction_targets = (
        {
            "source_id": SOURCE_ID,
            "expected_page_count": 1,
            "facts": (),
        },
    )
    value.table_targets = ()
    value.cases = (object(),)
    return value


def canonical_only_manifest():
    value = structured_manifest()
    value.cases = ()
    return value


def direct_environment():
    return {
        "BAUER_V3_MODEL_BASE_URL": "https://model.invalid/v1",
        "BAUER_V3_MODEL_API_KEY": "model-secret",
        "BAUER_V3_MODEL_NAME": "answer-model",
        "BAUER_V3_EMBEDDING_BASE_URL": "https://embedding.invalid/v1",
        "BAUER_V3_EMBEDDING_API_KEY": "embedding-secret",
        "BAUER_V3_EMBEDDING_MODEL": "embedding-model",
        "BAUER_V3_EMBEDDING_DIMENSIONS": "1024",
        "BAUER_V3_EVAL_USER_ID": "bauer-v3-evaluation-user",
        "BAUER_V3_EVAL_AGENT_ID": "bauer-v3-direct-evaluator",
        "BAUER_V3_EVAL_AUDIENCE": "bauer-evidence-v3-evaluation",
    }


class EvalCliTests(unittest.TestCase):
    def test_direct_target_is_validating_scoped_and_least_privilege(self):
        calls = {}
        base_registry = object()
        validating_registry = SimpleNamespace()

        def release_registry_factory(database_url, **kwargs):
            calls["release_registry"] = (database_url, kwargs)
            return base_registry

        def validation_registry_factory(registry, **kwargs):
            self.assertIs(registry, base_registry)
            calls["validation_registry"] = kwargs

            def pin_active(knowledge_base_id):
                calls["pin_active"] = knowledge_base_id
                return SimpleNamespace(
                    release_id=RELEASE_ID,
                    tenant_id=TENANT_ID,
                    knowledge_base_id=KB_ID,
                    status=ReleaseStatus.VALIDATING,
                )

            validating_registry.pin_active = pin_active
            return validating_registry

        def embedding_provider_factory(**kwargs):
            calls["embedding"] = kwargs
            return "embedding"

        def evidence_index_factory(database_url, **kwargs):
            calls["index"] = (database_url, kwargs)
            return "index"

        def model_gateway_factory(**kwargs):
            calls["model"] = kwargs
            return "model"

        def answer_service_factory(**kwargs):
            calls["service"] = kwargs
            return "service"

        def target_factory(**kwargs):
            calls["target"] = kwargs
            return "target"

        target = eval_cli._build_direct_validating_target(
            manifest=manifest(),
            release_id=RELEASE_ID,
            query_database_url="postgresql://reader@database/v3",
            query_principal_ids=(QUERY_PRINCIPAL_ID,),
            timeout_seconds=42,
            environment=direct_environment(),
            release_registry_factory=release_registry_factory,
            validation_registry_factory=validation_registry_factory,
            evidence_index_factory=evidence_index_factory,
            embedding_provider_factory=embedding_provider_factory,
            model_gateway_factory=model_gateway_factory,
            answer_service_factory=answer_service_factory,
            target_factory=target_factory,
            clock=lambda: 1_000,
        )

        self.assertEqual(target, "target")
        self.assertEqual(
            calls["release_registry"],
            (
                "postgresql://reader@database/v3",
                {
                    "tenant_id": TENANT_ID,
                    "knowledge_base_id": KB_ID,
                    "principal_ids": (QUERY_PRINCIPAL_ID,),
                    "enforce_least_privilege": True,
                },
            ),
        )
        self.assertEqual(
            calls["validation_registry"],
            {"release_id": RELEASE_ID},
        )
        self.assertEqual(calls["pin_active"], KB_ID)
        index_database_url, index_kwargs = calls["index"]
        self.assertEqual(
            index_database_url,
            "postgresql://reader@database/v3",
        )
        self.assertEqual(
            index_kwargs["release_status"],
            ReleaseStatus.VALIDATING,
        )
        self.assertTrue(index_kwargs["enforce_least_privilege"])
        self.assertEqual(
            index_kwargs["principal_ids"],
            (QUERY_PRINCIPAL_ID,),
        )
        self.assertIs(calls["service"]["releases"], validating_registry)
        self.assertEqual(calls["target"]["answer_service"], "service")
        authorization = calls["target"]["authorization"]
        self.assertEqual(authorization.tenant_id, TENANT_ID)
        self.assertEqual(authorization.knowledge_base_id, KB_ID)
        self.assertEqual(
            authorization.authorized_source_ids,
            (SOURCE_ID,),
        )
        self.assertEqual(authorization.issued_at, 1_000)
        self.assertEqual(
            authorization.expires_at,
            1_000 + eval_cli.DIRECT_AUTHORIZATION_TTL_SECONDS,
        )
        self.assertEqual(calls["embedding"]["timeout_seconds"], 42)
        self.assertEqual(calls["model"]["timeout_seconds"], 42)

    def test_direct_target_refuses_non_validating_preflight(self):
        class ValidationRegistry:
            def pin_active(self, _knowledge_base_id):
                return SimpleNamespace(
                    release_id=RELEASE_ID,
                    tenant_id=TENANT_ID,
                    knowledge_base_id=KB_ID,
                    status=ReleaseStatus.READY,
                )

        with self.assertRaisesRegex(
            EvaluationError,
            "exact validating manifest scope",
        ):
            eval_cli._build_direct_validating_target(
                manifest=manifest(),
                release_id=RELEASE_ID,
                query_database_url="postgresql://reader@database/v3",
                query_principal_ids=(QUERY_PRINCIPAL_ID,),
                timeout_seconds=10,
                environment=direct_environment(),
                release_registry_factory=lambda *args, **kwargs: object(),
                validation_registry_factory=(
                    lambda *args, **kwargs: ValidationRegistry()
                ),
                embedding_provider_factory=lambda **kwargs: object(),
            )

    def test_direct_cli_requires_no_http_token(self):
        fake_store = Mock()
        fake_report = SimpleNamespace(
            spec=SimpleNamespace(
                eval_run_id="70000000-0000-4000-8000-000000000001",
                release_id=RELEASE_ID,
            ),
            passed=True,
            hard_failures=(),
            results=(),
        )
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": (
                "postgresql://admin@database/v3"
            ),
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_QUERY_DATABASE_URL": (
                "postgresql://reader@database/v3"
            ),
            "BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS": (
                f'["{QUERY_PRINCIPAL_ID}"]'
            ),
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=manifest(),
            ),
            patch.object(
                eval_cli,
                "_build_direct_validating_target",
                return_value="direct-target",
            ) as build_target,
            patch.object(
                eval_cli.PostgresEvaluationStore,
                "from_dsn",
                return_value=fake_store,
            ) as build_store,
            patch.object(
                eval_cli,
                "_execute",
                new=AsyncMock(return_value=fake_report),
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = eval_cli.main(
                [
                    "--manifest",
                    "development.yaml",
                    "--split",
                    "development",
                    "--release-id",
                    RELEASE_ID,
                    "--direct-validating",
                    "--code-version",
                    "commit-sha",
                ]
            )

        self.assertEqual(result, 0)
        build_store.assert_called_once_with(
            "postgresql://admin@database/v3",
            tenant_id=TENANT_ID,
            principal_ids=(ADMIN_PRINCIPAL_ID,),
        )
        fake_store.verify_database_role.assert_called_once_with()
        self.assertEqual(
            build_target.call_args.kwargs["query_database_url"],
            "postgresql://reader@database/v3",
        )
        self.assertEqual(
            build_target.call_args.kwargs["query_principal_ids"],
            (QUERY_PRINCIPAL_ID,),
        )

    def test_http_cli_requires_token_but_not_query_credentials(self):
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": (
                "postgresql://admin@database/v3"
            ),
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=manifest(),
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                eval_cli.main(
                    [
                        "--manifest",
                        "development.yaml",
                        "--split",
                        "development",
                        "--release-id",
                        RELEASE_ID,
                        "--api-url",
                        "https://private-v3.invalid",
                        "--code-version",
                        "commit-sha",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("authorization token is empty", stderr.getvalue())

    def test_http_cli_runs_without_direct_query_credentials(self):
        fake_store = Mock()
        fake_report = SimpleNamespace(
            spec=SimpleNamespace(
                eval_run_id="70000000-0000-4000-8000-000000000002",
                release_id=RELEASE_ID,
            ),
            passed=True,
            hard_failures=(),
            results=(),
        )
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": (
                "postgresql://admin@database/v3"
            ),
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_AUTHORIZATION_TOKEN": "signed-context",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=manifest(),
            ),
            patch.object(
                eval_cli.PostgresEvaluationStore,
                "from_dsn",
                return_value=fake_store,
            ),
            patch.object(
                eval_cli,
                "_execute",
                new=AsyncMock(return_value=fake_report),
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            result = eval_cli.main(
                [
                    "--manifest",
                    "development.yaml",
                    "--split",
                    "development",
                    "--release-id",
                    RELEASE_ID,
                    "--api-url",
                    "https://private-v3.invalid",
                    "--code-version",
                    "commit-sha",
                ]
            )

        self.assertEqual(result, 0)
        target = execute.call_args.kwargs["target"]
        self.assertIsInstance(target, eval_cli.HttpEvaluationTarget)

    def test_http_structured_capture_builds_separate_reader_source(self):
        fake_store = Mock()
        fake_source = object()
        fake_report = SimpleNamespace(
            spec=SimpleNamespace(
                eval_run_id="70000000-0000-4000-8000-000000000003",
                release_id=RELEASE_ID,
            ),
            passed=True,
            hard_failures=(),
            results=(),
        )
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": (
                "postgresql://admin@database/v3"
            ),
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_QUERY_DATABASE_URL": (
                "postgresql://reader@database/v3"
            ),
            "BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS": (
                f'["{QUERY_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_AUTHORIZATION_TOKEN": "signed-context",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=structured_manifest(),
            ),
            patch.object(
                eval_cli.PostgresEvaluationObservationSource,
                "from_dsn",
                return_value=fake_source,
            ) as build_source,
            patch.object(
                eval_cli.PostgresEvaluationStore,
                "from_dsn",
                return_value=fake_store,
            ),
            patch.object(
                eval_cli,
                "_execute",
                new=AsyncMock(return_value=fake_report),
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            result = eval_cli.main(
                [
                    "--manifest",
                    "development.yaml",
                    "--split",
                    "development",
                    "--release-id",
                    RELEASE_ID,
                    "--api-url",
                    "https://private-v3.invalid",
                    "--code-version",
                    "commit-sha",
                ]
            )

        self.assertEqual(result, 0)
        build_source.assert_called_once_with(
            "postgresql://reader@database/v3",
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(QUERY_PRINCIPAL_ID,),
            enforce_least_privilege=True,
            statement_timeout_ms=60_000,
        )
        self.assertIs(
            execute.call_args.kwargs["observation_source"],
            fake_source,
        )

    def test_http_structured_capture_requires_reader_credentials(self):
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": (
                "postgresql://admin@database/v3"
            ),
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_AUTHORIZATION_TOKEN": "signed-context",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=structured_manifest(),
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                eval_cli.main(
                    [
                        "--manifest",
                        "development.yaml",
                        "--split",
                        "development",
                        "--release-id",
                        RELEASE_ID,
                        "--api-url",
                        "https://private-v3.invalid",
                        "--code-version",
                        "commit-sha",
                    ]
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "BAUER_V3_EVAL_QUERY_DATABASE_URL",
            stderr.getvalue(),
        )

    def test_canonical_only_cli_needs_no_model_or_http_credentials(self):
        fake_store = Mock()
        fake_source = object()
        fake_report = SimpleNamespace(
            spec=SimpleNamespace(
                eval_run_id="70000000-0000-4000-8000-000000000004",
                release_id=RELEASE_ID,
            ),
            passed=True,
            hard_failures=(),
            results=(),
        )
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": (
                "postgresql://admin@database/v3"
            ),
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_QUERY_DATABASE_URL": (
                "postgresql://reader@database/v3"
            ),
            "BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS": (
                f'["{QUERY_PRINCIPAL_ID}"]'
            ),
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=canonical_only_manifest(),
            ),
            patch.object(
                eval_cli.PostgresEvaluationObservationSource,
                "from_dsn",
                return_value=fake_source,
            ),
            patch.object(
                eval_cli.PostgresEvaluationStore,
                "from_dsn",
                return_value=fake_store,
            ),
            patch.object(
                eval_cli,
                "_execute",
                new=AsyncMock(return_value=fake_report),
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            result = eval_cli.main(
                [
                    "--manifest",
                    "development.yaml",
                    "--split",
                    "development",
                    "--release-id",
                    RELEASE_ID,
                    "--canonical-only",
                    "--code-version",
                    "commit-sha",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIsNone(execute.call_args.kwargs["target"])
        self.assertIs(
            execute.call_args.kwargs["observation_source"],
            fake_source,
        )

    def test_target_modes_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                eval_cli.main(
                    [
                        "--manifest",
                        "development.yaml",
                        "--split",
                        "development",
                        "--release-id",
                        RELEASE_ID,
                        "--api-url",
                        "https://private-v3.invalid",
                        "--direct-validating",
                        "--code-version",
                        "commit-sha",
                    ]
                )
        self.assertEqual(raised.exception.code, 2)

    def test_direct_cli_rejects_admin_dsn_reuse(self):
        shared_dsn = "postgresql://unsafe-shared-role@database/v3"
        environment = {
            "BAUER_V3_EVAL_DATABASE_URL": shared_dsn,
            "BAUER_V3_EVAL_PRINCIPAL_IDS": (
                f'["{ADMIN_PRINCIPAL_ID}"]'
            ),
            "BAUER_V3_EVAL_QUERY_DATABASE_URL": shared_dsn,
            "BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS": (
                f'["{QUERY_PRINCIPAL_ID}"]'
            ),
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                eval_cli,
                "load_evaluation_manifest",
                return_value=manifest(),
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                eval_cli.main(
                    [
                        "--manifest",
                        "development.yaml",
                        "--split",
                        "development",
                        "--release-id",
                        RELEASE_ID,
                        "--direct-validating",
                        "--code-version",
                        "commit-sha",
                    ]
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("query DSN separate", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
