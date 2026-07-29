# Principal architect prompt: Bauer RAG V4

Use this prompt to produce or refresh the executable Bauer RAG V4 engineering plan. It is a
planning prompt, not deployment authorization.

---

## Prompt

Assume the combined role of principal software architect, principal retrieval engineer, principal
data engineer, and principal application-integration engineer. Design Bauer RAG V4 as a
company-deployable, client-independent retrieval and evidence service informed by the measured
results and implementation history of V1, V2, and V3.

The objective is not to preserve version purity or add sophistication. The objective is to produce
the most accurate, explainable, maintainable, and portable Bauer retrieval system that can serve
LibreChat now and ONIX later, while reusing existing Railway and Fedora infrastructure where that
does not preserve a known defect.

### Required outcome

Produce one exhaustive, implementation-ready V4 plan that:

1. explains what V1, V2, and V3 actually proved;
2. traces V3's measured failures to specific data, retrieval, serving, validation, and release-gate
   causes;
3. makes an explicit keep/adapt/replace decision for every reusable subsystem;
4. defines the V4 data model, compiler, retrieval path, answering path, authorization boundary,
   client contract, evaluation system, deployment topology, migration approach, and rollback;
5. separates the original user question from retrieval queries;
6. makes the core RAG service independent of LibreChat and ONIX;
7. reuses the current V3 physical Railway PostgreSQL, object storage, API, worker, and migrator
   capacity where safe, using isolated V4 logical namespaces and immutable releases;
8. avoids new always-on Railway compute unless measured capacity or isolation evidence requires it;
9. includes ordered work packages, dependencies, acceptance gates, failure/stop conditions,
   observability, test strategy, and definition of done;
10. keeps the locked holdout closed until an explicitly authorized promotion evaluation.

Do not implement, deploy, activate, or mutate infrastructure while running this planning prompt.
Do not print credentials, bearer tokens, connection strings, Railway variables containing secrets,
or administrator credentials.

### Mandatory read order

Read each document completely before forming the architecture:

1. `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v3-shadow-deployment-handover-20260726.md`
2. `D:\02_Code\00_Project_Management_n_skills\01_tracks\rapiddraft-studio\plans\260724_bauer-rag-v3\README.md`
3. `D:\02_Code\00_Project_Management_n_skills\01_tracks\rapiddraft-studio\handover\260724_bauer-rag-v3.md`
4. `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-runbook.md`
5. `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-architecture.md`
6. `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v2-rollout-20260723.md`
7. `D:\02_Code\LibreChat_Setup-rag-v2\docs\bauer-demo-findings.md`
8. `D:\02_Code\LibreChat_Setup-rag-v2\docs\bauer-rag-v2-runbook.md`
9. `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-three-way-development-benchmark-20260727-corrected.json`
10. `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-three-way-development-benchmark-20260727-corrected-score.json`
11. `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\three-way-retrieval-benchmark-after-11ea000.json`
12. `D:\02_Code\LibreChat_Setup\docs\bauer-rag-v4-architecture-and-delivery-plan.md`, if it
    already exists, treating it as a plan to audit and improve rather than unquestioned truth.

Inspect the exact deployed source revisions and preserve unrelated dirty files:

- V2: `D:\02_Code\LibreChat_Setup-rag-v2`
- V3: `D:\02_Code\LibreChat_Setup-rag-v3`
- LibreChat V3 overlay:
  `D:\02_Code\LibreChat_Setup\tmp\v3-deploy\librechat-overlay-9496`
- Hosted wiki source: `D:\02_Code\wiki-rapiddraft`

Never open the locked holdout.

### Evidence that must be verified

Treat the following as high-confidence findings to verify against code and immutable evidence, not
as assumptions to repeat:

- In the corrected same-prompt LibreChat benchmark, V2 materially outperformed V3 on claim
  correctness and constraint completion.
- In the direct retrieval benchmark, V2 Recall@5 was `0.6167`; the corrected V3 Recall@5 was
  `0.2667`.
- V3 exact-document metadata and table-integrity retrieval were both `0`.
- The deployed V3 database contained 120,950 table cells and zero populated `numeric_value`,
  `unit_raw`, or `unit_ucum` values.
- V3's compiler inserted null typed values for table cells while the runtime depended on those
  fields for numeric and unit filtering.
- V3's table projection did not reliably preserve multi-row headers, unit rows, captions, section
  paths, and footnotes in the representation used for retrieval and answering.
- V3 attached source-native metadata terms to representative content rather than creating complete
  document-metadata evidence records.
- V3's evidence prompt omitted fields needed by exact metadata and table questions.
- The LibreChat integration allowed a shortened `file_search` query to replace the full original
  user question for answer generation and validation.
- The V3 final boundary failed closed when LibreChat made multiple tool calls, even when a
  multi-part question legitimately required multiple searches.
- The extractive fallback optimized lexical support but often produced long, irrelevant evidence
  dumps.
- The V3 readiness gate could be satisfied by one passing selected evaluation and did not enforce
  the named full benchmark or its documented metric thresholds.
- V2 was not a generic baseline: its Bauer-specific table parsers and repeated retrieval tuning
  preserved titles, units, footnotes, identifiers, and section context more effectively.
