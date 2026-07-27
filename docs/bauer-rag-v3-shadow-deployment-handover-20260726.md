# Bauer RAG V3 private-shadow deployment handover

Date: 2026-07-27  
Environment: Railway `bk-RAG-test/testing`  
Status: private fixed-candidate shadow and corrected V3-only LibreChat overlay deployed and
verified. The same 30-prompt V1/V2/V3 development benchmark is complete and published honestly:
V3 improved substantially from its first failed run but remains below V2 and is not approved for
production promotion. A new explicit one-login authorization is still required for the final
13-case LibreChat regression.

## Scope and safety boundary

This is a **private shadow**, not a production promotion.

- V3 serves only the fixed ready candidate in `BAUER_V3_CANDIDATE_RELEASE_ID`.
- The V3 active-release pointer remains empty and was never mutated.
- V1 and V2 Agent IDs, file sets, variables, indexes, and active deployments were not changed.
- The locked holdout was never opened.
- No V3 public domain or TCP proxy exists.
- No credential, bearer token, or secret Railway variable was emitted.
- Production promotion remains `false`.

Protected existing Agents:

```text
V1 Agent          agent_Z8A2LtQWLeP4KuUDbvqZL
V2 Agent          agent_pmPMcA25UXS7vznUaz-DU
Test Archive      agent_QnRNYPGlShuSnY0CYgQnm
```

New private V3 Agent:

```text
V3 Agent          agent_DzeT_ugU3tuZC_VCKB8Bh
Tools             file_search only
Visibility        private Bauer group
Files             exact 373 V1 Bauer external file IDs
```

## Required read order

1. This handover.
2. Central V3 plan:
   `D:\02_Code\00_Project_Management_n_skills\01_tracks\rapiddraft-studio\plans\260724_bauer-rag-v3\README.md`
3. Central V3 handover:
   `D:\02_Code\00_Project_Management_n_skills\01_tracks\rapiddraft-studio\handover\260724_bauer-rag-v3.md`
4. V3 deployment runbook:
   `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-runbook.md`
5. V3 architecture:
   `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v3-architecture.md`
6. V2 live rollout:
   `D:\02_Code\LibreChat_Setup-rag-v3\docs\bauer-rag-v2-rollout-20260723.md`

Never open the locked holdout.

## Exact deployed code

```text
Repository      D:\02_Code\LibreChat_Setup-rag-v3
Branch          codex/bauer-rag-v3
Repository HEAD 11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59
V3 backend      11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59
LibreChat       9496ad326df3e3e1daf8fda32b6add931af0a01f
Remote          origin/codex/bauer-rag-v3
Worktree        clean
```

Latest verification:

```text
Backend tests             346 passed
V3 evaluation tests        32 passed, 2 subtests
Railway static tests      128 passed
Integration static tests   91 passed
Data-plane static checks    37 passed
Data-plane unit tests        7 passed
LibreChat boundary tests    26 passed
Benchmark runner tests       5 passed
```

The final stabilization preserves the one-model-repair limit. If that repair still contains
unsupported paraphrases, V3 constructs an extractive answer only from citations already used by
the repair, preserves any detected safe-refusal sentence, and runs the full deterministic validator
again. It still fails closed if that exact-evidence fallback does not validate.

## Railway resources

```text
Project ID       45bb0e8d-9eca-4973-8026-a3eddbd092b6
Environment ID   6c80a4d1-c8e3-4410-b712-a27f89012046

PostgreSQL       3454d164-bbeb-4309-a483-291628171fd2
Volume           24d3bb97-ef60-4925-bc71-dbda2665db08
Volume instance  988c185e-a18b-4dbc-9f0c-5f412653b6e1
Object bucket    39767cbe-40a2-4841-8959-b638898587f4
PITR config      39c76ec1-f21c-4da4-b8a0-b25cba07e8c4

Migrator         7f1f66ea-a994-49b7-8143-0d89bb52ccaf
Deployment       1a92b387-1403-4330-b060-913f252271ed (SUCCESS)

Worker           b2621053-a240-48c6-abed-6f48f31fe141
Deployment       250827d4-dcfc-4e39-a149-53bc0a290223 (SUCCESS)

Private API      138c97a8-1f5d-45c9-a5e6-4ea5291e142e
Deployment       13e2235d-a8c4-4103-bdb8-1f624a716026 (SUCCESS)
```

All three V3 services have auto-deploy disabled and are pinned to
`11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59`.

The final Railway database verification passed migrations `001-020`, non-owner role enforcement, RLS,
fixed-candidate serving, and zero active-pointer counts.

