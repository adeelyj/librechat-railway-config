-- Correct the intentionally broad bootstrap grants from migration 009.
--
-- This migration keeps the group-role hierarchy, but removes every inherited
-- object grant before rebuilding an explicit runtime capability boundary:
--
--   reader   - release-scoped serving and canonical evidence only
--   ingester - reader capabilities plus source compilation and durable jobs
--   evaluator- reader capabilities plus append-only review/evaluation writes
--   reviewer - reader capabilities plus one independent gold attestation path
--   admin    - control-plane, ACL, activation, and evaluation inspection
--
-- The authorization audit relation is not writable by the reader.  A narrow
-- SECURITY DEFINER function is the only reader write path and records only
-- identifiers and decision metadata, never prompts, answers, or evidence text.

ALTER ROLE bauer_rag_v3_reader
    WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
         NOREPLICATION NOBYPASSRLS INHERIT;
ALTER ROLE bauer_rag_v3_ingester
    WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
         NOREPLICATION NOBYPASSRLS INHERIT;
ALTER ROLE bauer_rag_v3_admin
    WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
         NOREPLICATION NOBYPASSRLS INHERIT;
ALTER ROLE bauer_rag_v3_evaluator
    WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
         NOREPLICATION NOBYPASSRLS INHERIT;
ALTER ROLE bauer_rag_v3_reviewer
    WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
         NOREPLICATION NOBYPASSRLS INHERIT;

-- Release-owned serving projections freeze as soon as validation starts.
-- QA records have their own policy and mutation guard below, so validators can
-- still record checks without reopening the evidence that was evaluated.
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
          AND release_row.status = 'building'
    )
$$;

-- A release UUID identifies one immutable manifest and toolchain forever.
-- Lifecycle columns remain mutable through the guarded transition path, but
-- neither a runtime admin nor an accidental upsert can rewrite provenance.
CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_release_specification()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    IF NEW.release_id IS DISTINCT FROM OLD.release_id
       OR NEW.kb_id IS DISTINCT FROM OLD.kb_id
       OR NEW.based_on_release_id IS DISTINCT FROM OLD.based_on_release_id
       OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
       OR NEW.expected_source_count IS DISTINCT FROM OLD.expected_source_count
       OR NEW.compiler_fingerprint IS DISTINCT FROM OLD.compiler_fingerprint
       OR NEW.parser_version IS DISTINCT FROM OLD.parser_version
       OR NEW.ocr_version IS DISTINCT FROM OLD.ocr_version
       OR NEW.fact_model_version IS DISTINCT FROM OLD.fact_model_version
       OR NEW.embedding_model_version
          IS DISTINCT FROM OLD.embedding_model_version
       OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
       OR NEW.metadata IS DISTINCT FROM OLD.metadata
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'immutable release specification fields cannot change'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_release_spec
BEFORE UPDATE
ON bauer_rag_v3.knowledge_releases
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_release_specification();

CREATE TABLE IF NOT EXISTS bauer_rag_v3.authorization_audit (
    authorization_audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    tenant_id uuid NOT NULL
        REFERENCES bauer_rag_v3.tenants(tenant_id) ON DELETE RESTRICT,
    actor_principal_ids uuid[] NOT NULL,
    database_role text NOT NULL,
    request_id text,
    action text NOT NULL,
    decision text NOT NULL
        CHECK (decision IN ('allow', 'deny', 'error')),
    reason_code text NOT NULL,
    target_kb_id uuid,
    target_release_id uuid,
    target_source_ids uuid[] NOT NULL DEFAULT '{}',
    CHECK (cardinality(actor_principal_ids) > 0),
    CHECK (array_position(actor_principal_ids, NULL) IS NULL),
    CHECK (cardinality(actor_principal_ids) <= 256),
    CHECK (array_position(target_source_ids, NULL) IS NULL),
    CHECK (cardinality(target_source_ids) <= 1024),
    CHECK (btrim(database_role) <> ''),
    CHECK (request_id IS NULL OR btrim(request_id) <> ''),
    CHECK (request_id IS NULL OR length(request_id) <= 256),
    CHECK (btrim(action) <> ''),
    CHECK (length(action) <= 128),
    CHECK (btrim(reason_code) <> ''),
    CHECK (length(reason_code) <= 128)
);

CREATE INDEX IF NOT EXISTS ix_v3_authorization_audit_tenant_time
    ON bauer_rag_v3.authorization_audit (
        tenant_id,
        recorded_at DESC,
        authorization_audit_id DESC
    );
