# Bauer RAG V4 answer-quality repair evidence — 2026-07-31

Status: **implemented, independently reviewed, deployed, and live-verified in the authorized
private shadow**. Production promotion and owner acceptance remain false.

## Meaning

The poor visible V4 answers were not primarily caused by DeepSeek. The old backend gave the model
mechanical planner labels and uneven raw excerpts, sometimes omitted requested facts, and then
treated syntactically valid output as success. DeepSeek made that weak evidence packet visible.

The repair changes the backend contract: the full original question remains authoritative; search
hints remain separate; source representation and row identity are repaired before retrieval;
exact, lexical, and dense candidates remain the only three retrieval families; coverage is built
from source-bound facts; and the validated task-shaped answer is materialized at the LibreChat
boundary. DeepSeek is still the Agent model, but it no longer decides how to turn an evidence dump
into the final Bauer answer.

## Live private-shadow identity

| Item | Value |
| --- | --- |
| Branch | `codex/bauer-rag-v4-answer-quality` |
| Candidate release | `ccb9e8ea-5894-405f-aac4-c4eae5b0d661` |
| Public release ID | `bauer-rag-v4-private-20260731-aq-r2` |
| API commit / deployment | `84e63d76d7a91aa73fe963ad8a054761c5bde432` / `4a8ce7b7-968e-480f-a879-09606af9c70e` |
| Worker deployment | `7f9806e1-32b8-4cb2-bfcb-ca9d2e9c8cf1` |
| LibreChat overlay / deployment | `e9c9f0916773059faf9d0c184e5c93540057c742` / `301bef74-9f25-4428-bfc6-46d48123245c` |
| Private Agent | `Bauer Kompressoren - RAG V4 Private Shadow` |
| Sources / artifacts / projections | 374 / 374 / 25,131 |
| Active-release pointers | 0 |

The 374 sources are the original 373 protected Bauer files plus one reviewed synthetic demo source
used to represent the already-public B03 comparison fixture. It passes through the same V4
compiler, retrieval, reranking, coverage, answering, and validation path; it is not a fourth
retrieval channel. All 22,840 reusable V3 embedding identities were reused, with 38 new identities
for the new source.

## Five-case comparative result

The cases were selected before repair by a fixed seeded random choice from the public B01-B30
development population. Only repaired V4 was rerun live. Its exact outputs were compared with the
preserved V1, V2, V3, and historical V4 outputs.

| Case | V1 | V2 | V3 | historical V4 | repaired live V4 | Meaning |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| B06 | 10 | 11 | 2 | 12 | **16** | 525-bar compressor maximum, 520-bar booster distinction, and pressure terminology with exact locations |
| B17 | 3 | 12 | 4 | 12 | **16** | One lossless BM 6.1/100-15 row, correct metric/imperial values, no neighboring row |
| B10 | 4 | 13 | 6 | 12 | **16** | Stationary/mobile fit, measurements, integrated logger, and dated 450/420-bar reconciliation |
| B19 | 4 | 13 | 16 | 12 | **16** | Both pressure limits and all gas-specific flow ranges without invented explanation |
| B03 | 10 | 11 | 7 | 5 | **16** | Complete synthetic comparison with explicit data-authority and engineering-approval boundaries |

All five exact live answers:

- completed without an execution error;
- passed the V4 validator with repair count zero;
- satisfied every task-specific correctness, completeness, structure, citation, uncertainty, and
  presentation check;
- met or exceeded the best preserved V1-V4 score;
- used only authorized evidence and deleted their temporary conversations.

The exact prompts, answers, citations, validation fingerprints, answer hashes, historical scores,
and deployment identity are in
`evidence/bauer-rag-v4-answer-quality/live-five-case-v4-attested.json` and its readable Markdown
companion. The source-controlled comparative result is
`evidence/bauer-rag-v4-answer-quality/live-five-case-comparative-evaluation.json`.

## Engineering and review verification

- V4 Python suite after the final functional change: 77/77 passed.
- LibreChat fail-closed and authorization suite after attestation persistence: 43/43 passed.
- Independent final review: one valid high finding was repaired; re-review verdict
  `MUST_FIX_RESOLVED`, with no must-fix remaining.
- Independent multi-release persistence review: completed before the live r2 release.
- Independent B-DETECTION coverage review: `No issues found` after the integrated-data-logger
  coverage correction.
- Locked holdout opened: no.
- Credentials, tokens, and connection details emitted: no.

## Operational boundary

The private V4 release is ready but deliberately unpointed. V1, V2, V3, Railway production
selectors, and ordinary LibreChat behavior were not changed. The latest `/ready/v4` evidence
reported 374 artifacts, 25,131 projections, the fixed candidate release, and
`active_release_pointer_used: false`.

The Fedora host was unreachable for a fresh 2026-07-31 rehearsal. The answer-quality repair did
not change migrations or schema. All seven LF-normalized migration checksums match the prior
passing PostgreSQL 18.3 rehearsal, including two forward/rollback cycles, roles, RLS, release
pinning, rollback, failure recovery, and cleanup. This is explicitly carry-forward operational
evidence, not a claim that a new Fedora rehearsal ran.

## Limits and decision boundary

Five public-development cases do not establish quality for the other 25 cases or a newly sealed
holdout. Independent review is not owner acceptance. The current truthful claim is:

> V4 answer-quality repair is implemented, independently reviewed, deployed, and verified on the
> fixed five-case public-development sample in the private shadow. It is not production-promoted
> and has not yet been accepted by the owner.

## Sources

- `evidence/bauer-rag-v4-answer-quality/live-five-case-v4-attested.json`
- `evidence/bauer-rag-v4-answer-quality/live-five-case-comparative-evaluation.json`
- `evidence/bauer-rag-v4-answer-quality/independent-claude-final-review.md`
- `evidence/bauer-rag-v4-answer-quality/independent-claude-release-persistence-review.md`
- `evidence/bauer-rag-v4-answer-quality/independent-claude-bdetection-coverage-review.md`
- `evidence/bauer-rag-v4-answer-quality/fedora-operational-rehearsal-carry-forward.json`