## Candidate and compilation

```text
Tenant ID             33738ad5-567c-4844-97bf-0941f1f6d36c
Knowledge-base ID     fbd5c3ab-950e-4878-9d4b-41b0abfca1c7
Candidate release ID  ddc8a68c-2af5-4b02-9f8f-a2c076e822d2
Release status        ready
Sources expected      373
Sources succeeded     373
Sources citable       373
Quarantined             0
Active pointer        null
```

The development evaluation is interim Codex-reviewed and is sufficient only for this private
shadow. It is not independent Bauer sign-off and is not production-promotion evidence.

## Fedora rehearsal

The final retained isolated Fedora rehearsal is:

```text
Run ID          v3-fedora-20260727-benchfix8
Database        bv3_reh_d87f8775a24b
Commit          11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59
Migrations      20
RLS             passed
Active pointers zero
Holdout         unopened
Promotion       not performed
Evidence SHA    adac5442fd633f4feb73e4d18ef8ba43549baab2c6b4ac7613659107061a1a70
```

It is retained for inspection. No automatic cleanup was performed.

## LibreChat overlay

```text
LibreChat service      c7f709a4-ec29-4ead-b21f-34439cdfb9e3
Baseline deployment    1142dfd1-fc50-477a-8cc4-7f226cc212c2
Prior overlay          f975add4-681b-41b8-a6fc-356ab727a5b6 (SUCCESS rollback)
Overlay deployment     bfcab6d7-baaf-4559-b7d1-d82deee03ec4 (SUCCESS)
Overlay commit         9496ad326df3e3e1daf8fda32b6add931af0a01f
Overlay image digest   sha256:2df3e22201cbc6ba4f160fd9e50e59e429eb40ac9870dd0a183ba84ec3f41f12
Rollback image digest  sha256:1c7a3275ee07221db7315d46e503de6afc2839b5c8c1d4404ce3517bc00868e9
```

Latest verification confirms:

- overlay exact commit and image digest match the V3-only final correction;
- the running `@librechat/api` bundle SHA-256 is
  `8ee1a98c6102b83cbbe025eab76653eb451a8ca3011e308bf737a6fa0a9259c5`;
- the checksum-bound `handleTools.js` SHA-256 is
  `dadb1a782c43d1afd367159b6e0f896348a246977debea18fd0d23af46b5ef32`;
- only the allow-listed single-agent V3 path sets SDK `AgentInputs.toolEnd`;
- only V3 projects the complete original request into `/v3/answer`; V1/V2 retain their existing
  tool-query behavior;
- V1/V2 variables are unchanged;
- only the new V3 Agent is allowed to use the private V3 API;
- fixed-candidate selection is intact;
- active pointer is unchanged;
- production promotion is false.

## Live backend gates

All six final `/v3/query` cases pass against the fixed release with 373 authorized sources:

```text
exact   pass
B08     pass
B11     pass
B20     pass
B21     pass
absence pass; retrieved evidence contains neither 375 nor Argon
```

All six final `/v3/answer` cases return HTTP 200, match the fixed release, pass deterministic
validation, contain all required values, and have zero violations:

```text
exact   answered_after_repair
B08     answered_after_repair
B11     answered_after_repair; safe refusal detected
B20     answered_after_repair
B21     answered_after_repair
absence answered_after_repair; safe refusal detected
```

Evidence:

```text
Path    D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\direct-v3-live-gates-final.json
SHA     d359d86aadb843d687d8485df0aa77a315e95eeb4922f7f3cc812e51c904e196
```

## One-session LibreChat regression attempt

The regression followed the required authentication sequence:

1. browser-shaped `User-Agent`;
2. DPAPI credential loaded without printing it;
3. exactly one `/api/auth/login`;
4. HTTP 200;
5. one in-memory bearer reused;
6. token and password references cleared;
7. no login retry.

The run did not fail because of a ban or bad credential. It reached three Agent cases:

```text
v1_exact  started HTTP 200; completed conversation; DELETE 201
v2_exact  started HTTP 200; completed conversation; DELETE 201
v3_exact  started HTTP 200; 109 successful message polls; no final assistant message within 240 s
```

The private V3 API continued returning HTTP 200. The V3 Agent made repeated `/v3/answer` calls, but
LibreChat did not finalize its assistant response before the runner deadline. No fourth case began.
All three temporary conversations were deleted successfully, and Railway recorded no HTTP errors.
Because the runner held observations only in memory and suppressed child diagnostics, V1/V2 answer
content was not preserved and cannot be counted as a completed regression gate.

