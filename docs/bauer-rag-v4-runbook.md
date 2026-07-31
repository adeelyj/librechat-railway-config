# Bauer RAG V4 private-shadow runbook

Status: implemented, independently reviewed, deployed, and live-verified as an authorized private
shadow. V4 is available through its private LibreChat Agent with DeepSeek, but it is not the active
production release and has not been owner-accepted.

Holdout warning: the development benchmark harness reads only B01-B30 and reports
`locked_holdout_opened: false`, but the wider implementation task's holdout process boundary was
contaminated. Promotion requires a freshly resealed holdout and an independent evaluation.

## Fixed deployment identity

| Item | Value |
| --- | --- |
| Railway project / environment | `bk-RAG-test` / `testing` |
| V4 tenant | `33738ad5-567c-4844-97bf-0941f1f6d36c` |
| V4 knowledge base | `fbd5c3ab-950e-4878-9d4b-41b0abfca1c7` |
| V4 candidate release | `ccb9e8ea-5894-405f-aac4-c4eae5b0d661` |
| Public release ID | `bauer-rag-v4-private-20260731-aq-r2` |
| Private LibreChat Agent | `agent_TEEBDBmMxnQwL10UjILhw` |
| Agent name | `Bauer Kompressoren - RAG V4 Private Shadow` |
| Backend commit | `84e63d76d7a91aa73fe963ad8a054761c5bde432` |
| Backend deployment | `4a8ce7b7-968e-480f-a879-09606af9c70e` |
| Worker deployment | `7f9806e1-32b8-4cb2-bfcb-ca9d2e9c8cf1` |
| LibreChat overlay commit | `e9c9f0916773059faf9d0c184e5c93540057c742` |
| LibreChat deployment | `301bef74-9f25-4428-bfc6-46d48123245c` |
| Source membership | 373 protected Bauer file IDs plus one reviewed synthetic demo source |
| Object prefix | isolated `v4/` prefix in the reused private bucket |

V4 deliberately does not use or mutate `active_release_pointers`. The API loads the fixed candidate
release from deployment configuration after that release reaches `ready`.

## Physical reuse and logical isolation

V4 reuses the existing private V3 PostgreSQL, bucket, migrator, worker, and API capacity. Its
database objects are confined to `bauer_rag_v4`, its own non-login/runtime roles and RLS policies,
its fixed release, and its `v4/` artifact prefix. V1, V2, and V3 Agents, data, routes, releases, and
benchmark evidence remain rollback-protected.

The shared API retains the frozen V3 routes and adds:

- `GET /ready/v4`
- `GET /version/v4`
- `POST /v4/answer`

The V4 answer route requires a short-lived scope signed for the distinct
`bauer-evidence-v4` audience, the fixed tenant/knowledge base, the private V4 Agent, and the exact
server-authorized source list.

## Access in LibreChat

1. Sign in to the existing private LibreChat deployment.
2. Open Agents and select `Bauer Kompressoren - RAG V4 Private Shadow`.
3. Ask the complete question. Do not shorten the question to keywords; LibreChat transports the
   complete user text separately from the model-generated search hint.
4. Treat `partial` and `not_found` as intentional coverage results. A missing field is never
   inferred to be zero, false, or approved.
5. Use the returned file/page/table citations for review.

Only the allow-listed V4 Agent selects `/v4/answer`. Its Agent model is DeepSeek; the backend now
returns the validated task-shaped answer rather than asking DeepSeek to turn raw evidence into an
answer. V1, V2, V3, conversation uploads, and other Agents retain their existing routes.

## Compilation and release checks

Run status without emitting credentials:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\deployment\Invoke-BauerV4DataPlane.ps1 `
  -Phase Status `
  -OutputPath tmp\v4-deploy\evidence\v4-data-plane-status.json
