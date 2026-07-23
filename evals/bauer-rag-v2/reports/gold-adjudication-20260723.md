# Bauer RAG V2 gold-adjudication audit

Date: 2026-07-23

Status: human review required before holdout or promotion evaluation.

Source run:
`../raw-runs/retrieval-smoke-20260723-04/run.json`

The final development smoke reached 66.67% exact metadata success against the provisional gold.
Three exact cases fail because the expected section/table labels are not represented by the
indexed source structure even though V2 returns the requested exact rows. Three additional cases
need a source-selection decision. These should be adjudicated before further retrieval tuning so
that the system is not optimized to disputed labels.

## Required adjudication

### B07 and B18 — I 15.11-11-V

V2 returns exact `I 15.11-11-V` table rows from the required English industry brochure on page 23,
including both pressure variants and footnotes 1 and 2. B18 ranks the required file/page/row first;
B07 ranks it third behind a current Bauer VERTICUS product page.

The provisional location requires section `Technical Data – Air Cooled Compressor Units` and
table `VERTICUS SERIES`. The extracted page exposes section
`2026-04 Compressors for Industry EN N39771 sc / Page 23`, table `TECHNICAL DATA`, and the VERTICUS
series/range in table headers. No indexed chunk can match the provisional section string.

Decision needed: either approve file + page + exact row + header/footnote matching as sufficient,
or specify a deterministic heading-to-table association rule that can be verified from the source.

### B24 — N7698

V2 ranks the exact `N7698` row first in the required accessories catalogue on page 4. It preserves
the application list and exposes table `Page 4 / Use / Order number`.

The provisional location additionally requires section `Intake pre-filter`. That label is earlier
on the flattened page and is not attached to the `N7698` table row in the source structure. No
current indexed chunk can satisfy both the exact row and that section.

Decision needed: approve file + page + `Use / Order number` + exact row matching, or identify a
source-level relationship that makes `Intake pre-filter` mandatory for this row.

## Source-selection review

### B06 — maximum compressor pressure

V2 returns structured compressor rows reaching 525 bar, but current Bauer product pages rank above
the brochure chosen by gold. The required brochure page is present at rank 10. The query asks for
the highest documented value rather than a specific brochure, so the reviewer should decide
whether any current authorized public Bauer source is valid or whether the brochure is mandatory.

### B19 — B-KOOL III

V2 ranks two near-duplicate current Bauer B-KOOL pages first and second; the gold file is second.
Both expose the exact product, but the web-page extraction does not emit the provisional
`Technical Data` / `B-KOOL Refrigeration dryer` labels. The reviewer should select the canonical
duplicate and confirm the acceptable location fields.

### B29 — K 22–K 28 product overview

The prompt explicitly asks for the product overview page. V2 ranks
`2025-06_Product_overview_EN_N37488_sc`, page 30, first. Provisional gold instead requires the
`Compressors for Industry` brochure, page 20. The reviewer should select which document the prompt
intends and update either the prompt or gold location so they agree.

## Adjudication procedure

1. Review the named source pages and the returned immutable evidence in the source run.
2. Confirm the canonical file, page, section, table, row, and footnote requirements for each case.
3. Update the gold manifest only from that decision and record the reviewer/date.
4. Rerun the development retrieval suite and require the 95% exact lookup gate without weakening
   authorization, safety, or mandatory evidence constraints.
5. Freeze tuning, change `gold_status` to `verified`, then run five serial repetitions of
   retrieval-only, V1/V2/Codex end-to-end, frozen-evidence comparison, and the locked holdout.

Until those steps are complete, the scorer must continue returning `promotion_allowed: false`.
