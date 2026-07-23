# Bauer RAG V2 private rollout

Date: 2026-07-23

Status: deployed for private evaluation; not promoted to the normal Bauer Agent.

## Scope

The rollout added Bauer RAG V2 to the existing RAG API, PostgreSQL/pgvector database, and
LibreChat deployment. It did not add a Railway service, replace V1 data, alter the Test Archive
Agent, or change the normal Bauer Agent's route.

The implementation worktree was created from
`2815a8f2ba1b7db04d40a80934544f092e54518b` on `codex/bauer-rag-v2`.
The final reproducible development smoke records evaluated source commit
`bf3c3cb0a96f054ad064824623baf25b4785005b`.

## Live deployment

| Item | Value |
| --- | --- |
| Railway project/environment | `bk-RAG-test` / `testing` |
| RAG API service | `b8069ba5-2c06-4f90-9a72-f854a596ba42` |
| Active RAG deployment | `4c779152-def4-4c9a-a245-3e205a603547` |
| LibreChat service | `c7f709a4-ec29-4ead-b21f-34439cdfb9e3` |
| Active LibreChat deployment | `1142dfd1-fc50-477a-8cc4-7f226cc212c2` |
| RAG rollback deployment | `5d434f9f-1111-4c64-85d6-4cb0f0ae8d1b` |
| LibreChat rollback deployment | `154edb4c-54f1-496f-ab4d-60aac87caef8` |
| Bauer Twin deployment | `b753b52e-c0c4-41f1-b824-f01a1940347f` |
| Wiki source commit | `48c8edb` in `wiki-rapiddraft` |
| Wiki production deploy | `6a622ede5870253a6411ed8f` |
| Wiki URL | `https://wiki.rapiddraft.ai/librechat/` |

An intermediate RAG candidate, `146b47dd-0fd8-444a-8266-acab0166b629`, was accidentally built
from the repository root and failed its health check because it used the Bauer Twin image without
that service's API key. It never became healthy or served traffic. Subsequent RAG deployments used
`services/rag-api-custom` as the explicit build root.

## Agent and authorization boundary

| Agent | ID | Route | Files | Access |
| --- | --- | --- | ---: | --- |
| Bauer Kompressoren | `agent_Z8A2LtQWLeP4KuUDbvqZL` | V1 | 373 | Existing private Bauer group |
| Bauer Kompressoren - RAG v2 Test | `agent_pmPMcA25UXS7vznUaz-DU` | V2 | 373 | Same private Bauer group |
| Test Archive | `agent_QnRNYPGlShuSnY0CYgQnm` | V1 | 41 | Existing private Test Archive group |

The private V2 Agent has the same provider, model parameters, instructions, tools, and exact Bauer
file-ID set as the normal Agent. Server-side allow-lists select V2 only for the private Agent.
RAG namespace, file-ID authorization, administrator diagnostics, and final response filtering
remain fail-closed.

## Active V2 index

| Field | Value |
| --- | --- |
| Index run | `7e23f6e1-d876-4da0-a677-9a3cdb37db36` |
| Namespace | `agent_pmPMcA25UXS7vznUaz-DU` |
| Index version | `bauer-rag-v2-2026-07` |
| Files | 373 |
| Extractor | `bauer-deterministic-v2.1` |
| Activation | Atomic and active |

The index is additive and versioned. V1 chunks, vectors, S3 files, and MongoDB associations were
not changed. A failed staging run, `52c1d53f-843e-4543-972d-484d31637966`, never activated; its
DATE-binding defect was fixed before the successful run.

## Retrieval implementation

V2 runs authorized exact-metadata, PostgreSQL lexical, and pgvector semantic candidate searches,
then applies reciprocal-rank fusion, source/location deduplication, bounded reranking, and response
filtering. Table-aware chunks preserve row labels, values, headers, units, footnotes, page, and
source provenance. Query analysis recognizes document numbers, part numbers, standards, model
identifiers, quoted values, units, table intent, multilingual stopwords, document lookup, and
maximum-pressure intent.

