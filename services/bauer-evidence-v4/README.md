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