Attempt evidence:

```text
Path    D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-shadow-regression-attempt-1.json
SHA     e0c6e271ca013f3cfc087f92cb61fa1db195ce50c28e9e9f9922dba7580d6d5b
```

The local regression runner is now hardened to:

- allow 600 seconds per Agent case;
- write sanitized progress after every completed case;
- write sanitized failure evidence with only the case label and exception type;
- never serialize credentials, tokens, raw prompts, or raw answers.

## Authorized regression attempt 2

After explicit authorization, the wrapper made exactly one new browser-shaped login. Railway
recorded `/api/auth/login` HTTP 200 on deployment
`f975add4-681b-41b8-a6fc-356ab727a5b6`. No Agent request or regression case started.

The child runner failed closed immediately after login because it still sealed itself to the
prior overlay commit and 25 unit tests, while the corrected wrapper supplied the deployed overlay
commit and 27 tests. The bearer remained in memory only, was cleared when the process exited, and
no login retry was attempted.

The wrapper and runner now share and statically verify:

```text
Backend commit     bc2ecfdfd09dc911430a277fce2199439cbd9362
Overlay commit     29f3c5fd46b841e12ed2cd1641304cefcc096956
Overlay unit tests 27
Static guards      91 passed
```

An offline exact-argument probe now accepts those three seals and reaches the expected missing
in-memory-token guard without making a network request. The protected Railway verification also
passes again after attempt 2.

Attempt evidence:

```text
Path    D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-shadow-regression-attempt-2.json
SHA     388ef975856d0a0485d9563165519ba52cb01c54f0f113fe13c9c1878d5a514f
```

## Authorized regression attempt 3

After explicit authorization, the corrected wrapper made exactly one browser-shaped login and
reused one in-memory bearer. Railway recorded the single `/api/auth/login` as HTTP 200. The run
completed and deleted the first three temporary conversations:

```text
v1_exact  HTTP 200; exact model found; DELETE 201
v2_exact  HTTP 200; exact model and 525 bar found; DELETE 201
v3_exact  HTTP 200; deterministic refusal; DELETE 201
```

The fourth case, `v3_b08_cross_source_table`, timed out after 600 seconds while the old overlay
repeatedly called `/v3/answer`. Railway later recorded its conversation DELETE as HTTP 201 at
`2026-07-27T10:24:23.748Z`; no temporary conversation remains. The failure document's
`temporary_conversation_cleanup_required` field reflects runner bookkeeping at exception time,
not the confirmed server-side cleanup.

The observations isolated two V3-only defects:

1. the graph patch wrote `toolEnd` to `graphConfig`, but pinned
   `@librechat/agents` 3.2.65 reads it from `AgentInputs`;
2. LibreChat sent the model-shortened tool query to `/v3/answer`, so the exact V3 case could fail
   deterministic validation even though the direct backend gate passes with the complete request.

V1's regression contract was also corrected offline: it now protects the existing sourced,
non-empty exact-model behavior without imposing V3's 525-bar candidate criterion. V2 and V3 retain
the model-plus-525 requirement.

Attempt evidence:

```text
Path    D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-shadow-regression.json.failure.json
SHA     6bccfdd0955e2e45f252cc78cdb6929e4d769162b3dec39e3849a2244e1c03c8
```

## V3 Agent finalization and request-fidelity correction

The repeated calls were caused by an integration defect, not authentication or the V3 backend.
The first finalization overlay propagated the private marker to `graphConfig.toolEnd`, but the
pinned SDK reads `toolEnd` from `AgentInputs`. The field was therefore a no-op and the model
re-entered after each successful `file_search`.

The replacement overlay:

- sets a private `bauerV3DirectFinal` marker only for the allow-listed V3 Agent;
- checksum-patches the exact pinned `@librechat/api` runtime bundle;
- sets `agentInputs[0].toolEnd = true` only for one marked V3 Agent;
- checksum-patches pinned `handleTools.js` to pass only the current request into `file_search`;
- sends the complete original request to `/v3/answer` only for V3 and fails closed if that request
  is missing or over 4,000 characters;
- preserves the exact validated V3 answer in the existing final boundary;
- leaves every non-V3 Agent path unchanged.

Three candidate images failed during build-only compiler experiments. All failed before runtime
replacement, so deployment `5997e042-c11c-47c9-8e4a-79362e19d626` remained live. The final
checksum-bound bundle patch built and deployed successfully as
`f975add4-681b-41b8-a6fc-356ab727a5b6`.

