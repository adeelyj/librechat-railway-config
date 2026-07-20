from __future__ import annotations

import os
from pathlib import Path

from pgvector.psycopg import register_vector
from psycopg import connect
from psycopg.types.json import Jsonb

from .catalog import build_catalog, searchable_text
from .embeddings import EmbeddingClient


def seed() -> dict[str, int]:
    database_url = os.environ["DATABASE_URL"]
    catalog = build_catalog()
    records = catalog["projects"] + catalog["parts"] + catalog["documents"]
    texts = [searchable_text(record) for record in records]
    vectors = EmbeddingClient().embed_many(texts)
    vector_by_identity = {
        record.get("project_id") or record.get("part_id") or record.get("document_id"): vector
        for record, vector in zip(records, vectors)
    }

    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with connect(database_url) as connection:
        connection.execute(schema)
        register_vector(connection)
        for project in catalog["projects"]:
            values = dict(project)
            values["search_text"] = searchable_text(project)
            values["embedding"] = vector_by_identity[project["project_id"]]
            for key in ("standards", "document_ids"):
                values[key] = Jsonb(values[key])
            columns = list(values)
            placeholders = ", ".join(["%s"] * len(columns))
            updates = ", ".join(f"{column}=EXCLUDED.{column}" for column in columns if column != "project_id")
            connection.execute(
                f"INSERT INTO bauer_twin.projects ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT (project_id) DO UPDATE SET {updates}, updated_at=now()",
                list(values.values()),
            )
        for part in catalog["parts"]:
            values = dict(part)
            values["search_text"] = searchable_text(part)
            values["embedding"] = vector_by_identity[part["part_id"]]
            for key in ("synonyms", "compatible_models", "compatible_media", "project_ids", "document_ids"):
                values[key] = Jsonb(values[key])
            columns = list(values)
            placeholders = ", ".join(["%s"] * len(columns))
            updates = ", ".join(f"{column}=EXCLUDED.{column}" for column in columns if column != "part_id")
            connection.execute(
                f"INSERT INTO bauer_twin.parts ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT (part_id) DO UPDATE SET {updates}, updated_at=now()",
                list(values.values()),
            )
        for document in catalog["documents"]:
            values = dict(document)
            values["search_text"] = searchable_text(document)
            values["embedding"] = vector_by_identity[document["document_id"]]
            columns = list(values)
            placeholders = ", ".join(["%s"] * len(columns))
            updates = ", ".join(f"{column}=EXCLUDED.{column}" for column in columns if column != "document_id")
            connection.execute(
                f"INSERT INTO bauer_twin.documents ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT (document_id) DO UPDATE SET {updates}, updated_at=now()",
                list(values.values()),
            )
    return {key: len(value) for key, value in catalog.items()}


if __name__ == "__main__":
    print(seed())

