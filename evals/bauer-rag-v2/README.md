# Bauer RAG V2 evaluation

This directory is the executable evaluation contract for the additive Bauer document-retrieval
path. JSON syntax is used inside the `.yaml` files; JSON is valid YAML 1.2 and lets the runners use
only the Python standard library.

## Layout

- `baselines/`: frozen deployment, Agent, and 373-file corpus identifiers.
- `cases/`: 30 development and 10 tuning-locked holdout prompts.
- `gold/`: required claims, forbidden claims, evidence locations, constraints, and refusals.
- `runners/`: serial retrieval, end-to-end, and frozen-evidence runners.
- `scorers/`: deterministic retrieval and answer scoring.
- `raw-runs/`: immutable runner output grouped by run ID.
- `reports/`: generated summaries and promotion-gate decisions.

The historical B01-B14 wording is copied exactly from the Bauer Retrieval Benchmark. Additional
cases target the identifiers, tables, certificates, contradictions, and safe refusals that
motivated V2.

## Gold status

The checked-in gold is an engineering seed derived from inspected public corpus records and the
historical benchmark. It is deliberately labelled `provisional_requires_bauer_adjudication`.
Neither Codex nor the local answer model may change that status. A human adjudicator must sign the
gold manifest before a blind holdout result can authorize promotion.

## Fair-run controls

Run systems serially. The retrieval runner alternates V1/V2 order by case and never sends debug
fields to model context. End-to-end runners create fresh conversations. The normal V1 Agent and
Test Archive Agent remain unchanged.

Promotion is blocked unless all hard gates in `gold/promotion-gates.json` pass. A weighted score
cannot override a fabricated identifier, unsupported high-severity number, cross-corpus result,
incorrect refusal, source-type error, or weakened mandatory constraint.

## Runners

- `retrieval.py`: direct V1/V2 retrieval, five serial repetitions, alternating order.
- `end_to_end.py`: fresh LibreChat V1/V2 Agent conversations, serial and alternating.
- `frozen_evidence.py`: the same V2 evidence through an OpenAI-compatible answer endpoint, or
  immutable export-only packages.
- `reference_packages.py` and `import_reference_answers.py`: corpus-restricted Codex reference
  export/import. The package forbids web and outside knowledge; the deterministic scorer remains
  the judge.

Every runner creates a new `raw-runs/<run-id>/run.json` and refuses to overwrite an existing run.
Credentials are environment-only and are not serialized. The holdout requires
`--acknowledge-locked-holdout`.

`raw-runs/v1-historical-20260721` preserves the one-run B01-B14 V1 capture that predates this
implementation. Its limitations are embedded in the artifact. It anchors the historical findings
but does not replace the required five-run V1 baseline once live authentication is available.
