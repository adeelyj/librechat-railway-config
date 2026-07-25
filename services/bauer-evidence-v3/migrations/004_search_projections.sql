CREATE TABLE IF NOT EXISTS bauer_rag_v3.search_units (
    search_unit_id uuid PRIMARY KEY,
    kb_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    artifact_set_id uuid NOT NULL,
    unit_key text NOT NULL,
    unit_type text NOT NULL
        CHECK (unit_type IN (
            'document_summary',
            'section',
            'paragraph',
            'table_row',
            'table_cell',
            'fact',
            'figure_caption',
            'navigation'
        )),
    page_id uuid,
    section_id uuid,
    table_id uuid,
    table_row_index integer
        CHECK (table_row_index IS NULL OR table_row_index >= 0),
    page_start integer CHECK (page_start IS NULL OR page_start >= 1),
    page_end integer CHECK (page_end IS NULL OR page_end >= 1),
    language text,
    display_text text NOT NULL,
    search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(search_text, ''))
    ) STORED,
    embedding_model_version text NOT NULL,
    embedding vector(1024),
    token_count integer CHECK (token_count IS NULL OR token_count >= 0),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    primary_provenance_id uuid,
    is_citable boolean NOT NULL DEFAULT true,
    generated_summary boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (release_id, search_unit_id),
    UNIQUE (
        release_id,
        search_unit_id,
        source_id,
        source_version_id
    ),
    UNIQUE (release_id, source_version_id, unit_key),
    FOREIGN KEY (
        release_id,
        source_id,
        source_version_id,
        artifact_set_id
    )
        REFERENCES bauer_rag_v3.release_sources(
            release_id,
            source_id,
            source_version_id,
            artifact_set_id
        )
        ON DELETE CASCADE,
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, page_id)
        REFERENCES bauer_rag_v3.pages(artifact_set_id, page_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_set_id, section_id)
        REFERENCES bauer_rag_v3.sections(artifact_set_id, section_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_set_id, table_id)
        REFERENCES bauer_rag_v3.tables(artifact_set_id, table_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_set_id, primary_provenance_id)
        REFERENCES bauer_rag_v3.provenance_spans(
            artifact_set_id,
            provenance_id
        )
        ON DELETE RESTRICT,
    CHECK (btrim(unit_key) <> ''),
    CHECK (btrim(display_text) <> ''),
    CHECK (btrim(search_text) <> ''),
    CHECK (btrim(embedding_model_version) <> ''),
    CHECK (NOT is_citable OR primary_provenance_id IS NOT NULL),
    CHECK (NOT generated_summary OR NOT is_citable),
    CHECK (table_row_index IS NULL OR table_id IS NOT NULL),
    CHECK (
        page_start IS NULL
        OR page_end IS NULL
        OR page_start <= page_end
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.exact_terms (
    exact_term_id uuid PRIMARY KEY,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    search_unit_id uuid NOT NULL,
    term_type text NOT NULL,
    raw_term text NOT NULL,
    normalized_term text NOT NULL,
    provenance_id uuid NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (
        release_id,
        term_type,
        normalized_term,
        search_unit_id
    ),
    FOREIGN KEY (release_id, source_id)
        REFERENCES bauer_rag_v3.release_sources(release_id, source_id)
        ON DELETE CASCADE,
    FOREIGN KEY (
        release_id,
        search_unit_id,
        source_id,
        source_version_id
    )
        REFERENCES bauer_rag_v3.search_units(
            release_id,
            search_unit_id,
            source_id,
            source_version_id
        )
        ON DELETE CASCADE,
    FOREIGN KEY (provenance_id)
        REFERENCES bauer_rag_v3.provenance_spans(provenance_id)
        ON DELETE RESTRICT,
    CHECK (btrim(term_type) <> ''),
    CHECK (btrim(raw_term) <> ''),
    CHECK (btrim(normalized_term) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.nav_nodes (
    nav_node_id uuid PRIMARY KEY,
    release_id uuid NOT NULL
        REFERENCES bauer_rag_v3.knowledge_releases(release_id)
        ON DELETE CASCADE,
    source_id uuid,
    node_type text NOT NULL
        CHECK (node_type IN (
            'category',
            'product',
            'document',
            'artifact',
            'topic'
        )),
    node_key text NOT NULL,
    label text NOT NULL,
    description_search_unit_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (release_id, nav_node_id),
    UNIQUE (release_id, node_type, node_key),
    FOREIGN KEY (release_id, source_id)
        REFERENCES bauer_rag_v3.release_sources(release_id, source_id)
        ON DELETE CASCADE,
    FOREIGN KEY (release_id, description_search_unit_id)
        REFERENCES bauer_rag_v3.search_units(release_id, search_unit_id)
        ON DELETE SET NULL,
    CHECK (btrim(node_key) <> ''),
    CHECK (btrim(label) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.nav_edges (
    nav_edge_id uuid PRIMARY KEY,
    release_id uuid NOT NULL,
    from_node_id uuid NOT NULL,
    to_node_id uuid NOT NULL,
    relation_type text NOT NULL,
    confidence numeric(6,5)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    provenance_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (release_id, from_node_id, to_node_id, relation_type),
    FOREIGN KEY (release_id, from_node_id)
        REFERENCES bauer_rag_v3.nav_nodes(release_id, nav_node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (release_id, to_node_id)
        REFERENCES bauer_rag_v3.nav_nodes(release_id, nav_node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (provenance_id)
        REFERENCES bauer_rag_v3.provenance_spans(provenance_id)
        ON DELETE RESTRICT,
    CHECK (from_node_id <> to_node_id),
    CHECK (btrim(relation_type) <> '')
);
