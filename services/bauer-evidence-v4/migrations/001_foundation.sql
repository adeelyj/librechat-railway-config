CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA bauer_rag_v4;
REVOKE ALL ON SCHEMA bauer_rag_v4 FROM PUBLIC;

CREATE TABLE bauer_rag_v4.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    filename text NOT NULL UNIQUE,
    checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    execution_ms integer NOT NULL DEFAULT 0 CHECK (execution_ms >= 0),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON ALL TABLES IN SCHEMA bauer_rag_v4 FROM PUBLIC;
