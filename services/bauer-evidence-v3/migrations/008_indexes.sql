CREATE INDEX IF NOT EXISTS ix_v3_kb_tenant
    ON bauer_rag_v3.knowledge_bases (tenant_id, kb_id);
CREATE INDEX IF NOT EXISTS ix_v3_principal_subject
    ON bauer_rag_v3.principals (
        tenant_id,
        external_subject,
        principal_type
    );
CREATE INDEX IF NOT EXISTS ix_v3_kb_grant_principal
    ON bauer_rag_v3.kb_grants (
        tenant_id,
        principal_id,
        permission,
        kb_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_source_grant_principal
    ON bauer_rag_v3.source_grants (
        tenant_id,
        principal_id,
        permission,
        source_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_source_kb_type
    ON bauer_rag_v3.sources (kb_id, source_type, source_id);
CREATE INDEX IF NOT EXISTS ix_v3_source_restricted
    ON bauer_rag_v3.sources (kb_id, source_id)
    WHERE visibility = 'restricted';
CREATE INDEX IF NOT EXISTS ix_v3_source_version_hash
    ON bauer_rag_v3.source_versions (sha256, source_id);
CREATE INDEX IF NOT EXISTS ix_v3_source_version_date
    ON bauer_rag_v3.source_versions (
        source_id,
        publication_date DESC NULLS LAST
    );
CREATE INDEX IF NOT EXISTS ix_v3_release_lifecycle
    ON bauer_rag_v3.knowledge_releases (
        kb_id,
        status,
        created_at DESC
    );
CREATE INDEX IF NOT EXISTS ix_v3_release_activation_history
    ON bauer_rag_v3.release_activations (
        kb_id,
        activated_at DESC
    );

CREATE INDEX IF NOT EXISTS ix_v3_artifact_source_status
    ON bauer_rag_v3.artifact_sets (
        source_id,
        source_version_id,
        status
    );
CREATE INDEX IF NOT EXISTS ix_v3_release_source_artifact
    ON bauer_rag_v3.release_sources (
        release_id,
        artifact_set_id,
        source_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_release_source_version
    ON bauer_rag_v3.release_sources (
        release_id,
        source_version_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_page_artifact
    ON bauer_rag_v3.pages (artifact_set_id, page_number);
CREATE INDEX IF NOT EXISTS ix_v3_section_parent
    ON bauer_rag_v3.sections (
        artifact_set_id,
        parent_section_id,
        ordinal
    );
CREATE INDEX IF NOT EXISTS ix_v3_block_reading_order
    ON bauer_rag_v3.blocks (
        artifact_set_id,
        page_id,
        reading_order
    );
CREATE INDEX IF NOT EXISTS ix_v3_table_artifact
    ON bauer_rag_v3.tables (artifact_set_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_v3_table_segment_page
    ON bauer_rag_v3.table_segments (
        artifact_set_id,
        page_id,
        table_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_cell_table_row
    ON bauer_rag_v3.table_cells (
        artifact_set_id,
        table_id,
        row_index,
        column_index
    );
CREATE INDEX IF NOT EXISTS ix_v3_cell_numeric
    ON bauer_rag_v3.table_cells (
        artifact_set_id,
        unit_ucum,
        numeric_value
    )
    WHERE numeric_value IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_provenance_page
    ON bauer_rag_v3.provenance_spans (artifact_set_id, page_id)
    WHERE page_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_provenance_block
    ON bauer_rag_v3.provenance_spans (artifact_set_id, block_id)
    WHERE block_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_provenance_cell
    ON bauer_rag_v3.provenance_spans (artifact_set_id, cell_id)
    WHERE cell_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_entity_exact
    ON bauer_rag_v3.entities (
        artifact_set_id,
        entity_type,
        normalized_key
    );
CREATE INDEX IF NOT EXISTS ix_v3_entity_key_trgm
    ON bauer_rag_v3.entities
    USING gin (normalized_key gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_v3_fact_subject_predicate
    ON bauer_rag_v3.facts (
        artifact_set_id,
        subject_key,
        predicate
    );
CREATE INDEX IF NOT EXISTS ix_v3_fact_numeric
    ON bauer_rag_v3.facts (
        artifact_set_id,
        predicate,
        unit_ucum,
        numeric_value
    )
    WHERE value_kind = 'numeric'
      AND verification_status <> 'rejected';
CREATE INDEX IF NOT EXISTS ix_v3_fact_verified
    ON bauer_rag_v3.facts (
        artifact_set_id,
        predicate,
        verification_status
    )
    WHERE verification_status IN ('candidate', 'verified');

CREATE INDEX IF NOT EXISTS ix_v3_search_scope
    ON bauer_rag_v3.search_units (
        release_id,
        source_id,
        unit_type,
        search_unit_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_search_page
    ON bauer_rag_v3.search_units (
        release_id,
        source_version_id,
        page_start,
        page_end
    );
CREATE INDEX IF NOT EXISTS ix_v3_search_lexical
    ON bauer_rag_v3.search_units
    USING gin (search_vector);
CREATE INDEX IF NOT EXISTS ix_v3_search_trgm
    ON bauer_rag_v3.search_units
    USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_v3_search_embedding_hnsw
    ON bauer_rag_v3.search_units
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_exact_term_lookup
    ON bauer_rag_v3.exact_terms (
        release_id,
        term_type,
        normalized_term,
        source_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_exact_term_trgm
    ON bauer_rag_v3.exact_terms
    USING gin (normalized_term gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_v3_nav_node_source
    ON bauer_rag_v3.nav_nodes (
        release_id,
        source_id,
        node_type
    );
CREATE INDEX IF NOT EXISTS ix_v3_nav_edge_from
    ON bauer_rag_v3.nav_edges (
        release_id,
        from_node_id,
        relation_type
    );
CREATE INDEX IF NOT EXISTS ix_v3_nav_edge_to
    ON bauer_rag_v3.nav_edges (
        release_id,
        to_node_id,
        relation_type
    );

CREATE INDEX IF NOT EXISTS ix_v3_job_available
    ON bauer_rag_v3.jobs (
        queue_name,
        priority DESC,
        available_at,
        created_at,
        job_id
    )
    WHERE state IN ('queued', 'retry_wait');
CREATE INDEX IF NOT EXISTS ix_v3_job_expired_lease
    ON bauer_rag_v3.jobs (lease_expires_at, job_id)
    WHERE state = 'running';
CREATE INDEX IF NOT EXISTS ix_v3_job_release_state
    ON bauer_rag_v3.jobs (release_id, state, job_type)
    WHERE release_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_job_source_state
    ON bauer_rag_v3.jobs (source_version_id, state, job_type)
    WHERE source_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_job_dependency_reverse
    ON bauer_rag_v3.job_dependencies (
        depends_on_job_id,
        job_id
    );
CREATE INDEX IF NOT EXISTS ix_v3_job_attempt_history
    ON bauer_rag_v3.job_attempts (
        job_id,
        attempt_number DESC
    );
CREATE INDEX IF NOT EXISTS ix_v3_dead_letter_unreplayed
    ON bauer_rag_v3.dead_letters (failed_at, job_id)
    WHERE replayed_by_job_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_v3_job_event_history
    ON bauer_rag_v3.job_events (job_id, created_at);

CREATE INDEX IF NOT EXISTS ix_v3_qa_release_status
    ON bauer_rag_v3.qa_checks (
        release_id,
        severity,
        status
    )
    WHERE release_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_qa_unresolved_blocker
    ON bauer_rag_v3.qa_checks (release_id, qa_check_id)
    WHERE severity = 'blocker'
      AND status = 'fail'
      AND resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_v3_qa_artifact
    ON bauer_rag_v3.qa_checks (
        artifact_set_id,
        severity,
        status
    )
    WHERE artifact_set_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v3_review_check
    ON bauer_rag_v3.review_decisions (
        qa_check_id,
        decided_at DESC
    );
CREATE INDEX IF NOT EXISTS ix_v3_eval_case_suite
    ON bauer_rag_v3.eval_cases (
        eval_suite_id,
        split,
        category
    );
CREATE INDEX IF NOT EXISTS ix_v3_eval_run_release
    ON bauer_rag_v3.eval_runs (
        release_id,
        status,
        passed,
        completed_at DESC
    );
CREATE INDEX IF NOT EXISTS ix_v3_eval_result_run
    ON bauer_rag_v3.eval_results (
        eval_run_id,
        passed,
        eval_case_id
    );