CREATE INDEX IF NOT EXISTS ix_v3_authorization_audit_request
    ON bauer_rag_v3.authorization_audit (tenant_id, request_id)
    WHERE request_id IS NOT NULL;

ALTER TABLE bauer_rag_v3.authorization_audit
    ENABLE ROW LEVEL SECURITY;

CREATE POLICY authorization_audit_admin_read
ON bauer_rag_v3.authorization_audit
FOR SELECT
USING (bauer_rag_v3.can_admin_tenant(tenant_id));

CREATE POLICY authorization_audit_admin_insert
ON bauer_rag_v3.authorization_audit
FOR INSERT
WITH CHECK (bauer_rag_v3.can_admin_tenant(tenant_id));

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_authorization_audit_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'authorization audit rows are append-only'
        USING ERRCODE = '55000';
END
$$;

CREATE TRIGGER trg_v3_guard_authorization_audit_append
BEFORE UPDATE OR DELETE
ON bauer_rag_v3.authorization_audit
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_authorization_audit_append();

-- Runtime compiler credentials may retry immutable upserts, but cannot
-- rewrite the object/source identity that an already-published artifact
-- resolves through.
CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_immutable_runtime_registry()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    IF TG_TABLE_NAME = 'objects' THEN
        IF NEW.sha256 IS DISTINCT FROM OLD.sha256
           OR NEW.object_key IS DISTINCT FROM OLD.object_key
           OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
           OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
           OR NEW.etag IS DISTINCT FROM OLD.etag
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'immutable object registry fields cannot change'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION
            'immutable % registry row cannot change',
            TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_object_registry
BEFORE UPDATE ON bauer_rag_v3.objects
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_immutable_runtime_registry();

CREATE TRIGGER trg_v3_guard_source_registry
BEFORE UPDATE ON bauer_rag_v3.sources
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_immutable_runtime_registry();

CREATE TRIGGER trg_v3_guard_source_version_registry
BEFORE UPDATE ON bauer_rag_v3.source_versions
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_immutable_runtime_registry();

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_artifact_set_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    IF OLD.status <> 'building' THEN
        RAISE EXCEPTION
            'finalized artifact sets are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.artifact_set_id IS DISTINCT FROM OLD.artifact_set_id
       OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.source_version_id IS DISTINCT FROM OLD.source_version_id
       OR NEW.compiler_fingerprint IS DISTINCT FROM OLD.compiler_fingerprint
       OR NEW.parser_version IS DISTINCT FROM OLD.parser_version
       OR NEW.ocr_version IS DISTINCT FROM OLD.ocr_version
       OR NEW.fact_model_version IS DISTINCT FROM OLD.fact_model_version
       OR NEW.worker_job_id IS DISTINCT FROM OLD.worker_job_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'artifact identity and toolchain fields are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status NOT IN ('building', 'valid', 'quarantined', 'invalid') THEN
        RAISE EXCEPTION 'invalid artifact transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_artifact_set_transition
BEFORE UPDATE ON bauer_rag_v3.artifact_sets
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_artifact_set_transition();

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_compiler_qa_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    target_release_id uuid;
    target_status text;
BEGIN
    target_release_id := CASE
        WHEN TG_OP = 'DELETE' THEN OLD.release_id
        ELSE NEW.release_id
    END;
    SELECT status
    INTO target_status
    FROM bauer_rag_v3.knowledge_releases
    WHERE release_id = target_release_id
    FOR SHARE;
    IF target_status NOT IN ('building', 'validating') THEN
        RAISE EXCEPTION
            'compiler QA is immutable when release is %',
            coalesce(target_status, 'missing')
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_compiler_qa_mutation
BEFORE INSERT OR UPDATE OR DELETE
ON bauer_rag_v3.qa_checks
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_compiler_qa_mutation();

-- Gold cases, review decisions, and per-case results are evidence records.
-- Corrections are appended under new identifiers; existing records are never
-- rewritten or deleted by a runtime administrator.
CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_append_only_control_record()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION
        '% rows are append-only',
        TG_TABLE_NAME
        USING ERRCODE = '55000';
END
$$;

CREATE TRIGGER trg_v3_guard_review_decision_append
BEFORE UPDATE OR DELETE ON bauer_rag_v3.review_decisions
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_append_only_control_record();

CREATE TRIGGER trg_v3_guard_eval_case_append
BEFORE UPDATE OR DELETE ON bauer_rag_v3.eval_cases
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_append_only_control_record();

