# Bauer Search Demo Runbook

Date: 2026-07-20

## Purpose and boundary

This demo covers only two Bauer priorities:

1. finding similar previous projects and configured designs;
2. finding documents, parts, and engineering knowledge.

The 12 project records, 75 part records, identifiers, and compatibility relationships are synthetic. They demonstrate the workflow and user experience; they are not Bauer internal master data. The linked documents are public Bauer sources already present in the isolated Bauer knowledge base.

## Before the meeting

1. Open <https://chat.rapiddraft.ai> and start a new chat.
2. Select the private **Bauer Kompressoren** Agent.
3. Confirm that <https://bauer-twin-api-testing.up.railway.app/health> reports `status: ok`, `projects: 12`, `parts: 75`, and `documents: 8`.
4. Run the live 15-query benchmark described in the repository README. It must report 15/15, zero hard-filter violations, and German/English consistency.
5. Use a new LibreChat conversation for the audience demo so earlier context cannot influence results.

## Suggested six-minute demonstration

### 1. Similar previous project

Ask:

> Find the closest previous nitrogen booster project for 420 bar and approximately 500 l/min. Explain why it matches and show incompatible alternatives that were rejected.

Expected top result: `SYN-BK-N2-420-500`. A 365-bar nitrogen project must be listed only as a hard exclusion, never as a compatible result.

### 2. Fine-difference comparison

Ask:

> Compare SYN-BK-N2-420-500 with SYN-BK-N2-365-500 field by field.

Expected behavior: capacity is a match; pressure is a deterministic difference. The answer must state that both records are synthetic.

### 3. Exact identifier lookup

Ask:

> Find part SYN-P-SNS-PRESSURE-500 and show its related demo projects and evidence documents.

Expected behavior: exact part ID is the first and only exact hit, not a semantic approximation.

### 4. Natural-language part search

Ask:

> I do not know the part number. Find a pressure sensor suitable for nitrogen at 420 bar.

Expected top result: `SYN-P-SNS-PRESSURE-500`; lower-rated or wrong-medium parts must be excluded.

### 5. German/English consistency

Ask first in English and then in a new conversation in German:

> Nitrogen booster, 420 bar, 500 l/min.

> Stickstoff Booster, 420 bar, 500 l/min.

Both queries must return the same project family and top ID.

### 6. Evidence-layer document search

Ask:

> Find the BM 40 public product information, then search the attached Bauer files and cite the supporting filename or section.

Expected behavior: the structured tool identifies `DOC-BM-40`; `file_search` supplies page/section-level evidence from the isolated Bauer corpus.

## Safe presentation language

Use: “This is a synthetic vertical slice using public Bauer material to demonstrate the retrieval and comparison workflow. Production would map these fields and rules to Bauer-approved project and part data.”

Do not claim that the demo identifiers, pressure compatibility, configured packages, or project relationships came from Bauer systems.

## Failure handling

- If no structured record exists, the Agent must say it was not found; do not invent an ID.
- If a project is below the requested pressure or has the wrong medium/family/topology, it must remain excluded even if its text looks similar.
- If a public document supports only general product information, present it as evidence for that information—not as proof of a synthetic compatibility relationship.
- If the structured service is unavailable, continue only with public document search and state that project/part comparison is temporarily unavailable.
