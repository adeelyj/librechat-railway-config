# Bauer RAG V4 live five-case answer-quality attestation

Status: **verified private shadow; not production-promoted; owner acceptance pending**.

Five selected public-development cases passed a case-level answer-quality hard stop against preserved V1-V4 outputs; this is not evidence for the other 25 cases.

| Case | V1 | V2 | V3 | historical V4 | repaired live V4 | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| B06 | 10 | 11 | 2 | 12 | 16 | pass |
| B17 | 3 | 12 | 4 | 12 | 16 | pass |
| B10 | 4 | 13 | 6 | 12 | 16 | pass |
| B19 | 4 | 13 | 16 | 12 | 16 | pass |
| B03 | 10 | 11 | 7 | 5 | 16 | pass |

## What passed

- 5/5 exact live LibreChat answers completed with no execution error.
- 5/5 passed the existing V4 answer validator with zero repair attempts.
- 5/5 met or exceeded the best preserved V1-V4 result under the case-level hard stop.
- Every temporary conversation created by the authoritative run was deleted.
- No authorization violation occurred; no credential or token was emitted; the holdout was not opened.

## Exact outputs

<details>
<summary><strong>B06 exact live V4 output</strong></summary>

**Prompt:** Based only on the uploaded public Bauer documents, what is the highest documented maximum operating pressure for a Bauer compressor? Name the applicable product families, distinguish compressors from boosters, and cite the exact source page or section. Also explain the difference between maximum operating pressure and shutdown pressure if the source makes that distinction. Do not assume that the most frequently retrieved pressure is the maximum.

```text
The highest documented maximum operating pressure for a BAUER compressor in the authorized evidence is **525 bar**.

## Compressors
Highest compressor maximum operating pressure: 525 bar; I Series | VERTICUS: 90–525 bar; I Series | K 22 - K 28: 90–525 bar. [citation_4a2d1c091eccd43ed64956bf1bb4e9c7]
Source: 2026-04_Compressors_for_Industry_EN_N39771_sc.pdf, PDF page 6, section 6 | PERFORMANCE OVERVIEW | COMPRESSORS FOR INDUSTRY BAUER KOMPRESSOREN > PERFORMANCE OVERVIEW > EXCELLENT COMPRESSOR SOLUTIONS FOR YOUR REQUIREMENTS, [citation_4a2d1c091eccd43ed64956bf1bb4e9c7]

## Boosters
BOOSTER AIR COOLED: 25–420 bar; BOOSTER WATER COOLED: 25–520 bar; GIB Series | BK 23 – BK 52: 90–520 bar. [citation_e962131b086fa77b196ba2d534cdd9b9]
Source: 2026-04_Compressors_for_Industry_EN_N39771_sc.pdf, printed page 7, section BAUER KOMPRESSOREN COMPRESSORS FOR INDUSTRY | PERFORMANCE OVERVIEW | 7, [citation_e962131b086fa77b196ba2d534cdd9b9]

## Pressure terminology
Maximum allowable working pressure = max. setting safety valve; final pressure (shut-down pressure) lower. [citation_e0e7757ddf613f7f9603c8aa1c1d74c8]
Source: 2026-04_Compressors_for_Industry_EN_N39771_sc.pdf, printed page 23, section BAUER KOMPRESSOREN COMPRESSORS FOR INDUSTRY | AIR-COOLED COMPRESSOR UNITS & BOOSTER | 23, [citation_e0e7757ddf613f7f9603c8aa1c1d74c8]
```

Answer SHA-256: `be49fd3f0b6d7fd5dccf2cca61e75d5700b9a8bad41b053711d676d1de5b310e`

Validation: passed, mode `grounded_structured`, repair count 0

Prior best: 12/16; repaired live V4: 16/16

</details>

<details>
<summary><strong>B17 exact live V4 output</strong></summary>

**Prompt:** In the BM series 100 bar 50 Hz technical table, give the complete row for BM 6.1/100-15 and preserve the table units.

```text
| Model | Effective free air delivery | Shutdown pressure | Number of stages | Rotational speed approx. | Motor power | Net weight approx. | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BM series 100 bar – 50 Hz: BM 6.1/100-15 | 22.2 cfm<br>37.8 m³/h<br>630 l/min | 100 bar<br>1450 psig | 3 | 1470 rpm | 15 kW | 425 kg<br>935 lbs | [citation_4a9d74759a88c05df0c65ed9ce904ead] |
```

