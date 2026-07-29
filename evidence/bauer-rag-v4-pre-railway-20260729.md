# Bauer RAG V4 pre-Railway checkpoint

Date: 2026-07-29

Status: WP0–WP8 complete. V4 is a proposed local/Fedora system; no V4 Railway or
LibreChat mutation, private-shadow deployment, holdout opening, active-release
change, or production promotion has occurred.

The machine-readable evidence package is
`evidence/bauer-rag-v4-pre-railway-20260729.json`.

## Bound implementation and evidence

- V4 repository: `D:\02_Code\LibreChat_Setup-rag-v4`
- Branch: `codex/bauer-rag-v4`
- Implementation commit: `402b739185fd233473c3b4f8e9175bebe59cf157`
- Exact V3 ancestor: `11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59`
- V4 tests: 43 passed, 0 failed
- Source contract: 373/373 selected originals, zero unexpected; 166 PDF and
  207 HTML; 550 originals accounted for with 177 prior duplicate aliases;
  503,391,181 selected bytes
- Source-contract digest:
  `40049a12aacb198018a633905d793c8ef9011403f3fdbcd34ed7fe0792ab2580`
- Public difficult-fixture digest:
  `94c02963aa89819465989c03f695ebd9ee3972d4cd49ea59063d375ea6c23dbd`
- Public V4 development-suite digest:
  `614b51a0efad8aad54ca17ec4af9fb0b79ebc44315c78d84b128ccb6cb4065cd`
- Gate-manifest digest:
  `4b4581cca4971744a4875fa4226bf24644fa38c460a3d4c6068b2b6cf0916170`

The locked holdout remained closed.

## Development results

### Representation and compiler

All six difficult documents passed semantic parser selection. The compiled
fixture contains 62 tables, 386 typed cells, and 454 typed facts. Exact
measured metrics are:

| Metric | Result | Gate |
| --- | ---: | ---: |
| Golden document metadata fields | 1.0000 | 1.0000 |
| Golden exact identifiers retained | 1.0000 | 1.0000 |
| Table header/unit association | 1.0000 | >= 0.9500 |
| Caption/section/footnote association | 1.0000 | >= 0.9500 |
| Numeric-looking golden cells typed | 1.0000 | >= 0.9800 |
| Exact metadata retrieval | 1.0000 | 1.0000 |
| Table integrity | 1.0000 | >= 0.9000 |

The final representation repair preserves the soft-hyphenated `PE-VE
INDUSTRY` section boundary, so its 85–1470 l/min range is not attributed to
the `K 22 – K 28 SERIES`. Exact group selection also separates `BM series 100
bar – 50 Hz` from the distinct `80/100 bar – 50 Hz` row.

### Projections, embeddings, retrieval, and reranking

- 858 projections: 55 metadata, 261 passage, 88 table-row, and 454 fact
  projections
- 858 unique projection IDs and 846 unique search-text identities
- Exact embedding retry: 0 new embeddings; changed identity: 846 embeddings
- Exactly three candidate families: exact/structured, lexical/trigram, dense
- Recall@5: 1.0000
- Recall@10: 1.0000
- Unauthorized evidence returned: 0
- Best corrected V2 public baseline Recall@5: 0.6167
- Selected reranker: `transparent-linear-v1`
- Reranker MRR: 1.0000
- Reranker p50/p95: 119.72/139.91 ms
- Reranker peak measured memory: 207,032 bytes

The V4 figures are from the named seven-case difficult public development
suite. The authorized private-shadow phase must still run the identical
30-case V1/V2/V3/V4 suite for a direct client-level comparison.

### Coverage-first answering

| Metric | Result | Gate |
| --- | ---: | ---: |
| Claim correctness | 1.0000 | >= 0.8000 |
| Requested-field coverage | 1.0000 | >= 0.9000 |
| Constraint compliance | 1.0000 | >= 0.9500 |
| High-severity citation correctness | 1.0000 | 1.0000 |
| Safe-refusal accuracy | 1.0000 | >= 0.9000 |
| Original-question preservation | 1.0000 | 1.0000 |
| Evidence-dump fallbacks | 0 | 0 |
| Unhandled failures | 0 | 0 |
| End-to-end p50 | 3.834 s | <= 30 s |
| End-to-end p95 | 4.695 s | <= 90 s |

The original question remains immutable request data; search hints are
separate. Answering renders only requested supported fields, validates support
and completion separately, permits at most one targeted repair, and has no
evidence-dump fallback.

## Fedora PostgreSQL rehearsal

The isolated PostgreSQL 18.3 rehearsal passed and then removed its database,
owner, schema, and global group roles:

- five migrations applied twice and rolled back twice with identical checksums;
- injected migration failure rolled back atomically;
- five runtime roles, all NOLOGIN/non-superuser/non-BYPASSRLS;
- 20 RLS-enabled tables and 34 policies;
- authorized projection count 1; unauthorized and non-ready counts 0;
- ready fixed release resolved once; non-ready release resolved zero times;
- client release mutation rejected; active-release pointer count stayed zero;
- two failed worker attempts reached `dead`;
- an expired lease was requeued and reclaimed by another worker;
- authorization audit had zero question/answer/body/prompt/token/secret
  columns and persisted no request or answer payload;
- representative public B16/B17/B29 materialization covered 1,515 blocks, 48
  tables, 559 cells, 338 facts, and 587 projections;
- final cleanup verified no V4 rehearsal database, owner, schema, or group role
  remained.

## Intended mutations after authorization

Railway changes are additive and V4-scoped:

1. Reuse the existing private V3 PostgreSQL service/volume, creating only the
   `bauer_rag_v4` schema, V4 roles/RLS, and V4 release/evaluation namespace.
2. Reuse the existing private object bucket, adding only the `v4/` prefix for
   V4 manifests and derived artifacts. Immutable original identities remain
   authoritative and are not rewritten.
3. Reuse the existing V3 private API replica, worker, and migrator capacity,
   adding the V4 route, signed V4 audience, V4 worker/migration entry points,
   and a server-pinned private candidate release.
4. Reuse existing Fedora/Local AI model services. No new always-on model or
   retrieval service is planned.
5. Compile the full 373-source candidate into the V4 logical namespace.
6. Never change an active-release pointer during private-shadow work.

LibreChat changes are also additive:

1. Add the V4 adapter/route while preserving the complete original user
   message and treating `file_search` text only as an optional hint.
2. Add one new private V4 Agent and its permission allow-list.
3. Preserve the existing V1, V2, and V3 Agents and routes.
4. Run one browser-shaped login and reuse its in-memory bearer for the
   authenticated V1/V2/V3/V4 development regression.
5. Update the wiki only after the measured private-shadow results exist.

Expected resource effect: no new always-on Railway service. The 373-source
compile creates transient worker/embedding load and incremental V4 canonical,
projection, embedding, evaluation, and derived-object storage. Exact growth is
measured during the candidate build; no capacity increase is pre-authorized by
this checkpoint.

## Fresh rollback baseline scope

Immediately before the first authorized mutation, capture a fresh, secret-free
baseline containing:

- exact Railway project/environment/service/deployment identities and image or
  commit pins for PostgreSQL, object storage, API, worker, and migrator;
- PostgreSQL migration version, V3 table/population/release counts, active
  pointer state, role/grant/RLS posture, backup/PITR status, and a fresh
  rollback snapshot identifier;
- V3 object-prefix inventory and manifest hashes;
- V1/V2/V3 Agent IDs, configs, permission scopes, and route mappings;
- current LibreChat overlay commit and configuration hashes;
- the V3 corrected benchmark/evidence hashes already bound by WP0.

No secret values, connection strings, tokens, or unrestricted content are
included in that baseline.

## Rollback procedure

1. Stop V4 compiler work and disable only the new private V4 Agent/allow-list.
2. Remove or disable only the additive V4 API route and restore the recorded
   shared API/worker/migrator deployment pins if code coexistence caused the
   failure.
3. Confirm the frozen V3 route and its server-pinned release are unchanged.
4. Export failure evidence, then reverse the five V4 migrations or restore the
   fresh pre-mutation database snapshot. Drop only V4 roles/schema.
5. Delete only the `v4/` object prefix if artifact rollback is required.
6. Verify V1/V2/V3 data, Agents, routes, release state, and corrected benchmark
   hashes against the fresh baseline.

The procedure never deletes or rewrites V1/V2/V3 canonical data or benchmark
evidence and never changes an active-release pointer.

## Remaining gates and ambiguity

There are no failed local/Fedora development gates. Remaining external gates
are:

- Bauer owner review/sign-off of the engineering-reviewed fixture;
- full private 373-source compilation;
- authenticated identical-suite V1/V2/V3/V4 regression through LibreChat;
- measured private-shadow correction and wiki update;
- separately authorized holdout opening and production promotion.

The B17 historical conflict remains explicitly documented: the current exact
`BM series 100 bar – 50 Hz` source is authoritative for the fixture (15 kW,
425 kg), while the distinct older/other-group observation is retained as a
conflict and not silently merged. Bauer owner sign-off is still required.

## Batched authorization request

Authorize one scoped private-shadow batch covering:

1. the new V4 logical schema, roles/RLS, release/evaluation namespace, and
   `v4/` object prefix on the existing private Railway resources;
2. the additive V4 route and signed audience in the existing private API,
   worker, and migrator capacity;
3. full 373-source V4 candidate compilation;
4. one new private V4 LibreChat Agent and allow-list;
5. authenticated V1/V2/V3/V4 development regression using one browser-shaped
   login and an in-memory bearer;
6. correction of measured development failures within that private V4 scope;
7. the post-regression wiki update.

This authorization does not cover the locked holdout, an active-release
change, production promotion, retirement of V3, or ONIX implementation.
