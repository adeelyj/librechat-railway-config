# Bauer RAG V3 runbook

Date: 2026-07-25
Status: local implementation only. No V3 Railway service or migration has been applied to a real
PostgreSQL instance, and no candidate knowledge release, benchmark, live Agent route, deployment,
or activation has been produced by this work.

## Non-negotiable safety boundary

The normal Bauer Agent remains V1 and the existing private candidate remains V2. Before any V3
deployment or routing change:

1. re-read the paired central V3 plan and handover;
2. record the current Railway project/environment, service IDs, deployment IDs, branches, commits,
   domains, V1/V2 Agent IDs, and active knowledge release;
3. record the target V3 commit, migration set, service configuration, candidate release, and both
   rollback paths;
4. obtain explicit deployment confirmation.

A pushed branch or ready candidate release is not deployment authorization.

## Required services

| Service | Exposure | Responsibility |
| --- | --- | --- |
| V3 migration job | Private, run-to-completion | Apply numbered migrations under an advisory lock |
| V3 compiler worker | Private | Parse, OCR, extract, QA, build projections |
| V3 Evidence API | Private to LibreChat | Authorize, retrieve, generate, validate |
| V3 PostgreSQL | Private | Canonical evidence, projections, queue, releases, QA/evals |
| Primary object store | Private credentials | Immutable source and canonical artifacts |
| Independent mirror | Separate failure domain | Versioned copy of originals/manifests/artifacts |

Use a dedicated V3 database or dedicated credentials and schema. Do not point the V3 migrator at
the V1/V2 database without confirming the target and rollback snapshot.

## Environment contract

### Common database

```text
BAUER_V3_ENVIRONMENT=production
BAUER_V3_DATABASE_URL=<private PostgreSQL DSN>
BAUER_V3_EXPECTED_MIGRATION_VERSION=17
BAUER_V3_BUILD_COMMIT=<lowercase Git commit; Railway commit metadata is the fallback>
```

### API

```text
BAUER_V3_SERVICE_ROLE=api
BAUER_V3_TENANT_ID=<UUID>
BAUER_V3_KB_ID=<UUID>
BAUER_V3_PRINCIPAL_IDS_JSON=["<read-principal UUID>"]
BAUER_V3_ALLOWED_AGENT_IDS_JSON=["<private V3 Agent ID>"]
# Shadow API only: omit for normal active-pointer serving.
BAUER_V3_CANDIDATE_RELEASE_ID=<ready candidate release UUID>
BAUER_V3_AUTH_AUDIENCE=bauer-evidence-v3
BAUER_V3_AUTH_KEYRING_JSON={"current":"<minimum 32-byte signing key>"}
BAUER_V3_MODEL_BASE_URL=<OpenAI-compatible /v1 base>
BAUER_V3_MODEL_API_KEY=<secret>
BAUER_V3_MODEL_NAME=<answer model>
BAUER_V3_EMBEDDING_BASE_URL=<OpenAI-compatible /v1 base>
BAUER_V3_EMBEDDING_API_KEY=<secret>
BAUER_V3_EMBEDDING_MODEL=<1024-dimensional embedding model>
BAUER_V3_EMBEDDING_DIMENSIONS=1024
OTEL_EXPORTER_OTLP_ENDPOINT=<optional collector base URL>
```

`BAUER_V3_CANDIDATE_RELEASE_ID` is a deployment setting, never a request parameter. Use it only
on a dedicated shadow API deployment whose Agent allow-list contains only the private shadow
Agent. Startup rejects a malformed UUID, an in-memory API, or use on a worker/migration role.
`/ready` reports `release_selection=fixed_candidate` and succeeds only while that exact release is
authorized, belongs to the configured tenant/KB, and remains `ready`. Removing the variable
restores normal atomic `active_releases` selection. Candidate mode never changes that pointer.
`/version` and `/health` expose the configured build commit so runtime evidence can be tied to the
exact source revision without exposing configuration secrets.

### Worker object storage