CREATE TRIGGER trg_v3_guard_eval_result_append
BEFORE UPDATE OR DELETE ON bauer_rag_v3.eval_results
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_append_only_control_record();

CREATE TRIGGER trg_v3_guard_independent_gold_attestation_append
BEFORE UPDATE OR DELETE
ON bauer_rag_v3.independent_gold_attestations
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_append_only_control_record();

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_eval_case_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    suite_status text;
BEGIN
    SELECT gold_status
    INTO suite_status
    FROM bauer_rag_v3.eval_suites
    WHERE eval_suite_id = NEW.eval_suite_id
    FOR SHARE;
    IF suite_status IS NULL OR suite_status = 'retired' THEN
        RAISE EXCEPTION
            'evaluation cases require a live suite'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.eval_runs
        WHERE eval_suite_id = NEW.eval_suite_id
    ) THEN
        RAISE EXCEPTION
            'evaluation cases are frozen after the first suite run'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_eval_case_insert
BEFORE INSERT ON bauer_rag_v3.eval_cases
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_eval_case_insert();

-- A suite manifest is immutable. Retirement is the only in-place lifecycle
-- change; a reviewed or corrected gold manifest must use a new suite version.
CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_eval_suite_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    IF NEW.eval_suite_id IS DISTINCT FROM OLD.eval_suite_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.suite_key IS DISTINCT FROM OLD.suite_key
       OR NEW.suite_version IS DISTINCT FROM OLD.suite_version
       OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
       OR NEW.metadata IS DISTINCT FROM OLD.metadata
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.verified_at IS DISTINCT FROM OLD.verified_at
       OR NEW.verified_by IS DISTINCT FROM OLD.verified_by
       OR NEW.gold_status IS DISTINCT FROM OLD.gold_status
          AND NOT (
              OLD.gold_status <> 'retired'
              AND NEW.gold_status = 'retired'
          ) THEN
        RAISE EXCEPTION
            'immutable evaluation suite manifest fields cannot change'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.gold_status = 'retired'
       AND EXISTS (
           SELECT 1
           FROM bauer_rag_v3.eval_runs
           WHERE eval_suite_id = OLD.eval_suite_id
             AND status = 'running'
       ) THEN
        RAISE EXCEPTION
            'evaluation suite cannot retire while a run is active'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_eval_suite
BEFORE UPDATE ON bauer_rag_v3.eval_suites
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_eval_suite_transition();

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_eval_run_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    suite_status text;
BEGIN
    IF NEW.status <> 'running'
       OR NEW.passed IS NOT NULL
       OR NEW.completed_at IS NOT NULL
       OR NEW.aggregate_metrics <> '{}'::jsonb
       OR cardinality(NEW.hard_failures) <> 0
       OR NEW.error IS NOT NULL THEN
        RAISE EXCEPTION
            'evaluation runs must be inserted in a clean running state'
            USING ERRCODE = '55000';
    END IF;
    SELECT gold_status
    INTO suite_status
    FROM bauer_rag_v3.eval_suites
    WHERE eval_suite_id = NEW.eval_suite_id
      AND tenant_id = NEW.tenant_id
    FOR UPDATE;
    IF suite_status IS NULL OR suite_status = 'retired' THEN
        RAISE EXCEPTION
            'evaluation runs require a live suite in the same tenant'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_eval_run_insert
BEFORE INSERT ON bauer_rag_v3.eval_runs
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_eval_run_insert();

-- A run may move once from running to a terminal outcome. Its pinned release,
-- suite, code/model identity, repetition contract, and terminal result cannot
-- be revised after the fact.
CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_eval_run_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
BEGIN
    IF OLD.status <> 'running' THEN
        RAISE EXCEPTION
            'terminal evaluation runs are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.eval_run_id IS DISTINCT FROM OLD.eval_run_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.kb_id IS DISTINCT FROM OLD.kb_id
       OR NEW.release_id IS DISTINCT FROM OLD.release_id
       OR NEW.eval_suite_id IS DISTINCT FROM OLD.eval_suite_id
       OR NEW.code_version IS DISTINCT FROM OLD.code_version
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
       OR NEW.repetitions IS DISTINCT FROM OLD.repetitions
       OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION
            'immutable evaluation run specification fields cannot change'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status NOT IN ('succeeded', 'failed', 'cancelled') THEN
        RAISE EXCEPTION
            'evaluation run update must make the run terminal'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_eval_run
BEFORE UPDATE ON bauer_rag_v3.eval_runs
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_eval_run_transition();