The replacement correction was tested with 31 overlay tests and 91 integration guard tests, then
deployed successfully as `bfcab6d7-baaf-4559-b7d1-d82deee03ec4`. Its pre-mutation rollback target
is the healthy `f975add4-681b-41b8-a6fc-356ab727a5b6` deployment.

Replacement-correction evidence:

```text
Path    D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-v3-toolend-correction-deployment.json
SHA     10c2cec1a9ff509b33d5e044aa071cce4afe634b4dcc966b874e48d97c5dc4be
```

## Full 30-prompt development benchmark

A direct, login-free backend comparison ran the same 30 permitted development prompts against
V1, V2, and the pinned V3 candidate. It used the same 373-source scope, scored the 20 cases with
evidence-location contracts, rotated system order serially, and produced 90 observations. The
locked holdout was not opened.

The first full comparison **failed the V3 development gate**:

| System | Recall@5 | Recall@10 | MRR | Exact metadata | Table integrity | p50 latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| V1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 39.91 ms |
| V2 current | 0.6167 | 0.6167 | 0.6250 | 0.5556 | 0.2308 | 964.85 ms |
| V3 before correction | 0.0000 | 0.0500 | 0.0050 | 0.0000 | 0.0000 | 15013.64 ms |

V3 returned HTTP 500 for B09 and B29. Railway logs identify both failures as PostgreSQL statement
timeouts while evaluating `bauer_rag_v3.readable_source_ids()`. Targeted B07/B08/B22 probes also
confirmed that the low V3 score is not just an external-ID normalization problem: the expected
sources can appear in the top ten while the required page/table row does not.

After V3-only query-scope, retrieval-balance, product-discovery, and identifier-context
corrections, exact commit `11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59` was independently
rehearsed, deployed to the same private fixed candidate, and evaluated with the identical suite:

| System | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | Exact metadata | Table integrity | p50 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| V1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 32.92 ms | 39.97 ms |
| V2 current | 0.5083 | 0.6167 | 0.6167 | 0.6167 | 0.6250 | 0.5556 | 0.2308 | 1068.45 ms | 1962.58 ms |
| V3 corrected shadow | 0.0667 | 0.2167 | 0.2667 | 0.3917 | 0.2335 | 0.0000 | 0.0000 | 2261.21 ms | 8233.47 ms |

All 90 corrected-run observations returned HTTP 200. There were zero runtime errors, zero
authorization violations, and zero V3 release mismatches. Raw answers were not stored.

The correction is material: V3 Recall@5 rose from `0.0000` to `0.2667`, Recall@10 rose from
`0.0500` to `0.3917`, p50 fell from about 15.0 seconds to 2.3 seconds, and both statement-timeout
errors were removed. V3 nevertheless remains behind V2 on aggregate recall, exact metadata, table
integrity, and tail latency. The remaining gaps concentrate in canonical table metadata and
exact certificate, document, part, and product-limit retrieval.

The current V2 result is below the historical tuned result (Recall@5 0.8667, exact metadata 1.0,
table integrity 0.6923). The protected-state check shows V1/V2 configuration was not changed by
this deployment, so this remains a separate runtime/index reproducibility question rather than a
V3-caused regression.

The benchmark therefore passes as a complete, reproducible private-shadow comparison but **fails
as a V3 production-promotion gate**. The live wiki records that distinction and does not claim
that V3 is ready to replace V2.

Benchmark evidence:

```text
First run  D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\three-way-retrieval-benchmark.stdout.json
SHA        41d8d3837a7c20adcde2ffed6d146c0babc0b3de7afa6b9c6d1dd6531267902d
Corrected  D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\three-way-retrieval-benchmark-after-11ea000.json
SHA        daf3cb8097d40376f20538cea43dcc4b4d78452949fdf4a618ec906415aa6e3e
```

## Wiki publication

The same-prompt comparison is published at:

```text
URL      https://wiki.rapiddraft.ai/librechat/06_benchmarks/v1_v2_v3_private_shadow_results/
Source   D:\02_Code\wiki-rapiddraft\docs_librechat\06_Benchmarks\V1_V2_V3_Private_Shadow_Results.md
Commit   4846c534d7515dd2896d7f527f47b6854138cb5f
Deploy   6a675ffb03e8c00008fd5bb8 (ready, production)
```

The live page was verified with HTTP 200 and contains the corrected V3 Recall@5 value `0.2667`.
It explicitly distinguishes a complete private-shadow comparison from a failed production-
promotion gate.

## Evidence hashes

