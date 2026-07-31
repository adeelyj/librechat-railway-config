# Bauer RAG V4 private-shadow evidence — 2026-07-29

Status: authorized private-shadow batch complete. The authenticated four-way
development benchmark and public-development scoring are complete. Production
promotion is not authorized.

Current-state note: this document preserves the original 2026-07-29 release and benchmark. The
later answer-quality repair, 374-source candidate, exact five live outputs, and current deployment
identity are documented in `docs/bauer-rag-v4-answer-quality-evidence-20260731.md`. The original
four-way outputs below remain historical evidence and were not silently relabeled as repaired V4.

## Decision boundary

- V4 is available through the private LibreChat Agent
  `Bauer Kompressoren - RAG V4 Private Shadow`
  (`agent_TEEBDBmMxnQwL10UjILhw`).
- The V4 API loads only fixed candidate release
  `45abb96f-c555-4a65-92a4-b6ee3be09da9`.
- `active_release_pointer_used` is `false`; the V4 active-release pointer is
  empty.
- No public selector, production release, ONIX adapter, or V1/V2/V3 mutation
  is part of this batch.

## Immutable lineage and deployment

| Item | Evidence |
| --- | --- |
| V3 ancestor | `11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59` |
| V4 branch | `codex/bauer-rag-v4` |
| V4 backend commit | `9e48fddabc2e5bbc0053eae7d4bf75b8543a8671` |
| V4 backend deployment | `5a6a8c88-6df5-4689-873b-4e7dc1ca063c` (`SUCCESS`) |
| LibreChat overlay commit | `1008ac53741a7e6888005651180dafd1abfd7d46` |
| LibreChat deployment | `d67e914e-b769-471d-8093-94389382823b` (`SUCCESS`) |
| Candidate public label | `bauer-rag-v4-private-20260729-r1` |

The deployed `/ready/v4` response reports the exact backend commit, 373
artifacts, 25,093 projections, the fixed candidate release, and
`active_release_pointer_used: false`.

## WP0–WP8 engineering evidence

- Host-neutral request, response, release, evidence, and authorization
  contracts are source-controlled with generated JSON Schema and OpenAPI 3.1.
- The complete user question remains immutable and is the reranking/answering
  authority; `search_hint` is a separate low-weight expansion that cannot
  displace requirement-derived candidates.
- Reviewed difficult-document fixtures gate canonical compilation before
  retrieval.
- Canonical projections include 53,483 blocks, 68,285 cells, 11,554 typed
  facts, and 1,932 tables.
- 22,840 embedding identities are stored for reuse; the release contains
  25,093 retrieval projections.
- Candidate generation has exactly three authorization-filtered families:
  exact/structured, lexical/trigram, and dense.
- One transparent reranking stage feeds requested-field evidence coverage,
  grounded rendering, and completion/support validation.
- Final validation passes 58/58 V4 service tests, 3/3 reviewed-fixture
  integrity tests, and 41/41 LibreChat adapter/authorization boundary tests.

## PostgreSQL and recovery rehearsal

The isolated Fedora PostgreSQL rehearsal applied and rolled back all seven
V4 migrations. It verified five non-login/runtime roles, 36 policies, RLS on
21 tables, release pinning, destructive teardown ordering, and final cleanup.
Railway uses the isolated `bauer_rag_v4` schema and `v4/` object prefix on
reused private capacity.

Normal rollback restores the captured LibreChat and V3 API deployments; it
does not delete V4 data. The fixed release remains inert because no active
pointer exists. Destructive database down migrations are reserved for a
separately scheduled teardown.

## Direct public-development verification

The final direct B01–B30 run against backend commit `1f37865` recorded:

- 30/30 observations;
- 20 `complete`;
- 3 intentional `partial` results where required synthetic records are absent;
- 7 correct `not_found` results;
- zero answer-validation failures.

Focused live checks additionally verify:

- B06: the exact public-development question returns the documented 525 bar
  compressor maximum, distinguishes the 520 bar water-cooled booster range,
  and preserves the lower shutdown-pressure definition even when LibreChat
  issues multiple search hints;
- B11: no documented B-SAFE approval for Nitrox at 300 bar; the headline
  limits Nitrox to 200 bar;
- B21: the 300/200 headline and 410/225/330 technical-data wording remain
  explicitly unresolved and are not converted into approval;
- B19: B-KOOL III uses the complete current 350/550 bar row and the
  200–700, 200–650, and 200–420 l/min flow ranges;
