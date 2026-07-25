from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")
MIGRATION_LOCK_KEY = 72_897_565_840_947
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


class MigrationError(RuntimeError):
    """Base class for migration discovery and database-state errors."""


class MigrationDependencyError(MigrationError):
    """Raised when the explicit migration command lacks its database driver."""


class MigrationDriftError(MigrationError):
    """Raised when an applied migration differs from its checked-in source."""


class MigrationStateError(MigrationError):
    """Raised when the database is missing or has an invalid migration prefix."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    path: Path
    checksum: str
    sql: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    filename: str
    checksum: str


@dataclass(frozen=True)
class MigrationReport:
    applied_versions: tuple[int, ...]
    current_version: int
    expected_version: int


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def discover_migrations(
    migrations_dir: str | Path | None = None,
) -> tuple[Migration, ...]:
    """Load a strict, contiguous sequence of immutable numbered SQL files."""

    directory = Path(migrations_dir or DEFAULT_MIGRATIONS_DIR).resolve()
    if not directory.is_dir():
        raise MigrationStateError(f"Migration directory does not exist: {directory}")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(directory.glob("*.sql"), key=lambda item: item.name):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationStateError(
                f"Invalid migration filename; expected NNN_name.sql: {path.name}"
            )
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationStateError(f"Duplicate migration version: {version:03d}")
        payload = path.read_bytes()
        if not payload.strip():
            raise MigrationStateError(f"Migration is empty: {path.name}")
        try:
            sql = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationStateError(
                f"Migration must be UTF-8: {path.name}"
            ) from error
        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                path=path,
                checksum=_sha256(payload),
                sql=sql,
            )
        )
        seen_versions.add(version)

    if not migrations:
        raise MigrationStateError(f"No SQL migrations found in {directory}")
    actual_versions = [migration.version for migration in migrations]
    expected_versions = list(range(1, len(migrations) + 1))
    if actual_versions != expected_versions:
        raise MigrationStateError(
            "Migration versions must be contiguous from 001; "
            f"found {actual_versions}"
        )
    return tuple(migrations)


def validate_applied_migrations(
    migrations: Sequence[Migration],
    applied: Mapping[int, AppliedMigration],
) -> tuple[Migration, ...]:
    """Refuse changed history and return the unapplied suffix."""

    local_by_version = {migration.version: migration for migration in migrations}
    unknown_versions = sorted(set(applied) - set(local_by_version))
    if unknown_versions:
        raise MigrationDriftError(
            "Database contains migration versions absent from this build: "
            + ", ".join(f"{version:03d}" for version in unknown_versions)
        )

    applied_versions = sorted(applied)
    if applied_versions:
        expected_prefix = list(range(1, applied_versions[-1] + 1))
        if applied_versions != expected_prefix:
            raise MigrationStateError(
                "Applied migrations are not a contiguous prefix; "
                f"found {applied_versions}"
            )

    for version, recorded in applied.items():
        local = local_by_version[version]
        if recorded.filename != local.filename:
            raise MigrationDriftError(
                f"Migration {version:03d} filename drift: "
                f"database={recorded.filename!r}, local={local.filename!r}"
            )
        if recorded.checksum != local.checksum:
            raise MigrationDriftError(
                f"Migration {version:03d} checksum drift for {local.filename}"
            )

    return tuple(
        migration
        for migration in migrations
        if migration.version not in applied
    )


def expected_schema_version(
    migrations_dir: str | Path | None = None,
) -> int:
    return discover_migrations(migrations_dir)[-1].version


def _load_psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as error:
        raise MigrationDependencyError(
            "Explicit V3 migration execution requires psycopg 3"
        ) from error
    return psycopg


def _read_applied(connection: Any) -> dict[int, AppliedMigration]:
    relation = connection.execute(
        "SELECT to_regclass('bauer_rag_v3.schema_migrations')"
    ).fetchone()
    if not relation or relation[0] is None:
        return {}
    rows = connection.execute(
        """
        SELECT version, filename, checksum
        FROM bauer_rag_v3.schema_migrations
        ORDER BY version
        """
    ).fetchall()
    return {
        int(version): AppliedMigration(
            version=int(version),
            filename=str(filename),
            checksum=str(checksum),
        )
        for version, filename, checksum in rows
    }


def _execute_script(connection: Any, psycopg: Any, sql: str) -> None:
    """Use PostgreSQL's simple-query protocol for multi-statement SQL files."""

    with psycopg.ClientCursor(connection) as cursor:
        cursor.execute(sql)


def run_migrations(
    database_url: str,
    migrations_dir: str | Path | None = None,
) -> MigrationReport:
    """Explicitly apply pending migrations under a session advisory lock."""

    if not database_url.strip():
        raise ValueError("database_url must not be empty")
    migrations = discover_migrations(migrations_dir)
    psycopg = _load_psycopg()
    newly_applied: list[int] = []

    with psycopg.connect(database_url, autocommit=True) as connection:
        locked = False
        try:
            connection.execute(
                "SELECT pg_advisory_lock(%s)",
                (MIGRATION_LOCK_KEY,),
            )
            locked = True
            applied = _read_applied(connection)
            pending = validate_applied_migrations(migrations, applied)

            for migration in pending:
                started = time.perf_counter()
                with connection.transaction():
                    _execute_script(connection, psycopg, migration.sql)
                    execution_ms = max(
                        0,
                        round((time.perf_counter() - started) * 1000),
                    )
                    connection.execute(
                        """
                        INSERT INTO bauer_rag_v3.schema_migrations (
                            version,
                            filename,
                            checksum,
                            execution_ms
                        )
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            migration.version,
                            migration.filename,
                            migration.checksum,
                            execution_ms,
                        ),
                    )
                newly_applied.append(migration.version)

            final_applied = _read_applied(connection)
            validate_applied_migrations(migrations, final_applied)
        finally:
            if locked:
                connection.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (MIGRATION_LOCK_KEY,),
                )

    expected = migrations[-1].version
    return MigrationReport(
        applied_versions=tuple(newly_applied),
        current_version=expected,
        expected_version=expected,
    )


def verify_schema_version(
    database_url: str,
    migrations_dir: str | Path | None = None,
    *,
    expected_version: int | None = None,
) -> MigrationReport:
    """Read-only readiness check; this function never creates or alters schema."""

    if not database_url.strip():
        raise ValueError("database_url must not be empty")
    migrations = discover_migrations(migrations_dir)
    local_expected = migrations[-1].version
    required = local_expected if expected_version is None else expected_version
    if required < 1 or required > local_expected:
        raise MigrationStateError(
            f"Expected version {required} is outside local range 1..{local_expected}"
        )

    psycopg = _load_psycopg()
    with psycopg.connect(database_url, autocommit=True) as connection:
        applied = _read_applied(connection)
    if not applied:
        raise MigrationStateError("Bauer RAG V3 schema has not been migrated")
    validate_applied_migrations(migrations, applied)
    current = max(applied)
    if current != required:
        raise MigrationStateError(
            f"Bauer RAG V3 schema is at {current}; expected {required}"
        )
    return MigrationReport(
        applied_versions=(),
        current_version=current,
        expected_version=required,
    )