```text
BAUER_V3_SERVICE_ROLE=worker
BAUER_V3_TENANT_ID=<same UUID as API>
BAUER_V3_KB_ID=<same UUID as API>
BAUER_V3_PRINCIPAL_IDS_JSON=["<ingest-principal UUID>"]
BAUER_V3_WORKER_ID=<stable instance identity>
BAUER_V3_EMBEDDING_BASE_URL=<OpenAI-compatible /v1 base>
BAUER_V3_EMBEDDING_API_KEY=<secret>
BAUER_V3_EMBEDDING_MODEL=<same 1024-dimensional model recorded by release>
BAUER_V3_EMBEDDING_DIMENSIONS=1024
BAUER_V3_EMBEDDING_BATCH_SIZE=32
BAUER_V3_OCR_ENABLED=true
BAUER_V3_OCR_DETECTION_MODEL=/models/<detection-model>.onnx
BAUER_V3_OCR_DETECTION_MODEL_SHA256=<64 lowercase hexadecimal characters>
BAUER_V3_OCR_RECOGNITION_MODEL=/models/<recognition-model>.onnx
BAUER_V3_OCR_RECOGNITION_MODEL_SHA256=<64 lowercase hexadecimal characters>
BAUER_V3_OCR_CLASSIFICATION_MODEL=/models/<classification-model>.onnx
BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256=<64 lowercase hexadecimal characters>
BAUER_V3_OCR_LANGUAGES_JSON=["de","en"]
BAUER_V3_OCR_MINIMUM_CONFIDENCE=0.80
BAUER_V3_OCR_RENDER_DPI=150
BAUER_V3_OBJECT_STORE_BACKEND=s3
BAUER_V3_S3_BUCKET=<primary bucket>
BAUER_V3_S3_ENDPOINT_URL=<primary endpoint>
BAUER_V3_S3_REGION=<region>
BAUER_V3_S3_ACCESS_KEY_ID=<secret>
BAUER_V3_S3_SECRET_ACCESS_KEY=<secret>
BAUER_V3_S3_PREFIX=bauer-rag-v3
BAUER_V3_MIRROR_S3_BUCKET=<independent versioned bucket>
BAUER_V3_MIRROR_S3_ENDPOINT_URL=<mirror endpoint>
BAUER_V3_MIRROR_S3_REGION=<region>
BAUER_V3_MIRROR_S3_ACCESS_KEY_ID=<secret>
BAUER_V3_MIRROR_S3_SECRET_ACCESS_KEY=<secret>
BAUER_V3_WORKER_POLL_SECONDS=2
BAUER_V3_JOB_LEASE_SECONDS=120
```

The worker refuses production startup without an independent mirror. Credentials must be injected
as secret variables and must never be written to a manifest or log. Production workers also refuse
startup unless OCR is enabled and all three checksum-pinned model files are configured. Bake or
mount the models; runtime downloads are forbidden. The supported confidence interval is 0 through
1, and render DPI must be between 72 and 300. Languages, confidence, DPI, engine version, and model
hashes become part of the compiler identity for the release.

### PostgreSQL credentials

The migration/bootstrap DSN and runtime DSNs must be different credentials:

- the migration owner applies migrations and performs only the initial tenant/KB/principal/grant
  bootstrap; never inject this DSN into the API or worker;
- the API login is granted membership in the `bauer_rag_v3_reader` NOLOGIN group;
- the worker login is granted membership in the `bauer_rag_v3_ingester` NOLOGIN group;
- the evaluation persistence login is granted membership in the
  `bauer_rag_v3_evaluator` NOLOGIN group;
- the independent Bauer reviewer login is granted membership in the
  `bauer_rag_v3_reviewer` NOLOGIN group;
- a release-control login is granted `bauer_rag_v3_admin` only when it needs lifecycle or
  activation operations.

Every non-bootstrap connection verifies its exact group tier and refuses a PostgreSQL superuser,
`BYPASSRLS` role, table owner, or unexpected inherited V3 group. The evaluator and release-control
admin are parallel roles: the evaluator can append immutable evaluation records but cannot attest,
activate releases, or change ACLs; the admin can inspect those records but cannot manufacture or
rewrite them. The reviewer is a fifth parallel tier that can create one immutable decision bound
to the exact gold-manifest digest and signed review-packet digest, but cannot run evaluations or
activate a release. Migration 009 creates the runtime group roles; migration 010 removes the broad
bootstrap grants, makes all five groups `NOLOGIN`, and grants only the serving, compiler/queue,
evaluation-persistence, independent-review, or control-plane surface required by each role.
Migration 011 scopes every RLS policy to the runtime group that owns the matching table capability,
so a reader query never evaluates write or control-plane predicates. The ingester cannot mutate
release lifecycle, active pointers, ACLs, review decisions, or evaluation gold. None of these
migrations creates login passwords.

