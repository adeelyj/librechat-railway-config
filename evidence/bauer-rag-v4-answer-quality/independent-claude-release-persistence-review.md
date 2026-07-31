# Independent Claude review — V4 multi-release persistence repair

- Date: 2026-07-31
- Reviewer: Claude Code 2.1.220, non-persistent read-only review
- Scope: release-scoped normalized database IDs and paused-build activation control
- Railway/LibreChat/holdout accessed by reviewer: no

## Incident meaning

The first r2 build attempt exposed two deployment assumptions that the original single-release V4
had not exercised:

1. normalized canonical rows used globally primary-keyed deterministic IDs, so a second release of
   the same source documents collided with r1; and
2. the worker lease function can claim any queued build in the tenant/knowledge base before its
   configured release ID is updated.

The build failed closed with seven dead jobs, zero published candidate artifacts, and zero active
release pointers. The worker was scaled to zero and all 374 r2 jobs were reset to a paused
`available_at = infinity` state before repair.

## Repair reviewed

- Database-only normalized IDs for documents, blocks, tables, cells, facts, records, and search
  projections are deterministically namespaced by release. Canonical JSON and projections JSON
  retain their stable canonical IDs; the API loads those JSON artifacts, so visible citations do
  not change.
- Fact provenance and projection canonical-evidence ID arrays are mapped into the same
  release-scoped database namespace.
- Embedding identity remains based on search-text SHA and the exact embedding specification; the
  shared embedding cache is neither release-namespaced nor cleared by the pause operation.
- New releases begin with paused jobs. `ActivateBuild` requires building status, exactly 374
  paused jobs, zero candidate artifacts/documents, and zero active pointers before making jobs
  available.

## Review findings and resolution

The initial focused review returned `MUST_FIX_REMAINS` because HTML metadata projections can cite
`record_*` IDs and the first ID-kind allow-list omitted `record`. That would have dead-lettered
record-bearing HTML sources. The finding was accepted and repaired by adding `record` to the one
canonical ID-kind gate.

Regression coverage now includes:

- all seven canonical ID kinds across two release identities;
- mixed `record_*` and `block_*` evidence arrays; and
- the real `english-en-iso-3834-2-certificate` fixture, which is required to contain a `record_*`
  evidence ID and whose every projection evidence ID must be release-scopeable.

Focused tests passed 13/13. Claude's focused re-review returned `MUST_FIX_RESOLVED`, `MUST-FIX:
None`, and `VALID MUST-FIX REMAINS: no`.

## Boundary

This review validates the code path and local fixtures. It is not a live PostgreSQL rehearsal, a
sealed-holdout result, production promotion, or owner acceptance. Live r2 compilation and release
accounting remain separate gates.