CREATE OR REPLACE FUNCTION bauer_rag_v3.guard_eval_result_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    run_status text;
    run_suite_id uuid;
    case_suite_id uuid;
BEGIN
    SELECT status, eval_suite_id
    INTO run_status, run_suite_id
    FROM bauer_rag_v3.eval_runs
    WHERE eval_run_id = NEW.eval_run_id
    FOR SHARE;
    SELECT eval_suite_id
    INTO case_suite_id
    FROM bauer_rag_v3.eval_cases
    WHERE eval_case_id = NEW.eval_case_id;
    IF run_status IS DISTINCT FROM 'running' THEN
        RAISE EXCEPTION
            'evaluation results require a running parent run'
            USING ERRCODE = '55000';
    END IF;
    IF case_suite_id IS NULL
       OR case_suite_id IS DISTINCT FROM run_suite_id THEN
        RAISE EXCEPTION
            'evaluation result case is outside the parent run suite'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_v3_guard_eval_result_insert
BEFORE INSERT ON bauer_rag_v3.eval_results
FOR EACH ROW
EXECUTE FUNCTION bauer_rag_v3.guard_eval_result_insert();

-- An independent review decision is authenticated by a dedicated database
-- tier. Evaluation and control-plane credentials cannot call this function or
-- insert the underlying row directly.
CREATE OR REPLACE FUNCTION bauer_rag_v3.attest_independent_gold(
    eval_suite_id uuid,
    expected_manifest_sha256 text,
    review_evidence_sha256 text,
    decision text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    context_tenant_id uuid;
    context_principal_ids uuid[];
    context_reviewer_principal_id uuid;
    suite_manifest_sha256 char(64);
    suite_gold_status text;
    attestation_decision text;
    attestation_evidence_sha256 text;
    new_attestation_id uuid;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS caller_role
        WHERE caller_role.rolname = session_user
          AND caller_role.rolcanlogin
          AND caller_role.rolinherit
          AND NOT caller_role.rolsuper
          AND NOT caller_role.rolcreaterole
          AND NOT caller_role.rolcreatedb
          AND NOT caller_role.rolbypassrls
          AND NOT caller_role.rolreplication
          AND pg_has_role(
              session_user,
              'bauer_rag_v3_reader',
              'member'
          )
          AND pg_has_role(
              session_user,
              'bauer_rag_v3_reviewer',
              'member'
          )
          AND NOT pg_has_role(
              session_user,
              'bauer_rag_v3_ingester',
              'member'
          )
          AND NOT pg_has_role(
              session_user,
              'bauer_rag_v3_evaluator',
              'member'
          )
          AND NOT pg_has_role(
              session_user,
              'bauer_rag_v3_admin',
              'member'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_roles AS inherited_role
              WHERE pg_has_role(
                  session_user,
                  inherited_role.oid,
                  'member'
              )
                AND (
                    inherited_role.rolsuper
                    OR inherited_role.rolcreaterole
                    OR inherited_role.rolcreatedb
                    OR inherited_role.rolbypassrls
                    OR inherited_role.rolreplication
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_namespace AS namespace
              WHERE namespace.nspname = 'bauer_rag_v3'
                AND pg_has_role(
                    session_user,
                    namespace.nspowner,
                    'member'
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_class AS relation
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = relation.relnamespace
              WHERE namespace.nspname = 'bauer_rag_v3'
                AND pg_has_role(
                    session_user,
                    relation.relowner,
                    'member'
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_proc AS routine
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = routine.pronamespace
              WHERE namespace.nspname = 'bauer_rag_v3'
                AND pg_has_role(
                    session_user,
                    routine.proowner,
                    'member'
                )
          )
    ) THEN
        RAISE EXCEPTION
            'independent gold attestation requires the exact reviewer tier'
            USING ERRCODE = '42501';
    END IF;

    context_tenant_id := bauer_rag_v3.current_tenant_id();
    context_principal_ids := bauer_rag_v3.current_principal_ids();
    IF context_tenant_id IS NULL
       OR cardinality(context_principal_ids) <> 1
       OR context_principal_ids[1] IS NULL THEN
        RAISE EXCEPTION
            'independent gold attestation requires one principal context'
            USING ERRCODE = '42501';
    END IF;
    context_reviewer_principal_id := context_principal_ids[1];
    IF NOT EXISTS (
        SELECT 1
        FROM bauer_rag_v3.principals AS principal
        WHERE principal.principal_id = context_reviewer_principal_id
          AND principal.tenant_id = context_tenant_id
          AND principal.principal_type = 'user'
    ) THEN
        RAISE EXCEPTION
            'reviewer user principal does not exist in the request tenant'
            USING ERRCODE = '42501';
    END IF;

    IF expected_manifest_sha256 IS NULL
       OR expected_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'expected manifest SHA-256 is invalid'
            USING ERRCODE = '22023';
    END IF;
    attestation_evidence_sha256 :=
        attest_independent_gold.review_evidence_sha256;
    IF attestation_evidence_sha256 IS NULL
       OR attestation_evidence_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'review evidence SHA-256 is invalid'
            USING ERRCODE = '22023';
    END IF;
    attestation_decision := attest_independent_gold.decision;
    IF attestation_decision IS NULL
       OR attestation_decision NOT IN ('approve', 'reject') THEN
        RAISE EXCEPTION 'review decision must be approve or reject'
            USING ERRCODE = '22023';
    END IF;

    SELECT suite.manifest_sha256, suite.gold_status
    INTO suite_manifest_sha256, suite_gold_status
    FROM bauer_rag_v3.eval_suites AS suite
    WHERE suite.eval_suite_id =
          attest_independent_gold.eval_suite_id
      AND suite.tenant_id = context_tenant_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'independent gold suite is absent from the request tenant'
            USING ERRCODE = '42501';
    END IF;
    IF suite_manifest_sha256::text IS DISTINCT FROM
       expected_manifest_sha256 THEN
        RAISE EXCEPTION
            'independent gold manifest SHA-256 does not match the suite'
            USING ERRCODE = '55000';
    END IF;
    IF suite_gold_status IS DISTINCT FROM
       'independent_bauer_verified' THEN
        RAISE EXCEPTION
            'independent gold attestation requires independently verified gold'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.independent_gold_attestations AS attestation
        WHERE attestation.eval_suite_id =
              attest_independent_gold.eval_suite_id
    ) THEN
        RAISE EXCEPTION
            'independent gold suite already has an attestation'
            USING ERRCODE = '55000';
    END IF;

    new_attestation_id := gen_random_uuid();
    INSERT INTO bauer_rag_v3.independent_gold_attestations (
        gold_attestation_id,
        tenant_id,
        eval_suite_id,
        manifest_sha256,
        reviewer_principal_id,
        reviewer_database_role,
        decision,
        review_evidence_sha256
    )
    VALUES (
        new_attestation_id,
        context_tenant_id,
        attest_independent_gold.eval_suite_id,
        expected_manifest_sha256,
        context_reviewer_principal_id,
        session_user,
        attestation_decision,
        attestation_evidence_sha256
    );
    RETURN new_attestation_id;
END
$$;

CREATE OR REPLACE FUNCTION bauer_rag_v3.record_authorization_audit(
    target_tenant_id uuid,
    target_kb_id uuid,
    target_release_id uuid,
    target_source_ids uuid[],
    audit_action text,
    audit_decision text,
    audit_reason_code text,
    audit_request_id text DEFAULT NULL
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    context_principal_ids uuid[];
    new_audit_id bigint;
BEGIN
    IF target_tenant_id IS NULL
       OR target_tenant_id IS DISTINCT FROM
          bauer_rag_v3.current_tenant_id() THEN
        RAISE EXCEPTION
            'authorization audit tenant does not match request context'
            USING ERRCODE = '42501';
    END IF;

    IF target_kb_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM bauer_rag_v3.knowledge_bases kb
           WHERE kb.kb_id = target_kb_id
             AND kb.tenant_id = target_tenant_id
             AND bauer_rag_v3.can_read_kb(kb.kb_id)
       ) THEN
        RAISE EXCEPTION
            'authorization audit knowledge base is not readable'
            USING ERRCODE = '42501';
    END IF;

    IF target_release_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM bauer_rag_v3.knowledge_releases release_row
           WHERE release_row.release_id = target_release_id
             AND (
                 target_kb_id IS NULL
                 OR release_row.kb_id = target_kb_id
             )
             AND bauer_rag_v3.can_read_release(release_row.release_id)
       ) THEN
        RAISE EXCEPTION
            'authorization audit release is not readable'
            USING ERRCODE = '42501';
    END IF;

    context_principal_ids :=
        bauer_rag_v3.current_principal_ids();
    IF cardinality(context_principal_ids) = 0
       OR array_position(context_principal_ids, NULL) IS NOT NULL
       OR cardinality(context_principal_ids) > 256 THEN
        RAISE EXCEPTION
            'authorization audit requires a bounded principal context'
            USING ERRCODE = '42501';
    END IF;

    target_source_ids := coalesce(target_source_ids, ARRAY[]::uuid[]);
    IF array_position(target_source_ids, NULL) IS NOT NULL
       OR cardinality(target_source_ids) > 1024 THEN
        RAISE EXCEPTION
            'authorization audit source scope is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM unnest(target_source_ids) target_source_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM bauer_rag_v3.sources source_row
            WHERE source_row.source_id = target_source_id
              AND (
                  target_kb_id IS NULL
                  OR source_row.kb_id = target_kb_id
              )
              AND bauer_rag_v3.can_read_source(source_row.source_id)
        )
    ) THEN
        RAISE EXCEPTION
            'authorization audit source scope is not readable'
            USING ERRCODE = '42501';
    END IF;
    IF audit_action IS NULL
       OR btrim(audit_action) = ''
       OR length(audit_action) > 128 THEN
        RAISE EXCEPTION 'authorization audit action is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF audit_decision IS NULL
       OR audit_decision NOT IN ('allow', 'deny', 'error') THEN
        RAISE EXCEPTION 'authorization audit decision is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF audit_reason_code IS NULL
       OR btrim(audit_reason_code) = ''
       OR length(audit_reason_code) > 128 THEN
        RAISE EXCEPTION 'authorization audit reason code is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF audit_request_id IS NOT NULL
       AND (
           btrim(audit_request_id) = ''
           OR length(audit_request_id) > 256
       ) THEN
        RAISE EXCEPTION 'authorization audit request id is invalid'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bauer_rag_v3.authorization_audit (
        tenant_id,
        actor_principal_ids,
        database_role,
        request_id,
        action,
        decision,
        reason_code,
        target_kb_id,
        target_release_id,
        target_source_ids
    )
    VALUES (
        target_tenant_id,
        context_principal_ids,
        session_user,
        audit_request_id,
        audit_action,
        audit_decision,
        audit_reason_code,
        target_kb_id,
        target_release_id,
        target_source_ids
    )
    RETURNING authorization_audit_id INTO new_audit_id;

    RETURN new_audit_id;
