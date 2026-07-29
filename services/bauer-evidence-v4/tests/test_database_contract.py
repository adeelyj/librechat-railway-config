from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
ROLLBACKS = ROOT / "rollbacks"


def test_numbered_migrations_have_reverse_rollbacks() -> None:
    up = sorted(MIGRATIONS.glob("*.sql"))
    down = sorted(ROLLBACKS.glob("*.down.sql"))
    assert [path.name[:3] for path in up] == [
        "001",
        "002",
        "003",
        "004",
        "005",
    ]
    assert [path.name[:3] for path in down] == [
        "001",
        "002",
        "003",
        "004",
        "005",
    ]
    assert all(path.read_text(encoding="utf-8").strip() for path in up + down)


def test_database_contract_is_v4_isolated_and_host_neutral() -> None:
    sql = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS.glob("*.sql"))
    )
    assert "bauer_rag_v3" not in sql
    assert "railway" not in sql.casefold()
    assert "librechat" not in sql.casefold()
    assert "CREATE SCHEMA bauer_rag_v4" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "readable_source_ids" in sql
    assert "resolve_pinned_release" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "embedding_identity_sha256" in sql
    assert "canonical_evidence_ids text[]" in sql


def test_runtime_roles_are_exact_non_privileged_groups() -> None:
    sql = (MIGRATIONS / "005_roles_rls.sql").read_text(
        encoding="utf-8"
    )
    expected = {
        "bauer_rag_v4_reader",
        "bauer_rag_v4_worker",
        "bauer_rag_v4_evaluator",
        "bauer_rag_v4_reviewer",
        "bauer_rag_v4_admin",
    }
    assert expected <= set(re.findall(r"'(bauer_rag_v4_[a-z]+)'", sql))
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in sql
    assert "NOBYPASSRLS" in sql
    assert "NOLOGIN" in sql


def test_audit_schema_is_body_free_and_hash_bounded() -> None:
    sql = (MIGRATIONS / "004_jobs_audit_validation.sql").read_text(
        encoding="utf-8"
    )
    audit = sql.split(
        "CREATE TABLE bauer_rag_v4.authorization_audit", 1
    )[1].split(");", 1)[0]
    assert "request_sha256" in audit
    assert "authorized_source_count" in audit
    for forbidden in ("question", "answer", "body", "prompt", "token"):
        assert forbidden not in audit.casefold()
