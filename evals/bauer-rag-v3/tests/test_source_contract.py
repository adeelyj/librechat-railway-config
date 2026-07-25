from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT))

from source_contract import (  # noqa: E402
    TAXONOMY_ID,
    TAXONOMY_SEMANTICS,
    SourceContractError,
    SourceContractIntegrityError,
    bind_release_spec,
    build_contract,
    contract_bytes,
    contract_sha256,
    navigation_category_path,
    verify_contract,
    write_contract,
    write_release_spec_once,
)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class SourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.pdf_dir = self.root / "bauer_index" / "raw_docs"
        self.html_dir = self.root / "bauer_index" / "raw_html"
        self.pdf_dir.mkdir(parents=True)
        self.html_dir.mkdir(parents=True)

        self.pdf_content = b"%PDF-1.7\none selected source\n"
        self.html_content = b"<!doctype html><title>two</title>"
        self.pdf_alias = self.pdf_dir / "one-copy.pdf"
        (self.pdf_dir / "one.pdf").write_bytes(self.pdf_content)
        self.pdf_alias.write_bytes(self.pdf_content)
        (self.html_dir / "two.html").write_bytes(self.html_content)

        self.records = [
            {
                "filename": "0002-two.md",
                "kind": "html",
                "sourcePath": "bauer_index\\raw_html\\two.html",
                "sourceChecksum": sha256(self.html_content),
                "exportedChecksum": sha256(b"two derivative"),
                "duplicatePaths": [],
                "pageCount": None,
                "ocrUsed": False,
                "characterCount": 25,
            },
            {
                "filename": "0001-one.md",
                "kind": "pdf",
                "sourcePath": "bauer_index\\raw_docs\\one.pdf",
                "sourceChecksum": sha256(self.pdf_content),
                "exportedChecksum": sha256(b"one derivative"),
                "duplicatePaths": [
                    "bauer_index\\raw_docs\\one-copy.pdf",
                ],
                "pageCount": 1,
                "ocrUsed": True,
                "characterCount": 20,
            },
        ]
        self.uploads = {
            "0001-one.md": {
                "fileId": "file-one",
                "sha256": sha256(b"one derivative"),
                "embedded": True,
            },
            "0002-two.md": {
                "fileId": "file-two",
                "sha256": sha256(b"two derivative"),
                "embedded": True,
            },
        }
        self.manifest = Path(self.temporary.name) / "manifest.json"
        self.state = Path(self.temporary.name) / "state.json"
        self._write_inputs()

    def _write_inputs(self) -> None:
        self.manifest.write_text(json.dumps(self.records), encoding="utf-8")
        self.state.write_text(
            json.dumps({"uploaded": {"bauer-kompressoren": self.uploads}}),
            encoding="utf-8",
        )

    def build(self) -> dict:
        return build_contract(
            v2_dedup_manifest=self.manifest,
            provision_state=self.state,
            source_root=self.root,
            expected_source_count=2,
        )

    def test_build_is_deterministic_and_freezes_only_originals(self):
        first = self.build()
        self.records.reverse()
        self.uploads = dict(reversed(list(self.uploads.items())))
        self._write_inputs()
        second = self.build()

        self.assertEqual(first, second)
        self.assertEqual(first["source_count"], 2)
        self.assertEqual(first["selected_counts"], {"html": 1, "pdf": 1})
        self.assertEqual(first["prior_duplicate_alias_count"], 1)
        self.assertEqual(first["accounted_original_file_count"], 3)
        self.assertEqual(
            [item["source_type"] for item in first["sources"]],
            ["pdf", "html"],
        )
        self.assertTrue(all(item["is_original"] for item in first["sources"]))
        self.assertTrue(
            all(not item["is_derivative"] for item in first["sources"])
        )
        self.assertEqual(
            {item["external_file_id"] for item in first["sources"]},
            {"file-one", "file-two"},
        )
        self.assertTrue(
            all(len(item["category_path"]) == 2 for item in first["sources"])
        )
        self.assertEqual(
            first["sources"][0]["category_path"],
            ["Documents", "Product & technical literature"],
        )
        self.assertEqual(
            first["sources"][1]["category_path"],
            ["Products & Systems", "Product pages"],
        )
        self.assertNotIn(b"one derivative", contract_bytes(first))

    def test_taxonomy_is_deterministic_navigation_only_and_non_citable(self):
        contract = self.build()
        taxonomy = contract["selection_basis"]["category_taxonomy"]
        self.assertEqual(taxonomy["taxonomy_id"], TAXONOMY_ID)
        self.assertEqual(taxonomy["semantics"], TAXONOMY_SEMANTICS)
        self.assertIs(taxonomy["citable"], False)
        self.assertEqual(taxonomy["inputs"], ["source_type", "source_path"])

        first = navigation_category_path(
            source_type="html",
            source_path=(
                "bauer_index/raw_html/"
                "0007_b-cloud-download-en_f6d564f12d.html"
            ),
        )
        second = navigation_category_path(
            source_type="html",
            source_path=(
                "bauer_index/raw_html/"
                "0007_b-cloud-download-en_f6d564f12d.html"
            ),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            ("Digital, Control & Service", "Digital & controls"),
        )

    def test_empty_or_tampered_category_is_rejected(self):
        contract = self.build()
        for invalid in (
            [],
            ["Documents", "Changed classification"],
        ):
            with self.subTest(category_path=invalid):
                tampered = copy.deepcopy(contract)
                tampered["sources"][0]["category_path"] = invalid
                tampered["contract_sha256"] = contract_sha256(tampered)
                with self.assertRaisesRegex(
                    SourceContractError,
                    "category_path",
                ):
                    verify_contract(
                        tampered,
                        source_root=self.root,
                        expected_source_count=2,
                    )

    def test_verify_reports_complete_read_only_accounting(self):
        contract = self.build()
        before = {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in self.root.rglob("*")
            if path.is_file()
        }
        report = verify_contract(
            contract,
            source_root=self.root,
            expected_source_count=2,
        )
        after = {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(report.source_count, 2)
        self.assertEqual(report.prior_duplicate_alias_count, 1)
        self.assertEqual(report.accounted_original_file_count, 3)

    def test_missing_selected_file_is_rejected(self):
        (self.html_dir / "two.html").unlink()
        with self.assertRaisesRegex(
            SourceContractIntegrityError, "source file is missing"
        ):
            self.build()

    def test_selected_hash_mismatch_is_rejected(self):
        self.records[0]["sourceChecksum"] = "0" * 64
        self._write_inputs()
        with self.assertRaisesRegex(
            SourceContractIntegrityError, "expected SHA-256"
        ):
            self.build()

    def test_duplicate_selected_content_is_rejected(self):
        (self.html_dir / "two.html").write_bytes(self.pdf_content)
        self.records[0]["sourceChecksum"] = sha256(self.pdf_content)
        self._write_inputs()
        with self.assertRaisesRegex(
            SourceContractError, "duplicate selected source SHA-256"
        ):
            self.build()

    def test_duplicate_external_file_id_is_rejected(self):
        self.uploads["0002-two.md"]["fileId"] = "file-one"
        self._write_inputs()
        with self.assertRaisesRegex(
            SourceContractError, "duplicate external file ID"
        ):
            self.build()

    def test_verify_detects_byte_drift_after_generation(self):
        contract = self.build()
        (self.pdf_dir / "one.pdf").write_bytes(b"changed")
        with self.assertRaisesRegex(
            SourceContractIntegrityError, "expected SHA-256"
        ):
            verify_contract(
                contract,
                source_root=self.root,
                expected_source_count=2,
            )

    def test_verify_detects_unaccounted_original(self):
        contract = self.build()
        (self.html_dir / "unexpected.html").write_text(
            "<p>unexpected</p>", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            SourceContractIntegrityError, "does not exactly account"
        ):
            verify_contract(
                contract,
                source_root=self.root,
                expected_source_count=2,
            )

    def test_contract_self_digest_detects_tampering(self):
        contract = self.build()
        tampered = copy.deepcopy(contract)
        tampered["sources"][0]["expected_byte_size"] += 1
        self.assertNotEqual(tampered["contract_sha256"], contract_sha256(tampered))
        with self.assertRaisesRegex(
            SourceContractIntegrityError, "contract_sha256"
        ):
            verify_contract(
                tampered,
                source_root=self.root,
                expected_source_count=2,
            )

    def test_output_inside_original_source_root_is_rejected(self):
        contract = self.build()
        with self.assertRaisesRegex(
            SourceContractError, "must not be written inside"
        ):
            write_contract(
                contract,
                output=self.root / "contract.json",
                source_root=self.root,
            )

    def test_release_binding_is_strict_portable_and_write_once(self):
        contract = self.build()
        specification = bind_release_spec(
            contract,
            tenant_id="10000000-0000-0000-0000-000000000001",
            knowledge_base_id="20000000-0000-0000-0000-000000000001",
            release_id="40000000-0000-0000-0000-000000000001",
            expected_source_count=2,
        )
        self.assertEqual(len(specification["sources"]), 2)
        self.assertEqual(
            set(specification["sources"][0]),
            {
                "external_file_id",
                "logical_path",
                "source_type",
                "declared_media_type",
                "visibility",
                "category_path",
                "metadata",
                "is_original",
                "is_derivative",
            },
        )
        self.assertEqual(
            specification["sources"][0]["metadata"][
                "source_contract_sha256"
            ],
            contract["contract_sha256"],
        )
        output = Path(self.temporary.name) / "release-sources.json"
        write_release_spec_once(
            specification,
            output=output,
            source_root=self.root,
        )
        write_release_spec_once(
            specification,
            output=output,
            source_root=self.root,
        )
        changed = copy.deepcopy(specification)
        changed["release_id"] = "40000000-0000-0000-0000-000000000002"
        with self.assertRaisesRegex(
            SourceContractIntegrityError,
            "refusing to overwrite",
        ):
            write_release_spec_once(
                changed,
                output=output,
                source_root=self.root,
            )

        with self.assertRaisesRegex(SourceContractError, "must be a UUID"):
            bind_release_spec(
                contract,
                tenant_id="not-a-uuid",
                knowledge_base_id="20000000-0000-0000-0000-000000000001",
                release_id="40000000-0000-0000-0000-000000000001",
                expected_source_count=2,
            )


if __name__ == "__main__":
    unittest.main()