- B24: N7698 is associated with the exact adjacent compressor-block list;
- B25: B-CLOUD/B-APP access and the documented B-CONTROL MICRO +Net 3.73
  requirement are returned without unrelated evidence;
- B30: the original German prompt retains and returns the requested delivery
  value.

## 2026-07-31 company-overview correction

The DeepSeek V4 Agent exposed a development defect for the broad prompt
`what does bauer kompressoren do`. The failure was localized before answer
tuning: task analysis reduced the request to low-information terms, which
allowed a supplier-contract page to displace available company-overview
evidence. The general renderer then exposed its internal evidence format.

Commit `68f8ca2530ff59d9655cd0ea534f16340e24c0d6` adds a V4-only company/portfolio
intent, three explicit coverage fields, strict evidence requirements, and a
concise grounded renderer. The deployed service now returns `complete` with
validation passing for both the exact incident prompt and
`list hte products from bayuer`. It cites the product overview, industrial
portfolio, and fuel-gas sources instead of the supplier contract.

The source-controlled regression is
`evals/bauer-rag-v4/cases/company-overview-regression.json`; live evidence is
`evidence/bauer-rag-v4-company-overview-regression-20260731.json`. The existing
B01-B30 analyzer routes are unchanged. The historical 120-observation
benchmark below was not relabeled or rerun for this targeted correction.

### Bare-company prompt and DeepSeek tool-loop follow-up

The later prompt `list the products of bauer` exposed two independent gaps. The analyzer required
an explicit `Bauer Kompressoren` alias, so the bare company reference fell back to generic terms
and again selected supplier-contract evidence. After correcting that representation boundary,
the backend returned the concise validated company answer, but DeepSeek continued issuing
redundant file searches because V4 was allowed multiple Agent tool rounds.

Backend commit `9e48fddabc2e5bbc0053eae7d4bf75b8543a8671` adds the literal prompt as public regression
`V4-R03` and restricts bare `Bauer` routing to broad overview/portfolio requests; named-product
and numerical requests remain on their specific planners. LibreChat overlay commit
`1008ac53741a7e6888005651180dafd1abfd7d46` ends the allow-listed V4 graph after its first tool
round and materializes the validated backend answer.

The authenticated DeepSeek probe improved from eight file-search calls and 408.8 seconds before
the boundary correction to two parallel calls in one tool round and 95.6 seconds after it. The
post-correction result contains all four required company/portfolio statements, contains neither
the internal requested-topic label nor the supplier clause, and deletes its temporary
conversation. Exact evidence is in
`evidence/bauer-rag-v4-bare-bauer-deepseek-regression-20260731.json`.

### Company-location incident correction

The later exact prompt `where is bauer based` incorrectly returned supplier and place-of-performance
contract fragments. The corpus already contained explicit English evidence for BAUER KOMPRESSOREN
GmbH at Stäblistr. 8, 81477 Munich, Germany; the defect was the generic planner and permissive
catch-all coverage, not missing source data.

Commit `46e81b5ce1859a20d4824d4262889d84ca64d06d` introduces an entity-specific company-location
contract and removes raw-evidence success from the unrecognized catch-all. The five-case public
regression is `evals/bauer-rag-v4/cases/company-facts-regression.json`. All 62 V4 tests pass. The
final private-shadow API deployment `4297cb41-ee95-48fe-b549-d4c627f67c73` is healthy and remains
pinned to fixed release `45abb96f-c555-4a65-92a4-b6ee3be09da9`; LibreChat and the release data were
not changed.

The exact deployed backend prompt returns `complete`, one `company_location` coverage item, and one
citation to `bauer_amfile_13.pdf`. The authenticated DeepSeek Agent returns the same final answer in
one tool round and deletes its temporary conversation. An unsupported founder question returns
`not_found`, a concise explanation, and zero citations. Machine-readable evidence is
`evidence/bauer-rag-v4-company-facts-regression-20260731.json`.

## Evidence inventory

