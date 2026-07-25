CREATE OR REPLACE FUNCTION bauer_rag_v3.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.current_principal_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(array_agg(value::uuid), ARRAY[]::uuid[])
    FROM jsonb_array_elements_text(
        coalesce(
            nullif(current_setting('app.principal_ids', true), '')::jsonb,
            '[]'::jsonb
        )
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_kb(target_kb_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.knowledge_bases kb
        JOIN bauer_rag_v3.kb_grants grant_row
          ON grant_row.kb_id = kb.kb_id
         AND grant_row.tenant_id = kb.tenant_id
        WHERE kb.kb_id = target_kb_id
          AND kb.tenant_id = bauer_rag_v3.current_tenant_id()
          AND grant_row.principal_id = ANY (
              bauer_rag_v3.current_principal_ids()
          )
          AND grant_row.permission IN ('read', 'ingest', 'admin')
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_ingest_kb(target_kb_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.knowledge_bases kb
        JOIN bauer_rag_v3.kb_grants grant_row
          ON grant_row.kb_id = kb.kb_id
         AND grant_row.tenant_id = kb.tenant_id
        WHERE kb.kb_id = target_kb_id
          AND kb.tenant_id = bauer_rag_v3.current_tenant_id()
          AND grant_row.principal_id = ANY (
              bauer_rag_v3.current_principal_ids()
          )
          AND grant_row.permission IN ('ingest', 'admin')
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_admin_kb(target_kb_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.knowledge_bases kb
        JOIN bauer_rag_v3.kb_grants grant_row
          ON grant_row.kb_id = kb.kb_id
         AND grant_row.tenant_id = kb.tenant_id
        WHERE kb.kb_id = target_kb_id
          AND kb.tenant_id = bauer_rag_v3.current_tenant_id()
          AND grant_row.principal_id = ANY (
              bauer_rag_v3.current_principal_ids()
          )
          AND grant_row.permission = 'admin'
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_admin_tenant(
    target_tenant_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT
        target_tenant_id = bauer_rag_v3.current_tenant_id()
        AND EXISTS (
            SELECT 1
            FROM bauer_rag_v3.kb_grants grant_row
            WHERE grant_row.tenant_id = target_tenant_id
              AND grant_row.principal_id = ANY (
                  bauer_rag_v3.current_principal_ids()
              )
              AND grant_row.permission = 'admin'
        )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_source(
    target_source_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.sources source_row
        WHERE source_row.source_id = target_source_id
          AND source_row.tenant_id = bauer_rag_v3.current_tenant_id()
          AND bauer_rag_v3.can_read_kb(source_row.kb_id)
          AND (
              source_row.visibility = 'inherited'
              OR EXISTS (
                  SELECT 1
                  FROM bauer_rag_v3.source_grants grant_row
                  WHERE grant_row.source_id = source_row.source_id
                    AND grant_row.tenant_id = source_row.tenant_id
                    AND grant_row.principal_id = ANY (
                        bauer_rag_v3.current_principal_ids()
                    )
                    AND grant_row.permission IN ('read', 'admin')
              )
          )
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_write_source(
    target_source_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.sources source_row
        WHERE source_row.source_id = target_source_id
          AND source_row.tenant_id = bauer_rag_v3.current_tenant_id()
          AND bauer_rag_v3.can_ingest_kb(source_row.kb_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_release(
    target_release_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.knowledge_releases release_row
        WHERE release_row.release_id = target_release_id
          AND bauer_rag_v3.can_read_kb(release_row.kb_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_write_release(
    target_release_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.knowledge_releases release_row
        WHERE release_row.release_id = target_release_id
          AND bauer_rag_v3.can_ingest_kb(release_row.kb_id)
          AND release_row.status IN ('building', 'validating')
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_evaluate_release(
    target_release_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.knowledge_releases release_row
        WHERE release_row.release_id = target_release_id
          AND bauer_rag_v3.can_admin_kb(release_row.kb_id)
          AND release_row.status IN ('validating', 'ready')
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_artifact(
    target_artifact_set_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.artifact_sets artifact
        WHERE artifact.artifact_set_id = target_artifact_set_id
          AND bauer_rag_v3.can_read_source(artifact.source_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_write_artifact(
    target_artifact_set_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.artifact_sets artifact
        WHERE artifact.artifact_set_id = target_artifact_set_id
          AND artifact.status = 'building'
          AND bauer_rag_v3.can_write_source(artifact.source_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_job(target_job_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.jobs job_row
        WHERE job_row.job_id = target_job_id
          AND bauer_rag_v3.can_read_kb(job_row.kb_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_write_job(target_job_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.jobs job_row
        WHERE job_row.job_id = target_job_id
          AND bauer_rag_v3.can_ingest_kb(job_row.kb_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_qa_check(
    target_qa_check_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.qa_checks check_row
        WHERE check_row.qa_check_id = target_qa_check_id
          AND bauer_rag_v3.can_read_kb(check_row.kb_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_write_qa_check(
    target_qa_check_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.qa_checks check_row
        WHERE check_row.qa_check_id = target_qa_check_id
          AND bauer_rag_v3.can_ingest_kb(check_row.kb_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_eval_run(
    target_eval_run_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.eval_runs run_row
        WHERE run_row.eval_run_id = target_eval_run_id
          AND bauer_rag_v3.can_read_release(run_row.release_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_write_eval_run(
    target_eval_run_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.eval_runs run_row
        WHERE run_row.eval_run_id = target_eval_run_id
          AND bauer_rag_v3.can_admin_kb(run_row.kb_id)
          AND bauer_rag_v3.can_evaluate_release(run_row.release_id)
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.can_read_nav_node(
    target_release_id uuid,
    target_nav_node_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.nav_nodes node_row
        WHERE node_row.release_id = target_release_id
          AND node_row.nav_node_id = target_nav_node_id
          AND bauer_rag_v3.can_read_release(node_row.release_id)
          AND (
              node_row.source_id IS NULL
              OR bauer_rag_v3.can_read_source(node_row.source_id)
          )
    )
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_release_status()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    IF NOT (
        (OLD.status = 'draft' AND NEW.status IN ('building', 'failed'))
        OR
        (
            OLD.status = 'building'
            AND NEW.status IN ('validating', 'failed')
        )
        OR
        (
            OLD.status = 'validating'
            AND NEW.status IN ('ready', 'failed')
        )
        OR
        (OLD.status = 'ready' AND NEW.status = 'retired')
    ) THEN
        RAISE EXCEPTION
            'invalid release transition: % -> %',
            OLD.status,
            NEW.status
            USING ERRCODE = '55000';
    END IF;

    IF OLD.status = 'ready'
       AND EXISTS (
           SELECT 1
           FROM bauer_rag_v3.active_releases active
           WHERE active.release_id = OLD.release_id
       ) THEN
        RAISE EXCEPTION
            'active release must be rolled back before retirement'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_release_status
BEFORE UPDATE OF status
ON bauer_rag_v3.knowledge_releases
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_release_status();

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_release_source_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    target_release_id uuid;
    target_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_release_id := OLD.release_id;
    ELSE
        target_release_id := NEW.release_id;
    END IF;
    SELECT status
    INTO target_status
    FROM bauer_rag_v3.knowledge_releases
    WHERE release_id = target_release_id
    FOR UPDATE;

    IF target_status <> 'building' THEN
        RAISE EXCEPTION
            'release membership is immutable when release is %',
            target_status
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_release_source_mutation
BEFORE INSERT OR UPDATE OR DELETE
ON bauer_rag_v3.release_sources
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_release_source_mutation();

ALTER TABLE bauer_rag_v3.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.knowledge_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.kb_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.source_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.source_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.knowledge_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.active_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.release_activations ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.release_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.artifact_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.sections ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.table_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.table_cells ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.provenance_spans ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.entity_mentions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.fact_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.search_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.exact_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.nav_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.nav_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.job_dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.job_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.qa_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.review_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.eval_suites ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.independent_gold_attestations
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.eval_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.eval_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bauer_rag_v3.eval_results ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON bauer_rag_v3.tenants
    FOR SELECT
    USING (tenant_id = bauer_rag_v3.current_tenant_id());
CREATE POLICY tenant_admin_write ON bauer_rag_v3.tenants
    FOR ALL
    USING (bauer_rag_v3.can_admin_tenant(tenant_id))
    WITH CHECK (tenant_id = bauer_rag_v3.current_tenant_id());

CREATE POLICY kb_read ON bauer_rag_v3.knowledge_bases
    FOR SELECT USING (bauer_rag_v3.can_read_kb(kb_id));
CREATE POLICY kb_admin_write ON bauer_rag_v3.knowledge_bases
    FOR ALL
    USING (bauer_rag_v3.can_admin_kb(kb_id))
    WITH CHECK (bauer_rag_v3.can_admin_tenant(tenant_id));

CREATE POLICY principal_self_read ON bauer_rag_v3.principals
    FOR SELECT
    USING (
        tenant_id = bauer_rag_v3.current_tenant_id()
        AND principal_id = ANY (bauer_rag_v3.current_principal_ids())
    );
CREATE POLICY principal_admin_write ON bauer_rag_v3.principals
    FOR ALL
    USING (bauer_rag_v3.can_admin_tenant(tenant_id))
    WITH CHECK (bauer_rag_v3.can_admin_tenant(tenant_id));

CREATE POLICY kb_grant_self_read ON bauer_rag_v3.kb_grants
    FOR SELECT
    USING (
        tenant_id = bauer_rag_v3.current_tenant_id()
        AND principal_id = ANY (bauer_rag_v3.current_principal_ids())
    );
CREATE POLICY kb_grant_admin_write ON bauer_rag_v3.kb_grants
    FOR ALL
    USING (bauer_rag_v3.can_admin_kb(kb_id))
    WITH CHECK (bauer_rag_v3.can_admin_kb(kb_id));

CREATE POLICY source_read ON bauer_rag_v3.sources
    FOR SELECT USING (bauer_rag_v3.can_read_source(source_id));
CREATE POLICY source_write ON bauer_rag_v3.sources
    FOR ALL
    USING (bauer_rag_v3.can_write_source(source_id))
    WITH CHECK (bauer_rag_v3.can_ingest_kb(kb_id));

CREATE POLICY source_grant_read ON bauer_rag_v3.source_grants
    FOR SELECT
    USING (
        principal_id = ANY (bauer_rag_v3.current_principal_ids())
        AND bauer_rag_v3.can_read_source(source_id)
    );
CREATE POLICY source_grant_admin_write ON bauer_rag_v3.source_grants
    FOR ALL
    USING (bauer_rag_v3.can_write_source(source_id))
    WITH CHECK (bauer_rag_v3.can_write_source(source_id));

CREATE POLICY source_version_read ON bauer_rag_v3.source_versions
    FOR SELECT USING (bauer_rag_v3.can_read_source(source_id));
CREATE POLICY source_version_write ON bauer_rag_v3.source_versions
    FOR ALL
    USING (bauer_rag_v3.can_write_source(source_id))
    WITH CHECK (bauer_rag_v3.can_write_source(source_id));

CREATE POLICY release_read ON bauer_rag_v3.knowledge_releases
    FOR SELECT USING (bauer_rag_v3.can_read_kb(kb_id));
CREATE POLICY release_write ON bauer_rag_v3.knowledge_releases
    FOR ALL
    USING (bauer_rag_v3.can_ingest_kb(kb_id))
    WITH CHECK (bauer_rag_v3.can_ingest_kb(kb_id));

CREATE POLICY active_release_read ON bauer_rag_v3.active_releases
    FOR SELECT USING (bauer_rag_v3.can_read_kb(kb_id));
CREATE POLICY release_activation_read ON bauer_rag_v3.release_activations
    FOR SELECT USING (bauer_rag_v3.can_admin_kb(kb_id));

CREATE POLICY release_source_read ON bauer_rag_v3.release_sources
    FOR SELECT
    USING (
        bauer_rag_v3.can_read_kb(kb_id)
        AND bauer_rag_v3.can_read_source(source_id)
    );
CREATE POLICY release_source_write ON bauer_rag_v3.release_sources
    FOR ALL
    USING (bauer_rag_v3.can_ingest_kb(kb_id))
    WITH CHECK (
        bauer_rag_v3.can_ingest_kb(kb_id)
        AND bauer_rag_v3.can_write_source(source_id)
    );

CREATE POLICY artifact_read ON bauer_rag_v3.artifact_sets
    FOR SELECT USING (bauer_rag_v3.can_read_source(source_id));
CREATE POLICY artifact_write ON bauer_rag_v3.artifact_sets
    FOR ALL
    USING (bauer_rag_v3.can_write_source(source_id))
    WITH CHECK (bauer_rag_v3.can_write_source(source_id));

CREATE POLICY page_access ON bauer_rag_v3.pages
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY page_write ON bauer_rag_v3.pages
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY section_access ON bauer_rag_v3.sections
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY section_write ON bauer_rag_v3.sections
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY block_access ON bauer_rag_v3.blocks
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY block_write ON bauer_rag_v3.blocks
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY table_access ON bauer_rag_v3.tables
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY table_write ON bauer_rag_v3.tables
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY table_segment_access ON bauer_rag_v3.table_segments
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY table_segment_write ON bauer_rag_v3.table_segments
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY table_cell_access ON bauer_rag_v3.table_cells
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY table_cell_write ON bauer_rag_v3.table_cells
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY provenance_access ON bauer_rag_v3.provenance_spans
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY provenance_write ON bauer_rag_v3.provenance_spans
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY entity_access ON bauer_rag_v3.entities
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY entity_write ON bauer_rag_v3.entities
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY entity_mention_access ON bauer_rag_v3.entity_mentions
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY entity_mention_write ON bauer_rag_v3.entity_mentions
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY fact_access ON bauer_rag_v3.facts
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY fact_write ON bauer_rag_v3.facts
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));
CREATE POLICY fact_provenance_access ON bauer_rag_v3.fact_provenance
    FOR SELECT
    USING (bauer_rag_v3.can_read_artifact(artifact_set_id));
CREATE POLICY fact_provenance_write ON bauer_rag_v3.fact_provenance
    FOR ALL
    USING (bauer_rag_v3.can_write_artifact(artifact_set_id))
    WITH CHECK (bauer_rag_v3.can_write_artifact(artifact_set_id));

CREATE POLICY search_unit_read ON bauer_rag_v3.search_units
    FOR SELECT
    USING (
        bauer_rag_v3.can_read_release(release_id)
        AND bauer_rag_v3.can_read_source(source_id)
    );
CREATE POLICY search_unit_write ON bauer_rag_v3.search_units
    FOR ALL
    USING (bauer_rag_v3.can_write_release(release_id))
    WITH CHECK (
        bauer_rag_v3.can_write_release(release_id)
        AND bauer_rag_v3.can_write_source(source_id)
    );

CREATE POLICY exact_term_read ON bauer_rag_v3.exact_terms
    FOR SELECT
    USING (
        bauer_rag_v3.can_read_release(release_id)
        AND bauer_rag_v3.can_read_source(source_id)
    );
CREATE POLICY exact_term_write ON bauer_rag_v3.exact_terms
    FOR ALL
    USING (bauer_rag_v3.can_write_release(release_id))
    WITH CHECK (
        bauer_rag_v3.can_write_release(release_id)
        AND bauer_rag_v3.can_write_source(source_id)
    );

CREATE POLICY nav_node_read ON bauer_rag_v3.nav_nodes
    FOR SELECT
    USING (
        bauer_rag_v3.can_read_release(release_id)
        AND (
            source_id IS NULL
            OR bauer_rag_v3.can_read_source(source_id)
        )
    );
CREATE POLICY nav_node_write ON bauer_rag_v3.nav_nodes
    FOR ALL
    USING (bauer_rag_v3.can_write_release(release_id))
    WITH CHECK (
        bauer_rag_v3.can_write_release(release_id)
        AND (
            source_id IS NULL
            OR bauer_rag_v3.can_write_source(source_id)
        )
    );
CREATE POLICY nav_edge_read ON bauer_rag_v3.nav_edges
    FOR SELECT
    USING (
        bauer_rag_v3.can_read_nav_node(release_id, from_node_id)
        AND bauer_rag_v3.can_read_nav_node(release_id, to_node_id)
    );
CREATE POLICY nav_edge_write ON bauer_rag_v3.nav_edges
    FOR ALL
    USING (bauer_rag_v3.can_write_release(release_id))
    WITH CHECK (bauer_rag_v3.can_write_release(release_id));

CREATE POLICY job_read ON bauer_rag_v3.jobs
    FOR SELECT USING (bauer_rag_v3.can_read_kb(kb_id));
CREATE POLICY job_write ON bauer_rag_v3.jobs
    FOR ALL
    USING (bauer_rag_v3.can_ingest_kb(kb_id))
    WITH CHECK (bauer_rag_v3.can_ingest_kb(kb_id));
CREATE POLICY job_dependency_access ON bauer_rag_v3.job_dependencies
    FOR SELECT
    USING (
        bauer_rag_v3.can_read_job(job_id)
        AND bauer_rag_v3.can_read_job(depends_on_job_id)
    );
CREATE POLICY job_dependency_write ON bauer_rag_v3.job_dependencies
    FOR ALL
    USING (
        bauer_rag_v3.can_write_job(job_id)
        AND bauer_rag_v3.can_write_job(depends_on_job_id)
    )
    WITH CHECK (
        bauer_rag_v3.can_write_job(job_id)
        AND bauer_rag_v3.can_write_job(depends_on_job_id)
    );
CREATE POLICY job_attempt_access ON bauer_rag_v3.job_attempts
    FOR SELECT
    USING (bauer_rag_v3.can_read_job(job_id));
CREATE POLICY job_attempt_write ON bauer_rag_v3.job_attempts
    FOR ALL
    USING (bauer_rag_v3.can_write_job(job_id))
    WITH CHECK (bauer_rag_v3.can_write_job(job_id));
CREATE POLICY dead_letter_access ON bauer_rag_v3.dead_letters
    FOR SELECT
    USING (bauer_rag_v3.can_read_job(job_id));
CREATE POLICY dead_letter_write ON bauer_rag_v3.dead_letters
    FOR ALL
    USING (bauer_rag_v3.can_write_job(job_id))
    WITH CHECK (bauer_rag_v3.can_write_job(job_id));
CREATE POLICY job_event_access ON bauer_rag_v3.job_events
    FOR SELECT
    USING (bauer_rag_v3.can_read_job(job_id));
CREATE POLICY job_event_write ON bauer_rag_v3.job_events
    FOR ALL
    USING (bauer_rag_v3.can_write_job(job_id))
    WITH CHECK (bauer_rag_v3.can_write_job(job_id));

CREATE POLICY qa_check_read ON bauer_rag_v3.qa_checks
    FOR SELECT USING (bauer_rag_v3.can_read_kb(kb_id));
CREATE POLICY qa_check_write ON bauer_rag_v3.qa_checks
    FOR ALL
    USING (bauer_rag_v3.can_ingest_kb(kb_id))
    WITH CHECK (bauer_rag_v3.can_ingest_kb(kb_id));
CREATE POLICY review_decision_read ON bauer_rag_v3.review_decisions
    FOR SELECT USING (bauer_rag_v3.can_read_qa_check(qa_check_id));
CREATE POLICY review_decision_write ON bauer_rag_v3.review_decisions
    FOR ALL
    USING (bauer_rag_v3.can_write_qa_check(qa_check_id))
    WITH CHECK (bauer_rag_v3.can_write_qa_check(qa_check_id));

CREATE POLICY eval_suite_read ON bauer_rag_v3.eval_suites
    FOR SELECT
    USING (tenant_id = bauer_rag_v3.current_tenant_id());
CREATE POLICY eval_suite_write ON bauer_rag_v3.eval_suites
    FOR ALL
    USING (bauer_rag_v3.can_admin_tenant(tenant_id))
    WITH CHECK (bauer_rag_v3.can_admin_tenant(tenant_id));
CREATE POLICY independent_gold_attestation_read
ON bauer_rag_v3.independent_gold_attestations
FOR SELECT
USING (tenant_id = bauer_rag_v3.current_tenant_id());
CREATE POLICY eval_case_read ON bauer_rag_v3.eval_cases
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM bauer_rag_v3.eval_suites suite
            WHERE suite.eval_suite_id = eval_cases.eval_suite_id
              AND suite.tenant_id = bauer_rag_v3.current_tenant_id()
        )
    );
CREATE POLICY eval_case_write ON bauer_rag_v3.eval_cases
    FOR ALL
    USING (
        EXISTS (
            SELECT 1
            FROM bauer_rag_v3.eval_suites suite
            WHERE suite.eval_suite_id = eval_cases.eval_suite_id
              AND bauer_rag_v3.can_admin_tenant(suite.tenant_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM bauer_rag_v3.eval_suites suite
            WHERE suite.eval_suite_id = eval_cases.eval_suite_id
              AND bauer_rag_v3.can_admin_tenant(suite.tenant_id)
        )
    );
CREATE POLICY eval_run_read ON bauer_rag_v3.eval_runs
    FOR SELECT USING (bauer_rag_v3.can_read_release(release_id));
CREATE POLICY eval_run_write ON bauer_rag_v3.eval_runs
    FOR ALL
    USING (bauer_rag_v3.can_evaluate_release(release_id))
    WITH CHECK (bauer_rag_v3.can_evaluate_release(release_id));
CREATE POLICY eval_result_read ON bauer_rag_v3.eval_results
    FOR SELECT USING (bauer_rag_v3.can_read_eval_run(eval_run_id));
CREATE POLICY eval_result_write ON bauer_rag_v3.eval_results
    FOR ALL
    USING (bauer_rag_v3.can_write_eval_run(eval_run_id))
    WITH CHECK (bauer_rag_v3.can_write_eval_run(eval_run_id));

CREATE OR REPLACE FUNCTION bauer_rag_v3.activate_release(
    target_kb_id uuid,
    target_release_id uuid,
    actor_principal_id uuid,
    activation_reason text,
    production_activation boolean DEFAULT true
)
RETURNS TABLE (
    previous_release_id uuid,
    active_release_id uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    release_row bauer_rag_v3.knowledge_releases%ROWTYPE;
    prior_release_id uuid;
    actual_source_count integer;
BEGIN
    IF NOT bauer_rag_v3.can_admin_kb(target_kb_id) THEN
        RAISE EXCEPTION 'release activation is not authorized'
            USING ERRCODE = '42501';
    END IF;

    IF actor_principal_id IS NULL
       OR NOT (
           actor_principal_id = ANY (
               bauer_rag_v3.current_principal_ids()
           )
       ) THEN
        RAISE EXCEPTION 'actor principal is not in the request context'
            USING ERRCODE = '42501';
    END IF;

    IF activation_reason IS NULL OR btrim(activation_reason) = '' THEN
        RAISE EXCEPTION 'release activation requires a reason'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(target_kb_id::text, 0)
    );

    SELECT *
    INTO release_row
    FROM bauer_rag_v3.knowledge_releases
    WHERE kb_id = target_kb_id
      AND release_id = target_release_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown release for knowledge base'
            USING ERRCODE = '22023';
    END IF;

    IF release_row.status <> 'ready' THEN
        RAISE EXCEPTION 'release is %, not ready', release_row.status
            USING ERRCODE = '55000';
    END IF;

    SELECT count(*)
    INTO actual_source_count
    FROM bauer_rag_v3.release_sources
    WHERE release_id = target_release_id;

    IF actual_source_count <> release_row.expected_source_count THEN
        RAISE EXCEPTION
            'release has % sources; expected %',
            actual_source_count,
            release_row.expected_source_count
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.jobs job_row
        WHERE job_row.release_id = target_release_id
          AND job_row.state <> 'succeeded'
    ) THEN
        RAISE EXCEPTION 'release has unfinished or failed jobs'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.release_sources member
        JOIN bauer_rag_v3.artifact_sets artifact
          ON artifact.artifact_set_id = member.artifact_set_id
        WHERE member.release_id = target_release_id
          AND artifact.status <> 'valid'
    ) THEN
        RAISE EXCEPTION 'release contains a non-valid artifact set'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.release_sources member
        WHERE member.release_id = target_release_id
          AND NOT EXISTS (
              SELECT 1
              FROM bauer_rag_v3.search_units unit
              WHERE unit.release_id = member.release_id
                AND unit.source_id = member.source_id
                AND unit.is_citable
          )
    ) THEN
        RAISE EXCEPTION
            'release contains a source without citable search evidence'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.qa_checks check_row
        WHERE check_row.release_id = target_release_id
          AND check_row.severity = 'blocker'
          AND check_row.status = 'fail'
          AND check_row.resolved_at IS NULL
    ) THEN
        RAISE EXCEPTION 'release has unresolved blocking QA checks'
            USING ERRCODE = '55000';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.eval_runs eval_run
        JOIN bauer_rag_v3.eval_suites suite
          ON suite.eval_suite_id = eval_run.eval_suite_id
        WHERE eval_run.release_id = target_release_id
          AND eval_run.status = 'succeeded'
          AND eval_run.passed
          AND cardinality(eval_run.hard_failures) = 0
          AND (
              (
                  production_activation
                  AND suite.gold_status = 'independent_bauer_verified'
                  AND EXISTS (
                      SELECT 1
                      FROM bauer_rag_v3.independent_gold_attestations
                           AS attestation
                      WHERE attestation.tenant_id = suite.tenant_id
                        AND attestation.eval_suite_id =
                            suite.eval_suite_id
                        AND attestation.manifest_sha256 =
                            suite.manifest_sha256
                        AND attestation.decision = 'approve'
                  )
              )
              OR
              (
                  NOT production_activation
                  AND suite.gold_status IN (
                      'verified',
                      'independent_bauer_verified'
                  )
              )
          )
    ) THEN
        RAISE EXCEPTION
            'release has no passing evaluation against verified gold'
            USING ERRCODE = '55000';
    END IF;

    SELECT active.release_id
    INTO prior_release_id
    FROM bauer_rag_v3.active_releases active
    WHERE active.kb_id = target_kb_id
    FOR UPDATE;

    IF prior_release_id = target_release_id THEN
        RAISE EXCEPTION 'release is already active'
            USING ERRCODE = '55000';
    END IF;

    INSERT INTO bauer_rag_v3.active_releases (
        kb_id,
        release_id,
        activated_at
    )
    VALUES (target_kb_id, target_release_id, now())
    ON CONFLICT (kb_id)
    DO UPDATE
       SET release_id = EXCLUDED.release_id,
           activated_at = EXCLUDED.activated_at;

    INSERT INTO bauer_rag_v3.release_activations (
        kb_id,
        release_id,
        previous_release_id,
        actor_principal_id,
        reason,
        production
    )
    VALUES (
        target_kb_id,
        target_release_id,
        prior_release_id,
        actor_principal_id,
        activation_reason,
        production_activation
    );

    RETURN QUERY
    SELECT prior_release_id, target_release_id;
END
$$;