```

A release is eligible for `MarkReady` only when all of these are true:

- 374 release members, compiled artifacts, canonical documents, and succeeded jobs;
- zero queued, running, failed, or dead jobs;
- zero active-release pointers;
- one passing named public-development evaluation with its exact suite digest and metrics.

The current release has 25,131 search projections, 53,486 blocks, 68,545 cells, 11,578 typed
facts, and 1,933 tables. Its embedding cache contains 22,878 identities: all 22,840 V3-compatible
identities were reused and only 38 identities were added for the new synthetic source.

The compiler tries the two native V4 PDF representations first. Only a PDF for which both are
empty may use the pinned V3 OCR engine through the V4 adapter. OCR output is translated into V4
blocks, tables, typed facts, and exact page/cell provenance; it does not bypass V4 quality gates.

## Failure recovery

- A transient compilation failure is retried by the PostgreSQL lease queue.
- A dead job is diagnosable by safe error type/fingerprint and media type; request bodies,
  credentials, and connection strings are not persisted in status evidence.
- `RetryDead` is allowed only while the release is `building`.
- `ResetBuild` deletes only disposable artifacts for the unpointed V4 candidate, clears its
  embedding cache, reseals the compiler identity, and requeues the same 374 immutable sources.
- API startup fails closed unless the exact configured release is `ready` and artifact accounting
  equals release membership.
- LibreChat rejects a V4 answer if validation did not pass or a citation lies outside its
  server-authorized file list.

## Rollback

Rollback does not require deleting V4 data:

1. To undo the answer-quality overlay and validation attestation, restore LibreChat deployment
   `9e867008-6cc6-4fa3-9c37-cdab82892195`.
2. To undo the answer-quality API repair, restore private API deployment
   `4297cb41-ee95-48fe-b549-d4c627f67c73`.
3. To undo the 374-source r2 compiler worker, restore worker deployment
   `250827d4-dcfc-4e39-a149-53bc0a290223`.
4. The following older restore points remain available for the pre-answer-quality incident fixes.
5. To undo the company-location and fail-closed correction, restore private API deployment
   `5a6a8c88-6df5-4689-873b-4e7dc1ca063c`.
6. To undo only the bare-`Bauer` portfolio routing correction, restore private API deployment
   `a216621d-729e-48ce-9617-7f3d8557ae17`.
7. To undo the earlier 2026-07-31 company-overview correction, restore private API deployment
   `c2bf14c8-3e71-4d21-b85a-67f122a03ffc`. The fixed V4 release and LibreChat adapter are unchanged.
8. Restore LibreChat deployment `ae7a03a5-3d5d-4b1f-85f4-ce65e2d54382` to remove the V4 adapter
   while retaining V1/V2/V3 behavior.
9. Restore private API deployment `13e2235d-a8c4-4103-bdb8-1f624a716026` to return to the frozen
   V3-only API image.
10. Leave the V4 schema and objects in place for evidence preservation. The active pointer remains
   empty, so retained V4 data cannot become production-active.

Database rollback scripts exist for all seven V4 migrations and are for an explicitly scheduled
destructive teardown only. They are not the normal application rollback.

The 2026-07-31 Fedora host was unreachable for a fresh rehearsal. No migration or schema file
changed in the answer-quality repair. The exact LF-normalized checksums of all seven migrations
match the previous PostgreSQL 18.3 rehearsal that passed two forward/rollback cycles, roles, RLS,
release pinning, rollback, failure recovery, and cleanup. The checksum-bound carry-forward record
is `evidence/bauer-rag-v4-answer-quality/fedora-operational-rehearsal-carry-forward.json`.

## Current five-case answer-quality gate

The current bounded gate uses the fixed seeded random public-development selection B06, B17, B10,
B19, and B03. It runs the actual DeepSeek-backed private V4 Agent through LibreChat and compares
the exact visible answers with preserved V1, V2, V3, and historical V4 outputs. It does not rerun
all 30 cases and does not read the locked holdout.

Publish the exact attested run and rerun the source-controlled comparative hard stop:

```powershell
python scripts\deployment\publish_v4_live_five.py `
  --run tmp\v4-deploy\evidence\librechat-five-case-v4-answer-quality-20260731-attested.json `
  --evaluation tmp\v4-deploy\evidence\live-five-case-comparative-evaluation.json `
  --status tmp\v4-deploy\evidence\v4-answer-quality-final-status.json `
  --independent-review evidence\bauer-rag-v4-answer-quality\independent-claude-final-review.md `
  --independent-review evidence\bauer-rag-v4-answer-quality\independent-claude-release-persistence-review.md `
  --independent-review evidence\bauer-rag-v4-answer-quality\independent-claude-bdetection-coverage-review.md

