CREATE TABLE bauer_rag_v4.compilation_jobs (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'dead')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    leased_by text,
    lease_expires_at timestamptz,
    error_code text,
    error_fingerprint char(64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, knowledge_base_id, release_id)
        REFERENCES bauer_rag_v4.knowledge_releases(
            tenant_id, knowledge_base_id, release_id
        ),
    FOREIGN KEY (tenant_id, knowledge_base_id, source_id)
        REFERENCES bauer_rag_v4.source_documents(
            tenant_id, knowledge_base_id, source_id
        ),
    CHECK (
        (status = 'running' AND leased_by IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'running' AND leased_by IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        error_fingerprint IS NULL
        OR error_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX compilation_jobs_claim_idx
    ON bauer_rag_v4.compilation_jobs
    (tenant_id, knowledge_base_id, status, available_at, created_at);

CREATE TABLE bauer_rag_v4.authorization_audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    release_id uuid,
    decision text NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z0-9_]{2,64}$'),
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    authorized_source_count integer NOT NULL CHECK (authorized_source_count >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE bauer_rag_v4.evaluation_runs (
    evaluation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    suite_sha256 char(64) NOT NULL CHECK (suite_sha256 ~ '^[0-9a-f]{64}$'),
    split text NOT NULL CHECK (split IN ('development', 'holdout')),
    case_count integer NOT NULL CHECK (case_count > 0),
    metrics jsonb NOT NULL,
    passed boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, knowledge_base_id, release_id)
        REFERENCES bauer_rag_v4.knowledge_releases(
            tenant_id, knowledge_base_id, release_id
        )
);

CREATE TABLE bauer_rag_v4.review_attestations (
    review_attestation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    evaluation_run_id uuid NOT NULL
        REFERENCES bauer_rag_v4.evaluation_runs(evaluation_run_id),
    decision text NOT NULL CHECK (decision IN ('approve', 'reject')),
    review_packet_sha256 char(64) NOT NULL
        CHECK (review_packet_sha256 ~ '^[0-9a-f]{64}$'),
    authority text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (evaluation_run_id)
);

CREATE FUNCTION bauer_rag_v4.claim_compilation_job(
    worker_identity text,
    lease_seconds integer DEFAULT 60
)
RETURNS SETOF bauer_rag_v4.compilation_jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
DECLARE
    claimed bauer_rag_v4.compilation_jobs;
BEGIN
    IF worker_identity !~ '^[a-zA-Z0-9_.:-]{3,128}$'
       OR lease_seconds NOT BETWEEN 5 AND 3600 THEN
        RAISE EXCEPTION 'invalid worker lease request' USING ERRCODE = '22023';
    END IF;
    SELECT job.*
    INTO claimed
    FROM bauer_rag_v4.compilation_jobs AS job
    WHERE job.tenant_id = bauer_rag_v4.current_tenant_id()
      AND job.knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
      AND job.status = 'queued'
      AND job.available_at <= clock_timestamp()
      AND job.attempts < job.max_attempts
    ORDER BY job.created_at, job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1;
    IF claimed.job_id IS NULL THEN
        RETURN;
    END IF;
    UPDATE bauer_rag_v4.compilation_jobs
    SET status = 'running',
        attempts = attempts + 1,
        leased_by = worker_identity,
        lease_expires_at = clock_timestamp() + make_interval(secs => lease_seconds),
        updated_at = clock_timestamp()
    WHERE job_id = claimed.job_id
    RETURNING * INTO claimed;
    RETURN NEXT claimed;
END
$$;

CREATE FUNCTION bauer_rag_v4.fail_compilation_job(
    target_job_id uuid,
    worker_identity text,
    failure_code text,
    failure_fingerprint text
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
DECLARE
    next_status text;
BEGIN
    IF failure_code !~ '^[a-zA-Z0-9_.:-]{2,128}$'
       OR failure_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'unsafe failure metadata' USING ERRCODE = '22023';
    END IF;
    UPDATE bauer_rag_v4.compilation_jobs
    SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
        available_at = clock_timestamp(),
        leased_by = NULL,
        lease_expires_at = NULL,
        error_code = failure_code,
        error_fingerprint = failure_fingerprint,
        updated_at = clock_timestamp()
    WHERE job_id = target_job_id
      AND tenant_id = bauer_rag_v4.current_tenant_id()
      AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
      AND status = 'running'
      AND leased_by = worker_identity
    RETURNING status INTO next_status;
    IF next_status IS NULL THEN
        RAISE EXCEPTION 'job lease not owned in scope' USING ERRCODE = '42501';
    END IF;
    RETURN next_status;
END
$$;

CREATE FUNCTION bauer_rag_v4.requeue_expired_jobs()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
DECLARE
    affected integer;
BEGIN
    UPDATE bauer_rag_v4.compilation_jobs
    SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
        available_at = clock_timestamp(),
        leased_by = NULL,
        lease_expires_at = NULL,
        error_code = 'lease_expired',
        error_fingerprint = repeat('0', 64),
        updated_at = clock_timestamp()
    WHERE tenant_id = bauer_rag_v4.current_tenant_id()
      AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
      AND status = 'running'
      AND lease_expires_at < clock_timestamp();
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END
$$;

CREATE FUNCTION bauer_rag_v4.record_authorization_audit(
    audit_decision text,
    audit_reason_code text,
    audit_request_sha256 text,
    audit_authorized_source_count integer
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
DECLARE
    new_id bigint;
BEGIN
    INSERT INTO bauer_rag_v4.authorization_audit (
        tenant_id,
        knowledge_base_id,
        principal_id,
        release_id,
        decision,
        reason_code,
        request_sha256,
        authorized_source_count
    ) VALUES (
        bauer_rag_v4.current_tenant_id(),
        bauer_rag_v4.current_knowledge_base_id(),
        bauer_rag_v4.current_principal_id(),
        bauer_rag_v4.current_release_id(),
        audit_decision,
        audit_reason_code,
        audit_request_sha256,
        audit_authorized_source_count
    )
    RETURNING audit_id INTO new_id;
    RETURN new_id;
END
$$;
