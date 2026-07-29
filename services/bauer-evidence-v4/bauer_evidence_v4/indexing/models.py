from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from ..canonical.models import canonical_data


@dataclass(frozen=True, slots=True)
class SearchProjection:
    projection_id: str
    projection_type: Literal["metadata", "passage", "table_row", "fact"]
    projection_schema: str
    source_sha256: str
    source_path: str
    title: str
    source_filename: str
    document_number: str | None
    language: str | None
    search_text: str
    search_text_sha256: str
    exact_terms: tuple[str, ...]
    canonical_evidence_ids: tuple[str, ...]
    physical_page: int | None = None
    printed_page: str | None = None
    section_path: tuple[str, ...] = ()
    table_id: str | None = None
    row_index: int | None = None
    subject: str | None = None
    predicate: str | None = None
    numeric_value: Decimal | None = None
    minimum_value: Decimal | None = None
    maximum_value: Decimal | None = None
    unit_raw: str | None = None
    unit_ucum: str | None = None
    qualifiers: tuple[tuple[str, str], ...] = ()

    def to_data(self) -> dict[str, Any]:
        return canonical_data(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
