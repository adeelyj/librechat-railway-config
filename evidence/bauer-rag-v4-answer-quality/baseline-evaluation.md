# Bauer RAG V4 Five-Case Historical Baseline Evaluation

- Date: 2026-07-31
- Split: public development
- Holdout opened: no
- Selected cases: B06, B17, B10, B19, B03
- Visible-answer source: `baseline-five-case-v1-v4.json`
- Review status: primary engineering review; final independent review still required

## Bottom line

No historical version passes all five selected cases. V4 has the best supported technical content
for B06, B17, and B10, but exposes planner/coverage labels instead of answering naturally. V3 is
the strongest historical answer for B19. No version gives a fully supported B03 answer: V1/V2 add
unsupported engineering implications, while V3/V4 refuse or report missing evidence.

The immediate product meaning is that answer presence and token coverage were misleading release
signals. The repaired V4 must combine V4's strongest supported facts with readable task completion,
and it must add the already-existing synthetic demo records as an explicitly labeled authorized
source before B03 can be answered safely.

## Rubric

Each dimension is scored from `0` (fail) through `1` (partial) to `2` (pass): task fulfillment,
factual correctness, completeness, clarity/directness, requested structure, citation support,
uncertainty/conflict handling, and presentation hygiene. A critical failure overrides the total.

The review treats token-group presence as diagnostic only. It never changes a case verdict.

## Case verdicts

### B06 — highest compressor operating pressure

| System | Score / 16 | Critical failure | Meaning |
| --- | ---: | --- | --- |
| V1 | 10 | yes | Selects 420 bar from a news passage, names unrelated product families, and does not establish the actual maximum. |
| V2 | 11 | yes | Improves the pressure definition but incorrectly treats 520 bar GIB material as the highest compressor result and misses the 525 bar compressor families. |
| V3 | 2 | yes | Emits a malformed technical row with conflated values and does not perform the requested comparison. |
| V4 | 12 | yes | Correctly separates 525 bar compressors from 520 bar boosters and explains shutdown pressure, but exposes `Supported result` and internal coverage labels. |

Strongest supported baseline: V4. Repair target: retain V4's facts and citations while answering in
plain prose or a compact compressor/booster table.

### B17 — complete BM 6.1/100-15 row

| System | Score / 16 | Critical failure | Meaning |
| --- | ---: | --- | --- |
| V1 | 3 | yes | Repeatedly searches and incorrectly concludes the row is absent. |
| V2 | 12 | yes | Uses a readable table but gives the wrong motor power and net weight (`11 kW`, `435 kg`). |
| V3 | 4 | yes | Contains some correct values but drops units and conflates metric/imperial cells into repeated labels. |
| V4 | 12 | yes | Contains the correct complete metric/imperial values, but duplicates group/model values and does not render the requested single row. |

Strongest supported baseline: V4 for correctness, V2 for presentation only. Repair target: one
lossless row with headers, units, and footnotes, with no duplicated group-level values.

### B10 — B-DETECTION PLUS i/s versus m

| System | Score / 16 | Critical failure | Meaning |
| --- | ---: | --- | --- |
| V1 | 4 | yes | Ends in an unsupported not-found conclusion after extensive tool-process narration. |
| V2 | 13 | yes | Highly readable and mostly complete, but includes weakly supported logging/date generalizations and visible tool-process narration. |
| V3 | 6 | yes | Refuses instead of answering despite supported public evidence. |
| V4 | 12 | yes | Captures the supported stationary/mobile, measurement, logging, and 420/450 bar distinction, but the values are pre-written in code and displayed as internal coverage fields. |

Strongest presentation baseline: V2. Strongest bounded fact set: historical V4, but it is
inadmissible as validation because the analyzer and coverage engine embed expected answer wording.
Repair target: extract the values from dated sources and present a concise comparison table plus a
source-date explanation.

### B19 — B-KOOL III limits

| System | Score / 16 | Critical failure | Meaning |
| --- | ---: | --- | --- |
| V1 | 4 | yes | Incorrectly concludes the technical data is absent. |
| V2 | 13 | yes | Gives the requested values clearly but adds an unsupported thermodynamic explanation. |
| V3 | 16 | no | Directly states the supported pressure and all three flow ranges with adjacent evidence IDs. |
| V4 | 12 | yes | Gives the right values but they are pre-written in analyzer/coverage code and displayed with internal labels. |

Strongest supported baseline: V3. Repair target: extract the same facts from the technical row and
render them at least as clearly as V3 without adding an explanation the source does not make.

### B03 — synthetic project comparison

| System | Score / 16 | Critical failure | Meaning |
| --- | ---: | --- | --- |
| V1 | 10 | yes | Performs the comparison but invents standards, design implications, and component requirements not established by the synthetic records. |
| V2 | 11 | yes | More readable, but still adds ungrounded engineering implications and regulatory assumptions. |
| V3 | 7 | yes | Safely states the authority limitation but does not perform the requested comparison. |
| V4 | 5 | yes | Correctly refuses unsupported public evidence, but the synthetic demo records are not in its authorized evidence path. |

Strongest safe baseline: none. Repair target: ingest the existing `bauer_synthetic_demo_v1`
records as an immutable, explicitly synthetic source using the existing lexical/vector/structural
retrieval families. Compare only stored attributes, linked record IDs, document IDs, and recorded
review assumptions. Do not infer component redesign or compatibility rules.

## Stage diagnosis

| Case | Representation | Candidate retrieval | Coverage | Answering/validation |
| --- | --- | --- | --- | --- |
| B06 | Required industry overview facts exist | V4 retrieves the decisive source | V4 extracts the correct distinctions | Mechanical field labels reach the user |
| B17 | Canonical table row preserves the correct values and units | V4 finds the row | Group/model projection duplication remains | Requested single-row format is not honored |
| B10 | Dated sources exist separately | Retrieval finds both | Values are hardcoded/canned rather than extracted | Mechanical output can pass token checks |
| B19 | Technical row exists | Retrieval finds it | Values are hardcoded/canned rather than extracted | Mechanical output can pass token checks |
| B03 | Synthetic catalog exists outside V4 canonical evidence | No authorized V4 candidate exists | Coverage is absent | Safe refusal, but task cannot be completed |

## Frozen repair implications

1. Remove literal answer values from B10/B19 search hints before measuring retrieval.
2. Replace canned special-case sentences with source-derived structured facts.
3. Deduplicate B17 at the row contract, not by post-render string cleanup.
4. Add the existing synthetic catalog as an explicit synthetic canonical source inside the same
   three retrieval families; this measured gap is not permission for another agent or tool.
5. Make task-shaped visible output and the hard-stop checklist—not token presence—the AQ7 gate.

## Limitations

This is a bounded engineering review of five public-development prompts and preserved visible
answers. It is not representative human-reviewed truth, does not measure the other 25 development
cases, does not open the holdout, and does not establish owner acceptance.
