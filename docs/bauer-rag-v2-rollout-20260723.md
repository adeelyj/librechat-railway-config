# Bauer RAG V2 private rollout

Date: 2026-07-23

Status: deployed for private evaluation; not promoted to the normal Bauer Agent.

## Scope

The rollout added Bauer RAG V2 to the existing RAG API, PostgreSQL/pgvector database, and
LibreChat deployment. It did not add a Railway service, replace V1 data, alter the Test Archive
Agent, or change the normal Bauer Agent's route.

The implementation worktree was created from
`2815a8f2ba1b7db04d40a80934544f092e54518b` on `codex/bauer-rag-v2`.
The final reproducible development tuning record evaluated source commit
`26895703bf828344f458e2dec59f672f7aeb7608`.

## Live deployment

| Item | Value |
| --- | --- |
| Railway project/environment | `bk-RAG-test` / `testing` |
| RAG API service | `b8069ba5-2c06-4f90-9a72-f854a596ba42` |
| Active RAG deployment | `29570c90-de9e-4fd7-be48-dd1cfdeb2414` |
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
| Index run | `351acc7b-2735-4598-934e-dcd200f0e0b4` |
| Namespace | `agent_pmPMcA25UXS7vznUaz-DU` |
| Index version | `bauer-rag-v2-2026-07-r2` |
| Files | 373 |
| Extractor | `bauer-deterministic-v2.2` |
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

The optional remote reranker is not configured in this deployment. A local
`bge-reranker-v2-m3` Q5_0 cross-encoder was benchmarked against the deterministic fallback on an
immutable development candidate set. It reduced exact lookup from `0.8889` to `0.6667` without
improving Recall@5 (`0.8167` for both), so the deterministic fallback was retained. The benchmark
and latency measurements are in
`../evals/bauer-rag-v2/reports/reranker-benchmark-20260723.json`.

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

## Interim gold review

At the user's request, Codex acted as interim reviewer for the six disputes recorded after the
smoke run. The source-by-source decisions are in
`../evals/bauer-rag-v2/reports/gold-interim-review-20260723.md`. The resulting manifest status is
`interim_codex_reviewed_requires_bauer_signoff`; it is deliberately not `verified`.

The same immutable run was rescored without rerunning retrieval:
`../evals/bauer-rag-v2/reports/retrieval-smoke-20260723-05-interim-gold-score.json`
(SHA-256 `51ff7aa7b49fa57ad2e0edb48e07d198fb934f398bb48f9e5048da38fd88a78e`).

| Measure | V1 | V2 after interim adjudication |
| --- | ---: | ---: |
| Recall@1 | 0.0000 | 0.5417 |
| Recall@3 | 0.0000 | 0.7667 |
| Recall@5 | 0.0000 | 0.8167 |
| MRR | 0.0000 | 0.6850 |
| Exact metadata success | 0.0000 | 0.8889 |
| Table integrity | 0.0000 | 0.3750 |

Exact lookup improved from 66.67% to 88.89% because unreachable provisional labels were corrected.
It still fails the 95% gate because B07's canonical brochure row is rank 3 rather than rank 1
(eight of nine exact-category cases pass). Separately, B06 remains a complete-evidence retrieval
miss, and B29 correctly exposes a page-boundary defect: printed page 31 content is attributed to
page 30 by the current prose chunk.

The first smoke directory (`retrieval-smoke-20260723-01`) is intentionally preserved as an
incomplete run: it stopped when the runner selected the wrong language field for a German prompt.
The runner was fixed and covered by a harness test before complete runs 02 through 04.

## Development tuning result

The final development-only confirmation is
`../evals/bauer-rag-v2/reports/retrieval-tuning-20260723-09-score.json`. It evaluated the active
`r2` index with one serial V1/V2 repetition across all 30 development cases. The locked holdout
was not opened.

Artifact SHA-256:

- Score report: `7926b88b45ab3f78b0601ced56f13b53dabf0e02efe03a88d34ae93eca969038`
- Raw run: `8752dce299849ff088f0669008e4b61822375656ec7344ac0bf11fcd03b4f1a6`
- Reranker benchmark:
  `9e0274718f12f66b3f341db0709fef7721d963d3915ebc449d314a59b4717d4c`

| Measure | V1 | V2 |
| --- | ---: | ---: |
| Recall@1 | 0.0000 | 0.7583 |
| Recall@3 | 0.0000 | 0.8667 |
| Recall@5 | 0.0000 | 0.8667 |
| Recall@10 | 0.0000 | 0.9167 |
| MRR | 0.0000 | 0.8800 |
| Exact metadata success | 0.0000 | 1.0000 |
| Table integrity | 0.0000 | 0.6923 |
| Duplicate rate | 0.0000 | 0.0850 |
| p50 latency | 438.66 ms | 1420.21 ms |
| p95 latency | 970.04 ms | 2655.97 ms |

The original tuning targets are resolved: B06's three canonical locations rank 1/2/3, B07 ranks
the canonical versioned table row first, and B29 attributes the range to printed page 31 and ranks
it first. All 60 retrieval calls succeeded and authorization violations remained zero.

Measured retrieval gates:

| Gate | Result |
| --- | --- |
| Exact identifier/document lookup >= 95% | Pass: 100% |
| Retrieval p95 < 2 seconds | Fail: 2.65597 seconds |
| Authorization violations = 0 | Pass |
| Recall@5 improvement >= 0.05 | Pass: +0.8667 |
| Worst category regression >= -0.05 | Pass: 0.0 |

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
| RAG Python tests | 48 passed |
| Evaluation harness tests | 13 passed |
| Bauer Twin Python tests | 24 passed |
| LibreChat Node tests | 9 passed |
| PowerShell parser checks | 3 scripts, zero parse errors |
| Corpus inspection | 373 documents; 35/35 structural gold checks; no critical failures |

## Promotion decision

Promotion is blocked. Exact lookup and authorization isolation now pass, but retrieval p95 remains
above the two-second gate. The gold manifest has only interim review and still requires independent
Bauer sign-off; the required five-run retrieval/end-to-end comparison and blind holdout have not
been run. The normal Bauer Agent therefore remains on V1.

See `../evals/bauer-rag-v2/reports/gold-interim-review-20260723.md` for the interim decisions and
remaining sign-off boundary.

## Rollback

The fastest rollback is to remove only `agent_pmPMcA25UXS7vznUaz-DU` from LibreChat's
`RAG_V2_AGENT_IDS` selector and redeploy LibreChat. This sends the private Agent through V1 without
touching data. If the RAG overlay itself is unhealthy, restore the recorded RAG deployment above.
The additive `bauer_rag_v2` schema can remain for diagnosis and must not be dropped as part of an
incident rollback.
