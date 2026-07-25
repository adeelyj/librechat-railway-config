from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.postgres_audit import (  # noqa: E402
    PostgresAuthorizationAuditError,
    PostgresAuthorizationAuditSink,
)
from bauer_evidence_v3.telemetry import AuthorizationAuditEvent  # noqa: E402


TENANT_ID = "11111111-1111-4111-8111-111111111111"
KB_ID = "22222222-2222-4222-8222-222222222222"
PRINCIPAL_ID = "33333333-3333-4333-8333-333333333333"
SOURCE_ID = "44444444-4444-4444-8444-444444444444"


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, audit_id=7):
        self.audit_id = audit_id
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def transaction(self):
        return FakeTransaction()

    def execute(self, sql, parameters=()):
        self.calls.append((" ".join(str(sql).split()), tuple(parameters)))
        if "record_authorization_audit" in sql:
            row = (
                {"authorization_audit_id": self.audit_id}
                if self.audit_id is not None
                else None
            )
            return FakeResult(row)
        return FakeResult()


def event(**overrides):
    values = {
        "request_id": "request-1",
        "route": "/v3/answer",
        "outcome": "allowed",
        "reason_code": "signed_scope_verified",
        "tenant_id": TENANT_ID,
        "knowledge_base_id": KB_ID,
        "user_id": "user",
        "agent_id": "agent",
        "authorized_source_count": 1,
        "authorized_source_ids": (SOURCE_ID,),
    }
    values.update(overrides)
    return AuthorizationAuditEvent(**values)


class PostgresAuthorizationAuditTests(unittest.TestCase):
    def make_sink(self, connection):
        return PostgresAuthorizationAuditSink(
            None,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            connection_factory=lambda: connection,
        )

    def test_in_scope_decision_resolves_sources_and_calls_narrow_function(self):
        connection = FakeConnection()
        self.make_sink(connection)(event())

        combined = "\n".join(sql for sql, _ in connection.calls)
        self.assertIn("record_authorization_audit", combined)
        self.assertIn("can_read_source", combined)
        record_parameters = next(
            parameters
            for sql, parameters in connection.calls
            if "record_authorization_audit" in sql
        )
        self.assertEqual(record_parameters[2], [SOURCE_ID])
        self.assertEqual(record_parameters[8], "allow")
        self.assertNotIn("user", record_parameters)
        self.assertNotIn("agent", record_parameters)

    def test_cross_deployment_or_unsigned_decision_is_log_only(self):
        connection = FakeConnection()
        sink = self.make_sink(connection)
        sink(event(tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
        sink(
            event(
                tenant_id=None,
                knowledge_base_id=None,
                authorized_source_ids=(),
                authorized_source_count=None,
            )
        )
        self.assertFalse(connection.calls)

    def test_unresolved_scope_and_unknown_route_fail_closed(self):
        with self.assertRaises(PostgresAuthorizationAuditError):
            self.make_sink(FakeConnection(audit_id=None))(event())
        with self.assertRaises(PostgresAuthorizationAuditError):
            self.make_sink(FakeConnection())(event(route="/health"))


if __name__ == "__main__":
    unittest.main()