Answer SHA-256: `af5eb97be287c8798ad5eed1294b506319af50bd0a461210d8f688aa35ceffb1`

Validation: passed, mode `lossless_deterministic`, repair count 0

Prior best: 12/16; repaired live V4: 16/16

</details>

<details>
<summary><strong>B10 exact live V4 output</strong></summary>

**Prompt:** Compare B-DETECTION PLUS i/s with B-DETECTION PLUS m for a stationary fire-brigade filling station. Explain which is stationary and which is mobile, what they measure, their logging capabilities, and their documented pressure range. One public product page mentions 420 bar while a newer brochure describes options up to 450 bar. Explain the difference using source dates and wording instead of silently selecting one value.

```text
For a stationary fire-brigade filling station, **B-DETECTION PLUS i/s is the stationary system**; **B-DETECTION PLUS m is the mobile system**.

| Aspect | B-DETECTION PLUS i/s | B-DETECTION PLUS m |
| --- | --- | --- |
| Form | stationary continuous online system [citation_279068e0e618b410e96031434fc2c92c] | mobile portable system; case-based configuration; measurement at cylinders; measurement at intake [citation_f93f63c403c7651decbe8e116e69e4e6] |
| Measurements | CO; CO2; O2; optional absolute humidity; optional residual oil/VOC [citation_f0ae1ac419e7111f9b3194a715153def] [citation_f93f63c403c7651decbe8e116e69e4e6] | CO; CO2; O2; optional absolute humidity; optional residual oil/VOC [citation_f0ae1ac419e7111f9b3194a715153def] [citation_f93f63c403c7651decbe8e116e69e4e6] |
| Logging / access | measurement-value logging [citation_0f0b75edb25d78dae1d6635530b2075c] [citation_5a2df238d581a7f58e133008ed18bf3e] [citation_a13d3e3be3da7f814561e7c0a864f174] | integrated data logger; SD-card storage; B-CLOUD remote access; B-APP remote access [citation_0f0b75edb25d78dae1d6635530b2075c] [citation_5a2df238d581a7f58e133008ed18bf3e] [citation_a13d3e3be3da7f814561e7c0a864f174] |
| Documented pressure wording | options up to 450 bar final pressure [citation_71ecc2358f54db9194c75f1220227e3c] [citation_92cc52856fe69750ffe653b28a8a31a7] | maximum documented pressure: 420 bar [citation_71ecc2358f54db9194c75f1220227e3c] [citation_92cc52856fe69750ffe653b28a8a31a7] |

## Why the two documented pressure values differ
The 2025-03 brochure uses the scoped wording “options up to 450 bar final pressure” for the new-generation i/s configuration. The public product evidence gives “maximum documented pressure: 420 bar” for B-DETECTION PLUS m. They differ by product scope and wording, so the i/s option must not be silently applied to the mobile system.
```

Answer SHA-256: `dc2996773c49a3a483bdd51aa2373456001025ae3471cc88bb12ceab5989ce5c`

Validation: passed, mode `grounded_structured`, repair count 0

Prior best: 13/16; repaired live V4: 16/16

</details>

<details>
<summary><strong>B19 exact live V4 output</strong></summary>

**Prompt:** What are the documented maximum operating pressures and maximum flow rates for B-KOOL III? State the separate helium and argon flow range and cite the technical data.

```text
- **Maximum operating pressures:** maximum operating pressure: 350 bar / 550 bar. [citation_e6e0e15049f0fcfc43530211ae6d66d1]
- **Maximum flow rates:**
  - 10 l cylinder filling from 0–200 bar: 200–700 l/min [citation_c70616c2c8bbdbac6171a3fc5fc23288]
  - air according to ISO 1217: 200–650 l/min [citation_c70616c2c8bbdbac6171a3fc5fc23288]
  - helium and argon: 200–420 l/min [citation_c70616c2c8bbdbac6171a3fc5fc23288]
```

Answer SHA-256: `90c20b7cbaaabf1f74ea15f4b5978ee9975d672c969ec4a25ce8fb67584da54b`

