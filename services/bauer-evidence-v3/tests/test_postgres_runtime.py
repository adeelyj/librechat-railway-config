from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.planner import analyze_query  # noqa: E402
from bauer_evidence_v3.models import ReleaseStatus  # noqa: E402
from bauer_evidence_v3.postgres_runtime import (  # noqa: E402
    PostgresCandidateReleaseRegistry,
    PostgresEvidenceIndex,
    PostgresReleaseRegistry,
    PostgresValidationReleaseRegistry,
)
from bauer_evidence_v3.releases import ReleaseError  # noqa: E402


TENANT_ID = "11111111-1111-4111-8111-111111111111"
KB_ID = "22222222-2222-4222-8222-222222222222"
PRINCIPAL_ID = "33333333-3333-4333-8333-333333333333"
RELEASE_ID = "44444444-4444-4444-8444-444444444444"
SOURCE_ID = "55555555-5555-4555-8555-555555555555"
SOURCE_VERSION_ID = "66666666-6666-4666-8666-666666666666"
ARTIFACT_ID = "77777777-7777-4777-8777-777777777777"
PROVENANCE_ID = "88888888-8888-4888-8888-888888888888"
TABLE_ID = "99999999-9999-4999-8999-999999999999"


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, channel_rows=None, *, candidate_available=True):
        self.channel_rows = channel_rows or {}
        self.candidate_available = candidate_available
        self.executions = []
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def transaction(self):
        return FakeTransaction()

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).split())
        self.executions.append((normalized, tuple(parameters)))
        if "v3:pin_active" in sql:
            return FakeResult(
                [
                    {
                        "release_id": RELEASE_ID,
                        "tenant_id": TENANT_ID,
                        "knowledge_base_id": KB_ID,
                    }
                ]
            )
        if "v3:pin_ready_candidate" in sql:
            if not self.candidate_available:
                return FakeResult()
            return FakeResult(
                [
                    {
                        "release_id": RELEASE_ID,
                        "tenant_id": TENANT_ID,
                        "knowledge_base_id": KB_ID,
                    }
                ]
            )
        if "v3:pin_validating_release" in sql:
            if not self.candidate_available:
                return FakeResult()
            return FakeResult(
                [
                    {
                        "release_id": RELEASE_ID,
                        "tenant_id": TENANT_ID,
                        "knowledge_base_id": KB_ID,
                    }
                ]
            )
        if "v3:readiness:relation" in sql:
            return FakeResult(
                [{"migration_table": "bauer_rag_v3.schema_migrations"}]
            )
        if "v3:readiness:migrations" in sql:
            return FakeResult([{"current_version": 9, "applied_count": 9}])
        if "v3:readiness:active" in sql:
            return FakeResult([{"active_count": 1}])
        if "v3:readiness:candidate" in sql:
            return FakeResult(
                [{"candidate_count": 1 if self.candidate_available else 0}]
            )
        for channel, rows in self.channel_rows.items():
            if f"v3:channel:{channel}" in sql:
                return FakeResult(rows)
        return FakeResult()


class FakeFactory:
    def __init__(self, connection):
        self.connection = connection
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.connection


class FixedEmbedding:
    def embed(self, text):
        vector = [0.0] * 1024
        vector[0] = 1.0
        return tuple(vector)


def evidence_row(
    evidence_id,
    *,
    channel,
    unit_type,
    score,
    content,
    page=23,
    source_id=SOURCE_ID,
    external_file_id="file-public-a",
):
    return {
        "evidence_id": evidence_id,
        "release_id": RELEASE_ID,
        "tenant_id": TENANT_ID,
        "knowledge_base_id": KB_ID,
        "source_document_id": source_id,
        "external_file_id": external_file_id,
        "source_type": "pdf",
        "source_version_id": SOURCE_VERSION_ID,
        "source_sha256": "ab" * 32,
        "title": "Industry brochure",
        "source_metadata": {"language": "en"},
        "unit_type": unit_type,
        "display_text": content,
        "search_text": content,
        "unit_metadata": {
            "table_headers": ["Model", "Maximum pressure"],
            "table_values": ["I 15.11-11-V", "525 bar"],
            "footnotes": ["Final pressure depends on configuration."],
        },
        "is_citable": True,
        "generated_summary": False,
        "page_number": page,
        "printed_page_label": str(page),
        "section_id": None,
        "block_id": None,
        "table_id": TABLE_ID if "table" in unit_type else None,
        "table_row_index": 4 if unit_type == "table_row" else None,
        "cell_id": None,
        "char_start": None,
        "char_end": None,
        "x0": 10.0,
        "y0": 20.0,
        "x1": 200.0,
        "y1": 60.0,
        "artifact_set_id": ARTIFACT_ID,
        "primary_provenance_id": PROVENANCE_ID,
        "raw_score": score,
        "channel": channel,
        "reason": f"{channel}:test",
        "channel_unit": "bar",
    }


