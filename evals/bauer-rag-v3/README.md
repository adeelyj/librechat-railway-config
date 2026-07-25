# Bauer RAG V3 evaluation harness

This directory is the development-first evaluation contract for the source-native V3 evidence
platform. It intentionally keeps extraction, retrieval, answer grounding, table integrity,
latency, and authorization leakage as separate metric families. There is no weighted score that
can hide a hard failure in one family.

The checked-in `cases/development.yaml` is an empty, reviewable scaffold. Populate it only with
development gold that can be inspected by the interim reviewer and later signed by an independent
Bauer reviewer. This harness does not copy V2 gold and does not contain a holdout file.

## Portable difficult-source fixtures

`fixtures/difficult-sources-interim.json` records portable extraction expectations for the known
difficult cases B08, B11, B20, and B21. It binds only original-source identities and hashes from
`baselines/bauer-source-contract.json`; it deliberately contains no tenant, knowledge-base,
release, search-unit, evidence, or run UUIDs.

The bundle is marked `interim_codex_reviewed_requires_bauer_signoff` and
`benchmark_eligible=false`. It is synthetic review material, not an observation from a V3 corpus
build and not a real-corpus benchmark result. Open verbatim checks, including the B-KOOL
cartridge-life wording and the three B-SELECT function descriptions, remain explicit rather than
being invented.

The case boundaries are explicit: B08 combines B-KOOL HTML with B-SELECT selector evidence on
physical PDF page 41; B11 preserves the breathing-air 300 bar versus Nitrox 200 bar safety limit
without inferring 300 bar Nitrox compatibility; B20 owns the complete B-SELECT pressure, range,
flow, and three-function expectations; and B21 keeps B-SAFE headline values distinct from the
later B-SAFE 300 technical-data values.

Verify its structure and frozen source identities without opening any source or holdout file:

```powershell
python evals\bauer-rag-v3\verify_difficult_sources.py
```

## Holdout controls

Development is the only default split. The loader:

1. accepts only `development` or `holdout` (there is no `all` mode);
2. refuses `holdout` unless `--acknowledge-locked-holdout` is supplied;
3. performs that refusal before opening or checking the holdout file; and
4. requires the run and suite to declare the same explicitly selected split.

The acknowledgement only permits loading. It is not permission to tune on the holdout, promote a
release, or change live traffic.

## Run

### Execute and persist a candidate run

Both evaluator modes verify that every response stayed on the selected release and within the
manifest's source scope. They persist the immutable suite, cases, run, and per-case results in the
existing PostgreSQL evaluation tables. The persistence login must be a non-owner member of
`bauer_rag_v3_evaluator`; the evaluator sets the tenant and principal RLS context in every
transaction.

When `extraction_targets` or `table_targets` are present, either mode also opens the separate
`BAUER_V3_EVAL_QUERY_DATABASE_URL` reader connection and captures canonical observations in one
`REPEATABLE READ, READ ONLY` transaction. The login must be reader-only
(ingester/evaluator/admin membership is refused), and every query is explicitly pinned to the
manifest tenant, knowledge base, release, and source IDs in addition to PostgreSQL RLS. The
evaluator persistence connection is never used to read canonical evidence.

The three target modes are mutually exclusive:

- `--canonical-only` captures and scores reviewed extraction/table targets without calling an
  answer model or API. It accepts a structured-only manifest whose `queries` array is empty.
- `--direct-validating` builds an in-process answer service fixed to the exact `validating`
  release. It uses a separate non-owner `bauer_rag_v3_reader` DSN, fixes retrieval to
  `validating`, and creates an authorization context from the manifest's exact tenant, knowledge
  base, and source IDs. It does not start or expose an API route.
- `--api-url` calls `/v3/answer` or `/v3/query` on a private API fixed to the exact `ready`
  release. It requires a short-lived signed authorization token. Do not point it at the ordinary
  active-release API.

The safe operator order is: enter `validating`, run canonical-only extraction/table evaluation,
run the direct query/answer evaluation, satisfy the remaining database gates and mark the release
`ready`, then run the HTTP shadow evaluation against a fixed-candidate private API. No evaluation
mode activates the release.

#### 0. Capture canonical extraction/table observations

Canonical-only mode requires only the separate evaluator persistence and reader capture
credentials; it does not require model, embedding, or HTTP token credentials:

```powershell
python -m bauer_evidence_v3.eval_cli `
  --manifest evals\bauer-rag-v3\cases\development.yaml `
  --split development `
  --release-id <candidate-release-uuid> `
  --canonical-only `
  --code-version <commit-sha> `
  --output evals\bauer-rag-v3\reports\<canonical-run-id>.json
```