Validation: passed, mode `grounded_structured`, repair count 0

Prior best: 16/16; repaired live V4: 16/16

</details>

<details>
<summary><strong>B03 exact live V4 output</strong></summary>

**Prompt:** Compare synthetic projects SYN-BK-N2-420-500 and SYN-BK-N2-365-500. Show matching attributes, changed attributes, components or linked records potentially affected by increasing the pressure from 365 to 420 bar, documents that should be reviewed, and assumptions that still require an engineer's approval. Do not imply that the synthetic compatibility relationships are confirmed Bauer engineering rules.

```text
## SYN-BK-N2-420-500 compared with SYN-BK-N2-365-500
These are **synthetic demo records**, not confirmed Bauer master data or engineering rules.

## Matching stored attributes
- **cluster:** nitrogen booster package [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **application sector:** industrial gas [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **medium:** nitrogen [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **capacity l min:** 500 [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **compressor family:** N2 Booster [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **topology:** booster [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **cooling:** water-cooled [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **control package:** B-CONTROL MICRO synthetic configuration [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **purification package:** synthetic nitrogen-compatible filtration package [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **installation:** indoor skid, service access on three sides [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **environment:** temperate industrial environment, 5-40 C [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **status:** reference [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]

## Changed stored attributes
- **pressure bar:** SYN-BK-N2-420-500: 420 | SYN-BK-N2-365-500: 365 [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **compressor model:** SYN-BK-N2-420-500: SYN-N2B-420 | SYN-BK-N2-365-500: SYN-N2B-365 [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **storage filling package:** SYN-BK-N2-420-500: Synthetic 420-bar storage and filling package | SYN-BK-N2-365-500: Synthetic 365-bar storage and filling package [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]

## Linked records potentially affected
- **shared linked records:** SYN-P-SNS-PRESSURE-500; SYN-P-SNS-TEMP-IND [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **only SYN-BK-N2-420-500:** SYN-P-CLG-N2-420-500; SYN-P-CMP-N2-420-500; SYN-P-CTL-N2-420-500; SYN-P-PUR-N2-420-500; SYN-P-STG-N2-420-500; SYN-P-VLV-N2-420-500 [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **only SYN-BK-N2-365-500:** SYN-P-CLG-N2-365-500; SYN-P-CMP-N2-365-500; SYN-P-CTL-N2-365-500; SYN-P-PUR-N2-365-500; SYN-P-STG-N2-365-500; SYN-P-VLV-N2-365-500 [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]

## Documents recorded for review
- **both projects:** DOC-B-NITROX; DOC-INDUSTRY; DOC-PRODUCT-OVERVIEW [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]

## Assumptions requiring engineer approval
- **stored review assumptions:** CE demo assumption; four-eyes engineering review required [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]
- **authority boundary:** DEMO DATA: project, part, and compatibility records are synthetic and are not confirmed Bauer Kompressoren master data. Linked documents are public evidence sources. [citation_137ee11aa4574d33cfbeb3bb9910ecdb] [citation_94ea144869c6688c62298bfc56ac3b0d]

The linked-record differences identify records to review only. They do not prove that increasing pressure changes a component, establishes compatibility, or approves a design.
```

Answer SHA-256: `07317381ceaeb019dffc42204f2fdabc67de812ec73c415e139fa3b726fd1b72`

Validation: passed, mode `grounded_structured`, repair count 0

Prior best: 11/16; repaired live V4: 16/16

</details>

## Deployment boundary

- Candidate release: `bauer-rag-v4-private-20260731-aq-r2` (`ccb9e8ea-5894-405f-aac4-c4eae5b0d661`)
- API / worker / LibreChat deployments: `4a8ce7b7-968e-480f-a879-09606af9c70e` / `7f9806e1-32b8-4cb2-bfcb-ca9d2e9c8cf1` / `301bef74-9f25-4428-bfc6-46d48123245c`
- Source/artifact/projection counts: 374 / 374 / 25,131
- Active-release pointers: 0
- Production promotion: no
- Owner acceptance: no; this remains a separate owner decision

## Limits

This bounded result does not establish quality for the other 25 public-development cases, a newly sealed holdout, or production. Token presence was diagnostic only; each case had to pass its complete task-specific checklist.
