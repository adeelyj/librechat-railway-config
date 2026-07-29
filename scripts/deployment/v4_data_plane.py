from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote


EXPECTED_SOURCE_COUNT = 373
EXPECTED_SOURCE_CONTRACT = (
    "40049a12aacb198018a633905d793c8ef9011403f3fdbcd34ed7fe0792ab2580"
)
LOGIN_ROLES = {
    "bauer_v3_shadow_reader_login": "bauer_rag_v4_reader",
    "bauer_v3_shadow_ingester_login": "bauer_rag_v4_worker",
    "bauer_v3_shadow_evaluator_login": "bauer_rag_v4_evaluator",
    "bauer_v3_shadow_reviewer_login": "bauer_rag_v4_reviewer",
    "bauer_v3_shadow_admin_login": "bauer_rag_v4_admin",
}


class DataPlaneError(RuntimeError):
    pass


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataPlaneError(f"{name} is required")
    return value.strip()


def _uuid(value: Any, name: str) -> str:
    try:
        return str(uuid.UUID(_required(value, name)))
    except ValueError as exc:
        raise DataPlaneError(f"{name} must be a UUID") from exc


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_request() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise DataPlaneError("stdin must contain JSON") from exc
    if not isinstance(value, dict):
        raise DataPlaneError("stdin must contain an object")
    operation = _required(value.get("operation"), "operation")
    if operation not in {
        "setup",
        "status",
        "retry-dead",
        "reset-build",
        "mark-ready",
    }:
        raise DataPlaneError("unsupported operation")
    normalized = {
        "operation": operation,
        "database": _required(value.get("database"), "database"),
        "port": int(value.get("port")),
        "owner_user": _required(value.get("owner_user"), "owner_user"),
        "owner_password": _required(
            value.get("owner_password"), "owner_password"
        ),
        "tenant_id": _uuid(value.get("tenant_id"), "tenant_id"),
        "knowledge_base_id": _uuid(
            value.get("knowledge_base_id"), "knowledge_base_id"
        ),
        "principal_id": _uuid(value.get("principal_id"), "principal_id"),
        "release_id": _uuid(value.get("release_id"), "release_id"),
        "v3_source_release_id": _uuid(
            value.get("v3_source_release_id"), "v3_source_release_id"
        ),
        "release_public_id": _required(
            value.get("release_public_id"), "release_public_id"
        ),
    }
    if (
        normalized["database"] != "bauer_v3"
        or normalized["owner_user"] != "postgres"
        or not 1 <= normalized["port"] <= 65535
        or len(normalized["owner_password"]) < 24
    ):
        raise DataPlaneError("database transport contract mismatch")
    normalized["evaluation"] = value.get("evaluation")
    return normalized


def _dsn(request: dict[str, Any]) -> str:
    return (
        "postgresql://"
        f"{quote(request['owner_user'], safe='')}:"
        f"{quote(request['owner_password'], safe='')}"
        f"@127.0.0.1:{request['port']}/{request['database']}"
        "?sslmode=require&connect_timeout=15"
    )