Migration 012 preserves the source registry's idempotent upsert path by expressing its RLS update
check directly as the current tenant plus ingest/admin permission on the row's knowledge base. It
does not add any table, function, or role grant.
Migration 013 lets the trigger-only compiler-QA guard take its existing release row lock as the
isolated schema owner. Runtime groups retain no direct execution right, and the ingester receives
no release-table update grant.
Migration 014 removes the compiler's obsolete `UPDATE` privilege on immutable release membership;
idempotent retries use insert-or-ignore followed by an exact RLS-protected identity check.
Migration 015 moves that insert/verify operation behind one context-validating, schema-owner
function so PostgreSQL can take the parent release key-share lock without any compiler update grant
on `knowledge_releases`; direct membership inserts and updates are revoked.

Migration 010 also creates the RLS-protected, append-only `authorization_audit` table and a
scope-checking function callable by the API reader. Valid in-scope authorization decisions are
written there; updates and deletes are rejected. Authorization outcomes and final validator
dispositions are also emitted as structured logs and bounded metrics without tokens, queries,
answers, or document content.

### LibreChat V3 overlay

```text
BAUER_V3_AGENT_IDS=<comma-separated private V3 Agent IDs>
BAUER_V3_API_URL=<private V3 API URL>
BAUER_V3_TENANT_ID=<same UUID as API>
BAUER_V3_KB_ID=<same UUID as API>
BAUER_V3_AUTH_AUDIENCE=bauer-evidence-v3
BAUER_V3_AUTH_KEY_ID=current
BAUER_V3_AUTH_SIGNING_KEY=<same current signing key>
```

V3 allow-list selection takes precedence only for IDs explicitly listed in `BAUER_V3_AGENT_IDS`.
All other private V2 IDs remain V2 and all remaining Agents remain V1.

The overlay signs only the file IDs returned by LibreChat's server-side Agent permission filter.
It rejects the complete V3 answer if any returned evidence lacks a matching authorized external
file ID. Conversation uploads are not mixed into a V3-validated Agent answer until they have been
compiled into a V3 release.

Every allow-listed V3 Agent must expose only `file_search`. It must not have connected Agents,
subagents, graph edges, or any additional tool. The overlay sets `toolEnd=true`, suppresses the
routing model's text and reasoning, and persists the exact validated V3 answer from one
direct-final tool envelope. Missing, malformed, unauthorized, or multiple tool completions fail
closed with a deterministic refusal. This rule applies only to `BAUER_V3_AGENT_IDS`; it does not
change V1 or V2 Agents.

For an allow-listed V3 request, LibreChat disables tool approval only on a request-local copy of
the Agent configuration. It never changes the global configuration. Resume payloads from an
earlier approval flow are rejected so a stale completion cannot bypass the direct-final boundary.

## Build and test

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\bauer-evidence-v3').Path
python -m unittest discover `
  -s services\bauer-evidence-v3\tests `
  -p 'test_*.py' -v

python -m unittest discover `
  -s evals\bauer-rag-v3\tests `
  -p 'test_*.py' -v

node --test `
  services\librechat-custom\tests\fileSearchBatch.test.js `
  services\librechat-custom\tests\v3Authorization.test.js `
  services\librechat-custom\tests\bauerV3FinalBoundary.test.js `
  services\librechat-custom\tests\patchBauerV3FinalBoundary.test.js
```

Build contexts:

```text
services/bauer-evidence-v3/Dockerfile.api
services/bauer-evidence-v3/Dockerfile.worker
services/librechat-custom/Dockerfile
```

## Freeze and bind the original-source contract

The checked-in source contract is
`evals/bauer-rag-v3/baselines/bauer-source-contract.json`. It freezes 373 selected originals
(166 PDF and 207 HTML), 177 duplicate aliases accounting for all 550 reviewed inputs, and
503,391,181 selected bytes. Its expected SHA-256 is
`40049a12aacb198018a633905d793c8ef9011403f3fdbcd34ed7fe0792ab2580`.

Verify it against the immutable original-source tree before choosing a release manifest:

```powershell
python evals\bauer-rag-v3\source_contract.py verify `
  --contract evals\bauer-rag-v3\baselines\bauer-source-contract.json `
  --source-root "D:\02_Code\Bauer Kompressoren Demo"
