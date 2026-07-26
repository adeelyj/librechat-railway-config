# Bauer Evidence V3

Bauer Evidence V3 is a source-native evidence compiler and answer service. Original source bytes,
canonical page/table/fact evidence, and immutable release manifests are authoritative. Exact,
relational, lexical, semantic, and navigation indexes are reproducible projections.

This directory is additive. It does not replace the current RAG API, modify the V1 schema, or use
the V2 Markdown derivatives as V3 source truth.

## Components

- `bauer_evidence_v3/ingest`: format probing, native parsing, selective page OCR, canonical IR, and
  extraction quality gates. Production workers require the OCR capability.
- `bauer_evidence_v3/projections.py`: exact terms, typed facts, table rows, evidence units, and
  non-citable navigation summaries.
- `bauer_evidence_v3/postgres_compiler.py`: PostgreSQL persistence for compiled canonical evidence
  and release-specific projections.
- `bauer_evidence_v3/postgres_jobs.py`: PostgreSQL queue with idempotency, dependencies, leases,
  heartbeats, retries, and dead letters.
- `bauer_evidence_v3/postgres_admin.py`: explicit release control plane. It never activates a
  release as a side effect.
- `bauer_evidence_v3/postgres_runtime.py`: active- or deployment-fixed-candidate release pinning
  with authorization-scoped retrieval.
- `bauer_evidence_v3/answering.py`: evidence packaging, answer generation, deterministic
  validation, one repair, and safe refusal.
- `bauer_evidence_v3/api.py`: fail-closed `/v3/answer`, retrieval-only `/v3/query`, health,
  readiness, and public build/version identity routes.
- `bauer_evidence_v3/evaluation.py`, `postgres_eval.py`, `observation_metrics.py`, and
  `eval_cli.py`: release-pinned answer/query evaluation plus reader-only canonical
  extraction/table capture, authorization checks, and immutable PostgreSQL run records.
- `postgres_review.py` and `gold_review_cli.py`: exact-reviewer, one-time independent gold
  attestation bound to the immutable suite manifest and signed review-packet hashes.
- `migrations`: isolated `bauer_rag_v3` schema through version 14, RLS, least-privilege runtime
  roles, append-only authorization audit, release activation gate, and indexes.

## Runtime roles

One image source exposes three explicit process entrypoints:

```text
python -m bauer_evidence_v3.entrypoint api
python -m bauer_evidence_v3.entrypoint worker
python -m bauer_evidence_v3.entrypoint migrate
```

The API is stateless. The worker is at-least-once and its compiler writes are idempotent.
Migrations run only through the explicit `migrate` command; readiness never mutates the schema.

PostgreSQL uses five exact, non-owner runtime group tiers. The API uses
`bauer_rag_v3_reader`, the worker uses `bauer_rag_v3_ingester`, evaluation persistence uses the
parallel `bauer_rag_v3_evaluator`, independent gold attestation uses
`bauer_rag_v3_reviewer`, and release control uses `bauer_rag_v3_admin`. The evaluator can append
guarded evaluation records but cannot attest or activate a release. The reviewer can bind one
immutable decision to the exact gold manifest and signed review-packet hash but cannot run
evaluations or activate a release. The admin can inspect those records but cannot create or
rewrite them. Each non-bootstrap connection rejects unexpected V3 group inheritance, superuser,
`BYPASSRLS`, and table ownership.

## Local verification

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\bauer-evidence-v3').Path
python -m unittest discover `
  -s services\bauer-evidence-v3\tests `
  -p 'test_*.py' -v
```

The in-memory golden vertical slice covers:

```text
source bytes
  -> immutable object
  -> canonical pages/blocks/table/cells
  -> typed fact + exact/table/lexical projections
  -> ready candidate release
  -> atomic activation
  -> validated cited answer
  -> atomic rollback
