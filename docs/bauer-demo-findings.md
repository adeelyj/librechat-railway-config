# Bauer Kompressoren Demo Findings

Date: 2026-07-20

## Agreed scope

The first Bauer demo will focus only on:

1. Search for similar previous projects and designs.
2. Document and part search / engineering knowledge database.

Drawing review, Dynamics 365 master-data creation, approval workflows, and supplier-change management are outside this first demo scope.

## Executive assessment

The existing implementation is a strong isolated document-RAG foundation, but it is not yet the Technical Twin described in the deep-research report.

The current Bauer Agent can search 373 unique OCR/deduplicated documents, retrieve semantically relevant passages, and cite source files. It cannot yet reliably identify similar historical projects, perform exact part/asset lookup, enforce engineering compatibility constraints, or explain structured differences between designs.

The demo should therefore add a small structured Technical Twin behind the existing LibreChat Agent while retaining the current file-search corpus as the evidence layer.

## Current implementation

- Private `Bauer Kompressoren` LibreChat Agent.
- Separate Bauer access group and disjoint files from other knowledge bases.
- 550 source PDF/HTML files reduced to 373 exact-unique documents.
- OCR for image-only PDFs.
- Source path, SHA-256, page headings, and document text retained.
- Local `local/qwen-coder` model for answers and tool calling.
- Local `local/embed-engineering` model for 1,024-dimensional embeddings.
- Railway RAG API and PostgreSQL/pgvector for file retrieval.
- Live file-search acceptance test for Bauer content.

## Missing capability: similar previous projects

The system contains documents, not historical project records. It does not currently represent projects as configured engineering solutions.

For the demo, each project must contain structured attributes such as:

- project ID and application sector;
- medium or gas family;
- target pressure and capacity;
- compressor family and model;
- compressor, booster, or combined topology;
- air- or water-cooled concept;
- control package;
- storage and filling package;
- measurement and purification equipment;
- installation and environmental constraints;
- standards and certificates;
- associated parts and documents.

The search flow must be:

1. Extract technical requirements from the user's question.
2. Apply hard compatibility exclusions for medium, pressure, topology, and safety-critical packages.
3. Retrieve candidates using structured, keyword, and vector search.
4. Rank compatible projects using weighted technical similarity.
5. Explain matching and differing attributes.
6. Link the result to supporting parts and documents.

## Missing capability: document and part search

The current corpus can find model names, article names, and technical concepts when they appear in document text. It does not maintain parts as first-class records.

For the demo, each part record should contain:

- part or article number;
- description and German/English synonyms;
- category and lifecycle status;
- compatible compressor models;
- compatible media;
- pressure envelope;
- related projects;
- related manuals, drawings, certificates, and spare-parts documents.

The search service must combine:

- exact identifier lookup;
- keyword/full-text search;
- semantic vector search;
- structured technical filters.

Exact part, project, model, serial, and document numbers must bypass fuzzy semantic ranking.

## Minimum demo dataset

The first demo does not require the full 50-project portfolio proposed by the research report. A carefully designed vertical slice is sufficient:

- 10-15 synthetic historical projects;
- 50-100 synthetic parts;
- associated project documents and evidence links;
- deliberate near-duplicate projects;
- German and English terminology;
- at least three project clusters, for example:
  - nitrogen compressor packages at 365 and 420 bar;
  - BM air-compressor variants at 40 and 100 bar;
  - breathing-air packages at 300 and 420 bar.

All synthetic records must be clearly labeled as synthetic and must not be represented as confirmed Bauer internal data.

## Recommended implementation

Keep LibreChat as the user interface and retain `file_search` for document evidence. Add a dedicated structured tool to the Bauer Agent, provisionally named `search_bauer_twin`.

The tool should support:

- `search_similar_projects`;
- `compare_projects`;
- `search_parts`;
- `search_documents`;
- `get_project_details`;
- `get_part_details`.

The initial structured implementation can use Railway PostgreSQL with normalized relational tables, relationship tables, JSONB attributes, PostgreSQL full-text/trigram search, and pgvector. A separate graph-database product is not required for the demo.

## Demo acceptance criteria

The demo is complete when it can reliably demonstrate:

1. Find the closest previous nitrogen project for 420 bar and approximately 500 l/min.
2. Reject otherwise similar projects with an incompatible medium or pressure envelope.
3. Compare the result with the closest 365-bar project.
4. Explain matching and differing engineering attributes.
5. Show all parts and documents associated with the selected project.
6. Find an exact part, project, model, or document identifier as the top result.
7. Find a compatible part from a natural-language description when the identifier is unknown.
8. Return the same project family for equivalent German and English queries.
9. Cite the public source documents supporting product and compatibility information.
10. Return a clear not-found result rather than inventing missing project or part data.

## Evaluation set

Select approximately 15 representative queries from the research report's 55-query benchmark, covering:

- exact identifier lookup;
- part and document search;
- project similarity;
- fine-difference detection;
- German/English retrieval;
- incompatible-medium and insufficient-pressure exclusions.

Record expected results and measure at least:

- exact-hit accuracy;
- Recall@k for compatible projects;
- hard-filter violation rate;
- difference-detection accuracy;
- German/English result consistency;
- response latency.

## Work Codex can execute

Codex can implement the complete synthetic vertical slice in the current environment:

1. Design and migrate the Technical Twin PostgreSQL schema.
2. Generate the synthetic project, configured-system, part, and document-link datasets.
3. Validate internal consistency and compatibility constraints.
4. Build the structured search and comparison API.
5. Add exact, full-text, vector, and filtered retrieval.
6. Implement the report's weighted similarity and hard-exclusion logic.
7. Implement deterministic project-difference explanations.
8. Connect the API as a tool available to the Bauer LibreChat Agent.
9. Build automated benchmark and regression tests.
10. Deploy the additional service/schema to Railway testing.
11. Run end-to-end demo acceptance tests and document the demo script.

## Boundaries requiring Bauer input later

The synthetic demo can be built without additional Bauer access. Production validation will later require Bauer confirmation or samples for:

- real project attributes and numbering conventions;
- actual part/master-data fields;
- repository and PLM/PDM boundaries;
- engineering compatibility and exclusion rules;
- representative historical projects;
- real user queries and expected results.

The demo must not imply that inferred public product relationships are confirmed internal Bauer engineering rules.