- V3's fixed release, canonical provenance, authorization scope, RLS, source contract, and isolated
  storage remain valuable and should not be discarded merely because retrieval quality was poor.

If any item is contradicted, show the contrary code or evidence and update the conclusion.

### Architectural constraints

The plan must enforce these constraints:

- Keep immutable original source bytes as the evidence authority.
- Permit V2 Markdown and V2 extraction logic only as derived parser inputs, fallbacks, diagnostics,
  or regression oracles; never silently make them the source of record.
- Preserve one stable canonical evidence model and make search projections disposable.
- Create explicit document-metadata evidence, page/section evidence, table-grid evidence,
  row evidence, cell/header/unit/footnote relationships, and typed facts.
- Make parser selection depend on semantic fidelity and golden-document QA, not merely successful
  parsing, issue count, or number of detected tables.
- Limit initial retrieval to three intelligible candidate families: exact/structured, lexical, and
  dense semantic.
- Use a separate reranking stage; do not grow another large set of hand-written fusion bonuses
  before candidate and representation quality pass their own gates.
- Preserve the complete original question. Optimized search queries are derived data and may not
  replace it.
- Support internal subquery decomposition for multi-part questions while returning one final,
  validated response.
- Validate both evidence support and task completion. Do not equate verbatim evidence with a useful
  answer.
- Return targeted partial answers with explicit field-level `not_found` states when evidence is
  incomplete. Do not emit generic evidence dumps.
- Keep the core API free of LibreChat Agent IDs, LibreChat conversation objects, MongoDB schemas,
  ONIX UI state, and host-specific tool-call formats.
- Put LibreChat and ONIX behavior in thin adapters around one versioned service contract.
- Pin private shadows to server-side candidate releases. A client may not select an arbitrary
  release.
- Keep V1 and V2 available during V4 development. Preserve the existing V3 evidence and benchmark
  artifacts even if its private compute services are reused by V4.
- Do not add a new PostgreSQL service, object bucket, worker replica, or API replica unless a
  measured resource or isolation requirement justifies it.

### Required plan decisions

Make and justify decisions for:

1. repository and package structure;
2. physical infrastructure reuse versus logical isolation;
3. source/artifact identity and content-hash reuse;
4. parser-candidate generation and semantic parser selection;
5. table header, unit, caption, footnote, and row reconstruction;
6. document metadata and exact identifier representation;
7. embedding cache and what must be re-embedded;
8. candidate generation and reranking;
9. multilingual and terminology handling;
10. multi-part question decomposition and evidence coverage;
11. answer generation, citation format, partial answers, refusal, and validation;
12. host-neutral request/response and authorization contracts;
13. LibreChat adapter behavior;
14. future ONIX adapter behavior;
15. database roles, RLS, immutable releases, and audit events;
16. readiness/promotion gates that name exact suites and minimum case counts;
17. local/Fedora rehearsal, 373-source compilation, Railway shadow deployment, and rollback;
18. monitoring, traces, metrics, cost controls, and capacity limits;
19. V1/V2/V3/V4 comparative evaluation and wiki publication;
20. risks, stop conditions, and decisions that require human review.

### Evaluation contract

The plan must define independent gates for:

- source-contract completeness;
- canonical extraction and reading order;
- document-metadata completeness;
- table-grid, header, unit, caption, and footnote integrity;
- exact and structured lookup;
- lexical and semantic retrieval;
- reranking;
- multi-part evidence coverage;
- answer claim correctness;
- constraint compliance;
- safe refusal and field-level absence;
- citation correctness and completeness;
- authorization isolation;
- client-adapter equivalence;
- p50/p95 latency and failure rate.

Use the same public 30-case development suite for V1/V2/V3/V4 comparison. Add compiler-level golden
fixtures for the specific documents behind B07, B13, B16, B17, B23, B29, and B30 before the full
compile. Keep extraction, retrieval, answer, and application-integration scores separate.

A release cannot become ready because an arbitrary evaluation passed. Readiness must require an
explicit gate manifest naming the suite digest, required split, minimum case counts, mandatory
metrics, required client route, code/compiler identity, source manifest, and absence of hard
authorization failures.

### Output format

Write a self-contained engineering document with:

1. executive verdict;
2. evidence baseline and factual version review;
3. V4 goals and non-goals;
4. architecture principles;
5. reuse/adapt/replace matrix;
6. target architecture diagrams;
7. canonical data and compiler design;
8. retrieval and reranking design;
9. answering and validation design;
10. host-neutral API and authorization contract;
11. LibreChat and ONIX adapters;
12. database, object storage, release, and audit design;
13. evaluation and acceptance gates;
14. ordered implementation work packages with dependencies and exit criteria;
15. deployment, rollback, and resource-reuse plan;
16. testing and observability;
17. risks, failure modes, and explicit stop conditions;
18. definition of done;
19. open questions requiring human input;
20. exact sources and evidence paths.

Use tables and diagrams only where they make comparisons or flows clearer. Distinguish verified
facts from proposed V4 design. Do not present proposed V4 as deployed. Do not include calendar
estimates without measured team capacity. Prefer deletion and consolidation over new components.
