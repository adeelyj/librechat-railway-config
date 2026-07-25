# Bauer Evidence V3 release control

Run release control from the same compiler image used by the worker. That makes
the release fingerprint describe the parser code, dependency versions,
projection code, embedding contract, OCR engine, and OCR model files that will
actually compile the sources.

```text
python -m bauer_evidence_v3.release_control_cli --help
```

The control CLI does not load API generation-model configuration. Database
commands use only:

```text
BAUER_V3_DATABASE_URL
BAUER_V3_TENANT_ID
BAUER_V3_KNOWLEDGE_BASE_ID
BAUER_V3_PRINCIPAL_IDS_JSON
```

Except for the explicit schema-owner bootstrap path, database commands require an exact non-owner
`bauer_rag_v3_admin` login. Evaluation persistence deliberately uses a different exact
`bauer_rag_v3_evaluator` login; neither credential is interchangeable with the other or with the
reader/ingester logins. Independent gold attestation uses a fifth exact
`bauer_rag_v3_reviewer` login and cannot be performed by either the evaluator or release admin.

Release staging also uses the `BAUER_V3_OBJECT_STORE_*`, primary
`BAUER_V3_S3_*`, mirror `BAUER_V3_MIRROR_S3_*`, and compiler/OCR variables
used by the worker. Production staging rejects a local store and requires the
independent S3 mirror. Production compiler identity requires OCR to be enabled
with checksum-pinned detection, recognition, and classification model paths,
plus explicit languages, minimum confidence, and render DPI:

```text
BAUER_V3_OCR_ENABLED=true
BAUER_V3_OCR_DETECTION_MODEL=/models/<detection-model>.onnx
BAUER_V3_OCR_DETECTION_MODEL_SHA256=<sha256>
BAUER_V3_OCR_RECOGNITION_MODEL=/models/<recognition-model>.onnx
BAUER_V3_OCR_RECOGNITION_MODEL_SHA256=<sha256>
BAUER_V3_OCR_CLASSIFICATION_MODEL=/models/<classification-model>.onnx
BAUER_V3_OCR_CLASSIFICATION_MODEL_SHA256=<sha256>
BAUER_V3_OCR_LANGUAGES_JSON=["de","en"]
BAUER_V3_OCR_MINIMUM_CONFIDENCE=0.80
BAUER_V3_OCR_RENDER_DPI=150
```

Model downloads during a job are forbidden.

## 1. Derive the compiler identity

```text
python -m bauer_evidence_v3.release_control_cli toolchain \
  --embedding-model-version <immutable-model-version> \
  --embedding-dimensions 1024
```

Capture the printed fingerprint as a deployment/build assertion. The release
build derives it again; `--expected-toolchain-fingerprint` checks the captured
value but never replaces the runtime-derived identity.

## 2. Verify and bind the Bauer original-source contract

The frozen contract contains 373 selected originals (166 PDF and 207 HTML),
177 duplicate aliases accounting for all 550 reviewed inputs, and 503,391,181
selected bytes. Its SHA-256 is
`40049a12aacb198018a633905d793c8ef9011403f3fdbcd34ed7fe0792ab2580`.

Verify the contract against the immutable source tree:

```text
python evals/bauer-rag-v3/source_contract.py verify \
  --contract evals/bauer-rag-v3/baselines/bauer-source-contract.json \
  --source-root /original-bauer-files
```

Run the source-contract commands from the repository checkout; the worker
image does not contain the evaluation directory. Run the release-control CLI
from the exact compiler image after the bound specification is available to
that environment.

After the deployment tenant, knowledge-base, and release UUIDs are chosen,
bind that verified contract to exactly one release:

```text
python evals/bauer-rag-v3/source_contract.py bind-release \
  --contract evals/bauer-rag-v3/baselines/bauer-source-contract.json \
  --source-root /original-bauer-files \
  --tenant-id <tenant-uuid> \
  --knowledge-base-id <kb-uuid> \
  --release-id <release-uuid> \
  --output release-sources.json
```

The binder writes outside the source tree, refuses a different existing
output, and carries each expected source SHA-256 and byte count into the
release specification. The contract's category path is navigation-only and
non-citable.

## 3. Create and verify an original-source manifest

The manifest creation input is strict JSON:

```json
{
  "tenant_id": "10000000-0000-0000-0000-000000000001",
  "knowledge_base_id": "20000000-0000-0000-0000-000000000001",
  "release_id": "40000000-0000-0000-0000-000000000001",
  "sources": [
    {
      "external_file_id": "manual-p-100",
      "logical_path": "manuals/p-100.pdf",
      "source_type": "pdf",
      "declared_media_type": "application/pdf",
      "visibility": "restricted",
      "category_path": ["Products", "P-100"],
      "metadata": {
        "product_tag": "P-100",
        "source_contract_sha256": "<sha256>",
        "expected_source_sha256": "<sha256>",
        "expected_source_byte_size": 123456
      }
    }
  ]
}
```

Paths are relative to the supplied source root. Derivatives, traversal paths,
unsupported formats, duplicate IDs, and duplicate paths are rejected.

```text
python -m bauer_evidence_v3.release_control_cli manifest-create \
  --spec sources.json \
  --source-root /original-bauer-files \
  --output release.manifest.json

python -m bauer_evidence_v3.release_control_cli manifest-verify \
  --manifest release.manifest.json \
  --source-root /original-bauer-files
```

