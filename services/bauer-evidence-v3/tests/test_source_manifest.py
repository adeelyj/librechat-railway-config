from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ids import sha256_json  # noqa: E402
from bauer_evidence_v3.object_store import MemoryObjectStore  # noqa: E402
from bauer_evidence_v3.postgres_admin import CompileJobSpec  # noqa: E402
from bauer_evidence_v3.source_manifest import (  # noqa: E402
    SourceManifestIntegrityError,
    SourceManifestValidationError,
    SourceSpec,
    create_source_manifest,
    deterministic_source_id,
    deterministic_source_version_id,
    load_source_manifest,
    stage_manifest_sources,
    verify_source_manifest,
)


TENANT_ID = "10000000-0000-0000-0000-000000000001"
KB_ID = "20000000-0000-0000-0000-000000000001"
RELEASE_ID = "30000000-0000-0000-0000-000000000001"


class SourceManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "manuals").mkdir()
        (self.root / "manuals" / "pump.pdf").write_bytes(
            b"%PDF-1.7\nsynthetic original PDF bytes"
        )
        (self.root / "notes.txt").write_text(
            "Original service notes.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def specs(self):
        return (
            SourceSpec(
                external_file_id="bauer-pump-manual",
                logical_path="manuals/pump.pdf",
                source_type="pdf",
                declared_media_type="application/pdf",
                visibility="restricted",
                category_path=("Products", "Pumps"),
                metadata={"product_tag": "P-100", "reviewed": True},
            ),
            SourceSpec(
                external_file_id="bauer-service-notes",
                logical_path="notes.txt",
                source_type="text",
                category_path=("Service",),
            ),
        )

    def manifest(self, specs=None, **kwargs):
        return create_source_manifest(
            self.root,
            tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID,
            release_id=RELEASE_ID,
            sources=self.specs() if specs is None else specs,
            **kwargs,
        )

    def test_creation_is_canonical_and_independent_of_input_order(self):
        first = self.manifest(
            created_at="2026-07-24T09:30:00Z",
        )
        second = self.manifest(
            tuple(reversed(self.specs())),
            created_at="2026-07-24T09:30:00Z",
        )

        self.assertEqual(first.to_bytes(), second.to_bytes())
        self.assertEqual(first.manifest_sha256, second.manifest_sha256)
        self.assertEqual(first.source_count, 2)
        self.assertEqual(
            [item.logical_path for item in first.sources],
            ["manuals/pump.pdf", "notes.txt"],
        )
        pdf = first.sources[0]
        self.assertEqual(pdf.detected_media_type, "application/pdf")
        self.assertEqual(pdf.declared_media_type, "application/pdf")
        self.assertEqual(pdf.visibility, "restricted")
        self.assertEqual(pdf.category_path, ("Products", "Pumps"))
        self.assertTrue(pdf.is_original)
        self.assertFalse(pdf.is_derivative)

    def test_no_implicit_timestamp_and_explicit_timestamp_changes_digest(self):
        without_timestamp = self.manifest()
        self.assertNotIn("created_at", without_timestamp.to_dict())

        with_timestamp = self.manifest(created_at="2026-07-24T09:30:00Z")
        self.assertNotEqual(
            without_timestamp.manifest_sha256,
            with_timestamp.manifest_sha256,
        )
        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "explicitly supplied UTC",
        ):
            self.manifest(created_at="2026-07-24 09:30:00")

    def test_load_round_trip_verifies_embedded_digest_and_strict_json(self):
        manifest = self.manifest()
        loaded = load_source_manifest(manifest.to_bytes())
        self.assertEqual(loaded, manifest)
        self.assertEqual(loaded.to_bytes(), manifest.to_bytes())

        changed = manifest.to_dict()
        changed["sources"][0]["visibility"] = "inherited"
        with self.assertRaisesRegex(
            SourceManifestIntegrityError,
            "checksum",
        ):
            load_source_manifest(json.dumps(changed))

        duplicate_key = '{"schema_version":1,"schema_version":1}'
        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "duplicate JSON object key",
        ):
            load_source_manifest(duplicate_key)

    def test_rejects_duplicate_external_ids_and_paths_case_insensitively(self):
        duplicate_external = (
            self.specs()[0],
            SourceSpec(
                external_file_id="BAUER-PUMP-MANUAL",
                logical_path="notes.txt",
                source_type="text",
            ),
        )
        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "duplicate external_file_id",
        ):
            self.manifest(duplicate_external)

        (self.root / "NOTES.TXT").write_text("other", encoding="utf-8")
        duplicate_path = (
            self.specs()[1],
            SourceSpec(
                external_file_id="different",
                logical_path="NOTES.TXT",
                source_type="text",
            ),
        )
        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "duplicate logical_path",
        ):
            self.manifest(duplicate_path)

    def test_rejects_traversal_missing_files_and_non_files(self):
        bad_paths = (
            "../secret.pdf",
            "/absolute/file.pdf",
            r"manuals\pump.pdf",
            "manuals/../notes.txt",
            "C:/source/file.pdf",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(
                SourceManifestValidationError
            ):
                SourceSpec(
                    external_file_id="bad",
                    logical_path=path,
                    source_type="pdf",
                )

        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "missing",
        ):
            self.manifest(
                (
                    SourceSpec(
                        external_file_id="missing",
                        logical_path="missing.pdf",
                        source_type="pdf",
                    ),
                )
            )

        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "not a file",
        ):
            self.manifest(
                (
                    SourceSpec(
                        external_file_id="directory",
                        logical_path="manuals",
                        source_type="pdf",
                    ),
                )
            )

    def test_rejects_unsupported_and_non_original_sources(self):
        for source_type in (
            "markdown_derivative",
            "image",
            "office_document",
            "structured_record",
            "synthetic_demo",
        ):
            with (
                self.subTest(source_type=source_type),
                self.assertRaisesRegex(
                    SourceManifestValidationError,
                    "unsupported source_type",
                ),
            ):
                SourceSpec(
                    external_file_id="bad",
                    logical_path="notes.txt",
                    source_type=source_type,
                )
        for original, derivative in ((False, False), (True, True), (False, True)):
            with (
                self.subTest(original=original, derivative=derivative),
                self.assertRaisesRegex(
                    SourceManifestValidationError,
                    "original, non-derivative",
                ),
            ):
                SourceSpec(
                    external_file_id="bad",
                    logical_path="notes.txt",
                    source_type="text",
                    is_original=original,
                    is_derivative=derivative,
                )

        raw = self.manifest().to_dict()
        raw["sources"][0]["is_original"] = False
        raw["manifest_sha256"] = sha256_json(
            {key: value for key, value in raw.items() if key != "manifest_sha256"}
        )
        with self.assertRaisesRegex(
            SourceManifestValidationError,
            "original, non-derivative",
        ):
            load_source_manifest(raw)

    def test_verification_detects_size_and_same_size_hash_drift(self):
        manifest = self.manifest()
        verify_source_manifest(manifest, self.root)

        notes = self.root / "notes.txt"
        original = notes.read_bytes()
        notes.write_bytes(b"X" * len(original))
        with self.assertRaisesRegex(
            SourceManifestIntegrityError,
            "hash drift",
        ):
            verify_source_manifest(manifest, self.root)

        notes.write_bytes(original + b"extra")
        with self.assertRaisesRegex(
            SourceManifestIntegrityError,
            "size drift",
        ):
            verify_source_manifest(manifest, self.root)

    def test_staging_verifies_content_and_returns_postgres_compile_specs(self):
        manifest = self.manifest()
        store = MemoryObjectStore()
        jobs = stage_manifest_sources(
            manifest,
            self.root,
            store,
            priority=7,
            max_attempts=3,
        )

        self.assertEqual(len(jobs), manifest.source_count)
        self.assertTrue(all(isinstance(job, CompileJobSpec) for job in jobs))
        first_entry = manifest.sources[0]
        first_job = jobs[0]
        expected_source_id = deterministic_source_id(
            TENANT_ID,
            KB_ID,
            first_entry.external_file_id,
        )
        self.assertEqual(first_job.source_id, expected_source_id)
        self.assertEqual(
            first_job.source_version_id,
            deterministic_source_version_id(
                expected_source_id,
                first_entry.sha256,
            ),
        )
        uuid.UUID(first_job.source_id)
        uuid.UUID(first_job.source_version_id)
        self.assertTrue(store.exists(first_job.source_object_key))
        self.assertEqual(
            store.get(first_job.source_object_key),
            (self.root / first_entry.logical_path).read_bytes(),
        )
        self.assertEqual(first_job.priority, 7)
        self.assertEqual(first_job.max_attempts, 3)
        self.assertEqual(
            first_job.payload["manifest_sha256"],
            manifest.manifest_sha256,
        )
        self.assertTrue(
            store.exists(first_job.payload["manifest_object_key"])
        )
        self.assertEqual(
            store.get(first_job.payload["manifest_object_key"]),
            manifest.to_bytes(),
        )
        self.assertEqual(
            sha256_json(manifest.to_dict()),
            first_job.payload["manifest_object_sha256"],
        )
        self.assertEqual(first_job.payload["visibility"], "restricted")
        self.assertEqual(
            first_job.payload["source_metadata"]["product_tag"],
            "P-100",
        )
        self.assertTrue(first_job.payload["is_original"])
        self.assertFalse(first_job.payload["is_derivative"])

        repeated = stage_manifest_sources(manifest, self.root, store)
        self.assertEqual(
            [(job.source_id, job.source_version_id) for job in repeated],
            [(job.source_id, job.source_version_id) for job in jobs],
        )

    def test_staging_refuses_drift_before_storing_changed_source(self):
        manifest = self.manifest()
        (self.root / "manuals" / "pump.pdf").write_bytes(b"%PDF-1.7\nchanged")
        store = MemoryObjectStore()
        with self.assertRaises(SourceManifestIntegrityError):
            stage_manifest_sources(manifest, self.root, store)
        self.assertEqual(store._objects, {})


if __name__ == "__main__":
    unittest.main()
