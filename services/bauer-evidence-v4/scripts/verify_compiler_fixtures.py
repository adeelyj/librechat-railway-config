from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bauer_evidence_v4.compilation import CanonicalCompiler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile and verify the unlocked Bauer V4 difficult documents."
    )
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    bundle = json.loads(args.fixture.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    results: list[dict[str, Any]] = []
    passed = True
    for fixture in bundle["documents"]:
        source = fixture["source"]
        path = args.source_root.joinpath(*source["logical_path"].split("/"))
        payload = path.read_bytes()
        result = compiler.compile(
            payload,
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
            enforce_gate=False,
        )
        passed = passed and result.status == "published"
        results.append(
            {
                "fixture_id": fixture["fixture_id"],
                "case_ids": fixture["case_ids"],
                "source_path": source["logical_path"],
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "status": result.status,
                "selected_parser_id": result.selected_parser_id,
                "canonical_sha256": (
                    hashlib.sha256(result.document.to_json().encode("utf-8")).hexdigest()
                    if result.document is not None
                    else None
                ),
                "table_count": (
                    len(result.document.tables)
                    if result.document is not None
                    else 0
                ),
                "typed_cell_count": (
                    sum(
                        cell.numeric_value is not None
                        for table in result.document.tables
                        for cell in table.cells
                    )
                    if result.document is not None
                    else 0
                ),
                "fact_count": (
                    len(result.document.facts)
                    if result.document is not None
                    else 0
                ),
                "candidates": [
                    {
                        "parser_id": candidate.parser_id,
                        "status": candidate.quality.status,
                        "semantic_score": candidate.quality.semantic_score,
                        "issues": [
                            {
                                "code": issue.code,
                                "severity": issue.severity,
                            }
                            for issue in candidate.quality.issues
                        ],
                        "error_type": (
                            candidate.error.split(":", 1)[0]
                            if candidate.error
                            else None
                        ),
                    }
                    for candidate in result.candidates
                ],
            }
        )
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-compiler-fixture-verification",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": passed,
        "locked_holdout_opened": False,
        "source_contract_sha256": bundle["source_contract_sha256"],
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "results": results,
    }
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
