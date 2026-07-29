# Bauer RAG V4 development handover

Date: 2026-07-29

Status: V4 architecture, evidence review, delivery plan, and wiki documentation complete; V4 code
and infrastructure mutations have not started

Next task: implement the V4 local/Fedora foundation through the pre-Railway checkpoint

## 1. Objective

Build Bauer RAG V4 as the company-oriented successor informed by the measured V1, V2, and V3
results:

> Preserve V3's evidence authority, provenance, authorization, RLS, fixed releases, and physical
> infrastructure; recover V2's proven Bauer-aware representation behavior; replace V3's lossy
> question boundary, malformed projections, complex runtime fusion, one-tool restriction,
> evidence-dump fallback, and insufficient readiness gate.

The target is one host-neutral evidence and answer service used by LibreChat first and ONIX later.
V4 should reuse existing Railway compute where safe and add no always-on service unless measured
capacity or security evidence requires it.

## 2. Current authorization and operating model

The user approved this working model:

1. Codex performs all local repository work and isolated Fedora/PostgreSQL work autonomously.
2. Codex pauses once before the first V4 Railway or LibreChat mutation.
3. After one scoped authorization, Codex deploys the private V4 shadow, compiles all 373 sources,
   benchmarks V1/V2/V3/V4, corrects development failures, and updates documentation autonomously.
4. The user tests the completed private V4 Agent.
5. Holdout opening, production promotion, and actual ONIX integration remain separate decisions.

The prior explicit deployment authorization applied to V3, not V4. This handover does **not**
authorize V4 Railway mutations. Local work, code changes, tests, fixture construction, and isolated
Fedora database/migration/RLS rehearsal may proceed without another question.

Do not repeatedly request application authentications. Existing Railway and Fedora access should be
treated as available. When LibreChat regression is eventually authorized, use one browser-shaped
login and reuse the in-memory bearer token.

## 3. Mandatory read order

Read each item completely before changing code:

1. This handover:
   `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-development-handover-20260729.md`
2. Exhaustive V4 plan:
   `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-architecture-and-delivery-plan.md`
3. Principal planning prompt and constraints:
   `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-principal-planning-prompt.md`
4. V3 deployment handover:
   `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v3-shadow-deployment-handover-20260726.md`
5. Central V3 plan:
   `D:\02_Code\00_Project_Management_n_skills\01_tracks\rapiddraft-studio\plans\260724_bauer-rag-v3\README.md`
6. Central V3 handover:
   `D:\02_Code\00_Project_Management_n_skills\01_tracks\rapiddraft-studio\handover\260724_bauer-rag-v3.md`
7. V3 runbook:
   `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-runbook.md`
8. V3 architecture:
   `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-architecture.md`
9. V2 private rollout:
   `D:\02_Code\LibreChat_Setup-rag-v2\docs\bauer-rag-v2-rollout-20260723.md`
10. V2 findings and runbook:
    `D:\02_Code\LibreChat_Setup-rag-v2\docs\bauer-demo-findings.md`
    `D:\02_Code\LibreChat_Setup-rag-v2\docs\bauer-rag-v2-runbook.md`
11. Corrected exact LibreChat benchmark:
    `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-three-way-development-benchmark-20260727-corrected.json`
12. Corrected answer score:
    `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-three-way-development-benchmark-20260727-corrected-score.json`
13. Corrected direct retrieval benchmark:
    `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\three-way-retrieval-benchmark-after-11ea000.json`
14. Reader-oriented wiki review:
    `D:\02_Code\wiki-rapiddraft\docs_librechat\02_Knowledge_and_Models\RAG_V1_V2_V3_Architecture.md`
15. ONIX integration boundary:
    `D:\02_Code\wiki-rapiddraft\docs_librechat\05_Product_Integration\ONIX_RAG_Integration.md`

Never open the locked holdout.

## 4. Repositories and Git strategy

### Planning/deployment repository

```text
Path      D:\02_Code\LibreChat_Setup
Remote    https://github.com/adeelyj/librechat-railway-config.git
Branch    codex/optimize-librechat-file-search
```

The branch contains the V3 deployment handover and the V4 planning/handover documents. Pull the
remote branch and verify it is not behind before starting.

Unrelated local file to preserve and never stage implicitly:

```text
D:\02_Code\LibreChat_Setup\artifacts\Bauer_LibreChat_Benchmark_Prompts.docx
```

### V2 reference

```text
Path      D:\02_Code\LibreChat_Setup-rag-v2
Branch    codex/bauer-rag-v2
State     clean at handover creation
```

Use V2 as a read-only reference unless a deliberately isolated compatibility fix is required.

### V3 source baseline

```text
Path      D:\02_Code\LibreChat_Setup-rag-v3
Branch    codex/bauer-rag-v3
Commit    11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59
State     clean at handover creation
```

### LibreChat V3 overlay baseline

```text
Path      D:\02_Code\LibreChat_Setup\tmp\v3-deploy\librechat-overlay-9496
Commit    9496ad326df3e3e1daf8fda32b6add931af0a01f
```

### V4 implementation repository

It does not exist yet. Create:

```text
D:\02_Code\LibreChat_Setup-rag-v4
```

Derive it from the exact V3 Git history so retained security/release behavior has provenance. Use a
new `codex/bauer-rag-v4` branch. Do not implement V4 directly in the V3 worktree.

Commit by coherent work package. Keep generated source objects, corpora, credentials, runtime
evidence, and secret-bearing variables out of Git. Preserve unrelated dirty files and stage with
explicit paths.

### Wiki

```text
Path      D:\02_Code\wiki-rapiddraft
Remote    https://github.com/adeelyj/wiki-rapiddraft.git
Branch    codex/restore-pivot-integration
```

The wiki is stable product documentation, not the execution tracker. Do not add one page per work
package. Update the consolidated version review, exact benchmark page, and ONIX page only when the
reader-facing facts change.

## 5. Verified version findings

### Same-prompt answer benchmark

| Measure | V1 | V2 | V3 |
| --- | ---: | ---: | ---: |
| Claim correctness | 0.3333 | 0.6667 | 0.0952 |
| Constraint compliance | 1.0000 | 0.9667 | 0.7000 |
| Safe-refusal accuracy | 0.7500 | 0.7500 | 0.5000 |
| Fabrication findings | 34 | 57 | 19 |
| Hard failures | 23 | 22 | 16 |
| p50 | 31.29 s | 22.60 s | 22.49 s |
| p95 | 198.71 s | 105.11 s | 164.03 s |

### Direct retrieval benchmark

| Measure | V1 | V2 | Corrected V3 |
| --- | ---: | ---: | ---: |
| Recall@1 | 0 | 0.5083 | 0.0667 |
| Recall@5 | 0 | 0.6167 | 0.2667 |
| Recall@10 | 0 | 0.6167 | 0.3917 |
| Exact metadata | 0 | 0.5556 | 0 |
| Table integrity | 0 | 0.2308 | 0 |

Historical tuned V2 evidence reached Recall@5 `0.8667`, exact lookup `1.0000`, and table integrity
`0.6923`.

### Root causes that V4 must repair

1. The deployed V3 database has 120,950 table cells and zero populated `numeric_value`,
   `unit_raw`, or `unit_ucum` fields.
2. V3 did not reliably reconstruct multi-row headers, unit rows, captions, section paths, and
   footnotes.
3. Document metadata terms pointed to representative content instead of a complete document
   metadata evidence record.
4. The evidence prompt omitted fields required for exact metadata and technical table answers.
5. LibreChat's shortened `file_search` query replaced the complete original question.
6. The final boundary rejected multiple legitimate tool outputs.
7. Seventeen of 30 corrected V3 answers used an extractive evidence-dump fallback.
8. The readiness transition required one passing selected evaluation rather than the full named
   gate.
9. V2's more effective behavior came from Bauer-specific representation and repeated empirical
   tuning, not from a generally superior security architecture.

Do not add runtime weights to compensate for missing canonical data.

## 6. V4 architecture decision

Keep:

- immutable original source authority;
- source and artifact content identities;
- canonical evidence separated from disposable projections;
- fixed candidate releases;
- signed tenant/KB/principal/source scope;
- PostgreSQL roles and RLS;
- existing physical PostgreSQL, object bucket, API, worker, migrator, and Local AI capacity.

Adapt:

- V3 manifest, release, audit, and migration machinery;
- V2 table, identifier, unit, title, section, and footnote parsers;
- benchmark and authenticated LibreChat API clients;
- embeddings by exact model plus projection-text hash.

