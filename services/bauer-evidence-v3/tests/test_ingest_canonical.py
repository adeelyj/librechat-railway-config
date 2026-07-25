from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest import canonical_json, compile_source, stable_id
from bauer_evidence_v3.ingest.canonical import attribute_items


class CanonicalIrTests(unittest.TestCase):
    def test_stable_ids_use_source_location_and_content(self) -> None:
        source_hash = hashlib.sha256(b"source").hexdigest()
        first = stable_id("block", source_hash, "page:0/block:0", "alpha")

        self.assertEqual(
            first,
            stable_id("block", source_hash, "page:0/block:0", "alpha"),
        )
        self.assertNotEqual(
            first,
            stable_id("block", source_hash, "page:0/block:1", "alpha"),
        )
        self.assertNotEqual(
            first,
            stable_id("block", source_hash, "page:0/block:0", "beta"),
        )

    def test_canonical_json_is_repeatable_and_ir_is_frozen(self) -> None:
        first = compile_source(
            b"Evidence heading\ncontinues here.",
            source_name="evidence.txt",
        )
        second = compile_source(
            b"Evidence heading\ncontinues here.",
            source_name="evidence.txt",
        )

        self.assertEqual(first.document, second.document)
        self.assertEqual(
            canonical_json(first.document),
            canonical_json(second.document),
        )
        self.assertEqual(
            first.document.attributes,
            tuple(sorted(first.document.attributes)),
        )
        with self.assertRaises(FrozenInstanceError):
            first.document.title = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            first.document.pages[0].blocks[0].text = "changed"  # type: ignore[misc]

    def test_attributes_are_sorted_and_detached_from_source_mapping(self) -> None:
        source = {"z": 2, "a": True}
        result = attribute_items(source)
        source["a"] = False

        self.assertEqual(result, (("a", "true"), ("z", "2")))


if __name__ == "__main__":
    unittest.main()