#### 1. Evaluate the validating release in-process

After compilation and blocking compiler QA complete, enter validation:

```powershell
python -m bauer_evidence_v3.release_control_cli release-begin-validation `
  --release-id <candidate-release-uuid>
```

Keep both database credentials and model secrets out of command arguments. The evaluator and
reader DSNs must identify separate logins; the CLI refuses an identical DSN:

```powershell
$env:BAUER_V3_EVAL_DATABASE_URL = '<evaluator-role PostgreSQL DSN>'
$env:BAUER_V3_EVAL_PRINCIPAL_IDS = '["<evaluation-principal-with-app-admin-grant>"]'
$env:BAUER_V3_EVAL_QUERY_DATABASE_URL = '<reader-role PostgreSQL DSN>'
$env:BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS = '["<reader-principal-uuid>"]'
$env:BAUER_V3_EVAL_USER_ID = '<evaluation-user-id>'
$env:BAUER_V3_EVAL_AGENT_ID = '<direct-evaluator-agent-id>'
$env:BAUER_V3_EVAL_AUDIENCE = 'bauer-evidence-v3-evaluation'
$env:BAUER_V3_MODEL_BASE_URL = 'https://<openai-compatible-model>/v1'
$env:BAUER_V3_MODEL_API_KEY = '<model-secret>'
$env:BAUER_V3_MODEL_NAME = '<answer-model>'
$env:BAUER_V3_EMBEDDING_BASE_URL = 'https://<openai-compatible-embedding>/v1'
$env:BAUER_V3_EMBEDDING_API_KEY = '<embedding-secret>'
$env:BAUER_V3_EMBEDDING_MODEL = '<embedding-model>'
$env:BAUER_V3_EMBEDDING_DIMENSIONS = '1024'
$env:PYTHONPATH = (Resolve-Path 'services\bauer-evidence-v3').Path

python -m bauer_evidence_v3.eval_cli `
  --manifest evals\bauer-rag-v3\cases\development.yaml `
  --split development `
  --release-id <candidate-release-uuid> `
  --direct-validating `
  --code-version <commit-sha> `
  --model-version <model-version> `
  --prompt-version <prompt-version> `
  --output evals\bauer-rag-v3\reports\<run-id>.json
```

The direct-mode reader connection is always checked for least privilege. Before creating the
durable run, the evaluator verifies that the requested UUID is still the exact authorized
`validating` release. The reader credential is never reused for evaluation-store writes, and the
evaluator credential is never used for evidence retrieval.

When the direct run and all other readiness gates pass:

```powershell
python -m bauer_evidence_v3.release_control_cli release-mark-ready `
  --release-id <candidate-release-uuid>
```

There is no force-ready path; the command will refuse a release that has not satisfied the
database evidence, QA, and required gold-evaluation gates.

#### 2. Shadow-test the ready release over HTTP

Deploy or configure a private API with `BAUER_V3_CANDIDATE_RELEASE_ID` set to the now-ready
candidate UUID. This candidate route does not consult or change the active pointer. Then run:

```powershell
$env:BAUER_V3_EVAL_AUTHORIZATION_TOKEN = '<short-lived signed context>'

python -m bauer_evidence_v3.eval_cli `
  --manifest evals\bauer-rag-v3\cases\development.yaml `
  --split development `
  --release-id <candidate-release-uuid> `
  --api-url https://<private-fixed-candidate-v3-api> `
  --code-version <commit-sha> `
  --model-version <model-version> `
  --prompt-version <prompt-version> `
  --output evals\bauer-rag-v3\reports\<shadow-run-id>.json
```

For a longer HTTP run, `--authorization-token-file` re-reads a sidecar-managed token before every
request. Direct mode neither reads nor requires an HTTP authorization token. A failed correctness
check produces a completed run with `passed=false`; transport, release-pin, authorization,
malformed-evidence, and invalid-accepted-answer failures are always recorded as hard failures.
The CLI exits non-zero when the run does not pass.

Cases use this executable shape:

```json
{
  "case_id": "pressure-answer",
  "category": "grounded_answer",
  "prompt": {
    "mode": "answer",
    "query": "What is the rated pressure?",
    "mandatory_constraints": {"unit": "bar"},
    "top_k": 8
  },
  "retrieval": {
    "required_evidence_ids": ["<search-unit-uuid>"],
    "forbidden_evidence_ids": [],
    "exact_lookup": true
  },
  "answer": {
    "refusal_expected": false,
    "accepted_statuses": ["answered", "answered_after_repair"],
    "required_substrings": ["525 bar"],
    "forbidden_substrings": []
  },
  "hard_failure_codes": ["required_evidence_missing"]
}
```

