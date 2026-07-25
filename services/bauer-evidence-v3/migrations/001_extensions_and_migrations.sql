CREATE SCHEMA IF NOT EXISTS bauer_rag_v3;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS bauer_rag_v3.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    filename text NOT NULL UNIQUE,
    checksum char(64) NOT NULL
        CHECK (checksum ~ '^[0-9a-f]{64}$'),
    execution_ms integer NOT NULL DEFAULT 0
        CHECK (execution_ms >= 0),
    applied_at timestamptz NOT NULL DEFAULT now()
);
