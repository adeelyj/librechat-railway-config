from __future__ import annotations

from types import SimpleNamespace

from bauer_evidence_v4.deployment.worker import _failure_code


def test_failure_code_preserves_only_safe_constraint_identity() -> None:
    error = RuntimeError("sensitive database detail")
    error.diag = SimpleNamespace(  # type: ignore[attr-defined]
        constraint_name="canonical_cells_table_id_row_index_column_index_key"
    )
    assert _failure_code(error) == (
        "RuntimeError."
        "canonical_cells_table_id_row_index_column_index_key"
    )


def test_failure_code_sanitizes_and_bounds_unknown_exception_types() -> None:
    error_type = type("Unsafe Name!" * 20, (RuntimeError,), {})
    value = _failure_code(error_type())
    assert len(value) <= 128
    assert value
    assert all(character.isalnum() or character in "_.:-" for character in value)