```

After the infrastructure UUIDs are fixed, bind the verified contract to exactly one release:

```powershell
python evals\bauer-rag-v3\source_contract.py bind-release `
  --contract evals\bauer-rag-v3\baselines\bauer-source-contract.json `
  --source-root "D:\02_Code\Bauer Kompressoren Demo" `
  --tenant-id <tenant-uuid> `
  --knowledge-base-id <kb-uuid> `
  --release-id <release-uuid> `
  --output <release-source-spec.json>

python -m bauer_evidence_v3.release_control_cli manifest-create `
  --spec <release-source-spec.json> `
  --source-root "D:\02_Code\Bauer Kompressoren Demo" `
  --output <release.manifest.json>

python -m bauer_evidence_v3.release_control_cli manifest-verify `
  --manifest <release.manifest.json> `
  --source-root "D:\02_Code\Bauer Kompressoren Demo"
```

The binder carries each expected source hash and byte count into the release spec.
`manifest-create` re-reads and re-hashes the exact original bytes. Any drift fails before a
manifest is produced; existing different bound specs or manifests are not overwritten. The
two-level category path is explicitly navigation-only and non-citable.

## Deployment sequence

The commands below are a checklist, not authorization to run them.

1. Create a private V3 PostgreSQL service and enable backups/PITR.
2. Create the primary evidence bucket and an independent versioned mirror.
3. Configure private networking and service variables.
4. Run `python -m bauer_evidence_v3.entrypoint migrate` once.
5. Bootstrap the tenant, KB, service principals, and grants with the schema-owner control path.
6. Start one worker.
7. Verify the frozen 373-source contract, bind it to the chosen tenant/KB/release UUIDs, create the
   release manifest, and verify that manifest against the original bytes. Do not use V2 Markdown
   derivatives as source truth.
8. Upload each verified original and manifest to both object stores and enqueue idempotent compile
   jobs.
9. Keep the candidate in `building` until every source is compiled, reused, or explicitly
   quarantined.
10. Run extraction QA, then transition to `validating`.
11. Run the development evaluation harness. Do not open the locked holdout.
12. Mark the passing release `ready`; do not activate it.
13. Start a dedicated private shadow API with `BAUER_V3_CANDIDATE_RELEASE_ID` set to the ready
    candidate and an allow-list containing only the new shadow Agent. Verify `/health`, confirm
    `/version` names the intended commit, and confirm `/ready` reports `postgres_fixed_candidate`;
    a missing, unauthorized, non-ready, or wrong-KB candidate must return not-ready. Railway uses
    `/health` for process health, while external serving monitors use `/ready`.
14. Provision the new private V3 Agent and shadow test it. Do not reuse the V1/V2 Agent IDs, and
    do not add the shadow Agent to an active-pointer API deployment.
15. Obtain the independently signed Bauer gold-review packet and record its immutable attestation
    through the exact `bauer_rag_v3_reviewer` credential as described below.
16. Only after a separate approval, call the database activation function and add the new Agent ID
    to the normal LibreChat V3 allow-list. Remove `BAUER_V3_CANDIDATE_RELEASE_ID` from any
    deployment intended to serve through the active pointer.

## Dead-letter recovery

Inspect the failed job and release before replaying it. Correct the underlying configuration,
source, or dependency problem, then create one linked, audited replay with a new idempotency key:

```powershell
python -m bauer_evidence_v3.release_control_cli job-replay-dead-letter `
  --job-id <dead-job-uuid> `
  --idempotency-key <incident-specific-new-key> `
  --actor-principal-id <operator-principal-uuid>
