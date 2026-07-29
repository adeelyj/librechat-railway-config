CREATE TABLE bauer_rag_v4.tenants (
    tenant_id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE bauer_rag_v4.knowledge_bases (
    knowledge_base_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bauer_rag_v4.tenants(tenant_id),
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, knowledge_base_id),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE bauer_rag_v4.principals (
    principal_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bauer_rag_v4.tenants(tenant_id),
    external_subject_sha256 char(64) NOT NULL
        CHECK (external_subject_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, principal_id),
    UNIQUE (tenant_id, external_subject_sha256)
);

CREATE TABLE bauer_rag_v4.source_documents (
    source_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    external_source_id text NOT NULL,
    original_filename text NOT NULL,
    authority text NOT NULL DEFAULT 'original'
        CHECK (authority IN ('original', 'derived_verified')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, knowledge_base_id)
        REFERENCES bauer_rag_v4.knowledge_bases(tenant_id, knowledge_base_id),
    UNIQUE (tenant_id, knowledge_base_id, source_id),
    UNIQUE (tenant_id, knowledge_base_id, external_source_id)
);

CREATE TABLE bauer_rag_v4.source_versions (
    source_version_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    source_id uuid NOT NULL,
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    media_type text NOT NULL,
    object_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, knowledge_base_id, source_id)
        REFERENCES bauer_rag_v4.source_documents(
            tenant_id, knowledge_base_id, source_id
        ),
    UNIQUE (tenant_id, knowledge_base_id, source_id, source_version_id),
    UNIQUE (tenant_id, knowledge_base_id, source_id, content_sha256)
);

CREATE TABLE bauer_rag_v4.principal_source_grants (
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    source_id uuid NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, knowledge_base_id, principal_id, source_id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES bauer_rag_v4.principals(tenant_id, principal_id),
    FOREIGN KEY (tenant_id, knowledge_base_id, source_id)
        REFERENCES bauer_rag_v4.source_documents(
            tenant_id, knowledge_base_id, source_id
        )
);

CREATE TABLE bauer_rag_v4.knowledge_releases (
    release_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_public_id text NOT NULL,
    status text NOT NULL
        CHECK (status IN ('building', 'validating', 'ready', 'failed', 'retired')),
    source_contract_sha256 char(64) NOT NULL
        CHECK (source_contract_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_schema_version text NOT NULL,
    compiler_identity_sha256 char(64) NOT NULL
        CHECK (compiler_identity_sha256 ~ '^[0-9a-f]{64}$'),
    projection_identity_sha256 char(64) NOT NULL
        CHECK (projection_identity_sha256 ~ '^[0-9a-f]{64}$'),
    embedding_identity_sha256 char(64) NOT NULL
        CHECK (embedding_identity_sha256 ~ '^[0-9a-f]{64}$'),
    reranker_identity_sha256 char(64) NOT NULL
        CHECK (reranker_identity_sha256 ~ '^[0-9a-f]{64}$'),
    gate_manifest_sha256 char(64) NOT NULL
        CHECK (gate_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ready_at timestamptz,
    FOREIGN KEY (tenant_id, knowledge_base_id)
        REFERENCES bauer_rag_v4.knowledge_bases(tenant_id, knowledge_base_id),
    UNIQUE (tenant_id, knowledge_base_id, release_id),
    UNIQUE (tenant_id, knowledge_base_id, release_public_id),
    CHECK ((status = 'ready') = (ready_at IS NOT NULL))
);

CREATE TABLE bauer_rag_v4.release_sources (
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (release_id, source_id),
    FOREIGN KEY (tenant_id, knowledge_base_id, release_id)
        REFERENCES bauer_rag_v4.knowledge_releases(
            tenant_id, knowledge_base_id, release_id
        ),
    FOREIGN KEY (tenant_id, knowledge_base_id, source_id, source_version_id)
        REFERENCES bauer_rag_v4.source_versions(
            tenant_id, knowledge_base_id, source_id, source_version_id
        ),
    UNIQUE (release_id, ordinal)
);

CREATE TABLE bauer_rag_v4.active_release_pointers (
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, knowledge_base_id),
    FOREIGN KEY (tenant_id, knowledge_base_id, release_id)
        REFERENCES bauer_rag_v4.knowledge_releases(
            tenant_id, knowledge_base_id, release_id
        )
);

CREATE FUNCTION bauer_rag_v4.reject_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = '55000';
END
$$;

CREATE TRIGGER source_versions_immutable
BEFORE UPDATE OR DELETE ON bauer_rag_v4.source_versions
FOR EACH ROW EXECUTE FUNCTION bauer_rag_v4.reject_immutable_mutation();

CREATE TRIGGER release_sources_immutable
BEFORE UPDATE OR DELETE ON bauer_rag_v4.release_sources
FOR EACH ROW EXECUTE FUNCTION bauer_rag_v4.reject_immutable_mutation();

CREATE FUNCTION bauer_rag_v4.require_building_release_membership()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM bauer_rag_v4.knowledge_releases AS release
        WHERE release.release_id = NEW.release_id
          AND release.tenant_id = NEW.tenant_id
          AND release.knowledge_base_id = NEW.knowledge_base_id
          AND release.status = 'building'
    ) THEN
        RAISE EXCEPTION 'release membership is writable only while building'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER release_sources_building_only
BEFORE INSERT ON bauer_rag_v4.release_sources
FOR EACH ROW EXECUTE FUNCTION bauer_rag_v4.require_building_release_membership();

CREATE FUNCTION bauer_rag_v4.require_ready_active_release()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM bauer_rag_v4.knowledge_releases AS release
        WHERE release.release_id = NEW.release_id
          AND release.tenant_id = NEW.tenant_id
          AND release.knowledge_base_id = NEW.knowledge_base_id
          AND release.status = 'ready'
    ) THEN
        RAISE EXCEPTION 'active release must be ready and in scope'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER active_release_must_be_ready
BEFORE INSERT OR UPDATE ON bauer_rag_v4.active_release_pointers
FOR EACH ROW EXECUTE FUNCTION bauer_rag_v4.require_ready_active_release();
