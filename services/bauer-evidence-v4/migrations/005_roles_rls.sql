DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'bauer_rag_v4_reader',
        'bauer_rag_v4_worker',
        'bauer_rag_v4_evaluator',
        'bauer_rag_v4_reviewer',
        'bauer_rag_v4_admin'
    ]
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            RAISE EXCEPTION 'refusing to reuse pre-existing role %', role_name;
        END IF;
        EXECUTE format(
            'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
            role_name
        );
    END LOOP;
END
$$;

CREATE FUNCTION bauer_rag_v4.current_tenant_id()
RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('bauer_rag_v4.tenant_id', true), '')::uuid $$;

CREATE FUNCTION bauer_rag_v4.current_knowledge_base_id()
RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('bauer_rag_v4.knowledge_base_id', true), '')::uuid $$;

CREATE FUNCTION bauer_rag_v4.current_principal_id()
RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('bauer_rag_v4.principal_id', true), '')::uuid $$;

CREATE FUNCTION bauer_rag_v4.current_release_id()
RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('bauer_rag_v4.release_id', true), '')::uuid $$;

CREATE FUNCTION bauer_rag_v4.requested_source_ids()
RETURNS uuid[]
LANGUAGE sql STABLE
AS $$
    SELECT CASE
        WHEN nullif(current_setting('bauer_rag_v4.source_ids', true), '') IS NULL
        THEN ARRAY[]::uuid[]
        ELSE string_to_array(
            current_setting('bauer_rag_v4.source_ids', true), ','
        )::uuid[]
    END
$$;

CREATE FUNCTION bauer_rag_v4.readable_source_ids()
RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
    SELECT coalesce(array_agg(grant_row.source_id ORDER BY grant_row.source_id), ARRAY[]::uuid[])
    FROM bauer_rag_v4.principal_source_grants AS grant_row
    WHERE grant_row.tenant_id = bauer_rag_v4.current_tenant_id()
      AND grant_row.knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
      AND grant_row.principal_id = bauer_rag_v4.current_principal_id()
      AND grant_row.source_id = ANY(bauer_rag_v4.requested_source_ids())
$$;

CREATE FUNCTION bauer_rag_v4.can_read_source(candidate_source_id uuid)
RETURNS boolean
LANGUAGE sql STABLE
AS $$
    SELECT candidate_source_id = ANY(
        bauer_rag_v4.readable_source_ids()
    )
$$;

CREATE FUNCTION bauer_rag_v4.resolve_pinned_release()
RETURNS SETOF bauer_rag_v4.knowledge_releases
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
    SELECT release.*
    FROM bauer_rag_v4.knowledge_releases AS release
    WHERE release.tenant_id = bauer_rag_v4.current_tenant_id()
      AND release.knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
      AND release.release_id = bauer_rag_v4.current_release_id()
      AND release.status = 'ready'
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA bauer_rag_v4 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bauer_rag_v4 FROM PUBLIC;
GRANT USAGE ON SCHEMA bauer_rag_v4 TO
    bauer_rag_v4_reader,
    bauer_rag_v4_worker,
    bauer_rag_v4_evaluator,
    bauer_rag_v4_reviewer,
    bauer_rag_v4_admin;

GRANT SELECT ON
    bauer_rag_v4.tenants,
    bauer_rag_v4.knowledge_bases,
    bauer_rag_v4.source_documents,
    bauer_rag_v4.source_versions,
    bauer_rag_v4.knowledge_releases,
    bauer_rag_v4.release_sources,
    bauer_rag_v4.canonical_documents,
    bauer_rag_v4.canonical_blocks,
    bauer_rag_v4.canonical_tables,
    bauer_rag_v4.canonical_cells,
    bauer_rag_v4.canonical_facts,
    bauer_rag_v4.search_projections
TO bauer_rag_v4_reader, bauer_rag_v4_evaluator;

GRANT SELECT ON
    bauer_rag_v4.knowledge_releases,
    bauer_rag_v4.evaluation_runs,
    bauer_rag_v4.review_attestations
TO bauer_rag_v4_reviewer;