Replace:

- V3 table and metadata projections;
- many-channel runtime fusion;
- shortened-query answer contract;
- exactly-one-tool final boundary;
- evidence-dump fallback;
- arbitrary-one-evaluation readiness rule.

Initial candidate retrieval is limited to:

1. exact/structured;
2. lexical;
3. dense semantic;
4. one separate reranking stage.

The full original question is immutable request data. Search hints and subqueries may not replace
it.

## 7. Immediate implementation scope

The new task should execute WP0 through WP8 from the exhaustive plan:

### WP0 — Freeze the factual baseline

- verify repos, commits, evidence hashes, and dirty files;
- capture read-only Railway state without secrets;
- record the current V3 database population statistics.

### WP1 — Create V4 repository and contracts

- derive V4 from V3 Git history;
- create host-neutral request, response, evidence, release, and authorization contracts;
- create OpenAPI/JSON Schema artifacts;
- enforce adapter/core dependency boundaries.

### WP2 — Build difficult-document fixtures

Use the unlocked source documents behind B07, B13, B16, B17, B23, B29, and B30. Record exact
metadata, table grid, headers, units, captions, section paths, footnotes, and source coordinates.

If an original is genuinely ambiguous, prepare an adjudication packet and continue other work. Do
not invent gold and do not open the holdout.

### WP3 — Implement the canonical compiler

- complete document metadata evidence;
- table grids and row/column spans;
- multi-row header paths;
- unit inheritance and normalization;
- numeric/range/qualifier values;
- caption/section/footnote relationships;
- exact source provenance;
- semantic parser-candidate QA.

Do not start retrieval tuning until compiler gates pass.

### WP4 — Implement projections and embedding reuse

Generate complete metadata, passage, table-row, and typed-fact projections. Reuse embeddings only
when model identity, dimensions, and normalized projection-text hash match.

### WP5 — Implement candidate generation

Implement exact/structured, lexical, and dense candidates with authorization/release filtering in
every query. Recall@10 must pass before reranking.

### WP6 — Select the reranker

Benchmark transparent and local-model rerankers on relevance, bilingual behavior, latency, and
resource use. Use the smallest passing option.

### WP7 — Implement coverage, answering, and validation

- requested-field coverage matrix;
- complete/partial/not-found/refused states;
- complete citation envelope;
- separate support and completion validation;
- at most one targeted repair;
- no evidence-dump fallback.

### WP8 — Rehearse on isolated Fedora PostgreSQL

- clean migrations and rollback;
- roles and RLS;
- representative compilation;
- API/worker failure recovery;
- release pinning;
- audit redaction.

## 8. Pre-Railway checkpoint

Pause before WP9 and provide one concise evidence package containing:

- exact intended Railway and LibreChat mutations;
- reused resources and expected storage/compute effect;
- fresh rollback baseline scope;
- migration and RLS rehearsal results;
- compiler fixture scores;
- exact metadata, table integrity, Recall@5/10, and latency results;
- confirmation that V1/V2/V3 data and Agents remain protected;
- remaining source ambiguities or gate failures;
- rollback procedure.

Request one batch authorization covering:

- new V4 logical schema/roles/object prefix;
- V4 route in the existing private API capacity;
- full 373-source candidate compilation;
- one new private V4 LibreChat Agent;
- authenticated V1/V2/V3/V4 development regression;
- wiki update.

Do not request separate approvals for each application authentication or each normal deployment
step after that batch authorization.

## 9. Planned Railway shape after authorization

Reuse physical resources:

| Capability | Reused resource |
| --- | --- |
| Database | Existing V3 shadow PostgreSQL service and volume |
| Objects | Existing V3 shadow object bucket |
| API | Existing V3 shadow API replica |
| Compiler | Existing V3 shadow worker |
| Migrations | Existing V3 shadow migrator |
| Models | Existing Fedora/Local AI services |
| Client | Existing LibreChat plus one new private V4 Agent |

Create logical isolation:

- `bauer_rag_v4` schema;
- V4-specific roles and RLS;
- `v4/` object prefix;
- V4 release namespace;
- V4 API route and signed audience;
- V4 Agent allow-list;
- V4 evaluation namespace.

The shared API replica may preserve a frozen V3 compatibility route while adding V4. V3 remains
pinned to its existing release. If coexistence is unsafe, obtain an explicit decision before
retiring the live V3 route; immutable V3 data and benchmark evidence must remain.

