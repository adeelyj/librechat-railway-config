"""Independent-review attestation persistence for Bauer Evidence V3.

Evaluation execution and release control deliberately cannot create this
record.  A dedicated exact reviewer database tier authenticates one reviewer
principal and binds its immutable decision to the exact gold manifest plus the
hash of the independently signed review packet.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ContextManager

from .db_security import verify_runtime_database_role
from .evaluation import EvaluationManifest


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INDEPENDENT_GOLD_STATUS = "independent_bauer_verified"


class PostgresReviewError(RuntimeError):
    """Independent-review persistence failed or returned an invalid result."""


class PostgresReviewDependencyError(PostgresReviewError):
    """The optional PostgreSQL driver is unavailable."""


class PostgresIndependentGoldReviewer:
    """Append one database-authenticated decision for an immutable gold suite."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any]],
        *,
        tenant_id: str,
        reviewer_principal_id: str,
    ) -> None:
        self._connection_provider = connection_provider
        self.tenant_id = _uuid_text(tenant_id, "tenant_id")
        self.reviewer_principal_id = _uuid_text(
            reviewer_principal_id,
            "reviewer_principal_id",
        )

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        tenant_id: str,
        reviewer_principal_id: str,
    ) -> "PostgresIndependentGoldReviewer":
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database DSN is required")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - deployment guard
            raise PostgresReviewDependencyError(
                "psycopg is required for independent gold attestation"
            ) from error

        @contextmanager
        def connection_provider() -> Iterator[Any]:
            with psycopg.connect(
                dsn.strip(),
                row_factory=dict_row,
                application_name="bauer-v3-independent-gold-review",
            ) as connection:
                yield connection

        return cls(
            connection_provider,
            tenant_id=tenant_id,
            reviewer_principal_id=reviewer_principal_id,
        )

    def attest(
        self,
        *,
        manifest: EvaluationManifest,
        review_evidence_sha256: str,
        decision: str,
    ) -> str:
        """Record the review once; conflicting or duplicate records fail closed."""

        if manifest.scope.tenant_id != self.tenant_id:
            raise ValueError("manifest tenant does not match reviewer tenant")
        if manifest.gold_status != _INDEPENDENT_GOLD_STATUS:
            raise ValueError(
                "independent attestation requires an "
                "independent_bauer_verified manifest"
            )
        evidence_hash = _sha256_text(
            review_evidence_sha256,
            "review_evidence_sha256",
        )
        normalized_decision = str(decision).strip().lower()
        if normalized_decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")

        with self._connection_provider() as connection, connection.transaction():
            verify_runtime_database_role(
                connection,
                required_group_role="bauer_rag_v3_reviewer",
            )
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                (self.tenant_id,),
            )
            connection.execute(
                "SELECT set_config('app.principal_ids', %s, true)",
                (json.dumps([self.reviewer_principal_id]),),
            )
            cursor = connection.execute(
                """
                SELECT bauer_rag_v3.attest_independent_gold(
                    %s::uuid,
                    %s,
                    %s,
                    %s
                ) AS attestation_id
                """,
                (
                    manifest.eval_suite_id,
                    manifest.manifest_sha256,
                    evidence_hash,
                    normalized_decision,
                ),
            )
            row = cursor.fetchone()
            attestation_id = _row_value(row, "attestation_id", 0)
            if attestation_id is None:
                raise PostgresReviewError(
                    "independent review attestation returned no identifier"
                )
            return _uuid_text(str(attestation_id), "attestation_id")


def _uuid_text(value: object, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a UUID") from error


def _sha256_text(value: object, label: str) -> str:
    normalized = str(value).strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return normalized


def _row_value(
    row: Any,
    key: str,
    index: int,
) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None
