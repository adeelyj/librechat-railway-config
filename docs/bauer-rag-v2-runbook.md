# Bauer RAG V2 runbook

Status: implemented locally; not deployed or promoted.

The V2 overlay is backward-compatible and keeps the current Bauer Agent, Test Archive Agent, V1
index, S3 objects, and MongoDB associations unchanged. Creating the private Agent, changing Railway
variables, deploying, indexing, activating, or promoting requires an explicit production-change
approval.

## Frozen baseline

The read-only baseline is under `evals/bauer-rag-v2/baselines/`. It records the service deployment
IDs, V1 Agent ID, Test Archive Agent ID, 373-file allow-list, filenames, checksums, source
provenance, and capture source. The local parser report is
`evals/bauer-rag-v2/reports/corpus-inspection.json`.

The checked-in gold set is provisional. A Bauer reviewer must adjudicate it and change
`gold_status` to `verified` before the scorer can permit promotion. The corpus inspection verifies
all 33 currently specified evidence locations against the frozen sources, but that engineering
check does not replace human review of answer claims, constraints, and source interpretation.

## Pre-deployment checks

From the repository root:

```powershell
node --test services/librechat-custom/tests/fileSearchBatch.test.js
python -m unittest discover -s services/rag-api-custom/tests -p 'test_*.py' -v
python -m unittest discover -s evals/bauer-rag-v2/tests -p 'test_*.py' -v
python scripts/inspect-bauer-rag-v2-corpus.py
python services/rag-api-custom/apply_batch_patch.py --root <extracted-pinned-image-root>
```

The last command must use files extracted from the exact image digest in
`services/rag-api-custom/UPSTREAM.md`. A checksum mismatch is a hard stop.

## Approved deployment sequence

1. Record fresh Railway deployment IDs and the rollback commit with
   `scripts/export-bauer-rag-v2-baseline.ps1`.
2. Deploy the backward-compatible RAG overlay to the existing RAG API service. Do not add a
   service or move a live deployment branch without approval.
3. Confirm V1 health and `/query_multiple` behavior before enabling any V2 Agent. Check
   `/v2/status` as an administrator; a schema/extension error disables V2 with HTTP 503 while V1
   remains available.
4. Run `scripts/provision-bauer-rag-v2-agent.ps1` without `-Apply` and review the plan. With
   approval, rerun with `-Apply`. It clones the V1 model/settings/tools and exact 373 file IDs,
   attaches the same group, remains private, and leaves V1 untouched.
5. Set both `BAUER_RAG_V2_NAMESPACE_IDS` on RAG API and `RAG_V2_AGENT_IDS` on LibreChat to only the
   new private Agent ID. Set the RAG administrator IDs as needed.
6. Deploy the LibreChat overlay and verify that only the private Agent logs `retrievalRoute=v2`.
7. Stage the index with `scripts/index-bauer-rag-v2.ps1`. The script refuses the normal V1 Agent
   namespace. It derives the private V2 namespace from `tmp/bauer-rag-v2-agent.json`, or accepts
   an explicit `-Namespace`.
8. Inspect the run status and sampled source records. Activate only with `-Activate`; activation is
   atomic and requires all 373 manifest entries.
9. Run isolation, V1 regression, V2 development, and then tuning-locked holdout checks.

The index command creates external state even without `-Activate`, so `-WhatIf` is the safe first
run. A failed run is terminal; start a new run. Checksum-identical inactive documents are reused.

## Evaluation

Credentials are read only from the named environment variables and are never written to raw runs.
Run directories are create-only.

```powershell
$env:RAG_API_TOKEN = '<short-lived token>'
python evals/bauer-rag-v2/runners/retrieval.py `
  --run-id retrieval-dev-001 `
  --v1-entity-id agent_Z8A2LtQWLeP4KuUDbvqZL `
  --v2-entity-id <private-v2-agent-id> `
  --v2-debug

$env:LIBRECHAT_TOKEN = '<short-lived token>'
python evals/bauer-rag-v2/runners/end_to_end.py `
  --run-id e2e-dev-001 `
  --v1-agent-id agent_Z8A2LtQWLeP4KuUDbvqZL `
  --v2-agent-id <private-v2-agent-id>

python evals/bauer-rag-v2/scorers/score.py `
  --retrieval-run evals/bauer-rag-v2/raw-runs/retrieval-dev-001/run.json `
  --answer-run evals/bauer-rag-v2/raw-runs/e2e-dev-001/run.json
```

Both runners execute serially, alternate V1/V2 order, and default to five repetitions. End-to-end
runs use a fresh conversation and remove only their own evaluation conversations. Holdout requires
`--acknowledge-locked-holdout`.

Use `frozen_evidence.py` with the Local AI OpenAI-compatible completion endpoint for Qwen. Its
`--export-only` mode produces exact immutable evidence packages. Use `reference_packages.py` and
`import_reference_answers.py` for the end-to-end Codex reference; Codex must not use web or outside
knowledge. Gold, not Codex, is the judge.

## Promotion gate

The scorer emits all measured thresholds, hard failures, authorization violations, and gold
status. Promotion is blocked unless a combined retrieval/end-to-end report passes every gate and
the gold status is `verified`.

Only then may a separately approved change add the normal Bauer Agent ID to the V2 selector.
Retain a private V1 rollback Agent and the V1 deployment/index.

## Rollback

The fastest rollback does not touch data:

1. Remove the affected Agent ID from LibreChat `RAG_V2_AGENT_IDS` and redeploy/restart LibreChat.
   The server-side selector immediately uses `/query_multiple`.
2. If the RAG overlay itself is unhealthy, restore the recorded RAG deployment/commit from
   `baselines/deployment.json`.
3. Verify RAG health, normal Bauer V1 retrieval, Test Archive isolation, and a known V1 query.
4. Keep the `bauer_rag_v2` schema for diagnosis. It is isolated and inactive data is harmless.

Do not drop the schema during an incident. A later schema removal is a separate destructive
maintenance change requiring a backup, retention decision, and explicit approval. V1 rollback
never requires deleting S3 objects, MongoDB records, or V1 vector rows.
