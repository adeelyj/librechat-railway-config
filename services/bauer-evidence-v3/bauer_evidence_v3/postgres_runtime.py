from __future__ import annotations

import json
import math
import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Iterator, Mapping, Sequence

from .migrations import expected_schema_version
from .db_security import verify_runtime_database_role
from .models import BoundingBox, EvidenceItem, ReleaseStatus, SourceCoordinate
from .planner import (
    NumericConstraint,
    QueryPlan,
    RetrievalChannel,
    normalize_text,
)
from .releases import ReleaseError
from .retrieval import EmbeddingProvider, RetrievalResult


ConnectionFactory = Callable[[], Any]
MAX_AUTHORIZED_SOURCES = 1_000
EXPECTED_EMBEDDING_DIMENSIONS = 1_024

_CHANNEL_WEIGHT = {
    RetrievalChannel.EXACT: 3.0,
    RetrievalChannel.FACT: 2.8,
    RetrievalChannel.TABLE: 2.5,
    RetrievalChannel.LEXICAL: 1.2,
    RetrievalChannel.SEMANTIC: 0.8,
    RetrievalChannel.NAVIGATION: 0.7,
}
_STRUCTURED_TYPES = {"fact", "table_row", "table_cell"}
_PHYSICAL_PAGE_PATTERN = re.compile(
    r"\bphysical\s+page\s+([1-9][0-9]{0,5})\b",
    flags=re.IGNORECASE,
)
_CATALOG_IDENTIFIER_PATTERN = re.compile(
    r"^B-[A-Z][A-Z0-9]{1,29}(?:-[A-Z0-9]{1,20})*$",
    flags=re.IGNORECASE,
)
_CATALOG_IDENTIFIER_FIND_PATTERN = re.compile(
    r"\bB-[A-Z][A-Z0-9]{1,29}(?:-[A-Z0-9]{1,20})*\b",
    flags=re.IGNORECASE,
)
_CONTEXT_TERM_PATTERN = re.compile(r"[a-z0-9]+", flags=re.IGNORECASE)
_CATALOG_FAMILY_EVIDENCE_PATTERN = re.compile(
    r"\b(?:PE-VE|MINI-VERTICUS|VERTICUS)\b",
    flags=re.IGNORECASE,
)
_NUMERIC_EVIDENCE_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z])(\d+(?:[.,]\d+)?)\s*"
    r"(bar|l/min|l\s+min-1|m3/h|kw|rpm)\b",
    flags=re.IGNORECASE,
)
_SHARED_NUMERIC_EVIDENCE_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z])(\d+(?:[.,]\d+)?)\s*/\s*"
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(bar|l/min|l\s+min-1|m3/h|kw|rpm)\b",
    flags=re.IGNORECASE,
)
_RAW_NUMERIC_EVIDENCE_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z])\d+(?:[.,]\d+)?(?![A-Za-z])"
)

_LEXICAL_NOISE_TERMS = frozenset(
    {
        "approximately",
        "based",
        "both",
        "cite",
        "documents",
        "each",
        "exact",
        "explain",
        "find",
        "give",
        "only",
        "public",
        "report",
        "requirement",
        "source",
        "sources",
        "supporting",
        "uploaded",
    }
)
_CONTEXT_STOP_TERMS = frozenset(
    {
        "all",
        "and",
        "any",
        "both",
        "cite",
        "distinct",
        "documented",
        "exact",
        "files",
        "from",
        "later",
        "not",
        "only",
        "preserve",
        "report",
        "source",
        "sources",
        "summarize",
        "that",
        "the",
        "three",
        "using",
        "values",
        "with",
    }
)
_LEXICAL_PHRASES = (
    ("automatic priority valve", "automatic selector unit"),
    ("maximum operating pressure", "maximum allowable working pressure"),
    ("shutdown pressure", "shut down pressure"),
    ("refrigeration dryer", "refrigeration dryer"),
    ("filter-cartridge", "filter cartridge"),
    ("free-air-delivery", "free air delivery"),
    ("motor-power", "motor power"),
    ("product overview", "product overview"),
    ("product families", "product families"),
    ("bm series", "bm series"),
    ("mini-verticus", "mini verticus"),
)
_PRODUCT_RANGE_PATTERN = re.compile(
    r"\b([a-z]{1,8})\s+(\d+(?:[.-]\d+)?)\s+"
    r"\1\s+(\d+(?:[.-]\d+)?)\s+series\b",
    flags=re.IGNORECASE,
)


class PostgresRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PinnedRelease:
    release_id: str
    tenant_id: str
    knowledge_base_id: str
    status: ReleaseStatus = ReleaseStatus.READY


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    ready: bool
    current_migration_version: int
    expected_migration_version: int
    applied_migration_count: int
    active_release_count: int
    selected_release_count: int = 0
    release_selection: str = "active"


@dataclass(frozen=True, slots=True)
class _Candidate:
    evidence: EvidenceItem
    unit_type: str
    raw_score: float
    channel: RetrievalChannel
    reason: str


def _numeric_constraint_document(
    constraint: NumericConstraint,
) -> dict[str, str | None]:
    return {
        "comparator": constraint.comparator.value,
        "lower_value": str(constraint.lower_value),
        "upper_value": (
            str(constraint.upper_value)
            if constraint.upper_value is not None
            else None
        ),
        "unit": constraint.unit,
    }


def _required_numeric_groups(plan: QueryPlan) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    if plan.numeric_constraints:
        # Numeric values mentioned in an ordinary query are retrieval clues,
        # not a demand that every value occur in one evidence unit.  A
        # comparison or catalogue question commonly spans multiple rows or
        # paragraphs, so model those mentions as alternatives within one
        # query group.  Explicit mandatory_numeric_constraints below remain
        # independent groups and therefore retain their all-groups-required
        # semantics in the typed fact/table SQL.
        groups.append(
            {
                "name": "query",
                "alternatives": [
                    _numeric_constraint_document(constraint)
                    for constraint in plan.numeric_constraints
                ],
            }
        )
    groups.extend(
        {
            "name": name,
            "alternatives": [
                _numeric_constraint_document(constraint)
                for constraint in constraints
            ],
        }
        for name, constraints in sorted(
            plan.mandatory_numeric_constraints.items()
        )
    )
    return groups


def _json_parameter(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _requested_physical_pages(plan: QueryPlan) -> frozenset[int]:
    return frozenset(
        int(match.group(1))
        for match in _PHYSICAL_PAGE_PATTERN.finditer(plan.query)
    )


def _catalog_identifiers(plan: QueryPlan) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            normalized
            for identifier in plan.identifiers
            if _CATALOG_IDENTIFIER_PATTERN.fullmatch(identifier)
            for normalized in (normalize_text(identifier),)
            if normalized
        )
    )


def _discovered_catalog_identifiers(
    plan: QueryPlan,
    channel_candidates: Mapping[
        RetrievalChannel,
        Sequence[_Candidate],
    ],
) -> tuple[str, ...]:
    """Promote consistently retrieved product names for discovery queries."""

    if plan.identifiers:
        return ()
    discovery_cues = (
        "do not know",
        "find",
        "identify",
        "locate",
        "which",
        "what",
    )
    if not any(cue in plan.normalized_query for cue in discovery_cues):
        return ()

    occurrence_count: dict[str, int] = defaultdict(int)
    channel_count: dict[str, set[RetrievalChannel]] = defaultdict(set)
    best_rank: dict[str, int] = {}
    for channel in (
        RetrievalChannel.FACT,
        RetrievalChannel.TABLE,
        RetrievalChannel.LEXICAL,
        RetrievalChannel.SEMANTIC,
        RetrievalChannel.NAVIGATION,
    ):
        for rank, candidate in enumerate(
            channel_candidates.get(channel, ())[:20],
            start=1,
        ):
            terms = {
                match.group(0).upper()
                for match in _CATALOG_IDENTIFIER_FIND_PATTERN.finditer(
                    " ".join(
                        (
                            candidate.evidence.title,
                            candidate.evidence.content,
                        )
                    )
                )
            }
            for term in terms:
                occurrence_count[term] += 1
                channel_count[term].add(channel)
                best_rank[term] = min(best_rank.get(term, rank), rank)

    eligible = [
        term
        for term, count in occurrence_count.items()
        if count >= 2 or len(channel_count[term]) >= 2
    ]
    return tuple(
        sorted(
            eligible,
            key=lambda term: (
                -len(channel_count[term]),
                -occurrence_count[term],
                best_rank[term],
                term,
            ),
        )[:4]
    )


def _identifier_context_terms(plan: QueryPlan) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term
            for token in plan.tokens
            for term in _CONTEXT_TERM_PATTERN.findall(token.casefold())
            if len(term) > 1 and term not in _CONTEXT_STOP_TERMS
        )
    )


def _numeric_evidence_tokens(content: str) -> frozenset[str]:
    tokens = {
        (
            f"{match.group(1).replace(',', '.').casefold()}:"
            f"{match.group(2).casefold()}"
        )
        for match in _NUMERIC_EVIDENCE_TOKEN_PATTERN.finditer(content)
    }
    for match in _SHARED_NUMERIC_EVIDENCE_TOKEN_PATTERN.finditer(
        content
    ):
        normalized_unit = match.group(3).casefold()
        tokens.update(
            {
                (
                    f"{match.group(1).replace(',', '.').casefold()}:"
                    f"{normalized_unit}"
                ),
                (
                    f"{match.group(2).replace(',', '.').casefold()}:"
                    f"{normalized_unit}"
                ),
            }
        )
    return frozenset(tokens)


def _raw_numeric_evidence_tokens(content: str) -> frozenset[str]:
    return frozenset(
        token.replace(",", ".")
        for token in _RAW_NUMERIC_EVIDENCE_TOKEN_PATTERN.findall(
            content
        )
    )


def _lexical_query_terms(plan: QueryPlan) -> tuple[str, ...]:
    anchors: list[str] = [
        search_phrase
        for query_phrase, search_phrase in _LEXICAL_PHRASES
        if query_phrase in plan.normalized_query
    ]
    anchors.extend(
        normalize_text(identifier)
        for identifier in plan.identifiers
    )
    for match in _PRODUCT_RANGE_PATTERN.finditer(plan.normalized_query):
        family = match.group(1)
        anchors.extend(
            (
                f"{family} {match.group(2)}",
                f"{family} {match.group(3)}",
            )
        )
    anchors.extend(
        normalize_text(mention.raw)
        for mention in plan.numeric_mentions
        if mention.unit is not None
    )

    weighted: list[tuple[int, int, str]] = []
    for position, value in enumerate(plan.tokens):
        normalized = normalize_text(value).strip(".")
        if (
            not normalized
            or normalized in _LEXICAL_NOISE_TERMS
            or (
                normalized.isdigit()
                and len(normalized) == 1
            )
            or (
                len(normalized) < 3
                and not any(ch.isdigit() for ch in normalized)
                and normalized not in {"bm", "gi", "k", "pe"}
            )
        ):
            continue
        score = len(normalized)
        if any(ch.isdigit() for ch in normalized):
            score += 100
        if "-" in normalized or "/" in normalized:
            score += 80
        if normalized in {"bm", "gi", "gib", "pe"}:
            score += 90
        if normalized in {
            "capacity",
            "delivery",
            "medium",
            "power",
            "pressure",
            "temperature",
        }:
            score += 50
        weighted.append((score, -position, normalized))
    ordered = [
        value
        for _score, _position, value in sorted(weighted, reverse=True)
    ]
    return (
        tuple(dict.fromkeys((*anchors, *ordered)))[:8]
        or (plan.normalized_query,)
    )


_AUTHORIZED_UNITS_CTE = """
WITH authorized_units AS (
    SELECT
        unit.search_unit_id::text AS evidence_id,
        unit.release_id::text AS release_id,
        kb.tenant_id::text AS tenant_id,
        kb.kb_id::text AS knowledge_base_id,
        source_row.source_id::text AS source_document_id,
        source_row.external_file_id,
        source_row.source_type,
        source_version.source_version_id::text AS source_version_id,
        source_version.sha256::text AS source_sha256,
        coalesce(
            source_version.discovered_metadata ->> 'title',
            source_version.filename
        ) AS title,
        source_version.discovered_metadata AS source_metadata,
        unit.unit_type,
        unit.display_text,
        unit.search_text,
        unit.search_vector,
        unit.embedding,
        unit.metadata AS unit_metadata,
        unit.is_citable,
        unit.generated_summary,
        coalesce(unit.page_start, 1)::integer AS page_number,
        NULL::text AS printed_page_label,
        unit.section_id::text AS section_id,
        NULL::text AS block_id,
        unit.table_id::text AS table_id,
        unit.table_row_index::integer AS table_row_index,
        NULL::text AS cell_id,
        NULL::integer AS char_start,
        NULL::integer AS char_end,
        NULL::double precision AS x0,
        NULL::double precision AS y0,
        NULL::double precision AS x1,
        NULL::double precision AS y1,
        unit.artifact_set_id,
        unit.primary_provenance_id
    FROM bauer_rag_v3.search_units unit
    JOIN bauer_rag_v3.release_sources member
      ON member.release_id = unit.release_id
     AND member.source_id = unit.source_id
     AND member.source_version_id = unit.source_version_id
     AND member.artifact_set_id = unit.artifact_set_id
    JOIN bauer_rag_v3.knowledge_releases release_row
      ON release_row.release_id = unit.release_id
     AND release_row.kb_id = unit.kb_id
    JOIN bauer_rag_v3.knowledge_bases kb
      ON kb.kb_id = release_row.kb_id
    JOIN bauer_rag_v3.sources source_row
      ON source_row.source_id = unit.source_id
     AND source_row.kb_id = kb.kb_id
    JOIN bauer_rag_v3.source_versions source_version
      ON source_version.source_version_id = unit.source_version_id
     AND source_version.source_id = source_row.source_id
    WHERE unit.release_id = %s::uuid
      AND release_row.status = %s::text
      AND release_row.kb_id = %s::uuid
      AND kb.tenant_id = %s::uuid
      AND (
          source_row.source_id::text = ANY (%s::text[])
          OR source_row.external_file_id = ANY (%s::text[])
      )
      AND unit.is_citable
      AND NOT unit.generated_summary
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_each(%s::jsonb)
              AS required_constraint(constraint_name, allowed_values)
          WHERE NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  required_constraint.allowed_values
              ) AS allowed_value(value)
              WHERE position(
                  allowed_value.value IN lower(
                      concat_ws(
                          ' ',
                          unit.search_text,
                          unit.display_text,
                          unit.metadata::text
                      )
                  )
              ) > 0
          )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(%s::text[]) AS forbidden_value(value)
          WHERE position(
              forbidden_value.value IN lower(
                  concat_ws(
                      ' ',
                      unit.search_text,
                      unit.display_text,
                      unit.metadata::text
                  )
              )
          ) > 0
      )
)
"""