Never change an active-release pointer during private-shadow work.

## 10. Evaluation gates

The initial V4 development gate requires:

| Gate | Minimum |
| --- | ---: |
| Source contract | 373/373, zero unexpected |
| Golden document metadata | 1.0000 |
| Golden exact identifiers | 1.0000 |
| Header/unit association | >= 0.9500 |
| Caption/section/footnote association | >= 0.9500 |
| Numeric-looking golden cells typed | >= 0.9800 |
| Exact metadata retrieval | 1.0000 |
| Table integrity | >= 0.9000 |
| Recall@5 | >= 0.9000 and above best V2 baseline |
| Recall@10 | >= 0.9500 |
| Claim correctness | >= 0.8000 |
| Constraint compliance | >= 0.9500 |
| Safe refusal | >= 0.9000 |
| High-severity citation correctness | 1.0000 |
| Unauthorized evidence | 0 |
| Multi-part field coverage | >= 0.9000 |
| LibreChat question preservation | 100% |
| End-to-end p50 | <= 30 s |
| End-to-end p95 | <= 90 s |
| Unhandled failures in 30-case run | 0 |

A release gate must name the exact suite digest, split, case count, route, source manifest,
compiler/projection/model/reranker identities, and required metrics. A generic passing evaluation
count is insufficient.

## 11. Required behavior for the next task

- Work persistently through WP0–WP8 without asking the user to manage routine technical choices.
- Send concise progress updates at meaningful quality gates.
- Repair the earliest failing layer rather than tuning downstream behavior around it.
- Preserve all unrelated dirty files.
- Use `apply_patch` for hand-authored edits.
- Commit V4 work by coherent work package on the V4 branch.
- Do not print credentials, connection strings, bearer tokens, or secret Railway variables.
- Do not edit MongoDB directly to provision application invariants.
- Do not open the locked holdout.
- Do not mutate Railway or LibreChat before the pre-Railway authorization.
- Clearly distinguish proposed/local/Fedora results from deployed private-shadow results.
- Do not describe V4 as production-ready merely because development gates pass.

## 12. New-chat startup prompt

Copy this into the new Codex task:

```text
Continue Bauer RAG V4 as the principal architect and principal engineer.

First read this handover completely:

D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-development-handover-20260729.md

Then read every document in its mandatory read order completely. Never open the locked holdout.

Execute the exhaustive V4 plan autonomously through WP0-WP8: freeze the factual baseline; create
the new D:\02_Code\LibreChat_Setup-rag-v4 repository/worktree from the exact V3 Git history on a
codex/bauer-rag-v4 branch; define host-neutral contracts; build the reviewed difficult-document
fixtures; implement and verify the canonical compiler, projections, embedding identity reuse,
three-family candidate retrieval, reranking, evidence coverage, answering, and validation; then
rehearse migrations, roles, RLS, release pinning, rollback, and failure recovery on isolated Fedora
PostgreSQL.

Treat existing Railway and Fedora access as available. Do not repeatedly ask for individual
authentications. Preserve unrelated dirty files and never print credentials or secret variables.
Do not mutate Railway or LibreChat yet. At the end of WP8, stop once and give me the pre-Railway
evidence package and one batched authorization request described in the handover.

Make informed engineering decisions without asking routine questions. Repair representation before
retrieval, candidate retrieval before reranking, and evidence coverage before answer tuning. Keep
the full original user question separate from search hints. Do not add more retrieval channels,
agents, or validators unless measured evidence requires them. Do not use an evidence-dump fallback.

Use the same public development evidence and preserve V1/V2/V3. V4 remains a proposed/local system
until there is explicit authorization for its private Railway shadow.
```

## 13. Sources

- `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-architecture-and-delivery-plan.md`
- `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-principal-planning-prompt.md`
- `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v3-shadow-deployment-handover-20260726.md`
- `D:\02_Code\LibreChat_Setup-rag-v2\docs\bauer-rag-v2-rollout-20260723.md`
- `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-architecture.md`
- `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-runbook.md`
- `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-three-way-development-benchmark-20260727-corrected.json`
- `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-three-way-development-benchmark-20260727-corrected-score.json`
- `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\three-way-retrieval-benchmark-after-11ea000.json`
