"""Narrow durable authorization-audit adapter for the V3 API reader role."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Iterator

from .db_security import verify_runtime_database_role
from .telemetry import AuthorizationAuditEvent


class PostgresAuthorizationAuditError(RuntimeError):
    """The body-free authorization decision could not be durably recorded."""


_RECORD_SQL = """
WITH resolved_scope AS (
    SELECT
        coalesce(
            array_agg(source_row.source_id ORDER BY source_row.source_id),
            ARRAY[]::uuid[]
        ) AS source_ids,
        count(*)::integer AS matched_count
    FROM bauer_rag_v3.sources source_row
    WHERE source_row.tenant_id = %s::uuid
      AND source_row.kb_id = %s::uuid
      AND (
          source_row.source_id::text = ANY (%s::text[])
          OR source_row.external_file_id = ANY (%s::text[])
      )
      AND bauer_rag_v3.can_read_source(source_row.source_id)
),
recorded AS (
    SELECT bauer_rag_v3.record_authorization_audit(
        %s::uuid,
        %s::uuid,
        %s::uuid,
        resolved_scope.source_ids,
        %s,
        %s,
        %s,
        %s
    ) AS authorization_audit_id
    FROM resolved_scope
    WHERE resolved_scope.matched_count = %s
)
SELECT authorization_audit_id
FROM recorded
"""


class PostgresAuthorizationAuditSink:
    """Append authorized scope decisions through the hardened audit function.

    Invalid or deployment-mismatched signed contexts remain in structured
    process logs because they cannot safely establish another tenant's
    PostgreSQL context. Verified in-scope decisions are durable and fail
    closed if their source identifiers cannot all resolve through reader RLS.
    """

    def __init__(
        self,
        database_url: str | None,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        principal_ids: Sequence[str],
        connection_factory: Callable[[], Any] | None = None,
        connect_timeout_seconds: int = 5,
        statement_timeout_ms: int = 5_000,
        enforce_least_privilege: bool = False,
    ) -> None:
        if connection_factory is None and not (
            isinstance(database_url, str) and database_url.strip()
        ):
            raise ValueError("database_url or connection_factory is required")
        self.database_url = database_url
        self.tenant_id = str(uuid.UUID(tenant_id))
        self.knowledge_base_id = str(uuid.UUID(knowledge_base_id))
        self.principal_ids = tuple(
            sorted({str(uuid.UUID(value)) for value in principal_ids})
        )
        if not self.principal_ids:
            raise ValueError("at least one database principal ID is required")
        if connect_timeout_seconds < 1 or statement_timeout_ms < 1:
            raise ValueError("database timeouts must be positive")
        self.connection_factory = connection_factory
        self.connect_timeout_seconds = connect_timeout_seconds
        self.statement_timeout_ms = statement_timeout_ms
        self.enforce_least_privilege = bool(enforce_least_privilege)

    def __call__(self, event: AuthorizationAuditEvent) -> None:
        if not isinstance(event, AuthorizationAuditEvent):
            raise TypeError("event must be an AuthorizationAuditEvent")
        # These decisions are still emitted to structured logs. They are not
        # allowed to establish an untrusted or cross-deployment DB context.
        if (
            event.tenant_id != self.tenant_id
            or event.knowledge_base_id != self.knowledge_base_id
        ):
            return

        scope = tuple(
            sorted(
                {
                    value.strip()
                    for value in event.authorized_source_ids
                    if isinstance(value, str) and value.strip()
                }
            )
        )
        if len(scope) != len(event.authorized_source_ids):
            raise PostgresAuthorizationAuditError(
                "authorization audit source scope is malformed"
            )
        decision = {
            "allowed": "allow",
            "denied": "deny",
            "error": "error",
        }.get(event.outcome)
        if decision is None:
            raise PostgresAuthorizationAuditError(
                "authorization audit outcome is unsupported"
            )
        action = _audit_action(event.route)
        release_id = (
            str(uuid.UUID(event.release_id))
            if event.release_id is not None
            else None
        )

        with self._connection() as connection, connection.transaction():
            if self.enforce_least_privilege:
                verify_runtime_database_role(
                    connection,
                    required_group_role="bauer_rag_v3_reader",
                )
            for setting, value in (
                ("app.tenant_id", self.tenant_id),
                ("app.knowledge_base_id", self.knowledge_base_id),
                (
                    "app.principal_ids",
                    json.dumps(self.principal_ids, separators=(",", ":")),
                ),
                (
                    "app.authorized_source_ids",
                    json.dumps(scope, separators=(",", ":")),
                ),
                ("statement_timeout", str(self.statement_timeout_ms)),
            ):
                connection.execute(
                    "SELECT set_config(%s, %s, true)",
                    (setting, value),
                )
            row = connection.execute(
                _RECORD_SQL,
                (
                    self.tenant_id,
                    self.knowledge_base_id,
                    list(scope),
                    list(scope),
                    self.tenant_id,
                    self.knowledge_base_id,
                    release_id,
                    action,
                    decision,
                    event.reason_code,
                    event.request_id,
                    len(scope),
                ),
            ).fetchone()
            if row is None:
                raise PostgresAuthorizationAuditError(
                    "authorization audit source scope did not fully resolve"
                )
            audit_id = (
                row.get("authorization_audit_id")
                if isinstance(row, Mapping)
                else row[0]
            )
            if (
                isinstance(audit_id, bool)
                or not isinstance(audit_id, int)
                or audit_id < 1
            ):
                raise PostgresAuthorizationAuditError(
                    "authorization audit append returned an invalid identity"
                )

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self.connection_factory is not None:
            connection = self.connection_factory()
        else:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ModuleNotFoundError as exc:  # pragma: no cover
                raise PostgresAuthorizationAuditError(
                    "PostgreSQL authorization audit requires psycopg 3"
                ) from exc
            connection = psycopg.connect(
                self.database_url,
                row_factory=dict_row,
                connect_timeout=self.connect_timeout_seconds,
                application_name="bauer-evidence-v3-audit",
            )
        with connection:
            yield connection


def _audit_action(route: str) -> str:
    actions = {
        "/v3/answer": "v3_answer",
        "/v3/query": "v3_query",
    }
    try:
        return actions[route]
    except KeyError as exc:
        raise PostgresAuthorizationAuditError(
            "authorization audit route is unsupported"
        ) from exc