_HYDRATE_CANDIDATES_SQL = """
/* v3:hydrate:candidates */
SELECT
    unit.search_unit_id::text AS evidence_id,
    coalesce(unit.page_start, source_page.page_number, 1)::integer
        AS page_number,
    source_page.page_label AS printed_page_label,
    coalesce(unit.section_id, source_block.section_id)::text AS section_id,
    provenance.block_id::text AS block_id,
    coalesce(unit.table_id, source_cell.table_id)::text AS table_id,
    coalesce(unit.table_row_index, source_cell.row_index)::integer
        AS table_row_index,
    provenance.cell_id::text AS cell_id,
    provenance.char_start,
    provenance.char_end,
    coalesce(provenance.x0, source_block.x0, source_cell.x0) AS x0,
    coalesce(provenance.y0, source_block.y0, source_cell.y0) AS y0,
    coalesce(provenance.x1, source_block.x1, source_cell.x1) AS x1,
    coalesce(provenance.y1, source_block.y1, source_cell.y1) AS y1,
    coalesce(
        source_section.metadata -> 'canonical_path',
        CASE
            WHEN coalesce(
                unit.page_start,
                source_page.page_number
            ) IS NOT NULL
            THEN jsonb_build_array(
                'Page ' || coalesce(
                    unit.page_start,
                    source_page.page_number
                )::text
            )
            ELSE '[]'::jsonb
        END
    ) AS section_path,
    coalesce(
        nullif(source_table.title, ''),
        nullif(source_table.caption, '')
    ) AS table_title,
    structured_row.row_label,
    structured_row.table_headers,
    structured_row.table_values,
    structured_row.table_units,
    structured_row.footnotes
FROM bauer_rag_v3.search_units unit
LEFT JOIN bauer_rag_v3.provenance_spans provenance
  ON provenance.provenance_id = unit.primary_provenance_id
 AND provenance.artifact_set_id = unit.artifact_set_id
LEFT JOIN bauer_rag_v3.blocks source_block
  ON source_block.block_id = provenance.block_id
 AND source_block.artifact_set_id = unit.artifact_set_id
LEFT JOIN bauer_rag_v3.table_cells source_cell
  ON source_cell.cell_id = provenance.cell_id
 AND source_cell.artifact_set_id = unit.artifact_set_id
LEFT JOIN bauer_rag_v3.pages source_page
  ON source_page.artifact_set_id = unit.artifact_set_id
 AND source_page.page_id = coalesce(
      provenance.page_id,
      source_block.page_id,
      source_cell.page_id,
      unit.page_id
 )
LEFT JOIN bauer_rag_v3.tables source_table
  ON source_table.artifact_set_id = unit.artifact_set_id
 AND source_table.table_id = coalesce(
      unit.table_id,
      source_cell.table_id
 )
LEFT JOIN bauer_rag_v3.sections source_section
  ON source_section.artifact_set_id = unit.artifact_set_id
 AND source_section.section_id = coalesce(
      unit.section_id,
      source_block.section_id,
      source_table.section_id
 )
LEFT JOIN LATERAL (
    SELECT
        (
            array_agg(
                table_cell.raw_text
                ORDER BY
                    CASE table_cell.cell_role
                        WHEN 'row_header' THEN 0
                        WHEN 'stub' THEN 1
                        WHEN 'body' THEN 2
                        ELSE 3
                    END,
                    table_cell.column_index,
                    table_cell.cell_id
            ) FILTER (
                WHERE table_cell.row_index =
                      unit.table_row_index
                  AND nullif(table_cell.raw_text, '') IS NOT NULL
            )
        )[1] AS row_label,
        coalesce(
            jsonb_agg(
                table_cell.raw_text
                ORDER BY
                    table_cell.row_index,
                    table_cell.column_index,
                    table_cell.cell_id
            ) FILTER (
                WHERE (
                    table_cell.cell_role = 'header'
                    OR table_cell.row_index <
                       source_table.header_row_count
                )
                  AND nullif(table_cell.raw_text, '') IS NOT NULL
            ),
            '[]'::jsonb
        ) AS table_headers,
        coalesce(
            jsonb_agg(
                table_cell.raw_text
                ORDER BY
                    table_cell.column_index,
                    table_cell.cell_id
            ) FILTER (
                WHERE table_cell.row_index =
                      unit.table_row_index
                  AND table_cell.cell_role <> 'note'
                  AND nullif(table_cell.raw_text, '') IS NOT NULL
            ),
            '[]'::jsonb
        ) AS table_values,
        coalesce(
            jsonb_agg(DISTINCT coalesce(
                table_cell.unit_ucum,
                table_cell.unit_raw
            )) FILTER (
                WHERE table_cell.row_index =
                      unit.table_row_index
                  AND coalesce(
                      table_cell.unit_ucum,
                      table_cell.unit_raw
                  ) IS NOT NULL
            ),
            '[]'::jsonb
        ) AS table_units,
        coalesce(
            jsonb_agg(
                table_cell.raw_text
                ORDER BY
                    table_cell.row_index,
                    table_cell.column_index,
                    table_cell.cell_id
            ) FILTER (
                WHERE table_cell.cell_role = 'note'
                  AND nullif(table_cell.raw_text, '') IS NOT NULL
            ),
            '[]'::jsonb
        ) AS footnotes
    FROM bauer_rag_v3.table_cells table_cell
    WHERE table_cell.artifact_set_id = unit.artifact_set_id
      AND table_cell.table_id = source_table.table_id
) structured_row ON TRUE
WHERE unit.release_id = %s::uuid
  AND unit.search_unit_id = ANY (%s::uuid[])
"""


_EXACT_SQL = """
/* v3:channel:exact */
WITH request_scope AS (
    SELECT
        %s::uuid AS release_id,
        %s::text AS release_status,
        %s::uuid AS knowledge_base_id,
        %s::uuid AS tenant_id,
        %s::text[] AS source_ids,
        %s::text[] AS external_file_ids,
        %s::jsonb AS mandatory_text_constraints,
        %s::text[] AS forbidden_claim_values
),
query_terms AS (
    SELECT unnest(%s::text[]) AS term
),
matched_units AS MATERIALIZED (
    SELECT
        exact_term.search_unit_id,
        min(query_term.term) AS matched_term
    FROM bauer_rag_v3.exact_terms exact_term
    CROSS JOIN request_scope request
    JOIN query_terms query_term
      ON query_term.term = exact_term.normalized_term
    WHERE exact_term.release_id = request.release_id
    GROUP BY exact_term.search_unit_id
)
SELECT
    unit.search_unit_id::text AS evidence_id,
    unit.release_id::text AS release_id,
    kb.tenant_id::text AS tenant_id,
    kb.kb_id::text AS knowledge_base_id,
    source_row.source_id::text AS source_document_id,
    source_row.external_file_id,
    source_row.source_type,
    source_version.source_version_id::text AS source_version_id,
    source_version.sha256::text AS source_sha256,
    coalesce(
        source_version.discovered_metadata ->> 'title',
        source_version.filename
    ) AS title,
    source_version.discovered_metadata AS source_metadata,
    unit.unit_type,
    unit.display_text,
    unit.search_text,
    unit.metadata AS unit_metadata,
    unit.is_citable,
    unit.generated_summary,
    coalesce(unit.page_start, 1)::integer AS page_number,
    NULL::text AS printed_page_label,
    unit.section_id::text AS section_id,
    NULL::text AS block_id,
    unit.table_id::text AS table_id,
    unit.table_row_index::integer AS table_row_index,
    NULL::text AS cell_id,
    NULL::integer AS char_start,
    NULL::integer AS char_end,
    NULL::double precision AS x0,
    NULL::double precision AS y0,
    NULL::double precision AS x1,
    NULL::double precision AS y1,
    unit.artifact_set_id,
    unit.primary_provenance_id,
    1.0::double precision AS raw_score,
    'exact'::text AS channel,
    'exact:' || matched.matched_term AS reason,
    NULL::text AS channel_unit
FROM matched_units matched
JOIN bauer_rag_v3.search_units unit
  ON unit.search_unit_id = matched.search_unit_id
CROSS JOIN request_scope request
JOIN bauer_rag_v3.release_sources member
  ON member.release_id = unit.release_id
 AND member.source_id = unit.source_id
 AND member.source_version_id = unit.source_version_id
 AND member.artifact_set_id = unit.artifact_set_id
JOIN bauer_rag_v3.knowledge_releases release_row
  ON release_row.release_id = unit.release_id
 AND release_row.kb_id = unit.kb_id
JOIN bauer_rag_v3.knowledge_bases kb
  ON kb.kb_id = release_row.kb_id
JOIN bauer_rag_v3.sources source_row
  ON source_row.source_id = unit.source_id
 AND source_row.kb_id = kb.kb_id
JOIN bauer_rag_v3.source_versions source_version
  ON source_version.source_version_id = unit.source_version_id
 AND source_version.source_id = source_row.source_id
WHERE unit.release_id = request.release_id
  AND release_row.status = request.release_status
  AND release_row.kb_id = request.knowledge_base_id
  AND kb.tenant_id = request.tenant_id
  AND (
      source_row.source_id::text = ANY (request.source_ids)
      OR source_row.external_file_id = ANY (request.external_file_ids)
  )
  AND unit.is_citable
  AND NOT unit.generated_summary
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(request.mandatory_text_constraints)
          AS required_constraint(constraint_name, allowed_values)
      WHERE NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(
              required_constraint.allowed_values
          ) AS allowed_value(value)
          WHERE position(
              allowed_value.value IN lower(
                  concat_ws(
                      ' ',
                      unit.search_text,
                      unit.display_text,
                      unit.metadata::text
                  )
              )
          ) > 0
      )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM unnest(request.forbidden_claim_values) AS forbidden_value(value)
      WHERE position(
          forbidden_value.value IN lower(
              concat_ws(
                  ' ',
                  unit.search_text,
                  unit.display_text,
                  unit.metadata::text
              )
          )
      ) > 0
  )
ORDER BY raw_score DESC, evidence_id
LIMIT %s
"""


_IDENTIFIER_CONTEXT_AUTHORIZED_UNITS_CTE = _AUTHORIZED_UNITS_CTE.replace(
    "      AND unit.is_citable",
    """
      AND EXISTS (
          SELECT 1
          FROM candidate_pages candidate_page
          WHERE candidate_page.source_document_id =
                unit.source_id::text
            AND candidate_page.source_version_id =
                unit.source_version_id::text
            AND candidate_page.page_number =
                coalesce(unit.page_start, 1)
      )
      AND unit.is_citable
""",
    1,
).replace(
    "WITH authorized_units AS (",
    """
WITH context_query AS (
    SELECT
        %s::text[] AS identifiers,
        %s::uuid AS release_id,
        %s::text[] AS source_ids,
        %s::text[] AS external_file_ids
),
candidate_sources AS MATERIALIZED (
    SELECT
        exact_term.source_id::text AS source_document_id,
        exact_term.source_version_id::text AS source_version_id,
        catalog_identifier.identifier
    FROM bauer_rag_v3.exact_terms exact_term
    JOIN bauer_rag_v3.sources source_row
      ON source_row.source_id = exact_term.source_id
    CROSS JOIN context_query
    JOIN LATERAL unnest(
        context_query.identifiers
    ) AS catalog_identifier(identifier)
      ON position(
          catalog_identifier.identifier
          IN exact_term.normalized_term
      ) > 0
    WHERE exact_term.release_id = context_query.release_id
      AND (
          exact_term.source_id::text = ANY (
              context_query.source_ids
          )
          OR source_row.external_file_id = ANY (
              context_query.external_file_ids
          )
      )
    GROUP BY
        exact_term.source_id,
        exact_term.source_version_id,
        catalog_identifier.identifier
    UNION
    SELECT
        unit.source_id::text AS source_document_id,
        unit.source_version_id::text AS source_version_id,
        catalog_identifier.identifier
    FROM bauer_rag_v3.search_units unit
    JOIN bauer_rag_v3.sources source_row
      ON source_row.source_id = unit.source_id
    CROSS JOIN context_query
    JOIN LATERAL unnest(
        context_query.identifiers
    ) AS catalog_identifier(identifier)
      ON unit.search_text ILIKE (
          '%%' || catalog_identifier.identifier || '%%'
      )
    WHERE unit.release_id = context_query.release_id
      AND (
          unit.source_id::text = ANY (
              context_query.source_ids
          )
          OR source_row.external_file_id = ANY (
              context_query.external_file_ids
          )
      )
      AND unit.is_citable
      AND NOT unit.generated_summary
    GROUP BY
        unit.source_id,
        unit.source_version_id,
        catalog_identifier.identifier
),
anchor_occurrences AS MATERIALIZED (
    SELECT
        candidate_source.source_document_id,
        candidate_source.source_version_id,
        coalesce(unit.page_start, 1)::integer AS page_number,
        candidate_source.identifier
    FROM candidate_sources candidate_source
    JOIN bauer_rag_v3.search_units unit
      ON unit.source_id::text =
         candidate_source.source_document_id
     AND unit.source_version_id::text =
         candidate_source.source_version_id
    WHERE position(
        candidate_source.identifier IN lower(
            concat_ws(
                ' ',
                unit.search_text,
                unit.display_text,
                unit.metadata::text
            )
        )
    ) > 0
      AND unit.is_citable
      AND NOT unit.generated_summary
    GROUP BY
        candidate_source.source_document_id,
        candidate_source.source_version_id,
        coalesce(unit.page_start, 1),
        candidate_source.identifier
),
candidate_pages AS MATERIALIZED (
    SELECT DISTINCT
        anchor_occurrences.source_document_id,
        anchor_occurrences.source_version_id,
        anchor_occurrences.page_number
    FROM anchor_occurrences
),
authorized_units AS (
""",
    1,
)