GRANT SELECT, INSERT, UPDATE ON
    bauer_rag_v4.source_documents,
    bauer_rag_v4.source_versions,
    bauer_rag_v4.release_sources,
    bauer_rag_v4.canonical_documents,
    bauer_rag_v4.canonical_blocks,
    bauer_rag_v4.canonical_tables,
    bauer_rag_v4.canonical_cells,
    bauer_rag_v4.canonical_facts,
    bauer_rag_v4.search_projections,
    bauer_rag_v4.embedding_cache,
    bauer_rag_v4.compilation_jobs
TO bauer_rag_v4_worker;

GRANT INSERT, SELECT ON bauer_rag_v4.evaluation_runs
TO bauer_rag_v4_evaluator;
GRANT INSERT, SELECT ON bauer_rag_v4.review_attestations
TO bauer_rag_v4_reviewer;

GRANT SELECT, INSERT, UPDATE ON
    bauer_rag_v4.tenants,
    bauer_rag_v4.knowledge_bases,
    bauer_rag_v4.principals,
    bauer_rag_v4.principal_source_grants,
    bauer_rag_v4.source_documents,
    bauer_rag_v4.knowledge_releases,
    bauer_rag_v4.active_release_pointers
TO bauer_rag_v4_admin;
GRANT SELECT ON
    bauer_rag_v4.source_versions,
    bauer_rag_v4.release_sources,
    bauer_rag_v4.authorization_audit,
    bauer_rag_v4.evaluation_runs,
    bauer_rag_v4.review_attestations
TO bauer_rag_v4_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bauer_rag_v4
TO bauer_rag_v4_reader, bauer_rag_v4_worker, bauer_rag_v4_evaluator,
   bauer_rag_v4_reviewer, bauer_rag_v4_admin;

GRANT EXECUTE ON FUNCTION bauer_rag_v4.current_tenant_id() TO
    bauer_rag_v4_reader, bauer_rag_v4_worker, bauer_rag_v4_evaluator,
    bauer_rag_v4_reviewer, bauer_rag_v4_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.current_knowledge_base_id() TO
    bauer_rag_v4_reader, bauer_rag_v4_worker, bauer_rag_v4_evaluator,
    bauer_rag_v4_reviewer, bauer_rag_v4_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.current_principal_id() TO
    bauer_rag_v4_reader, bauer_rag_v4_worker, bauer_rag_v4_evaluator,
    bauer_rag_v4_reviewer, bauer_rag_v4_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.current_release_id() TO
    bauer_rag_v4_reader, bauer_rag_v4_worker, bauer_rag_v4_evaluator,
    bauer_rag_v4_reviewer, bauer_rag_v4_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.requested_source_ids() TO
    bauer_rag_v4_reader, bauer_rag_v4_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.readable_source_ids() TO
    bauer_rag_v4_reader, bauer_rag_v4_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.can_read_source(uuid) TO
    bauer_rag_v4_reader, bauer_rag_v4_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.resolve_pinned_release() TO
    bauer_rag_v4_reader, bauer_rag_v4_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.claim_compilation_job(text, integer),
    bauer_rag_v4.fail_compilation_job(uuid, text, text, text),
    bauer_rag_v4.requeue_expired_jobs()
TO bauer_rag_v4_worker;
GRANT EXECUTE ON FUNCTION bauer_rag_v4.record_authorization_audit(
    text, text, text, integer
) TO bauer_rag_v4_reader;

ALTER TABLE bauer_rag_v4.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.knowledge_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.principal_source_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.source_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.source_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.knowledge_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.release_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.active_release_pointers ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.canonical_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.canonical_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.canonical_tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.canonical_cells ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.canonical_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.search_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.embedding_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.compilation_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.authorization_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.evaluation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v4.review_attestations ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_scope ON bauer_rag_v4.tenants
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator,
        bauer_rag_v4_reviewer, bauer_rag_v4_admin
    USING (tenant_id = bauer_rag_v4.current_tenant_id());
CREATE POLICY tenant_admin_scope ON bauer_rag_v4.tenants
    TO bauer_rag_v4_admin
    USING (tenant_id = bauer_rag_v4.current_tenant_id())
    WITH CHECK (tenant_id = bauer_rag_v4.current_tenant_id());
