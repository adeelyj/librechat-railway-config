CREATE TABLE IF NOT EXISTS bauer_rag_v3.qa_checks (
    qa_check_id uuid PRIMARY KEY,
    kb_id uuid NOT NULL
        REFERENCES bauer_rag_v3.knowledge_bases(kb_id) ON DELETE CASCADE,
    check_code text NOT NULL,
    checker_version text NOT NULL,
    severity text NOT NULL
        CHECK (severity IN ('info', 'warning', 'error', 'blocker')),
    status text NOT NULL
        CHECK (status IN ('pass', 'fail', 'warn', 'skipped')),
    release_id uuid NOT NULL
        REFERENCES bauer_rag_v3.knowledge_releases(release_id)
        ON DELETE CASCADE,
    artifact_set_id uuid
        REFERENCES bauer_rag_v3.artifact_sets(artifact_set_id)
        ON DELETE CASCADE,
    page_id uuid
        REFERENCES bauer_rag_v3.pages(page_id) ON DELETE CASCADE,
    table_id uuid
        REFERENCES bauer_rag_v3.tables(table_id) ON DELETE CASCADE,
    fact_id uuid
        REFERENCES bauer_rag_v3.facts(fact_id) ON DELETE CASCADE,
    search_unit_id uuid
        REFERENCES bauer_rag_v3.search_units(search_unit_id)
        ON DELETE CASCADE,
    metric numeric,
    threshold numeric,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (
        check_code,
        checker_version,
        release_id,
        artifact_set_id,
        page_id,
        table_id,
        fact_id,
        search_unit_id
    ),
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE CASCADE,
    CHECK (btrim(check_code) <> ''),
    CHECK (btrim(checker_version) <> ''),
    CHECK (
        num_nonnulls(
            artifact_set_id,
            page_id,
            table_id,
            fact_id,
            search_unit_id
        ) <= 1
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.review_decisions (
    review_decision_id uuid PRIMARY KEY,
    qa_check_id uuid NOT NULL
        REFERENCES bauer_rag_v3.qa_checks(qa_check_id) ON DELETE CASCADE,
    reviewer_principal_id uuid
        REFERENCES bauer_rag_v3.principals(principal_id)
        ON DELETE SET NULL,
    decision text NOT NULL
        CHECK (decision IN ('approve', 'reject', 'correct', 'waive')),
    notes text,
    proposed_correction jsonb,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.eval_suites (
    eval_suite_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL
        REFERENCES bauer_rag_v3.tenants(tenant_id) ON DELETE CASCADE,
    suite_key text NOT NULL,
    suite_version text NOT NULL,
    manifest_sha256 char(64) NOT NULL
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    gold_status text NOT NULL
        CHECK (gold_status IN (
            'draft',
            'interim_reviewed',
            'verified',
            'independent_bauer_verified',
            'retired'
        )),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    verified_by text,
    UNIQUE (tenant_id, eval_suite_id),
    UNIQUE (tenant_id, suite_key, suite_version),
    CHECK (btrim(suite_key) <> ''),
    CHECK (btrim(suite_version) <> ''),
    CHECK (
        (
            gold_status IN ('verified', 'independent_bauer_verified')
            AND verified_at IS NOT NULL
        )
        OR gold_status NOT IN ('verified', 'independent_bauer_verified')
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.independent_gold_attestations (
    gold_attestation_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    eval_suite_id uuid NOT NULL UNIQUE,
    manifest_sha256 char(64) NOT NULL
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    reviewer_principal_id uuid NOT NULL,
    reviewer_database_role text NOT NULL,
    decision text NOT NULL
        CHECK (decision IN ('approve', 'reject')),
    review_evidence_sha256 char(64) NOT NULL
        CHECK (review_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    attested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, eval_suite_id)
        REFERENCES bauer_rag_v3.eval_suites(tenant_id, eval_suite_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, reviewer_principal_id)
        REFERENCES bauer_rag_v3.principals(tenant_id, principal_id)
        ON DELETE RESTRICT,
    CHECK (btrim(reviewer_database_role) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.eval_cases (
    eval_case_id uuid PRIMARY KEY,
    eval_suite_id uuid NOT NULL
        REFERENCES bauer_rag_v3.eval_suites(eval_suite_id)
        ON DELETE CASCADE,
    case_key text NOT NULL,
    split text NOT NULL CHECK (split IN ('development', 'holdout')),
    category text NOT NULL,
    prompt jsonb NOT NULL,
    expected jsonb NOT NULL,
    hard_failure_codes text[] NOT NULL DEFAULT '{}',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (eval_suite_id, case_key),
    CHECK (btrim(case_key) <> ''),
    CHECK (btrim(category) <> '')
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.eval_runs (
    eval_run_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    kb_id uuid NOT NULL,
    release_id uuid NOT NULL,
    eval_suite_id uuid NOT NULL,
    status text NOT NULL
        CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled')),
    passed boolean,
    code_version text NOT NULL,
    model_version text,
    prompt_version text,
    repetitions integer NOT NULL CHECK (repetitions >= 1),
    aggregate_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    hard_failures text[] NOT NULL DEFAULT '{}',
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error text,
    UNIQUE (
        release_id,
        eval_suite_id,
        code_version,
        model_version,
        prompt_version,
        started_at
    ),
    FOREIGN KEY (tenant_id, kb_id)
        REFERENCES bauer_rag_v3.knowledge_bases(tenant_id, kb_id)
        ON DELETE CASCADE,
    FOREIGN KEY (kb_id, release_id)
        REFERENCES bauer_rag_v3.knowledge_releases(kb_id, release_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, eval_suite_id)
        REFERENCES bauer_rag_v3.eval_suites(tenant_id, eval_suite_id)
        ON DELETE RESTRICT,
    CHECK (btrim(code_version) <> ''),
    CHECK (
        (
            status = 'running'
            AND completed_at IS NULL
            AND passed IS NULL
        )
        OR
        (
            status <> 'running'
            AND completed_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS bauer_rag_v3.eval_results (
    eval_result_id uuid PRIMARY KEY,
    eval_run_id uuid NOT NULL
        REFERENCES bauer_rag_v3.eval_runs(eval_run_id) ON DELETE CASCADE,
    eval_case_id uuid NOT NULL
        REFERENCES bauer_rag_v3.eval_cases(eval_case_id) ON DELETE RESTRICT,
    repetition integer NOT NULL CHECK (repetition >= 1),
    passed boolean NOT NULL,
    scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    hard_failures text[] NOT NULL DEFAULT '{}',
    evidence_search_unit_ids uuid[] NOT NULL DEFAULT '{}',
    latency_ms integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (eval_run_id, eval_case_id, repetition)
);
