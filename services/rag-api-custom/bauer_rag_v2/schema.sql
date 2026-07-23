CREATE SCHEMA IF NOT EXISTS bauer_rag_v2;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS bauer_rag_v2.index_runs (
    run_id uuid PRIMARY KEY,
    namespace text NOT NULL,
    index_version text NOT NULL,
    extractor_version text NOT NULL,
    embedding_version text NOT NULL,
    expected_file_count integer NOT NULL CHECK (expected_file_count > 0),
    status text NOT NULL CHECK (status IN ('running', 'active', 'failed', 'abandoned')),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS bauer_rag_v2.documents (
    document_id uuid PRIMARY KEY,
    namespace text NOT NULL,
    index_version text NOT NULL,
    file_id text NOT NULL,
    checksum text NOT NULL,
    filename text NOT NULL,
    title text,
    language text,
    source_type text NOT NULL,
    source_path text,
    document_number text,
    certificate text,
    certificates text[] NOT NULL DEFAULT '{}',
    revision text,
    publication_date date,
    organization text,
    address text,
    product_families text[] NOT NULL DEFAULT '{}',
    media text[] NOT NULL DEFAULT '{}',
    component_categories text[] NOT NULL DEFAULT '{}',
    standards text[] NOT NULL DEFAULT '{}',
    extractor_version text NOT NULL,
    embedding_version text NOT NULL,
    active boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    UNIQUE (
        namespace,
        index_version,
        file_id,
        checksum,
        extractor_version,
        embedding_version
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v2.chunks (
    chunk_id text NOT NULL,
    document_id uuid NOT NULL
        REFERENCES bauer_rag_v2.documents(document_id) ON DELETE CASCADE,
    namespace text NOT NULL,
    index_version text NOT NULL,
    file_id text NOT NULL,
    checksum text NOT NULL,
    ordinal integer NOT NULL,
    chunk_kind text NOT NULL CHECK (chunk_kind IN ('prose', 'table_row')),
    page integer,
    section_path text[] NOT NULL DEFAULT '{}',
    table_title text,
    row_label text,
    headers jsonb NOT NULL DEFAULT '[]'::jsonb,
    row_values jsonb NOT NULL DEFAULT '[]'::jsonb,
    units text[] NOT NULL DEFAULT '{}',
    footnotes text,
    content text NOT NULL,
    search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(search_text, ''))
    ) STORED,
    embedding vector(1024),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (document_id, chunk_id),
    UNIQUE (document_id, ordinal)
);

CREATE TABLE IF NOT EXISTS bauer_rag_v2.entities (
    entity_id bigserial PRIMARY KEY,
    document_id uuid NOT NULL
        REFERENCES bauer_rag_v2.documents(document_id) ON DELETE CASCADE,
    chunk_id text,
    kind text NOT NULL,
    raw_value text NOT NULL,
    normalized_value text NOT NULL,
    numeric_value double precision,
    unit text,
    source_span jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY (document_id, chunk_id)
        REFERENCES bauer_rag_v2.chunks(document_id, chunk_id) ON DELETE CASCADE,
    UNIQUE (document_id, chunk_id, kind, normalized_value, raw_value)
);

CREATE TABLE IF NOT EXISTS bauer_rag_v2.index_run_documents (
    run_id uuid NOT NULL
        REFERENCES bauer_rag_v2.index_runs(run_id) ON DELETE CASCADE,
    document_id uuid NOT NULL
        REFERENCES bauer_rag_v2.documents(document_id) ON DELETE CASCADE,
    file_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('indexed', 'reused')),
    PRIMARY KEY (run_id, file_id)
);

CREATE INDEX IF NOT EXISTS ix_bauer_v2_runs_namespace_version
    ON bauer_rag_v2.index_runs (namespace, index_version, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bauer_v2_active_run
    ON bauer_rag_v2.index_runs (namespace, index_version)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_bauer_v2_documents_authorized
    ON bauer_rag_v2.documents (namespace, index_version, file_id)
    WHERE active;
CREATE INDEX IF NOT EXISTS ix_bauer_v2_documents_checksum
    ON bauer_rag_v2.documents (namespace, file_id, checksum);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_documents_filename_trgm
    ON bauer_rag_v2.documents USING gin (filename gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_documents_title_trgm
    ON bauer_rag_v2.documents USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_chunks_authorized
    ON bauer_rag_v2.chunks (namespace, index_version, file_id);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_chunks_lexical
    ON bauer_rag_v2.chunks USING gin (search_vector);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_chunks_trgm
    ON bauer_rag_v2.chunks USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_chunks_embedding
    ON bauer_rag_v2.chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_entities_exact
    ON bauer_rag_v2.entities (kind, normalized_value);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_entities_document_exact
    ON bauer_rag_v2.entities (document_id, normalized_value);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_entities_chunk_exact
    ON bauer_rag_v2.entities (document_id, chunk_id, normalized_value);
CREATE INDEX IF NOT EXISTS ix_bauer_v2_entities_trgm
    ON bauer_rag_v2.entities USING gin (normalized_value gin_trgm_ops);
