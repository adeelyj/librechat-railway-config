"""Production database-role checks for the V3 RLS boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DatabaseRoleSecurityError(RuntimeError):
    """The connected PostgreSQL role can bypass the V3 isolation boundary."""


_ROLE_CHECK_SQL = """
    SELECT
        current_user AS role_name,
        session_user AS session_role_name,
        role_row.rolsuper AS is_superuser,
        role_row.rolcreaterole AS creates_roles,
        role_row.rolcreatedb AS creates_databases,
        role_row.rolbypassrls AS bypasses_rls,
        role_row.rolreplication AS can_replicate,
        role_row.rolcanlogin AS can_login,
        role_row.rolinherit AS inherits_privileges,
        coalesce(
            (
                SELECT pg_has_role(
                    current_user,
                    required_role.oid,
                    'member'
                )
                FROM pg_catalog.pg_roles AS required_role
                WHERE required_role.rolname = %s
            ),
            false
        ) AS has_required_membership,
        pg_has_role(
            current_user,
            'bauer_rag_v3_reader',
            'member'
        ) AS has_reader_membership,
        pg_has_role(
            current_user,
            'bauer_rag_v3_ingester',
            'member'
        ) AS has_ingester_membership,
        pg_has_role(
            current_user,
            'bauer_rag_v3_evaluator',
            'member'
        ) AS has_evaluator_membership,
        pg_has_role(
            current_user,
            'bauer_rag_v3_reviewer',
            'member'
        ) AS has_reviewer_membership,
        pg_has_role(
            current_user,
            'bauer_rag_v3_admin',
            'member'
        ) AS has_admin_membership,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_roles AS inherited_role
            WHERE pg_has_role(
                current_user,
                inherited_role.oid,
                'member'
            )
              AND (
                  inherited_role.rolsuper
                  OR inherited_role.rolcreaterole
                  OR inherited_role.rolcreatedb
                  OR inherited_role.rolbypassrls
                  OR inherited_role.rolreplication
              )
        ) AS has_privileged_membership,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = 'bauer_rag_v3'
              AND pg_has_role(
                  current_user,
                  namespace.nspowner,
                  'member'
              )
        ) AS owns_v3_schema,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'bauer_rag_v3'
              AND pg_has_role(
                  current_user,
                  relation.relowner,
                  'member'
              )
        ) AS owns_v3_relation,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = 'bauer_rag_v3'
              AND pg_has_role(
                  current_user,
                  routine.proowner,
                  'member'
              )
        ) AS owns_v3_routine
    FROM pg_catalog.pg_roles AS role_row
    WHERE role_row.rolname = current_user
"""


def verify_runtime_database_role(
    connection: Any,
    *,
    required_group_role: str,
) -> str:
    """Reject owner, superuser, BYPASSRLS, or wrongly granted connections."""

    if not isinstance(required_group_role, str) or not required_group_role.strip():
        raise ValueError("required_group_role is required")
    required_group_role = required_group_role.strip()
    supported_roles = {
        "bauer_rag_v3_reader",
        "bauer_rag_v3_ingester",
        "bauer_rag_v3_evaluator",
        "bauer_rag_v3_reviewer",
        "bauer_rag_v3_admin",
    }
    if required_group_role not in supported_roles:
        raise ValueError("required_group_role is not a supported V3 runtime tier")
    cursor = connection.execute(_ROLE_CHECK_SQL, (required_group_role,))
    row = cursor.fetchone()
    if row is None:
        raise DatabaseRoleSecurityError(
            "database runtime role could not be inspected"
        )

    role_name = str(_field(row, "role_name", 0) or "").strip()
    if not role_name:
        raise DatabaseRoleSecurityError(
            "database runtime role has no inspectable identity"
        )
    session_role_name = str(_field(row, "session_role_name", 1) or "").strip()
    if not session_role_name or session_role_name != role_name:
        raise DatabaseRoleSecurityError(
            "production runtime must not rely on SET ROLE"
        )
    if _truth(_field(row, "is_superuser", 2)):
        raise DatabaseRoleSecurityError(
            "production runtime must not use a PostgreSQL superuser"
        )
    if any(
        _truth(_field(row, name, index))
        for name, index in (
            ("creates_roles", 3),
            ("creates_databases", 4),
            ("can_replicate", 6),
        )
    ):
        raise DatabaseRoleSecurityError(
            "production runtime role must not hold PostgreSQL administrative privileges"
        )
    if _truth(_field(row, "bypasses_rls", 5)):
        raise DatabaseRoleSecurityError(
            "production runtime role must not have BYPASSRLS"
        )
    if not _truth(_field(row, "can_login", 7)):
        raise DatabaseRoleSecurityError(
            "production runtime identity must be a dedicated login role"
        )
    if not _truth(_field(row, "inherits_privileges", 8)):
        raise DatabaseRoleSecurityError(
            "production runtime login must inherit its exact V3 role tier"
        )
    if not _truth(_field(row, "has_required_membership", 9)):
        raise DatabaseRoleSecurityError(
            f"production runtime role lacks {required_group_role} membership"
        )
    tier_memberships = {
        "bauer_rag_v3_reader": _truth(
            _field(row, "has_reader_membership", 10)
        ),
        "bauer_rag_v3_ingester": _truth(
            _field(row, "has_ingester_membership", 11)
        ),
        "bauer_rag_v3_evaluator": _truth(
            _field(row, "has_evaluator_membership", 12)
        ),
        "bauer_rag_v3_reviewer": _truth(
            _field(row, "has_reviewer_membership", 13)
        ),
        "bauer_rag_v3_admin": _truth(
            _field(row, "has_admin_membership", 14)
        ),
    }
    permitted_memberships = {
        "bauer_rag_v3_reader": {"bauer_rag_v3_reader"},
        "bauer_rag_v3_ingester": {
            "bauer_rag_v3_reader",
            "bauer_rag_v3_ingester",
        },
        "bauer_rag_v3_evaluator": {
            "bauer_rag_v3_reader",
            "bauer_rag_v3_evaluator",
        },
        "bauer_rag_v3_reviewer": {
            "bauer_rag_v3_reader",
            "bauer_rag_v3_reviewer",
        },
        "bauer_rag_v3_admin": {
            "bauer_rag_v3_reader",
            "bauer_rag_v3_ingester",
            "bauer_rag_v3_admin",
        },
    }
    if any(
        present and tier not in permitted_memberships[required_group_role]
        for tier, present in tier_memberships.items()
    ):
        raise DatabaseRoleSecurityError(
            f"production runtime role exceeds the exact {required_group_role} tier"
        )
    if _truth(_field(row, "has_privileged_membership", 15)):
        raise DatabaseRoleSecurityError(
            "production runtime role inherits a PostgreSQL privileged role"
        )
    if any(
        _truth(_field(row, name, index))
        for name, index in (
            ("owns_v3_schema", 16),
            ("owns_v3_relation", 17),
            ("owns_v3_routine", 18),
        )
    ):
        raise DatabaseRoleSecurityError(
            "production runtime role must not own V3 schema objects"
        )
    return role_name


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError) as exc:
        raise DatabaseRoleSecurityError(
            "database runtime role check returned an invalid row"
        ) from exc


def _truth(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value in {0, None}:
        return False
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "t", "1"}:
            return True
        if normalized in {"false", "f", "0", ""}:
            return False
    raise DatabaseRoleSecurityError(
        "database runtime role check returned a non-boolean flag"
    )
