from __future__ import annotations

import ast
import re
import sys
import unittest
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ids import sha256_json, stable_id
from bauer_evidence_v3.ingest import compile_source
from bauer_evidence_v3.ingest.canonical import attribute_items
from bauer_evidence_v3.ingest.render import PageRender
from bauer_evidence_v3.object_store import MemoryObjectStore
from bauer_evidence_v3.postgres_compiler import (
    CompilerPersistenceContext,
    PersistenceInvariantError,
    PostgresCompilerPersistence,
    ReleaseStateError,
    _artifact_projection_fingerprint,
    canonical_uuid,
)
from bauer_evidence_v3.projections import project_document


SOURCE = b"""<main>
<h1>BM compressor</h1>
<table>
  <tr><th>Model</th><th>Pressure</th></tr>
  <tr><td>BM 40</td><td>350 bar</td></tr>
</table>
</main>"""
FINGERPRINT = sha256_json(
    {
        "compiler": "bauer-v3-test",
        "parser": "source-native-1",
        "embedding": "text-embedding-test-1024",
    }
)
MANIFEST_SHA = "a" * 64
ARTIFACT_ID = canonical_uuid(
    stable_id("artifact_set", "version", FINGERPRINT)
)
PRINCIPAL_ID = "30000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class FakeJsonb:
    value: object


class FakeCursor:
    def __init__(self, row=None, rows=()):
        self._row = row
        self._rows = list(rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, handler, events):
        self.handler = handler
        self.events = events
        self.calls = []
        self.transactions = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        placeholders = len(re.findall(r"%s", sql))
        if placeholders != len(params):
            raise AssertionError(
                f"placeholder mismatch {placeholders} != {len(params)}: "
                f"{normalized}"
            )
        return self.handler(normalized, params)

    def transaction(self):
        self.transactions += 1
        self.events.append("transaction")
        return nullcontext()


class RecordingObjectStore(MemoryObjectStore):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def put(self, content, *, media_type, suffix=""):
        self.events.append(f"put:{suffix}")
        return super().put(content, media_type=media_type, suffix=suffix)


def compiled_and_projected(release_id="release"):
    compiled = compile_source(
        SOURCE,
        source_name="bm.html",
        declared_media_type="text/html",
    )
    projected = project_document(
        compiled.document,
        release_id=release_id,
        tenant_id="tenant",
        knowledge_base_id="kb",
        source_document_id="source",
        source_version_id="version",
        source_type="html",
        external_file_id="file-bm",
        category_path=("Products",),
        source_metadata={
            "document_number": "N28210",
            "product_tags": ["BM 40"],
            "aliases": ["Junior II"],
        },
    )
    return compiled, projected


def context(release_id="release"):
    return CompilerPersistenceContext(
        tenant_id="tenant",
        knowledge_base_id="kb",
        release_id=release_id,
        manifest_sha256=MANIFEST_SHA,
        external_file_id="file-bm",
        compiler_fingerprint=FINGERPRINT,
        embedding_model_version="text-embedding-test-1024",
        source_type="html",
        ordinal=0,
        source_document_id="source",
        source_version_id="version",
    )


def inserted_handler(sql, params):
    if "FROM bauer_rag_v3.knowledge_releases AS release" in sql:
        return FakeCursor(
            (
                "building",
                MANIFEST_SHA,
                FINGERPRINT,
                "text-embedding-test-1024",
            )
        )
    if "INSERT INTO bauer_rag_v3.objects AS object" in sql:
        return FakeCursor((params[0], params[1], params[2], params[3]))
    if "INSERT INTO bauer_rag_v3.sources AS source" in sql:
        return FakeCursor((canonical_uuid("source"),))
    if "INSERT INTO bauer_rag_v3.source_versions AS version" in sql:
        return FakeCursor((canonical_uuid("version"),))
    if (
        "FROM bauer_rag_v3.artifact_sets" in sql
        and "FOR UPDATE" in sql
    ):
        return FakeCursor(None)
    if "INSERT INTO bauer_rag_v3.artifact_sets" in sql:
        return FakeCursor((params[0], "building", {}))
    if sql.startswith("SELECT (SELECT count(*)"):
        return FakeCursor((1, 1, 1, 1, 1, 4, 5, 0, 1, 4))
    if "UPDATE bauer_rag_v3.artifact_sets AS artifact" in sql:
        return FakeCursor((params[2],))
    if "INSERT INTO bauer_rag_v3.release_sources AS membership" in sql:
        return FakeCursor((params[2],))
    if "INSERT INTO bauer_rag_v3.nav_nodes AS existing_node" in sql:
        return FakeCursor((params[0],))
    return FakeCursor()


