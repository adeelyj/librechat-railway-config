from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path


MIGRATION_NAME = re.compile(r"^(?P<version>\d{3})_[a-z0-9_]+\.sql$")
MIGRATION_LOCK_KEY = 72_897_565_840_948
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MIGRATIONS_DIR = (
    _SERVICE_ROOT / "migrations_v4"
    if (_SERVICE_ROOT / "migrations_v4").is_dir()
    else _SERVICE_ROOT / "migrations"
)


@dataclass(frozen=True, slots=True)
class MigrationReport:
    applied_versions: tuple[int, ...]
    current_version: int


def run_migrations(
    database_url: str,
    migrations_dir: str | Path | None = None,
) -> MigrationReport:
    import psycopg

    directory = Path(migrations_dir or DEFAULT_MIGRATIONS_DIR)
    files = sorted(directory.glob("*.sql"))
    versions = []
    for path in files:
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"invalid V4 migration filename: {path.name}")
        versions.append(int(match.group("version")))
    if versions != list(range(1, len(files) + 1)):
        raise RuntimeError("V4 migrations must be contiguous from 001")
    applied_now: list[int] = []
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            exists = connection.execute(
                "SELECT to_regclass('bauer_rag_v4.schema_migrations')"
            ).fetchone()[0]
            applied = {}
            if exists:
                applied = {
                    int(version): (str(filename), str(checksum))
                    for version, filename, checksum in connection.execute(
                        """
                        SELECT version, filename, checksum
                        FROM bauer_rag_v4.schema_migrations
                        ORDER BY version
                        """
                    ).fetchall()
                }
            if sorted(applied) not in (
                [],
                list(range(1, max(applied, default=0) + 1)),
            ):
                raise RuntimeError("V4 migration history is not contiguous")
            for version, path in zip(versions, files, strict=True):
                payload = path.read_bytes()
                checksum = hashlib.sha256(payload).hexdigest()
                if version in applied:
                    if applied[version] != (path.name, checksum):
                        raise RuntimeError(
                            f"V4 migration drift at {version:03d}"
                        )
                    continue
                started = time.perf_counter()
                with connection.transaction():
                    with psycopg.ClientCursor(connection) as cursor:
                        cursor.execute(payload.decode("utf-8"))
                    connection.execute(
                        """
                        INSERT INTO bauer_rag_v4.schema_migrations
                            (version, filename, checksum, execution_ms)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            version,
                            path.name,
                            checksum,
                            round((time.perf_counter() - started) * 1000),
                        ),
                    )
                applied_now.append(version)
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,)
            )
    return MigrationReport(tuple(applied_now), versions[-1])
