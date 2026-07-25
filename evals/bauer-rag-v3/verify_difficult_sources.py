"""Verify the portable interim B08/B11/B20/B21 fixture bundle.

This verifier intentionally reads only the fixture and the frozen V3 source
contract. It never opens source documents, V2 files, or a holdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE = ROOT / "fixtures" / "difficult-sources-interim.json"
DEFAULT_SOURCE_CONTRACT = (
    ROOT / "baselines" / "bauer-source-contract.json"
)
EXPECTED_CASE_IDS = ("B08", "B11", "B20", "B21")
REVIEW_STATUS = "interim_codex_reviewed_requires_bauer_signoff"
FORBIDDEN_BINDING_KEYS = frozenset(
    {
        "eval_run_id",
        "evidence_id",
        "knowledge_base_id",
        "release_id",
        "search_unit_id",
        "tenant_id",
    }
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DifficultSourceFixtureError(ValueError):
    """The portable fixture or its source-contract binding is invalid."""


def load_json(path: Path) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DifficultSourceFixtureError(
                    f"duplicate JSON key {key!r} in {path}"
                )
            result[key] = value
        return result

    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicates)
    except FileNotFoundError as error:
        raise DifficultSourceFixtureError(
            f"JSON file does not exist: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise DifficultSourceFixtureError(
            f"invalid JSON in {path}: {error}"
        ) from error


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_bundle(
    fixture: Mapping[str, Any],
    *,
    source_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    _require(fixture.get("schema_version") == 1, "schema_version must be 1")
    _require(
        fixture.get("bundle_kind")
        == "portable_interim_synthetic_extraction_fixture",
        "bundle_kind must identify the portable interim fixture",
    )
    _require(
        fixture.get("review_status") == REVIEW_STATUS,
        f"review_status must be {REVIEW_STATUS}",
    )
    _require(
        fixture.get("benchmark_eligible") is False,
        "fixture must explicitly be ineligible as a benchmark",
    )
    _reject_forbidden_bindings(fixture)
    limitations = _string_list(fixture.get("limitations"), "limitations")
    _require(
        any("not a real-corpus benchmark" in item for item in limitations),
        "limitations must prohibit real-corpus benchmark claims",
    )
    _require(
        any("independent Bauer sign-off" in item for item in limitations),
        "limitations must require independent Bauer sign-off",
    )

    raw_cases = fixture.get("cases")
    _require(isinstance(raw_cases, list), "cases must be an array")
    cases = [
        _mapping(value, f"cases[{index}]")
        for index, value in enumerate(raw_cases)
    ]
    case_ids = [str(case.get("case_id") or "") for case in cases]
    _require(
        tuple(case_ids) == EXPECTED_CASE_IDS,
        "cases must be ordered exactly B08, B11, B20, B21",
    )
    for case in cases:
        _verify_case(case)
    _verify_semantic_expectations({case["case_id"]: case for case in cases})

    checked_sources = 0
    if source_contract is not None:
        checked_sources = _verify_source_contract(cases, source_contract)
    return {
        "valid": True,
        "fixture_sha256": canonical_sha256(fixture),
        "case_ids": list(EXPECTED_CASE_IDS),
        "review_status": REVIEW_STATUS,
        "benchmark_eligible": False,
        "source_contract_identity_count": checked_sources,
    }


def _verify_case(case: Mapping[str, Any]) -> None:
    case_id = str(case.get("case_id") or "")
    _require(
        case.get("fixture_status") == "interim_synthetic_not_benchmark",
        f"{case_id}: fixture_status must remain interim/synthetic",
    )
    source_entries = [case]
    additional = case.get("additional_source_expectations", [])
    _require(
        isinstance(additional, list),
        f"{case_id}: additional_source_expectations must be an array",
    )
    source_entries.extend(
        _mapping(item, f"{case_id}.additional_source_expectations[{index}]")
        for index, item in enumerate(additional)
    )
    for index, entry in enumerate(source_entries):
        label = case_id if index == 0 else f"{case_id}.source[{index}]"
        _verify_source_expectation(entry, label=label)


def _verify_source_expectation(
    entry: Mapping[str, Any],
    *,
    label: str,
) -> None:
    identity = _mapping(
        entry.get("source_identity"),
        f"{label}.source_identity",
    )
    try:
        uuid.UUID(str(identity.get("external_file_id") or ""))
    except ValueError as error:
        raise DifficultSourceFixtureError(
            f"{label}: external_file_id must be a UUID source identity"
        ) from error
    digest = str(identity.get("expected_sha256") or "")
    _require(
        SHA256_PATTERN.fullmatch(digest) is not None,
        f"{label}: expected_sha256 must be lowercase SHA-256",
    )
    _require(
        bool(str(identity.get("logical_path") or "").strip()),
        f"{label}: logical_path is required",
    )
    media_type = str(identity.get("declared_media_type") or "")
    _require(
        media_type in {"text/html", "application/pdf"},
        f"{label}: unsupported declared_media_type",
    )
    provenance = _mapping(
        entry.get("provenance"),
        f"{label}.provenance",
    )
    _require(
        provenance.get("source_coordinate_required") is True,
        f"{label}: source coordinates must be mandatory",
    )
    if media_type == "application/pdf":
        _require(
            provenance.get("physical_page") == 41,
            f"{label}: PDF provenance must pin physical page 41",
        )
    else:
        _require(
            provenance.get("physical_page") is None,
            f"{label}: HTML provenance cannot invent a physical page",
        )
    expectations = _mapping(
        entry.get("expectations"),
        f"{label}.expectations",
    )
    tables = expectations.get("tables")
    _require(
        isinstance(tables, list) and tables,
        f"{label}: at least one table/panel expectation is required",
    )
    _require(
        all(
            isinstance(table, Mapping)
            and table.get("source_coordinate_required") is True
            for table in tables
        ),
        f"{label}: every table/panel must require source coordinates",
    )
    _require(
        bool(expectations.get("facts"))
        or bool(expectations.get("structural_checks")),
        f"{label}: fact or structural expectations are required",
    )


def _verify_semantic_expectations(
    cases: Mapping[str, Mapping[str, Any]],
) -> None:
    b08 = cases["B08"]["expectations"]
    _require(
        _fact_values(b08, "max_operating_pressure")
        == [{"unit": "bar", "value": 350}, {"unit": "bar", "value": 550}],
        "B08: pressure expectation must remain 350/550 bar",
    )
    _require(
        _fact_values(b08, "compatible_product_family")
        == ["MINI-VERTICUS", "VERTICUS"],
        "B08: compatible product families changed",
    )
    _require(
        b08.get("cross_source_checks")
        == [
            {
                "kind": "dryer_pressure_vs_selector_operating_pressure",
                "required_source_count": 2,
            }
        ],
        "B08: two-source comparison contract changed",
    )
    _require(
        bool(b08.get("verbatim_review_requirements")),
        "B08: cartridge-life text must remain pending verbatim review",
    )
    b08_additional = cases["B08"].get(
        "additional_source_expectations"
    ) or []
    _require(
        len(b08_additional) == 1
        and _fact_values(
            b08_additional[0]["expectations"],
            "operating_pressure",
        )
        == [414, 420],
        "B08: B-SELECT 414/420 bar selector evidence is required",
    )

    b11 = cases["B11"]["expectations"]
    b11_facts = b11["facts"]
    _require(
        any(
            fact.get("subject") == "Breathing air"
            and fact.get("value") == 300
            and fact.get("unit") == "bar"
            for fact in b11_facts
        )
        and any(
            fact.get("subject") == "Nitrox"
            and fact.get("value") == 200
            and fact.get("unit") == "bar"
            for fact in b11_facts
        ),
        "B11: documented medium pressure limits are incomplete",
    )
    _require(
        b11.get("decision_checks")
        == [
            {
                "documented_limit": {
                    "medium": "Nitrox",
                    "unit": "bar",
                    "value": 200,
                },
                "kind": "safety_limit",
                "requested_condition": {
                    "cylinder_medium": "Nitrox",
                    "unit": "bar",
                    "value": 300,
                },
                "required_outcome": (
                    "not_supported_by_documented_limit"
                ),
            }
        ]
        and bool(b11.get("negative_constraints")),
        "B11: Nitrox 300 bar non-inference safety check changed",
    )

    b20 = cases["B20"]["expectations"]
    _require(
        _fact_values(b20, "operating_pressure") == [414, 420],
        "B20: B-SELECT operating pressures changed",
    )
    _require(
        _fact_values(b20, "adjustment_range")
        == [
            {"maximum": 414, "minimum": 100},
            {"maximum": 420, "minimum": 100},
        ],
        "B20: B-SELECT adjustment ranges changed",
    )
    _require(
        _fact_values(b20, "flow")
        == {"50": 2750, "200": 3500, "300": 3700},
        "B20: B-SELECT flow expectations changed",
    )
    checks = b20.get("structural_checks") or []
    _require(
        checks
        == [
            {
                "expected_count": 3,
                "kind": "documented_product_functions",
                "subject": "B-SELECT",
            }
        ],
        "B20: exactly three documented function slots are required",
    )
    _require(
        bool(b20.get("verbatim_review_requirements")),
        "B20: function text must remain pending verbatim review",
    )

    b21 = cases["B21"]["expectations"]
    b21_facts = b21["facts"]
    _require(
        any(
            fact.get("subject") == "Breathing air"
            and fact.get("value") == 300
            and fact.get("unit") == "bar"
            for fact in b21_facts
        )
        and any(
            fact.get("subject") == "Nitrox"
            and fact.get("value") == 200
            and fact.get("unit") == "bar"
            for fact in b21_facts
        ),
        "B21: headline pressure expectations are incomplete",
    )
    _require(
        any(
            fact.get("predicate") == "medium"
            and fact.get("values") == ["Air", "Nitrox"]
            for fact in b21_facts
        )
        and any(
            fact.get("predicate") == "max_operating_pressure"
            and fact.get("value") == 410
            for fact in b21_facts
        )
        and any(
            fact.get("predicate") == "filling_pressure"
            and fact.get("values") == [225, 330]
            for fact in b21_facts
        ),
        "B21: later B-SAFE 300 technical data is incomplete",
    )
    _require(
        b21.get("contradiction_checks")
        == [
            {
                "do_not_conflate": [
                    "headline application pressure",
                    "B-SAFE 300 maximum operating pressure",
                    "B-SAFE 300 filling pressure",
                ],
                "kind": "headline_vs_later_technical_data",
            }
        ],
        "B21: headline/technical-data contradiction contract changed",
    )


def _fact_values(expectations: Mapping[str, Any], predicate: str) -> Any:
    facts = expectations.get("facts") or []
    matches = [
        fact
        for fact in facts
        if isinstance(fact, Mapping) and fact.get("predicate") == predicate
    ]
    _require(len(matches) == 1, f"expected one {predicate!r} fact")
    fact = matches[0]
    if "values_by_setting" in fact:
        return fact["values_by_setting"]
    if "values" in fact:
        return fact["values"]
    return fact.get("value")


def _verify_source_contract(
    cases: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> int:
    _require(
        contract.get("contract_kind") == "bauer-v3-original-source-corpus",
        "source contract has the wrong contract_kind",
    )
    sources = contract.get("sources")
    _require(isinstance(sources, list), "source contract sources must be an array")
    by_external_id = {
        str(source.get("external_file_id") or ""): source
        for source in sources
        if isinstance(source, Mapping)
    }
    unique_checked: set[str] = set()
    for case in cases:
        entries = [
            case,
            *(case.get("additional_source_expectations") or []),
        ]
        for entry_index, entry in enumerate(entries):
            label = (
                str(case["case_id"])
                if entry_index == 0
                else f"{case['case_id']}.source[{entry_index}]"
            )
            identity = entry["source_identity"]
            external_id = str(identity["external_file_id"])
            source = by_external_id.get(external_id)
            _require(
                source is not None,
                f"{label}: source identity is absent from source contract",
            )
            for field, contract_field in (
                ("expected_sha256", "expected_sha256"),
                ("logical_path", "logical_path"),
                ("declared_media_type", "declared_media_type"),
            ):
                _require(
                    identity[field] == source.get(contract_field),
                    f"{label}: {field} disagrees with source contract",
                )
            _require(
                source.get("is_original") is True
                and source.get("is_derivative") is False,
                f"{label}: fixture must bind an original source",
            )
            unique_checked.add(external_id)
    return len(unique_checked)


def _reject_forbidden_bindings(value: Any, path: str = "fixture") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in FORBIDDEN_BINDING_KEYS:
                raise DifficultSourceFixtureError(
                    f"{path}.{key} is a forbidden production/runtime binding"
                )
            _reject_forbidden_bindings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_bindings(item, f"{path}[{index}]")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DifficultSourceFixtureError(f"{label} must be an object")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DifficultSourceFixtureError(
            f"{label} must be an array of non-empty strings"
        )
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DifficultSourceFixtureError(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify portable interim B08/B11/B20/B21 fixtures against "
            "the frozen V3 source contract without opening source files."
        )
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--source-contract",
        type=Path,
        default=DEFAULT_SOURCE_CONTRACT,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        fixture = _mapping(load_json(args.fixture), "fixture")
        contract = _mapping(
            load_json(args.source_contract),
            "source contract",
        )
        result = verify_bundle(fixture, source_contract=contract)
    except DifficultSourceFixtureError as error:
        print(f"fixture verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
