# Bauer RAG V4 architecture

Status: local/Fedora proposal under implementation; not deployed to Railway or LibreChat.

## Provenance

The V4 worktree and `codex/bauer-rag-v4` branch start at exact V3 commit
`11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59`. V1, V2, and V3 code, releases, Agents, data, and
benchmark evidence remain unchanged.

V4 retains:

- immutable original-source identity and canonical provenance;
- fixed candidate releases and server-side release selection;
- signed tenant, knowledge-base, principal, and source scope;
- separate PostgreSQL runtime roles and RLS;
- append-only audit and manifest-bound readiness evidence.

V4 replaces:

- incomplete document-metadata and table projections;
- runtime compensation for missing canonical values;
- a shortened-search-query answer contract;
- the exactly-one-tool-output boundary;
- many-channel fusion and the extractive evidence-dump fallback;
- arbitrary-one-evaluation readiness.

## Dependency boundary

```text
LibreChat / future ONIX adapter
  -> bauer_evidence_v4.contracts
  -> host-neutral API
  -> authorization + pinned release repository
  -> exact/structured + lexical + dense candidate generation
  -> one reranking stage
  -> requested-field coverage
  -> grounded answer
  -> support and completion validation
```

Rules enforced by tests:

- contracts import no LibreChat, ONIX, MongoDB, adapter, or storage module;
- adapters may import contracts but not canonical or database internals;
- the request contains no client-selectable release ID;
- `question` is required immutable data and is never replaced by `search_hint`;
- a `complete` response cannot hide missing required fields;
- every supported coverage item resolves to a citation in the same response.

## Public contract artifacts

The authoritative Python contracts are in
`services/bauer-evidence-v4/bauer_evidence_v4/contracts/`.

Deterministically generated artifacts are in
`services/bauer-evidence-v4/contracts/schemas/`:

- request JSON Schema;
- response JSON Schema;
- evidence-unit JSON Schema;
- release JSON Schema;
- authorization-claims JSON Schema;
- OpenAPI 3.1.

The only initial endpoint is `POST /v4/answer`. It returns `complete`, `partial`, `not_found`, or
`refused`, plus field coverage, explicit absence, stable citations, a public release identity, and
an opaque trace ID.

## Implementation order

Representation gates must pass on the reviewed difficult-document fixtures before retrieval is
tuned. Candidate Recall@10 must pass before a reranker is selected. Evidence coverage must pass
before answer behavior is tuned.

The locked holdout is outside local V4 development. Railway and LibreChat remain outside scope
until the WP8 pre-Railway checkpoint receives explicit authorization.