def _setup(connection: Any, request: dict[str, Any]) -> dict[str, Any]:
    version = connection.execute(
        "SELECT max(version) FROM bauer_rag_v4.schema_migrations"
    ).fetchone()[0]
    if int(version or 0) != 6:
        raise DataPlaneError("V4 schema is not at migration 006")
    v3 = connection.execute(
        """
        SELECT status, expected_source_count
        FROM bauer_rag_v3.knowledge_releases
        WHERE release_id = %s
        """,
        (request["v3_source_release_id"],),
    ).fetchone()
    if v3 is None or str(v3[0]) != "ready" or int(v3[1]) != 373:
        raise DataPlaneError("V3 source release is not the fixed ready release")
    member_count = connection.execute(
        """
        SELECT count(*)
        FROM bauer_rag_v3.release_sources
        WHERE release_id = %s
        """,
        (request["v3_source_release_id"],),
    ).fetchone()[0]
    if int(member_count) != EXPECTED_SOURCE_COUNT:
        raise DataPlaneError("V3 source membership is not 373")
    for login, group in LOGIN_ROLES.items():
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (login,),
        ).fetchone()
        if exists is None:
            raise DataPlaneError("expected shared runtime login is absent")
        connection.execute(f'GRANT "{group}" TO "{login}"')
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.tenants (tenant_id, slug)
            VALUES (%s, 'bauer-v4-shadow')
            """,
            (request["tenant_id"],),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.knowledge_bases (
                knowledge_base_id, tenant_id, slug
            )
            VALUES (%s, %s, 'bauer-corpus-v4')
            """,
            (request["knowledge_base_id"], request["tenant_id"]),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.principals (
                principal_id, tenant_id, external_subject_sha256
            )
            VALUES (%s, %s, %s)
            """,
            (
                request["principal_id"],
                request["tenant_id"],
                _digest("bauer-v4-private-reader"),
            ),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.knowledge_releases (
                release_id, tenant_id, knowledge_base_id,
                release_public_id, status, source_contract_sha256,
                canonical_schema_version, compiler_identity_sha256,
                projection_identity_sha256, embedding_identity_sha256,
                reranker_identity_sha256, gate_manifest_sha256
            )
            VALUES (
                %s, %s, %s, %s, 'building', %s, '4.1',
                %s, %s, %s, %s, %s
            )
            """,
            (
                request["release_id"],
                request["tenant_id"],
                request["knowledge_base_id"],
                request["release_public_id"],
                EXPECTED_SOURCE_CONTRACT,
                _digest("canonical-compiler-v4.1"),
                _digest("search-projection-v4.1"),
                _digest("local-hashing-dense-v1"),
                _digest("transparent-linear-v1"),
                "4b4581cca4971744a4875fa4226bf24644fa38c460a3d4c6068b2b6cf0916170",
            ),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.source_documents (
                source_id, tenant_id, knowledge_base_id,
                external_source_id, original_filename, authority
            )
            SELECT source.source_id, %s, %s, source.external_file_id,
                   version.filename, 'original'
            FROM bauer_rag_v3.release_sources AS member
            JOIN bauer_rag_v3.sources AS source
              ON source.source_id = member.source_id
            JOIN bauer_rag_v3.source_versions AS version
              ON version.source_version_id = member.source_version_id
            WHERE member.release_id = %s
            ORDER BY member.ordinal
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
                request["v3_source_release_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.source_versions (
                source_version_id, tenant_id, knowledge_base_id,
                source_id, content_sha256, byte_size, media_type,
                object_key
            )
            SELECT version.source_version_id, %s, %s, source.source_id,
                   object.sha256, object.byte_size, object.mime_type,
                   object.object_key
            FROM bauer_rag_v3.release_sources AS member
            JOIN bauer_rag_v3.sources AS source
              ON source.source_id = member.source_id
            JOIN bauer_rag_v3.source_versions AS version
              ON version.source_version_id = member.source_version_id
            JOIN bauer_rag_v3.objects AS object
              ON object.sha256 = version.sha256
            WHERE member.release_id = %s
            ORDER BY member.ordinal
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
                request["v3_source_release_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.release_sources (
                tenant_id, knowledge_base_id, release_id, source_id,
                source_version_id, ordinal
            )
            SELECT %s, %s, %s, member.source_id,
                   member.source_version_id, member.ordinal
            FROM bauer_rag_v3.release_sources AS member
            WHERE member.release_id = %s
            ORDER BY member.ordinal
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
                request["release_id"],
                request["v3_source_release_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.principal_source_grants (
                tenant_id, knowledge_base_id, principal_id, source_id
            )
            SELECT %s, %s, %s, member.source_id
            FROM bauer_rag_v3.release_sources AS member
            WHERE member.release_id = %s
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
                request["principal_id"],
                request["v3_source_release_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.compilation_jobs (
                tenant_id, knowledge_base_id, release_id, source_id
            )
            SELECT %s, %s, %s, member.source_id
            FROM bauer_rag_v3.release_sources AS member
            WHERE member.release_id = %s
            ORDER BY member.ordinal
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
                request["release_id"],
                request["v3_source_release_id"],
            ),
        )
    return _status(connection, request)