_IDENTIFIER_CONTEXT_SQL = (
    "/* v3:channel:identifier_context */\n"
    + _IDENTIFIER_CONTEXT_AUTHORIZED_UNITS_CTE
    + """
, context_scoring AS (
    SELECT
        %s::text[] AS query_terms,
        %s::boolean AS structured_intent,
        %s::integer AS anchor_page_limit
),
ranked_anchor_pages AS MATERIALIZED (
    SELECT
        anchor_occurrences.*,
        row_number() OVER (
            PARTITION BY
                anchor_occurrences.identifier,
                anchor_occurrences.source_document_id
            ORDER BY
                sum(page_term_hits.query_term_hits) DESC,
                count(*) FILTER (
                    WHERE page_unit.unit_type IN (
                        'fact',
                        'table_row',
                        'table_cell'
                    )
                ) DESC,
                count(*) FILTER (
                    WHERE page_unit.search_text ~ '[0-9]'
                ) DESC,
                count(*) DESC,
                anchor_occurrences.source_document_id,
                anchor_occurrences.page_number
        ) AS anchor_rank
    FROM anchor_occurrences
    JOIN authorized_units AS page_unit
      ON page_unit.source_document_id =
         anchor_occurrences.source_document_id
     AND page_unit.source_version_id =
         anchor_occurrences.source_version_id
     AND page_unit.page_number =
         anchor_occurrences.page_number
    CROSS JOIN context_scoring
    CROSS JOIN LATERAL (
        SELECT count(*)::integer AS query_term_hits
        FROM unnest(
            context_scoring.query_terms
        ) AS query_term
        WHERE position(
            query_term IN lower(
                concat_ws(
                    ' ',
                    page_unit.search_text,
                    page_unit.display_text,
                    page_unit.unit_metadata::text
                )
            )
        ) > 0
    ) AS page_term_hits
    GROUP BY
        anchor_occurrences.source_document_id,
        anchor_occurrences.source_version_id,
        anchor_occurrences.page_number,
        anchor_occurrences.identifier
),
anchor_pages AS MATERIALIZED (
    SELECT
        ranked_anchor_pages.source_document_id,
        ranked_anchor_pages.source_version_id,
        ranked_anchor_pages.page_number,
        array_agg(
            ranked_anchor_pages.identifier
            ORDER BY ranked_anchor_pages.identifier
        ) AS anchor_identifiers
    FROM ranked_anchor_pages
    CROSS JOIN context_scoring
    WHERE ranked_anchor_pages.anchor_rank <=
          context_scoring.anchor_page_limit
    GROUP BY
        ranked_anchor_pages.source_document_id,
        ranked_anchor_pages.source_version_id,
        ranked_anchor_pages.page_number
),
scored_context AS (
    SELECT
        authorized_units.*,
        anchor_pages.anchor_identifiers,
        (
            SELECT count(*)::integer
            FROM unnest(anchor_pages.anchor_identifiers) AS identifier
            WHERE position(
                identifier IN lower(
                    concat_ws(
                        ' ',
                        authorized_units.search_text,
                        authorized_units.display_text,
                        authorized_units.unit_metadata::text
                    )
                )
            ) > 0
        ) AS identifier_hits,
        (
            SELECT count(*)::integer
            FROM unnest(context_scoring.query_terms) AS query_term
            WHERE position(
                query_term IN lower(
                    concat_ws(
                        ' ',
                        authorized_units.search_text,
                        authorized_units.display_text,
                        authorized_units.unit_metadata::text
                    )
                )
            ) > 0
        ) AS query_term_hits,
        authorized_units.search_text ~ '[0-9]' AS has_number,
        (
            context_scoring.structured_intent
            AND authorized_units.search_text ~ '[0-9]'
            AND position(
                'bar' IN lower(
                    concat_ws(
                        ' ',
                        authorized_units.search_text,
                        authorized_units.display_text,
                        authorized_units.unit_metadata::text
                    )
                )
            ) > 0
        ) AS has_structured_pressure,
        (
            EXISTS (
                SELECT 1
                FROM unnest(
                    context_scoring.query_terms
                ) AS context_term
                WHERE context_term IN (
                    'compatible',
                    'families',
                    'family'
                )
            )
            AND (
                position(
                    'verticus' IN lower(
                        concat_ws(
                            ' ',
                            authorized_units.search_text,
                            authorized_units.display_text,
                            authorized_units.unit_metadata::text
                        )
                    )
                ) > 0
                OR position(
                    'pe-ve' IN lower(
                        concat_ws(
                            ' ',
                            authorized_units.search_text,
                            authorized_units.display_text,
                            authorized_units.unit_metadata::text
                        )
                    )
                ) > 0
            )
        ) AS has_catalog_family,
        context_scoring.structured_intent
    FROM authorized_units
    JOIN anchor_pages
      ON anchor_pages.source_document_id =
         authorized_units.source_document_id
     AND anchor_pages.source_version_id =
         authorized_units.source_version_id
     AND anchor_pages.page_number = authorized_units.page_number
    CROSS JOIN context_scoring
)
SELECT
    scored_context.*,
    least(
        1.0,
        0.30
        + CASE
              WHEN scored_context.identifier_hits > 0 THEN 0.60
              ELSE 0.0
          END
        + least(scored_context.query_term_hits, 4) * 0.08
        + CASE
              WHEN scored_context.structured_intent
                   AND scored_context.has_number
              THEN 0.18
              ELSE 0.0
          END
        + CASE
              WHEN scored_context.structured_intent
                   AND scored_context.unit_type IN (
                       'fact',
                       'table_row',
                       'table_cell'
                   )
              THEN 0.05
              ELSE 0.0
          END
    )::double precision AS raw_score,
    'exact'::text AS channel,
    'identifier_context:'
        || array_to_string(
            scored_context.anchor_identifiers,
            ','
        ) AS reason,
    scored_context.unit_metadata ->> 'unit' AS channel_unit
FROM scored_context
ORDER BY
    scored_context.has_catalog_family DESC,
    scored_context.has_structured_pressure DESC,
    scored_context.query_term_hits DESC,
    scored_context.identifier_hits DESC,
    scored_context.has_number DESC,
    (
        scored_context.unit_type IN (
            'fact',
            'table_row',
            'table_cell'
        )
    ) DESC,
    raw_score DESC,
    scored_context.source_document_id,
    scored_context.page_number,
    scored_context.evidence_id
LIMIT %s
"""
)


def _fact_sql(superlative: str | None) -> str:
    if superlative == "maximum":
        fact_choice_ordering = (
            "fact_relevance DESC, "
            "fact_numeric_value DESC NULLS LAST, fact_id"
        )
        result_ordering = (
            "raw_score DESC, fact_numeric_value DESC NULLS LAST, evidence_id"
        )
    elif superlative == "minimum":
        fact_choice_ordering = (
            "fact_relevance DESC, "
            "fact_numeric_value ASC NULLS LAST, fact_id"
        )
        result_ordering = (
            "raw_score DESC, fact_numeric_value ASC NULLS LAST, evidence_id"
        )
    else:
        fact_choice_ordering = (
            "fact_relevance DESC, fact_id"
        )
        result_ordering = (
            "raw_score DESC, evidence_id"
        )
    allow_extremum = "TRUE" if superlative else "FALSE"
    return (
        "/* v3:channel:fact */\n"
        + _AUTHORIZED_UNITS_CTE
        + f"""
, fact_query AS (
    SELECT
        %s::jsonb AS required_numeric_groups,
        %s::jsonb AS forbidden_numeric_constraints,
        %s::text[] AS query_tokens,
        %s::text AS query_text
),
scored_facts AS MATERIALIZED (
    SELECT
        authorized_units.*,
        least(
            1.0,
            0.05
            + least(
                (
                    SELECT count(DISTINCT token)::integer
                    FROM unnest(fact_query.query_tokens) token
                    WHERE (
                        length(token) >= 3
                        OR token IN ('bm', 'gi', 'gib', 'k', 'pe')
                    )
                      AND position(
                          token IN lower(
                              fact.subject_key || ' ' || fact.predicate
                          )
                      ) > 0
                ),
                8
            )::double precision * 0.10
            + CASE
                  WHEN jsonb_array_length(
                      fact_query.required_numeric_groups
                  ) > 0
                  THEN 0.15
                  ELSE 0.0
              END
        )::double precision AS fact_relevance,
        fact.numeric_value AS fact_numeric_value,
        fact.unit_ucum AS fact_unit_ucum,
        fact.unit_raw AS fact_unit_raw,
        fact.predicate AS fact_predicate,
        fact.fact_id
    FROM authorized_units
    JOIN bauer_rag_v3.facts fact
      ON fact.artifact_set_id = authorized_units.artifact_set_id
     AND fact.primary_provenance_id =
         authorized_units.primary_provenance_id
    CROSS JOIN fact_query
    WHERE authorized_units.unit_type = 'fact'
      AND fact.verification_status <> 'rejected'
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
              fact_query.required_numeric_groups
          ) AS required_group(value)
          WHERE NOT EXISTS (
              SELECT 1
              FROM jsonb_to_recordset(
                  required_group.value -> 'alternatives'
              ) AS required_constraint(
                  comparator text,
                  lower_value numeric,
                  upper_value numeric,
                  unit text
              )
              WHERE fact.numeric_value IS NOT NULL
                AND (
                    required_constraint.unit IS NULL
                    OR lower(
                        coalesce(fact.unit_ucum, fact.unit_raw, '')
                    ) = required_constraint.unit
                )
                AND CASE required_constraint.comparator
                    WHEN 'eq' THEN
                        fact.numeric_value =
                            required_constraint.lower_value
                    WHEN 'gt' THEN
                        fact.numeric_value >
                            required_constraint.lower_value
                    WHEN 'gte' THEN
                        fact.numeric_value >=
                            required_constraint.lower_value
                    WHEN 'lt' THEN
                        fact.numeric_value <
                            required_constraint.lower_value
                    WHEN 'lte' THEN
                        fact.numeric_value <=
                            required_constraint.lower_value
                    WHEN 'between' THEN
                        fact.numeric_value BETWEEN
                            required_constraint.lower_value
                            AND required_constraint.upper_value
                    ELSE FALSE
                END
          )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_to_recordset(
              fact_query.forbidden_numeric_constraints
          ) AS forbidden_constraint(
              comparator text,
              lower_value numeric,
              upper_value numeric,
              unit text
          )
          WHERE fact.numeric_value IS NOT NULL
            AND (
                forbidden_constraint.unit IS NULL
                OR lower(
                    coalesce(fact.unit_ucum, fact.unit_raw, '')
                ) = forbidden_constraint.unit
            )
            AND CASE forbidden_constraint.comparator
                WHEN 'eq' THEN
                    fact.numeric_value =
                        forbidden_constraint.lower_value
                WHEN 'gt' THEN
                    fact.numeric_value >
                        forbidden_constraint.lower_value
                WHEN 'gte' THEN
                    fact.numeric_value >=
                        forbidden_constraint.lower_value
                WHEN 'lt' THEN
                    fact.numeric_value <
                        forbidden_constraint.lower_value
                WHEN 'lte' THEN
                    fact.numeric_value <=
                        forbidden_constraint.lower_value
                WHEN 'between' THEN
                    fact.numeric_value BETWEEN
                        forbidden_constraint.lower_value
                        AND forbidden_constraint.upper_value
                ELSE FALSE
            END
      )
      AND (
          EXISTS (
              SELECT 1
              FROM unnest(fact_query.query_tokens) token
              WHERE (
                  length(token) >= 3
                  OR token IN ('bm', 'gi', 'gib', 'k', 'pe')
              )
                AND position(
                    token IN lower(
                        fact.subject_key || ' ' || fact.predicate
                    )
                ) > 0
          )
          OR (
              {allow_extremum}
              AND similarity(
                  lower(fact.subject_key || ' ' || fact.predicate),
                  fact_query.query_text
              ) > 0.02
          )
      )
),
ranked_facts AS (
    SELECT DISTINCT ON (evidence_id)
        scored_facts.*,
        fact_relevance AS raw_score,
        'fact'::text AS channel,
        'fact:' || fact_predicate AS reason,
        coalesce(fact_unit_ucum, fact_unit_raw) AS channel_unit
    FROM scored_facts
    ORDER BY
        evidence_id,
        {fact_choice_ordering}
)
SELECT
    ranked_facts.*
FROM ranked_facts
ORDER BY {result_ordering}
LIMIT %s
"""
    )


