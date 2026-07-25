from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.compile_handler import (  # noqa: E402
    CompilationJobHandler,
    embed_projection_bundle,
)
from bauer_evidence_v3.ids import sha256_bytes  # noqa: E402
from bauer_evidence_v3.ingest import compile_source  # noqa: E402
from bauer_evidence_v3.ingest.pipeline import Compiler  # noqa: E402
from bauer_evidence_v3.ingest.quality import QualityPolicy  # noqa: E402
from bauer_evidence_v3.object_store import MemoryObjectStore  # noqa: E402
from bauer_evidence_v3.projections import project_document  # noqa: E402
from bauer_evidence_v3.toolchain import (  # noqa: E402
    compute_compiler_fingerprint,
    derive_compiler_toolchain,
)


SOURCE = b"<main><h1>BM 40</h1><p>Maximum pressure is 350 bar.</p></main>"


class VersionedOcr:
    engine_id = "test_ocr"
    engine_version = "ocr-v1"

    def recognize(self, image, *, languages):  # pragma: no cover - HTML fixture
        raise AssertionError("OCR should not run for this native HTML fixture")


class VersionedRenderer:
    renderer_id = "test_renderer"
    renderer_version = "renderer-v1"

    def render_pages(  # pragma: no cover - HTML fixture
        self,
        payload,
        *,
        page_indexes,
        dpi,
    ):
        raise AssertionError("rendering should not run for this HTML fixture")


def ocr_compiler() -> Compiler:
    return Compiler(
        ocr_engine=VersionedOcr(),
        page_renderer=VersionedRenderer(),
    )


class Embeddings:
    def __init__(self):
        self.dimensions = 1024
        self.batches = []

    def embed_many(self, texts):
        self.batches.append(tuple(texts))
        return tuple((float(index),) * 1024 for index, _ in enumerate(texts))


class Persistence:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def persist(self, **kwargs):
        self.calls.append(kwargs)
        source_object = self.store.put(
            kwargs["source_bytes"],
            media_type="text/html",
            suffix=".html",
        )
        canonical_object = self.store.put(
            kwargs["compilation"].document.to_json().encode(),
            media_type="application/json",
            suffix=".canonical.json",
        )
        return SimpleNamespace(
            source_id="source-db",
            source_version_id="version-db",
            artifact_set_id="artifact-db",
            source_object=source_object,
            canonical_object=canonical_object,
            reused=False,
            artifact_counts=(("pages", 1),),
            projection_counts=(("search_units", 2),),
        )


def payload(stored, *, compiler_fingerprint):
    return {
        "release_id": "40000000-0000-0000-0000-000000000001",
        "tenant_id": "10000000-0000-0000-0000-000000000001",
        "knowledge_base_id": "20000000-0000-0000-0000-000000000001",
        "source_id": "50000000-0000-0000-0000-000000000001",
        "source_version_id": "60000000-0000-0000-0000-000000000001",
        "external_file_id": "file-bm40",
        "source_name": "bm40.html",
        "source_object_key": stored.object_key,
        "source_type": "html",
        "source_sha256": stored.sha256,
        "source_byte_size": stored.byte_size,
        "manifest_sha256": "b" * 64,
        "compiler_fingerprint": compiler_fingerprint,
        "ordinal": 0,
        "category_path": ["Products", "Compressors"],
    }