```

Replay is allowed only through the release-control path. It creates a new job linked by
`replay_of_job_id`, records the actor/event, refuses the original or occupied idempotency key, and
does not reopen a terminal release.

## Knowledge-release rollback

Knowledge rollback does not require an application redeploy:

1. identify the prior ready release from `release_activations`;
2. verify its sources, jobs, citable evidence, QA, and evaluation records still pass;
3. call `PostgresReleaseAdmin.rollback_release(...)` with the target prior release, actor principal,
   reason, and `production=True`;
4. verify `/ready`, a pinned exact query, a table query, and an authorization-negative query;
5. record the new activation audit row.

Never update `active_releases` directly.

## Application rollback

Application rollback does not change the knowledge pointer:

1. remove only the private V3 Agent ID from `BAUER_V3_AGENT_IDS` or restore the recorded prior
   LibreChat deployment;
2. restore the recorded prior V3 API/worker deployment if needed;
3. leave V1/V2 routes and data untouched;
4. confirm the normal Bauer Agent still selects V1 and the private V2 Agent still selects V2.

## Evaluation

The V3 scorer defaults to development and refuses combined splits. Loading a holdout requires both
`--split holdout` and `--acknowledge-locked-holdout`; that acknowledgement is not permission to
tune or promote.

There are three deliberately separate execution modes:

- `--direct-validating` evaluates one exact `validating` release in process with a non-owner reader
  DSN while a separate exact `bauer_rag_v3_evaluator` DSN stores the immutable run;
- `--canonical-only` captures only declared extraction/table targets through a reader-only DSN,
  from one release-pinned repeatable-read snapshot, with no answer model or API call;
- `--api-url` evaluates answer/query cases through a private shadow API fixed to the exact
  authorized `ready` release by `BAUER_V3_CANDIDATE_RELEASE_ID`.

For example, after a reviewed canonical-target manifest exists:

```powershell
python -m bauer_evidence_v3.eval_cli `
  --manifest <canonical-targets.yaml> `
  --split development `
  --release-id <candidate-release-uuid> `
  --canonical-only `
  --code-version <commit-sha>
```

`BAUER_V3_EVAL_DATABASE_URL` must contain the exact `bauer_rag_v3_evaluator` persistence DSN.
`BAUER_V3_EVAL_QUERY_DATABASE_URL` must be a different reader-only DSN, with its principals supplied
through `BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS` or repeated `--query-principal-id` arguments. The
release-control `bauer_rag_v3_admin` DSN is separate and is not accepted by the evaluator.

Reports keep extraction, retrieval, answer grounding, table integrity, route-specific latency, and
authorization leakage separate. Zero authorization leakage and zero accepted unsupported
high-risk claims are hard gates. Initial targets remain:

- source/citation completeness: 100%;
- exact lookup: 100%;
- table integrity: at least 0.90 and above V2's 0.6923;
- Recall@5: at least 0.90 and no worse than V2's 0.8667;
- authorization violations: 0.

No V3 benchmark result exists yet. No candidate release has been built, and no development or
shadow run has been performed.

### Independent gold attestation

The `independent_bauer_verified` manifest label is necessary but is not production authority by
itself. After an independent Bauer reviewer signs the review packet, provision a dedicated login
with only `bauer_rag_v3_reviewer` membership and run:

```powershell
$env:BAUER_V3_REVIEW_DATABASE_URL = '<exact-reviewer-role-DSN>'
$env:BAUER_V3_REVIEW_TENANT_ID = '<tenant-uuid>'
$env:BAUER_V3_REVIEW_PRINCIPAL_ID = '<active-independent-reviewer-principal-uuid>'

python -m bauer_evidence_v3.gold_review_cli `
  --manifest <independent-reviewed-gold.yaml> `
  --split development `
  --review-evidence <independently-signed-review.packet> `
  --decision approve `
  --confirm-manifest-sha256 <independently-confirmed-sha256>
```

This creates one append-only attestation bound to the exact suite manifest and review-packet
hashes. A rejection is terminal for that suite; corrections use a new immutable suite version.
Production activation requires the matching approval. The evaluator and release admin cannot
create it, and the reviewer cannot run evaluations or change the active release.

## Known pre-deployment checks

- Exercise all ten migrations and every RLS/role path against a disposable real PostgreSQL
  instance.
- Provision managed PostgreSQL, a primary object store, and an independently versioned mirror.
- Verify the selected parser/OCR licences; bake the pinned answer, embedding, and OCR assets.
- Build and account for the complete 373-source release, including quarantine and recovery tests.
- Execute the portable B08/B11/B20/B21 fixtures against the built canonical source IDs, review
  every still-open verbatim requirement against the originals, and run canonical extraction/table
  evaluation.
- Run fixed-candidate V2/V3 shadow comparison, authorization negatives, and route-specific load
  tests.
- Verify primary/mirror recovery from a deliberately missing primary object.
- Confirm LibreChat returns the exact V3 direct-final answer and that a V3 outage cannot affect V1
  or V2.
- Obtain independent Bauer gold signoff.
- Obtain separate explicit approval before any Railway deployment, Agent routing change, release
  activation, or production spend.
