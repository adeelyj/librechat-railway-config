from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from bauer_evidence_v3.object_store import (
    LocalObjectStore,
    MemoryObjectStore,
    MirroredObjectStore,
    S3ObjectStore,
)


class MissingObject(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects = {}
        self.read_unavailable = False

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise MissingObject()
        item = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(item["content"]),
            "Metadata": item["metadata"],
        }

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata, ChecksumSHA256):
        self.objects[(Bucket, Key)] = {
            "content": bytes(Body),
            "media_type": ContentType,
            "metadata": dict(Metadata),
            "checksum": ChecksumSHA256,
        }

    def get_object(self, *, Bucket, Key):
        if self.read_unavailable:
            raise RuntimeError("endpoint unavailable")
        if (Bucket, Key) not in self.objects:
            raise MissingObject()
        return {"Body": BytesIO(self.objects[(Bucket, Key)]["content"])}


class ObjectStoreTests(unittest.TestCase):
    def test_memory_store_is_content_addressed_and_idempotent(self) -> None:
        store = MemoryObjectStore()
        first = store.put(b"source", media_type="application/pdf", suffix=".pdf")
        second = store.put(b"source", media_type="application/pdf", suffix=".pdf")
        self.assertEqual(first.object_key, second.object_key)
        self.assertEqual(store.get(first.object_key), b"source")

    def test_local_store_round_trip_and_partitioned_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(directory)
            item = store.put(b"canonical", media_type="application/json", suffix=".json")
            self.assertTrue(store.exists(item.object_key))
            self.assertEqual(store.get(item.object_key), b"canonical")
            self.assertTrue((Path(directory) / item.object_key).is_file())

    def test_local_store_rejects_traversal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(directory)
            for key in ("../secret", "/absolute", r"sha256\..\secret"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    store.get(key)

    def test_s3_store_is_idempotent_and_verifies_sha_metadata(self) -> None:
        client = FakeS3()
        store = S3ObjectStore(bucket="evidence", prefix="v3", client=client)
        first = store.put(b"source", media_type="application/pdf", suffix=".pdf")
        second = store.put(b"source", media_type="application/pdf", suffix=".pdf")
        self.assertEqual(first, second)
        self.assertEqual(len(client.objects), 1)
        self.assertEqual(store.get(first.object_key), b"source")
        self.assertTrue(store.exists(first.object_key))

    def test_mirror_requires_both_writes_and_can_read_fallback(self) -> None:
        primary = MemoryObjectStore()
        mirror = MemoryObjectStore()
        store = MirroredObjectStore(primary=primary, mirror=mirror)
        item = store.put(b"release-manifest", media_type="application/json", suffix=".json")
        self.assertTrue(store.exists(item.object_key))
        primary._objects.clear()
        self.assertEqual(store.get(item.object_key), b"release-manifest")

    def test_s3_mirror_handles_missing_and_unavailable_primary(self) -> None:
        primary_client = FakeS3()
        mirror_client = FakeS3()
        store = MirroredObjectStore(
            primary=S3ObjectStore(
                bucket="primary",
                prefix="v3",
                client=primary_client,
            ),
            mirror=S3ObjectStore(
                bucket="mirror",
                prefix="v3",
                client=mirror_client,
            ),
        )
        item = store.put(
            b"release-manifest",
            media_type="application/json",
            suffix=".json",
        )

        primary_client.objects.clear()
        self.assertEqual(store.get(item.object_key), b"release-manifest")

        primary_client.read_unavailable = True
        self.assertEqual(store.get(item.object_key), b"release-manifest")


if __name__ == "__main__":
    unittest.main()
