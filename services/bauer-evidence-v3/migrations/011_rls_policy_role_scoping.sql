-- Scope every RLS policy to the runtime group roles that can use the
-- corresponding table capability.
--
-- Migration 010 intentionally revoked broad function and object grants, but
-- the policies created before runtime roles existed still targeted PUBLIC.
-- PostgreSQL applies a permissive FOR ALL policy to SELECT as well as writes.
-- A reader query could therefore attempt a write/admin predicate for which the
-- reader correctly had no EXECUTE privilege and fail with SQLSTATE 42501.
--
-- Role-scoping the policies preserves the least-privilege function matrix:
-- readers never need write/admin predicates, while inherited group membership
-- keeps the ingester and admin serving paths intact.

-- Control-plane registry and ACL relations.
ALTER POLICY tenant_isolation
    ON bauer_rag_v3.tenants
    TO bauer_rag_v3_admin;
ALTER POLICY tenant_admin_write
    ON bauer_rag_v3.tenants
    TO bauer_rag_v3_admin;

ALTER POLICY kb_read
    ON bauer_rag_v3.knowledge_bases
    TO bauer_rag_v3_reader;
ALTER POLICY kb_admin_write
    ON bauer_rag_v3.knowledge_bases
    TO bauer_rag_v3_admin;

ALTER POLICY principal_self_read
    ON bauer_rag_v3.principals
    TO bauer_rag_v3_admin;
ALTER POLICY principal_admin_write
    ON bauer_rag_v3.principals
    TO bauer_rag_v3_admin;

ALTER POLICY kb_grant_self_read
    ON bauer_rag_v3.kb_grants
    TO bauer_rag_v3_admin;
ALTER POLICY kb_grant_admin_write
    ON bauer_rag_v3.kb_grants
    TO bauer_rag_v3_admin;

ALTER POLICY source_grant_read
    ON bauer_rag_v3.source_grants
    TO bauer_rag_v3_admin;
ALTER POLICY source_grant_admin_write
    ON bauer_rag_v3.source_grants
    TO bauer_rag_v3_admin;

-- Serving registry and canonical evidence.
ALTER POLICY source_read
    ON bauer_rag_v3.sources
    TO bauer_rag_v3_reader;
ALTER POLICY source_write
    ON bauer_rag_v3.sources
    TO bauer_rag_v3_ingester;

ALTER POLICY source_version_read
    ON bauer_rag_v3.source_versions
    TO bauer_rag_v3_reader;
ALTER POLICY source_version_write
    ON bauer_rag_v3.source_versions
    TO bauer_rag_v3_ingester;

ALTER POLICY release_read
    ON bauer_rag_v3.knowledge_releases
    TO bauer_rag_v3_reader;
ALTER POLICY release_write
    ON bauer_rag_v3.knowledge_releases
    TO bauer_rag_v3_admin;

ALTER POLICY active_release_read
    ON bauer_rag_v3.active_releases
    TO bauer_rag_v3_reader;
ALTER POLICY release_activation_read
    ON bauer_rag_v3.release_activations
    TO bauer_rag_v3_admin;

ALTER POLICY release_source_read
    ON bauer_rag_v3.release_sources
    TO bauer_rag_v3_reader;
ALTER POLICY release_source_write
    ON bauer_rag_v3.release_sources
    TO bauer_rag_v3_ingester;

ALTER POLICY artifact_read
    ON bauer_rag_v3.artifact_sets
    TO bauer_rag_v3_reader;
ALTER POLICY artifact_write
    ON bauer_rag_v3.artifact_sets
    TO bauer_rag_v3_ingester;

ALTER POLICY page_access
    ON bauer_rag_v3.pages
    TO bauer_rag_v3_reader;
ALTER POLICY page_write
    ON bauer_rag_v3.pages
    TO bauer_rag_v3_ingester;
ALTER POLICY section_access
    ON bauer_rag_v3.sections
    TO bauer_rag_v3_reader;
ALTER POLICY section_write
    ON bauer_rag_v3.sections
    TO bauer_rag_v3_ingester;
ALTER POLICY block_access
    ON bauer_rag_v3.blocks
    TO bauer_rag_v3_reader;
ALTER POLICY block_write
    ON bauer_rag_v3.blocks
    TO bauer_rag_v3_ingester;
ALTER POLICY table_access
    ON bauer_rag_v3.tables
    TO bauer_rag_v3_reader;
ALTER POLICY table_write
    ON bauer_rag_v3.tables
    TO bauer_rag_v3_ingester;
ALTER POLICY table_segment_access
    ON bauer_rag_v3.table_segments
    TO bauer_rag_v3_reader;
ALTER POLICY table_segment_write
    ON bauer_rag_v3.table_segments
    TO bauer_rag_v3_ingester;
ALTER POLICY table_cell_access
    ON bauer_rag_v3.table_cells
    TO bauer_rag_v3_reader;
ALTER POLICY table_cell_write
    ON bauer_rag_v3.table_cells
    TO bauer_rag_v3_ingester;
ALTER POLICY provenance_access
    ON bauer_rag_v3.provenance_spans
    TO bauer_rag_v3_reader;
ALTER POLICY provenance_write
    ON bauer_rag_v3.provenance_spans
    TO bauer_rag_v3_ingester;
ALTER POLICY entity_access
    ON bauer_rag_v3.entities
    TO bauer_rag_v3_reader;
ALTER POLICY entity_write
    ON bauer_rag_v3.entities
    TO bauer_rag_v3_ingester;
