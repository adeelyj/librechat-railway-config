# Bauer RAG V4 private-shadow runbook

Status: authorized private shadow. V4 is not the active production release and the locked holdout
remains closed.

## Fixed deployment identity

| Item | Value |
| --- | --- |
| Railway project / environment | `bk-RAG-test` / `testing` |
| V4 tenant | `33738ad5-567c-4844-97bf-0941f1f6d36c` |
| V4 knowledge base | `fbd5c3ab-950e-4878-9d4b-41b0abfca1c7` |
| V4 candidate release | `45abb96f-c555-4a65-92a4-b6ee3be09da9` |
| Public release ID | `bauer-rag-v4-private-20260729-r1` |
| Private LibreChat Agent | `agent_TEEBDBmMxnQwL10UjILhw` |
| Agent name | `Bauer Kompressoren - RAG V4 Private Shadow` |
| Source membership | 373 exact protected Bauer file IDs |
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

Only the allow-listed V4 Agent selects `/v4/answer`. V1, V2, V3, conversation uploads, and other
Agents retain their existing routes.

## Compilation and release checks

Run status without emitting credentials:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\deployment\Invoke-BauerV4DataPlane.ps1 `
  -Phase Status `
  -OutputPath tmp\v4-deploy\evidence\v4-data-plane-status.json
```

A release is eligible for `MarkReady` only when all of these are true:

- 373 release members, compiled artifacts, canonical documents, and succeeded jobs;
- zero queued, running, failed, or dead jobs;
- zero active-release pointers;
- one passing named public-development evaluation with its exact suite digest and metrics.

The compiler tries the two native V4 PDF representations first. Only a PDF for which both are
empty may use the pinned V3 OCR engine through the V4 adapter. OCR output is translated into V4
blocks, tables, typed facts, and exact page/cell provenance; it does not bypass V4 quality gates.

## Failure recovery

- A transient compilation failure is retried by the PostgreSQL lease queue.
- A dead job is diagnosable by safe error type/fingerprint and media type; request bodies,
  credentials, and connection strings are not persisted in status evidence.
- `RetryDead` is allowed only while the release is `building`.
- `ResetBuild` deletes only disposable artifacts for the unpointed V4 candidate, clears its
  embedding cache, reseals the compiler identity, and requeues the same 373 immutable sources.
- API startup fails closed unless the exact configured release is `ready` and artifact accounting
  equals release membership.
- LibreChat rejects a V4 answer if validation did not pass or a citation lies outside its
  server-authorized file list.

## Rollback

Rollback does not require deleting V4 data:

1. Restore LibreChat deployment `ae7a03a5-3d5d-4b1f-85f4-ce65e2d54382` to remove the V4 adapter
   while retaining V1/V2/V3 behavior.
2. Restore private API deployment `13e2235d-a8c4-4103-bdb8-1f624a716026` to return to the frozen
   V3-only API image.
3. If needed, restore worker deployment `7369b37b-bd55-4d62-87a0-8139edef6357`; a ready V4 release
   has no claimable compilation jobs.
4. Leave the V4 schema and objects in place for evidence preservation. The active pointer remains
   empty, so retained V4 data cannot become production-active.

Database rollback scripts exist for all six V4 migrations and are for an explicitly scheduled
destructive teardown only. They are not the normal application rollback.

## Development regression

The authenticated regression uses exactly one browser-shaped login, normal in-session token
refresh, a fresh deleted conversation per observation, the same 30 public development prompts, and
all four protected/private Agents. It records 120 exact visible outputs. The locked holdout is
never read.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\deployment\Invoke-BauerV4ShadowRegression.ps1 `
  -V3AgentId agent_DzeT_ugU3tuZC_VCKB8Bh `
  -V4AgentId agent_TEEBDBmMxnQwL10UjILhw `
  -DeployedCommit <exact-deployed-commit>
```

The output and score are development evidence, not production promotion. Any measured V4 defect is
repaired in representation first, then candidate retrieval, then reranking, then coverage/answering.