END
$$;

REVOKE CREATE ON SCHEMA bauer_rag_v3 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA bauer_rag_v3 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA bauer_rag_v3 FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA bauer_rag_v3 FROM PUBLIC;

-- Remove every broad object grant made in migration 009 before rebuilding
-- the intended least-privilege matrix.
REVOKE ALL ON ALL TABLES IN SCHEMA bauer_rag_v3
    FROM bauer_rag_v3_reader,
         bauer_rag_v3_ingester,
         bauer_rag_v3_evaluator,
         bauer_rag_v3_reviewer,
         bauer_rag_v3_admin;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA bauer_rag_v3
    FROM bauer_rag_v3_reader,
         bauer_rag_v3_ingester,
         bauer_rag_v3_evaluator,
         bauer_rag_v3_reviewer,
         bauer_rag_v3_admin;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA bauer_rag_v3
    FROM bauer_rag_v3_reader,
         bauer_rag_v3_ingester,
         bauer_rag_v3_evaluator,
         bauer_rag_v3_reviewer,
         bauer_rag_v3_admin;

GRANT USAGE ON SCHEMA bauer_rag_v3
    TO bauer_rag_v3_reader,
       bauer_rag_v3_ingester,
       bauer_rag_v3_evaluator,
       bauer_rag_v3_reviewer,
       bauer_rag_v3_admin;

