CREATE TABLE IF NOT EXISTS bauer_rag_v3.jobs (
    job_id uuid PRIMARY KEY,
    queue_name text NOT NULL,
    job_type text NOT NULL,
    kb_id uuid NOT NULL
        REFERENCES bauer_rag_v3.knowledge_bases(kb_id) ON DELETE CASCADE,
    release_id uuid
        REFERENCES bauer_rag_v3.knowledge_releases(release_id)
        ON DELETE CASCADE,
    source_id uuid
        REFERENCES bauer_rag_v3.sources(source_id)
        ON DELETE CASCADE,
    source_version_id uuid
        REFERENCES bauer_rag_v3.source_versions(source_version_id)
        ON DELETE CASCADE,
    artifact_set_id uuid
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    parent_job_id uuid
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE SET NULL,
    replay_of_job_id uuid
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE SET NULL,
    idempotency_key text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN (
            'queued',
            'running',
            'retry_wait',
            'succeeded',
            'dead',
            'cancelled'
        )),
    priority smallint NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    max_attempts smallint NOT NULL DEFAULT 5
        CHECK (max_attempts >= 1),
    attempt_count smallint NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    leased_by text,
    lease_token uuid,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    last_error_code text,
    last_error_class text,
    last_error_message text,
    result jsonb,
    result_object_sha256 char(64)
        REFERENCES bauer_rag_v3.objects(sha256) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (queue_name, idempotency_key),
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE CASCADE,
    FOREIGN KEY (kb_id, source_id)
        REFERENCES bauer_rag_v3.sources(kb_id, source_id)
        ON DELETE CASCADE,
    FOREIGN KEY (source_id, source_version_id)
        REFERENCES bauer_rag_v3.source_versions(source_id, source_version_id)
        ON DELETE CASCADE,
    FOREIGN KEY (source_version_id, artifact_set_id)
        REFERENCES bauer_rag_v3.artifact_sets(
            source_version_id,
            artifact_set_id
        )
        ON DELETE CASCADE,
    CHECK (btrim(queue_name) <> ''),
    CHECK (btrim(job_type) <> ''),
    CHECK (btrim(idempotency_key) <> ''),
    CHECK (attempt_count <= max_attempts),
    CHECK (source_version_id IS NULL OR source_id IS NOT NULL),
    CHECK (artifact_set_id IS NULL OR source_version_id IS NOT NULL),
    CHECK (
        (
            state = 'running'
            AND leased_by IS NOT NULL
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR
        (
            state <> 'running'
            AND leased_by IS NULL
            AND lease_token IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CHECK (
        (
            state IN ('succeeded', 'dead', 'cancelled')
            AND finished_at IS NOT NULL
        )
        OR
        (
            state NOT IN ('succeeded', 'dead', 'cancelled')
            AND finished_at IS NULL
        )
    ),
    CHECK (parent_job_id IS NULL OR parent_job_id <> job_id),
    CHECK (replay_of_job_id IS NULL OR replay_of_job_id <> job_id)
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.job_dependencies (
    job_id uuid NOT NULL
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE CASCADE,
    depends_on_job_id uuid NOT NULL
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE CASCADE,
    requirement text NOT NULL DEFAULT 'succeeded'
        CHECK (requirement IN ('succeeded', 'completed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, depends_on_job_id),
    CHECK (job_id <> depends_on_job_id)
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.job_attempts (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE CASCADE,
    attempt_number smallint NOT NULL CHECK (attempt_number >= 1),
    worker_id text NOT NULL,
    lease_token uuid NOT NULL,
    outcome text NOT NULL DEFAULT 'running'
        CHECK (outcome IN (
            'running',
            'succeeded',
            'failed',
            'lease_expired',
            'cancelled'
        )),
    started_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz,
    finished_at timestamptz,
    error_code text,
    error_class text,
    error_message text,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, attempt_number),
    UNIQUE (job_id, lease_token),
    CHECK (btrim(worker_id) <> ''),
    CHECK (
        (outcome = 'running' AND finished_at IS NULL)
        OR
        (outcome <> 'running' AND finished_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.dead_letters (
    job_id uuid PRIMARY KEY
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE CASCADE,
    failed_at timestamptz NOT NULL DEFAULT now(),
    final_error_code text,
    final_error_class text,
    final_error_message text NOT NULL,
    payload_snapshot jsonb NOT NULL,
    replayed_by_job_id uuid
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE SET NULL,
    replayed_at timestamptz,
    CHECK (
        (replayed_by_job_id IS NULL AND replayed_at IS NULL)
        OR
        (replayed_by_job_id IS NOT NULL AND replayed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.job_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL
        REFERENCES bauer_rag_v3.jobs(job_id) ON DELETE CASCADE,
    event_type text NOT NULL,
    actor text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (btrim(event_type) <> '')
);

ALTER TABLE bauer_rag_v3.artifact_sets
    ADD CONSTRAINT artifact_sets_worker_job_fk
    FOREIGN KEY (worker_job_id)
    REFERENCES bauer_rag_v3.jobs(job_id)
    ON DELETE SET NULL;