```

Exact lookup is built from both source content and immutable source-native metadata. The compiler
projects the external file ID, source filename (and stem), parsed and manifest titles, document
number, product tags, and aliases when those fields are present. Metadata terms are attached to one
deterministic, source-backed evidence unit, so a lookup routes to original evidence without
pretending that manifest metadata is a page quote.

Every source-bound category and artifact navigation node has a dedicated description search unit.
Those units are release- and source-scoped, marked `generated_summary=true` and
`is_citable=false`, have no primary provenance span, and are referenced by
`nav_nodes.description_search_unit_id`. They may guide navigation ranking, but the runtime evidence
query and final citation path exclude them.

## Dependencies

`requirements.txt` is the shared API/worker runtime. The worker additionally installs
`requirements-parser.txt`.

- `pdfplumber` is the default PDF adapter.
- `requirements-pymupdf-licensed.txt` is deliberately separate because PyMuPDF requires an
  AGPL-compatible deployment or a commercial licence.
- `requirements-ocr.txt` is worker-only but mandatory for a production worker. OCR must be enabled,
  and the detection, recognition, and classification model files must be baked or mounted with
  their SHA-256 hashes. Languages, minimum confidence, and render DPI are explicit configuration;
  the worker never downloads models during a job.
- `requirements-observability.txt` enables OTLP traces and metrics.

The database projection currently fixes embeddings at 1,024 dimensions. Changing the embedding
model or dimensions requires a new compiler fingerprint and a new knowledge release, plus an
additive schema migration if the dimension changes.

## Security boundary

- The API accepts only a short-lived HMAC-signed authorization context.
- A production deployment is bound to one configured tenant, knowledge base, database principal
  set, and Agent allow-list.
- Every retrieval channel applies release, tenant, KB, source, and database ACL filters before
  fusion.
- Empty source scope returns no evidence.
- Generated summaries are never citable.
- Logs redact authorization, credentials, tokens, and document content.
- Authorization decisions and final validator dispositions are structured, body-free log/metric
  events. Valid in-scope authorization decisions are also recorded in the RLS-protected,
  append-only `authorization_audit` table.
- The API returns `cache-control: no-store`.

LibreChat creates the signed context from files resolved by its server-side permission service.
Client-supplied file IDs are not accepted as proof of access.

For allow-listed private V3 Agents, LibreChat seals the Agent graph to `file_search` only, enables
`toolEnd`, and suppresses the routing model's text/reasoning. The exact validated V3 answer is the
final response only when one valid direct-final envelope exists. Additional tools, connected
Agents, subagents, malformed output, or multiple completions fail closed. V1 and V2 Agents are not
changed by this boundary.

## Release rule

A database release row can be `draft`, `building`, `validating`, `ready`, `failed`, or `retired`.
`Active` is not a mutable release status. The only serving truth is the single row in
`active_releases` for a knowledge base. Activation and rollback both call the guarded
`bauer_rag_v3.activate_release(...)` function.

Production activation requires a passing evaluation suite whose gold state is
`independent_bauer_verified` plus an approved immutable attestation from the exact reviewer
database tier. The attestation binds the suite manifest SHA-256 to the SHA-256 of the independently
signed review packet. Interim Codex review can support development, but cannot create or pass that
production gate.

For pre-activation shadow tests, an API deployment may set
`BAUER_V3_CANDIDATE_RELEASE_ID` to one UUID. That deployment pins only the matching authorized
`ready` release and reports `postgres_fixed_candidate` from `/ready`; it neither reads nor writes
the active pointer for request selection. The value is not accepted in request bodies and is
rejected for worker, migration, and in-memory roles. Omitting it preserves normal atomic
`active_releases` selection.

Canonical extraction and table evaluation read the pinned release directly through a separate
reader-only PostgreSQL connection and one repeatable-read snapshot; it does not depend on an answer
model. Query/answer shadow evaluation uses only a dedicated API process fixed to the selected
authorized `ready` candidate.

The checked-in source contract covers 373 selected originals (166 PDF and 207 HTML), 177 duplicate
aliases that account for all 550 reviewed inputs, and 503,391,181 selected bytes. Its SHA-256 is
`40049a12aacb198018a633905d793c8ef9011403f3fdbcd34ed7fe0792ab2580`. Verify the contract against
the original corpus, bind it to the target tenant/KB/release, and then run `manifest-create`; the
bound hash and byte count are checked again before the immutable release manifest is written.

No V3 candidate release, benchmark, Railway deployment, or live V3 Agent route exists yet.
External gates remain: a real PostgreSQL/RLS exercise, managed database and mirrored stores,
pinned model/OCR assets, the complete 373-source build, real-corpus execution and source review of
the portable B08/B11/B20/B21 extraction fixtures, V2/V3 shadow comparison, independent Bauer
signoff, and explicit deployment/activation approval.

See [the architecture](../../docs/bauer-rag-v3-architecture.md) and
[the runbook](../../docs/bauer-rag-v3-runbook.md).
