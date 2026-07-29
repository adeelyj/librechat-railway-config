from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{7,62}$")
SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,47}$")
GROUP_ROLES = (
    "bauer_rag_v4_reader",
    "bauer_rag_v4_worker",
    "bauer_rag_v4_evaluator",
    "bauer_rag_v4_reviewer",
    "bauer_rag_v4_admin",
)
NAMESPACE = uuid.UUID("4bce439f-4528-54ed-a27a-714f29d16f33")


class RehearsalError(RuntimeError):
    pass


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("SQL literal contains NUL")
    return "'" + value.replace("'", "''") + "'"


def _sql_array(values: list[str]) -> str:
    return "ARRAY[" + ",".join(_sql_literal(value) for value in values) + "]"


def _stable_uuid(run_id: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{run_id}:{name}"))


def _safe_tail(value: str) -> str:
    sanitized = re.sub(
        r"(?i)postgres(?:ql)?://[^\s\"')]+",
        "[REDACTED_DATABASE_URL]",
        value,
    )
    sanitized = re.sub(
        r"(?i)(password|token|secret)=\S+",
        r"\1=[REDACTED]",
        sanitized,
    )
    return sanitized[-2000:]


@dataclass(frozen=True, slots=True)
class Target:
    run_id: str
    database: str
    owner: str

    @classmethod
    def from_run_id(cls, run_id: str) -> "Target":
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("unsafe run id")
        suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
        target = cls(
            run_id=run_id,
            database=f"bv4_reh_{suffix}",
            owner=f"bv4_owner_{suffix}",
        )
        if not all(
            SAFE_IDENTIFIER.fullmatch(item)
            for item in (target.database, target.owner, *GROUP_ROLES)
        ):
            raise ValueError("generated identifier is unsafe")
        return target


@dataclass(slots=True)
class SshPostgres:
    ssh_exe: str
    ssh_target: str
    ssh_key: Path

    def psql(
        self,
        database: str,
        sql: str,
        *,
        expect_failure: bool = False,
    ) -> str:
        if not SAFE_IDENTIFIER.fullmatch(database) and database != "postgres":
            raise ValueError("unsafe database name")
        command = [
            self.ssh_exe,
            "-T",
            "-i",
            str(self.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            self.ssh_target,
            (
                "sudo -n -u postgres psql -X --no-psqlrc "
                "--set ON_ERROR_STOP=1 --quiet --tuples-only --no-align "
                f"--dbname={database}"
            ),
        ]
        completed = subprocess.run(
            command,
            input=sql,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if expect_failure:
            if completed.returncode == 0:
                raise RehearsalError("expected PostgreSQL failure succeeded")
            return ""
        if completed.returncode != 0:
            raise RehearsalError(
                "PostgreSQL phase failed: "
                + _safe_tail(completed.stdout + "\n" + completed.stderr)
            )
        return completed.stdout.strip()


def _migration_files(root: Path) -> tuple[list[Path], list[Path]]:
    migrations = sorted((root / "migrations").glob("*.sql"))
    rollbacks = sorted((root / "rollbacks").glob("*.down.sql"))
    versions = [path.name[:3] for path in migrations]
    if versions != ["001", "002", "003", "004", "005", "006"]:
        raise RehearsalError("unexpected migration sequence")
    if [path.name[:3] for path in rollbacks] != versions:
        raise RehearsalError("rollback sequence does not match migrations")
    return migrations, rollbacks


def _apply_migrations(
    remote: SshPostgres,
    target: Target,
    migrations: list[Path],
) -> list[dict[str, Any]]:
    applied = []
    for path in migrations:
        version = int(path.name[:3])
        payload = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        sql = (
            "BEGIN;\n"
            f"SET LOCAL ROLE {target.owner};\n"
            f"{payload}\n"
            "INSERT INTO bauer_rag_v4.schema_migrations "
            "(version, filename, checksum, execution_ms) VALUES "
            f"({version}, {_sql_literal(path.name)}, "
            f"{_sql_literal(checksum)}, 0);\n"
            "COMMIT;\n"
        )
        remote.psql(target.database, sql)
        applied.append(
            {
                "version": version,
                "filename": path.name,
                "sha256": checksum,
            }
        )
    count = remote.psql(
        target.database,
        "SELECT count(*) FROM bauer_rag_v4.schema_migrations;\n",
    )
    if count != str(len(migrations)):
        raise RehearsalError("migration ledger count mismatch")
    return applied


def _provision_extensions(
    remote: SshPostgres,
    target: Target,
) -> None:
    # Extension installation is a database bootstrap responsibility. The
    # migration owner remains non-superuser and only verifies/reuses them.
    remote.psql(
        target.database,
        """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
""",
    )


def _rollback_migrations(
    remote: SshPostgres,
    target: Target,
    rollbacks: list[Path],
) -> None:
    for path in reversed(rollbacks):
        payload = path.read_text(encoding="utf-8")
        remote.psql(
            target.database,
            "BEGIN;\n" + payload + "\nCOMMIT;\n",
        )
    state = remote.psql(
        target.database,
        """
SELECT json_build_object(
    'schema_absent', to_regnamespace('bauer_rag_v4') IS NULL,
    'role_count', (
        SELECT count(*) FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'bauer_rag_v4_reader',
            'bauer_rag_v4_worker',
            'bauer_rag_v4_evaluator',
            'bauer_rag_v4_reviewer',
            'bauer_rag_v4_admin'
        ])
    )
)::text;
""",
    )
    parsed = json.loads(state)
    if not parsed["schema_absent"] or parsed["role_count"] != 0:
        raise RehearsalError("rollback did not return to a clean V4 state")


def _inject_atomic_failure(remote: SshPostgres, target: Target) -> None:
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE {target.owner};
CREATE TABLE bauer_rag_v4.injected_failure_must_rollback(id integer);
INSERT INTO bauer_rag_v4.schema_migrations
    (version, filename, checksum, execution_ms)
VALUES (999, '999_injected_failure.sql', repeat('f', 64), 0);
SELECT 1 / 0;
COMMIT;
""",
        expect_failure=True,
    )
    state = remote.psql(
        target.database,
        """
SELECT json_build_object(
    'table_absent',
        to_regclass('bauer_rag_v4.injected_failure_must_rollback') IS NULL,
    'ledger_absent',
        NOT EXISTS (
            SELECT 1 FROM bauer_rag_v4.schema_migrations
            WHERE version = 999
        )
)::text;
""",
    )
    if json.loads(state) != {
        "table_absent": True,
        "ledger_absent": True,
    }:
        raise RehearsalError("failed migration was not atomic")


def _scope_sql(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    principal_id: str,
    release_id: str,
    source_ids: list[str],
) -> str:
    return "\n".join(
        (
            "SELECT set_config("
            "'bauer_rag_v4.tenant_id', "
            f"{_sql_literal(tenant_id)}, true);",
            "SELECT set_config("
            "'bauer_rag_v4.knowledge_base_id', "
            f"{_sql_literal(knowledge_base_id)}, true);",
            "SELECT set_config("
            "'bauer_rag_v4.principal_id', "
            f"{_sql_literal(principal_id)}, true);",
            "SELECT set_config("
            "'bauer_rag_v4.release_id', "
            f"{_sql_literal(release_id)}, true);",
            "SELECT set_config("
            "'bauer_rag_v4.source_ids', "
            f"{_sql_literal(','.join(source_ids))}, true);",
        )
    )


def _seed_sql(
    target: Target,
    representative: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    ids = {
        name: _stable_uuid(target.run_id, name)
        for name in (
            "tenant",
            "other_tenant",
            "knowledge_base",
            "other_knowledge_base",
            "principal",
            "other_principal",
            "ready_release",
            "building_release",
        )
    }
    documents = representative["documents"]
    if len(documents) != 3:
        raise RehearsalError("representative compilation must contain 3 documents")
    values = [
        (
            ids["tenant"],
            "v4-rehearsal",
        ),
        (
            ids["other_tenant"],
            "v4-other",
        ),
    ]
    statements = [
        "BEGIN;",
        f"SET LOCAL ROLE {target.owner};",
        "INSERT INTO bauer_rag_v4.tenants (tenant_id, slug) VALUES "
        + ",".join(
            f"({_sql_literal(tenant)}, {_sql_literal(slug)})"
            for tenant, slug in values
        )
        + ";",
        (
            "INSERT INTO bauer_rag_v4.knowledge_bases "
            "(knowledge_base_id, tenant_id, slug) VALUES "
            f"({_sql_literal(ids['knowledge_base'])}, "
            f"{_sql_literal(ids['tenant'])}, 'bauer-v4'),"
            f"({_sql_literal(ids['other_knowledge_base'])}, "
            f"{_sql_literal(ids['other_tenant'])}, 'other-v4');"
        ),
        (
            "INSERT INTO bauer_rag_v4.principals "
            "(principal_id, tenant_id, external_subject_sha256) VALUES "
            f"({_sql_literal(ids['principal'])}, "
            f"{_sql_literal(ids['tenant'])}, repeat('1', 64)),"
            f"({_sql_literal(ids['other_principal'])}, "
            f"{_sql_literal(ids['tenant'])}, repeat('2', 64));"
        ),
        (
            "INSERT INTO bauer_rag_v4.knowledge_releases "
            "(release_id, tenant_id, knowledge_base_id, release_public_id, "
            "status, source_contract_sha256, canonical_schema_version, "
            "compiler_identity_sha256, projection_identity_sha256, "
            "embedding_identity_sha256, reranker_identity_sha256, "
            "gate_manifest_sha256, ready_at) VALUES "
            f"({_sql_literal(ids['ready_release'])}, "
            f"{_sql_literal(ids['tenant'])}, "
            f"{_sql_literal(ids['knowledge_base'])}, "
            "'v4-rehearsal-ready', 'building', repeat('a',64), '4.1', "
            "repeat('b',64), repeat('c',64), repeat('d',64), "
            "repeat('e',64), repeat('f',64), NULL),"
            f"({_sql_literal(ids['building_release'])}, "
            f"{_sql_literal(ids['tenant'])}, "
            f"{_sql_literal(ids['knowledge_base'])}, "
            "'v4-rehearsal-building', 'building', repeat('a',64), '4.1', "
            "repeat('b',64), repeat('c',64), repeat('d',64), "
            "repeat('e',64), repeat('f',64), NULL);"
        ),
    ]
    source_ids: list[str] = []
    for index, item in enumerate(documents):
        source_id = item["external_source_id"]
        source_ids.append(source_id)
        version_id = _stable_uuid(target.run_id, f"source-version-{index}")
        projection = item["representative_projection"]
        statements.extend(
            (
                (
                    "INSERT INTO bauer_rag_v4.source_documents "
                    "(source_id, tenant_id, knowledge_base_id, "
                    "external_source_id, original_filename) VALUES "
                    f"({_sql_literal(source_id)}, "
                    f"{_sql_literal(ids['tenant'])}, "
                    f"{_sql_literal(ids['knowledge_base'])}, "
                    f"{_sql_literal(source_id)}, "
                    f"{_sql_literal(item['source_filename'])});"
                ),
                (
                    "INSERT INTO bauer_rag_v4.source_versions "
                    "(source_version_id, tenant_id, knowledge_base_id, "
                    "source_id, content_sha256, byte_size, media_type, "
                    "object_key) VALUES "
                    f"({_sql_literal(version_id)}, "
                    f"{_sql_literal(ids['tenant'])}, "
                    f"{_sql_literal(ids['knowledge_base'])}, "
                    f"{_sql_literal(source_id)}, "
                    f"{_sql_literal(item['content_sha256'])}, "
                    f"{int(item['byte_size'])}, "
                    f"{_sql_literal(item['media_type'])}, "
                    f"{_sql_literal('v4/rehearsal/' + item['content_sha256'])});"
                ),
                (
                    "INSERT INTO bauer_rag_v4.release_sources "
                    "(tenant_id, knowledge_base_id, release_id, source_id, "
                    "source_version_id, ordinal) VALUES "
                    f"({_sql_literal(ids['tenant'])}, "
                    f"{_sql_literal(ids['knowledge_base'])}, "
                    f"{_sql_literal(ids['ready_release'])}, "
                    f"{_sql_literal(source_id)}, "
                    f"{_sql_literal(version_id)}, {index});"
                ),
                (
                    "INSERT INTO bauer_rag_v4.canonical_documents "
                    "(canonical_document_id, tenant_id, knowledge_base_id, "
                    "release_id, source_id, source_version_id, "
                    "canonical_sha256, canonical_schema_version, "
                    "parser_identity, block_count, table_count, cell_count, "
                    "fact_count) VALUES "
                    f"({_sql_literal(item['canonical_document_id'])}, "
                    f"{_sql_literal(ids['tenant'])}, "
                    f"{_sql_literal(ids['knowledge_base'])}, "
                    f"{_sql_literal(ids['ready_release'])}, "
                    f"{_sql_literal(source_id)}, "
                    f"{_sql_literal(version_id)}, "
                    f"{_sql_literal(item['canonical_sha256'])}, "
                    f"{_sql_literal(item['canonical_schema_version'])}, "
                    f"{_sql_literal(item['parser_identity'])}, "
                    f"{int(item['block_count'])}, "
                    f"{int(item['table_count'])}, "
                    f"{int(item['cell_count'])}, "
                    f"{int(item['fact_count'])});"
                ),
                (
                    "INSERT INTO bauer_rag_v4.search_projections "
                    "(projection_id, canonical_document_id, tenant_id, "
                    "knowledge_base_id, release_id, source_id, "
                    "projection_type, projection_schema, search_text, "
                    "search_text_sha256, exact_terms, "
                    "canonical_evidence_ids, subject, predicate) VALUES "
                    f"({_sql_literal(projection['projection_id'])}, "
                    f"{_sql_literal(item['canonical_document_id'])}, "
                    f"{_sql_literal(ids['tenant'])}, "
                    f"{_sql_literal(ids['knowledge_base'])}, "
                    f"{_sql_literal(ids['ready_release'])}, "
                    f"{_sql_literal(source_id)}, "
                    f"{_sql_literal(projection['projection_type'])}, "
                    f"{_sql_literal(projection['projection_schema'])}, "
                    f"{_sql_literal(projection['search_text'])}, "
                    f"{_sql_literal(projection['search_text_sha256'])}, "
                    f"{_sql_array(projection['exact_terms'])}::text[], "
                    f"{_sql_array(projection['canonical_evidence_ids'])}::text[], "
                    f"{_sql_literal(projection['subject']) if projection['subject'] else 'NULL'}, "
                    f"{_sql_literal(projection['predicate']) if projection['predicate'] else 'NULL'});"
                ),
            )
        )
    statements.append(
        "INSERT INTO bauer_rag_v4.principal_source_grants "
        "(tenant_id, knowledge_base_id, principal_id, source_id) VALUES "
        f"({_sql_literal(ids['tenant'])}, "
        f"{_sql_literal(ids['knowledge_base'])}, "
        f"{_sql_literal(ids['principal'])}, "
        f"{_sql_literal(source_ids[0])});"
    )
    statements.append(
        "UPDATE bauer_rag_v4.knowledge_releases "
        "SET status = 'ready', ready_at = clock_timestamp() "
        f"WHERE release_id = {_sql_literal(ids['ready_release'])};"
    )
    statements.append("COMMIT;")
    ids["source_ids"] = ",".join(source_ids)
    return "\n".join(statements), ids


def _exercise_roles_and_rls(
    remote: SshPostgres,
    target: Target,
    ids: dict[str, str],
) -> dict[str, Any]:
    source_ids = ids["source_ids"].split(",")
    ready_scope = _scope_sql(
        tenant_id=ids["tenant"],
        knowledge_base_id=ids["knowledge_base"],
        principal_id=ids["principal"],
        release_id=ids["ready_release"],
        source_ids=source_ids,
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_reader;
{ready_scope}
DO $block$
DECLARE
    projection_count integer;
    release_count integer;
BEGIN
    SELECT count(*) INTO projection_count
    FROM bauer_rag_v4.search_projections;
    IF projection_count <> 1 THEN
        RAISE EXCEPTION 'reader source intersection failed';
    END IF;
    SELECT count(*) INTO release_count
    FROM bauer_rag_v4.resolve_pinned_release();
    IF release_count <> 1 THEN
        RAISE EXCEPTION 'ready release pin failed';
    END IF;
END
$block$;
ROLLBACK;
""",
    )
    denied_scope = _scope_sql(
        tenant_id=ids["tenant"],
        knowledge_base_id=ids["knowledge_base"],
        principal_id=ids["other_principal"],
        release_id=ids["ready_release"],
        source_ids=source_ids,
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_reader;
{denied_scope}
DO $block$
BEGIN
    IF (SELECT count(*) FROM bauer_rag_v4.search_projections) <> 0 THEN
        RAISE EXCEPTION 'unauthorized principal saw projections';
    END IF;
END
$block$;
ROLLBACK;
""",
    )
    nonready_scope = _scope_sql(
        tenant_id=ids["tenant"],
        knowledge_base_id=ids["knowledge_base"],
        principal_id=ids["principal"],
        release_id=ids["building_release"],
        source_ids=source_ids,
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_reader;
{nonready_scope}
DO $block$
BEGIN
    IF (SELECT count(*) FROM bauer_rag_v4.resolve_pinned_release()) <> 0
       OR (SELECT count(*) FROM bauer_rag_v4.search_projections) <> 0 THEN
        RAISE EXCEPTION 'non-ready release did not fail closed';
    END IF;
END
$block$;
ROLLBACK;
""",
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_reader;
{ready_scope}
UPDATE bauer_rag_v4.active_release_pointers
SET release_id = {_sql_literal(ids['ready_release'])};
COMMIT;
""",
        expect_failure=True,
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_reader;
{ready_scope}
DO $block$
BEGIN
    IF (SELECT count(*) FROM bauer_rag_v4.search_projections) <> 1 THEN
        RAISE EXCEPTION 'reader did not recover after rejected mutation';
    END IF;
END
$block$;
ROLLBACK;
""",
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_reader;
{ready_scope}
SELECT bauer_rag_v4.record_authorization_audit(
    'allow', 'scope_intersection_passed', repeat('9', 64), 1
);
ROLLBACK;
""",
    )
    # The rollback above proves the function path without retaining even
    # body-free rehearsal audit data.
    role_state = json.loads(
        remote.psql(
            target.database,
            """
SELECT json_build_object(
    'exact_role_count', count(*),
    'privileged_role_count', count(*) FILTER (
        WHERE rolsuper OR rolbypassrls OR rolcanlogin
              OR rolcreatedb OR rolcreaterole
    )
)::text
FROM pg_roles
WHERE rolname = ANY(ARRAY[
    'bauer_rag_v4_reader',
    'bauer_rag_v4_worker',
    'bauer_rag_v4_evaluator',
    'bauer_rag_v4_reviewer',
    'bauer_rag_v4_admin'
]);
""",
        )
    )
    if role_state != {
        "exact_role_count": 5,
        "privileged_role_count": 0,
    }:
        raise RehearsalError("runtime role posture failed")
    return {
        "exact_runtime_roles": 5,
        "privileged_runtime_roles": 0,
        "authorized_projection_count": 1,
        "unauthorized_projection_count": 0,
        "nonready_projection_count": 0,
        "ready_release_pin_count": 1,
        "client_release_mutation": "rejected",
        "audit_function": "body_free_and_transactionally_exercised",
    }


def _exercise_job_recovery(
    remote: SshPostgres,
    target: Target,
    ids: dict[str, str],
) -> dict[str, Any]:
    source_id = ids["source_ids"].split(",")[0]
    dead_job = _stable_uuid(target.run_id, "dead-job")
    crash_job = _stable_uuid(target.run_id, "crash-job")
    remote.psql(
        target.database,
        f"""
INSERT INTO bauer_rag_v4.compilation_jobs (
    job_id, tenant_id, knowledge_base_id, release_id, source_id, max_attempts
) VALUES
({_sql_literal(dead_job)}, {_sql_literal(ids['tenant'])},
 {_sql_literal(ids['knowledge_base'])}, {_sql_literal(ids['ready_release'])},
 {_sql_literal(source_id)}, 2),
({_sql_literal(crash_job)}, {_sql_literal(ids['tenant'])},
 {_sql_literal(ids['knowledge_base'])}, {_sql_literal(ids['ready_release'])},
 {_sql_literal(source_id)}, 3);
UPDATE bauer_rag_v4.compilation_jobs
SET available_at = clock_timestamp() + interval '1 hour'
WHERE job_id = {_sql_literal(crash_job)};
""",
    )
    scope = _scope_sql(
        tenant_id=ids["tenant"],
        knowledge_base_id=ids["knowledge_base"],
        principal_id=ids["principal"],
        release_id=ids["ready_release"],
        source_ids=[source_id],
    )
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_worker;
{scope}
SELECT job_id FROM bauer_rag_v4.claim_compilation_job('worker-a', 60);
SELECT bauer_rag_v4.fail_compilation_job(
    {_sql_literal(dead_job)}, 'worker-a', 'parser_failure', repeat('1', 64)
);
SELECT job_id FROM bauer_rag_v4.claim_compilation_job('worker-a', 60);
SELECT bauer_rag_v4.fail_compilation_job(
    {_sql_literal(dead_job)}, 'worker-a', 'parser_failure', repeat('2', 64)
);
COMMIT;
UPDATE bauer_rag_v4.compilation_jobs
SET available_at = clock_timestamp()
WHERE job_id = {_sql_literal(crash_job)};
""",
    )
    # Make the second job's lease stale as the migration owner, then prove a
    # different worker can requeue and reclaim it.
    remote.psql(
        target.database,
        f"""
BEGIN;
SET LOCAL ROLE bauer_rag_v4_worker;
{scope}
SELECT job_id FROM bauer_rag_v4.claim_compilation_job('worker-crashed', 60);
COMMIT;
UPDATE bauer_rag_v4.compilation_jobs
SET lease_expires_at = clock_timestamp() - interval '1 second'
WHERE job_id = {_sql_literal(crash_job)};
BEGIN;
SET LOCAL ROLE bauer_rag_v4_worker;
{scope}
SELECT bauer_rag_v4.requeue_expired_jobs();
SELECT job_id FROM bauer_rag_v4.claim_compilation_job('worker-recovery', 60);
COMMIT;
""",
    )
    states = json.loads(
        remote.psql(
            target.database,
            f"""
SELECT json_object_agg(job_id::text, json_build_object(
    'status', status,
    'attempts', attempts,
    'error_code', error_code
))::text
FROM bauer_rag_v4.compilation_jobs
WHERE job_id IN ({_sql_literal(dead_job)}, {_sql_literal(crash_job)});
""",
        )
    )
    if (
        states[dead_job]["status"] != "dead"
        or states[dead_job]["attempts"] != 2
        or states[crash_job]["status"] != "running"
        or states[crash_job]["attempts"] != 2
        or states[crash_job]["error_code"] != "lease_expired"
    ):
        raise RehearsalError("worker retry/dead-letter recovery failed")
    return {
        "bounded_retry_status": states[dead_job]["status"],
        "bounded_retry_attempts": states[dead_job]["attempts"],
        "expired_lease_requeued": True,
        "recovered_worker_status": states[crash_job]["status"],
        "recovered_worker_attempts": states[crash_job]["attempts"],
    }


def _database_state(
    remote: SshPostgres,
    target: Target,
    representative: dict[str, Any],
) -> dict[str, Any]:
    state = json.loads(
        remote.psql(
            target.database,
            """
SELECT json_build_object(
    'migration_count', (
        SELECT count(*) FROM bauer_rag_v4.schema_migrations
    ),
    'rls_table_count', (
        SELECT count(*) FROM pg_class AS class
        JOIN pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        WHERE namespace.nspname = 'bauer_rag_v4'
          AND class.relrowsecurity
    ),
    'policy_count', (
        SELECT count(*) FROM pg_policies
        WHERE schemaname = 'bauer_rag_v4'
    ),
    'canonical_document_count', (
        SELECT count(*) FROM bauer_rag_v4.canonical_documents
    ),
    'projection_count', (
        SELECT count(*) FROM bauer_rag_v4.search_projections
    ),
    'active_release_count', (
        SELECT count(*) FROM bauer_rag_v4.active_release_pointers
    ),
    'audit_forbidden_column_count', (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'bauer_rag_v4'
          AND table_name = 'authorization_audit'
          AND column_name ~* '(question|answer|body|prompt|token|secret)'
    )
)::text;
""",
        )
    )
    expected = {
        "migration_count": 6,
        "canonical_document_count": representative["document_count"],
        "projection_count": representative["document_count"],
        "active_release_count": 0,
        "audit_forbidden_column_count": 0,
    }
    if any(state[key] != value for key, value in expected.items()):
        raise RehearsalError("database state did not match rehearsal contract")
    if state["rls_table_count"] < 19 or state["policy_count"] < 20:
        raise RehearsalError("RLS policy coverage is incomplete")
    return state


def _preflight(remote: SshPostgres, target: Target) -> dict[str, Any]:
    output = remote.psql(
        "postgres",
        f"""
SELECT json_build_object(
    'server_version', current_setting('server_version'),
    'server_encoding', current_setting('server_encoding'),
    'database_exists', EXISTS(
        SELECT 1 FROM pg_database WHERE datname = {_sql_literal(target.database)}
    ),
    'owner_exists', EXISTS(
        SELECT 1 FROM pg_roles WHERE rolname = {_sql_literal(target.owner)}
    ),
    'v4_group_role_count', (
        SELECT count(*) FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'bauer_rag_v4_reader',
            'bauer_rag_v4_worker',
            'bauer_rag_v4_evaluator',
            'bauer_rag_v4_reviewer',
            'bauer_rag_v4_admin'
        ])
    ),
    'required_extension_count', (
        SELECT count(*) FROM pg_available_extensions
        WHERE name = ANY(ARRAY['pgcrypto', 'pg_trgm', 'vector'])
    )
)::text;
""",
    )
    state = json.loads(output)
    if state["required_extension_count"] != 3:
        raise RehearsalError("Fedora PostgreSQL lacks a required extension")
    return state


def _create_target(remote: SshPostgres, target: Target) -> None:
    state = _preflight(remote, target)
    if (
        state["database_exists"]
        or state["owner_exists"]
        or state["v4_group_role_count"]
    ):
        raise RehearsalError("isolated target or V4 role already exists")
    remote.psql(
        "postgres",
        (
            f"CREATE ROLE {target.owner} NOLOGIN NOSUPERUSER "
            "NOCREATEDB CREATEROLE NOINHERIT NOBYPASSRLS;\n"
        ),
    )
    remote.psql(
        "postgres",
        f"CREATE DATABASE {target.database} OWNER {target.owner};\n",
    )


def _cleanup_target(remote: SshPostgres, target: Target) -> None:
    remote.psql(
        "postgres",
        f"""
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = {_sql_literal(target.database)}
  AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS {target.database};
DO $block$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'bauer_rag_v4_reader',
        'bauer_rag_v4_worker',
        'bauer_rag_v4_evaluator',
        'bauer_rag_v4_reviewer',
        'bauer_rag_v4_admin',
        {_sql_literal(target.owner)}
    ]
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('DROP ROLE %I', role_name);
        END IF;
    END LOOP;
END
$block$;
""",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bauer RAG V4 isolated Fedora PostgreSQL rehearsal"
    )
    parser.add_argument("--mode", choices=("preflight", "apply"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--service-root", required=True, type=Path)
    parser.add_argument("--representative", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ssh-exe", default="ssh.exe")
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-key", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    target = Target.from_run_id(args.run_id)
    if not args.ssh_key.is_file():
        raise RehearsalError("scoped SSH key is unavailable")
    remote = SshPostgres(args.ssh_exe, args.ssh_target, args.ssh_key)
    preflight = _preflight(remote, target)
    if args.mode == "preflight":
        print(
            json.dumps(
                {
                    "passed": True,
                    "mode": "preflight",
                    "server_version": preflight["server_version"],
                    "server_encoding": preflight["server_encoding"],
                    "required_extension_count": preflight[
                        "required_extension_count"
                    ],
                    "target_available": not (
                        preflight["database_exists"]
                        or preflight["owner_exists"]
                        or preflight["v4_group_role_count"]
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.representative is None or args.output is None:
        raise RehearsalError(
            "apply mode requires representative input and output"
        )
    representative = json.loads(
        args.representative.read_text(encoding="utf-8")
    )
    if representative.get("locked_holdout_opened") is not False:
        raise RehearsalError("representative evidence lacks holdout boundary")
    migrations, rollbacks = _migration_files(args.service_root)
    created = False
    report: dict[str, Any] | None = None
    try:
        _create_target(remote, target)
        created = True
        _provision_extensions(remote, target)
        first_apply = _apply_migrations(remote, target, migrations)
        _inject_atomic_failure(remote, target)
        _rollback_migrations(remote, target, rollbacks)
        _provision_extensions(remote, target)
        second_apply = _apply_migrations(remote, target, migrations)
        seed_sql, ids = _seed_sql(target, representative)
        remote.psql(target.database, seed_sql)
        rls = _exercise_roles_and_rls(remote, target, ids)
        recovery = _exercise_job_recovery(remote, target, ids)
        state = _database_state(
            remote, target, representative
        )
        _rollback_migrations(remote, target, rollbacks)
        report = {
            "schema_version": 1,
            "kind": "bauer-rag-v4-fedora-postgresql-rehearsal",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "passed": True,
            "run_id": target.run_id,
            "scope": "isolated_fedora_postgresql",
            "server_version": preflight["server_version"],
            "server_encoding": preflight["server_encoding"],
            "database_name": target.database,
            "migration_owner_role": target.owner,
            "forward_migration_cycles": 2,
            "rollback_cycles": 2,
            "migration_count": len(second_apply),
            "migrations": second_apply,
            "first_cycle_checksums_equal": first_apply == second_apply,
            "failed_migration_atomicity": True,
            "roles_and_rls": rls,
            "representative_compilation": {
                "fixture_sha256": representative["fixture_sha256"],
                "case_ids": representative["case_ids"],
                "document_count": representative["document_count"],
                "totals": representative["totals"],
                "materialized_document_count": state[
                    "canonical_document_count"
                ],
                "materialized_projection_count": state[
                    "projection_count"
                ],
            },
            "api_failure_recovery": {
                "unauthorized_mutation_failed_closed": True,
                "subsequent_authorized_read_passed": True,
            },
            "worker_failure_recovery": recovery,
            "release_pinning": {
                "ready_pin_count": rls["ready_release_pin_count"],
                "nonready_pin_count": 0,
                "active_release_pointer_count": state[
                    "active_release_count"
                ],
                "client_mutation": rls["client_release_mutation"],
            },
            "audit_redaction": {
                "forbidden_column_count": state[
                    "audit_forbidden_column_count"
                ],
                "request_payload_persisted": False,
                "answer_payload_persisted": False,
            },
            "rls_table_count": state["rls_table_count"],
            "policy_count": state["policy_count"],
            "final_schema_present": False,
            "final_v4_group_role_count": 0,
            "cleanup_database_drop_pending": True,
            "locked_holdout_opened": False,
            "railway_mutated": False,
            "librechat_mutated": False,
        }
    finally:
        if created:
            _cleanup_target(remote, target)
    if report is None:
        raise RehearsalError("rehearsal did not produce evidence")
    postflight = _preflight(remote, target)
    report["cleanup_database_drop_pending"] = False
    report["cleanup_verified"] = (
        not postflight["database_exists"]
        and not postflight["owner_exists"]
        and postflight["v4_group_role_count"] == 0
    )
    if not report["cleanup_verified"]:
        raise RehearsalError("isolated Fedora cleanup did not verify")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "run_id": report["run_id"],
                "migration_count": report["migration_count"],
                "rls_table_count": report["rls_table_count"],
                "policy_count": report["policy_count"],
                "cleanup_verified": report["cleanup_verified"],
            },
            sort_keys=True,
        )
    )
    return 0


def run() -> int:
    try:
        return main()
    except RehearsalError as error:
        print(f"rehearsal error: {_safe_tail(str(error))}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"rehearsal failed closed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