_TABLE_SQL = (
    "/* v3:channel:table */\n"
    + _AUTHORIZED_UNITS_CTE
    + """
, table_query AS (
    SELECT
        %s::text AS query_text,
        %s::text[] AS identifiers,
        %s::jsonb AS required_numeric_groups,
        %s::jsonb AS forbidden_numeric_constraints
)
SELECT
    authorized_units.*,
    greatest(
        ts_rank_cd(
            authorized_units.search_vector,
            websearch_to_tsquery('simple', table_query.query_text)
        ),
        similarity(
            lower(authorized_units.search_text),
            table_query.query_text
        ),
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM unnest(table_query.identifiers) identifier
                WHERE position(
                    identifier IN lower(authorized_units.search_text)
                ) > 0
            ) THEN 0.95
            ELSE 0.0
        END
    )::double precision AS raw_score,
    'table'::text AS channel,
    'structured_table'::text AS reason,
    authorized_units.unit_metadata ->> 'unit' AS channel_unit
FROM authorized_units
CROSS JOIN table_query
WHERE authorized_units.unit_type IN ('table_row', 'table_cell')
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
          table_query.required_numeric_groups
      ) AS required_group(value)
      WHERE NOT EXISTS (
          SELECT 1
          FROM jsonb_to_recordset(
              required_group.value -> 'alternatives'
          ) AS required_constraint(
              comparator text,
              lower_value numeric,
              upper_value numeric,
              unit text
          )
          WHERE EXISTS (
              SELECT 1
              FROM bauer_rag_v3.table_cells numeric_cell
              WHERE numeric_cell.artifact_set_id =
                    authorized_units.artifact_set_id
                AND numeric_cell.table_id::text =
                    authorized_units.table_id
                AND (
                    authorized_units.table_row_index IS NULL
                    OR numeric_cell.row_index =
                       authorized_units.table_row_index
                )
                AND numeric_cell.numeric_value IS NOT NULL
                AND (
                    required_constraint.unit IS NULL
                    OR lower(
                        coalesce(
                            numeric_cell.unit_ucum,
                            numeric_cell.unit_raw,
                            ''
                        )
                    ) = required_constraint.unit
                )
                AND CASE required_constraint.comparator
                    WHEN 'eq' THEN
                        numeric_cell.numeric_value =
                            required_constraint.lower_value
                    WHEN 'gt' THEN
                        numeric_cell.numeric_value >
                            required_constraint.lower_value
                    WHEN 'gte' THEN
                        numeric_cell.numeric_value >=
                            required_constraint.lower_value
                    WHEN 'lt' THEN
                        numeric_cell.numeric_value <
                            required_constraint.lower_value
                    WHEN 'lte' THEN
                        numeric_cell.numeric_value <=
                            required_constraint.lower_value
                    WHEN 'between' THEN
                        numeric_cell.numeric_value BETWEEN
                            required_constraint.lower_value
                            AND required_constraint.upper_value
                    ELSE FALSE
                END
          )
      )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_to_recordset(
          table_query.forbidden_numeric_constraints
      ) AS forbidden_constraint(
          comparator text,
          lower_value numeric,
          upper_value numeric,
          unit text
      )
      WHERE EXISTS (
          SELECT 1
          FROM bauer_rag_v3.table_cells numeric_cell
          WHERE numeric_cell.artifact_set_id =
                authorized_units.artifact_set_id
            AND numeric_cell.table_id::text =
                authorized_units.table_id
            AND (
                authorized_units.table_row_index IS NULL
                OR numeric_cell.row_index =
                   authorized_units.table_row_index
            )
            AND numeric_cell.numeric_value IS NOT NULL
            AND (
                forbidden_constraint.unit IS NULL
                OR lower(
                    coalesce(
                        numeric_cell.unit_ucum,
                        numeric_cell.unit_raw,
                        ''
                    )
                ) = forbidden_constraint.unit
            )
            AND CASE forbidden_constraint.comparator
                WHEN 'eq' THEN
                    numeric_cell.numeric_value =
                        forbidden_constraint.lower_value
                WHEN 'gt' THEN
                    numeric_cell.numeric_value >
                        forbidden_constraint.lower_value
                WHEN 'gte' THEN
                    numeric_cell.numeric_value >=
                        forbidden_constraint.lower_value
                WHEN 'lt' THEN
                    numeric_cell.numeric_value <
                        forbidden_constraint.lower_value
                WHEN 'lte' THEN
                    numeric_cell.numeric_value <=
                        forbidden_constraint.lower_value
                WHEN 'between' THEN
                    numeric_cell.numeric_value BETWEEN
                        forbidden_constraint.lower_value
                        AND forbidden_constraint.upper_value
                ELSE FALSE
            END
      )
  )
  AND (
      authorized_units.search_vector @@
          websearch_to_tsquery('simple', table_query.query_text)
      OR similarity(
          lower(authorized_units.search_text),
          table_query.query_text
      ) > 0.05
      OR EXISTS (
          SELECT 1
          FROM unnest(table_query.identifiers) identifier
          WHERE position(
              identifier IN lower(authorized_units.search_text)
          ) > 0
      )
      OR jsonb_array_length(table_query.required_numeric_groups) > 0
  )
ORDER BY raw_score DESC, evidence_id
LIMIT %s
"""
)


_LEXICAL_SQL = """
/* v3:channel:lexical */
WITH request_scope AS (
    SELECT
        %s::uuid AS release_id,
        %s::text AS release_status,
        %s::uuid AS knowledge_base_id,
        %s::uuid AS tenant_id,
        %s::text[] AS source_ids,
        %s::text[] AS external_file_ids,
        %s::jsonb AS mandatory_text_constraints,
        %s::text[] AS forbidden_claim_values
),
lexical_query AS (
    SELECT
        query_input.query_text,
        query_input.query_terms,
        to_tsquery(
            'simple',
            array_to_string(
                ARRAY(
                    SELECT plainto_tsquery(
                        'simple',
                        query_term
                    )::text
                    FROM unnest(query_input.query_terms) query_term
                ),
                ' | '
            )
        ) AS token_query
    FROM (
        SELECT
            %s::text AS query_text,
            %s::text[] AS query_terms
    ) query_input
),
source_scores AS MATERIALIZED (
    SELECT
        member.source_id,
        member.source_version_id,
        ts_rank_cd(
            to_tsvector(
                'simple',
                regexp_replace(
                    lower(
                        concat_ws(
                            ' ',
                            source_version.filename,
                            source_version.discovered_metadata ->> 'title'
                        )
                    ),
                    '[_/.-]+',
                    ' ',
                    'g'
                )
            ),
            lexical_query.token_query
        )::double precision AS title_score,
        (
            SELECT count(*)::integer
            FROM unnest(lexical_query.query_terms) query_term
            WHERE to_tsvector(
                'simple',
                regexp_replace(
                    lower(
                        concat_ws(
                            ' ',
                            source_version.filename,
                            source_version.discovered_metadata ->> 'title'
                        )
                    ),
                    '[_/.-]+',
                    ' ',
                    'g'
                )
            ) @@ plainto_tsquery('simple', query_term)
        ) AS title_term_hits
    FROM bauer_rag_v3.release_sources member
    JOIN bauer_rag_v3.source_versions source_version
      ON source_version.source_version_id =
         member.source_version_id
     AND source_version.source_id = member.source_id
    CROSS JOIN request_scope request
    CROSS JOIN lexical_query
    WHERE member.release_id = request.release_id
),
fts_units AS MATERIALIZED (
    SELECT
        unit.search_unit_id,
        unit.source_id,
        unit.kb_id,
        unit.search_text,
        unit.display_text,
        unit.metadata,
        (
            ts_rank_cd(
                unit.search_vector,
                lexical_query.token_query
            )
            + least(source_scores.title_score, 1.0) * 1.5
            + least(source_scores.title_term_hits, 4) * 0.50
            + least(
                (
                    SELECT count(*)::integer
                    FROM unnest(
                        lexical_query.query_terms
                    ) query_term
                    WHERE unit.search_vector @@
                          plainto_tsquery('simple', query_term)
                ),
                8
            ) * 0.25
            + CASE
                  WHEN lexical_query.query_text LIKE
                       '%%shutdown pressure%%'
                   AND unit.search_vector @@
                       plainto_tsquery(
                           'simple',
                           'shut down pressure'
                       )
                  THEN 2.0
                  ELSE 0.0
              END
        )::double precision AS raw_score
    FROM bauer_rag_v3.search_units unit
    JOIN source_scores
      ON source_scores.source_id = unit.source_id
     AND source_scores.source_version_id = unit.source_version_id
    CROSS JOIN request_scope request
    CROSS JOIN lexical_query
    WHERE unit.release_id = request.release_id
      AND unit.is_citable
      AND NOT unit.generated_summary
      AND unit.search_vector @@ lexical_query.token_query
),
source_ranked_units AS MATERIALIZED (
    SELECT
        candidate.*,
        row_number() OVER (
            PARTITION BY candidate.source_id
            ORDER BY
                candidate.raw_score DESC,
                candidate.search_unit_id
        ) AS source_rank
    FROM fts_units candidate
),
matched_units AS MATERIALIZED (
    SELECT
        candidate.search_unit_id,
        candidate.raw_score
    FROM source_ranked_units candidate
    JOIN bauer_rag_v3.sources source_scope
      ON source_scope.source_id = candidate.source_id
     AND source_scope.kb_id = candidate.kb_id
    CROSS JOIN request_scope request
    WHERE candidate.source_rank <= 5
      AND (
          source_scope.source_id::text = ANY (request.source_ids)
          OR source_scope.external_file_id =
              ANY (request.external_file_ids)
      )
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_each(request.mandatory_text_constraints)
              AS required_constraint(constraint_name, allowed_values)
          WHERE NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  required_constraint.allowed_values
              ) AS allowed_value(value)
              WHERE position(
                  allowed_value.value IN lower(
                      concat_ws(
                          ' ',
                          candidate.search_text,
                          candidate.display_text,
                          candidate.metadata::text
                      )
                  )
              ) > 0
          )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(request.forbidden_claim_values)
              AS forbidden_value(value)
          WHERE position(
              forbidden_value.value IN lower(
                  concat_ws(
                      ' ',
                      candidate.search_text,
                      candidate.display_text,
                      candidate.metadata::text
                  )
              )
          ) > 0
      )
    ORDER BY candidate.raw_score DESC, candidate.search_unit_id
    LIMIT %s
),
selected_units AS MATERIALIZED (
    SELECT
        unit.*,
        matched.raw_score
    FROM matched_units matched
    JOIN bauer_rag_v3.search_units unit
      ON unit.search_unit_id = matched.search_unit_id
)
SELECT
    unit.search_unit_id::text AS evidence_id,
    unit.release_id::text AS release_id,
    kb.tenant_id::text AS tenant_id,
    kb.kb_id::text AS knowledge_base_id,
    source_row.source_id::text AS source_document_id,
    source_row.external_file_id,
    source_row.source_type,
    source_version.source_version_id::text AS source_version_id,
    source_version.sha256::text AS source_sha256,
    coalesce(
        source_version.discovered_metadata ->> 'title',
        source_version.filename
    ) AS title,
    source_version.discovered_metadata AS source_metadata,
    unit.unit_type,
    unit.display_text,
    unit.search_text,
    unit.metadata AS unit_metadata,
    unit.is_citable,
    unit.generated_summary,
    coalesce(unit.page_start, 1)::integer AS page_number,
    NULL::text AS printed_page_label,
    unit.section_id::text AS section_id,
    NULL::text AS block_id,
    unit.table_id::text AS table_id,
    unit.table_row_index::integer AS table_row_index,
    NULL::text AS cell_id,
    NULL::integer AS char_start,
    NULL::integer AS char_end,
    NULL::double precision AS x0,
    NULL::double precision AS y0,
    NULL::double precision AS x1,
    NULL::double precision AS y1,
    unit.artifact_set_id,
    unit.primary_provenance_id,
    unit.raw_score,
    'lexical'::text AS channel,
    'fts_ranked'::text AS reason,
    unit.metadata ->> 'unit' AS channel_unit
FROM selected_units unit
CROSS JOIN request_scope request
JOIN bauer_rag_v3.release_sources member
  ON member.release_id = unit.release_id
 AND member.source_id = unit.source_id
 AND member.source_version_id = unit.source_version_id
 AND member.artifact_set_id = unit.artifact_set_id
JOIN bauer_rag_v3.knowledge_releases release_row
  ON release_row.release_id = unit.release_id
 AND release_row.kb_id = unit.kb_id
JOIN bauer_rag_v3.knowledge_bases kb
  ON kb.kb_id = release_row.kb_id
JOIN bauer_rag_v3.sources source_row
  ON source_row.source_id = unit.source_id
 AND source_row.kb_id = kb.kb_id
JOIN bauer_rag_v3.source_versions source_version
  ON source_version.source_version_id = unit.source_version_id
 AND source_version.source_id = source_row.source_id
WHERE unit.release_id = request.release_id
  AND release_row.status = request.release_status
  AND release_row.kb_id = request.knowledge_base_id
  AND kb.tenant_id = request.tenant_id
  AND (
      source_row.source_id::text = ANY (request.source_ids)
      OR source_row.external_file_id = ANY (request.external_file_ids)
  )
ORDER BY unit.raw_score DESC, evidence_id
"""


