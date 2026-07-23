from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = REPO_ROOT / "services" / "rag-api-custom"
EVAL_ROOT = REPO_ROOT / "evals" / "bauer-rag-v2"
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_rag_v2.extraction import normalize_for_search, parse_markdown_document  # noqa: E402


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def contains(text: str, value: Any) -> bool:
    return not value or normalize_for_search(str(value)) in normalize_for_search(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse and validate the frozen 373-file Bauer V2 corpus without a database."
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(r"D:\02_Code\LibreChat_Setup\tmp\corpora\bauer-kompressoren"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=EVAL_ROOT / "reports" / "corpus-inspection.json",
    )
    args = parser.parse_args()
    if not args.corpus.is_dir():
        parser.error(f"Corpus directory does not exist: {args.corpus}")

    manifest = load_json(EVAL_ROOT / "baselines" / "corpus-manifest.json")
    evidence_gold = load_json(EVAL_ROOT / "gold" / "evidence.yaml")
    by_id = {record["file_id"]: record for record in manifest["records"]}
    parsed_by_id = {}
    errors = []
    checksum_mismatches = []
    namespace_mismatches = []
    languages: Counter[str] = Counter()
    source_types: Counter[str] = Counter()
    entity_kinds: Counter[str] = Counter()
    table_parsers: Counter[str] = Counter()
    totals: Counter[str] = Counter()

    for index, record in enumerate(manifest["records"], start=1):
        path = args.corpus / record["filename"]
        try:
            payload = path.read_bytes()
            actual_checksum = sha256_bytes(payload)
            if actual_checksum != record["checksum"]:
                checksum_mismatches.append(
                    {
                        "file_id": record["file_id"],
                        "expected": record["checksum"],
                        "actual": actual_checksum,
                    }
                )
            if record["authorization_namespace"] != manifest["authorization_namespace"]:
                namespace_mismatches.append(record["file_id"])
            document = parse_markdown_document(
                text=payload.decode("utf-8-sig"),
                file_id=record["file_id"],
                checksum=actual_checksum,
                filename=record["filename"],
                namespace=record["authorization_namespace"],
                source_type=record["source_type"],
            )
            parsed_by_id[record["file_id"]] = document
            languages[document.language] += 1
            source_types[document.source_type] += 1
            totals["documents"] += 1
            totals["chunks"] += len(document.chunks)
            totals["entities"] += len(document.entities)
            totals["documents_with_publication_date"] += bool(
                document.publication_date
            )
            totals["documents_with_media"] += bool(document.media)
            totals["documents_with_component_categories"] += bool(
                document.component_categories
            )
            totals["documents_with_document_number"] += bool(
                document.document_number
            )
            totals["documents_with_certificate"] += bool(document.certificate)
            for entity in document.entities:
                entity_kinds[entity.kind] += 1
            for chunk in document.chunks:
                totals[f"{chunk.chunk_kind}_chunks"] += 1
                if chunk.chunk_kind == "table_row":
                    table_parsers[str(chunk.metadata.get("parser"))] += 1
                    if chunk.footnotes:
                        totals["table_rows_with_footnotes"] += 1
            if index % 50 == 0 or index == len(manifest["records"]):
                print(f"parsed {index}/{len(manifest['records'])}")
        except Exception as error:
            errors.append(
                {
                    "file_id": record["file_id"],
                    "filename": record["filename"],
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    evidence_checks = []
    for case in evidence_gold["cases"]:
        for required in case["required_evidence"]:
            document = parsed_by_id.get(required["file_id"])
            record = by_id.get(required["file_id"])
            raw_text = ""
            if record:
                raw_text = (args.corpus / record["filename"]).read_text(
                    encoding="utf-8-sig"
                )
            matching_rows = (
                [
                    chunk
                    for chunk in document.chunks
                    if chunk.chunk_kind == "table_row"
                    and contains(chunk.row_label, required.get("row"))
                ]
                if document and required.get("row") and required.get("table")
                else []
            )
            checks = {
                "file_present": record is not None and document is not None,
                "filename_exact": bool(record)
                and record["filename"] == required["filename"],
                "page_present": required.get("page") is None
                or f"Page {required['page']}" in raw_text,
                "section_present": contains(raw_text, required.get("section")),
                "table_present": contains(raw_text, required.get("table")),
                "row_present": contains(raw_text, required.get("row")),
                "row_structured": not (
                    required.get("row") and required.get("table")
                )
                or bool(matching_rows),
            }
            evidence_checks.append(
                {
                    "case_id": case["id"],
                    "file_id": required["file_id"],
                    "expected_location": required,
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            )

    required_languages = {"de", "en"}
    critical_failures = [
        *(["document_parse_error"] if errors else []),
        *(["checksum_mismatch"] if checksum_mismatches else []),
        *(["namespace_mismatch"] if namespace_mismatches else []),
        *(
            ["missing_german_or_english"]
            if not required_languages <= set(languages)
            else []
        ),
        *(["no_table_rows"] if not totals["table_row_chunks"] else []),
        *(
            ["no_identifier_entities"]
            if not entity_kinds["document_number"]
            else []
        ),
        *(
            ["no_certificate_entities"]
            if not entity_kinds["certificate"]
            else []
        ),
        *(
            ["no_table_footnotes"]
            if not totals["table_rows_with_footnotes"]
            else []
        ),
        *(
            ["structured_gold_row_missing"]
            if any(
                not item["checks"]["row_structured"] for item in evidence_checks
            )
            else []
        ),
    ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": str(args.corpus.resolve()),
        "manifest_file_count": manifest["file_count"],
        "authorization_namespace": manifest["authorization_namespace"],
        "totals": dict(totals),
        "languages": dict(languages),
        "source_types": dict(source_types),
        "entity_kinds": dict(entity_kinds),
        "table_parsers": dict(table_parsers),
        "checksum_mismatches": checksum_mismatches,
        "namespace_mismatches": namespace_mismatches,
        "parse_errors": errors,
        "gold_evidence_checks": evidence_checks,
        "gold_evidence_passed": sum(item["passed"] for item in evidence_checks),
        "gold_evidence_total": len(evidence_checks),
        "critical_failures": critical_failures,
        "passed": not critical_failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in ("totals", "languages", "gold_evidence_passed", "gold_evidence_total", "critical_failures", "passed")}, indent=2))
    print(args.output)
    if critical_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
