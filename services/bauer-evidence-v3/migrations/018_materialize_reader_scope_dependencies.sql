-- Evaluate nested reader scope helpers once per helper invocation.
--
-- PostgreSQL may otherwise execute a STABLE function used by ANY(...) once
-- for each candidate row.  That made readable_artifact_set_ids() repeatedly
-- rebuild the same 373-source ACL array while scanning artifact sets.  The
-- materialized scope relations below preserve the exact ACL semantics while
-- making each dependent scope an explicit one-time input.

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_source_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    WITH readable_kbs AS MATERIALIZED (
        SELECT unnest(bauer_rag_v3.readable_kb_ids()) AS kb_id
    )
    SELECT coalesce(
        array_agg(source_row.source_id ORDER BY source_row.source_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.sources source_row
    JOIN readable_kbs
      ON readable_kbs.kb_id = source_row.kb_id
    WHERE source_row.tenant_id = bauer_rag_v3.current_tenant_id()
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
    WITH readable_kbs AS MATERIALIZED (
        SELECT unnest(bauer_rag_v3.readable_kb_ids()) AS kb_id
    )
    SELECT coalesce(
        array_agg(release_row.release_id ORDER BY release_row.release_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.knowledge_releases release_row
    JOIN readable_kbs
      ON readable_kbs.kb_id = release_row.kb_id
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_artifact_set_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    WITH readable_sources AS MATERIALIZED (
        SELECT unnest(bauer_rag_v3.readable_source_ids()) AS source_id
    )
    SELECT coalesce(
        array_agg(
            artifact.artifact_set_id
            ORDER BY artifact.artifact_set_id
        ),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.artifact_sets artifact
    JOIN readable_sources
      ON readable_sources.source_id = artifact.source_id
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.readable_nav_node_ids()
RETURNS uuid[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
    WITH readable_releases AS MATERIALIZED (
        SELECT unnest(bauer_rag_v3.readable_release_ids()) AS release_id
    ),
    readable_sources AS MATERIALIZED (
        SELECT unnest(bauer_rag_v3.readable_source_ids()) AS source_id
    )
    SELECT coalesce(
        array_agg(node_row.nav_node_id ORDER BY node_row.nav_node_id),
        ARRAY[]::uuid[]
    )
    FROM bauer_rag_v3.nav_nodes node_row
    JOIN readable_releases
      ON readable_releases.release_id = node_row.release_id
    LEFT JOIN readable_sources
      ON readable_sources.source_id = node_row.source_id
    WHERE node_row.source_id IS NULL
       OR readable_sources.source_id IS NOT NULL
$$;

REVOKE ALL ON FUNCTION bauer_rag_v3.readable_source_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_release_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_artifact_set_ids()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION bauer_rag_v3.readable_nav_node_ids()
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_source_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_release_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_artifact_set_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.readable_nav_node_ids()
    TO bauer_rag_v3_reader;
