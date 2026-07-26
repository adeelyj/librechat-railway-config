"""RLS-scoped PostgreSQL capture and persistence for V3 evaluations.

Suites and cases are content-addressed by their deterministic UUIDs and are
verified after an insert-on-conflict.  A run is inserted once as ``running``;
all per-case rows and the terminal ``succeeded`` update are then committed in
one transaction.  Canonical extraction/table observations use an independent
reader-only connection and one release-pinned repeatable-read snapshot.  No
class exposes an operation that edits a completed run or result.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, ContextManager

from .db_security import (
    DatabaseRoleSecurityError,
    verify_runtime_database_role,
)
from .evaluation import (
    EvaluationCase,
    EvaluationManifest,
    EvaluationObservations,
    EvaluationRunReport,
    EvaluationRunSpec,
)


class PostgresEvaluationError(RuntimeError):
    """Base class for PostgreSQL evaluation persistence failures."""


class PostgresEvaluationDependencyError(PostgresEvaluationError):
    """The optional PostgreSQL driver is unavailable."""


class ImmutableEvaluationConflictError(PostgresEvaluationError):
    """A deterministic suite/case UUID already represents different content."""


class EvaluationRunStateError(PostgresEvaluationError):
    """A run is missing, terminal, or does not match the completion payload."""


class EvaluationObservationScopeError(PostgresEvaluationError):
    """Canonical observations could not be proven to belong to the pin."""


class PostgresEvaluationObservationSource:
    """Read canonical compiler observations under one RLS-scoped snapshot."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any]],
        *,
        tenant_id: str,
        knowledge_base_id: str,
        principal_ids: tuple[str, ...],
        enforce_least_privilege: bool = True,
        statement_timeout_ms: int = 60_000,
    ) -> None:
        self._connection_provider = connection_provider
        self.tenant_id = _uuid_text(tenant_id, "tenant_id")
        self.knowledge_base_id = _uuid_text(
            knowledge_base_id,
            "knowledge_base_id",
        )
        self.principal_ids = tuple(
            _uuid_text(value, "principal_ids item") for value in principal_ids
        )
        if not self.principal_ids:
            raise ValueError(
                "principal_ids must contain a reader principal"
            )
        if (
            isinstance(statement_timeout_ms, bool)
            or statement_timeout_ms < 1
        ):
            raise ValueError("statement_timeout_ms must be positive")
        self.enforce_least_privilege = bool(enforce_least_privilege)
        self.statement_timeout_ms = int(statement_timeout_ms)

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        principal_ids: tuple[str, ...],
        enforce_least_privilege: bool = True,
        statement_timeout_ms: int = 60_000,
    ) -> "PostgresEvaluationObservationSource":
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database DSN is required")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - deployment guard
            raise PostgresEvaluationDependencyError(
                "psycopg is required for PostgreSQL observation capture"
            ) from error

        @contextmanager
        def connection_provider() -> Iterator[Any]:
            with psycopg.connect(
                dsn.strip(),
                row_factory=dict_row,
                application_name="bauer-v3-evaluation-capture",
            ) as connection:
                yield connection

        return cls(
            connection_provider,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            principal_ids=principal_ids,
            enforce_least_privilege=enforce_least_privilege,
            statement_timeout_ms=statement_timeout_ms,
        )

    def capture(
        self,
        *,
        manifest: EvaluationManifest,
        release_id: str,
    ) -> EvaluationObservations:
        normalized_release_id = _uuid_text(release_id, "release_id")
        self._verify_manifest_scope(manifest, normalized_release_id)
        target_source_ids = tuple(
            sorted(
                {
                    _uuid_text(str(target["source_id"]), "target source_id")
                    for target in (
                        *manifest.extraction_targets,
                        *manifest.table_targets,
                    )
                }
            )
        )
        if not target_source_ids:
            return EvaluationObservations()

        with self._read_transaction(
            authorized_source_ids=manifest.scope.allowed_source_ids
        ) as connection:
            self._verify_release(
                connection,
                release_id=normalized_release_id,
            )
            self._verify_sources(
                connection,
                release_id=normalized_release_id,
                source_ids=target_source_ids,
            )
            extraction = tuple(
                self._capture_extraction(
                    connection,
                    release_id=normalized_release_id,
                    target=target,
                )
                for target in manifest.extraction_targets
            )
            tables = tuple(
                observation
                for target in manifest.table_targets
                if (
                    observation := self._capture_table(
                        connection,
                        release_id=normalized_release_id,
                        target=target,
                    )
                )
                is not None
            )
        return EvaluationObservations(
            extraction=extraction,
            tables=tables,
        )

    def _verify_manifest_scope(
        self,
        manifest: EvaluationManifest,
        release_id: str,
    ) -> None:
        if (
            manifest.scope.tenant_id != self.tenant_id
            or manifest.scope.knowledge_base_id != self.knowledge_base_id
        ):
            raise EvaluationObservationScopeError(
                "observation source does not match manifest tenant/KB"
            )
        if release_id not in manifest.scope.allowed_release_ids:
            raise EvaluationObservationScopeError(
                "observation release is outside manifest scope"
            )
        allowed_sources = set(manifest.scope.allowed_source_ids)
        requested_sources = {
            str(target["source_id"])
            for target in (
                *manifest.extraction_targets,
                *manifest.table_targets,
            )
        }
        if not requested_sources.issubset(allowed_sources):
            raise EvaluationObservationScopeError(
                "observation target is outside manifest source scope"
            )

    def _verify_release(
        self,
        connection: Any,
        *,
        release_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT
                release_row.release_id::text AS release_id,
                release_row.kb_id::text AS knowledge_base_id,
                kb_row.tenant_id::text AS tenant_id,
                release_row.status
            FROM bauer_rag_v3.knowledge_releases AS release_row
            JOIN bauer_rag_v3.knowledge_bases AS kb_row
              ON kb_row.kb_id = release_row.kb_id
            WHERE release_row.release_id = %s::uuid
              AND release_row.kb_id = %s::uuid
              AND kb_row.tenant_id = %s::uuid
              AND bauer_rag_v3.can_read_release(release_row.release_id)
            """,
            (
                release_id,
                self.knowledge_base_id,
                self.tenant_id,
            ),
        ).fetchone()
        if row is None:
            raise EvaluationObservationScopeError(
                "release is missing or not visible to the observation reader"
            )
        actual = (
            str(_row_value(row, "release_id", 0)),
            str(_row_value(row, "knowledge_base_id", 1)),
            str(_row_value(row, "tenant_id", 2)),
            str(_row_value(row, "status", 3)),
        )
        if actual[:3] != (
            release_id,
            self.knowledge_base_id,
            self.tenant_id,
        ):
            raise EvaluationObservationScopeError(
                "release preflight returned a different tenant/KB/release"
            )
        if actual[3] not in {"validating", "ready"}:
            raise EvaluationObservationScopeError(
                "canonical observations require a validating or ready release"
            )

    def _verify_sources(
        self,
        connection: Any,
        *,
        release_id: str,
        source_ids: tuple[str, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT member.source_id::text AS source_id
            FROM bauer_rag_v3.release_sources AS member
            WHERE member.release_id = %s::uuid
              AND member.kb_id = %s::uuid
              AND member.source_id = ANY(%s::uuid[])
              AND bauer_rag_v3.can_read_release(member.release_id)
              AND bauer_rag_v3.can_read_source(member.source_id)
            ORDER BY member.source_id
            """,
            (release_id, self.knowledge_base_id, list(source_ids)),
        ).fetchall()
        visible = {
            str(_row_value(row, "source_id", 0))
            for row in rows
        }
        if visible != set(source_ids):
            raise EvaluationObservationScopeError(
                "one or more observation sources are absent or unauthorized"
            )

    def _capture_extraction(
        self,
        connection: Any,
        *,
        release_id: str,
        target: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        source_id = str(target["source_id"])
        row = connection.execute(
            """
            SELECT
                kb_row.tenant_id::text AS tenant_id,
                member.kb_id::text AS knowledge_base_id,
                member.release_id::text AS release_id,
                member.source_id::text AS source_id,
                member.source_version_id::text AS source_version_id,
                member.artifact_set_id::text AS artifact_set_id,
                source_version.sha256::text AS source_sha256,
                artifact.status AS artifact_status,
                artifact.quality_summary,
                (
                    SELECT count(*)
                    FROM bauer_rag_v3.pages AS page_row
                    WHERE page_row.artifact_set_id =
                        member.artifact_set_id
                ) AS page_count,
                (
                    SELECT count(*)
                    FROM bauer_rag_v3.blocks AS block_row
                    WHERE block_row.artifact_set_id =
                        member.artifact_set_id
                ) AS block_count,
                (
                    SELECT count(*)
                    FROM bauer_rag_v3.table_cells AS cell_row
                    WHERE cell_row.artifact_set_id =
                        member.artifact_set_id
                ) AS cell_count,
                (
                    SELECT count(*)
                    FROM bauer_rag_v3.search_units AS unit_row
                    WHERE unit_row.release_id = member.release_id
                      AND unit_row.source_id = member.source_id
                      AND unit_row.is_citable
                ) AS evidence_count,
                (
                    SELECT count(*)
                    FROM bauer_rag_v3.search_units AS unit_row
                    JOIN bauer_rag_v3.provenance_spans AS span_row
                      ON span_row.artifact_set_id =
                         unit_row.artifact_set_id
                     AND span_row.provenance_id =
                         unit_row.primary_provenance_id
                    LEFT JOIN bauer_rag_v3.blocks AS span_block
                      ON span_block.artifact_set_id =
                         span_row.artifact_set_id
                     AND span_block.block_id = span_row.block_id
                    LEFT JOIN bauer_rag_v3.table_cells AS span_cell
                      ON span_cell.artifact_set_id =
                         span_row.artifact_set_id
                     AND span_cell.cell_id = span_row.cell_id
                    WHERE unit_row.release_id = member.release_id
                      AND unit_row.source_id = member.source_id
                      AND unit_row.is_citable
                      AND coalesce(
                          span_row.page_id,
                          span_block.page_id,
                          span_cell.page_id
                      ) IS NOT NULL
                ) AS resolvable_evidence_count,
                (
                    SELECT count(*)
                    FROM (
                        SELECT
                            block_row.page_id,
                            block_row.reading_order AS item_order
                        FROM bauer_rag_v3.blocks AS block_row
                        WHERE block_row.artifact_set_id =
                            member.artifact_set_id
                        UNION ALL
                        SELECT DISTINCT
                            segment_row.page_id,
                            CASE
                                WHEN table_row.metadata ->>
                                    'canonical_order' ~ '^[0-9]+$'
                                THEN (
                                    table_row.metadata ->>
                                    'canonical_order'
                                )::integer
                                ELSE -1
                            END AS item_order
                        FROM bauer_rag_v3.tables AS table_row
                        JOIN bauer_rag_v3.table_segments AS segment_row
                          ON segment_row.artifact_set_id =
                             table_row.artifact_set_id
                         AND segment_row.table_id = table_row.table_id
                        WHERE table_row.artifact_set_id =
                            member.artifact_set_id
                          AND table_row.metadata ->> 'parser_id' =
                              'html_source'
                    ) AS ordered_item
                ) AS reading_order_checks,
                (
                    SELECT count(*)
                    FROM (
                        SELECT
                            item.page_id,
                            item.item_order,
                            row_number() OVER (
                                PARTITION BY item.page_id
                                ORDER BY item.item_order
                            ) - 1 AS expected_order
                        FROM (
                            SELECT
                                block_row.page_id,
                                block_row.reading_order AS item_order
                            FROM bauer_rag_v3.blocks AS block_row
                            WHERE block_row.artifact_set_id =
                                member.artifact_set_id
                            UNION ALL
                            SELECT DISTINCT
                                segment_row.page_id,
                                CASE
                                    WHEN table_row.metadata ->>
                                        'canonical_order' ~ '^[0-9]+$'
                                    THEN (
                                        table_row.metadata ->>
                                        'canonical_order'
                                    )::integer
                                    ELSE -1
                                END AS item_order
                            FROM bauer_rag_v3.tables AS table_row
                            JOIN bauer_rag_v3.table_segments AS segment_row
                              ON segment_row.artifact_set_id =
                                 table_row.artifact_set_id
                             AND segment_row.table_id =
                                 table_row.table_id
                            WHERE table_row.artifact_set_id =
                                member.artifact_set_id
                              AND table_row.metadata ->> 'parser_id' =
                                  'html_source'
                        ) AS item
                    ) AS ordered_item
                    WHERE ordered_item.item_order =
                        ordered_item.expected_order
                ) AS reading_order_passes
            FROM bauer_rag_v3.release_sources AS member
            JOIN bauer_rag_v3.knowledge_releases AS release_row
              ON release_row.release_id = member.release_id
             AND release_row.kb_id = member.kb_id
            JOIN bauer_rag_v3.knowledge_bases AS kb_row
              ON kb_row.kb_id = member.kb_id
            JOIN bauer_rag_v3.source_versions AS source_version
              ON source_version.source_id = member.source_id
             AND source_version.source_version_id =
                 member.source_version_id
            JOIN bauer_rag_v3.artifact_sets AS artifact
              ON artifact.artifact_set_id = member.artifact_set_id
             AND artifact.source_version_id =
                 member.source_version_id
            WHERE member.release_id = %s::uuid
              AND member.kb_id = %s::uuid
              AND kb_row.tenant_id = %s::uuid
              AND member.source_id = %s::uuid
              AND bauer_rag_v3.can_read_release(member.release_id)
              AND bauer_rag_v3.can_read_source(member.source_id)
            """,
            (
                release_id,
                self.knowledge_base_id,
                self.tenant_id,
                source_id,
            ),
        ).fetchone()
        if row is None:
            raise EvaluationObservationScopeError(
                "authorized release source disappeared during capture"
            )

        quality = _json_value(_row_value(row, "quality_summary", 8)) or {}
        if not isinstance(quality, Mapping):
            quality = {}
        block_count = int(_row_value(row, "block_count", 10) or 0)
        cell_count = int(_row_value(row, "cell_count", 11) or 0)
        stable_checks = 1 + block_count + cell_count
        unstable_count = sum(
            1
            for issue in quality.get("quality_issues", ())
            if isinstance(issue, Mapping)
            and str(issue.get("code") or "").startswith("unstable_")
        )
        facts = self._capture_facts(
            connection,
            release_id=release_id,
            source_id=source_id,
            artifact_set_id=str(
                _row_value(row, "artifact_set_id", 5)
            ),
            target_facts=target.get("facts") or (),
        )
        artifact_status = str(
            _row_value(row, "artifact_status", 7) or ""
        )
        status = {
            "valid": "published",
            "quarantined": "quarantined",
        }.get(artifact_status, "failed")
        return {
            "tenant_id": str(_row_value(row, "tenant_id", 0)),
            "knowledge_base_id": str(
                _row_value(row, "knowledge_base_id", 1)
            ),
            "release_id": str(_row_value(row, "release_id", 2)),
            "source_id": str(_row_value(row, "source_id", 3)),
            "source_version_id": str(
                _row_value(row, "source_version_id", 4)
            ),
            "artifact_set_id": str(
                _row_value(row, "artifact_set_id", 5)
            ),
            "source_sha256": str(
                _row_value(row, "source_sha256", 6)
            ),
            "status": status,
            "page_count": int(_row_value(row, "page_count", 9) or 0),
            "evidence_count": int(
                _row_value(row, "evidence_count", 12) or 0
            ),
            "resolvable_evidence_count": int(
                _row_value(row, "resolvable_evidence_count", 13) or 0
            ),
            "reading_order_checks": int(
                _row_value(row, "reading_order_checks", 14) or 0
            ),
            "reading_order_passes": int(
                _row_value(row, "reading_order_passes", 15) or 0
            ),
            "stable_id_checks": stable_checks,
            "stable_id_matches": max(
                0,
                stable_checks - unstable_count,
            ),
            "facts": facts,
        }

    def _capture_facts(
        self,
        connection: Any,
        *,
        release_id: str,
        source_id: str,
        artifact_set_id: str,
        target_facts: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        target_ids = {
            str(target.get("fact_id") or "").strip()
            for target in target_facts
            if str(target.get("fact_id") or "").strip()
        }
        if not target_ids:
            return []
        rows = connection.execute(
            """
            SELECT
                fact.fact_id::text AS physical_fact_id,
                fact.metadata ->> 'canonical_id' AS canonical_fact_id,
                fact.value_kind,
                fact.raw_value,
                fact.value_text,
                fact.numeric_value::text AS numeric_value,
                fact.date_value::text AS date_value,
                fact.boolean_value::text AS boolean_value,
                fact.unit_raw,
                fact.unit_ucum,
                fact.primary_provenance_id::text AS provenance_id,
                coalesce(
                    span.page_id,
                    span_block.page_id,
                    span_cell.page_id
                )::text AS page_id
            FROM bauer_rag_v3.release_sources AS member
            JOIN bauer_rag_v3.facts AS fact
              ON fact.artifact_set_id = member.artifact_set_id
            JOIN bauer_rag_v3.provenance_spans AS span
              ON span.artifact_set_id = fact.artifact_set_id
             AND span.provenance_id = fact.primary_provenance_id
            LEFT JOIN bauer_rag_v3.blocks AS span_block
              ON span_block.artifact_set_id = span.artifact_set_id
             AND span_block.block_id = span.block_id
            LEFT JOIN bauer_rag_v3.table_cells AS span_cell
              ON span_cell.artifact_set_id = span.artifact_set_id
             AND span_cell.cell_id = span.cell_id
            WHERE member.release_id = %s::uuid
              AND member.source_id = %s::uuid
              AND member.artifact_set_id = %s::uuid
              AND (
                  fact.fact_id::text = ANY(%s::text[])
                  OR fact.metadata ->> 'canonical_id' =
                     ANY(%s::text[])
              )
              AND bauer_rag_v3.can_read_release(member.release_id)
              AND bauer_rag_v3.can_read_source(member.source_id)
            ORDER BY fact.fact_id
            """,
            (
                release_id,
                source_id,
                artifact_set_id,
                list(target_ids),
                list(target_ids),
            ),
        ).fetchall()
        observations: list[Mapping[str, Any]] = []
        for row in rows:
            value_kind = str(_row_value(row, "value_kind", 2))
            value = {
                "text": _row_value(row, "value_text", 4),
                "numeric": _row_value(row, "numeric_value", 5),
                "date": _row_value(row, "date_value", 6),
                "boolean": _row_value(row, "boolean_value", 7),
            }.get(value_kind)
            if value is None:
                value = _row_value(row, "raw_value", 3)
            observations.append(
                {
                    "fact_id": _preferred_identifier(
                        physical=_row_value(row, "physical_fact_id", 0),
                        canonical=_row_value(
                            row,
                            "canonical_fact_id",
                            1,
                        ),
                        expected=target_ids,
                    ),
                    "value": str(value),
                    "unit": _optional_string(
                        _row_value(row, "unit_ucum", 9)
                        or _row_value(row, "unit_raw", 8)
                    ),
                    "source_id": source_id,
                    "artifact_set_id": artifact_set_id,
                    "provenance_id": str(
                        _row_value(row, "provenance_id", 10)
                    ),
                    "page_id": _optional_string(
                        _row_value(row, "page_id", 11)
                    ),
                    "source_coordinate_resolvable": (
                        _row_value(row, "page_id", 11) is not None
                    ),
                }
            )
        return observations

    def _capture_table(
        self,
        connection: Any,
        *,
        release_id: str,
        target: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        source_id = str(target["source_id"])
        table_id = str(target["table_id"])
        rows = connection.execute(
            """
            SELECT
                kb_row.tenant_id::text AS tenant_id,
                member.kb_id::text AS knowledge_base_id,
                member.release_id::text AS release_id,
                member.source_id::text AS source_id,
                member.source_version_id::text AS source_version_id,
                member.artifact_set_id::text AS artifact_set_id,
                source_version.sha256::text AS source_sha256,
                table_row.table_id::text AS physical_table_id,
                table_row.metadata ->> 'canonical_id'
                    AS canonical_table_id,
                table_row.row_count,
                table_row.column_count
            FROM bauer_rag_v3.release_sources AS member
            JOIN bauer_rag_v3.knowledge_bases AS kb_row
              ON kb_row.kb_id = member.kb_id
            JOIN bauer_rag_v3.source_versions AS source_version
              ON source_version.source_id = member.source_id
             AND source_version.source_version_id =
                 member.source_version_id
            JOIN bauer_rag_v3.tables AS table_row
              ON table_row.artifact_set_id = member.artifact_set_id
            WHERE member.release_id = %s::uuid
              AND member.kb_id = %s::uuid
              AND kb_row.tenant_id = %s::uuid
              AND member.source_id = %s::uuid
              AND (
                  table_row.table_id::text = %s
                  OR table_row.metadata ->> 'canonical_id' = %s
              )
              AND bauer_rag_v3.can_read_release(member.release_id)
              AND bauer_rag_v3.can_read_source(member.source_id)
            ORDER BY table_row.table_id
            """,
            (
                release_id,
                self.knowledge_base_id,
                self.tenant_id,
                source_id,
                table_id,
                table_id,
            ),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise EvaluationObservationScopeError(
                "table target resolved to multiple canonical tables"
            )
        row = rows[0]
        artifact_set_id = str(
            _row_value(row, "artifact_set_id", 5)
        )
        physical_table_id = str(
            _row_value(row, "physical_table_id", 7)
        )
        cells = self._capture_table_cells(
            connection,
            release_id=release_id,
            source_id=source_id,
            artifact_set_id=artifact_set_id,
            physical_table_id=physical_table_id,
            target_cells=target.get("cells") or (),
        )
        return {
            "tenant_id": str(_row_value(row, "tenant_id", 0)),
            "knowledge_base_id": str(
                _row_value(row, "knowledge_base_id", 1)
            ),
            "release_id": str(_row_value(row, "release_id", 2)),
            "source_id": str(_row_value(row, "source_id", 3)),
            "source_version_id": str(
                _row_value(row, "source_version_id", 4)
            ),
            "artifact_set_id": artifact_set_id,
            "source_sha256": str(
                _row_value(row, "source_sha256", 6)
            ),
            "table_id": table_id,
            "row_count": int(_row_value(row, "row_count", 9)),
            "column_count": int(
                _row_value(row, "column_count", 10)
            ),
            "cells": cells,
        }

    def _capture_table_cells(
        self,
        connection: Any,
        *,
        release_id: str,
        source_id: str,
        artifact_set_id: str,
        physical_table_id: str,
        target_cells: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        expected_ids = {
            str(target.get("cell_id") or "").strip()
            for target in target_cells
            if str(target.get("cell_id") or "").strip()
        }
        rows = connection.execute(
            """
            SELECT
                cell.cell_id::text AS physical_cell_id,
                cell.metadata ->> 'canonical_id' AS canonical_cell_id,
                cell.page_id::text AS page_id,
                page_row.page_number,
                cell.row_index,
                cell.column_index,
                cell.row_span,
                cell.column_span,
                cell.raw_text,
                cell.numeric_value::text AS numeric_value,
                cell.unit_raw,
                cell.unit_ucum,
                cell.x0,
                cell.y0,
                cell.x1,
                cell.y1
            FROM bauer_rag_v3.release_sources AS member
            JOIN bauer_rag_v3.table_cells AS cell
              ON cell.artifact_set_id = member.artifact_set_id
             AND cell.table_id = %s::uuid
            JOIN bauer_rag_v3.pages AS page_row
              ON page_row.artifact_set_id = cell.artifact_set_id
             AND page_row.page_id = cell.page_id
            WHERE member.release_id = %s::uuid
              AND member.source_id = %s::uuid
              AND member.artifact_set_id = %s::uuid
              AND bauer_rag_v3.can_read_release(member.release_id)
              AND bauer_rag_v3.can_read_source(member.source_id)
            ORDER BY cell.row_index, cell.column_index, cell.cell_id
            """,
            (
                physical_table_id,
                release_id,
                source_id,
                artifact_set_id,
            ),
        ).fetchall()
        result: list[Mapping[str, Any]] = []
        for row in rows:
            numeric_value = _row_value(row, "numeric_value", 9)
            raw_value = _row_value(row, "raw_text", 8)
            result.append(
                {
                    "cell_id": _preferred_identifier(
                        physical=_row_value(row, "physical_cell_id", 0),
                        canonical=_row_value(
                            row,
                            "canonical_cell_id",
                            1,
                        ),
                        expected=expected_ids,
                    ),
                    "value": str(
                        numeric_value
                        if numeric_value is not None
                        else raw_value
                    ),
                    "unit": _optional_string(
                        _row_value(row, "unit_ucum", 11)
                        or _row_value(row, "unit_raw", 10)
                    ),
                    "row_index": int(
                        _row_value(row, "row_index", 4)
                    ),
                    "column_index": int(
                        _row_value(row, "column_index", 5)
                    ),
                    "row_span": int(_row_value(row, "row_span", 6)),
                    "column_span": int(
                        _row_value(row, "column_span", 7)
                    ),
                    "source_id": source_id,
                    "artifact_set_id": artifact_set_id,
                    "page_id": str(_row_value(row, "page_id", 2)),
                    "page_number": int(
                        _row_value(row, "page_number", 3)
                    ),
                    "bbox": [
                        _row_value(row, "x0", 12),
                        _row_value(row, "y0", 13),
                        _row_value(row, "x1", 14),
                        _row_value(row, "y1", 15),
                    ],
                    "source_coordinate_resolvable": True,
                }
            )
        return result

    @contextmanager
    def _read_transaction(
        self,
        *,
        authorized_source_ids: Sequence[str],
    ) -> Iterator[Any]:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            if self.enforce_least_privilege:
                self._verify_reader_only_role(connection)
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
                (json.dumps(self.principal_ids, separators=(",", ":")),),
            )
            connection.execute(
                "SELECT set_config('app.authorized_source_ids', %s, true)",
                (
                    json.dumps(
                        tuple(authorized_source_ids),
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
    def _verify_reader_only_role(connection: Any) -> str:
        role_name = verify_runtime_database_role(
            connection,
            required_group_role="bauer_rag_v3_reader",
        )
        row = connection.execute(
            """
            SELECT
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
                    'bauer_rag_v3_admin',
                    'member'
                ) AS has_admin_membership
            """
        ).fetchone()
        if row is None:
            raise DatabaseRoleSecurityError(
                "observation reader privilege level could not be inspected"
            )
        if (
            bool(_row_value(row, "has_ingester_membership", 0))
            or bool(_row_value(row, "has_evaluator_membership", 1))
            or bool(_row_value(row, "has_admin_membership", 2))
        ):
            raise DatabaseRoleSecurityError(
                "observation capture requires a reader-only database login"
            )
        return role_name

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_provider()
        with resource as connection:
            yield connection


class PostgresEvaluationStore:
    """Persist one tenant's suites and runs under transaction-local RLS state."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any]],
        *,
        tenant_id: str,
        principal_ids: tuple[str, ...],
        jsonb_factory: Callable[[Any], Any],
    ) -> None:
        self._connection_provider = connection_provider
        self.tenant_id = _uuid_text(tenant_id, "tenant_id")
        self.principal_ids = tuple(
            _uuid_text(value, "principal_ids item") for value in principal_ids
        )
        if not self.principal_ids:
            raise ValueError(
                "principal_ids must contain an evaluation principal with the "
                "app-level admin grant"
            )
        self._jsonb_factory = jsonb_factory

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        tenant_id: str,
        principal_ids: tuple[str, ...],
    ) -> "PostgresEvaluationStore":
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database DSN is required")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as error:  # pragma: no cover - deployment guard
            raise PostgresEvaluationDependencyError(
                "psycopg is required for PostgreSQL evaluation persistence"
            ) from error

        @contextmanager
        def connection_provider() -> Iterator[Any]:
            with psycopg.connect(dsn.strip(), row_factory=dict_row) as connection:
                yield connection

        return cls(
            connection_provider,
            tenant_id=tenant_id,
            principal_ids=principal_ids,
            jsonb_factory=Jsonb,
        )

    def verify_database_role(self) -> str:
        """Fail closed unless the login is an exact non-owner evaluator."""

        with self._connection() as connection:
            return verify_runtime_database_role(
                connection,
                required_group_role="bauer_rag_v3_evaluator",
            )

    def prepare_suite(self, manifest: EvaluationManifest) -> None:
        if manifest.scope.tenant_id != self.tenant_id:
            raise ImmutableEvaluationConflictError(
                "manifest tenant does not match evaluation store tenant"
            )
        metadata = dict(manifest.metadata)
        metadata["split"] = manifest.split
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            connection.execute(
                """
                INSERT INTO bauer_rag_v3.eval_suites (
                    eval_suite_id,
                    tenant_id,
                    suite_key,
                    suite_version,
                    manifest_sha256,
                    gold_status,
                    metadata,
                    verified_at,
                    verified_by
                )
                VALUES (
                    %s::uuid,
                    %s::uuid,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (eval_suite_id) DO NOTHING
                """,
                (
                    manifest.eval_suite_id,
                    manifest.scope.tenant_id,
                    manifest.suite_key,
                    manifest.suite_version,
                    manifest.manifest_sha256,
                    manifest.gold_status,
                    self._jsonb(metadata),
                    manifest.verified_at,
                    manifest.verified_by,
                ),
            )
            suite_row = connection.execute(
                """
                SELECT
                    eval_suite_id::text AS eval_suite_id,
                    tenant_id::text AS tenant_id,
                    suite_key,
                    suite_version,
                    manifest_sha256,
                    gold_status,
                    metadata,
                    verified_at,
                    verified_by
                FROM bauer_rag_v3.eval_suites
                WHERE eval_suite_id = %s::uuid
                """,
                (manifest.eval_suite_id,),
            ).fetchone()
            self._verify_suite_row(
                suite_row,
                manifest=manifest,
                metadata=metadata,
            )

            for case in manifest.cases:
                self._prepare_case(
                    connection,
                    manifest=manifest,
                    case=case,
                )
            stored_case_rows = connection.execute(
                """
                SELECT eval_case_id::text, case_key
                FROM bauer_rag_v3.eval_cases
                WHERE eval_suite_id = %s::uuid
                ORDER BY case_key
                """,
                (manifest.eval_suite_id,),
            ).fetchall()
            stored_cases = {
                str(_row_value(row, "eval_case_id", 0)): str(
                    _row_value(row, "case_key", 1)
                )
                for row in stored_case_rows
            }
            expected_cases = {
                case.eval_case_id: case.case_key for case in manifest.cases
            }
            if stored_cases != expected_cases:
                raise ImmutableEvaluationConflictError(
                    "stored evaluation cases do not exactly match manifest"
                )

    def begin_run(self, spec: EvaluationRunSpec) -> None:
        self._verify_spec_tenant(spec)
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            release_row = connection.execute(
                """
                SELECT
                    release_row.release_id::text AS release_id,
                    release_row.kb_id::text AS kb_id,
                    kb_row.tenant_id::text AS tenant_id,
                    release_row.status
                FROM bauer_rag_v3.knowledge_releases AS release_row
                JOIN bauer_rag_v3.knowledge_bases AS kb_row
                  ON kb_row.kb_id = release_row.kb_id
                WHERE release_row.release_id = %s::uuid
                """,
                (spec.release_id,),
            ).fetchone()
            if release_row is None:
                raise EvaluationRunStateError(
                    "release is missing or not visible to the evaluation principal"
                )
            actual_scope = (
                str(_row_value(release_row, "release_id", 0)),
                str(_row_value(release_row, "kb_id", 1)),
                str(_row_value(release_row, "tenant_id", 2)),
            )
            if actual_scope != (
                spec.release_id,
                spec.knowledge_base_id,
                spec.tenant_id,
            ):
                raise EvaluationRunStateError(
                    "release does not match the run tenant/knowledge-base scope"
                )
            release_status = str(_row_value(release_row, "status", 3))
            if release_status not in {"validating", "ready"}:
                raise EvaluationRunStateError(
                    "new evaluation runs require a validating or ready release"
                )
            cursor = connection.execute(
                """
                INSERT INTO bauer_rag_v3.eval_runs (
                    eval_run_id,
                    tenant_id,
                    kb_id,
                    release_id,
                    eval_suite_id,
                    status,
                    passed,
                    code_version,
                    model_version,
                    prompt_version,
                    repetitions,
                    aggregate_metrics,
                    hard_failures,
                    started_at
                )
                VALUES (
                    %s::uuid,
                    %s::uuid,
                    %s::uuid,
                    %s::uuid,
                    %s::uuid,
                    'running',
                    NULL,
                    %s,
                    %s,
                    %s,
                    %s,
                    '{}'::jsonb,
                    '{}'::text[],
                    %s
                )
                """,
                (
                    spec.eval_run_id,
                    spec.tenant_id,
                    spec.knowledge_base_id,
                    spec.release_id,
                    spec.eval_suite_id,
                    spec.code_version,
                    spec.model_version,
                    spec.prompt_version,
                    spec.repetitions,
                    spec.started_at,
                ),
            )
            if _rowcount(cursor) != 1:
                raise EvaluationRunStateError("evaluation run was not inserted")

    def complete_run(self, report: EvaluationRunReport) -> None:
        spec = report.spec
        self._verify_spec_tenant(spec)
        self._validate_report(report)
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            run_row = connection.execute(
                """
                SELECT
                    status,
                    tenant_id::text AS tenant_id,
                    kb_id::text AS kb_id,
                    release_id::text AS release_id,
                    eval_suite_id::text AS eval_suite_id,
                    repetitions
                FROM bauer_rag_v3.eval_runs
                WHERE eval_run_id = %s::uuid
                FOR UPDATE
                """,
                (spec.eval_run_id,),
            ).fetchone()
            if run_row is None:
                raise EvaluationRunStateError("evaluation run does not exist")
            stored_contract = (
                str(_row_value(run_row, "status", 0)),
                str(_row_value(run_row, "tenant_id", 1)),
                str(_row_value(run_row, "kb_id", 2)),
                str(_row_value(run_row, "release_id", 3)),
                str(_row_value(run_row, "eval_suite_id", 4)),
                int(_row_value(run_row, "repetitions", 5)),
            )
            expected_contract = (
                "running",
                spec.tenant_id,
                spec.knowledge_base_id,
                spec.release_id,
                spec.eval_suite_id,
                spec.repetitions,
            )
            if stored_contract != expected_contract:
                raise EvaluationRunStateError(
                    "evaluation run is terminal or its immutable scope changed"
                )

            case_ids = sorted(
                {result.eval_case_id for result in report.results}
            )
            if case_ids:
                visible_case_rows = connection.execute(
                    """
                    SELECT eval_case_id::text AS eval_case_id
                    FROM bauer_rag_v3.eval_cases
                    WHERE eval_suite_id = %s::uuid
                      AND eval_case_id = ANY(%s::uuid[])
                    """,
                    (spec.eval_suite_id, case_ids),
                ).fetchall()
                visible_case_ids = {
                    str(_row_value(row, "eval_case_id", 0))
                    for row in visible_case_rows
                }
                if visible_case_ids != set(case_ids):
                    raise EvaluationRunStateError(
                        "run results contain a case outside the pinned suite"
                    )

            for result in report.results:
                connection.execute(
                    """
                    INSERT INTO bauer_rag_v3.eval_results (
                        eval_result_id,
                        eval_run_id,
                        eval_case_id,
                        repetition,
                        passed,
                        scores,
                        hard_failures,
                        evidence_search_unit_ids,
                        latency_ms,
                        details
                    )
                    VALUES (
                        %s::uuid,
                        %s::uuid,
                        %s::uuid,
                        %s,
                        %s,
                        %s,
                        %s::text[],
                        %s::uuid[],
                        %s,
                        %s
                    )
                    """,
                    (
                        result.eval_result_id,
                        spec.eval_run_id,
                        result.eval_case_id,
                        result.repetition,
                        result.passed,
                        self._jsonb(result.scores),
                        list(result.hard_failures),
                        list(result.evidence_search_unit_ids),
                        result.latency_ms,
                        self._jsonb(result.details),
                    ),
                )
            cursor = connection.execute(
                """
                UPDATE bauer_rag_v3.eval_runs
                SET
                    status = 'succeeded',
                    passed = %s,
                    aggregate_metrics = %s,
                    hard_failures = %s::text[],
                    completed_at = %s,
                    error = NULL
                WHERE eval_run_id = %s::uuid
                  AND status = 'running'
                """,
                (
                    report.passed,
                    self._jsonb(report.aggregate_metrics),
                    list(report.hard_failures),
                    report.completed_at,
                    spec.eval_run_id,
                ),
            )
            if _rowcount(cursor) != 1:
                raise EvaluationRunStateError(
                    "evaluation run could not be finalized"
                )

    def fail_run(
        self,
        spec: EvaluationRunSpec,
        *,
        completed_at: datetime,
        error: str,
    ) -> None:
        self._verify_spec_tenant(spec)
        if completed_at.tzinfo is None:
            raise ValueError("completed_at must include a timezone")
        normalized_error = str(error).strip()[:2_000]
        if not normalized_error:
            normalized_error = "evaluation failed without an error message"
        with self._connection() as connection, connection.transaction():
            self._set_context(connection)
            cursor = connection.execute(
                """
                UPDATE bauer_rag_v3.eval_runs
                SET
                    status = 'failed',
                    passed = NULL,
                    completed_at = %s,
                    error = %s
                WHERE eval_run_id = %s::uuid
                  AND status = 'running'
                """,
                (
                    completed_at.astimezone(UTC),
                    normalized_error,
                    spec.eval_run_id,
                ),
            )
            if _rowcount(cursor) != 1:
                raise EvaluationRunStateError(
                    "only a running evaluation can be marked failed"
                )

    def _prepare_case(
        self,
        connection: Any,
        *,
        manifest: EvaluationManifest,
        case: EvaluationCase,
    ) -> None:
        connection.execute(
            """
            INSERT INTO bauer_rag_v3.eval_cases (
                eval_case_id,
                eval_suite_id,
                case_key,
                split,
                category,
                prompt,
                expected,
                hard_failure_codes,
                metadata
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s::text[],
                %s
            )
            ON CONFLICT (eval_case_id) DO NOTHING
            """,
            (
                case.eval_case_id,
                manifest.eval_suite_id,
                case.case_key,
                case.split,
                case.category,
                self._jsonb(case.prompt_payload),
                self._jsonb(case.expected_payload),
                list(case.hard_failure_codes),
                self._jsonb(case.metadata),
            ),
        )
        row = connection.execute(
            """
            SELECT
                eval_case_id::text AS eval_case_id,
                eval_suite_id::text AS eval_suite_id,
                case_key,
                split,
                category,
                prompt,
                expected,
                hard_failure_codes,
                metadata
            FROM bauer_rag_v3.eval_cases
            WHERE eval_case_id = %s::uuid
            """,
            (case.eval_case_id,),
        ).fetchone()
        if row is None:
            raise ImmutableEvaluationConflictError(
                f"evaluation case is not visible after insert: {case.case_key}"
            )
        actual = (
            str(_row_value(row, "eval_case_id", 0)),
            str(_row_value(row, "eval_suite_id", 1)),
            str(_row_value(row, "case_key", 2)),
            str(_row_value(row, "split", 3)),
            str(_row_value(row, "category", 4)),
            _json_value(_row_value(row, "prompt", 5)),
            _json_value(_row_value(row, "expected", 6)),
            tuple(_row_value(row, "hard_failure_codes", 7) or ()),
            _json_value(_row_value(row, "metadata", 8)),
        )
        expected = (
            case.eval_case_id,
            manifest.eval_suite_id,
            case.case_key,
            case.split,
            case.category,
            case.prompt_payload,
            case.expected_payload,
            case.hard_failure_codes,
            dict(case.metadata),
        )
        if actual != expected:
            raise ImmutableEvaluationConflictError(
                f"evaluation case UUID has conflicting content: {case.case_key}"
            )

    def _verify_suite_row(
        self,
        row: Any,
        *,
        manifest: EvaluationManifest,
        metadata: Mapping[str, Any],
    ) -> None:
        if row is None:
            raise ImmutableEvaluationConflictError(
                "evaluation suite is not visible after insert"
            )
        actual = (
            str(_row_value(row, "eval_suite_id", 0)),
            str(_row_value(row, "tenant_id", 1)),
            str(_row_value(row, "suite_key", 2)),
            str(_row_value(row, "suite_version", 3)),
            str(_row_value(row, "manifest_sha256", 4)),
            str(_row_value(row, "gold_status", 5)),
            _json_value(_row_value(row, "metadata", 6)),
            _datetime_value(_row_value(row, "verified_at", 7)),
            _optional_db_text(_row_value(row, "verified_by", 8)),
        )
        expected = (
            manifest.eval_suite_id,
            manifest.scope.tenant_id,
            manifest.suite_key,
            manifest.suite_version,
            manifest.manifest_sha256,
            manifest.gold_status,
            dict(metadata),
            (
                manifest.verified_at.astimezone(UTC)
                if manifest.verified_at is not None
                else None
            ),
            manifest.verified_by,
        )
        if actual != expected:
            raise ImmutableEvaluationConflictError(
                "evaluation suite UUID has conflicting content"
            )

    def _validate_report(self, report: EvaluationRunReport) -> None:
        if report.status != "succeeded":
            raise EvaluationRunStateError(
                "complete_run accepts only succeeded evaluator reports"
            )
        observation_target_count = sum(
            int(
                (
                    report.aggregate_metrics.get(metric_name) or {}
                ).get("target_count", 0)
            )
            for metric_name in ("extraction_qa", "table_integrity")
        )
        if not report.results and observation_target_count < 1:
            raise EvaluationRunStateError(
                "evaluation run must contain cases or structured targets"
            )
        pairs = [
            (result.eval_case_id, result.repetition)
            for result in report.results
        ]
        if len(pairs) != len(set(pairs)):
            raise EvaluationRunStateError(
                "evaluation run contains duplicate case/repetition results"
            )
        if report.results:
            expected_repetitions = set(
                range(1, report.spec.repetitions + 1)
            )
            case_sets = {
                repetition: {
                    result.eval_case_id
                    for result in report.results
                    if result.repetition == repetition
                }
                for repetition in expected_repetitions
            }
            first = case_sets.get(1, set())
            if not first or any(
                value != first for value in case_sets.values()
            ):
                raise EvaluationRunStateError(
                    "every repetition must contain the same non-empty case set"
                )
        for result in report.results:
            _uuid_text(result.eval_result_id, "eval_result_id")
            _uuid_text(result.eval_case_id, "eval_case_id")
            if result.latency_ms < 0:
                raise EvaluationRunStateError("latency_ms cannot be negative")
            if result.passed and result.hard_failures:
                raise EvaluationRunStateError(
                    "a passing case result cannot contain hard failures"
                )
        expected_pass = (
            all(result.passed for result in report.results)
            and not report.observation_hard_failures
        )
        if report.passed != expected_pass:
            raise EvaluationRunStateError(
                "run passed flag does not match its per-case results"
            )
        expected_hard_failures = tuple(
            sorted(
                {
                    code
                    for result in report.results
                    for code in result.hard_failures
                }.union(report.observation_hard_failures)
            )
        )
        if report.hard_failures != expected_hard_failures:
            raise EvaluationRunStateError(
                "run hard failures do not match its per-case results"
            )

    def _verify_spec_tenant(self, spec: EvaluationRunSpec) -> None:
        if spec.tenant_id != self.tenant_id:
            raise EvaluationRunStateError(
                "run tenant does not match evaluation store tenant"
            )
        for value, label in (
            (spec.eval_run_id, "eval_run_id"),
            (spec.tenant_id, "tenant_id"),
            (spec.knowledge_base_id, "knowledge_base_id"),
            (spec.release_id, "release_id"),
            (spec.eval_suite_id, "eval_suite_id"),
        ):
            _uuid_text(value, label)
        if spec.started_at.tzinfo is None:
            raise EvaluationRunStateError(
                "run started_at must include a timezone"
            )

    def _set_context(self, connection: Any) -> None:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (self.tenant_id,),
        )
        connection.execute(
            "SELECT set_config('app.principal_ids', %s, true)",
            (json.dumps(self.principal_ids, separators=(",", ":")),),
        )

    def _jsonb(self, value: Any) -> Any:
        return self._jsonb_factory(value)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_provider()
        with resource as connection:
            yield connection


def _row_value(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _rowcount(cursor: Any) -> int:
    value = getattr(cursor, "rowcount", None)
    if value is None:
        return 1
    return int(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    if hasattr(value, "obj"):
        return value.obj
    if hasattr(value, "value"):
        return value.value
    return value


def _datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ImmutableEvaluationConflictError(
            "stored verification timestamp has an invalid type"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_db_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _preferred_identifier(
    *,
    physical: Any,
    canonical: Any,
    expected: set[str],
) -> str:
    physical_text = str(physical or "").strip()
    canonical_text = str(canonical or "").strip()
    if canonical_text in expected:
        return canonical_text
    if physical_text in expected:
        return physical_text
    return canonical_text or physical_text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _uuid_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a UUID")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as error:
        raise ValueError(f"{label} must be a UUID") from error
