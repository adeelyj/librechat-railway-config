from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.indexing import ProjectionBuilder


REPRESENTATIVE_CASES = ("B16", "B17", "B29")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    args = _parser().parse_args()
    fixture_bytes = args.fixture.read_bytes()
    fixture_bundle = json.loads(fixture_bytes)
    compiler = CanonicalCompiler()
    projections = ProjectionBuilder()
    items = []
    for case_id in REPRESENTATIVE_CASES:
        fixture = next(
            item
            for item in fixture_bundle["documents"]
            if case_id in item["case_ids"]
        )
        source = fixture["source"]
        path = args.source_root.joinpath(
            *source["logical_path"].split("/")
        )
        result = compiler.compile(
            path.read_bytes(),
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
        )
        if result.status != "published" or result.document is None:
            raise RuntimeError(
                f"representative compilation failed for {case_id}"
            )
        document = result.document
        built = projections.build(document)
        selected = next(
            (
                item
                for item in built
                if item.projection_type
                in {"table_row", "fact", "metadata"}
                and (
                    case_id != "B29"
                    or item.subject == "K 22 – K 28 SERIES"
                )
            ),
            built[0],
        )
        items.append(
            {
                "case_id": case_id,
                "external_source_id": source["external_file_id"],
                "content_sha256": document.source_sha256,
                "byte_size": source["byte_size"],
                "media_type": document.media_type,
                "source_filename": document.source_filename,
                "canonical_document_id": document.document_id,
                "canonical_sha256": _digest(document.to_json()),
                "canonical_schema_version": str(document.schema_version),
                "parser_identity": (
                    f"{document.parser_id}@{document.parser_version}"
                ),
                "block_count": len(document.blocks),
                "table_count": len(document.tables),
                "cell_count": sum(
                    len(table.cells) for table in document.tables
                ),
                "fact_count": len(document.facts),
                "projection_count": len(built),
                "representative_projection": {
                    "projection_id": selected.projection_id,
                    "projection_type": selected.projection_type,
                    "projection_schema": selected.projection_schema,
                    "search_text": selected.search_text,
                    "search_text_sha256": selected.search_text_sha256,
                    "exact_terms": list(selected.exact_terms),
                    "canonical_evidence_ids": list(
                        selected.canonical_evidence_ids
                    ),
                    "subject": selected.subject,
                    "predicate": selected.predicate,
                },
            }
        )
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-representative-compilation",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "locked_holdout_opened": False,
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "case_ids": list(REPRESENTATIVE_CASES),
        "document_count": len(items),
        "totals": {
            "blocks": sum(item["block_count"] for item in items),
            "tables": sum(item["table_count"] for item in items),
            "cells": sum(item["cell_count"] for item in items),
            "facts": sum(item["fact_count"] for item in items),
            "projections": sum(
                item["projection_count"] for item in items
            ),
        },
        "documents": items,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
