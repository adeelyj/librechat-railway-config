CREATE TABLE bauer_rag_v4.compiled_artifacts (
    tenant_id uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    release_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    artifact_object_key text NOT NULL CHECK (artifact_object_key LIKE 'v4/%'),
    artifact_sha256 char(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_json jsonb NOT NULL,
    projections_json jsonb NOT NULL,
    parser_identity text NOT NULL,
    quality_status text NOT NULL
        CHECK (quality_status IN ('pass', 'warning')),
    quality_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    compiled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (release_id, source_id),
    FOREIGN KEY (tenant_id, knowledge_base_id, release_id)
        REFERENCES bauer_rag_v4.knowledge_releases(
            tenant_id, knowledge_base_id, release_id
        ),
    FOREIGN KEY (tenant_id, knowledge_base_id, source_id, source_version_id)
        REFERENCES bauer_rag_v4.source_versions(
            tenant_id, knowledge_base_id, source_id, source_version_id
        ),
    UNIQUE (release_id, artifact_object_key),
    UNIQUE (release_id, artifact_sha256)
);

CREATE INDEX compiled_artifacts_scope_idx
    ON bauer_rag_v4.compiled_artifacts
    (tenant_id, knowledge_base_id, release_id, source_id);

ALTER TABLE bauer_rag_v4.compiled_artifacts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON bauer_rag_v4.compiled_artifacts FROM PUBLIC;
GRANT SELECT ON bauer_rag_v4.compiled_artifacts
TO bauer_rag_v4_reader, bauer_rag_v4_evaluator;
GRANT SELECT, INSERT ON bauer_rag_v4.compiled_artifacts
TO bauer_rag_v4_worker;

CREATE POLICY compiled_artifact_read_scope
ON bauer_rag_v4.compiled_artifacts
FOR SELECT TO bauer_rag_v4_reader, bauer_rag_v4_evaluator
USING (
    tenant_id = bauer_rag_v4.current_tenant_id()
    AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
    AND release_id = bauer_rag_v4.current_release_id()
    AND bauer_rag_v4.can_read_source(source_id)
);

CREATE POLICY compiled_artifact_worker_scope
ON bauer_rag_v4.compiled_artifacts
TO bauer_rag_v4_worker
USING (
    tenant_id = bauer_rag_v4.current_tenant_id()
    AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
)
WITH CHECK (
    tenant_id = bauer_rag_v4.current_tenant_id()
    AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
);

CREATE FUNCTION bauer_rag_v4.succeed_compilation_job(
    target_job_id uuid,
    worker_identity text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bauer_rag_v4
AS $$
DECLARE
    completed boolean;
BEGIN
    UPDATE bauer_rag_v4.compilation_jobs
    SET status = 'succeeded',
        leased_by = NULL,
        lease_expires_at = NULL,
        error_code = NULL,
        error_fingerprint = NULL,
        updated_at = clock_timestamp()
    WHERE job_id = target_job_id
      AND tenant_id = bauer_rag_v4.current_tenant_id()
      AND knowledge_base_id = bauer_rag_v4.current_knowledge_base_id()
      AND status = 'running'
      AND leased_by = worker_identity
    RETURNING true INTO completed;
    IF completed IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'job lease not owned in scope' USING ERRCODE = '42501';
    END IF;
    RETURN true;
END
$$;

REVOKE ALL ON FUNCTION bauer_rag_v4.succeed_compilation_job(uuid, text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    bauer_rag_v4.succeed_compilation_job(uuid, text)
TO bauer_rag_v4_worker;