python scripts\deployment\evaluate_v4_answer_quality_five.py `
  --baseline evidence\bauer-rag-v4-answer-quality\baseline-five-case-v1-v4.json `
  --run evidence\bauer-rag-v4-answer-quality\live-five-case-v4-attested.json `
  --output evidence\bauer-rag-v4-answer-quality\live-five-case-comparative-evaluation.json `
  --markdown evidence\bauer-rag-v4-answer-quality\live-five-case-comparative-evaluation.md `
  --independent-review evidence\bauer-rag-v4-answer-quality\independent-claude-final-review.md `
  --independent-review evidence\bauer-rag-v4-answer-quality\independent-claude-release-persistence-review.md `
  --independent-review evidence\bauer-rag-v4-answer-quality\independent-claude-bdetection-coverage-review.md
```

The live result is 5/5. Every case is complete, validator-passed, zero-repair, citation-backed,
and 16/16 under its task-specific hard stop. The prior-best scores for the same cases were 12,
12, 13, 16, and 11. Passing means the repaired V4 met or exceeded each prior best on this bounded
sample; it does not establish the other 25 cases, a sealed holdout, production promotion, or owner
acceptance.

The functional verification after the final coverage repair is 77/77 V4 Python tests and 43/43
LibreChat fail-closed/authorization tests. Three independent read-only Claude reviews found the
initial synthesis-boundary defect, confirmed its repair, reviewed multi-release persistence, and
reported no issue with the final B-DETECTION coverage union.

## Development regression

The authenticated regression uses exactly one browser-shaped login, normal in-session token
refresh, a fresh deleted conversation per observation, the same 30 public development prompts, and
all four protected/private Agents. It records 120 exact visible outputs. The regression harness
does not read the holdout.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\deployment\Invoke-BauerV4ShadowRegression.ps1 `
  -V3AgentId agent_DzeT_ugU3tuZC_VCKB8Bh `
  -V4AgentId agent_TEEBDBmMxnQwL10UjILhw `
  -LibreChatOverlayCommit 1008ac53741a7e6888005651180dafd1abfd7d46 `
  -V4BackendCommit 9e48fddabc2e5bbc0053eae7d4bf75b8543a8671 `
  -OutputPath <development-benchmark-output>
```

Score the output only with the source-controlled public-development scorer:

```powershell
python scripts\deployment\score_v4_public_development.py `
  --benchmark <development-benchmark-output> `
  --output <development-score-output>
```

The scorer contains only B01-B30 public-development expectations and does not read the combined
gold or holdout files. The output and score are development evidence, not production promotion.
Any measured V4 defect is repaired in representation first, then candidate retrieval, then
reranking, then coverage/answering.

The public company-overview regression is
`evals/bauer-rag-v4/cases/company-overview-regression.json`. It covers the exact prompt
`what does bauer kompressoren do`, the observed typo prompt `list hte products from bayuer`, and
the bare-company prompt `list the products of bauer`.
Both must route to the three company coverage fields, produce a concise cited answer, and reject
the former internal evidence label and supplier-contract contamination. The corresponding live
correction evidence is
`evidence/bauer-rag-v4-company-overview-regression-20260731.json`.

For the allow-listed V4 Agent, LibreChat ends the graph after the first file-search tool round and
materializes the validated V4 answer. Providers may emit parallel searches inside that first
round, but cannot continue into additional search rounds. The exact bare-company DeepSeek probe
and before/after tool counts are recorded in
`evidence/bauer-rag-v4-bare-bauer-deepseek-regression-20260731.json`.

### Company-location and fail-closed regression

The prompt `where is bauer based` exposed a separate representation defect: the catch-all planner
discarded both `Bauer` and `based` as low-information terms, then allowed weak lexical overlap to
be rendered as raw contract evidence. Commit
`46e81b5ce1859a20d4824d4262889d84ca64d06d` adds an explicit, product-isolated company-location
field and requires evidence that contains the legal company name, street, postal code, city, and
country together. It also makes the unrecognized catch-all field fail closed with no citations and
no evidence-dump fallback.

The public regression suite is
`evals/bauer-rag-v4/cases/company-facts-regression.json`. It contains the exact incident prompt,
three English/German location variants, a named-product isolation assertion, and an unsupported
company-fact case. The final API deployment is `4297cb41-ee95-48fe-b549-d4c627f67c73`, pinned to
the commit above and the unchanged fixed release `45abb96f-c555-4a65-92a4-b6ee3be09da9`. Exact
backend and authenticated DeepSeek Agent results are recorded in
`evidence/bauer-rag-v4-company-facts-regression-20260731.json`.