An existing different output manifest is never overwritten.

For Bauer, `release-sources.json` must come from the contract-binding command
above. `manifest-create` re-reads the same original bytes and rejects any
source whose bound hash or byte count has drifted, before writing the
immutable release manifest.

## 4. Bootstrap the database scope

Bootstrap is idempotent and must run with the dedicated bootstrap/schema-owner
credential, never an API or worker runtime credential. Its strict JSON input is:

```json
{
  "external_key": "bauer",
  "tenant_display_name": "Bauer",
  "external_namespace": "bauer-evidence",
  "kb_name": "Bauer Evidence",
  "principals": [
    {
      "principal_id": "30000000-0000-0000-0000-000000000001",
      "principal_type": "service",
      "external_subject": "bauer-v3-release-admin"
    }
  ],
  "grants": [
    {
      "principal_id": "30000000-0000-0000-0000-000000000001",
      "permission": "admin",
      "granted_by": "bootstrap"
    }
  ]
}
```

```text
python -m bauer_evidence_v3.release_control_cli bootstrap \
  --spec bootstrap.json
```

The optional `kb_id` must match `BAUER_V3_KNOWLEDGE_BASE_ID`.

## 5. Stage and build

```text
python -m bauer_evidence_v3.release_control_cli release-build \
  --manifest release.manifest.json \
  --source-root /original-bauer-files \
  --created-by <operator-or-build-id> \
  --embedding-model-version <immutable-model-version> \
  --expected-toolchain-fingerprint <sha256>
```

This command verifies every source, writes the canonical manifest and originals
to content-addressed storage, creates the immutable draft release, advances it
to `building`, and idempotently enqueues one compile job per source. It never
activates the release.

## 6. Validate, inspect, and mark ready

```text
python -m bauer_evidence_v3.release_control_cli release-begin-validation \
  --release-id <uuid>

python -m bauer_evidence_v3.release_control_cli release-status \
  --release-id <uuid>

python -m bauer_evidence_v3.release_control_cli release-mark-ready \
  --release-id <uuid>
```

The database remains authoritative for evidence completeness, blocking QA, and
verified-gold evaluation gates. There is no force-ready option.

Stop a draft/building/validating release, or retire an inactive ready release:

```text
python -m bauer_evidence_v3.release_control_cli release-mark-failed \
  --release-id <uuid> \
  --error "<terminal reason>"

python -m bauer_evidence_v3.release_control_cli release-retire \
  --release-id <uuid>
```

## 7. Replay a dead job

Inspect the job and release and correct the underlying failure first. Then
create one linked, audited replay through the release-control path:

```text
python -m bauer_evidence_v3.release_control_cli job-replay-dead-letter \
  --job-id <dead-job-uuid> \
  --idempotency-key <incident-specific-new-key> \
  --actor-principal-id <operator-principal-uuid>
```

The replay uses a new job ID, records `replay_of_job_id` and the actor event,
and is idempotent only for the same replay key. It rejects reuse of the
original or an occupied key and refuses replay into a terminal release.

## 8. Record the independent gold attestation

The independent Bauer reviewer signs a review packet outside this repository. Provision a
dedicated login that inherits only `bauer_rag_v3_reviewer`, plus one active reviewer principal in
the target tenant. Inject those values without placing credentials on the command line:

```text
BAUER_V3_REVIEW_DATABASE_URL=<exact reviewer-role PostgreSQL DSN>
BAUER_V3_REVIEW_TENANT_ID=<tenant UUID>
BAUER_V3_REVIEW_PRINCIPAL_ID=<independent reviewer principal UUID>
```

After independently confirming the displayed manifest digest, bind the one-time decision to both
the immutable gold manifest and signed packet:

```text
python -m bauer_evidence_v3.gold_review_cli \
  --manifest <independent-reviewed-gold.yaml> \
  --split development \
  --review-evidence <independently-signed-review.packet> \
  --decision approve \
  --confirm-manifest-sha256 <independently-confirmed-sha256>
```

The reviewer path cannot run an evaluation, alter its results, change ACLs, or activate a release.
The evaluator and release admin cannot insert an attestation. A rejection is terminal for that
suite manifest; corrections require a new immutable suite version and a new review.

## 9. Explicit production pointer changes

Activation and rollback always call the production database gate. Both require
a scoped actor, a reason, and the explicit confirmation flag:

```text
python -m bauer_evidence_v3.release_control_cli release-activate \
  --release-id <uuid> \
  --actor-principal-id <uuid> \
  --reason "<promotion reason>" \
  --confirm-active-pointer-change

python -m bauer_evidence_v3.release_control_cli release-rollback \
  --target-release-id <earlier-ready-release-uuid> \
  --actor-principal-id <uuid> \
  --reason "<rollback reason>" \
  --confirm-active-pointer-change
```

Production activation still requires the database's `independent_bauer_verified` gold gate, a
passing immutable run, and the matching approved independent-review attestation. The CLI cannot
bypass them.

No V3 candidate release, benchmark, Railway deployment, live V3 Agent route,
or activation exists yet. Before any pointer change, complete the real
PostgreSQL/RLS exercise, managed storage and pinned model/OCR setup, full
373-source build, difficult extraction/table fixture review, fixed-candidate
V2/V3 shadow comparison, independent Bauer gold signoff, and separate
deployment/activation approval.
