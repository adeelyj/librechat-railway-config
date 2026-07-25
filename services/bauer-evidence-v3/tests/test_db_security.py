from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.db_security import (  # noqa: E402
    DatabaseRoleSecurityError,
    verify_runtime_database_role,
)


class Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        return Cursor(self.row)


class DatabaseRoleSecurityTests(unittest.TestCase):
    def test_accepts_only_member_that_cannot_bypass_or_own(self):
        connection = Connection(
            {
                "role_name": "bauer_v3_api_login",
                "session_role_name": "bauer_v3_api_login",
                "is_superuser": False,
                "creates_roles": False,
                "creates_databases": False,
                "bypasses_rls": False,
                "can_replicate": False,
                "can_login": True,
                "inherits_privileges": True,
                "has_required_membership": True,
                "has_reader_membership": True,
                "has_ingester_membership": False,
                "has_evaluator_membership": False,
                "has_reviewer_membership": False,
                "has_admin_membership": False,
                "has_privileged_membership": False,
                "owns_v3_schema": False,
                "owns_v3_relation": False,
                "owns_v3_routine": False,
            }
        )
        role = verify_runtime_database_role(
            connection,
            required_group_role="bauer_rag_v3_reader",
        )
        self.assertEqual(role, "bauer_v3_api_login")
        self.assertEqual(
            connection.calls[0][1],
            ("bauer_rag_v3_reader",),
        )

    def test_rejects_every_rls_bypass_path(self):
        base = {
            "role_name": "runtime",
            "session_role_name": "runtime",
            "is_superuser": False,
            "creates_roles": False,
            "creates_databases": False,
            "bypasses_rls": False,
            "can_replicate": False,
            "can_login": True,
            "inherits_privileges": True,
            "has_required_membership": True,
            "has_reader_membership": True,
            "has_ingester_membership": False,
            "has_evaluator_membership": False,
            "has_reviewer_membership": False,
            "has_admin_membership": False,
            "has_privileged_membership": False,
            "owns_v3_schema": False,
            "owns_v3_relation": False,
            "owns_v3_routine": False,
        }
        cases = (
            ({"is_superuser": True}, "superuser"),
            ({"bypasses_rls": True}, "BYPASSRLS"),
            ({"creates_roles": True}, "administrative"),
            ({"has_required_membership": False}, "lacks"),
            ({"has_ingester_membership": True}, "exceeds the exact"),
            ({"has_evaluator_membership": True}, "exceeds the exact"),
            ({"has_reviewer_membership": True}, "exceeds the exact"),
            ({"has_privileged_membership": True}, "privileged role"),
            ({"owns_v3_relation": True}, "must not own"),
            ({"session_role_name": "session-owner"}, "SET ROLE"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                row = {**base, **changes}
                with self.assertRaisesRegex(
                    DatabaseRoleSecurityError,
                    message,
                ):
                    verify_runtime_database_role(
                        Connection(row),
                        required_group_role="bauer_rag_v3_reader",
                    )

    def test_ingester_tier_accepts_reader_inheritance_but_rejects_admin(self):
        row = {
            "role_name": "worker",
            "session_role_name": "worker",
            "is_superuser": False,
            "creates_roles": False,
            "creates_databases": False,
            "bypasses_rls": False,
            "can_replicate": False,
            "can_login": True,
            "inherits_privileges": True,
            "has_required_membership": True,
            "has_reader_membership": True,
            "has_ingester_membership": True,
            "has_evaluator_membership": False,
            "has_reviewer_membership": False,
            "has_admin_membership": False,
            "has_privileged_membership": False,
            "owns_v3_schema": False,
            "owns_v3_relation": False,
            "owns_v3_routine": False,
        }
        self.assertEqual(
            verify_runtime_database_role(
                Connection(row),
                required_group_role="bauer_rag_v3_ingester",
            ),
            "worker",
        )
        row["has_admin_membership"] = True
        with self.assertRaisesRegex(DatabaseRoleSecurityError, "exceeds the exact"):
            verify_runtime_database_role(
                Connection(row),
                required_group_role="bauer_rag_v3_ingester",
            )

    def test_evaluator_is_parallel_to_ingester_and_admin(self):
        row = {
            "role_name": "evaluator",
            "session_role_name": "evaluator",
            "is_superuser": False,
            "creates_roles": False,
            "creates_databases": False,
            "bypasses_rls": False,
            "can_replicate": False,
            "can_login": True,
            "inherits_privileges": True,
            "has_required_membership": True,
            "has_reader_membership": True,
            "has_ingester_membership": False,
            "has_evaluator_membership": True,
            "has_reviewer_membership": False,
            "has_admin_membership": False,
            "has_privileged_membership": False,
            "owns_v3_schema": False,
            "owns_v3_relation": False,
            "owns_v3_routine": False,
        }
        self.assertEqual(
            verify_runtime_database_role(
                Connection(row),
                required_group_role="bauer_rag_v3_evaluator",
            ),
            "evaluator",
        )
        row["has_admin_membership"] = True
        with self.assertRaisesRegex(DatabaseRoleSecurityError, "exceeds the exact"):
            verify_runtime_database_role(
                Connection(row),
                required_group_role="bauer_rag_v3_evaluator",
            )

    def test_reviewer_is_parallel_to_evaluator_ingester_and_admin(self):
        row = {
            "role_name": "reviewer",
            "session_role_name": "reviewer",
            "is_superuser": False,
            "creates_roles": False,
            "creates_databases": False,
            "bypasses_rls": False,
            "can_replicate": False,
            "can_login": True,
            "inherits_privileges": True,
            "has_required_membership": True,
            "has_reader_membership": True,
            "has_ingester_membership": False,
            "has_evaluator_membership": False,
            "has_reviewer_membership": True,
            "has_admin_membership": False,
            "has_privileged_membership": False,
            "owns_v3_schema": False,
            "owns_v3_relation": False,
            "owns_v3_routine": False,
        }
        self.assertEqual(
            verify_runtime_database_role(
                Connection(row),
                required_group_role="bauer_rag_v3_reviewer",
            ),
            "reviewer",
        )
        for conflicting_tier in (
            "has_ingester_membership",
            "has_evaluator_membership",
            "has_admin_membership",
        ):
            with self.subTest(conflicting_tier=conflicting_tier):
                conflicting_row = {**row, conflicting_tier: True}
                with self.assertRaisesRegex(
                    DatabaseRoleSecurityError,
                    "exceeds the exact",
                ):
                    verify_runtime_database_role(
                        Connection(conflicting_row),
                        required_group_role="bauer_rag_v3_reviewer",
                    )

    def test_reviewer_tuple_result_uses_the_extended_column_order(self):
        row = (
            "reviewer",
            "reviewer",
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
        )
        self.assertEqual(
            verify_runtime_database_role(
                Connection(row),
                required_group_role="bauer_rag_v3_reviewer",
            ),
            "reviewer",
        )


if __name__ == "__main__":
    unittest.main()
