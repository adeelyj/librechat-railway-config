# Bauer RAG V2 interim gold review

Date: 2026-07-23

Reviewer: OpenAI Codex, acting as the interim gold reviewer at the user's request.

Status: interim review complete for B06, B07, B18, B19, B24, and B29. Final independent
Bauer subject-matter-expert sign-off remains required. The promotion gate therefore remains
closed and the manifest is not marked `verified`.

Reviewed inputs:

- Frozen public corpus under `tmp/corpora/bauer-kompressoren`
- Immutable development run `retrieval-smoke-20260723-05`
- Provisional answer and evidence manifests
- The prompt wording for each reviewed case

## Decisions

### B06 — maximum compressor pressure

Decision: use the April 2026 English `Compressors for Industry` brochure as the canonical
source. Printed page 6 identifies 525 bar as the maximum for air-cooled compressors and names
the I-series VERTICUS and K 22–K 28 families. Printed page 7 separates boosters: air-cooled
boosters reach 420 bar and water-cooled boosters reach 520 bar. Printed page 23 supplies the
technical-table footnote: maximum allowable working pressure is the maximum safety-valve
setting and final/shutdown pressure is lower.

Gold change: require all three source locations and strengthen the answer claims so that both
families, the compressor/booster distinction, and the pressure qualification are present.

Finding: the frozen V2 result does not retrieve the complete canonical evidence set in its top
five. This remains a retrieval gap.

### B07 — I 15.11-11-V exact row

Decision: retain the April 2026 English industry brochure, printed page 23, as canonical.
Accept the indexed page heading (`Page 23`) and `TECHNICAL DATA` table structure rather than
the provisional section label that does not exist in the extracted source. Preserve the exact
row and footnotes.

Gold change: correct the reachable location fields and require the ISO 1217 and
safety-valve/final-pressure qualifications in the answer.

Finding: the canonical evidence is rank 3 in the frozen V2 result.

### B18 — two I 15.11-11-V pressure variants

Decision: retain the April 2026 English industry brochure, printed page 23. The source contains
both rows: 420 l/min at 420 bar and 420 l/min at 525 bar, each with four stages, 1320 rpm, and
11 kW. Footnotes 1 and 2 apply.

Gold change: correct the section and table labels while preserving the exact row and footnote
requirements. The answer manifest already requires both variants and both footnotes.

Finding: the 420-bar row is rank 1 and the 525-bar row is rank 6 in the frozen V2 result. The
answer-level gold detects omission of either variant; the retrieval metric continues to use
the canonical page/table/row location.

### B19 — B-KOOL III limits

Decision: use the April 2026 English `Accessory Systems` brochure, printed page 12, as the
canonical versioned source instead of either of the two near-duplicate web pages. It documents
350/550 bar, 200–700 l/min for cylinder filling, 200–650 l/min according to ISO 1217 for air,
and 200–420 l/min for helium and argon.

Gold change: point evidence to the versioned brochure and require all documented flow limits
and qualifications in the answer.

Finding: the canonical evidence is rank 3 in the frozen V2 result.

### B24 — order number N7698

Decision: retain the English high-pressure accessories catalogue, printed page 4. The exact
`Use / Order number` row attaches N7698 to large blocks/medium pressure applications:
K28.3, 21.0, 25.0, 23.1, 25.4, K28.0, and K28.2. `Intake pre-filter` is not the row's section
and must not be required.

Gold change: use the actual page/table location and require the complete application list.

Finding: the exact evidence is rank 1 in the frozen V2 result.

### B29 — K 22–K 28 family range

Decision: follow the prompt literally and use the June 2025 English `Product overview`,
printed page 31. It documents 600–6,800 l/min, 22–110 kW, and 30–525 bar for the family.
The provisional gold incorrectly used printed page 20 of `Compressors for Industry`, which
documents 800–6,800 l/min and is not the product-overview page named by the prompt.

Gold change: replace the canonical file/page and correct 800 to 600.

Finding: the frozen V2 result retrieves the correct source content at rank 1 but assigns it
page 30 because the prose chunk crosses the `Page 31` boundary. Gold retains the printed source
page 31. This is an extractor/page-attribution defect, not a reason to weaken gold.

## Sign-off boundary

This review resolves the six documented disputes sufficiently for continued development
tuning. It does not constitute independent Bauer approval because the reviewer is also part of
the implementation process and is not a Bauer product owner. Before promotion:

1. A Bauer subject-matter expert must verify the source selections and answer claims.
2. The manifest status must be changed to `verified` only after that independent sign-off.
3. The locked holdout and five serial promotion repetitions must remain unopened until tuning
   is frozen.