| Evidence | SHA-256 |
| --- | --- |
| `evidence/bauer-rag-v4-company-facts-regression-20260731.json` | `28fdd494a7453a1b192bbd26215e075f6aa28022bd221eeb1d1a7751d29a44cc` |
| `tmp/v4-development/wp8-fedora-postgresql-7migrations.json` | `122d75b725cfd06ae849f13d64c7759ddbf34f92a07407926c8324a8778a5555` |
| `tmp/v4-deploy/evidence/v4-data-plane-ready.json` | `8eb795c11eb2d9f081f0bc2686584bf2145ed505ea84c51b1a9b44575863ea38` |
| `tmp/v4-deploy/evidence/v4-private-agent.json` | `7ef0eb30a9ec3dac4b36ae605f6b516d843c053edbadc0f721597278ebbed892` |
| `tmp/v4-deploy/evidence/wp9-rollback-baseline.json` | `f1dd2d16ae9dc3a621152fa454bdc632ff564bfaf8594ae43883cffcbdb3c3a1` |
| `tmp/v4-deploy/evidence/v4-direct-development-sweep-1f37865.jsonl` | `261c5ac7a893d2dc4e22a8d31ee60866ce28d69f0c47edd08559ef95a7724550` |
| `tmp/v4-deploy/evidence/focused-v4-b06-public-1f37865.stdout.json` | `caf40003a733ca3fb618309ea9ebadaeb2e547f38a58a245d6117293e717b62e` |
| `tmp/v4-deploy/evidence/focused-v4-b11-84988e2.stdout.json` | `0361dfb363ca31927a6570c03880b43414b9e68ead5f05acf525bd263d2fec3d` |
| `tmp/v4-deploy/evidence/librechat-four-way-development-benchmark-final-1f37865-20260729.json` | `0385c8231cbdac4bf3a08e61c249da1419b0370a3ceb4a8cba90b5622c2e1ddb` |
| `tmp/v4-deploy/evidence/librechat-four-way-development-benchmark-final-1f37865-20260729-public-score.json` | `a90ff4cf6ba8e08fb0c6ec3ba74be20d2184fb1fca5d5253f0c628b5d73ebf75` |

## LibreChat verification

Authenticated private-Agent probes returned HTTP 200, used only `file_search`,
retained citation evidence, returned the B06 pressure reconciliation and B11
safety conclusion, and deleted their temporary conversations.

The final comparable benchmark uses one browser-shaped authenticated session,
normal in-session refresh, a fresh deleted conversation per observation, the
same 30 public-development prompts, and V1/V2/V3/V4. It records all 120 exact
visible-output observations and is scored only with
`scripts/deployment/score_v4_public_development.py`. The benchmark retained
118 answers and deleted all 120 temporary conversations. Its only execution
errors were V1 timeouts on B16 and B21; V4 completed all 30 observations
without an execution error.

| Measure | V1 | V2 | V3 private shadow | V4 private shadow |
| --- | ---: | ---: | ---: | ---: |
| Exact-answer accuracy | 0.0667 | 0.3333 | 0.1000 | **0.8333** |
| Claim-level correctness | 0.4070 | 0.7907 | 0.2965 | **0.9593** |
| Grounded-claim rate | 0.4070 | 0.7907 | 0.2965 | **0.9593** |
| Citation correctness/completeness | 0.9333 | 1.0000 | 1.0000 | **1.0000** |
| Source-type discipline | 1.0000 | 1.0000 | 1.0000 | **1.0000** |
| Constraint compliance | 0.6667 | 0.7333 | 0.7000 | **1.0000** |
| Safe-refusal accuracy | 0.2000 | 0.2000 | 0.1000 | **1.0000** |
| Fabrication findings | 8 | 8 | 9 | **0** |
| Hard failures | 28 | 20 | 27 | **5** |
| End-to-end p50 | 25.82 s | 24.65 s | 25.84 s | 26.95 s |
| End-to-end p95 | 208.90 s | 132.32 s | 64.60 s | 267.37 s |

The five remaining V4 exact-answer misses are B01, B02, B05, B12, and B14.
They are missing requested public-development signals, not authorization
violations, unsupported identifiers/numbers, citation defects, or execution
errors. The deterministic rubric SHA-256 is
`c687b5d10baa739c8eaa4b36e3394cbb926baccb2677aac071a061ce79eba207`.
It applies the same public-only semantic matching to all four systems.

The high V4 p95 is retained as measured evidence and is not hidden by the
stronger answer-quality results. It remains an optimization target if a later
promotion program is authorized.

## Holdout process warning

The benchmark harness reads only the B01–B30 development manifest and reports
`locked_holdout_opened: false`. The wider implementation task's holdout
process boundary was contaminated by accidental exposure to combined
gold/holdout-linked entries. These results are not valid sealed-holdout
evidence. Production promotion requires a freshly resealed holdout and an
independent evaluation.