CREATE POLICY kb_scope ON bauer_rag_v4.knowledge_bases
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator,
        bauer_rag_v4_reviewer, bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY kb_admin_scope ON bauer_rag_v4.knowledge_bases
    TO bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY principal_admin_scope ON bauer_rag_v4.principals
    TO bauer_rag_v4_admin
    USING (tenant_id = bauer_rag_v4.current_tenant_id())
    WITH CHECK (tenant_id = bauer_rag_v4.current_tenant_id());
CREATE POLICY grants_admin_scope ON bauer_rag_v4.principal_source_grants
    TO bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY source_read_scope ON bauer_rag_v4.source_documents
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
        AND bauer_rag_v4.can_read_source(source_id)
    );
CREATE POLICY source_worker_scope ON bauer_rag_v4.source_documents
    TO bauer_rag_v4_worker
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY source_admin_scope ON bauer_rag_v4.source_documents
    TO bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );

CREATE POLICY version_read_scope ON bauer_rag_v4.source_versions
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
        AND bauer_rag_v4.can_read_source(source_id)
    );
CREATE POLICY version_worker_scope ON bauer_rag_v4.source_versions
    TO bauer_rag_v4_worker
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );

CREATE POLICY release_pinned_scope ON bauer_rag_v4.knowledge_releases
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
        AND release_id = bauer_rag_v4.current_release_id()
        AND status = 'ready'
    );
CREATE POLICY release_reviewer_scope ON bauer_rag_v4.knowledge_releases
    FOR SELECT TO bauer_rag_v4_reviewer
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY release_admin_scope ON bauer_rag_v4.knowledge_releases
    TO bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY release_source_read_scope ON bauer_rag_v4.release_sources
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
        AND release_id = bauer_rag_v4.current_release_id()
        AND bauer_rag_v4.can_read_source(source_id)
    );
CREATE POLICY release_source_worker_scope ON bauer_rag_v4.release_sources
    TO bauer_rag_v4_worker
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY active_pointer_admin_scope ON bauer_rag_v4.active_release_pointers
    TO bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );

CREATE POLICY canonical_document_read_scope ON bauer_rag_v4.canonical_documents
    FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
        AND release_id = bauer_rag_v4.current_release_id()
        AND bauer_rag_v4.can_read_source(source_id)
    );
CREATE POLICY canonical_document_worker_scope ON bauer_rag_v4.canonical_documents
    TO bauer_rag_v4_worker
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'canonical_blocks',
        'canonical_tables',
        'canonical_cells',
        'canonical_facts',
        'search_projections'
    ]
    LOOP
        EXECUTE format(
            'CREATE POLICY %I ON bauer_rag_v4.%I FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator USING (
                tenant_id = bauer_rag_v4.current_tenant_id()
                AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
                AND release_id = bauer_rag_v4.current_release_id()
                AND bauer_rag_v4.can_read_source(source_id)
            )',
            table_name || '_read_scope',
            table_name
        );
        EXECUTE format(
            'CREATE POLICY %I ON bauer_rag_v4.%I TO bauer_rag_v4_worker USING (
                tenant_id = bauer_rag_v4.current_tenant_id()
                AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
            ) WITH CHECK (
                tenant_id = bauer_rag_v4.current_tenant_id()
                AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
            )',
            table_name || '_worker_scope',
            table_name
        );
    END LOOP;
END
$$;

CREATE POLICY embedding_cache_worker_scope ON bauer_rag_v4.embedding_cache
    TO bauer_rag_v4_worker
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY job_worker_scope ON bauer_rag_v4.compilation_jobs
    TO bauer_rag_v4_worker
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY audit_admin_scope ON bauer_rag_v4.authorization_audit
    FOR SELECT TO bauer_rag_v4_admin
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
CREATE POLICY evaluation_scope ON bauer_rag_v4.evaluation_runs
    TO bauer_rag_v4_evaluator, bauer_rag_v4_reviewer
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
        AND split = 'development'
    );
CREATE POLICY review_scope ON bauer_rag_v4.review_attestations
    TO bauer_rag_v4_reviewer
    USING (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    )
    WITH CHECK (
        tenant_id = bauer_rag_v4.current_tenant_id()
        AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    );