class PostgresCompilerPersistenceTests(unittest.TestCase):
    def test_psycopg_and_jsonb_are_lazy(self):
        module_path = (
            SERVICE_ROOT / "bauer_evidence_v3" / "postgres_compiler.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (
                    isinstance(node, ast.Import)
                    and any(
                        alias.name.startswith("psycopg")
                        for alias in node.names
                    )
                )
                or (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").startswith("psycopg")
                )
            )
        ]
        self.assertEqual(imports, [])

    def test_uuidv5_mapping_is_stable_scoped_and_preserves_real_uuids(self):
        logical = "page_0123456789abcdef0123456789abcdef"
        first = canonical_uuid(logical)

        self.assertEqual(first, canonical_uuid(logical))
        self.assertNotEqual(first, canonical_uuid(logical, scope="artifact-a"))
        self.assertNotEqual(
            canonical_uuid(logical, scope="artifact-a"),
            canonical_uuid(logical, scope="artifact-b"),
        )
        self.assertEqual(str(uuid.UUID(first)), first)
        existing = "10000000-0000-0000-0000-000000000001"
        self.assertEqual(canonical_uuid(existing), existing)

    def test_release_scoped_navigation_does_not_poison_artifact_reuse(self):
        _, release_a = compiled_and_projected("release-a")
        _, release_b = compiled_and_projected("release-b")

        self.assertNotEqual(
            {
                node.node_id for node in release_a.navigation_nodes
            },
            {
                node.node_id for node in release_b.navigation_nodes
            },
        )
        self.assertEqual(
            _artifact_projection_fingerprint(release_a),
            _artifact_projection_fingerprint(release_b),
        )

    def test_object_writes_precede_one_transaction_and_full_graph_finalizes(self):
        events = []
        store = RecordingObjectStore(events)
        connection = FakeConnection(inserted_handler, events)
        compiled, projected = compiled_and_projected()
        repository = PostgresCompilerPersistence(
            lambda: nullcontext(connection),
            object_store=store,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )

        result = repository.persist(
            source_bytes=SOURCE,
            compilation=compiled,
            projections=projected,
            context=context(),
        )

        self.assertFalse(result.reused)
        self.assertEqual(connection.transactions, 1)
        self.assertEqual(events[:3], ["put:.html", "put:.canonical.json", "transaction"])
        self.assertEqual(
            [statement for statement, _ in connection.calls[:3]],
            [
                "SELECT set_config('app.tenant_id', %s, true)",
                "SELECT set_config('app.knowledge_base_id', %s, true)",
                "SELECT set_config('app.principal_ids', %s, true)",
            ],
        )
        self.assertEqual(
            connection.calls[2][1],
            ('["30000000-0000-0000-0000-000000000001"]',),
        )
        release_lock = connection.calls[3]
        release_read = connection.calls[4]
        self.assertIn("pg_advisory_xact_lock", release_lock[0])
        self.assertIn("hashtextextended", release_lock[0])
        self.assertIn(
            "FROM bauer_rag_v3.knowledge_releases AS release",
            release_read[0],
        )
        self.assertNotIn("FOR UPDATE", release_read[0])
        sql = "\n".join(statement for statement, _ in connection.calls)
        for table in (
            "objects",
            "sources",
            "source_versions",
            "artifact_sets",
            "pages",
            "sections",
            "blocks",
            "tables",
            "table_segments",
            "table_cells",
            "provenance_spans",
            "facts",
            "fact_provenance",
            "release_sources",
            "search_units",
            "exact_terms",
            "nav_nodes",
            "nav_edges",
            "qa_checks",
        ):
            self.assertIn(f"bauer_rag_v3.{table}", sql)
        self.assertIn(
            "artifact.status = 'building'",
            next(
                statement
                for statement, _ in connection.calls
                if "UPDATE bauer_rag_v3.artifact_sets" in statement
            ),
        )
        source_upsert = next(
            statement
            for statement, _ in connection.calls
            if statement.startswith("INSERT INTO bauer_rag_v3.sources")
        )
        version_upsert = next(
            statement
            for statement, _ in connection.calls
            if statement.startswith(
                "INSERT INTO bauer_rag_v3.source_versions"
            )
        )
        self.assertIn("SET updated_at = source.updated_at", source_upsert)
        self.assertNotIn("source.metadata ||", source_upsert)
        self.assertIn(
            "SET source_version_id = version.source_version_id",
            version_upsert,
        )
        self.assertNotIn("version.discovered_metadata ||", version_upsert)
        json_parameters = [
            parameter
            for _, params in connection.calls
            for parameter in params
            if isinstance(parameter, FakeJsonb)
        ]
        self.assertTrue(json_parameters)
        metadata_values = [
            parameter.value
            for parameter in json_parameters
            if isinstance(parameter.value, dict)
        ]
        self.assertTrue(
            any(
                value.get("canonical_id", "").startswith("page_")
                for value in metadata_values
            )
        )
        self.assertTrue(
            any(
                value.get("source_locator", "").startswith("page:")
                for value in metadata_values
            )
        )
        provenance_quotes = {
            params[0]: params[11]
            for statement, params in connection.calls
            if statement.startswith(
                "INSERT INTO bauer_rag_v3.provenance_spans"
            )
        }
        fact_params = next(
            params
            for statement, params in connection.calls
            if statement.startswith("INSERT INTO bauer_rag_v3.facts")
        )
        self.assertEqual(fact_params[6], "350 bar")
        self.assertEqual(provenance_quotes[fact_params[18]], "350 bar")

    def test_rendered_ocr_pages_are_checksum_registered_and_linked(self):
        events = []
        store = RecordingObjectStore(events)
        connection = FakeConnection(inserted_handler, events)
        compiled, projected = compiled_and_projected()
        page = replace(
            compiled.document.pages[0],
            signals=attribute_items(
                {
                    **dict(compiled.document.pages[0].signals),
                    "ocr_average_confidence": 0.925,
                }
            ),
        )
        render = PageRender(
            page_index=0,
            image_bytes=b"immutable-page-render",
            width_pixels=1200,
            height_pixels=1600,
            dpi=150,
        )
        compiled = replace(
            compiled,
            document=replace(compiled.document, pages=(page,)),
            page_renders=(render,),
        )
        repository = PostgresCompilerPersistence(
            lambda: nullcontext(connection),
            object_store=store,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )

        result = repository.persist(
            source_bytes=SOURCE,
            compilation=compiled,
            projections=projected,
            context=context(),
        )

        self.assertEqual(
            events[:4],
            [
                "put:.html",
                "put:.canonical.json",
                "put:.page.png",
                "transaction",
            ],
        )
        self.assertEqual(result.page_render_objects[0][0], 0)
        self.assertEqual(
            result.page_render_objects[0][1].sha256,
            render.sha256,
        )
        page_call = next(
            call
            for call in connection.calls
            if "INSERT INTO bauer_rag_v3.pages" in call[0]
        )
        self.assertEqual(page_call[1][7], render.sha256)
        self.assertAlmostEqual(page_call[1][8], 0.925)

    def test_metadata_terms_and_navigation_descriptions_are_persisted_safely(self):
        events = []
        store = RecordingObjectStore(events)
        connection = FakeConnection(inserted_handler, events)
        compiled, projected = compiled_and_projected()
        repository = PostgresCompilerPersistence(
            lambda: nullcontext(connection),
            object_store=store,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )

        repository.persist(
            source_bytes=SOURCE,
            compilation=compiled,
            projections=projected,
            context=context(),
        )

        exact_calls = [
            params
            for statement, params in connection.calls
            if statement.startswith("INSERT INTO bauer_rag_v3.exact_terms")
        ]
        exact_values = {
            (params[5], params[7]) for params in exact_calls
        }
        self.assertTrue(
            {
                ("external_file_id", "file-bm"),
                ("filename", "bm.html"),
                ("document_number", "n28210"),
                ("product_tag", "bm 40"),
                ("alias", "junior ii"),
            }.issubset(exact_values)
        )

        search_calls = [
            params
            for statement, params in connection.calls
            if statement.startswith("INSERT INTO bauer_rag_v3.search_units")
        ]
        description_calls = [
            params for params in search_calls if params[7] == "navigation"
        ]
        self.assertEqual(
            len(description_calls),
            len(projected.navigation_nodes),
        )
        description_ids = {params[0] for params in description_calls}
        for params in description_calls:
            self.assertIsNone(params[8])
            self.assertIsNone(params[12])
            self.assertIsNone(params[13])
            self.assertIsNone(params[21])
            self.assertFalse(params[22])
            self.assertTrue(params[23])
            self.assertEqual(params[3], canonical_uuid("source"))

        nav_calls = [
            params
            for statement, params in connection.calls
            if statement.startswith(
                "INSERT INTO bauer_rag_v3.nav_nodes AS existing_node"
            )
        ]
        self.assertEqual(len(nav_calls), len(projected.navigation_nodes))
        self.assertEqual(
            [params[3] for params in nav_calls],
            ["category", "artifact"],
        )
        for params in nav_calls:
            self.assertEqual(params[2], canonical_uuid("source"))
            self.assertIn(params[6], description_ids)
            self.assertNotIn("description", params[7].value)
            self.assertIn(
                "description_search_unit_canonical_id",
                params[7].value,
            )

    def test_release_manifest_mismatch_rejects_compiler_output(self):
        events = []
        store = RecordingObjectStore(events)
        connection = FakeConnection(inserted_handler, events)
        compiled, projected = compiled_and_projected()
        repository = PostgresCompilerPersistence(
            lambda: nullcontext(connection),
            object_store=store,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )

        with self.assertRaisesRegex(ReleaseStateError, "manifest differs"):
            repository.persist(
                source_bytes=SOURCE,
                compilation=compiled,
                projections=projected,
                context=replace(context(), manifest_sha256="c" * 64),
            )

        sql = "\n".join(statement for statement, _ in connection.calls)
        self.assertNotIn("INSERT INTO bauer_rag_v3.sources", sql)

    def test_valid_artifact_is_not_mutated_but_release_projection_is_rebuilt(self):
        events = []
        store = RecordingObjectStore(events)
        compiled, projected = compiled_and_projected("release-b")

        def reused_handler(sql, params):
            if "FROM bauer_rag_v3.knowledge_releases AS release" in sql:
                return FakeCursor(
                    (
                        "building",
                        MANIFEST_SHA,
                        FINGERPRINT,
                        "text-embedding-test-1024",
                    )
                )
            if "INSERT INTO bauer_rag_v3.objects AS object" in sql:
                return FakeCursor((params[0], params[1], params[2], params[3]))
            if "INSERT INTO bauer_rag_v3.sources AS source" in sql:
                return FakeCursor((canonical_uuid("source"),))
            if "INSERT INTO bauer_rag_v3.source_versions AS version" in sql:
                return FakeCursor((canonical_uuid("version"),))
            if (
                "FROM bauer_rag_v3.artifact_sets" in sql
                and "FOR UPDATE" in sql
            ):
                return FakeCursor((ARTIFACT_ID, "valid", 1, {}))
            if "INSERT INTO bauer_rag_v3.release_sources AS membership" in sql:
                return FakeCursor((params[2],))
            if "INSERT INTO bauer_rag_v3.nav_nodes AS existing_node" in sql:
                return FakeCursor((params[0],))
            return FakeCursor()

        connection = FakeConnection(reused_handler, events)
        repository = PostgresCompilerPersistence(
            lambda: nullcontext(connection),
            object_store=store,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )
        result = repository.persist(
            source_bytes=SOURCE,
            compilation=compiled,
            projections=projected,
            context=context("release-b"),
        )

        self.assertTrue(result.reused)
        statements = [sql for sql, _ in connection.calls]
        self.assertFalse(
            any(
                sql.startswith("INSERT INTO bauer_rag_v3.pages")
                for sql in statements
            )
        )
        self.assertFalse(
            any(
                sql.startswith("UPDATE bauer_rag_v3.artifact_sets")
                for sql in statements
            )
        )
        self.assertTrue(
            any(
                sql.startswith("DELETE FROM bauer_rag_v3.search_units")
                for sql in statements
            )
        )
        self.assertTrue(
            any(
                sql.startswith("INSERT INTO bauer_rag_v3.search_units")
                for sql in statements
            )
        )

    def test_nonpublishable_compilation_never_writes_or_opens_database(self):
        events = []
        store = RecordingObjectStore(events)
        connection = FakeConnection(inserted_handler, events)
        compiled, projected = compiled_and_projected()
        quarantined = replace(
            compiled,
            quality=replace(compiled.quality, status="quarantine"),
        )
        repository = PostgresCompilerPersistence(
            lambda: nullcontext(connection),
            object_store=store,
            principal_ids=(PRINCIPAL_ID,),
            jsonb_factory=FakeJsonb,
        )

        with self.assertRaises(PersistenceInvariantError):
            repository.persist(
                source_bytes=SOURCE,
                compilation=quarantined,
                projections=projected,
                context=context(),
            )
        self.assertEqual(events, [])
        self.assertEqual(connection.calls, [])

    def test_empty_principal_context_fails_closed(self):
        with self.assertRaises(ValueError):
            PostgresCompilerPersistence(
                lambda: nullcontext(
                    FakeConnection(inserted_handler, [])
                ),
                object_store=MemoryObjectStore(),
                principal_ids=(),
                jsonb_factory=FakeJsonb,
            )


if __name__ == "__main__":
    unittest.main()