_SEMANTIC_SQL = """
/* v3:channel:semantic */
WITH request_scope AS (
    SELECT
        %s::uuid AS release_id,
        %s::text AS release_status,
        %s::uuid AS knowledge_base_id,
        %s::uuid AS tenant_id,
        %s::text[] AS source_ids,
        %s::text[] AS external_file_ids,
        %s::jsonb AS mandatory_text_constraints,
        %s::text[] AS forbidden_claim_values
),
semantic_query AS (
    SELECT %s::vector AS query_embedding
),
nearest_units AS MATERIALIZED (
    SELECT
        unit.search_unit_id,
        unit.embedding <=> semantic_query.query_embedding AS distance
    FROM bauer_rag_v3.search_units unit
    CROSS JOIN request_scope request
    CROSS JOIN semantic_query
    WHERE unit.release_id = request.release_id
      AND unit.embedding IS NOT NULL
    ORDER BY unit.embedding <=> semantic_query.query_embedding
    LIMIT %s
),
matched_units AS MATERIALIZED (
    SELECT
        unit.search_unit_id,
        (1.0 - nearest.distance)::double precision AS raw_score
    FROM nearest_units nearest
    JOIN bauer_rag_v3.search_units unit
      ON unit.search_unit_id = nearest.search_unit_id
    JOIN bauer_rag_v3.sources source_scope
      ON source_scope.source_id = unit.source_id
     AND source_scope.kb_id = unit.kb_id
    CROSS JOIN request_scope request
    WHERE unit.release_id = request.release_id
      AND (
          source_scope.source_id::text = ANY (request.source_ids)
          OR source_scope.external_file_id =
              ANY (request.external_file_ids)
      )
      AND unit.is_citable
      AND NOT unit.generated_summary
      AND nearest.distance < 1.0
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_each(request.mandatory_text_constraints)
              AS required_constraint(constraint_name, allowed_values)
          WHERE NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  required_constraint.allowed_values
              ) AS allowed_value(value)
              WHERE position(
                  allowed_value.value IN lower(
                      concat_ws(
                          ' ',
                          unit.search_text,
                          unit.display_text,
                          unit.metadata::text
                      )
                  )
              ) > 0
          )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(request.forbidden_claim_values)
              AS forbidden_value(value)
          WHERE position(
              forbidden_value.value IN lower(
                  concat_ws(
                      ' ',
                      unit.search_text,
                      unit.display_text,
                      unit.metadata::text
                  )
              )
          ) > 0
      )
    ORDER BY raw_score DESC, unit.search_unit_id
    LIMIT %s
),
selected_units AS MATERIALIZED (
    SELECT
        unit.*,
        matched.raw_score
    FROM matched_units matched
    JOIN bauer_rag_v3.search_units unit
      ON unit.search_unit_id = matched.search_unit_id
)
SELECT
    unit.search_unit_id::text AS evidence_id,
    unit.release_id::text AS release_id,
    kb.tenant_id::text AS tenant_id,
    kb.kb_id::text AS knowledge_base_id,
    source_row.source_id::text AS source_document_id,
    source_row.external_file_id,
    source_row.source_type,
    source_version.source_version_id::text AS source_version_id,
    source_version.sha256::text AS source_sha256,
    coalesce(
        source_version.discovered_metadata ->> 'title',
        source_version.filename
    ) AS title,
    source_version.discovered_metadata AS source_metadata,
    unit.unit_type,
    unit.display_text,
    unit.search_text,
    unit.metadata AS unit_metadata,
    unit.is_citable,
    unit.generated_summary,
    coalesce(unit.page_start, 1)::integer AS page_number,
    NULL::text AS printed_page_label,
    unit.section_id::text AS section_id,
    NULL::text AS block_id,
    unit.table_id::text AS table_id,
    unit.table_row_index::integer AS table_row_index,
    NULL::text AS cell_id,
    NULL::integer AS char_start,
    NULL::integer AS char_end,
    NULL::double precision AS x0,
    NULL::double precision AS y0,
    NULL::double precision AS x1,
    NULL::double precision AS y1,
    unit.artifact_set_id,
    unit.primary_provenance_id,
    unit.raw_score,
    'semantic'::text AS channel,
    'vector_cosine'::text AS reason,
    unit.metadata ->> 'unit' AS channel_unit
FROM selected_units unit
CROSS JOIN request_scope request
JOIN bauer_rag_v3.release_sources member
  ON member.release_id = unit.release_id
 AND member.source_id = unit.source_id
 AND member.source_version_id = unit.source_version_id
 AND member.artifact_set_id = unit.artifact_set_id
JOIN bauer_rag_v3.knowledge_releases release_row
  ON release_row.release_id = unit.release_id
 AND release_row.kb_id = unit.kb_id
JOIN bauer_rag_v3.knowledge_bases kb
  ON kb.kb_id = release_row.kb_id
JOIN bauer_rag_v3.sources source_row
  ON source_row.source_id = unit.source_id
 AND source_row.kb_id = kb.kb_id
JOIN bauer_rag_v3.source_versions source_version
  ON source_version.source_version_id = unit.source_version_id
 AND source_version.source_id = source_row.source_id
WHERE unit.release_id = request.release_id
  AND release_row.status = request.release_status
  AND release_row.kb_id = request.knowledge_base_id
  AND kb.tenant_id = request.tenant_id
  AND (
      source_row.source_id::text = ANY (request.source_ids)
      OR source_row.external_file_id = ANY (request.external_file_ids)
  )
ORDER BY unit.raw_score DESC, evidence_id
"""


_NAVIGATION_SQL = (
    "/* v3:channel:navigation */\n"
    + _AUTHORIZED_UNITS_CTE
    + """
, navigation_query AS (
    SELECT %s::text AS query_text
),
matched_nodes AS (
    SELECT
        node.release_id,
        node.nav_node_id,
        node.source_id,
        greatest(
            similarity(lower(node.label), navigation_query.query_text),
            similarity(lower(node.node_key), navigation_query.query_text),
            coalesce(
                similarity(
                    lower(description_unit.search_text),
                    navigation_query.query_text
                ),
                0.0
            )
        )::double precision AS node_score
    FROM bauer_rag_v3.nav_nodes node
    CROSS JOIN navigation_query
    LEFT JOIN bauer_rag_v3.search_units description_unit
      ON description_unit.release_id = node.release_id
     AND description_unit.search_unit_id =
         node.description_search_unit_id
    WHERE node.release_id = %s::uuid
      AND bauer_rag_v3.can_read_release(node.release_id)
      AND greatest(
          similarity(lower(node.label), navigation_query.query_text),
          similarity(lower(node.node_key), navigation_query.query_text),
          coalesce(
              similarity(
                  lower(description_unit.search_text),
                  navigation_query.query_text
              ),
              0.0
          )
      ) > 0.05
),
expanded_sources AS (
    SELECT
        matched.release_id,
        matched.source_id,
        matched.nav_node_id,
        matched.node_score
    FROM matched_nodes matched
    WHERE matched.source_id IS NOT NULL
    UNION ALL
    SELECT
        matched.release_id,
        target.source_id,
        matched.nav_node_id,
        matched.node_score
    FROM matched_nodes matched
    JOIN bauer_rag_v3.nav_edges edge
      ON edge.release_id = matched.release_id
     AND (
         edge.from_node_id = matched.nav_node_id
         OR edge.to_node_id = matched.nav_node_id
     )
    JOIN bauer_rag_v3.nav_nodes target
      ON target.release_id = edge.release_id
     AND target.nav_node_id = CASE
         WHEN edge.from_node_id = matched.nav_node_id
             THEN edge.to_node_id
         ELSE edge.from_node_id
     END
    WHERE target.source_id IS NOT NULL
)
SELECT *
FROM (
    SELECT DISTINCT ON (authorized_units.evidence_id)
        authorized_units.*,
        expanded.node_score::double precision AS raw_score,
        'navigation'::text AS channel,
        'navigation:' || expanded.nav_node_id::text AS reason,
        authorized_units.unit_metadata ->> 'unit' AS channel_unit
    FROM authorized_units
    JOIN expanded_sources expanded
      ON expanded.release_id::text = authorized_units.release_id
     AND expanded.source_id::text =
         authorized_units.source_document_id
    ORDER BY
        authorized_units.evidence_id,
        expanded.node_score DESC,
        expanded.nav_node_id
) ranked_navigation
ORDER BY raw_score DESC, evidence_id
LIMIT %s
"""
)


_PIN_ACTIVE_SQL = """
/* v3:pin_active */
SELECT
    active.release_id::text AS release_id,
    kb.tenant_id::text AS tenant_id,
    kb.kb_id::text AS knowledge_base_id
FROM bauer_rag_v3.active_releases active
JOIN bauer_rag_v3.knowledge_releases release_row
  ON release_row.release_id = active.release_id
 AND release_row.kb_id = active.kb_id
JOIN bauer_rag_v3.knowledge_bases kb
  ON kb.kb_id = active.kb_id
WHERE active.kb_id = %s::uuid
  AND kb.tenant_id = %s::uuid
  AND release_row.status = 'ready'
  AND bauer_rag_v3.can_read_kb(kb.kb_id)
ORDER BY active.release_id
LIMIT 2
"""


_PIN_READY_CANDIDATE_SQL = """
/* v3:pin_ready_candidate */
SELECT
    release_row.release_id::text AS release_id,
    kb.tenant_id::text AS tenant_id,
    kb.kb_id::text AS knowledge_base_id
FROM bauer_rag_v3.knowledge_releases release_row
JOIN bauer_rag_v3.knowledge_bases kb
  ON kb.kb_id = release_row.kb_id
WHERE release_row.release_id = %s::uuid
  AND release_row.kb_id = %s::uuid
  AND kb.tenant_id = %s::uuid
  AND release_row.status = 'ready'
  AND bauer_rag_v3.can_read_release(release_row.release_id)
ORDER BY release_row.release_id
LIMIT 2
"""


_PIN_VALIDATING_RELEASE_SQL = """
/* v3:pin_validating_release */
SELECT
    release_row.release_id::text AS release_id,
    kb.tenant_id::text AS tenant_id,
    kb.kb_id::text AS knowledge_base_id
FROM bauer_rag_v3.knowledge_releases release_row
JOIN bauer_rag_v3.knowledge_bases kb
  ON kb.kb_id = release_row.kb_id
WHERE release_row.release_id = %s::uuid
  AND release_row.kb_id = %s::uuid
  AND kb.tenant_id = %s::uuid
  AND release_row.status = 'validating'
  AND bauer_rag_v3.can_read_release(release_row.release_id)
ORDER BY release_row.release_id
LIMIT 2
"""