-- Serving registry and immutable/canonical evidence.  ACLs, object-store
-- locations, jobs, QA/review data, eval gold/results, and activation history
-- are intentionally absent.
GRANT SELECT ON
    bauer_rag_v3.schema_migrations,
    bauer_rag_v3.knowledge_bases,
    bauer_rag_v3.knowledge_releases,
    bauer_rag_v3.active_releases,
    bauer_rag_v3.sources,
    bauer_rag_v3.source_versions,
    bauer_rag_v3.artifact_sets,
    bauer_rag_v3.release_sources,
    bauer_rag_v3.pages,
    bauer_rag_v3.sections,
    bauer_rag_v3.blocks,
    bauer_rag_v3.tables,
    bauer_rag_v3.table_segments,
    bauer_rag_v3.table_cells,
    bauer_rag_v3.provenance_spans,
    bauer_rag_v3.entities,
    bauer_rag_v3.entity_mentions,
    bauer_rag_v3.facts,
    bauer_rag_v3.fact_provenance,
    bauer_rag_v3.search_units,
    bauer_rag_v3.exact_terms,
    bauer_rag_v3.nav_nodes,
    bauer_rag_v3.nav_edges
TO bauer_rag_v3_reader;

-- Compilation repositories use idempotent upserts throughout.  They can
-- write source-native evidence and release-owned projections, but cannot
-- change release status, active pointers, ACLs, review decisions, or evals.
GRANT SELECT, INSERT, UPDATE ON
    bauer_rag_v3.objects,
    bauer_rag_v3.sources,
    bauer_rag_v3.source_versions,
    bauer_rag_v3.artifact_sets,
    bauer_rag_v3.release_sources,
    bauer_rag_v3.nav_nodes,
    bauer_rag_v3.qa_checks