The optional remote reranker is not configured in this deployment. The measured smoke run used
the documented deterministic multilingual fallback. This is an explicit evaluation limitation,
not an unreported service failure.

## Retrieval smoke

The final development smoke is
`../evals/bauer-rag-v2/reports/retrieval-smoke-20260723-05-score.json`. It contains 30 development
cases with one serial V1/V2 repetition, 60 successful HTTP calls, and no authorization violations.
Twenty cases have exact evidence-location gold and contribute to the reported retrieval metrics.

Artifact SHA-256:

- Score report: `b929e17c24a7955cd8ec0457898d22a6121e5cf6420aa877df24281e62330a5b`
- Raw run: `fdaf707c0ddf5000712ef176b583b07ae2030fc110e4a0cbe2579177ac534d4a`

| Measure | V1 | V2 |
| --- | ---: | ---: |
| Recall@1 | 0.0000 | 0.4417 |
| Recall@3 | 0.0000 | 0.5667 |
| Recall@5 | 0.0000 | 0.6167 |
| MRR | 0.0000 | 0.5517 |
| Exact metadata success | 0.0000 | 0.6667 |
| Table integrity | 0.0000 | 0.3125 |
| Duplicate rate | 0.0000 | 0.0521 |
| p50 latency | 277.84 ms | 1193.24 ms |
| p95 latency | 379.30 ms | 1519.11 ms |

Measured retrieval gates:

| Gate | Result |
| --- | --- |
| Exact identifier/document lookup >= 95% | Fail: 66.67% against provisional locations |
| Retrieval p95 < 2 seconds | Pass: 1.51911 seconds |
| Authorization violations = 0 | Pass |
| Recall@5 improvement >= 0.05 | Pass: +0.6167 |
| Worst category regression >= -0.05 | Pass: 0.0 |

The first smoke directory (`retrieval-smoke-20260723-01`) is intentionally preserved as an
incomplete run: it stopped when the runner selected the wrong language field for a German prompt.
The runner was fixed and covered by a harness test before complete runs 02 through 04.

## Verification

Final verification after the live deployments:

| Check | Result |
| --- | --- |
| LibreChat `/health` | HTTP 200, `OK` |
| RAG API `/health` | `UP` |
| Bauer Twin `/health` | `ok`; 12 projects, 75 parts, 8 documents |
| RAG `/v2/status` | Enabled; schema ready; one namespace allow-listed |
| Active index-run lookup | Active; expected file count 373 |
| V1 regression query | HTTP 200; 10 results |
| V2 exact-document query | HTTP 200; V2 route; `N39771` document ranked first |
| Normal Bauer/Test Archive namespace sent to V2 | HTTP 403 |
| Test Archive file sent inside private Bauer V2 request | Zero results; explicit safe refusal |
| Agent file-set comparison | Both Bauer Agents: 373 files, identical file-set SHA-256 |
| RAG + evaluation Python tests | 48 passed |
| Bauer Twin Python tests | 24 passed |
| LibreChat Node tests | 9 passed |
| PowerShell parser checks | 3 scripts, zero parse errors |
| Corpus inspection | 373 documents; 33/33 structural gold checks; no critical failures |

## Promotion decision

Promotion is blocked. The gold manifest is still
`provisional_requires_bauer_adjudication`, the exact-location gate is below target, a remote
reranker has not been selected or benchmarked, and the required five-run end-to-end and blind
holdout comparisons have not been run. The normal Bauer Agent therefore remains on V1.

See `../evals/bauer-rag-v2/reports/gold-adjudication-20260723.md` for the disputed evidence
locations that must be resolved before locking the formal evaluation.

## Rollback

The fastest rollback is to remove only `agent_pmPMcA25UXS7vznUaz-DU` from LibreChat's
`RAG_V2_AGENT_IDS` selector and redeploy LibreChat. This sends the private Agent through V1 without
touching data. If the RAG overlay itself is unhealthy, restore the recorded RAG deployment above.
The additive `bauer_rag_v2` schema can remain for diagnosis and must not be dropped as part of an
incident rollback.