class _PostgresAdapter:
    def __init__(
        self,
        database_url: str | None,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        principal_ids: Sequence[str],
        connection_factory: ConnectionFactory | None = None,
        connect_timeout_seconds: int = 5,
        statement_timeout_ms: int = 15_000,
        enforce_least_privilege: bool = False,
    ) -> None:
        if not connection_factory and not (database_url and database_url.strip()):
            raise ValueError("database_url or connection_factory is required")
        if not tenant_id.strip() or not knowledge_base_id.strip():
            raise ValueError("tenant_id and knowledge_base_id are required")
        if connect_timeout_seconds < 1:
            raise ValueError("connect_timeout_seconds must be positive")
        if statement_timeout_ms < 1:
            raise ValueError("statement_timeout_ms must be positive")
        normalized_principals = tuple(
            sorted({str(value).strip() for value in principal_ids if str(value).strip()})
        )
        if not normalized_principals:
            raise ValueError("at least one database principal ID is required")

        self.database_url = database_url
        self.tenant_id = tenant_id.strip()
        self.knowledge_base_id = knowledge_base_id.strip()
        self.principal_ids = normalized_principals
        self.connection_factory = connection_factory
        self.connect_timeout_seconds = connect_timeout_seconds
        self.statement_timeout_ms = statement_timeout_ms
        self.enforce_least_privilege = bool(enforce_least_privilege)

    def _open_connection(self) -> Any:
        if self.connection_factory is not None:
            return self.connection_factory()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as error:
            raise PostgresRuntimeError(
                "PostgreSQL runtime adapters require psycopg 3"
            ) from error
        return psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=self.connect_timeout_seconds,
            application_name="bauer-evidence-v3",
        )

    @contextmanager
    def _read_transaction(
        self,
        *,
        authorized_source_ids: Sequence[str] = (),
    ) -> Iterator[Any]:
        source_context = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in authorized_source_ids
                    if str(value).strip()
                }
            )
        )
        with self._open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                if self.enforce_least_privilege:
                    verify_runtime_database_role(
                        connection,
                        required_group_role="bauer_rag_v3_reader",
                    )
                # set_config(..., true) is the parameterized SET LOCAL equivalent.
                connection.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    (self.tenant_id,),
                )
                connection.execute(
                    "SELECT set_config('app.knowledge_base_id', %s, true)",
                    (self.knowledge_base_id,),
                )
                connection.execute(
                    "SELECT set_config('app.principal_ids', %s, true)",
                    (
                        json.dumps(
                            self.principal_ids,
                            separators=(",", ":"),
                        ),
                    ),
                )
                connection.execute(
                    "SELECT set_config('app.authorized_source_ids', %s, true)",
                    (
                        json.dumps(
                            source_context,
                            separators=(",", ":"),
                        ),
                    ),
                )
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self.statement_timeout_ms),),
                )
                yield connection

    @staticmethod
    def _fetchall(
        connection: Any,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> list[Mapping[str, Any]]:
        rows = connection.execute(sql, tuple(parameters)).fetchall()
        if any(not isinstance(row, Mapping) for row in rows):
            raise PostgresRuntimeError(
                "PostgreSQL connection must use a mapping row factory"
            )
        return list(rows)

    @staticmethod
    def _fetchone(
        connection: Any,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> Mapping[str, Any] | None:
        row = connection.execute(sql, tuple(parameters)).fetchone()
        if row is None:
            return None
        if not isinstance(row, Mapping):
            raise PostgresRuntimeError(
                "PostgreSQL connection must use a mapping row factory"
            )
        return row

    def readiness(
        self,
        *,
        expected_version: int | None = None,
        minimum_active_releases: int = 1,
        candidate_release_id: str | None = None,
    ) -> RuntimeReadiness:
        if minimum_active_releases < 0:
            raise ValueError("minimum_active_releases cannot be negative")
        normalized_candidate = (
            str(uuid.UUID(candidate_release_id))
            if candidate_release_id
            else None
        )
        release_selection = (
            "fixed_candidate" if normalized_candidate else "active"
        )
        required = (
            expected_schema_version()
            if expected_version is None
            else expected_version
        )
        if required < 1:
            raise ValueError("expected_version must be positive")

        with self._read_transaction() as connection:
            relation = self._fetchone(
                connection,
                """
                /* v3:readiness:relation */
                SELECT to_regclass(
                    'bauer_rag_v3.schema_migrations'
                )::text AS migration_table
                """,
            )
            if not relation or relation.get("migration_table") is None:
                return RuntimeReadiness(
                    ready=False,
                    current_migration_version=0,
                    expected_migration_version=required,
                    applied_migration_count=0,
                    active_release_count=0,
                    selected_release_count=0,
                    release_selection=release_selection,
                )
            migration = self._fetchone(
                connection,
                """
                /* v3:readiness:migrations */
                SELECT
                    coalesce(max(version), 0)::integer AS current_version,
                    count(*)::integer AS applied_count
                FROM bauer_rag_v3.schema_migrations
                """,
            )
            current = int((migration or {}).get("current_version") or 0)
            applied_count = int((migration or {}).get("applied_count") or 0)
            active_count = 0
            selected_count = 0
            if current >= 2:
                if normalized_candidate:
                    candidate = self._fetchone(
                        connection,
                        """
                        /* v3:readiness:candidate */
                        SELECT count(*)::integer AS candidate_count
                        FROM bauer_rag_v3.knowledge_releases release_row
                        JOIN bauer_rag_v3.knowledge_bases kb
                          ON kb.kb_id = release_row.kb_id
                        WHERE release_row.release_id = %s::uuid
                          AND release_row.kb_id = %s::uuid
                          AND kb.tenant_id = %s::uuid
                          AND release_row.status = 'ready'
                          AND bauer_rag_v3.can_read_release(
                              release_row.release_id
                          )
                        """,
                        (
                            normalized_candidate,
                            self.knowledge_base_id,
                            self.tenant_id,
                        ),
                    )
                    selected_count = int(
                        (candidate or {}).get("candidate_count") or 0
                    )
                else:
                    active = self._fetchone(
                        connection,
                        """
                        /* v3:readiness:active */
                        SELECT count(*)::integer AS active_count
                        FROM bauer_rag_v3.active_releases active
                        JOIN bauer_rag_v3.knowledge_releases release_row
                          ON release_row.release_id = active.release_id
                         AND release_row.kb_id = active.kb_id
                        JOIN bauer_rag_v3.knowledge_bases kb
                          ON kb.kb_id = active.kb_id
                        WHERE kb.kb_id = %s::uuid
                          AND kb.tenant_id = %s::uuid
                          AND release_row.status = 'ready'
                          AND bauer_rag_v3.can_read_kb(kb.kb_id)
                        """,
                        (self.knowledge_base_id, self.tenant_id),
                    )
                    active_count = int(
                        (active or {}).get("active_count") or 0
                    )
                    selected_count = active_count

        return RuntimeReadiness(
            ready=(
                current == required
                and applied_count == required
                and selected_count >= minimum_active_releases
            ),
            current_migration_version=current,
            expected_migration_version=required,
            applied_migration_count=applied_count,
            active_release_count=active_count,
            selected_release_count=selected_count,
            release_selection=release_selection,
        )


class PostgresReleaseRegistry(_PostgresAdapter):
    """Read-only release adapter duck-compatible with AnswerService."""

    def pin_active(self, knowledge_base_id: str) -> PinnedRelease:
        if knowledge_base_id != self.knowledge_base_id:
            raise PermissionError(
                "requested knowledge base does not match database context"
            )
        with self._read_transaction() as connection:
            rows = self._fetchall(
                connection,
                _PIN_ACTIVE_SQL,
                (knowledge_base_id, self.tenant_id),
            )
        if not rows:
            raise ReleaseError(
                f"knowledge base has no authorized ready release: "
                f"{knowledge_base_id}"
            )
        if len(rows) != 1:
            raise PostgresRuntimeError(
                "active-release registry returned more than one release"
            )
        row = rows[0]
        pinned = PinnedRelease(
            release_id=str(row["release_id"]),
            tenant_id=str(row["tenant_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
        )
        if (
            pinned.tenant_id != self.tenant_id
            or pinned.knowledge_base_id != self.knowledge_base_id
        ):
            raise PermissionError("active release crossed its database context")
        return pinned

    def pin_ready_candidate(
        self,
        knowledge_base_id: str,
        release_id: str,
    ) -> PinnedRelease:
        """Pin one authorized ready release without consulting or changing ACTIVE."""
        if knowledge_base_id != self.knowledge_base_id:
            raise PermissionError(
                "requested knowledge base does not match database context"
            )
        normalized_release_id = str(uuid.UUID(release_id))
        with self._read_transaction() as connection:
            rows = self._fetchall(
                connection,
                _PIN_READY_CANDIDATE_SQL,
                (
                    normalized_release_id,
                    knowledge_base_id,
                    self.tenant_id,
                ),
            )
        if not rows:
            raise ReleaseError(
                "configured candidate is not an authorized ready release for "
                f"knowledge base {knowledge_base_id}"
            )
        if len(rows) != 1:
            raise PostgresRuntimeError(
                "candidate-release registry returned more than one release"
            )
        row = rows[0]
        pinned = PinnedRelease(
            release_id=str(row["release_id"]),
            tenant_id=str(row["tenant_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
        )
        if (
            pinned.release_id != normalized_release_id
            or pinned.tenant_id != self.tenant_id
            or pinned.knowledge_base_id != self.knowledge_base_id
        ):
            raise PermissionError("candidate release crossed its database context")
        return pinned

    def pin_validating_release(
        self,
        knowledge_base_id: str,
        release_id: str,
    ) -> PinnedRelease:
        """Pin one validating release for an in-process evaluator only.

        This method is intentionally not wired into the API runtime. It breaks
        the VALIDATING -> evaluated -> READY cycle without exposing incomplete
        releases through a network serving route.
        """

        if knowledge_base_id != self.knowledge_base_id:
            raise PermissionError(
                "requested knowledge base does not match database context"
            )
        normalized_release_id = str(uuid.UUID(release_id))
        with self._read_transaction() as connection:
            rows = self._fetchall(
                connection,
                _PIN_VALIDATING_RELEASE_SQL,
                (
                    normalized_release_id,
                    knowledge_base_id,
                    self.tenant_id,
                ),
            )
        if not rows:
            raise ReleaseError(
                "configured evaluation release is not an authorized validating "
                f"release for knowledge base {knowledge_base_id}"
            )
        if len(rows) != 1:
            raise PostgresRuntimeError(
                "validating-release registry returned more than one release"
            )
        row = rows[0]
        pinned = PinnedRelease(
            release_id=str(row["release_id"]),
            tenant_id=str(row["tenant_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            status=ReleaseStatus.VALIDATING,
        )
        if (
            pinned.release_id != normalized_release_id
            or pinned.tenant_id != self.tenant_id
            or pinned.knowledge_base_id != self.knowledge_base_id
        ):
            raise PermissionError(
                "validating release crossed its database context"
            )
        return pinned


class PostgresCandidateReleaseRegistry:
    """Deployment-fixed shadow registry implementing the active-pin interface."""

    def __init__(
        self,
        registry: PostgresReleaseRegistry,
        *,
        candidate_release_id: str,
    ) -> None:
        self.registry = registry
        self.candidate_release_id = str(uuid.UUID(candidate_release_id))

    def pin_active(self, knowledge_base_id: str) -> PinnedRelease:
        return self.registry.pin_ready_candidate(
            knowledge_base_id,
            self.candidate_release_id,
        )

    def readiness(
        self,
        *,
        expected_version: int | None = None,
        minimum_active_releases: int = 1,
    ) -> RuntimeReadiness:
        return self.registry.readiness(
            expected_version=expected_version,
            minimum_active_releases=minimum_active_releases,
            candidate_release_id=self.candidate_release_id,
        )


class PostgresValidationReleaseRegistry:
    """Fixed validating-release adapter reserved for in-process evaluation."""

    def __init__(
        self,
        registry: PostgresReleaseRegistry,
        *,
        release_id: str,
    ) -> None:
        self.registry = registry
        self.release_id = str(uuid.UUID(release_id))

    def pin_active(self, knowledge_base_id: str) -> PinnedRelease:
        return self.registry.pin_validating_release(
            knowledge_base_id,
            self.release_id,
        )


class PostgresEvidenceIndex(_PostgresAdapter):
    """Synchronous PostgreSQL retrieval adapter with SQL-first authorization."""

    def __init__(
        self,
        database_url: str | None,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        principal_ids: Sequence[str],
        embedding_provider: EmbeddingProvider | None = None,
        embedding_dimensions: int = EXPECTED_EMBEDDING_DIMENSIONS,
        release_status: ReleaseStatus = ReleaseStatus.READY,
        connection_factory: ConnectionFactory | None = None,
        connect_timeout_seconds: int = 5,
        statement_timeout_ms: int = 15_000,
        enforce_least_privilege: bool = False,
    ) -> None:
        super().__init__(
            database_url,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            principal_ids=principal_ids,
            connection_factory=connection_factory,
            connect_timeout_seconds=connect_timeout_seconds,
            statement_timeout_ms=statement_timeout_ms,
            enforce_least_privilege=enforce_least_privilege,
        )
        if embedding_dimensions < 1:
            raise ValueError("embedding_dimensions must be positive")
        if release_status not in {
            ReleaseStatus.READY,
            ReleaseStatus.VALIDATING,
        }:
            raise ValueError(
                "runtime retrieval release_status must be ready or validating"
            )
        self.embedding_provider = embedding_provider
        self.embedding_dimensions = embedding_dimensions
        self.release_status = release_status

    def retrieve(
        self,
        plan: QueryPlan,
        *,
        release_id: str,
        authorized_source_ids: set[str] | frozenset[str],
    ) -> tuple[RetrievalResult, ...]:
        if plan.constraint_failure is not None:
            return ()
        sources = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in authorized_source_ids
                    if str(value).strip()
                }
            )
        )
        if not sources:
            return ()
        if len(sources) > MAX_AUTHORIZED_SOURCES:
            raise ValueError(
                f"at most {MAX_AUTHORIZED_SOURCES} authorized sources are allowed"
            )
        channel_limit = min(max(plan.top_k * 4, 20), 80)
        requested_physical_pages = _requested_physical_pages(plan)
        catalog_identifiers = _catalog_identifiers(plan)
        identifier_context_terms = _identifier_context_terms(plan)
        required_numeric_groups = _required_numeric_groups(plan)
        forbidden_numeric_constraints = [
            _numeric_constraint_document(constraint)
            for constraint in plan.forbidden_numeric_constraints
        ]
        base_parameters: tuple[Any, ...] = (
            release_id,
            self.release_status.value,
            self.knowledge_base_id,
            self.tenant_id,
            list(sources),
            list(sources),
            _json_parameter(plan.mandatory_text_constraints),
            list(plan.forbidden_claim_values),
        )
        channel_candidates: dict[RetrievalChannel, list[_Candidate]] = {}
        structured_constraint_filter = bool(
            plan.mandatory_numeric_constraints
            or forbidden_numeric_constraints
        )
        channels = (
            tuple(
                channel
                for channel in plan.channels
                if channel
                in {
                    RetrievalChannel.FACT,
                    RetrievalChannel.TABLE,
                }
            )
            if structured_constraint_filter
            else plan.channels
        )

        fusion_plan = plan
        with self._read_transaction(
            authorized_source_ids=sources
        ) as connection:
            for channel in channels:
                rows: list[Mapping[str, Any]]
                if channel is RetrievalChannel.EXACT:
                    terms = tuple(
                        dict.fromkeys(
                            value
                            for value in (
                                normalize_text(item)
                                for item in (
                                    *plan.identifiers,
                                    *plan.quoted_phrases,
                                )
                            )
                            if value
                        )
                    )
                    if not terms:
                        continue
                    rows = self._fetchall(
                        connection,
                        _EXACT_SQL,
                        (*base_parameters, list(terms), channel_limit),
                    )
                    if catalog_identifiers:
                        family_context_requested = (
                            "famil" in plan.normalized_query
                            or "compatible" in plan.normalized_query
                        )
                        context_limit = min(
                            max(
                                plan.top_k
                                * (
                                    60
                                    if family_context_requested
                                    else 20
                                ),
                                80,
                            ),
                            480 if family_context_requested else 160,
                        )
                        anchor_page_limit = (
                            3
                            if (
                                "b-select"
                                in catalog_identifiers
                                and (
                                    plan.table_intent
                                    or "function"
                                    in plan.normalized_query
                                )
                            )
                            else 1
                        )
                        rows.extend(
                            self._fetchall(
                                connection,
                                _IDENTIFIER_CONTEXT_SQL,
                                (
                                    list(catalog_identifiers),
                                    release_id,
                                    list(sources),
                                    list(sources),
                                    *base_parameters,
                                    list(identifier_context_terms),
                                    bool(
                                        plan.table_intent
                                        or RetrievalChannel.FACT
                                        in plan.channels
                                    ),
                                    anchor_page_limit,
                                    context_limit,
                                ),
                            )
                        )
                elif channel is RetrievalChannel.FACT:
                    rows = self._fetchall(
                        connection,
                        _fact_sql(plan.superlative),
                        (
                            *base_parameters,
                            _json_parameter(required_numeric_groups),
                            _json_parameter(
                                forbidden_numeric_constraints
                            ),
                            list(plan.tokens),
                            plan.normalized_query,
                            channel_limit,
                        ),
                    )
                elif channel is RetrievalChannel.TABLE:
                    rows = self._fetchall(
                        connection,
                        _TABLE_SQL,
                        (
                            *base_parameters,
                            plan.normalized_query,
                            [
                                normalize_text(identifier)
                                for identifier in plan.identifiers
                            ],
                            _json_parameter(required_numeric_groups),
                            _json_parameter(
                                forbidden_numeric_constraints
                            ),
                            channel_limit,
                        ),
                    )
                elif channel is RetrievalChannel.LEXICAL:
                    rows = self._fetchall(
                        connection,
                        _LEXICAL_SQL,
                        (
                            *base_parameters,
                            plan.normalized_query,
                            list(_lexical_query_terms(plan)),
                            channel_limit,
                        ),
                    )
                elif channel is RetrievalChannel.SEMANTIC:
                    if self.embedding_provider is None:
                        continue
                    vector = self.embedding_provider.embed(plan.query)
                    semantic_candidate_limit = min(
                        max(channel_limit * 8, 80),
                        640,
                    )
                    rows = self._fetchall(
                        connection,
                        _SEMANTIC_SQL,
                        (
                            *base_parameters,
                            self._vector_literal(vector),
                            semantic_candidate_limit,
                            channel_limit,
                        ),
                    )
                elif channel is RetrievalChannel.NAVIGATION:
                    rows = self._fetchall(
                        connection,
                        _NAVIGATION_SQL,
                        (
                            *base_parameters,
                            plan.normalized_query,
                            release_id,
                            channel_limit,
                        ),
                    )
                else:  # pragma: no cover - exhaustive enum guard
                    continue

                rows = self._hydrate_candidate_rows(
                    connection,
                    rows,
                    release_id=release_id,
                )
                candidates = [
                    self._candidate_from_row(
                        row,
                        expected_channel=channel,
                        release_id=release_id,
                        authorized_sources=frozenset(sources),
                    )
                    for row in rows
                ]
                if requested_physical_pages:
                    candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.evidence.coordinate.page_number
                        in requested_physical_pages
                    ]
                if candidates:
                    channel_candidates[channel] = self._dedupe_channel(
                        candidates
                    )

            discovered_identifiers = _discovered_catalog_identifiers(
                plan,
                channel_candidates,
            )
            if discovered_identifiers:
                context_limit = min(
                    max(plan.top_k * 60, 80),
                    480,
                )
                rows = self._fetchall(
                    connection,
                    _IDENTIFIER_CONTEXT_SQL,
                    (
                        [
                            normalize_text(identifier)
                            for identifier in discovered_identifiers
                        ],
                        release_id,
                        list(sources),
                        list(sources),
                        *base_parameters,
                        list(_identifier_context_terms(plan)),
                        bool(
                            plan.table_intent
                            or RetrievalChannel.FACT in plan.channels
                        ),
                        3,
                        context_limit,
                    ),
                )
                rows = self._hydrate_candidate_rows(
                    connection,
                    rows,
                    release_id=release_id,
                )
                discovered_candidates = [
                    self._candidate_from_row(
                        row,
                        expected_channel=RetrievalChannel.EXACT,
                        release_id=release_id,
                        authorized_sources=frozenset(sources),
                    )
                    for row in rows
                ]
                if discovered_candidates:
                    channel_candidates[RetrievalChannel.EXACT] = (
                        self._dedupe_channel(
                            [
                                *channel_candidates.get(
                                    RetrievalChannel.EXACT,
                                    (),
                                ),
                                *discovered_candidates,
                            ]
                        )
                    )
                    fusion_plan = replace(
                        plan,
                        identifiers=discovered_identifiers,
                        channels=tuple(
                            dict.fromkeys(
                                (
                                    RetrievalChannel.EXACT,
                                    *plan.channels,
                                )
                            )
                        ),
                    )

        return self._fuse(fusion_plan, channel_candidates)

    def _hydrate_candidate_rows(
        self,
        connection: Any,
        rows: Sequence[Mapping[str, Any]],
        *,
        release_id: str,
    ) -> list[Mapping[str, Any]]:
        """Join canonical coordinates only after each channel has applied LIMIT."""

        evidence_ids = tuple(
            dict.fromkeys(
                str(row.get("evidence_id") or "").strip()
                for row in rows
                if str(row.get("evidence_id") or "").strip()
            )
        )
        if not evidence_ids:
            return list(rows)
        hydrated = self._fetchall(
            connection,
            _HYDRATE_CANDIDATES_SQL,
            (release_id, list(evidence_ids)),
        )
        hydration_by_id = {
            str(row.get("evidence_id") or ""): row
            for row in hydrated
            if str(row.get("evidence_id") or "")
        }
        return [
            {
                **dict(row),
                **dict(
                    hydration_by_id.get(
                        str(row.get("evidence_id") or ""),
                        {},
                    )
                ),
            }
            for row in rows
        ]

    def _candidate_from_row(
        self,
        row: Mapping[str, Any],
        *,
        expected_channel: RetrievalChannel,
        release_id: str,
        authorized_sources: frozenset[str],
    ) -> _Candidate:
        row_release = str(row.get("release_id") or "")
        row_tenant = str(row.get("tenant_id") or "")
        row_kb = str(row.get("knowledge_base_id") or "")
        source_id = str(row.get("source_document_id") or "")
        external_file_id = str(row.get("external_file_id") or "")
        if row_release != release_id:
            raise PermissionError("PostgreSQL result crossed the pinned release")
        if row_tenant != self.tenant_id or row_kb != self.knowledge_base_id:
            raise PermissionError("PostgreSQL result crossed its tenant or knowledge base")
        if (
            source_id not in authorized_sources
            and external_file_id not in authorized_sources
        ):
            raise PermissionError("PostgreSQL returned evidence outside the source scope")

        unit_metadata = _json_object(row.get("unit_metadata"))
        source_metadata = _json_object(row.get("source_metadata"))
        section_path = _text_tuple(row.get("section_path"))
        table_headers = _text_tuple(row.get("table_headers"))
        table_values = _text_tuple(row.get("table_values"))
        table_units = _text_tuple(row.get("table_units"))
        hydrated_footnotes = _text_tuple(row.get("footnotes"))
        metadata = {
            **source_metadata,
            **unit_metadata,
            "external_file_id": external_file_id,
            "section_path": section_path,
            "table_title": _optional_text(row.get("table_title")),
            "row_label": _optional_text(row.get("row_label")),
            "table_units": table_units,
        }
        bbox = _bounding_box(row)
        coordinate = SourceCoordinate(
            page_number=int(row.get("page_number") or 1),
            printed_page_label=_optional_text(
                row.get("printed_page_label")
            ),
            bounding_box=bbox,
            section_id=_optional_text(row.get("section_id")),
            block_id=_optional_text(row.get("block_id")),
            table_id=_optional_text(row.get("table_id")),
            row_index=_optional_int(row.get("table_row_index")),
            cell_id=_optional_text(row.get("cell_id")),
            character_start=_optional_int(row.get("char_start")),
            character_end=_optional_int(row.get("char_end")),
        )
        channel_value = str(row.get("channel") or expected_channel.value)
        if channel_value != expected_channel.value:
            raise PostgresRuntimeError(
                f"channel query returned {channel_value!r}; "
                f"expected {expected_channel.value!r}"
            )
        raw_score = _finite_score(row.get("raw_score"))
        content = str(row.get("display_text") or "").strip()
        if not content:
            raise PostgresRuntimeError("PostgreSQL evidence content is empty")
        evidence = EvidenceItem(
            evidence_id=str(row.get("evidence_id") or ""),
            release_id=row_release,
            tenant_id=row_tenant,
            knowledge_base_id=row_kb,
            source_document_id=source_id,
            source_version_id=str(row.get("source_version_id") or ""),
            source_sha256=str(row.get("source_sha256") or "").strip(),
            source_type=str(row.get("source_type") or ""),
            title=str(row.get("title") or "").strip(),
            content=content,
            coordinate=coordinate,
            language=_optional_text(
                unit_metadata.get("language")
                or source_metadata.get("language")
            ),
            table_headers=_text_tuple(
                table_headers
                or unit_metadata.get("table_headers")
                or unit_metadata.get("headers")
            ),
            table_values=_text_tuple(
                table_values
                or unit_metadata.get("table_values")
                or unit_metadata.get("values")
            ),
            unit=_optional_text(
                row.get("channel_unit")
                or unit_metadata.get("unit")
                or (table_units[0] if table_units else None)
            ),
            footnotes=_text_tuple(
                hydrated_footnotes
                or unit_metadata.get("footnotes")
            ),
            is_citable=bool(row.get("is_citable", True)),
            generated_summary=bool(row.get("generated_summary", False)),
            metadata=metadata,
        )
        return _Candidate(
            evidence=evidence,
            unit_type=str(row.get("unit_type") or ""),
            raw_score=raw_score,
            channel=expected_channel,
            reason=str(row.get("reason") or expected_channel.value),
        )

    @staticmethod
    def _dedupe_channel(
        candidates: Sequence[_Candidate],
    ) -> list[_Candidate]:
        best: dict[str, _Candidate] = {}
        for candidate in candidates:
            current = best.get(candidate.evidence.evidence_id)
            if current is None or (
                candidate.raw_score,
                candidate.reason,
            ) > (
                current.raw_score,
                current.reason,
            ):
                best[candidate.evidence.evidence_id] = candidate
        return sorted(
            best.values(),
            key=lambda item: (
                -item.raw_score,
                item.evidence.evidence_id,
            ),
        )

    @staticmethod
    def _fuse(
        plan: QueryPlan,
        channel_candidates: Mapping[
            RetrievalChannel,
            Sequence[_Candidate],
        ],
    ) -> tuple[RetrievalResult, ...]:
        fused: dict[str, float] = defaultdict(float)
        channels: dict[str, list[str]] = defaultdict(list)
        reasons: dict[str, list[str]] = defaultdict(list)
        evidence_by_id: dict[str, EvidenceItem] = {}
        unit_type_by_id: dict[str, str] = {}

        for channel in plan.channels:
            ranking = channel_candidates.get(channel, ())
            weight = _CHANNEL_WEIGHT[channel]
            for rank, candidate in enumerate(ranking, start=1):
                evidence_id = candidate.evidence.evidence_id
                current_evidence = evidence_by_id.get(evidence_id)
                evidence_by_id[evidence_id] = (
                    candidate.evidence
                    if current_evidence is None
                    else PostgresEvidenceIndex._merge_evidence(
                        current_evidence,
                        candidate.evidence,
                    )
                )
                unit_type_by_id[evidence_id] = candidate.unit_type
                fused[evidence_id] += weight / (20.0 + rank)
                fused[evidence_id] += (
                    min(max(candidate.raw_score, 0.0), 1.0)
                    * weight
                    * 0.002
                )
                channels[evidence_id].append(channel.value)
                reasons[evidence_id].append(candidate.reason)

        for evidence_id, unit_type in unit_type_by_id.items():
            if unit_type in _STRUCTURED_TYPES and (
                plan.table_intent
                or RetrievalChannel.FACT in plan.channels
            ):
                fused[evidence_id] += 0.05

        ordered = sorted(
            fused,
            key=lambda evidence_id: (
                -fused[evidence_id],
                evidence_by_id[evidence_id].source_document_id,
                evidence_by_id[evidence_id].coordinate.page_number,
                evidence_id,
            ),
        )
        ranked_results: list[RetrievalResult] = []
        seen_locations: set[tuple[object, ...]] = set()
        for evidence_id in ordered:
            evidence = evidence_by_id[evidence_id]
            location = (
                evidence.source_version_id,
                normalize_text(evidence.content),
            )
            if location in seen_locations:
                continue
            seen_locations.add(location)
            channel_values = tuple(dict.fromkeys(channels[evidence_id]))
            projected = replace(
                evidence,
                retrieval_channels=channel_values,
                score=fused[evidence_id],
            )
            if not projected.is_citable or projected.generated_summary:
                continue
            ranked_results.append(
                RetrievalResult(
                    evidence=projected,
                    score=fused[evidence_id],
                    channels=channel_values,
                    reasons=tuple(dict.fromkeys(reasons[evidence_id])),
                )
            )
        ranked_results = list(
            PostgresEvidenceIndex._balance_catalog_identifier_context(
                plan,
                ranked_results,
                unit_type_by_id,
            )
        )
        return PostgresEvidenceIndex._limit_with_unit_diversity(
            plan,
            ranked_results,
            unit_type_by_id,
        )

    @staticmethod
    def _merge_evidence(
        current: EvidenceItem,
        candidate: EvidenceItem,
    ) -> EvidenceItem:
        """Retain the richest canonical projection seen across channels."""

        return replace(
            current,
            language=current.language or candidate.language,
            table_headers=current.table_headers or candidate.table_headers,
            table_values=current.table_values or candidate.table_values,
            unit=current.unit or candidate.unit,
            footnotes=current.footnotes or candidate.footnotes,
            metadata={
                **candidate.metadata,
                **current.metadata,
                "section_path": (
                    current.metadata.get("section_path")
                    or candidate.metadata.get("section_path")
                    or ()
                ),
                "table_title": (
                    current.metadata.get("table_title")
                    or candidate.metadata.get("table_title")
                ),
                "row_label": (
                    current.metadata.get("row_label")
                    or candidate.metadata.get("row_label")
                ),
                "table_units": (
                    current.metadata.get("table_units")
                    or candidate.metadata.get("table_units")
                    or ()
                ),
            },
        )

    @staticmethod
    def _balance_catalog_identifier_context(
        plan: QueryPlan,
        ranked_results: Sequence[RetrievalResult],
        unit_type_by_id: Mapping[str, str],
    ) -> tuple[RetrievalResult, ...]:
        """Keep multi-product catalogue comparisons balanced across anchors."""

        identifiers = _catalog_identifiers(plan)
        single_catalog_table = (
            len(identifiers) == 1
            and (
                plan.table_intent
                or "function" in plan.normalized_query
            )
        )
        if (
            (len(identifiers) < 2 and not single_catalog_table)
            or len(ranked_results) <= plan.top_k
            or plan.top_k < len(identifiers)
        ):
            return tuple(ranked_results)

        buckets: dict[str, list[RetrievalResult]] = {
            identifier: [] for identifier in identifiers
        }
        for item in ranked_results:
            contexts = {
                context
                for reason in item.reasons
                if reason.startswith("identifier_context:")
                for context in reason.removeprefix(
                    "identifier_context:"
                ).split(",")
                if context
            }
            for identifier in identifiers:
                if identifier in contexts:
                    buckets[identifier].append(item)

        if any(not buckets[identifier] for identifier in identifiers):
            return tuple(ranked_results)

        result_rank = {
            item.evidence.evidence_id: rank
            for rank, item in enumerate(ranked_results)
        }
        query_terms = _identifier_context_terms(plan)
        family_context_requested = (
            "famil" in plan.normalized_query
            or "compatible" in plan.normalized_query
        )
        source_options: dict[str, list[str]] = {}
        source_best_rank: dict[tuple[str, str], int] = {}
        source_cost: dict[
            tuple[str, str],
            tuple[int, int, int, int, int],
        ] = {}
        for identifier in identifiers:
            options: list[str] = []
            for item in buckets[identifier]:
                source_id = item.evidence.source_document_id
                source_best_rank.setdefault(
                    (identifier, source_id),
                    result_rank[item.evidence.evidence_id],
                )
                if source_id not in options:
                    options.append(source_id)
            source_options[identifier] = options[:8]
            for source_id in source_options[identifier]:
                source_items = [
                    item
                    for item in buckets[identifier]
                    if item.evidence.source_document_id == source_id
                ]
                numeric_tokens = {
                    token
                    for item in source_items
                    for token in _numeric_evidence_tokens(
                        item.evidence.content
                    )
                }
                covered_query_terms = {
                    term
                    for item in source_items
                    for term in query_terms
                    if term in normalize_text(item.evidence.content)
                }
                family_present = (
                    family_context_requested
                    and identifier == "b-kool"
                    and any(
                        _CATALOG_FAMILY_EVIDENCE_PATTERN.search(
                            item.evidence.content
                        )
                        for item in source_items
                    )
                )
                structured_count = sum(
                    unit_type_by_id.get(
                        item.evidence.evidence_id
                    )
                    in _STRUCTURED_TYPES
                    for item in source_items
                )
                function_present = any(
                    "function"
                    in normalize_text(item.evidence.content)
                    for item in source_items
                )
                if single_catalog_table:
                    source_cost[(identifier, source_id)] = (
                        -int(
                            "function" in plan.normalized_query
                            and function_present
                        ),
                        -min(len(covered_query_terms), 50),
                        -min(len(numeric_tokens), 20),
                        -min(structured_count, 20),
                        source_best_rank[(identifier, source_id)],
                    )
                else:
                    pressure_tokens = {
                        token
                        for item in source_items
                        if (
                            "operating"
                            not in plan.normalized_query
                            or "operating"
                            in normalize_text(
                                item.evidence.content
                            )
                        )
                        for token in _numeric_evidence_tokens(
                            item.evidence.content
                        )
                        if token.endswith(":bar")
                    }
                    pressure_token_count = len(pressure_tokens)
                    context_quality = min(
                        (structured_count * 10)
                        + len(covered_query_terms),
                        1_000,
                    )
                    source_cost[(identifier, source_id)] = (
                        -int(family_present),
                        -min(pressure_token_count, 20),
                        -min(len(numeric_tokens), 20),
                        -context_quality,
                        source_best_rank[(identifier, source_id)],
                    )

        best_assignment: tuple[
            tuple[int, int, int, int, int],
            tuple[str, ...],
        ] | None = None

        def visit_sources(
            index: int,
            used_sources: frozenset[str],
            score: tuple[int, int, int, int, int],
            assignment: tuple[str, ...],
        ) -> None:
            nonlocal best_assignment
            if index >= len(identifiers):
                candidate = (score, assignment)
                if best_assignment is None or candidate < best_assignment:
                    best_assignment = candidate
                return
            identifier = identifiers[index]
            for source_id in source_options[identifier]:
                if source_id in used_sources:
                    continue
                visit_sources(
                    index + 1,
                    used_sources.union({source_id}),
                    tuple(
                        current + cost
                        for current, cost in zip(
                            score,
                            source_cost[(identifier, source_id)],
                        )
                    ),
                    (*assignment, source_id),
                )

        visit_sources(0, frozenset(), (0, 0, 0, 0, 0), ())
        assigned_sources = (
            dict(zip(identifiers, best_assignment[1]))
            if best_assignment is not None
            else {}
        )

        quota = max(1, plan.top_k // len(identifiers))
        selected_ids: set[str] = set()
        for identifier in identifiers:
            added = 0
            assigned_source = assigned_sources.get(identifier)
            candidates = (
                [
                    item
                    for item in ranked_results
                    if item.evidence.source_document_id
                    == assigned_source
                ]
                if (
                    single_catalog_table
                    and assigned_source is not None
                )
                else [
                    item
                    for item in buckets[identifier]
                    if (
                        assigned_source is None
                        or item.evidence.source_document_id
                        == assigned_source
                    )
                ]
            )
            priority: list[RetrievalResult] = []
            if (
                family_context_requested
                and identifier == "b-kool"
            ):
                family_item = next(
                    (
                        item
                        for item in candidates
                        if _CATALOG_FAMILY_EVIDENCE_PATTERN.search(
                            item.evidence.content
                        )
                    ),
                    None,
                )
                if family_item is not None:
                    priority.append(family_item)
            if "function" in plan.normalized_query:
                function_item = min(
                    (
                        item
                        for item in candidates
                        if "function" in normalize_text(
                            item.evidence.content
                        )
                    ),
                    key=lambda item: result_rank[
                        item.evidence.evidence_id
                    ],
                    default=None,
                )
                if function_item is not None:
                    priority.append(function_item)
            for concept in (
                "medium",
                "operating",
                "filling",
                "application",
                "adjust",
            ):
                if concept not in plan.normalized_query:
                    continue

                def concept_priority(
                    item: RetrievalResult,
                ) -> tuple[int, int, int, int, int]:
                    normalized_content = normalize_text(
                        item.evidence.content
                    )
                    label_present = (
                        f"column 1 {concept}"
                        in normalized_content
                        or normalized_content.startswith(concept)
                    )
                    bar_count = (
                        0
                        if concept == "medium"
                        else sum(
                            token.endswith(":bar")
                            for token in _numeric_evidence_tokens(
                                item.evidence.content
                            )
                        )
                    )
                    return (
                        -int(label_present),
                        -int(
                            unit_type_by_id.get(
                                item.evidence.evidence_id
                            )
                            in _STRUCTURED_TYPES
                        ),
                        -bar_count,
                        -sum(
                            term in normalized_content
                            for term in query_terms
                        ),
                        result_rank[item.evidence.evidence_id],
                    )

                concept_item = min(
                    (
                        item
                        for item in candidates
                        if concept in normalize_text(
                            item.evidence.content
                        )
                    ),
                    key=concept_priority,
                    default=None,
                )
                if concept_item is not None:
                    priority.append(concept_item)
            anchor_item = min(
                (
                    item
                    for item in candidates
                    if identifier in normalize_text(
                        item.evidence.content
                    )
                ),
                key=lambda item: (
                    -int(
                        normalize_text(
                            item.evidence.content
                        ).strip()
                        == identifier
                    ),
                    len(item.evidence.content),
                    -sum(
                        term
                        in normalize_text(item.evidence.content)
                        for term in query_terms
                    ),
                    result_rank[item.evidence.evidence_id],
                ),
                default=None,
            )
            if anchor_item is not None:
                priority.append(anchor_item)
            structured_items = [
                item
                for item in candidates
                if unit_type_by_id.get(
                    item.evidence.evidence_id
                )
                in _STRUCTURED_TYPES
            ]
            structured_items.sort(
                key=lambda item: (
                    -sum(
                        term
                        in normalize_text(item.evidence.content)
                        for term in query_terms
                    ),
                    result_rank[item.evidence.evidence_id],
                )
            )
            seen_numeric_tokens: set[str] = set()
            structured_priority_added = False
            for item in structured_items:
                numeric_tokens = _numeric_evidence_tokens(
                    item.evidence.content
                )
                if (
                    not structured_priority_added
                    or (
                        numeric_tokens
                        and not numeric_tokens.issubset(
                            seen_numeric_tokens
                        )
                    )
                ):
                    priority.append(item)
                    structured_priority_added = True
                    seen_numeric_tokens.update(numeric_tokens)
            if "pressure" in plan.normalized_query:
                pressure_items = [
                    (
                        max(
                            float(token.removesuffix(":bar"))
                            for token in _numeric_evidence_tokens(
                                item.evidence.content
                            )
                            if token.endswith(":bar")
                        ),
                        item,
                    )
                    for item in candidates
                    if any(
                        token.endswith(":bar")
                        for token in _numeric_evidence_tokens(
                            item.evidence.content
                        )
                    )
                ]
                pressure_items.sort(
                    key=lambda value: (
                        -value[0],
                        -len(
                            _raw_numeric_evidence_tokens(
                                value[1].evidence.content
                            )
                        ),
                        -sum(
                            term
                            in normalize_text(
                                value[1].evidence.content
                            )
                            for term in query_terms
                        ),
                        result_rank[value[1].evidence.evidence_id],
                    )
                )
                seen_pressure_tokens = {
                    token
                    for item in priority
                    for token in _numeric_evidence_tokens(
                        item.evidence.content
                    )
                    if token.endswith(":bar")
                }
                for _, item in pressure_items:
                    pressure_tokens = {
                        token
                        for token in _numeric_evidence_tokens(
                            item.evidence.content
                        )
                        if token.endswith(":bar")
                    }
                    if pressure_tokens.issubset(
                        seen_pressure_tokens
                    ):
                        continue
                    priority.append(item)
                    seen_pressure_tokens.update(pressure_tokens)
            if query_terms:
                priority.append(
                    min(
                        candidates,
                        key=lambda item: (
                            -sum(
                                term
                                in normalize_text(
                                    item.evidence.content
                                )
                                for term in query_terms
                            ),
                            result_rank[
                                item.evidence.evidence_id
                            ],
                        ),
                    )
                )
            priority.extend(candidates)
            for item in priority:
                evidence_id = item.evidence.evidence_id
                if evidence_id in selected_ids:
                    continue
                selected_ids.add(evidence_id)
                added += 1
                if added >= quota:
                    break

        for item in ranked_results:
            if len(selected_ids) >= plan.top_k:
                break
            selected_ids.add(item.evidence.evidence_id)

        return tuple(
            item
            for item in ranked_results
            if item.evidence.evidence_id in selected_ids
        )

    @staticmethod
    def _limit_with_unit_diversity(
        plan: QueryPlan,
        ranked_results: Sequence[RetrievalResult],
        unit_type_by_id: Mapping[str, str],
    ) -> tuple[RetrievalResult, ...]:
        """Prevent structured rows from starving relevant source prose."""

        if len(ranked_results) <= plan.top_k:
            return tuple(ranked_results)
        if plan.top_k < 4:
            return tuple(ranked_results[: plan.top_k])

        unstructured = [
            item
            for item in ranked_results
            if unit_type_by_id.get(item.evidence.evidence_id)
            not in _STRUCTURED_TYPES
        ]
        context_terms = _identifier_context_terms(plan)
        if _catalog_identifiers(plan) and context_terms:
            original_rank = {
                item.evidence.evidence_id: rank
                for rank, item in enumerate(ranked_results)
            }
            unstructured.sort(
                key=lambda item: (
                    -sum(
                        term in normalize_text(item.evidence.content)
                        for term in context_terms
                    ),
                    original_rank[item.evidence.evidence_id],
                )
            )
        structured_present = any(
            unit_type_by_id.get(item.evidence.evidence_id)
            in _STRUCTURED_TYPES
            for item in ranked_results
        )
        if not unstructured or not structured_present:
            return tuple(ranked_results[: plan.top_k])

        reserve_count = min(
            len(unstructured),
            max(1, plan.top_k // 4),
        )
        if _catalog_identifiers(plan) and plan.top_k >= 8:
            reserve_count = min(
                len(unstructured),
                max(3, reserve_count),
            )
        pending = {
            item.evidence.evidence_id
            for item in unstructured[:reserve_count]
        }
        selected: list[RetrievalResult] = []
        for item in ranked_results:
            evidence_id = item.evidence.evidence_id
            if evidence_id in pending:
                selected.append(item)
                pending.remove(evidence_id)
            elif len(selected) < plan.top_k - len(pending):
                selected.append(item)
            if len(selected) >= plan.top_k and not pending:
                break
        return tuple(selected[: plan.top_k])

    def _vector_literal(self, values: Sequence[float]) -> str:
        vector = tuple(float(value) for value in values)
        if len(vector) != self.embedding_dimensions:
            raise ValueError(
                f"embedding provider returned {len(vector)} dimensions; "
                f"expected {self.embedding_dimensions}"
            )
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding provider returned a non-finite value")
        return "[" + ",".join(format(value, ".12g") for value in vector) + "]"


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise PostgresRuntimeError("PostgreSQL JSON field is invalid") from error
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise PostgresRuntimeError("PostgreSQL JSON field is not an object")


def _text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return (stripped,)
            if isinstance(decoded, list):
                return tuple(str(item) for item in decoded if str(item).strip())
        return (stripped,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return (str(value),)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _finite_score(value: Any) -> float:
    score = float(value or 0.0)
    if not math.isfinite(score):
        raise PostgresRuntimeError("PostgreSQL ranking score is not finite")
    return score


def _bounding_box(row: Mapping[str, Any]) -> BoundingBox | None:
    values = tuple(row.get(name) for name in ("x0", "y0", "x1", "y1"))
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise PostgresRuntimeError("PostgreSQL returned a partial bounding box")
    return BoundingBox(*(float(value) for value in values))