ALTER POLICY entity_mention_access
    ON bauer_rag_v3.entity_mentions
    TO bauer_rag_v3_reader;
ALTER POLICY entity_mention_write
    ON bauer_rag_v3.entity_mentions
    TO bauer_rag_v3_ingester;
ALTER POLICY fact_access
    ON bauer_rag_v3.facts
    TO bauer_rag_v3_reader;
ALTER POLICY fact_write
    ON bauer_rag_v3.facts
    TO bauer_rag_v3_ingester;
ALTER POLICY fact_provenance_access
    ON bauer_rag_v3.fact_provenance
    TO bauer_rag_v3_reader;
ALTER POLICY fact_provenance_write
    ON bauer_rag_v3.fact_provenance
    TO bauer_rag_v3_ingester;

-- Release-owned search and navigation projections.
ALTER POLICY search_unit_read
    ON bauer_rag_v3.search_units
    TO bauer_rag_v3_reader;
ALTER POLICY search_unit_write
    ON bauer_rag_v3.search_units
    TO bauer_rag_v3_ingester;
ALTER POLICY exact_term_read
    ON bauer_rag_v3.exact_terms
    TO bauer_rag_v3_reader;
ALTER POLICY exact_term_write
    ON bauer_rag_v3.exact_terms
    TO bauer_rag_v3_ingester;
ALTER POLICY nav_node_read
    ON bauer_rag_v3.nav_nodes
    TO bauer_rag_v3_reader;
ALTER POLICY nav_node_write
    ON bauer_rag_v3.nav_nodes
    TO bauer_rag_v3_ingester;
ALTER POLICY nav_edge_read
    ON bauer_rag_v3.nav_edges
    TO bauer_rag_v3_reader;
ALTER POLICY nav_edge_write
    ON bauer_rag_v3.nav_edges
    TO bauer_rag_v3_ingester;

-- Durable compilation queue and compiler QA.
ALTER POLICY job_read
    ON bauer_rag_v3.jobs
    TO bauer_rag_v3_ingester;
ALTER POLICY job_write
    ON bauer_rag_v3.jobs
    TO bauer_rag_v3_ingester;
ALTER POLICY job_dependency_access
    ON bauer_rag_v3.job_dependencies
    TO bauer_rag_v3_ingester;
ALTER POLICY job_dependency_write
    ON bauer_rag_v3.job_dependencies
    TO bauer_rag_v3_ingester;
ALTER POLICY job_attempt_access
    ON bauer_rag_v3.job_attempts
    TO bauer_rag_v3_ingester;
ALTER POLICY job_attempt_write
    ON bauer_rag_v3.job_attempts
    TO bauer_rag_v3_ingester;
ALTER POLICY dead_letter_access
    ON bauer_rag_v3.dead_letters
    TO bauer_rag_v3_ingester;
ALTER POLICY dead_letter_write
    ON bauer_rag_v3.dead_letters
    TO bauer_rag_v3_ingester;
ALTER POLICY job_event_access
    ON bauer_rag_v3.job_events
    TO bauer_rag_v3_ingester;
ALTER POLICY job_event_write
    ON bauer_rag_v3.job_events
    TO bauer_rag_v3_ingester;
ALTER POLICY qa_check_read
    ON bauer_rag_v3.qa_checks
    TO bauer_rag_v3_ingester;
ALTER POLICY qa_check_write
    ON bauer_rag_v3.qa_checks
    TO bauer_rag_v3_ingester;

-- Evaluation persistence is independent of release control.
ALTER POLICY review_decision_read
    ON bauer_rag_v3.review_decisions
    TO bauer_rag_v3_evaluator, bauer_rag_v3_admin;
ALTER POLICY review_decision_write
    ON bauer_rag_v3.review_decisions
    TO bauer_rag_v3_evaluator;

ALTER POLICY eval_suite_read
    ON bauer_rag_v3.eval_suites
    TO bauer_rag_v3_evaluator, bauer_rag_v3_admin;
ALTER POLICY eval_suite_write
    ON bauer_rag_v3.eval_suites
    TO bauer_rag_v3_evaluator;

ALTER POLICY eval_case_read
    ON bauer_rag_v3.eval_cases
    TO bauer_rag_v3_evaluator, bauer_rag_v3_admin;
ALTER POLICY eval_case_write
    ON bauer_rag_v3.eval_cases
    TO bauer_rag_v3_evaluator;

ALTER POLICY eval_run_read
    ON bauer_rag_v3.eval_runs
    TO bauer_rag_v3_evaluator, bauer_rag_v3_admin;
ALTER POLICY eval_run_write
    ON bauer_rag_v3.eval_runs
    TO bauer_rag_v3_evaluator;

ALTER POLICY eval_result_read
    ON bauer_rag_v3.eval_results
    TO bauer_rag_v3_evaluator, bauer_rag_v3_admin;
ALTER POLICY eval_result_write
    ON bauer_rag_v3.eval_results
    TO bauer_rag_v3_evaluator;

-- Independent review and authorization audit.
ALTER POLICY independent_gold_attestation_read
    ON bauer_rag_v3.independent_gold_attestations
    TO bauer_rag_v3_reviewer, bauer_rag_v3_admin;

ALTER POLICY authorization_audit_admin_read
    ON bauer_rag_v3.authorization_audit
    TO bauer_rag_v3_admin;
ALTER POLICY authorization_audit_admin_insert
    ON bauer_rag_v3.authorization_audit
    TO bauer_rag_v3_admin;