def _status(connection: Any, request: dict[str, Any]) -> dict[str, Any]:
    counts = {}
    for table in (
        "source_documents",
        "source_versions",
        "release_sources",
        "compiled_artifacts",
        "canonical_documents",
        "canonical_blocks",
        "canonical_tables",
        "canonical_cells",
        "canonical_facts",
        "search_projections",
        "embedding_cache",
        "active_release_pointers",
    ):
        row = connection.execute(
            f"""
            SELECT count(*)
            FROM bauer_rag_v4.{table}
            WHERE {
                "release_id = %s"
                if table not in {
                    "source_documents", "source_versions",
                    "embedding_cache", "active_release_pointers"
                }
                else "tenant_id = %s AND knowledge_base_id = %s"
            }
            """,
            (
                (request["release_id"],)
                if table not in {
                    "source_documents", "source_versions",
                    "embedding_cache", "active_release_pointers"
                }
                else (
                    request["tenant_id"],
                    request["knowledge_base_id"],
                )
            ),
        ).fetchone()
        counts[table] = int(row[0])
    jobs = {
        str(status): int(count)
        for status, count in connection.execute(
            """
            SELECT status, count(*)
            FROM bauer_rag_v4.compilation_jobs
            WHERE release_id = %s
            GROUP BY status
            """,
            (request["release_id"],),
        ).fetchall()
    }
    failures = [
        {
            "error_code": str(error_code),
            "error_fingerprint": str(error_fingerprint),
            "count": int(count),
        }
        for error_code, error_fingerprint, count in connection.execute(
            """
            SELECT error_code, error_fingerprint, count(*)
            FROM bauer_rag_v4.compilation_jobs
            WHERE release_id = %s
              AND status = 'dead'
            GROUP BY error_code, error_fingerprint
            ORDER BY error_code, error_fingerprint
            """,
            (request["release_id"],),
        ).fetchall()
    ]
    dead_by_media_type = {
        str(media_type): int(count)
        for media_type, count in connection.execute(
            """
            SELECT version.media_type, count(*)
            FROM bauer_rag_v4.compilation_jobs AS job
            JOIN bauer_rag_v4.release_sources AS member
              ON member.release_id = job.release_id
             AND member.source_id = job.source_id
            JOIN bauer_rag_v4.source_versions AS version
              ON version.source_version_id = member.source_version_id
            WHERE job.release_id = %s
              AND job.status = 'dead'
            GROUP BY version.media_type
            ORDER BY version.media_type
            """,
            (request["release_id"],),
        ).fetchall()
    }
    duplicate_metrics = connection.execute(
        """
        WITH content_groups AS (
            SELECT version.content_sha256,
                   count(*) AS source_count
            FROM bauer_rag_v4.release_sources AS member
            JOIN bauer_rag_v4.source_versions AS version
              ON version.source_version_id = member.source_version_id
            WHERE member.release_id = %s
            GROUP BY version.content_sha256
            HAVING count(*) > 1
        ),
        path_groups AS (
            SELECT version.content_sha256,
                   document.original_filename,
                   count(*) AS source_count
            FROM bauer_rag_v4.release_sources AS member
            JOIN bauer_rag_v4.source_versions AS version
              ON version.source_version_id = member.source_version_id
            JOIN bauer_rag_v4.source_documents AS document
              ON document.source_id = member.source_id
            WHERE member.release_id = %s
            GROUP BY version.content_sha256, document.original_filename
            HAVING count(*) > 1
        ),
        dead AS (
            SELECT job.source_id
            FROM bauer_rag_v4.compilation_jobs AS job
            WHERE job.release_id = %s
              AND job.status = 'dead'
        )
        SELECT
            (SELECT count(*) FROM content_groups),
            (SELECT coalesce(sum(source_count), 0) FROM content_groups),
            (SELECT count(*) FROM path_groups),
            (SELECT coalesce(sum(source_count), 0) FROM path_groups),
            (
                SELECT count(*)
                FROM dead
                JOIN bauer_rag_v4.source_versions AS version
                  ON version.source_id = dead.source_id
                JOIN content_groups
                  ON content_groups.content_sha256 =
                     version.content_sha256
            ),
            (
                SELECT count(*)
                FROM dead
                JOIN bauer_rag_v4.source_versions AS version
                  ON version.source_id = dead.source_id
                JOIN bauer_rag_v4.source_documents AS document
                  ON document.source_id = dead.source_id
                JOIN path_groups
                  ON path_groups.content_sha256 =
                     version.content_sha256
                 AND path_groups.original_filename =
                     document.original_filename
            )
        """,
        (
            request["release_id"],
            request["release_id"],
            request["release_id"],
        ),
    ).fetchone()
    release = connection.execute(
        """
        SELECT status, ready_at IS NOT NULL
        FROM bauer_rag_v4.knowledge_releases
        WHERE release_id = %s
        """,
        (request["release_id"],),
    ).fetchone()
    return {
        "schema_version": 1,
        "kind": "bauer-rag-v4-private-shadow-status",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "release_id": request["release_id"],
        "release_status": str(release[0]) if release else "absent",
        "release_ready_at_present": bool(release[1]) if release else False,
        "counts": counts,
        "jobs": jobs,
        "dead_job_failures": failures,
        "dead_jobs_by_media_type": dead_by_media_type,
        "duplicate_source_metrics": {
            "content_groups": int(duplicate_metrics[0]),
            "content_sources": int(duplicate_metrics[1]),
            "content_and_filename_groups": int(duplicate_metrics[2]),
            "content_and_filename_sources": int(duplicate_metrics[3]),
            "dead_jobs_in_content_groups": int(duplicate_metrics[4]),
            "dead_jobs_in_content_and_filename_groups": int(
                duplicate_metrics[5]
            ),
        },
        "source_accounting_complete": (
            counts["release_sources"] == EXPECTED_SOURCE_COUNT
        ),
        "active_release_pointer_count": counts[
            "active_release_pointers"
        ],
        "locked_holdout_opened": False,
        "credentials_or_connection_details_emitted": False,
    }


