CREATE TABLE IF NOT EXISTS bauer_rag_v3.tenants (
    tenant_id uuid PRIMARY KEY,
    external_key text NOT NULL UNIQUE,
    display_name text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (btrim(external_key) <> ''),
    CHECK (btrim(display_name) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.knowledge_bases (
    kb_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL
        REFERENCES bauer_rag_v3.tenants(tenant_id) ON DELETE RESTRICT,
    external_namespace text NOT NULL,
    name text NOT NULL,
    description text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kb_id),
    UNIQUE (tenant_id, external_namespace),
    CHECK (btrim(external_namespace) <> ''),
    CHECK (btrim(name) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.principals (
    principal_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL
        REFERENCES bauer_rag_v3.tenants(tenant_id) ON DELETE CASCADE,
    principal_type text NOT NULL
        CHECK (principal_type IN ('user', 'group', 'service')),
    external_subject text NOT NULL,
    display_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, principal_id),
    UNIQUE (tenant_id, principal_type, external_subject),
    CHECK (btrim(external_subject) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.kb_grants (
    tenant_id uuid NOT NULL,
    kb_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    permission text NOT NULL
        CHECK (permission IN ('read', 'ingest', 'admin')),
    granted_by text,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kb_id, principal_id, permission),
    FOREIGN KEY (tenant_id, kb_id)
        REFERENCES bauer_rag_v3.knowledge_bases(tenant_id, kb_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES bauer_rag_v3.principals(tenant_id, principal_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.objects (
    sha256 char(64) PRIMARY KEY
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    object_key text NOT NULL UNIQUE,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    mime_type text NOT NULL,
    etag text,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (btrim(object_key) <> ''),
    CHECK (btrim(mime_type) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.sources (
    source_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    kb_id uuid NOT NULL,
    external_file_id text NOT NULL,
    canonical_uri text,
    source_type text NOT NULL
        CHECK (source_type IN (
            'pdf',
            'html',
            'image',
            'text',
            'office_document',
            'structured_record',
            'synthetic_demo'
        )),
    visibility text NOT NULL DEFAULT 'inherited'
        CHECK (visibility IN ('inherited', 'restricted')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kb_id, source_id),
    UNIQUE (tenant_id, source_id),
    UNIQUE (kb_id, external_file_id),
    FOREIGN KEY (tenant_id, kb_id)
        REFERENCES bauer_rag_v3.knowledge_bases(tenant_id, kb_id)
        ON DELETE CASCADE,
    CHECK (btrim(external_file_id) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.source_grants (
    tenant_id uuid NOT NULL,
    source_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    permission text NOT NULL
        CHECK (permission IN ('read', 'admin')),
    granted_by text,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, principal_id, permission),
    FOREIGN KEY (tenant_id, source_id)
        REFERENCES bauer_rag_v3.sources(tenant_id, source_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES bauer_rag_v3.principals(tenant_id, principal_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.source_versions (
    source_version_id uuid PRIMARY KEY,
    source_id uuid NOT NULL,
    sha256 char(64) NOT NULL
        REFERENCES bauer_rag_v3.objects(sha256) ON DELETE RESTRICT,
    filename text NOT NULL,
    source_revision text,
    publication_date date,
    language text,
    page_count integer CHECK (page_count IS NULL OR page_count >= 0),
    discovered_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_version_id),
    UNIQUE (source_id, sha256),
    FOREIGN KEY (source_id)
        REFERENCES bauer_rag_v3.sources(source_id) ON DELETE CASCADE,
    CHECK (btrim(filename) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.knowledge_releases (
    release_id uuid PRIMARY KEY,
    kb_id uuid NOT NULL
        REFERENCES bauer_rag_v3.knowledge_bases(kb_id) ON DELETE CASCADE,
    status text NOT NULL
        CHECK (status IN (
            'draft',
            'building',
            'validating',
            'ready',
            'failed',
            'retired'
        )),
    based_on_release_id uuid,
    manifest_sha256 char(64) NOT NULL
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    expected_source_count integer NOT NULL
        CHECK (expected_source_count > 0),
    compiler_fingerprint char(64) NOT NULL
        CHECK (compiler_fingerprint ~ '^[0-9a-f]{64}$'),
    parser_version text NOT NULL,
    ocr_version text,
    fact_model_version text,
    embedding_model_version text NOT NULL,
    prompt_version text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    validation_started_at timestamptz,
    ready_at timestamptz,
    failed_at timestamptz,
    retired_at timestamptz,
    error text,
    UNIQUE (kb_id, release_id),
    FOREIGN KEY (kb_id, based_on_release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE RESTRICT,
    CHECK (btrim(parser_version) <> ''),
    CHECK (btrim(embedding_model_version) <> ''),
    CHECK (btrim(created_by) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.active_releases (
    kb_id uuid PRIMARY KEY,
    release_id uuid NOT NULL UNIQUE,
    activated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.release_activations (
    activation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kb_id uuid NOT NULL,
    release_id uuid NOT NULL,
    previous_release_id uuid,
    actor_principal_id uuid,
    reason text NOT NULL,
    production boolean NOT NULL DEFAULT true,
    activated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (kb_id, previous_release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (actor_principal_id)
        REFERENCES bauer_rag_v3.principals(principal_id)
        ON DELETE SET NULL,
    CHECK (btrim(reason) <> '')
);
