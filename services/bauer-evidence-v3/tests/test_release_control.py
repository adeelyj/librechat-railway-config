from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ids import sha256_bytes  # noqa: E402
from bauer_evidence_v3.object_store import MemoryObjectStore  # noqa: E402
from bauer_evidence_v3.postgres_admin import (  # noqa: E402
    ActivationResult,
    BootstrapResult,
    BuildStartResult,
)
from bauer_evidence_v3.release_control import (  # noqa: E402
    CONFIRM_ACTIVE_POINTER_CHANGE,
    ControlScope,
    DatabaseControlSettings,
    PostgresReleaseStatusReader,
    ReleaseControlError,
    ReleaseController,
    build_object_store_from_environment,
    create_manifest_from_spec,
    write_manifest_once,
)
from bauer_evidence_v3.release_control_cli import (  # noqa: E402
    CliDependencies,
    main,
)
from bauer_evidence_v3.source_manifest import (  # noqa: E402
    SourceManifestIntegrityError,
    SourceSpec,
    create_source_manifest,
)
from bauer_evidence_v3.toolchain import (  # noqa: E402
    CompilerToolchainIdentity,
)


TENANT_ID = "10000000-0000-0000-0000-000000000001"
KB_ID = "20000000-0000-0000-0000-000000000001"
ADMIN_ID = "30000000-0000-0000-0000-000000000001"
READER_ID = "30000000-0000-0000-0000-000000000002"
RELEASE_ID = "40000000-0000-0000-0000-000000000001"
TARGET_RELEASE_ID = "40000000-0000-0000-0000-000000000002"
JOB_ID = "50000000-0000-0000-0000-000000000001"
FINGERPRINT = "a" * 64


