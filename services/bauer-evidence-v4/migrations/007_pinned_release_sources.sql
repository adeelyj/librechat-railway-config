CREATE FUNCTION bauer_rag_v4.resolve_pinned_release_sources()
RETURNS TABLE (
    source_id uuid,
    external_source_id text,
    ordinal integer
)
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
    SELECT source.source_id,
           source.external_source_id,
           member.ordinal
    FROM bauer_rag_v4.release_sources AS member
    JOIN bauer_rag_v4.source_documents AS source
      ON source.tenant_id = member.tenant_id
     AND source.knowledge_base_id = member.knowledge_base_id
     AND source.source_id = member.source_id
    JOIN bauer_rag_v4.knowledge_releases AS release
      ON release.tenant_id = member.tenant_id
     AND release.knowledge_base_id = member.knowledge_base_id
     AND release.release_id = member.release_id
    JOIN bauer_rag_v4.principal_source_grants AS grant_row
      ON grant_row.tenant_id = member.tenant_id
     AND grant_row.knowledge_base_id = member.knowledge_base_id
     AND grant_row.source_id = member.source_id
    WHERE member.tenant_id = bauer_rag_v4.current_tenant_id()
      AND member.knowledge_base_id =
          bauer_rag_v4.current_knowledge_base_id()
      AND member.release_id = bauer_rag_v4.current_release_id()
      AND release.status = 'ready'
      AND grant_row.principal_id = bauer_rag_v4.current_principal_id()
    ORDER BY member.ordinal
$$;

REVOKE ALL
ON FUNCTION bauer_rag_v4.resolve_pinned_release_sources()
FROM PUBLIC;

GRANT EXECUTE
ON FUNCTION bauer_rag_v4.resolve_pinned_release_sources()
TO bauer_rag_v4_reader, bauer_rag_v4_evaluator;
