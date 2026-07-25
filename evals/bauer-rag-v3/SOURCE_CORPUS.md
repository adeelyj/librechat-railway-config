# Bauer V3 source corpus contract

`baselines/bauer-source-contract.json` freezes the reviewed V2 dedup selection
as V3 **original-source** inputs:

- exactly 373 selected originals: 166 PDFs and 207 HTML files;
- the existing external file ID for each selected source;
- a portable source-root-relative logical/source path;
- expected source SHA-256 and byte size;
- a deterministic, non-empty two-level navigation category derived only from
  source type and source-path basename;
- prior V2 page-count, OCR, character-count, derivative-checksum, and duplicate
  lineage as metadata only;
- 177 prior duplicate aliases, accounting for all 550 PDF/HTML files in the
  original corpus.

Reviewed Markdown exports are used only to join the existing external IDs.
They are never listed as V3 sources.

## Navigation-only taxonomy

The frozen contract uses taxonomy
`bauer-source-path-navigation-v1`. Categories are deterministic routing aids,
not evidence:

- they are derived only from `source_type` and `source_path`;
- every selected source has exactly two non-empty category levels;
- the contract declares `semantics: navigation-only-non-citable` and
  `citable: false`;
- category labels must never substantiate or be cited in a Bauer answer.
  Only evidence compiled from verified original bytes may do that.

The baseline distribution is:

| Navigation path | Sources |
| --- | ---: |
| Accessories & Consumables > Accessories & maintenance | 18 |
| Applications & Industries > Energy & gas | 17 |
| Applications & Industries > Industry & specialist uses | 28 |
| Archive > Legacy document IDs | 46 |
| Corporate & Compliance > Legal, policy & certification | 21 |
| Corporate & Resources > Downloads & catalogues | 3 |
| Digital, Control & Service > Digital & controls | 19 |
| Documents > Product & technical literature | 18 |
| News & References > Newsletters | 37 |
| News & References > References | 5 |
| Products & Systems > Air & gas treatment | 33 |
| Products & Systems > Compressors & boosters | 35 |
| Products & Systems > Product pages | 69 |
| Products & Systems > Storage, filling & distribution | 24 |

These counts total 373. The current canonical contract SHA-256 is
`40049a12aacb198018a633905d793c8ef9011403f3fdbcd34ed7fe0792ab2580`;
the selected original bytes total 503,391,181 bytes.

## Rebuild and verify

Rebuild and compare the deterministic contract without writing:

```powershell
python evals\bauer-rag-v3\source_contract.py generate `
  --v2-dedup-manifest D:\02_Code\LibreChat_Setup\tmp\corpora\bauer-kompressoren\manifest.json `
  --provision-state D:\02_Code\LibreChat_Setup\tmp\knowledge-base-provision-state.json `
  --source-root "D:\02_Code\Bauer Kompressoren Demo" `
  --output evals\bauer-rag-v3\baselines\bauer-source-contract.json `
  --check
```

Verify the frozen identities directly against the original corpus:

```powershell
python evals\bauer-rag-v3\source_contract.py verify `
  --contract evals\bauer-rag-v3\baselines\bauer-source-contract.json `
  --source-root "D:\02_Code\Bauer Kompressoren Demo"
```

Both commands are read-only with respect to the source root. Generation refuses
an output path inside that root. Any missing source, changed hash/size,
duplicate selected hash/path/external ID, broken prior duplicate alias, or
unaccounted PDF/HTML file fails closed.

After infrastructure UUIDs are chosen, bind the verified contract to the strict
input accepted by `release_control_cli manifest-create`:

```powershell
python evals\bauer-rag-v3\source_contract.py bind-release `
  --contract evals\bauer-rag-v3\baselines\bauer-source-contract.json `
  --source-root "D:\02_Code\Bauer Kompressoren Demo" `
  --tenant-id <tenant-uuid> `
  --knowledge-base-id <kb-uuid> `
  --release-id <release-uuid> `
  --output <release-source-spec.json>
```

The binding carries the corpus checksum and expected source hash/size as
metadata, writes only outside the original-source tree, and refuses to replace
a different existing output. `manifest-create` then re-hashes the same original
bytes and produces the immutable release-bound manifest.
