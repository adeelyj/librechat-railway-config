# Bauer Evidence V4

V4 is a host-neutral evidence and answer service derived from the exact V3 Git history. It keeps
V3's source authority, provenance, release pinning, authorization, roles, and RLS design while
replacing the lossy representation and client boundary.

The public contract is under `bauer_evidence_v4/contracts`. It deliberately has no LibreChat,
ONIX, database, or model-client dependencies. Host-specific request mapping lives under the
repository-level `adapters/` directory.

Generate and verify the contract artifacts:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\bauer-evidence-v4').Path
python services\bauer-evidence-v4\scripts\generate_contract_artifacts.py
python -m unittest discover -s services\bauer-evidence-v4\tests -p 'test_*.py' -v
```

The original `question` is immutable request data. `search_hint` is optional derived data and may
only influence candidate generation.

The canonical compiler is under `bauer_evidence_v4/canonical` and
`bauer_evidence_v4/compilation`. It evaluates independent HTML/PDF parser candidates against
semantic quality gates, reconstructs table grids and inherited header paths, attaches units,
qualifiers, sections, captions, and footnotes, emits typed facts with exact provenance, and
quarantines unresolved sources.

Verify the unlocked difficult-document compiler suite:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\bauer-evidence-v4').Path
python -m pytest services\bauer-evidence-v4\tests evals\bauer-rag-v4\tests -q
python services\bauer-evidence-v4\scripts\verify_compiler_fixtures.py `
  --fixture evals\bauer-rag-v4\fixtures\difficult-documents.json `
  --source-root 'D:\02_Code\Bauer Kompressoren Demo'
```

Search projections retain canonical evidence identifiers and exact coordinates.
Embedding reuse is keyed by the complete provider/model/revision/dimensions/
normalization/search-text identity. Candidate generation has exactly three
authorization-filtered families: exact/structured, lexical/trigram, and dense.
The selected `transparent-linear-v1` reranker exposes every feature and uses
deterministic tie-breaking.

Answering is coverage-first: the service analyzes requested fields without
rewriting the original question, retrieves per subquestion, materializes exact
evidence envelopes, checks field coverage, renders only supported requested
fields, validates support/citations/constraints, and permits one targeted
repair. It never falls back to an evidence dump.

Verify the public development answer gates:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\bauer-evidence-v4').Path
python services\bauer-evidence-v4\scripts\verify_answering.py `
  --fixture evals\bauer-rag-v4\fixtures\difficult-documents.json `
  --cases evals\bauer-rag-v4\cases\development-retrieval.json `
  --gates evals\bauer-rag-v4\gates\development-gates.json `
  --source-root 'D:\02_Code\Bauer Kompressoren Demo'
```