def _mark_ready(connection: Any, request: dict[str, Any]) -> dict[str, Any]:
    status = _status(connection, request)
    if (
        status["counts"]["release_sources"] != EXPECTED_SOURCE_COUNT
        or status["counts"]["compiled_artifacts"] != EXPECTED_SOURCE_COUNT
        or status["counts"]["canonical_documents"] != EXPECTED_SOURCE_COUNT
        or status["jobs"].get("succeeded") != EXPECTED_SOURCE_COUNT
        or status["jobs"].get("dead", 0) != 0
        or status["active_release_pointer_count"] != 0
    ):
        raise DataPlaneError("release accounting is not ready for validation")
    evaluation = request.get("evaluation")
    if not isinstance(evaluation, dict) or not evaluation.get("passed"):
        raise DataPlaneError("passing named development evaluation is required")
    suite_sha = _required(evaluation.get("suite_sha256"), "suite_sha256")
    if len(suite_sha) != 64:
        raise DataPlaneError("suite_sha256 is invalid")
    case_count = int(evaluation.get("case_count", 0))
    if case_count < 1:
        raise DataPlaneError("evaluation case count is invalid")
    metrics = evaluation.get("metrics")
    if not isinstance(metrics, dict):
        raise DataPlaneError("evaluation metrics are required")
    with connection.transaction():
        connection.execute(
            """
            UPDATE bauer_rag_v4.knowledge_releases
            SET status = 'validating'
            WHERE release_id = %s AND status = 'building'
            """,
            (request["release_id"],),
        )
        connection.execute(
            """
            INSERT INTO bauer_rag_v4.evaluation_runs (
                tenant_id, knowledge_base_id, release_id,
                suite_sha256, split, case_count, metrics, passed
            )
            VALUES (%s, %s, %s, %s, 'development', %s, %s::jsonb, true)
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
                request["release_id"],
                suite_sha,
                case_count,
                json.dumps(metrics, sort_keys=True),
            ),
        )
        connection.execute(
            """
            UPDATE bauer_rag_v4.knowledge_releases
            SET status = 'ready', ready_at = clock_timestamp()
            WHERE release_id = %s AND status = 'validating'
            """,
            (request["release_id"],),
        )
    return _status(connection, request)


def _retry_dead(connection: Any, request: dict[str, Any]) -> dict[str, Any]:
    release = connection.execute(
        """
        SELECT status
        FROM bauer_rag_v4.knowledge_releases
        WHERE release_id = %s
        """,
        (request["release_id"],),
    ).fetchone()
    if release is None or str(release[0]) != "building":
        raise DataPlaneError("only a building release can retry dead jobs")
    with connection.transaction():
        cursor = connection.execute(
            """
            UPDATE bauer_rag_v4.compilation_jobs
            SET status = 'queued',
                attempts = 0,
                available_at = clock_timestamp(),
                leased_by = NULL,
                lease_expires_at = NULL,
                error_code = NULL,
                error_fingerprint = NULL,
                updated_at = clock_timestamp()
            WHERE release_id = %s
              AND status = 'dead'
            """,
            (request["release_id"],),
        )
    result = _status(connection, request)
    result["retried_dead_jobs"] = int(cursor.rowcount)
    return result


def _reset_build(connection: Any, request: dict[str, Any]) -> dict[str, Any]:
    release = connection.execute(
        """
        SELECT status
        FROM bauer_rag_v4.knowledge_releases
        WHERE release_id = %s
        """,
        (request["release_id"],),
    ).fetchone()
    if release is None or str(release[0]) != "building":
        raise DataPlaneError("only a building release can be reset")
    pointer_count = connection.execute(
        """
        SELECT count(*)
        FROM bauer_rag_v4.active_release_pointers
        WHERE release_id = %s
        """,
        (request["release_id"],),
    ).fetchone()[0]
    if int(pointer_count) != 0:
        raise DataPlaneError("a pointed release cannot be reset")
    with connection.transaction():
        for table in (
            "compiled_artifacts",
            "search_projections",
            "canonical_facts",
            "canonical_cells",
            "canonical_tables",
            "canonical_blocks",
            "canonical_documents",
        ):
            connection.execute(
                f"DELETE FROM bauer_rag_v4.{table} WHERE release_id = %s",
                (request["release_id"],),
            )
        connection.execute(
            """
            DELETE FROM bauer_rag_v4.embedding_cache
            WHERE tenant_id = %s
              AND knowledge_base_id = %s
            """,
            (
                request["tenant_id"],
                request["knowledge_base_id"],
            ),
        )
        connection.execute(
            """
            UPDATE bauer_rag_v4.compilation_jobs
            SET status = 'queued',
                attempts = 0,
                available_at = clock_timestamp(),
                leased_by = NULL,
                lease_expires_at = NULL,
                error_code = NULL,
                error_fingerprint = NULL,
                updated_at = clock_timestamp()
            WHERE release_id = %s
            """,
            (request["release_id"],),
        )
        connection.execute(
            """
            UPDATE bauer_rag_v4.knowledge_releases
            SET compiler_identity_sha256 = %s
            WHERE release_id = %s
              AND status = 'building'
            """,
            (
                _digest(
                    "canonical-compiler-v4.2-cell-provenance-ocr-fallback"
                ),
                request["release_id"],
            ),
        )
    result = _status(connection, request)
    result["build_reset_performed"] = True
    result["orphaned_candidate_objects_deleted"] = False
    return result


def main() -> int:
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--operation",
            choices=(
                "Setup",
                "Status",
                "RetryDead",
                "ResetBuild",
                "MarkReady",
            ),
        )
        arguments = parser.parse_args()
        request = _load_request()
        if (
            arguments.operation is not None
            and arguments.operation.lower()
            .replace("markready", "mark-ready")
            .replace("retrydead", "retry-dead")
            .replace("resetbuild", "reset-build")
            != request["operation"]
        ):
            raise DataPlaneError("operation argument does not match stdin")
        import psycopg

        with psycopg.connect(_dsn(request), autocommit=True) as connection:
            if request["operation"] == "setup":
                result = _setup(connection, request)
            elif request["operation"] == "mark-ready":
                result = _mark_ready(connection, request)
            elif request["operation"] == "retry-dead":
                result = _retry_dead(connection, request)
            elif request["operation"] == "reset-build":
                result = _reset_build(connection, request)
            else:
                result = _status(connection, request)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as error:
        safe = {
            "error_type": type(error).__name__,
            "error_fingerprint": hashlib.sha256(
                f"{type(error).__name__}:{error}".encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest(),
        }
        print(json.dumps(safe, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