def toolchain(
    *,
    embedding_model: str = "bauer-embedding-v3",
    dimensions: int = 1024,
    fact_model: str | None = None,
) -> CompilerToolchainIdentity:
    return CompilerToolchainIdentity(
        fingerprint=FINGERPRINT,
        parser_version="bauer-v3-parsers-test",
        manifest_json=json.dumps(
            {
                "embedding": {
                    "model_version": embedding_model,
                    "dimensions": dimensions,
                },
                "ocr": None,
                "fact_model": (
                    {"version": fact_model}
                    if fact_model is not None
                    else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


class FakeAdmin:
    def __init__(self):
        self.calls = []

    def bootstrap_tenant_knowledge_base(self, **kwargs):
        self.calls.append(("bootstrap", kwargs))
        return BootstrapResult(
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            principal_ids=(ADMIN_ID,),
            grants=((ADMIN_ID, "admin"),),
        )

    def create_release(self, spec):
        self.calls.append(("create_release", spec))
        return {
            "release_id": spec.release_id,
            "status": "draft",
            "compiler_fingerprint": spec.compiler_fingerprint,
        }

    def start_build(self, release_id, *, compile_jobs=()):
        jobs = tuple(
            {
                "job_id": f"job-{index}",
                "source_version_id": value.source_version_id,
            }
            for index, value in enumerate(compile_jobs)
        )
        self.calls.append(("start_build", release_id, tuple(compile_jobs)))
        return BuildStartResult(
            release={"release_id": release_id, "status": "building"},
            jobs=jobs,
        )

    def begin_validation(self, release_id):
        self.calls.append(("begin_validation", release_id))
        return {"release_id": release_id, "status": "validating"}

    def mark_ready(self, release_id):
        self.calls.append(("mark_ready", release_id))
        return {"release_id": release_id, "status": "ready"}

    def mark_failed(self, release_id, *, error):
        self.calls.append(("mark_failed", release_id, error))
        return {"release_id": release_id, "status": "failed", "error": error}

    def replay_dead_letter(
        self,
        job_id,
        *,
        idempotency_key,
        actor_principal_id,
    ):
        self.calls.append(
            (
                "replay_dead_letter",
                job_id,
                idempotency_key,
                actor_principal_id,
            )
        )
        return {
            "job_id": TARGET_RELEASE_ID,
            "replay_of_job_id": job_id,
            "idempotency_key": idempotency_key,
        }

    def retire_release(self, release_id):
        self.calls.append(("retire", release_id))
        return {"release_id": release_id, "status": "retired"}

    def activate_release(self, **kwargs):
        self.calls.append(("activate", kwargs))
        return ActivationResult(None, kwargs["release_id"])

    def rollback_release(self, **kwargs):
        self.calls.append(("rollback", kwargs))
        return ActivationResult(RELEASE_ID, kwargs["target_release_id"])


class FakeStatusReader:
    def __init__(self):
        self.calls = []

    def inspect(self, *, kb_id, release_id=None):
        self.calls.append((kb_id, release_id))
        return {
            "release": {"release_id": release_id, "status": "ready"},
            "active_release_id": release_id,
            "is_active": True,
        }


class FakeCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, final_payload):
        self.final_payload = final_payload
        self.calls = []
        self.closed = False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "WITH selected AS" in normalized:
            return FakeCursor((self.final_payload,))
        return FakeCursor()

    def transaction(self):
        return nullcontext()

    def close(self):
        self.closed = True


class ReleaseControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "manual.pdf").write_bytes(
            b"%PDF-1.7\noriginal manual"
        )
        self.scope = ControlScope(
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(READER_ID, ADMIN_ID, ADMIN_ID),
        )
        self.admin = FakeAdmin()
        self.status = FakeStatusReader()
        self.controller = ReleaseController(
            scope=self.scope,
            admin=self.admin,
            status_reader=self.status,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self, *, tenant_id=TENANT_ID, kb_id=KB_ID):
        return create_source_manifest(
            self.root,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            release_id=RELEASE_ID,
            sources=(
                SourceSpec(
                    external_file_id="manual",
                    logical_path="manual.pdf",
                    source_type="pdf",
                    declared_media_type="application/pdf",
                ),
            ),
        )

    def test_build_verifies_stages_and_binds_derived_toolchain(self):
        store = MemoryObjectStore()
        result = self.controller.build_release(
            manifest=self.manifest(),
            source_root=self.root,
            object_store=store,
            toolchain=toolchain(),
            embedding_model_version="bauer-embedding-v3",
            created_by="release-operator",
            priority=7,
            max_attempts=3,
        )

        self.assertEqual(result.compile_job_count, 1)
        self.assertEqual(
            [call[0] for call in self.admin.calls],
            ["create_release", "start_build"],
        )
        release_spec = self.admin.calls[0][1]
        self.assertEqual(release_spec.compiler_fingerprint, FINGERPRINT)
        self.assertEqual(
            release_spec.manifest_sha256,
            self.manifest().manifest_sha256,
        )
        self.assertEqual(
            release_spec.metadata["toolchain"]["embedding"]["dimensions"],
            1024,
        )
        compile_job = self.admin.calls[1][2][0]
        self.assertEqual(compile_job.priority, 7)
        self.assertEqual(compile_job.max_attempts, 3)
        self.assertTrue(store.exists(compile_job.source_object_key))

    def test_build_rejects_scope_and_toolchain_drift_before_writes(self):
        store = MemoryObjectStore()
        other_tenant = "10000000-0000-0000-0000-000000000099"
        with self.assertRaisesRegex(ReleaseControlError, "tenant_id"):
            self.controller.build_release(
                manifest=self.manifest(tenant_id=other_tenant),
                source_root=self.root,
                object_store=store,
                toolchain=toolchain(),
                embedding_model_version="bauer-embedding-v3",
                created_by="operator",
            )
        with self.assertRaisesRegex(
            ReleaseControlError,
            "1024 embedding dimensions",
        ):
            self.controller.build_release(
                manifest=self.manifest(),
                source_root=self.root,
                object_store=store,
                toolchain=toolchain(dimensions=768),
                embedding_model_version="bauer-embedding-v3",
                created_by="operator",
            )
        self.assertEqual(store._objects, {})
        self.assertEqual(self.admin.calls, [])

    def test_mark_ready_delegates_to_existing_database_gates(self):
        ready = self.controller.mark_ready(RELEASE_ID)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(
            self.admin.calls,
            [("mark_ready", RELEASE_ID)],
        )

    def test_dead_letter_replay_requires_a_scoped_actor_and_new_key(self):
        replay = self.controller.replay_dead_letter(
            JOB_ID,
            idempotency_key="replay:compile:attempt-2",
            actor_principal_id=ADMIN_ID,
        )
        self.assertEqual(replay["replay_of_job_id"], JOB_ID)
        self.assertEqual(
            self.admin.calls,
            [
                (
                    "replay_dead_letter",
                    JOB_ID,
                    "replay:compile:attempt-2",
                    ADMIN_ID,
                )
            ],
        )
        with self.assertRaisesRegex(ReleaseControlError, "not in"):
            self.controller.replay_dead_letter(
                JOB_ID,
                idempotency_key="replay:unauthorized",
                actor_principal_id=(
                    "30000000-0000-0000-0000-000000000099"
                ),
            )

    def test_activation_and_rollback_require_exact_confirmation_actor_reason(self):
        with self.assertRaisesRegex(ReleaseControlError, "confirmation"):
            self.controller.activate(
                release_id=RELEASE_ID,
                actor_principal_id=ADMIN_ID,
                reason="promote verified release",
                confirmation="yes",
            )
        with self.assertRaisesRegex(ReleaseControlError, "not in"):
            self.controller.activate(
                release_id=RELEASE_ID,
                actor_principal_id="30000000-0000-0000-0000-000000000099",
                reason="promote verified release",
                confirmation=CONFIRM_ACTIVE_POINTER_CHANGE,
            )

        activated = self.controller.activate(
            release_id=RELEASE_ID,
            actor_principal_id=ADMIN_ID,
            reason="promote independently verified release",
            confirmation=CONFIRM_ACTIVE_POINTER_CHANGE,
        )
        rolled_back = self.controller.rollback(
            target_release_id=TARGET_RELEASE_ID,
            actor_principal_id=ADMIN_ID,
            reason="rollback after a production regression",
            confirmation=CONFIRM_ACTIVE_POINTER_CHANGE,
        )
        self.assertEqual(activated.active_release_id, RELEASE_ID)
        self.assertEqual(rolled_back.active_release_id, TARGET_RELEASE_ID)
        activate_kwargs = self.admin.calls[-2][1]
        rollback_kwargs = self.admin.calls[-1][1]
        self.assertTrue(activate_kwargs["production"])
        self.assertTrue(rollback_kwargs["production"])
        self.assertEqual(activate_kwargs["actor_principal_id"], ADMIN_ID)

    def test_bootstrap_is_strict_and_confined_to_configured_scope(self):
        result = self.controller.bootstrap(
            {
                "external_key": "bauer",
                "tenant_display_name": "Bauer",
                "external_namespace": "bauer",
                "kb_name": "Bauer Evidence",
                "principals": [
                    {
                        "principal_id": ADMIN_ID,
                        "principal_type": "service",
                        "external_subject": "bauer-v3-release-admin",
                    }
                ],
                "grants": [
                    {
                        "principal_id": ADMIN_ID,
                        "permission": "admin",
                        "granted_by": "bootstrap",
                    }
                ],
            }
        )
        self.assertEqual(result.kb_id, KB_ID)
        kwargs = self.admin.calls[0][1]
        self.assertEqual(kwargs["kb_id"], KB_ID)
        self.assertEqual(kwargs["principals"][0].principal_id, ADMIN_ID)

        with self.assertRaisesRegex(ReleaseControlError, "does not match"):
            self.controller.bootstrap(
                {
                    "external_key": "bauer",
                    "tenant_display_name": "Bauer",
                    "kb_id": "20000000-0000-0000-0000-000000000099",
                    "external_namespace": "bauer",
                    "kb_name": "Bauer",
                    "principals": [],
                    "grants": [],
                }
            )

    def test_status_reader_uses_rls_context_and_returns_operational_summary(self):
        payload = {
            "release": {
                "release_id": RELEASE_ID,
                "status": "ready",
            },
            "active_release_id": RELEASE_ID,
            "job_counts": {"succeeded": 1},
            "unresolved_blocker_count": 0,
            "latest_eval": {"passed": True},
        }
        connection = FakeConnection(payload)
        reader = PostgresReleaseStatusReader(
            lambda: connection,
            tenant_id=TENANT_ID,
            principal_ids=(ADMIN_ID,),
        )
        result = reader.inspect(kb_id=KB_ID, release_id=RELEASE_ID)

        self.assertTrue(result["is_active"])
        self.assertTrue(connection.closed)
        self.assertIn(
            "SELECT set_config('app.tenant_id', %s, true)",
            [sql for sql, _params in connection.calls],
        )
        status_sql, status_params = next(
            (sql, params)
            for sql, params in connection.calls
            if "WITH selected AS" in sql
        )
        self.assertNotIn("UPDATE ", status_sql)
        self.assertIn("SELECT job.state", status_sql)
        self.assertIn("GROUP BY job.state", status_sql)
        self.assertNotIn("job.status", status_sql)
        self.assertEqual(status_params[0], KB_ID)
        self.assertEqual(status_params[1], RELEASE_ID)

    def test_status_reader_can_enforce_exact_admin_tier(self):
        connection = FakeConnection(
            {
                "release": {
                    "release_id": RELEASE_ID,
                    "status": "ready",
                },
                "active_release_id": RELEASE_ID,
            }
        )
        reader = PostgresReleaseStatusReader(
            lambda: connection,
            tenant_id=TENANT_ID,
            principal_ids=(ADMIN_ID,),
            enforce_database_role=True,
        )
        with patch(
            "bauer_evidence_v3.release_control.verify_runtime_database_role",
            return_value="bauer_v3_admin_login",
        ) as verify:
            reader.inspect(kb_id=KB_ID, release_id=RELEASE_ID)
        verify.assert_called_once_with(
            connection,
            required_group_role="bauer_rag_v3_admin",
        )

    def test_database_settings_require_only_database_and_scope(self):
        settings = DatabaseControlSettings.from_environment(
            {
                "BAUER_V3_DATABASE_URL": "postgresql://release-control",
                "BAUER_V3_TENANT_ID": TENANT_ID,
                "BAUER_V3_KNOWLEDGE_BASE_ID": KB_ID,
                "BAUER_V3_PRINCIPAL_IDS_JSON": json.dumps(
                    [ADMIN_ID, READER_ID]
                ),
            }
        )
        self.assertEqual(settings.scope.tenant_id, TENANT_ID)
        self.assertEqual(
            settings.scope.principal_ids,
            (ADMIN_ID, READER_ID),
        )

    def test_production_local_object_store_is_rejected_before_creation(self):
        with self.assertRaisesRegex(
            ReleaseControlError,
            "mirrored S3",
        ):
            build_object_store_from_environment(
                {
                    "BAUER_V3_ENVIRONMENT": "production",
                    "BAUER_V3_OBJECT_STORE_BACKEND": "local",
                }
            )
        with self.assertRaisesRegex(
            ReleaseControlError,
            "BAUER_V3_MIRROR_S3_BUCKET",
        ):
            build_object_store_from_environment(
                {
                    "BAUER_V3_ENVIRONMENT": "production",
                    "BAUER_V3_OBJECT_STORE_BACKEND": "s3",
                    "BAUER_V3_S3_BUCKET": "primary",
                }
            )

    def test_manifest_spec_is_strict_and_output_never_overwrites(self):
        spec = self.root / "sources.json"
        spec.write_text(
            json.dumps(
                {
                    "tenant_id": TENANT_ID,
                    "knowledge_base_id": KB_ID,
                    "release_id": RELEASE_ID,
                    "sources": [
                        {
                            "external_file_id": "manual",
                            "logical_path": "manual.pdf",
                            "source_type": "pdf",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        manifest = create_manifest_from_spec(
            spec,
            source_root=self.root,
        )
        output = self.root / "manifest.json"
        write_manifest_once(manifest, output)
        write_manifest_once(manifest, output)
        output.write_text("different", encoding="utf-8")
        with self.assertRaisesRegex(
            ReleaseControlError,
            "refusing to overwrite",
        ):
            write_manifest_once(manifest, output)


class ReleaseControlCliTests(unittest.TestCase):
    def env(self):
        return {
            "BAUER_V3_DATABASE_URL": "postgresql://not-opened-in-tests",
            "BAUER_V3_TENANT_ID": TENANT_ID,
            "BAUER_V3_KNOWLEDGE_BASE_ID": KB_ID,
            "BAUER_V3_PRINCIPAL_IDS_JSON": json.dumps([ADMIN_ID]),
            "BAUER_V3_EMBEDDING_MODEL": "bauer-embedding-v3",
            "BAUER_V3_EMBEDDING_DIMENSIONS": "1024",
        }

    @staticmethod
    def write_bound_manifest_spec(
        root: Path,
        *,
        expected_content: bytes,
    ) -> Path:
        specification = root / "bound-sources.json"
        specification.write_text(
            json.dumps(
                {
                    "tenant_id": TENANT_ID,
                    "knowledge_base_id": KB_ID,
                    "release_id": RELEASE_ID,
                    "sources": [
                        {
                            "external_file_id": "manual",
                            "logical_path": "manual.pdf",
                            "source_type": "pdf",
                            "metadata": {
                                "expected_source_sha256": sha256_bytes(
                                    expected_content
                                ),
                                "expected_source_byte_size": len(
                                    expected_content
                                ),
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return specification

    def test_manifest_create_accepts_matching_frozen_source_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = b"%PDF-1.7\nfrozen manual"
            (root / "manual.pdf").write_bytes(content)
            specification = self.write_bound_manifest_spec(
                root,
                expected_content=content,
            )
            manifest_path = root / "manifest.json"

            result = main(
                [
                    "manifest-create",
                    "--spec",
                    str(specification),
                    "--source-root",
                    str(root),
                    "--output",
                    str(manifest_path),
                ],
                environment={},
                stdout=io.StringIO(),
            )

            self.assertEqual(result, 0)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["sources"][0]["sha256"],
                sha256_bytes(content),
            )
            self.assertEqual(
                manifest["sources"][0]["byte_size"],
                len(content),
            )

    def test_manifest_create_rejects_frozen_source_hash_drift_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frozen = b"%PDF-1.7\nfrozen manual"
            changed = b"%PDF-1.7\nFROZEN MANUAL"
            self.assertEqual(len(changed), len(frozen))
            (root / "manual.pdf").write_bytes(changed)
            specification = self.write_bound_manifest_spec(
                root,
                expected_content=frozen,
            )
            manifest_path = root / "manifest.json"

            with self.assertRaisesRegex(
                SourceManifestIntegrityError,
                "hash drift",
            ):
                main(
                    [
                        "manifest-create",
                        "--spec",
                        str(specification),
                        "--source-root",
                        str(root),
                        "--output",
                        str(manifest_path),
                    ],
                    environment={},
                    stdout=io.StringIO(),
                )

            self.assertFalse(manifest_path.exists())

    def test_manifest_create_rejects_frozen_source_size_drift_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frozen = b"%PDF-1.7\nfrozen manual"
            (root / "manual.pdf").write_bytes(frozen + b"\nrevision")
            specification = self.write_bound_manifest_spec(
                root,
                expected_content=frozen,
            )
            manifest_path = root / "manifest.json"

            with self.assertRaisesRegex(
                SourceManifestIntegrityError,
                "size drift",
            ):
                main(
                    [
                        "manifest-create",
                        "--spec",
                        str(specification),
                        "--source-root",
                        str(root),
                        "--output",
                        str(manifest_path),
                    ],
                    environment={},
                    stdout=io.StringIO(),
                )

            self.assertFalse(manifest_path.exists())

    def test_toolchain_command_is_offline_and_prints_runtime_identity(self):
        def no_database(_settings):
            raise AssertionError("toolchain command must not initialize DB")

        output = io.StringIO()
        result = main(
            ["toolchain"],
            environment={
                "BAUER_V3_EMBEDDING_MODEL": "bauer-embedding-v3",
                "BAUER_V3_EMBEDDING_DIMENSIONS": "1024",
            },
            stdout=output,
            dependencies=CliDependencies(
                admin_factory=no_database,
                toolchain_factory=lambda **_kwargs: toolchain(),
            ),
        )
        self.assertEqual(result, 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["fingerprint"], FINGERPRINT)
        self.assertEqual(
            rendered["manifest"]["embedding"]["dimensions"],
            1024,
        )

    def test_activation_flag_is_required_before_dependencies_are_built(self):
        with self.assertRaises(SystemExit):
            main(
                [
                    "release-activate",
                    "--release-id",
                    RELEASE_ID,
                    "--actor-principal-id",
                    ADMIN_ID,
                    "--reason",
                    "promote",
                ],
                environment=self.env(),
                dependencies=CliDependencies(
                    admin_factory=lambda _settings: (_ for _ in ()).throw(
                        AssertionError("argparse must fail first")
                    )
                ),
            )

    def test_bootstrap_uses_only_the_owner_factory(self):
        admin = FakeAdmin()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            spec = Path(temporary) / "bootstrap.json"
            spec.write_text(
                json.dumps(
                    {
                        "external_key": "bauer",
                        "tenant_display_name": "Bauer",
                        "external_namespace": "bauer",
                        "kb_name": "Bauer",
                        "principals": [
                            {
                                "principal_id": ADMIN_ID,
                                "principal_type": "service",
                                "external_subject": "bauer-v3-admin",
                            }
                        ],
                        "grants": [
                            {
                                "principal_id": ADMIN_ID,
                                "permission": "admin",
                                "granted_by": "bootstrap",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = main(
                ["bootstrap", "--spec", str(spec)],
                environment=self.env(),
                stdout=output,
                dependencies=CliDependencies(
                    admin_factory=lambda _settings: (_ for _ in ()).throw(
                        AssertionError(
                            "bootstrap must not use the runtime admin factory"
                        )
                    ),
                    bootstrap_admin_factory=lambda _settings: admin,
                ),
            )
        self.assertEqual(result, 0)
        self.assertEqual(admin.calls[0][0], "bootstrap")

    def test_activation_with_confirmation_calls_production_gate(self):
        admin = FakeAdmin()
        output = io.StringIO()
        result = main(
            [
                "release-activate",
                "--release-id",
                RELEASE_ID,
                "--actor-principal-id",
                ADMIN_ID,
                "--reason",
                "promote independently verified release",
                "--confirm-active-pointer-change",
            ],
            environment=self.env(),
            stdout=output,
            dependencies=CliDependencies(
                admin_factory=lambda _settings: admin,
            ),
        )
        self.assertEqual(result, 0)
        self.assertEqual(admin.calls[0][0], "activate")
        self.assertTrue(admin.calls[0][1]["production"])
        self.assertEqual(
            json.loads(output.getvalue())["active_release_id"],
            RELEASE_ID,
        )

    def test_dead_letter_replay_cli_calls_the_audited_admin_path(self):
        admin = FakeAdmin()
        output = io.StringIO()
        result = main(
            [
                "job-replay-dead-letter",
                "--job-id",
                JOB_ID,
                "--idempotency-key",
                "replay:compile:attempt-2",
                "--actor-principal-id",
                ADMIN_ID,
            ],
            environment=self.env(),
            stdout=output,
            dependencies=CliDependencies(
                admin_factory=lambda _settings: admin,
            ),
        )
        self.assertEqual(result, 0)
        self.assertEqual(admin.calls[0][0], "replay_dead_letter")
        self.assertEqual(
            json.loads(output.getvalue())["replay_of_job_id"],
            JOB_ID,
        )

    def test_status_only_constructs_injected_status_reader(self):
        status = FakeStatusReader()
        output = io.StringIO()
        result = main(
            [
                "release-status",
                "--release-id",
                RELEASE_ID,
            ],
            environment=self.env(),
            stdout=output,
            dependencies=CliDependencies(
                admin_factory=lambda _settings: (_ for _ in ()).throw(
                    AssertionError("status must not construct release admin")
                ),
                status_reader_factory=lambda _settings: status,
            ),
        )
        self.assertEqual(result, 0)
        self.assertEqual(status.calls, [(KB_ID, RELEASE_ID)])

    def test_release_build_cli_uses_derived_identity_and_injected_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manual.pdf").write_bytes(b"%PDF-1.7\nmanual")
            manifest = create_source_manifest(
                root,
                tenant_id=TENANT_ID,
                knowledge_base_id=KB_ID,
                release_id=RELEASE_ID,
                sources=(
                    SourceSpec(
                        external_file_id="manual",
                        logical_path="manual.pdf",
                        source_type="pdf",
                    ),
                ),
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_bytes(manifest.to_bytes())
            admin = FakeAdmin()
            store = MemoryObjectStore()
            output = io.StringIO()

            result = main(
                [
                    "release-build",
                    "--manifest",
                    str(manifest_path),
                    "--source-root",
                    str(root),
                    "--created-by",
                    "release-operator",
                    "--expected-toolchain-fingerprint",
                    FINGERPRINT,
                ],
                environment=self.env(),
                stdout=output,
                dependencies=CliDependencies(
                    admin_factory=lambda _settings: admin,
                    object_store_factory=lambda _environment: store,
                    toolchain_factory=lambda **_kwargs: toolchain(),
                ),
            )

        self.assertEqual(result, 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["compiler_fingerprint"], FINGERPRINT)
        self.assertEqual(rendered["compile_job_count"], 1)
        self.assertEqual(
            [call[0] for call in admin.calls],
            ["create_release", "start_build"],
        )

    def test_manifest_create_and_verify_cli_remain_offline(self):
        def no_database(_settings):
            raise AssertionError("offline commands must not initialize DB")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manual.pdf").write_bytes(b"%PDF-1.7\nmanual")
            spec = root / "sources.json"
            spec.write_text(
                json.dumps(
                    {
                        "tenant_id": TENANT_ID,
                        "knowledge_base_id": KB_ID,
                        "release_id": RELEASE_ID,
                        "sources": [
                            {
                                "external_file_id": "manual",
                                "logical_path": "manual.pdf",
                                "source_type": "pdf",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"
            create_output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "manifest-create",
                        "--spec",
                        str(spec),
                        "--source-root",
                        str(root),
                        "--output",
                        str(manifest_path),
                    ],
                    environment={},
                    stdout=create_output,
                    dependencies=CliDependencies(
                        admin_factory=no_database,
                    ),
                ),
                0,
            )
            verify_output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "manifest-verify",
                        "--manifest",
                        str(manifest_path),
                        "--source-root",
                        str(root),
                    ],
                    environment={},
                    stdout=verify_output,
                    dependencies=CliDependencies(
                        admin_factory=no_database,
                    ),
                ),
                0,
            )
        self.assertTrue(json.loads(verify_output.getvalue())["verified"])


if __name__ == "__main__":
    unittest.main()