TO bauer_rag_v3_ingester;

GRANT SELECT, INSERT ON
    bauer_rag_v3.pages,
    bauer_rag_v3.sections,
    bauer_rag_v3.blocks,
    bauer_rag_v3.tables,
    bauer_rag_v3.table_segments,
    bauer_rag_v3.table_cells,
    bauer_rag_v3.provenance_spans,
    bauer_rag_v3.entities,
    bauer_rag_v3.entity_mentions,
    bauer_rag_v3.facts,
    bauer_rag_v3.fact_provenance,
    bauer_rag_v3.search_units,
    bauer_rag_v3.exact_terms,
    bauer_rag_v3.nav_edges
TO bauer_rag_v3_ingester;

-- Rebuilding a projection deletes only release-owned search/navigation rows.
GRANT DELETE ON
    bauer_rag_v3.search_units,
    bauer_rag_v3.nav_nodes
TO bauer_rag_v3_ingester;

-- Durable queue operations never require DELETE or TRUNCATE.
GRANT SELECT, INSERT, UPDATE ON
    bauer_rag_v3.jobs,
    bauer_rag_v3.job_dependencies,
    bauer_rag_v3.job_attempts,
    bauer_rag_v3.dead_letters,
    bauer_rag_v3.job_events
TO bauer_rag_v3_ingester;
GRANT USAGE ON SEQUENCE
    bauer_rag_v3.job_attempts_attempt_id_seq,
    bauer_rag_v3.job_events_event_id_seq
TO bauer_rag_v3_ingester;

-- Gold/review persistence is deliberately separate from release control.
-- The evaluator can append suites, cases, runs, results, and review decisions,
-- but cannot activate a release or mutate canonical evidence.
GRANT SELECT, INSERT ON
    bauer_rag_v3.review_decisions,
    bauer_rag_v3.eval_suites,
    bauer_rag_v3.eval_cases,
    bauer_rag_v3.eval_runs,
    bauer_rag_v3.eval_results
TO bauer_rag_v3_evaluator;
GRANT UPDATE (gold_status)
ON bauer_rag_v3.eval_suites
TO bauer_rag_v3_evaluator;
GRANT UPDATE (
    status,
    passed,
    aggregate_metrics,
    hard_failures,
    completed_at,
    error
) ON bauer_rag_v3.eval_runs
TO bauer_rag_v3_evaluator;

-- The reviewer can inspect only the immutable record created through its
-- dedicated function. It receives no direct INSERT, UPDATE, or DELETE grant.
GRANT SELECT ON
    bauer_rag_v3.independent_gold_attestations
TO bauer_rag_v3_reviewer;

-- Control-plane privileges are named explicitly.  The admin role inherits
-- reader and ingester, but not evaluator. It can inspect evaluation evidence
-- while being unable to fabricate it, TRUNCATE canonical evidence, or mutate
-- active pointers outside the guarded activation function.
GRANT SELECT, INSERT, UPDATE ON
    bauer_rag_v3.tenants,
    bauer_rag_v3.knowledge_bases,
    bauer_rag_v3.principals,
    bauer_rag_v3.kb_grants,
    bauer_rag_v3.source_grants
TO bauer_rag_v3_admin;

GRANT DELETE ON
    bauer_rag_v3.kb_grants,
    bauer_rag_v3.source_grants
TO bauer_rag_v3_admin;

GRANT SELECT ON
    bauer_rag_v3.review_decisions,
    bauer_rag_v3.eval_suites,
    bauer_rag_v3.eval_cases,
    bauer_rag_v3.eval_runs,
    bauer_rag_v3.eval_results,
    bauer_rag_v3.independent_gold_attestations
