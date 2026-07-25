CREATE TABLE IF NOT EXISTS bauer_rag_v3.artifact_sets (
    artifact_set_id uuid PRIMARY KEY,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    compiler_fingerprint char(64) NOT NULL
        CHECK (compiler_fingerprint ~ '^[0-9a-f]{64}$'),
    status text NOT NULL
        CHECK (status IN ('building', 'valid', 'quarantined', 'invalid')),
    parser_version text NOT NULL,
    ocr_version text,
    fact_model_version text,
    worker_job_id uuid,
    expected_page_count integer
        CHECK (expected_page_count IS NULL OR expected_page_count >= 0),
    quality_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    finalized_at timestamptz,
    error text,
    UNIQUE (source_version_id, artifact_set_id),
    UNIQUE (source_version_id, compiler_fingerprint),
    FOREIGN KEY (source_id, source_version_id)
        REFERENCES bauer_rag_v3.source_versions(source_id, source_version_id)
        ON DELETE CASCADE,
    CHECK (btrim(parser_version) <> ''),
    CHECK (
        (status = 'building' AND finalized_at IS NULL)
        OR
        (status <> 'building' AND finalized_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.release_sources (
    kb_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    artifact_set_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    action text NOT NULL CHECK (action IN ('compiled', 'reused')),
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (release_id, source_id),
    UNIQUE (release_id, source_version_id),
    UNIQUE (
        release_id,
        source_id,
        source_version_id,
        artifact_set_id
    ),
    UNIQUE (release_id, ordinal),
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE CASCADE,
    FOREIGN KEY (kb_id, source_id)
        REFERENCES bauer_rag_v3.sources(kb_id, source_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (source_id, source_version_id)
        REFERENCES bauer_rag_v3.source_versions(source_id, source_version_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (source_version_id, artifact_set_id)
        REFERENCES bauer_rag_v3.artifact_sets(
            source_version_id,
            artifact_set_id
        )
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.pages (
    page_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    page_number integer NOT NULL CHECK (page_number >= 1),
    page_label text,
    width_points double precision
        CHECK (width_points IS NULL OR width_points > 0),
    height_points double precision
        CHECK (height_points IS NULL OR height_points > 0),
    rotation_degrees smallint NOT NULL DEFAULT 0
        CHECK (rotation_degrees IN (0, 90, 180, 270)),
    rendered_object_sha256 char(64)
        REFERENCES bauer_rag_v3.objects(sha256) ON DELETE RESTRICT,
    ocr_confidence numeric(6,5)
        CHECK (
            ocr_confidence IS NULL
            OR (ocr_confidence >= 0 AND ocr_confidence <= 1)
        ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, page_id),
    UNIQUE (artifact_set_id, page_number)
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.sections (
    section_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    parent_section_id uuid,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    level integer NOT NULL CHECK (level >= 0),
    title text,
    normalized_title text,
    start_page_number integer CHECK (start_page_number IS NULL OR start_page_number >= 1),
    end_page_number integer CHECK (end_page_number IS NULL OR end_page_number >= 1),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, section_id),
    UNIQUE (artifact_set_id, ordinal),
    FOREIGN KEY (artifact_set_id, parent_section_id)
        REFERENCES bauer_rag_v3.sections(artifact_set_id, section_id)
        ON DELETE CASCADE,
    CHECK (
        start_page_number IS NULL
        OR end_page_number IS NULL
        OR start_page_number <= end_page_number
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.blocks (
    block_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL,
    page_id uuid NOT NULL,
    section_id uuid,
    reading_order integer NOT NULL CHECK (reading_order >= 0),
    block_type text NOT NULL
        CHECK (block_type IN (
            'heading',
            'paragraph',
            'list_item',
            'caption',
            'table_anchor',
            'figure',
            'header',
            'footer',
            'other'
        )),
    original_text text NOT NULL,
    normalized_text text NOT NULL,
    x0 double precision,
    y0 double precision,
    x1 double precision,
    y1 double precision,
    confidence numeric(6,5)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    crop_object_sha256 char(64)
        REFERENCES bauer_rag_v3.objects(sha256) ON DELETE RESTRICT,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, block_id),
    UNIQUE (artifact_set_id, page_id, reading_order),
    FOREIGN KEY (artifact_set_id, page_id)
        REFERENCES bauer_rag_v3.pages(artifact_set_id, page_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, section_id)
        REFERENCES bauer_rag_v3.sections(artifact_set_id, section_id)
        ON DELETE SET NULL,
    CHECK (
        (num_nonnulls(x0, y0, x1, y1) = 0)
        OR
        (
            num_nonnulls(x0, y0, x1, y1) = 4
            AND x0 >= 0
            AND y0 >= 0
            AND x1 > x0
            AND y1 > y0
        )
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.tables (
    table_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    section_id uuid,
    continuation_of_table_id uuid,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    title text,
    caption text,
    row_count integer NOT NULL CHECK (row_count >= 0),
    column_count integer NOT NULL CHECK (column_count > 0),
    header_row_count integer NOT NULL DEFAULT 0
        CHECK (header_row_count >= 0 AND header_row_count <= row_count),
    confidence numeric(6,5)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, table_id),
    UNIQUE (artifact_set_id, ordinal),
    FOREIGN KEY (artifact_set_id, section_id)
        REFERENCES bauer_rag_v3.sections(artifact_set_id, section_id)
        ON DELETE SET NULL,
    FOREIGN KEY (artifact_set_id, continuation_of_table_id)
        REFERENCES bauer_rag_v3.tables(artifact_set_id, table_id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.table_segments (
    table_segment_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL,
    table_id uuid NOT NULL,
    page_id uuid NOT NULL,
    segment_ordinal integer NOT NULL CHECK (segment_ordinal >= 0),
    x0 double precision NOT NULL CHECK (x0 >= 0),
    y0 double precision NOT NULL CHECK (y0 >= 0),
    x1 double precision NOT NULL,
    y1 double precision NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, table_segment_id),
    UNIQUE (artifact_set_id, table_id, segment_ordinal),
    FOREIGN KEY (artifact_set_id, table_id)
        REFERENCES bauer_rag_v3.tables(artifact_set_id, table_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, page_id)
        REFERENCES bauer_rag_v3.pages(artifact_set_id, page_id)
        ON DELETE CASCADE,
    CHECK (x1 > x0 AND y1 > y0)
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.table_cells (
    cell_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL,
    table_id uuid NOT NULL,
    page_id uuid NOT NULL,
    row_index integer NOT NULL CHECK (row_index >= 0),
    column_index integer NOT NULL CHECK (column_index >= 0),
    row_span integer NOT NULL DEFAULT 1 CHECK (row_span > 0),
    column_span integer NOT NULL DEFAULT 1 CHECK (column_span > 0),
    cell_role text NOT NULL
        CHECK (cell_role IN ('header', 'row_header', 'body', 'stub', 'note')),
    raw_text text NOT NULL,
    normalized_text text NOT NULL,
    numeric_value numeric,
    unit_raw text,
    unit_ucum text,
    x0 double precision,
    y0 double precision,
    x1 double precision,
    y1 double precision,
    confidence numeric(6,5)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, cell_id),
    UNIQUE (artifact_set_id, table_id, row_index, column_index),
    FOREIGN KEY (artifact_set_id, table_id)
        REFERENCES bauer_rag_v3.tables(artifact_set_id, table_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, page_id)
        REFERENCES bauer_rag_v3.pages(artifact_set_id, page_id)
        ON DELETE CASCADE,
    CHECK (
        (num_nonnulls(x0, y0, x1, y1) = 0)
        OR
        (
            num_nonnulls(x0, y0, x1, y1) = 4
            AND x0 >= 0
            AND y0 >= 0
            AND x1 > x0
            AND y1 > y0
        )
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.provenance_spans (
    provenance_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    page_id uuid,
    block_id uuid,
    cell_id uuid,
    char_start integer,
    char_end integer,
    x0 double precision,
    y0 double precision,
    x1 double precision,
    y1 double precision,
    quoted_text text,
    quote_sha256 char(64)
        CHECK (quote_sha256 IS NULL OR quote_sha256 ~ '^[0-9a-f]{64}$'),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, provenance_id),
    FOREIGN KEY (artifact_set_id, page_id)
        REFERENCES bauer_rag_v3.pages(artifact_set_id, page_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, block_id)
        REFERENCES bauer_rag_v3.blocks(artifact_set_id, block_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, cell_id)
        REFERENCES bauer_rag_v3.table_cells(artifact_set_id, cell_id)
        ON DELETE CASCADE,
    CHECK (num_nonnulls(page_id, block_id, cell_id) = 1),
    CHECK (
        (char_start IS NULL AND char_end IS NULL)
        OR
        (
            char_start IS NOT NULL
            AND char_end IS NOT NULL
            AND char_start >= 0
            AND char_end > char_start
        )
    ),
    CHECK (
        (num_nonnulls(x0, y0, x1, y1) = 0)
        OR
        (
            num_nonnulls(x0, y0, x1, y1) = 4
            AND x0 >= 0
            AND y0 >= 0
            AND x1 > x0
            AND y1 > y0
        )
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.entities (
    entity_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    entity_type text NOT NULL,
    canonical_name text NOT NULL,
    normalized_key text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_set_id, entity_id),
    UNIQUE (artifact_set_id, entity_type, normalized_key),
    CHECK (btrim(entity_type) <> ''),
    CHECK (btrim(canonical_name) <> ''),
    CHECK (btrim(normalized_key) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.entity_mentions (
    entity_id uuid NOT NULL,
    artifact_set_id uuid NOT NULL,
    provenance_id uuid NOT NULL,
    raw_mention text NOT NULL,
    confidence numeric(6,5)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (entity_id, provenance_id),
    FOREIGN KEY (artifact_set_id, entity_id)
        REFERENCES bauer_rag_v3.entities(artifact_set_id, entity_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, provenance_id)
        REFERENCES bauer_rag_v3.provenance_spans(
            artifact_set_id,
            provenance_id
        )
        ON DELETE CASCADE,
    CHECK (btrim(raw_mention) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.facts (
    fact_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL,
    fact_sha256 char(64) NOT NULL
        CHECK (fact_sha256 ~ '^[0-9a-f]{64}$'),
    subject_entity_id uuid,
    subject_key text NOT NULL,
    predicate text NOT NULL,
    value_kind text NOT NULL
        CHECK (value_kind IN ('text', 'numeric', 'date', 'boolean', 'entity')),
    raw_value text NOT NULL,
    value_text text,
    numeric_value numeric,
    date_value date,
    boolean_value boolean,
    object_entity_id uuid,
    unit_raw text,
    unit_ucum text,
    qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence numeric(6,5)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    extraction_method text NOT NULL,
    verification_status text NOT NULL DEFAULT 'candidate'
        CHECK (verification_status IN (
            'candidate',
            'verified',
            'rejected',
            'superseded'
        )),
    primary_provenance_id uuid NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_set_id, fact_id),
    UNIQUE (artifact_set_id, fact_sha256),
    FOREIGN KEY (artifact_set_id)
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, subject_entity_id)
        REFERENCES bauer_rag_v3.entities(artifact_set_id, entity_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_set_id, object_entity_id)
        REFERENCES bauer_rag_v3.entities(artifact_set_id, entity_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_set_id, primary_provenance_id)
        REFERENCES bauer_rag_v3.provenance_spans(
            artifact_set_id,
            provenance_id
        )
        ON DELETE RESTRICT,
    CHECK (btrim(subject_key) <> ''),
    CHECK (btrim(predicate) <> ''),
    CHECK (btrim(raw_value) <> ''),
    CHECK (btrim(extraction_method) <> ''),
    CHECK (
        (
            value_kind = 'text'
            AND value_text IS NOT NULL
            AND num_nonnulls(
                numeric_value,
                date_value,
                boolean_value,
                object_entity_id
            ) = 0
        )
        OR
        (
            value_kind = 'numeric'
            AND numeric_value IS NOT NULL
            AND num_nonnulls(
                value_text,
                date_value,
                boolean_value,
                object_entity_id
            ) = 0
        )
        OR
        (
            value_kind = 'date'
            AND date_value IS NOT NULL
            AND num_nonnulls(
                value_text,
                numeric_value,
                boolean_value,
                object_entity_id
            ) = 0
        )
        OR
        (
            value_kind = 'boolean'
            AND boolean_value IS NOT NULL
            AND num_nonnulls(
                value_text,
                numeric_value,
                date_value,
                object_entity_id
            ) = 0
        )
        OR
        (
            value_kind = 'entity'
            AND object_entity_id IS NOT NULL
            AND num_nonnulls(
                value_text,
                numeric_value,
                date_value,
                boolean_value
            ) = 0
        )
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.fact_provenance (
    fact_id uuid NOT NULL,
    artifact_set_id uuid NOT NULL,
    provenance_id uuid NOT NULL,
    provenance_role text NOT NULL
        CHECK (provenance_role IN ('primary', 'supporting', 'qualifier')),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (fact_id, provenance_id),
    UNIQUE (fact_id, provenance_role, ordinal),
    FOREIGN KEY (artifact_set_id, fact_id)
        REFERENCES bauer_rag_v3.facts(artifact_set_id, fact_id)
        ON DELETE CASCADE,
    FOREIGN KEY (artifact_set_id, provenance_id)
        REFERENCES bauer_rag_v3.provenance_spans(
            artifact_set_id,
            provenance_id
        )
        ON DELETE RESTRICT
);