```text
Rollback baseline          af71feb489c220d05e4926e57be350881673cca4da27fa327f5921688aab83ca
PITR                       6813e2f60cc5cc884d4d5a28729fe3ac27c4abb6fbd813149f7d9927aa106102
Fedora fix37               21f3f8e6a3d98e61241f632d29dfff0373193d2fe39bcf2cebb5b4d33c7a4deb
Railway migration/RLS      1e7d186c597c3dd8cadc2e77a59d2a94583b6f780aa04f708088d234e85c2e6e
Candidate ready            9862b18fea1592679501344711144fdab60e7acfd879e96a380e611ee831d761
Development evaluation     e7b00ba2e48dd23b5bf6b35a5a959f8823f35dec316963024ea54c72a43c8ce7
Private Agent              104290e69adc90e158d1c0a2034de8fc90fe50e7bdcdd3da483d9b56c5b0b9b6
Deployment input           7480637f792ebbf60df327aebd40671e53798f866ddcb451ceb20d04e9ac57a2
Railway config identity    ebd23fef4cf186b29ae6b4967d4a528ac1334f5a560c6e1e9030678ed495264d
Direct live gates          d359d86aadb843d687d8485df0aa77a315e95eeb4922f7f3cc812e51c904e196
Regression attempt 1       e0c6e271ca013f3cfc087f92cb61fa1db195ce50c28e9e9f9922dba7580d6d5b
Regression attempt 2       388ef975856d0a0485d9563165519ba52cb01c54f0f113fe13c9c1878d5a514f
Regression attempt 3       6bccfdd0955e2e45f252cc78cdb6929e4d769162b3dec39e3849a2244e1c03c8
Finalization rollback      c107895eb01076e116cec6d24fe8bd4b0064d888b21063817cb6b8e48915e110
Finalization deployment    db0b6b37d3079443ee40e32de98fd71573de9fa615e3fc44af47a4f4f34318f3
Correction rollback        a5a7ad42ce8dbd6b4daddb36133077043a6313b0c228854f07a1996493d188f0
Correction deployment      10c2cec1a9ff509b33d5e044aa071cce4afe634b4dcc966b874e48d97c5dc4be
Three-way benchmark        41d8d3837a7c20adcde2ffed6d146c0babc0b3de7afa6b9c6d1dd6531267902d
Benchmark-fix rollback     4a7a824f59887176794e93a200846990af54f2c2be70f55283672f3b754708c6
Fedora benchfix8           adac5442fd633f4feb73e4d18ef8ba43549baab2c6b4ac7613659107061a1a70
Railway migration/RLS now  151bf07c2cc69435f4f3431d389b1a93c8826539ebf6f2aa92b7d97dbb2f99b9
Corrected three-way run    daf3cb8097d40376f20538cea43dcc4b4d78452949fdf4a618ec906415aa6e3e
```

## Rollback baseline

The pre-mutation Railway/V1/V2 baseline is preserved at:

```text
D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\rollback-baseline.json
D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-v3-finalization-fix-rollback-baseline.json
D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\librechat-v3-toolend-correction-rollback-baseline.json
D:\02_Code\LibreChat_Setup\tmp\v3-deploy\evidence\v3-benchmark-fix6-rollback-baseline.json
```

Fastest shadow disablement is to remove only the new V3 Agent ID from the additive V3 allow-list
and redeploy the already-recorded LibreChat rollback image. Do not change the V1 or V2 Agent IDs,
their file sets, or the active-release pointer.

No rollback action is currently required: the private shadow resources are healthy and isolated.

## Exact next step

The same-prompt comparison and wiki publication are complete. The result shows that V3 is
operationally stable but not yet competitive with V2. The next sequence is:

1. repair canonical table-title, header, unit, footnote, and section-path projection;
2. improve exact certificate, document, part, product-limit, and first-five ranking using only the
   permitted B01-B30 development evidence;
3. rehearse every database/RLS change on a new isolated Fedora database;
4. redeploy only the pinned private V3 candidate, never the active-release pointer;
5. rerun the identical 30-prompt V1/V2/V3 comparison and require V3 to meet the documented gate;
6. after a new explicit one-login authorization, run the 13-case browser-shaped LibreChat
   regression with one in-memory bearer;
7. request independent Bauer gold review and separate promotion authorization only after those
   private-shadow gates pass.

Until those steps succeed, V3 remains a private development shadow only. The 30-prompt benchmark
is complete, but it is not a production-promotion pass. The full authenticated LibreChat
regression is still pending and must not be described as passed.
