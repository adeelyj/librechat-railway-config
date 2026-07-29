CREATE TABLE bauer_rag_v4.canonical_documents (
    canonical_document_id text PRIMARY KEY
        CHECK (canonical_document_id ~ '^document_[0-9a-f]{32}$'),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    canonical_sha256 char(64) NOT NULL CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_schema_version text NOT NULL,
    parser_identity text NOT NULL,
    block_count integer NOT NULL CHECK (block_count >= 0),
    table_count integer NOT NULL CHECK (table_count >= 0),
    cell_count integer NOT NULL CHECK (cell_count >= 0),
    fact_count integer NOT NULL CHECK (fact_count >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, knowledge_base_id, release_id)
        REFERENCES bauer_rag_v4.knowledge_releases(
            tenant_id, knowledge_base_id, release_id
        ),
    FOREIGN KEY (tenant_id, knowledge_base_id, source_id, source_version_id)
        REFERENCES bauer_rag_v4.source_versions(
            tenant_id, knowledge_base_id, source_id, source_version_id
        ),
    UNIQUE (tenant_id, knowledge_base_id, release_id, source_id),
    UNIQUE (tenant_id, knowledge_base_id, release_id, canonical_sha256)
);

CREATE TABLE bauer_rag_v4.canonical_blocks (
    block_id text PRIMARY KEY CHECK (block_id ~ '^block_[0-9a-f]{32}$'),
    canonical_document_id text NOT NULL
        REFERENCES bauer_rag_v4.canonical_documents(canonical_document_id),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    block_kind text NOT NULL,
    physical_page integer,
    printed_page text,
    section_path text[] NOT NULL DEFAULT '{}',
    source_locator text NOT NULL,
    text_sha256 char(64) NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    text_content text NOT NULL
);

CREATE TABLE bauer_rag_v4.canonical_tables (
    table_id text PRIMARY KEY CHECK (table_id ~ '^table_[0-9a-f]{32}$'),
    canonical_document_id text NOT NULL
        REFERENCES bauer_rag_v4.canonical_documents(canonical_document_id),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    caption text,
    section_path text[] NOT NULL DEFAULT '{}',
    physical_page integer,
    printed_page text,
    source_locator text NOT NULL,
    row_count integer NOT NULL CHECK (row_count > 0),
    column_count integer NOT NULL CHECK (column_count > 0)
);

CREATE TABLE bauer_rag_v4.canonical_cells (
    cell_id text PRIMARY KEY CHECK (cell_id ~ '^cell_[0-9a-f]{32}$'),
    table_id text NOT NULL
        REFERENCES bauer_rag_v4.canonical_tables(table_id),
    canonical_document_id text NOT NULL
        REFERENCES bauer_rag_v4.canonical_documents(canonical_document_id),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    row_index integer NOT NULL CHECK (row_index >= 0),
    column_index integer NOT NULL CHECK (column_index >= 0),
    role text NOT NULL
        CHECK (
            role IN ('header', 'group_header', 'row_header', 'body', 'note')
        ),
    header_path text[] NOT NULL DEFAULT '{}',
    raw_text text NOT NULL,
    normalized_value text,
    raw_unit text,
    normalized_unit text,
    qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_locator text NOT NULL,
    UNIQUE (table_id, row_index, column_index)
);

CREATE TABLE bauer_rag_v4.canonical_facts (
    fact_id text PRIMARY KEY CHECK (fact_id ~ '^fact_[0-9a-f]{32}$'),
    canonical_document_id text NOT NULL
        REFERENCES bauer_rag_v4.canonical_documents(canonical_document_id),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    subject text NOT NULL,
    predicate text NOT NULL,
    raw_value text NOT NULL,
    numeric_value numeric,
    minimum_value numeric,
    maximum_value numeric,
    raw_unit text,
    normalized_unit text,
    qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance_ids text[] NOT NULL CHECK (cardinality(provenance_ids) > 0),
    confidence numeric(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    review_status text NOT NULL
        CHECK (review_status IN ('candidate', 'verified', 'conflicted'))
);

CREATE TABLE bauer_rag_v4.search_projections (
    projection_id text PRIMARY KEY
        CHECK (projection_id ~ '^projection_[0-9a-f]{32}$'),
    canonical_document_id text NOT NULL
        REFERENCES bauer_rag_v4.canonical_documents(canonical_document_id),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    projection_type text NOT NULL
        CHECK (projection_type IN ('metadata', 'passage', 'table_row', 'fact')),
    projection_schema text NOT NULL,
    search_text text NOT NULL,
    search_text_sha256 char(64) NOT NULL
        CHECK (search_text_sha256 ~ '^[0-9a-f]{64}$'),
    exact_terms text[] NOT NULL DEFAULT '{}',
    canonical_evidence_ids text[] NOT NULL
        CHECK (cardinality(canonical_evidence_ids) > 0),
    subject text,
    predicate text,
    qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding_identity_sha256 char(64)
        CHECK (
            embedding_identity_sha256 IS NULL
            OR embedding_identity_sha256 ~ '^[0-9a-f]{64}$'
        ),
    embedding vector
);

CREATE TABLE bauer_rag_v4.embedding_cache (
    embedding_cache_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    revision text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    normalization text NOT NULL,
    search_text_sha256 char(64) NOT NULL
        CHECK (search_text_sha256 ~ '^[0-9a-f]{64}$'),
    embedding_identity_sha256 char(64) NOT NULL
        CHECK (embedding_identity_sha256 ~ '^[0-9a-f]{64}$'),
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, knowledge_base_id)
        REFERENCES bauer_rag_v4.knowledge_bases(
            tenant_id, knowledge_base_id
        ),
    UNIQUE (
        tenant_id,
        knowledge_base_id,
        provider,
        model,
        revision,
        dimensions,
        normalization,
        search_text_sha256
    ),
    UNIQUE (tenant_id, knowledge_base_id, embedding_identity_sha256)
);

CREATE INDEX canonical_blocks_scope_idx
    ON bauer_rag_v4.canonical_blocks
    (tenant_id, knowledge_base_id, release_id, source_id);
CREATE INDEX canonical_tables_scope_idx
    ON bauer_rag_v4.canonical_tables
    (tenant_id, knowledge_base_id, release_id, source_id);
CREATE INDEX canonical_cells_scope_idx
    ON bauer_rag_v4.canonical_cells
    (tenant_id, knowledge_base_id, release_id, source_id);
CREATE INDEX canonical_facts_lookup_idx
    ON bauer_rag_v4.canonical_facts
    (tenant_id, knowledge_base_id, release_id, subject, predicate);
CREATE INDEX projections_scope_type_idx
    ON bauer_rag_v4.search_projections
    (tenant_id, knowledge_base_id, release_id, projection_type, source_id);
CREATE INDEX projections_exact_terms_gin
    ON bauer_rag_v4.search_projections USING gin (exact_terms);
CREATE INDEX projections_lexical_gin
    ON bauer_rag_v4.search_projections
    USING gin (to_tsvector('simple', search_text));
CREATE INDEX projections_trigram_gin
    ON bauer_rag_v4.search_projections
    USING gin (search_text gin_trgm_ops);
