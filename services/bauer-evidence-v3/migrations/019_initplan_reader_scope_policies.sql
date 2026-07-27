-- Force PostgreSQL to evaluate each stable reader scope once per statement.
--
-- A direct STABLE function call in an RLS expression can be evaluated for
-- every candidate row. On a full release that repeatedly rebuilt the same
-- 373-source ACL arrays and exhausted the serving statement timeout. Each
-- uncorrelated SELECT below becomes a one-time subplan: the scope is still
-- derived from the same SECURITY DEFINER ACL helpers, but it is computed once
-- and reused by the policy.
--
-- This migration changes evaluation shape only. It neither trusts the
-- request-scoped source list nor broadens any tenant, knowledge-base,
-- principal, visibility, grant, release, artifact, or navigation rule.

ALTER POLICY kb_read
    ON bauer_rag_v3.knowledge_bases
    USING (
        kb_id IN (
            SELECT unnest(bauer_rag_v3.readable_kb_ids())
        )
    );

ALTER POLICY source_read
    ON bauer_rag_v3.sources
    USING (
        source_id IN (
            SELECT unnest(bauer_rag_v3.readable_source_ids())
        )
    );

ALTER POLICY source_version_read
    ON bauer_rag_v3.source_versions
    USING (
        source_id IN (
            SELECT unnest(bauer_rag_v3.readable_source_ids())
        )
    );

ALTER POLICY release_read
    ON bauer_rag_v3.knowledge_releases
    USING (
        release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
    );

ALTER POLICY active_release_read
    ON bauer_rag_v3.active_releases
    USING (
        kb_id IN (
            SELECT unnest(bauer_rag_v3.readable_kb_ids())
        )
        AND release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
    );

ALTER POLICY release_source_read
    ON bauer_rag_v3.release_sources
    USING (
        kb_id IN (
            SELECT unnest(bauer_rag_v3.readable_kb_ids())
        )
        AND release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
        AND source_id IN (
            SELECT unnest(bauer_rag_v3.readable_source_ids())
        )
    );

ALTER POLICY artifact_read
    ON bauer_rag_v3.artifact_sets
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY page_access
    ON bauer_rag_v3.pages
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY section_access
    ON bauer_rag_v3.sections
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY block_access
    ON bauer_rag_v3.blocks
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY table_access
    ON bauer_rag_v3.tables
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY table_segment_access
    ON bauer_rag_v3.table_segments
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY table_cell_access
    ON bauer_rag_v3.table_cells
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY provenance_access
    ON bauer_rag_v3.provenance_spans
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY entity_access
    ON bauer_rag_v3.entities
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY entity_mention_access
    ON bauer_rag_v3.entity_mentions
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY fact_access
    ON bauer_rag_v3.facts
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY fact_provenance_access
    ON bauer_rag_v3.fact_provenance
    USING (
        artifact_set_id IN (
            SELECT unnest(
                bauer_rag_v3.readable_artifact_set_ids()
            )
        )
    );

ALTER POLICY search_unit_read
    ON bauer_rag_v3.search_units
    USING (
        release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
        AND source_id IN (
            SELECT unnest(bauer_rag_v3.readable_source_ids())
        )
    );

ALTER POLICY exact_term_read
    ON bauer_rag_v3.exact_terms
    USING (
        release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
        AND source_id IN (
            SELECT unnest(bauer_rag_v3.readable_source_ids())
        )
    );

ALTER POLICY nav_node_read
    ON bauer_rag_v3.nav_nodes
    USING (
        release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
        AND nav_node_id IN (
            SELECT unnest(bauer_rag_v3.readable_nav_node_ids())
        )
    );

ALTER POLICY nav_edge_read
    ON bauer_rag_v3.nav_edges
    USING (
        release_id IN (
            SELECT unnest(bauer_rag_v3.readable_release_ids())
        )
        AND from_node_id IN (
            SELECT unnest(bauer_rag_v3.readable_nav_node_ids())
        )
        AND to_node_id IN (
            SELECT unnest(bauer_rag_v3.readable_nav_node_ids())
        )
    );