class CompileHandlerTests(unittest.TestCase):
    def test_manifest_object_to_embedded_persistence(self):
        store = MemoryObjectStore()
        stored = store.put(SOURCE, media_type="text/html", suffix=".html")
        persistence = Persistence(store)
        embeddings = Embeddings()
        handler = CompilationJobHandler(
            object_store=store,
            persistence=persistence,
            embedding_provider=embeddings,
            embedding_model_version="embedding-v3-1024",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            embedding_batch_size=2,
        )

        request = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        request["source_metadata"] = {
            "document_number": "N28210",
            "product_tags": ["BM 40"],
            "aliases": ["Junior II"],
        }
        result = handler(request)

        self.assertEqual(result["artifact_set_id"], "artifact-db")
        self.assertEqual(result["quality_status"], "pass")
        self.assertEqual(len(persistence.calls), 1)
        call = persistence.calls[0]
        self.assertEqual(call["context"].ordinal, 0)
        self.assertEqual(call["context"].manifest_sha256, "b" * 64)
        self.assertEqual(call["context"].external_file_id, "file-bm40")
        self.assertEqual(
            call["context"].source_metadata["document_number"],
            "N28210",
        )
        projected_terms = {
            (term.term_type, term.normalized_term)
            for term in call["projections"].exact_terms
        }
        self.assertTrue(
            {
                ("external_file_id", "file-bm40"),
                ("filename", "bm40.html"),
                ("document_number", "n28210"),
                ("product_tag", "bm 40"),
                ("alias", "junior ii"),
            }.issubset(projected_terms)
        )
        self.assertTrue(call["projections"].search_units)
        self.assertTrue(
            all(len(unit.embedding) == 1024 for unit in call["projections"].search_units)
        )
        self.assertGreaterEqual(len(embeddings.batches), 1)

    def test_hash_or_size_drift_fails_before_compilation_persistence(self):
        store = MemoryObjectStore()
        stored = store.put(SOURCE, media_type="text/html", suffix=".html")
        persistence = Persistence(store)
        handler = CompilationJobHandler(
            object_store=store,
            persistence=persistence,
            embedding_provider=Embeddings(),
            embedding_model_version="embedding-v3-1024",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
        )
        invalid = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        invalid["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            handler(invalid)
        self.assertEqual(persistence.calls, [])

        invalid = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        invalid["source_byte_size"] += 1
        with self.assertRaisesRegex(ValueError, "byte size"):
            handler(invalid)
        self.assertEqual(persistence.calls, [])

    def test_worker_scope_mismatch_fails_before_object_read(self):
        store = MemoryObjectStore()
        stored = store.put(SOURCE, media_type="text/html", suffix=".html")
        persistence = Persistence(store)
        handler = CompilationJobHandler(
            object_store=store,
            persistence=persistence,
            embedding_provider=Embeddings(),
            embedding_model_version="embedding-v3-1024",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
        )
        invalid = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        invalid["knowledge_base_id"] = "20000000-0000-0000-0000-000000000099"
        with self.assertRaises(PermissionError):
            handler(invalid)
        self.assertEqual(persistence.calls, [])

    def test_toolchain_mismatch_fails_before_object_read(self):
        class ReadTrackingStore(MemoryObjectStore):
            def __init__(self):
                super().__init__()
                self.reads = []

            def get(self, object_key):
                self.reads.append(object_key)
                return super().get(object_key)

        store = ReadTrackingStore()
        stored = store.put(SOURCE, media_type="text/html", suffix=".html")
        persistence = Persistence(store)
        embeddings = Embeddings()
        handler = CompilationJobHandler(
            object_store=store,
            persistence=persistence,
            embedding_provider=embeddings,
            embedding_model_version="embedding-v3-1024",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
        )
        invalid = payload(stored, compiler_fingerprint="a" * 64)
        invalid["source_object_key"] = "missing/source-object"

        with self.assertRaisesRegex(ValueError, "does not match"):
            handler(invalid)

        self.assertEqual(store.reads, [])
        self.assertEqual(embeddings.batches, [])
        self.assertEqual(persistence.calls, [])

    def test_source_type_is_compiler_supported_and_matches_probed_bytes(self):
        store = MemoryObjectStore()
        stored = store.put(SOURCE, media_type="text/html", suffix=".html")
        persistence = Persistence(store)
        handler = CompilationJobHandler(
            object_store=store,
            persistence=persistence,
            embedding_provider=Embeddings(),
            embedding_model_version="embedding-v3-1024",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
        )
        unsupported = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        unsupported["source_type"] = "office_document"
        with self.assertRaisesRegex(ValueError, "pdf, html, or text"):
            handler(unsupported)

        mismatched = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        mismatched["source_type"] = "pdf"
        with self.assertRaisesRegex(ValueError, "does not match"):
            handler(mismatched)
        self.assertEqual(persistence.calls, [])

    def test_optional_model_versions_are_worker_owned_and_fingerprinted(self):
        store = MemoryObjectStore()
        stored = store.put(SOURCE, media_type="text/html", suffix=".html")
        handler = CompilationJobHandler(
            object_store=store,
            persistence=Persistence(store),
            embedding_provider=Embeddings(),
            embedding_model_version="embedding-v3-1024",
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            compiler=ocr_compiler(),
            ocr_version="ocr-v1",
            fact_model_version="facts-v2",
        )
        invalid = payload(
            stored,
            compiler_fingerprint=handler.compiler_fingerprint,
        )
        invalid["ocr_version"] = "operator-injected-version"
        invalid["fact_model_version"] = "facts-v2"

        with self.assertRaisesRegex(ValueError, "OCR version"):
            handler(invalid)

    def test_release_helper_derives_stable_runtime_identity(self):
        first = derive_compiler_toolchain(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=1024,
        )
        second = derive_compiler_toolchain(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=1024,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.fingerprint,
            compute_compiler_fingerprint(
                embedding_model_version="embedding-v3-1024",
                embedding_dimensions=1024,
            ),
        )
        self.assertRegex(first.fingerprint, r"^[0-9a-f]{64}$")
        self.assertTrue(first.parser_version.startswith("bauer-v3-parsers-"))
        self.assertEqual(
            [item["parser_id"] for item in first.manifest["parsers"]],
            [
                "pdfplumber_native",
                "pymupdf_native",
                "html_source",
                "markdown_source",
                "text_utf8",
            ],
        )
        self.assertEqual(
            first.manifest["embedding"],
            {
                "dimensions": 1024,
                "model_version": "embedding-v3-1024",
            },
        )

    def test_fingerprint_changes_with_output_affecting_toolchain_contract(self):
        baseline = compute_compiler_fingerprint(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=1024,
        )
        changed_model = compute_compiler_fingerprint(
            embedding_model_version="embedding-v3-2048",
            embedding_dimensions=1024,
        )
        changed_dimensions = compute_compiler_fingerprint(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=2048,
        )
        changed_ocr = compute_compiler_fingerprint(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=1024,
            compiler=ocr_compiler(),
            ocr_version="ocr-v1",
        )
        changed_fact_model = compute_compiler_fingerprint(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=1024,
            fact_model_version="facts-v2",
        )
        changed_policy = compute_compiler_fingerprint(
            embedding_model_version="embedding-v3-1024",
            embedding_dimensions=1024,
            compiler=Compiler(
                quality_policy=replace(
                    QualityPolicy(),
                    max_pages=499,
                )
            ),
        )

        self.assertEqual(
            len(
                {
                    baseline,
                    changed_model,
                    changed_dimensions,
                    changed_ocr,
                    changed_fact_model,
                    changed_policy,
                }
            ),
            6,
        )

    def test_projection_embedding_is_batched_without_reordering(self):
        compilation = compile_source(
            SOURCE,
            source_name="bm40.html",
            declared_media_type="text/html",
        )
        bundle = project_document(
            compilation.document,
            release_id="release",
            tenant_id="tenant",
            knowledge_base_id="kb",
            source_document_id="source",
            source_version_id="version",
            source_type="html",
        )
        embeddings = Embeddings()
        embedded = embed_projection_bundle(
            bundle,
            provider=embeddings,
            batch_size=1,
        )
        self.assertEqual(
            [unit.search_unit_id for unit in embedded.search_units],
            [unit.search_unit_id for unit in bundle.search_units],
        )
        self.assertEqual(len(embeddings.batches), len(bundle.search_units))


if __name__ == "__main__":
    unittest.main()