TO bauer_rag_v3_admin;

GRANT SELECT, INSERT ON
    bauer_rag_v3.knowledge_releases
TO bauer_rag_v3_admin;
GRANT UPDATE (
    status,
    validation_started_at,
    ready_at,
    failed_at,
    retired_at,
    error
) ON bauer_rag_v3.knowledge_releases
TO bauer_rag_v3_admin;

GRANT SELECT ON
    bauer_rag_v3.release_activations,
    bauer_rag_v3.authorization_audit
TO bauer_rag_v3_admin;

REVOKE TRUNCATE ON ALL TABLES IN SCHEMA bauer_rag_v3
FROM bauer_rag_v3_reader,
     bauer_rag_v3_ingester,
     bauer_rag_v3_evaluator,
     bauer_rag_v3_reviewer,
     bauer_rag_v3_admin;

-- Even a runtime admin cannot rewrite or truncate the append-only audit log.
REVOKE UPDATE, DELETE, TRUNCATE
ON bauer_rag_v3.authorization_audit
FROM bauer_rag_v3_admin;
REVOKE UPDATE
ON SEQUENCE bauer_rag_v3.authorization_audit_authorization_audit_id_seq
FROM bauer_rag_v3_admin;

-- Reader RLS predicates and the one narrow audit append capability.
GRANT EXECUTE ON FUNCTION bauer_rag_v3.current_tenant_id()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.current_principal_ids()
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_kb(uuid)
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_source(uuid)
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_release(uuid)
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_artifact(uuid)
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_nav_node(uuid, uuid)
    TO bauer_rag_v3_reader;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.record_authorization_audit(
    uuid,
    uuid,
    uuid,
    uuid[],
    text,
    text,
    text,
    text
) TO bauer_rag_v3_reader;

-- Independent review is a separate authority from evaluation and release
-- control. Role inheritance supplies the two request-context functions.
GRANT EXECUTE ON FUNCTION bauer_rag_v3.attest_independent_gold(
    uuid,
    text,
    text,
    text
) TO bauer_rag_v3_reviewer;

-- Additional predicates used by compilation, release membership, jobs, and
-- compiler QA policies.  Role inheritance supplies all reader functions.
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_ingest_kb(uuid)
    TO bauer_rag_v3_ingester;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_source(uuid)
    TO bauer_rag_v3_ingester;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_release(uuid)
    TO bauer_rag_v3_ingester;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_artifact(uuid)
    TO bauer_rag_v3_ingester;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_job(uuid)
    TO bauer_rag_v3_ingester;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_job(uuid)
    TO bauer_rag_v3_ingester;

-- Evaluation persistence requires its own group and app-level admin
-- principal context for the existing RLS predicates.
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_admin_kb(uuid)
    TO bauer_rag_v3_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_admin_tenant(uuid)
    TO bauer_rag_v3_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_evaluate_release(uuid)
    TO bauer_rag_v3_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_qa_check(uuid)
    TO bauer_rag_v3_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_qa_check(uuid)
    TO bauer_rag_v3_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_eval_run(uuid)
    TO bauer_rag_v3_evaluator;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_eval_run(uuid)
    TO bauer_rag_v3_evaluator;

-- Control-plane operations use the admin group.
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_admin_kb(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_admin_tenant(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_evaluate_release(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_qa_check(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_qa_check(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_read_eval_run(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.can_write_eval_run(uuid)
    TO bauer_rag_v3_admin;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.activate_release(
    uuid,
    uuid,
    uuid,
    text,
    boolean
) TO bauer_rag_v3_admin;

-- Undo migration 009's unsafe future-object grants.  New relations and
-- functions remain owner-only until a later reviewed migration names the
-- precise runtime role and capability.
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE SELECT ON TABLES FROM bauer_rag_v3_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE SELECT, INSERT, UPDATE, DELETE
    ON TABLES FROM
        bauer_rag_v3_ingester,
        bauer_rag_v3_evaluator,
        bauer_rag_v3_reviewer,
        bauer_rag_v3_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE USAGE, SELECT
    ON SEQUENCES FROM
        bauer_rag_v3_ingester,
        bauer_rag_v3_evaluator,
        bauer_rag_v3_reviewer,
        bauer_rag_v3_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE EXECUTE ON FUNCTIONS
    FROM bauer_rag_v3_reader,
         bauer_rag_v3_ingester,
         bauer_rag_v3_evaluator,
         bauer_rag_v3_reviewer,
         bauer_rag_v3_admin;