class PostgresRuntimeTests(unittest.TestCase):
    def make_registry(self, connection):
        return PostgresReleaseRegistry(
            None,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            connection_factory=FakeFactory(connection),
        )

    def make_index(self, connection, *, embedding_provider=None):
        return PostgresEvidenceIndex(
            None,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            embedding_provider=embedding_provider,
            connection_factory=FakeFactory(connection),
        )

    def test_driver_is_lazily_imported(self):
        path = (
            SERVICE_ROOT
            / "bauer_evidence_v3"
            / "postgres_runtime.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top_level_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        imported = {
            alias.name
            for node in top_level_imports
            for alias in node.names
        }
        self.assertNotIn("psycopg", imported)

    def test_pin_active_sets_local_context_and_requires_ready_release(self):
        connection = FakeConnection()
        registry = self.make_registry(connection)
        release = registry.pin_active(KB_ID)

        self.assertEqual(release.release_id, RELEASE_ID)
        self.assertEqual(release.status, ReleaseStatus.READY)
        self.assertEqual(release.tenant_id, TENANT_ID)
        self.assertEqual(release.knowledge_base_id, KB_ID)
        combined = "\n".join(sql for sql, _ in connection.executions)
        self.assertIn("REPEATABLE READ, READ ONLY", combined)
        self.assertIn("set_config('app.tenant_id', %s, true)", combined)
        self.assertIn("set_config('app.principal_ids', %s, true)", combined)
        self.assertIn("release_row.status = 'ready'", combined)
        self.assertIn("bauer_rag_v3.can_read_kb", combined)
        self.assertNotIn(TENANT_ID, combined)
        self.assertNotIn(KB_ID, combined)

        with self.assertRaises(PermissionError):
            registry.pin_active("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def test_fixed_candidate_pins_ready_authorized_release_without_active(self):
        connection = FakeConnection()
        active_registry = self.make_registry(connection)
        registry = PostgresCandidateReleaseRegistry(
            active_registry,
            candidate_release_id=RELEASE_ID,
        )

        release = registry.pin_active(KB_ID)

        self.assertEqual(release.release_id, RELEASE_ID)
        self.assertEqual(release.status, ReleaseStatus.READY)
        combined = "\n".join(sql for sql, _ in connection.executions)
        self.assertIn("v3:pin_ready_candidate", combined)
        self.assertNotIn("v3:pin_active", combined)
        self.assertNotIn("active_releases", combined)
        self.assertIn("release_row.status = 'ready'", combined)
        self.assertIn("can_read_release", combined)
        candidate_call = next(
            parameters
            for sql, parameters in connection.executions
            if "v3:pin_ready_candidate" in sql
        )
        self.assertEqual(candidate_call, (RELEASE_ID, KB_ID, TENANT_ID))

    def test_fixed_candidate_fails_closed_when_not_ready_or_authorized(self):
        connection = FakeConnection(candidate_available=False)
        registry = PostgresCandidateReleaseRegistry(
            self.make_registry(connection),
            candidate_release_id=RELEASE_ID,
        )
        with self.assertRaisesRegex(ReleaseError, "not an authorized ready"):
            registry.pin_active(KB_ID)

    def test_in_process_evaluator_can_pin_only_validating_release(self):
        connection = FakeConnection()
        registry = PostgresValidationReleaseRegistry(
            self.make_registry(connection),
            release_id=RELEASE_ID,
        )

        release = registry.pin_active(KB_ID)

        self.assertEqual(release.release_id, RELEASE_ID)
        self.assertEqual(release.status, ReleaseStatus.VALIDATING)
        combined = "\n".join(sql for sql, _ in connection.executions)
        self.assertIn("v3:pin_validating_release", combined)
        self.assertIn("release_row.status = 'validating'", combined)
        self.assertNotIn("v3:pin_active", combined)
        self.assertNotIn("active_releases", combined)

        unavailable = PostgresValidationReleaseRegistry(
            self.make_registry(
                FakeConnection(candidate_available=False)
            ),
            release_id=RELEASE_ID,
        )
        with self.assertRaisesRegex(
            ReleaseError,
            "authorized validating",
        ):
            unavailable.pin_active(KB_ID)

    def test_validating_evaluator_binds_validating_status_in_every_query(self):
        prose = evidence_row(
            "unit-prose",
            channel="lexical",
            unit_type="paragraph",
            score=0.7,
            content="Overview of the compressor product family.",
        )
        connection = FakeConnection({"lexical": [prose]})
        index = PostgresEvidenceIndex(
            None,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            release_status=ReleaseStatus.VALIDATING,
            connection_factory=FakeFactory(connection),
        )

        results = index.retrieve(
            analyze_query("compressor overview"),
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        self.assertTrue(results)
        channel_calls = [
            (sql, parameters)
            for sql, parameters in connection.executions
            if "v3:channel:" in sql
        ]
        self.assertTrue(channel_calls)
        for sql, parameters in channel_calls:
            self.assertIn(
                "release_row.status = %s::text",
                sql,
            )
            self.assertEqual(parameters[1], "validating")

        with self.assertRaisesRegex(ValueError, "ready or validating"):
            PostgresEvidenceIndex(
                None,
                tenant_id=TENANT_ID,
                knowledge_base_id=KB_ID,
                principal_ids=(PRINCIPAL_ID,),
                release_status=ReleaseStatus.DRAFT,
                connection_factory=FakeFactory(FakeConnection()),
            )

    def test_retrieval_authorizes_in_every_channel_and_fuses_deterministically(self):
        table = evidence_row(
            "unit-table",
            channel="exact",
            unit_type="table_row",
            score=1.0,
            content="I 15.11-11-V has a maximum pressure of 525 bar.",
        )
        fact = evidence_row(
            "unit-fact",
            channel="fact",
            unit_type="fact",
            score=1.0,
            content="Maximum pressure for I 15.11-11-V: 525 bar.",
        )
        prose = evidence_row(
            "unit-prose",
            channel="lexical",
            unit_type="paragraph",
            score=0.7,
            content="Overview of the I 15.11-11-V product family.",
            page=3,
        )
        rows = {
            "exact": [table],
            "fact": [fact],
            "table": [{**table, "channel": "table"}],
            "lexical": [prose],
            "semantic": [{**table, "channel": "semantic", "raw_score": 0.8}],
            "navigation": [
                {**table, "channel": "navigation", "raw_score": 0.6}
            ],
        }
        connection = FakeConnection(rows)
        index = self.make_index(
            connection,
            embedding_provider=FixedEmbedding(),
        )
        plan = analyze_query(
            "Give a technical table overview and maximum pressure for "
            "I 15.11-11-V."
        )
        results = index.retrieve(
            plan,
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        self.assertTrue(results)
        self.assertEqual(results[0].evidence.evidence_id, "unit-table")
        self.assertIn("exact", results[0].channels)
        self.assertIn("table", results[0].channels)
        self.assertEqual(results[0].evidence.coordinate.page_number, 23)
        self.assertEqual(
            results[0].evidence.coordinate.bounding_box.x1,
            200.0,
        )
        self.assertEqual(
            results[0].evidence.table_values,
            ("I 15.11-11-V", "525 bar"),
        )

        channel_sql = {
            channel: sql
            for sql, _ in connection.executions
            for channel in (
                "exact",
                "fact",
                "table",
                "lexical",
                "semantic",
                "navigation",
            )
            if f"v3:channel:{channel}" in sql
        }
        self.assertEqual(
            set(channel_sql),
            {
                "exact",
                "fact",
                "table",
                "lexical",
                "semantic",
                "navigation",
            },
        )
        for channel, sql in channel_sql.items():
            lowered = sql.casefold()
            if channel == "exact":
                self.assertIn(
                    "exact_term.release_id = request.release_id",
                    lowered,
                )
                self.assertIn(
                    "kb.tenant_id = request.tenant_id",
                    lowered,
                )
                self.assertIn(
                    "source_row.source_id::text = any "
                    "(request.source_ids)",
                    lowered,
                )
                self.assertIn("as materialized", lowered)
            else:
                self.assertIn("unit.release_id = %s::uuid", lowered)
                self.assertIn("kb.tenant_id = %s::uuid", lowered)
                self.assertIn(
                    "source_row.source_id::text = any (%s::text[])",
                    lowered,
                )
            self.assertNotIn("provenance_spans", lowered)
            self.assertNotIn("table_cells source_cell", lowered)
            self.assertNotIn(SOURCE_ID, sql)
        hydration_sql = [
            sql
            for sql, _ in connection.executions
            if "v3:hydrate:candidates" in sql
        ]
        self.assertEqual(len(hydration_sql), 6)
        self.assertTrue(
            all("provenance_spans" in sql for sql in hydration_sql)
        )

    def test_typed_fact_and_table_sql_receive_numeric_comparators_and_ranges(
        self,
    ):
        cases = {
            "pressure > 300 bar": ("gt", "300", None),
            "pressure >= 300 bar": ("gte", "300", None),
            "pressure < 300 bar": ("lt", "300", None),
            "pressure <= 300 bar": ("lte", "300", None),
            "pressure 300 bar": ("eq", "300", None),
            "pressure between 300 and 500 bar": (
                "between",
                "300",
                "500",
            ),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                connection = FakeConnection()
                self.make_index(connection).retrieve(
                    analyze_query(query),
                    release_id=RELEASE_ID,
                    authorized_source_ids=frozenset({SOURCE_ID}),
                )
                channel_calls = [
                    (sql, parameters)
                    for sql, parameters in connection.executions
                    if "v3:channel:" in sql
                ]
                self.assertEqual(
                    {
                        channel
                        for sql, _ in channel_calls
                        for channel in ("fact", "table")
                        if f"v3:channel:{channel}" in sql
                    },
                    {"fact", "table"},
                )
                fact_sql, fact_parameters = next(
                    item
                    for item in channel_calls
                    if "v3:channel:fact" in item[0]
                )
                table_sql, table_parameters = next(
                    item
                    for item in channel_calls
                    if "v3:channel:table" in item[0]
                )
                fact_groups = json.loads(fact_parameters[8])
                table_groups = json.loads(table_parameters[10])
                self.assertEqual(fact_groups, table_groups)
                numeric = fact_groups[0]["alternatives"][0]
                self.assertEqual(
                    (
                        numeric["comparator"],
                        numeric["lower_value"],
                        numeric["upper_value"],
                    ),
                    expected,
                )
                self.assertEqual(numeric["unit"], "bar")
                self.assertIn(
                    "CASE required_constraint.comparator",
                    fact_sql,
                )
                self.assertIn(
                    "numeric_cell.numeric_value",
                    table_sql,
                )
                self.assertNotIn("numeric_values", fact_sql)
                self.assertNotIn("fact.numeric_value = value::numeric", fact_sql)

    def test_multi_value_query_retrieves_across_units_and_all_channels(self):
        connection = FakeConnection()
        self.make_index(
            connection,
            embedding_provider=FixedEmbedding(),
        ).retrieve(
            analyze_query(
                "Compare breathing-air 300 bar with Nitrox 200 bar."
            ),
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        channel_calls = [
            (sql, parameters)
            for sql, parameters in connection.executions
            if "v3:channel:" in sql
        ]
        self.assertEqual(
            {
                channel
                for sql, _ in channel_calls
                for channel in (
                    "exact",
                    "fact",
                    "table",
                    "lexical",
                    "semantic",
                    "navigation",
                )
                if f"v3:channel:{channel}" in sql
            },
            {"exact", "fact", "table", "lexical", "semantic"},
        )
        fact_parameters = next(
            parameters
            for sql, parameters in channel_calls
            if "v3:channel:fact" in sql
        )
        groups = json.loads(fact_parameters[8])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "query")
        self.assertEqual(
            {
                (item["comparator"], item["lower_value"], item["unit"])
                for item in groups[0]["alternatives"]
            },
            {("eq", "300", "bar"), ("eq", "200", "bar")},
        )

    def test_explicit_numeric_filters_stay_on_structured_channels(self):
        connection = FakeConnection()
        self.make_index(
            connection,
            embedding_provider=FixedEmbedding(),
        ).retrieve(
            analyze_query(
                "Find pressure data",
                mandatory_constraints={"pressure": ("300 bar",)},
            ),
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        channel_sql = [
            sql
            for sql, _ in connection.executions
            if "v3:channel:" in sql
        ]
        self.assertEqual(len(channel_sql), 2)
        self.assertTrue(
            all(
                "v3:channel:fact" in sql or "v3:channel:table" in sql
                for sql in channel_sql
            )
        )

    def test_multi_value_results_reserve_room_for_source_prose(self):
        fact_rows = [
            evidence_row(
                f"fact-{index:02d}",
                channel="fact",
                unit_type="fact",
                score=1.0,
                content=f"Structured pressure fact {index}: 300 bar.",
            )
            for index in range(24)
        ]
        prose_rows = [
            evidence_row(
                f"prose-{index}",
                channel="lexical",
                unit_type="paragraph",
                score=0.9,
                content=(
                    f"Relevant source context {index}: breathing air "
                    "300 bar and Nitrox 200 bar."
                ),
            )
            for index in range(5)
        ]
        connection = FakeConnection(
            {
                "fact": fact_rows,
                "table": [],
                "lexical": prose_rows,
                "semantic": [],
            }
        )
        results = self.make_index(
            connection,
            embedding_provider=FixedEmbedding(),
        ).retrieve(
            analyze_query(
                "Compare breathing-air 300 bar with Nitrox 200 bar.",
                top_k=20,
            ),
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        self.assertEqual(len(results), 20)
        self.assertEqual(
            {
                item.evidence.evidence_id
                for item in results
                if item.evidence.evidence_id.startswith("prose-")
            },
            {"prose-0", "prose-1", "prose-2", "prose-3", "prose-4"},
        )

    def test_explicit_physical_page_filters_every_retrieval_channel(self):
        page_40 = evidence_row(
            "page-40",
            channel="lexical",
            unit_type="paragraph",
            score=1.0,
            content="Operating pressure: 350 bar.",
            page=40,
        )
        page_41 = evidence_row(
            "page-41",
            channel="lexical",
            unit_type="paragraph",
            score=0.5,
            content="Operating pressure: 414/420 bar.",
            page=41,
        )
        connection = FakeConnection(
            {
                "lexical": [page_40, page_41],
                "semantic": [
                    {**page_40, "channel": "semantic"},
                    {**page_41, "channel": "semantic"},
                ],
            }
        )
        results = self.make_index(
            connection,
            embedding_provider=FixedEmbedding(),
        ).retrieve(
            analyze_query(
                "For the catalogue physical page 41, find operating "
                "pressure."
            ),
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        self.assertEqual(
            [item.evidence.evidence_id for item in results],
            ["page-41"],
        )

    def test_mandatory_and_forbidden_constraints_are_sql_prefilters(self):
        connection = FakeConnection()
        plan = analyze_query(
            "Find pressure data",
            mandatory_constraints={"medium": ("nitrogen", "air")},
            forbidden_claim_values=("oxygen",),
        )
        self.make_index(connection).retrieve(
            plan,
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        channel_calls = [
            (sql, parameters)
            for sql, parameters in connection.executions
            if "v3:channel:" in sql
        ]
        self.assertTrue(channel_calls)
        for sql, parameters in channel_calls:
            self.assertEqual(
                json.loads(parameters[6]),
                {"medium": ["nitrogen", "air"]},
            )
            self.assertEqual(parameters[7], ["oxygen"])
            self.assertIn("jsonb_each(%s::jsonb)", sql)
            self.assertIn("unnest(%s::text[])", sql)
            self.assertIn("unit.metadata::text", sql)

    def test_numeric_mandatory_alternatives_and_forbidden_values_are_typed(
        self,
    ):
        connection = FakeConnection()
        plan = analyze_query(
            "Find pressure data",
            mandatory_constraints={
                "pressure": ("> 300 bar", ">= 500 bar"),
            },
            forbidden_claim_values=("<= 100 bar",),
        )
        self.make_index(connection).retrieve(
            plan,
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset({SOURCE_ID}),
        )

        fact_sql, fact_parameters = next(
            (sql, parameters)
            for sql, parameters in connection.executions
            if "v3:channel:fact" in sql
        )
        groups = json.loads(fact_parameters[8])
        self.assertEqual(groups[0]["name"], "pressure")
        self.assertEqual(
            [
                item["comparator"]
                for item in groups[0]["alternatives"]
            ],
            ["gt", "gte"],
        )
        forbidden = json.loads(fact_parameters[9])
        self.assertEqual(forbidden[0]["comparator"], "lte")
        self.assertEqual(forbidden[0]["lower_value"], "100")
        self.assertEqual(forbidden[0]["unit"], "bar")
        self.assertIn(
            "fact_query.forbidden_numeric_constraints",
            fact_sql,
        )

    def test_unenforceable_numeric_constraint_fails_before_database_access(
        self,
    ):
        connection = FakeConnection()
        factory = FakeFactory(connection)
        index = PostgresEvidenceIndex(
            None,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            connection_factory=factory,
        )
        plan = analyze_query(
            "pressure between 300 bar and 500 psi"
        )

        self.assertIsNotNone(plan.constraint_failure)
        self.assertEqual(
            index.retrieve(
                plan,
                release_id=RELEASE_ID,
                authorized_source_ids=frozenset({SOURCE_ID}),
            ),
            (),
        )
        self.assertEqual(factory.calls, 0)

    def test_empty_source_scope_never_connects(self):
        connection = FakeConnection()
        factory = FakeFactory(connection)
        index = PostgresEvidenceIndex(
            None,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            principal_ids=(PRINCIPAL_ID,),
            connection_factory=factory,
        )
        results = index.retrieve(
            analyze_query("maximum pressure"),
            release_id=RELEASE_ID,
            authorized_source_ids=frozenset(),
        )
        self.assertEqual(results, ())
        self.assertEqual(factory.calls, 0)

    def test_scope_mismatch_from_database_is_a_hard_failure(self):
        unauthorized = evidence_row(
            "unauthorized",
            channel="lexical",
            unit_type="paragraph",
            score=1.0,
            content="Secret evidence.",
            source_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            external_file_id="secret",
        )
        connection = FakeConnection({"lexical": [unauthorized]})
        index = self.make_index(connection)
        with self.assertRaises(PermissionError):
            index.retrieve(
                analyze_query("secret evidence"),
                release_id=RELEASE_ID,
                authorized_source_ids=frozenset({SOURCE_ID}),
            )

    def test_readiness_checks_exact_migration_and_active_release_count(self):
        connection = FakeConnection()
        readiness = self.make_registry(connection).readiness(
            expected_version=9
        )
        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.current_migration_version, 9)
        self.assertEqual(readiness.applied_migration_count, 9)
        self.assertEqual(readiness.active_release_count, 1)
        self.assertEqual(readiness.selected_release_count, 1)
        self.assertEqual(readiness.release_selection, "active")
        combined = "\n".join(sql for sql, _ in connection.executions)
        self.assertIn("v3:readiness:migrations", combined)
        self.assertIn("v3:readiness:active", combined)
        self.assertIn("release_row.status = 'ready'", combined)

    def test_candidate_readiness_depends_on_candidate_not_active_pointer(self):
        ready_connection = FakeConnection(candidate_available=True)
        registry = PostgresCandidateReleaseRegistry(
            self.make_registry(ready_connection),
            candidate_release_id=RELEASE_ID,
        )
        readiness = registry.readiness(expected_version=9)
        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.release_selection, "fixed_candidate")
        self.assertEqual(readiness.selected_release_count, 1)
        self.assertEqual(readiness.active_release_count, 0)
        readiness_sql = "\n".join(
            sql for sql, _ in ready_connection.executions
        )
        self.assertNotIn("v3:readiness:active", readiness_sql)
        self.assertNotIn("active_releases", readiness_sql)
        candidate_sql = next(
            (sql, parameters)
            for sql, parameters in ready_connection.executions
            if "v3:readiness:candidate" in sql
        )
        self.assertIn("release_row.status = 'ready'", candidate_sql[0])
        self.assertIn("can_read_release", candidate_sql[0])
        self.assertEqual(
            candidate_sql[1],
            (RELEASE_ID, KB_ID, TENANT_ID),
        )

        unavailable = PostgresCandidateReleaseRegistry(
            self.make_registry(
                FakeConnection(candidate_available=False)
            ),
            candidate_release_id=RELEASE_ID,
        ).readiness(expected_version=9)
        self.assertFalse(unavailable.ready)
        self.assertEqual(unavailable.selected_release_count, 0)

    def test_wrong_embedding_dimension_fails_before_semantic_sql(self):
        class WrongEmbedding:
            def embed(self, text):
                return (1.0, 0.0)

        connection = FakeConnection()
        index = self.make_index(
            connection,
            embedding_provider=WrongEmbedding(),
        )
        with self.assertRaisesRegex(ValueError, "dimensions"):
            index.retrieve(
                analyze_query("product overview"),
                release_id=RELEASE_ID,
                authorized_source_ids=frozenset({SOURCE_ID}),
            )
        self.assertFalse(
            any(
                "v3:channel:semantic" in sql
                for sql, _ in connection.executions
            )
        )


if __name__ == "__main__":
    unittest.main()