Set `prompt.mode` to `query` for retrieval-only cases. Suite and case UUIDs are derived
deterministically from the tenant, suite key/version, and case key. Reusing those identities with
different content is refused instead of overwritten. Non-empty extraction/table targets require
the canonical PostgreSQL observation source and can never be silently skipped. Each table target
must include its canonical `source_id`, and reviewed fact/table/cell IDs may be either the stable
canonical ID or its persisted UUID. Missing or mismatched structured evidence makes the durable
evaluation run fail.

### Score an already captured run

The standalone scorer uses only the Python standard library. Files with a `.yaml` suffix use JSON
syntax, which is valid YAML 1.2.

```powershell
python evals\bauer-rag-v3\score.py `
  --run path\to\immutable-run.json `
  --split development `
  --output evals\bauer-rag-v3\reports\candidate-score.json
```

A locked evaluation must name the split and acknowledge it:

```powershell
python evals\bauer-rag-v3\score.py `
  --run path\to\immutable-holdout-run.json `
  --split holdout `
  --acknowledge-locked-holdout
```

No holdout file is checked in here. An independent evaluation owner must place it at
`cases/holdout.yaml` only when the locked evaluation is authorized.

## Suite contract

Each split file contains:

- `gold_status`: review state; development results never authorize promotion by themselves;
- `authorization_scope`: exact tenant, knowledge base, release, and allowed source IDs;
- `extraction_targets`: expected source IDs, page counts, and typed facts;
- `table_targets`: expected grid shape and source-native cells;
- `queries`: retrieval evidence and answer constraints for each case.

Every target should carry enough stable IDs for a reviewer to trace it to the original source.
Generated summaries and category descriptions must not be listed as claim-supporting evidence.

## Immutable run contract

```json
{
  "schema_version": 1,
  "run_id": "candidate-development-001",
  "split": "development",
  "release_id": "release-uuid",
  "observations": {
    "extraction": [],
    "tables": [],
    "retrieval": [],
    "answers": [],
    "latency": [],
    "authorization": []
  }
}
```

The observation schemas are deliberately structured:

- extraction observations report source status, page count, evidence resolvability, reading-order
  checks, deterministic-ID checks, and typed fact value/unit/source-coordinate checks;
- table observations report cell values, units, spans, and source-coordinate resolvability;
- retrieval observations return stable evidence IDs plus complete authorization coordinates;
- answer observations use the V3 validator's structured claim, citation, constraint, refusal, and
  decision records;
- latency observations name both stage and route so exact, structured, semantic, and visual paths
  are never averaged together;
- authorization observations record any explicit denial or unauthorized-result counter.

See the synthetic unit fixtures in `tests/` for minimal complete examples.

The exclusive `--output` report is directly consumable by the standalone scorer and contains the
targeted fact/cell values needed for review. Those raw structured observations are not written to
the evaluation database: PostgreSQL stores only structured metrics, hard-failure codes, and the
existing per-query results.

## Output

The report has six peer metric sections:

- `extraction_qa`;
- `retrieval`;
- `answer_grounding`;
- `table_integrity`;
- `latency`;
- `authorization_leakage`.

Authorization leakage and accepted unsupported high-risk claims are surfaced as hard failures.
Promotion remains unevaluated: it requires a complete corpus release, independent Bauer-reviewed
gold, an authorized locked run, an immutable reviewer attestation, and a separate recorded
deployment/activation decision.

## Independent review boundary

The routine evaluator cannot turn its own `independent_bauer_verified` label into production
authority. An independently signed review packet must be recorded by a different exact
`bauer_rag_v3_reviewer` login:

```powershell
$env:BAUER_V3_REVIEW_DATABASE_URL = '<reviewer-role PostgreSQL DSN>'
$env:BAUER_V3_REVIEW_TENANT_ID = '<tenant-uuid>'
$env:BAUER_V3_REVIEW_PRINCIPAL_ID = '<independent-reviewer-principal-uuid>'

python -m bauer_evidence_v3.gold_review_cli `
  --manifest <independent-reviewed-gold.yaml> `
  --split development `
  --review-evidence <independently-signed-review.packet> `
  --decision approve `
  --confirm-manifest-sha256 <independently-confirmed-sha256>
```

The command hashes the signed packet and binds its digest to the exact suite-manifest digest. The
reviewer tier cannot persist evaluation results or activate a release; the evaluator and release
admin cannot create the attestation. Interim Codex review never satisfies this boundary.

## Tests

```powershell
python -m unittest discover -s evals\bauer-rag-v3\tests -p "test_*.py" -v
```

All tests construct synthetic suites and runs in temporary directories. They never load the
repository's V2 cases or any real holdout.
