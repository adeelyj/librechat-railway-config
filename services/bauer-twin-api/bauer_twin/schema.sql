CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS bauer_twin;

CREATE TABLE IF NOT EXISTS bauer_twin.projects (
    project_id text PRIMARY KEY,
    name text NOT NULL,
    synthetic boolean NOT NULL DEFAULT true CHECK (synthetic),
    cluster text NOT NULL,
    application_sector text NOT NULL,
    medium text NOT NULL,
    pressure_bar numeric NOT NULL CHECK (pressure_bar > 0),
    capacity_l_min numeric NOT NULL CHECK (capacity_l_min > 0),
    compressor_family text NOT NULL,
    compressor_model text NOT NULL,
    topology text NOT NULL,
    cooling text NOT NULL,
    control_package text NOT NULL,
    purification_package text NOT NULL,
    storage_filling_package text NOT NULL,
    installation text NOT NULL,
    environment text NOT NULL,
    standards jsonb NOT NULL,
    status text NOT NULL,
    summary text NOT NULL,
    document_ids jsonb NOT NULL,
    search_text text NOT NULL,
    embedding vector(1024) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bauer_twin.parts (
    part_id text PRIMARY KEY,
    description text NOT NULL,
    synthetic boolean NOT NULL DEFAULT true CHECK (synthetic),
    category text NOT NULL,
    lifecycle_status text NOT NULL,
    synonyms jsonb NOT NULL,
    compatible_models jsonb NOT NULL,
    compatible_media jsonb NOT NULL,
    min_pressure_bar numeric NOT NULL,
    max_pressure_bar numeric NOT NULL CHECK (max_pressure_bar >= min_pressure_bar),
    project_ids jsonb NOT NULL,
    document_ids jsonb NOT NULL,
    summary text NOT NULL,
    search_text text NOT NULL,
    embedding vector(1024) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bauer_twin.documents (
    document_id text PRIMARY KEY,
    title text NOT NULL,
    document_type text NOT NULL,
    source_url text NOT NULL,
    language text NOT NULL,
    local_filename text NOT NULL,
    summary text NOT NULL,
    search_text text NOT NULL,
    embedding vector(1024) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bauer_twin.terminology_aliases (
    domain text NOT NULL,
    canonical_value text NOT NULL,
    alias text NOT NULL,
    language text NOT NULL,
    query_safe boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (domain, alias)
);

CREATE INDEX IF NOT EXISTS projects_search_fts_idx ON bauer_twin.projects USING gin (to_tsvector('simple', search_text));
CREATE INDEX IF NOT EXISTS projects_search_trgm_idx ON bauer_twin.projects USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS projects_embedding_hnsw_idx ON bauer_twin.projects USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS parts_search_fts_idx ON bauer_twin.parts USING gin (to_tsvector('simple', search_text));
CREATE INDEX IF NOT EXISTS parts_search_trgm_idx ON bauer_twin.parts USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS parts_embedding_hnsw_idx ON bauer_twin.parts USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS documents_search_fts_idx ON bauer_twin.documents USING gin (to_tsvector('simple', search_text));
CREATE INDEX IF NOT EXISTS documents_embedding_hnsw_idx ON bauer_twin.documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS terminology_canonical_idx ON bauer_twin.terminology_aliases (domain, canonical_value);

