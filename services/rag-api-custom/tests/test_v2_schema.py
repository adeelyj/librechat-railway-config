import unittest
from pathlib import Path


SCHEMA = (
    Path(__file__).resolve().parents[1] / "bauer_rag_v2" / "schema.sql"
).read_text(encoding="utf-8")


class V2SchemaTests(unittest.TestCase):
    def test_schema_is_additive_and_versioned(self):
        lowered = SCHEMA.casefold()
        self.assertIn("create schema if not exists bauer_rag_v2", lowered)
        self.assertNotIn("drop table", lowered)
        self.assertNotIn("alter table langchain_pg_embedding", lowered)
        for table in ("index_runs", "documents", "chunks", "entities", "index_run_documents"):
            self.assertIn(f"bauer_rag_v2.{table}", lowered)

    def test_authorization_and_provenance_columns_exist(self):
        for field in (
            "namespace",
            "file_id",
            "checksum",
            "filename",
            "language",
            "page",
            "section_path",
            "table_title",
            "row_label",
            "source_type",
            "publication_date",
            "media",
            "component_categories",
            "extractor_version",
            "embedding_version",
            "index_version",
        ):
            self.assertIn(field, SCHEMA)

    def test_stable_chunk_id_is_scoped_to_document_version(self):
        self.assertIn("PRIMARY KEY (document_id, chunk_id)", SCHEMA)
        self.assertIn(
            "REFERENCES bauer_rag_v2.chunks(document_id, chunk_id)",
            SCHEMA,
        )


if __name__ == "__main__":
    unittest.main()
