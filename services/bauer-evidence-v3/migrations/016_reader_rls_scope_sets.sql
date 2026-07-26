-- Cache reader authorization as stable, zero-argument scope sets.
--
-- The original per-row predicates recursively looked up the same KB grant,
-- source grant, release, and artifact ownership for every joined evidence row.
-- A full candidate contains enough search/provenance rows for those repeated
-- SECURITY DEFINER calls to exceed the serving statement timeout.  These
-- helpers preserve the same tenant/principal/visibility rules, but let
-- PostgreSQL evaluate each request scope once and use UUID-array membership in
-- the reader policies and compatibility predicates.

CREATE OR REPLACE FUNCTION bauer_rag_v3.current_knowledge_base_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(
        current_setting('app.knowledge_base_id', true),
        ''
    )::uuid
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_kb_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT coalesce(
        array_agg(DISTINCT kb.kb_id ORDER BY kb.kb_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.knowledge_bases kb
    JOIN bauer_rag_v3.kb_grants grant_row
      ON grant_row.kb_id = kb.kb_id
     AND grant_row.tenant_id = kb.tenant_id
    WHERE kb.tenant_id = bauer_rag_v3.current_tenant_id()
      AND (
          bauer_rag_v3.current_knowledge_base_id() IS NULL
          OR kb.kb_id = bauer_rag_v3.current_knowledge_base_id()
      )
      AND grant_row.principal_id = ANY (
          bauer_rag_v3.current_principal_ids()
      )
      AND grant_row.permission IN ('read', 'ingest', 'admin')
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_source_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT coalesce(
        array_agg(source_row.source_id ORDER BY source_row.source_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.sources source_row
    WHERE source_row.tenant_id = bauer_rag_v3.current_tenant_id()
      AND source_row.kb_id = ANY (bauer_rag_v3.readable_kb_ids())
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
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_release_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT coalesce(
        array_agg(release_row.release_id ORDER BY release_row.release_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.knowledge_releases release_row
    WHERE release_row.kb_id = ANY (bauer_rag_v3.readable_kb_ids())
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_artifact_set_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT coalesce(
        array_agg(
            artifact.artifact_set_id
            ORDER BY artifact.artifact_set_id
        ),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.artifact_sets artifact
    WHERE artifact.source_id = ANY (bauer_rag_v3.readable_source_ids())
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_nav_node_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    SELECT coalesce(
        array_agg(node_row.nav_node_id ORDER BY node_row.nav_node_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.nav_nodes node_row
    WHERE node_row.release_id = ANY (
        bauer_rag_v3.readable_release_ids()
    )
      AND (
          node_row.source_id IS NULL
          OR node_row.source_id = ANY (
              bauer_rag_v3.readable_source_ids()
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
    SELECT target_kb_id = ANY (bauer_rag_v3.readable_kb_ids())
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
    SELECT target_source_id = ANY (
        bauer_rag_v3.readable_source_ids()
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
    SELECT target_release_id = ANY (
        bauer_rag_v3.readable_release_ids()
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
    SELECT target_artifact_set_id = ANY (
        bauer_rag_v3.readable_artifact_set_ids()
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
    SELECT
        target_release_id = ANY (
            bauer_rag_v3.readable_release_ids()
        )
        AND target_nav_node_id = ANY (
            bauer_rag_v3.readable_nav_node_ids()
        )
$$;

-- Registry, source, release, and membership relations.
ALTER POLICY kb_read
    ON bauer_rag_v3.knowledge_bases
    USING (kb_id = ANY (bauer_rag_v3.readable_kb_ids()));
ALTER POLICY source_read
    ON bauer_rag_v3.sources
    USING (source_id = ANY (bauer_rag_v3.readable_source_ids()));
ALTER POLICY source_version_read
    ON bauer_rag_v3.source_versions
    USING (source_id = ANY (bauer_rag_v3.readable_source_ids()));
ALTER POLICY release_read
    ON bauer_rag_v3.knowledge_releases
    USING (release_id = ANY (bauer_rag_v3.readable_release_ids()));
ALTER POLICY active_release_read
    ON bauer_rag_v3.active_releases
    USING (
        kb_id = ANY (bauer_rag_v3.readable_kb_ids())
        AND release_id = ANY (bauer_rag_v3.readable_release_ids())
    );
ALTER POLICY release_source_read
    ON bauer_rag_v3.release_sources
    USING (
        kb_id = ANY (bauer_rag_v3.readable_kb_ids())
        AND release_id = ANY (bauer_rag_v3.readable_release_ids())
        AND source_id = ANY (bauer_rag_v3.readable_source_ids())
    );
ALTER POLICY artifact_read
    ON bauer_rag_v3.artifact_sets
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );

-- Canonical evidence relations all carry artifact_set_id.
ALTER POLICY page_access
    ON bauer_rag_v3.pages
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY section_access
    ON bauer_rag_v3.sections
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY block_access
    ON bauer_rag_v3.blocks
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY table_access
    ON bauer_rag_v3.tables
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY table_segment_access
    ON bauer_rag_v3.table_segments
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY table_cell_access
    ON bauer_rag_v3.table_cells
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY provenance_access
    ON bauer_rag_v3.provenance_spans
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY entity_access
    ON bauer_rag_v3.entities
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY entity_mention_access
    ON bauer_rag_v3.entity_mentions
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY fact_access
    ON bauer_rag_v3.facts
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );
ALTER POLICY fact_provenance_access
    ON bauer_rag_v3.fact_provenance
    USING (
        artifact_set_id = ANY (
            bauer_rag_v3.readable_artifact_set_ids()
        )
    );

-- Release-owned serving projections.
ALTER POLICY search_unit_read
    ON bauer_rag_v3.search_units
    USING (
        release_id = ANY (bauer_rag_v3.readable_release_ids())
        AND source_id = ANY (bauer_rag_v3.readable_source_ids())
    );
ALTER POLICY exact_term_read
    ON bauer_rag_v3.exact_terms
    USING (
        release_id = ANY (bauer_rag_v3.readable_release_ids())
        AND source_id = ANY (bauer_rag_v3.readable_source_ids())
    );
ALTER POLICY nav_node_read
    ON bauer_rag_v3.nav_nodes
    USING (
        release_id = ANY (bauer_rag_v3.readable_release_ids())
        AND nav_node_id = ANY (
            bauer_rag_v3.readable_nav_node_ids()
        )
    );
ALTER POLICY nav_edge_read
    ON bauer_rag_v3.nav_edges
    USING (
        release_id = ANY (bauer_rag_v3.readable_release_ids())
        AND from_node_id = ANY (
            bauer_rag_v3.readable_nav_node_ids()
        )
        AND to_node_id = ANY (
            bauer_rag_v3.readable_nav_node_ids()
        )
    );

REVOKE ALL ON FUNCTION bauer_rag_v3.current_knowledge_base_id()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_kb_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_source_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_release_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_artifact_set_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_nav_node_ids()
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION bauer_rag_v3.current_knowledge_base_id()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_kb_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_source_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_release_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_artifact_set_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_nav_node_ids()
    TO bauer_rag_v3_reader;
