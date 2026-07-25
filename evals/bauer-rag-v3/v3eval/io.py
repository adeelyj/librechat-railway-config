from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


class EvaluationContractError(ValueError):
    """Raised when an evaluation suite or run violates the V3 contract."""


class LockedHoldoutError(EvaluationContractError):
    """Raised before any locked holdout file is accessed without acknowledgement."""


ALLOWED_SPLITS = frozenset({"development", "holdout"})


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise EvaluationContractError(f"Evaluation file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationContractError(
            f"Evaluation file is not valid JSON-compatible YAML: {path}: {error}"
        ) from error


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationContractError(f"{label} must be an object")
    return value


def _require_list(payload: Mapping[str, Any], field: str, label: str) -> list[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise EvaluationContractError(f"{label}.{field} must be an array")
    return value


def validate_split(split: str, *, acknowledge_locked_holdout: bool = False) -> str:
    if split not in ALLOWED_SPLITS:
        raise EvaluationContractError(
            "split must be exactly 'development' or 'holdout'; combined loading is forbidden"
        )
    if split == "holdout" and not acknowledge_locked_holdout:
        raise LockedHoldoutError(
            "The locked holdout requires --acknowledge-locked-holdout before its file "
            "may be accessed."
        )
    return split


def load_suite(
    eval_root: Path,
    *,
    split: str = "development",
    acknowledge_locked_holdout: bool = False,
) -> dict[str, Any]:
    """Load one explicitly selected suite.

    Holdout authorization is checked before constructing or opening the holdout path. There is no
    automatic split discovery and no combined mode.
    """

    validated_split = validate_split(
        split,
        acknowledge_locked_holdout=acknowledge_locked_holdout,
    )
    path = Path(eval_root) / "cases" / f"{validated_split}.yaml"
    payload = dict(_require_mapping(_read_json(path), f"suite {validated_split}"))
    if payload.get("split") != validated_split:
        raise EvaluationContractError(
            f"Suite declares split {payload.get('split')!r}, expected {validated_split!r}"
        )
    if payload.get("schema_version") != 1:
        raise EvaluationContractError("Suite schema_version must be 1")
    gold_status = payload.get("gold_status")
    if not isinstance(gold_status, str) or not gold_status.strip():
        raise EvaluationContractError("Suite gold_status must be a non-empty string")
    _require_mapping(payload.get("authorization_scope"), "suite.authorization_scope")
    for field in ("extraction_targets", "table_targets", "queries"):
        _require_list(payload, field, "suite")
    return payload


def load_run(path: Path, *, expected_split: str) -> dict[str, Any]:
    """Load an immutable run after the caller has explicitly selected its split."""

    if expected_split not in ALLOWED_SPLITS:
        raise EvaluationContractError("expected_split must be development or holdout")
    payload = dict(_require_mapping(_read_json(Path(path)), "run"))
    if payload.get("schema_version") != 1:
        raise EvaluationContractError("Run schema_version must be 1")
    if payload.get("split") != expected_split:
        raise EvaluationContractError(
            f"Run declares split {payload.get('split')!r}, expected {expected_split!r}"
        )
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise EvaluationContractError("Run run_id must be a non-empty string")
    observations = _require_mapping(payload.get("observations"), "run.observations")
    for field in (
        "extraction",
        "tables",
        "retrieval",
        "answers",
        "latency",
        "authorization",
    ):
        _require_list(observations, field, "run.observations")
    return payload


def write_json_exclusive(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
