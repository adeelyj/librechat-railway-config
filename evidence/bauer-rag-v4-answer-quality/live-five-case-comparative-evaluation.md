# Bauer RAG V4 repaired five-case comparative evaluation

This is the primary engineering hard-stop evaluation over the fixed random public-development selection. It compares the repaired local V4 answer against preserved V1–V4 visible outputs. Token presence is diagnostic only; every case must be complete, source-grounded, task-shaped, citation-backed, validator-approved, and free of a critical defect.

- Engineering gate: **PASS**
- Selected cases: B06, B17, B10, B19, B03
- Holdout opened: no
- Independent final review complete: yes
- Owner acceptance: no; this remains a separate decision

| Case | V1 | V2 | V3 | historical V4 | repaired local V4 | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| B06 | 10 | 11 | 2 | 12 | 16 | pass |
| B17 | 3 | 12 | 4 | 12 | 16 | pass |
| B10 | 4 | 13 | 6 | 12 | 16 | pass |
| B19 | 4 | 13 | 16 | 12 | 16 | pass |
| B03 | 10 | 11 | 7 | 5 | 16 | pass |

## Meaning

- B06 now retains historical V4's correct 525-bar compressor result and 520-bar booster distinction, but presents the exact source location instead of planner labels.
- B17 now renders one lossless technical row with correct metric and imperial values; it no longer combines the neighboring 11 kW / 435 kg row.
- B10 now extracts the 2025-03 450-bar i/s option and the 420-bar mobile wording from their sources, rather than embedding those answers in the analyzer.
- B19 now matches the strong historical V3 answer from source-derived facts and adds no unsupported thermodynamic explanation.
- B03 is now complete and authority-safe because the existing catalog is represented as an explicit synthetic source; it reports stored differences and linked IDs without inventing engineering rules.

Passing this bounded gate means the repaired pipeline is materially better on these five selected development cases. It does not establish performance on the other 25 cases, sealed-holdout performance, production promotion, or owner acceptance.
