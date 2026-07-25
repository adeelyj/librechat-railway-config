from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import PurePath

from .ids import stable_id
from .ingest.canonical import Block, Cell, Document, Table
from .models import BoundingBox, EvidenceItem, SourceCoordinate, TypedFact
from .planner import normalize_text
from .retrieval import NavigationNode, SearchUnit


_NUMBER_UNIT_RE = re.compile(
    r"^\s*(?P<number>[+-]?\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>bar(?:g)?|psi(?:g)?|mpa|kpa|pa|l/min|m[³3]/h|cfm|kw|w|"
    r"rpm|min-?1|ppm|%|°c|kg|mm|cm|m)?\s*$",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(
    r"\b(?:N\d{4,7}(?:[_-]\d+)?|(?:BM|K|GIB|GI|PE|I)\s+"
    r"[A-Z0-9][A-Z0-9./_-]*|(?=[A-Z0-9./_-]{3,40}\b)"
    r"(?=[A-Z0-9./_-]*[A-Z])(?=[A-Z0-9./_-]*\d)"
    r"[A-Z0-9][A-Z0-9./_-]*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExactTerm:
    term_id: str
    evidence_id: str
    term_type: str
    raw_term: str
    normalized_term: str


@dataclass(frozen=True, slots=True)
class NavigationDescriptionLink:
    node_id: str
    search_unit_id: str


@dataclass(frozen=True, slots=True)
class ProjectionBundle:
    evidence: tuple[EvidenceItem, ...]
    search_units: tuple[SearchUnit, ...]
    facts: tuple[TypedFact, ...]
    exact_terms: tuple[ExactTerm, ...]
    navigation_nodes: tuple[NavigationNode, ...]
    navigation_descriptions: tuple[NavigationDescriptionLink, ...] = ()


def project_document(
    document: Document,
    *,
    release_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    source_document_id: str,
    source_version_id: str,
    source_type: str,
    external_file_id: str | None = None,
    category_path: tuple[str, ...] = (),
    source_metadata: Mapping[str, object] | None = None,
) -> ProjectionBundle:
    evidence: list[EvidenceItem] = []
    units: list[SearchUnit] = []
    facts: list[TypedFact] = []
    exact_terms: list[ExactTerm] = []
    source_terms = _source_native_terms(
        document,
        external_file_id=external_file_id,
        source_metadata=source_metadata,
    )
    title = document.title or next(
        (
            term.raw_term
            for term in source_terms
            if term.term_type == "title"
        ),
        document.source_name,
    )
    common_metadata = {
        "canonical_document_id": document.document_id,
        "parser_id": document.parser_id,
        "parser_version": document.parser_version,
    }
    if external_file_id:
        common_metadata["external_file_id"] = external_file_id

    for page in document.pages:
        for block in page.blocks:
            if not block.text.strip() or block.kind in {"header", "footer"}:
                continue
            item = _block_evidence(
                block,
                page_number=page.index + 1,
                printed_page_label=page.printed_label,
                release_id=release_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                source_document_id=source_document_id,
                source_version_id=source_version_id,
                source_sha256=document.source_sha256,
                source_type=source_type,
                title=title,
                metadata=common_metadata,
            )
            terms = _identifiers(block.text)
            evidence.append(item)
            units.append(
                SearchUnit(
                    search_unit_id=stable_id(
                        "search_unit",
                        release_id,
                        source_version_id,
                        block.block_id,
                    ),
                    evidence=item,
                    unit_type="block",
                    search_text=block.text,
                    exact_terms=terms,
                    navigation_path=category_path,
                )
            )
            exact_terms.extend(
                _exact_term_rows(
                    item.evidence_id,
                    (("identifier", term) for term in terms),
                )
            )

        for table in page.tables:
            rows = _table_rows(table)
            headers = _table_headers(table, rows)
            for row_index, row_cells in rows:
                if not row_cells or all(cell.role in {"header", "group_header"} for cell in row_cells):
                    continue
                values = tuple(cell.text.strip() for cell in row_cells)
                if not any(values):
                    continue
                group = next((cell.group for cell in row_cells if cell.group), None)
                row_text_parts = []
                if group:
                    row_text_parts.append(f"Group: {group}")
                row_text_parts.extend(
                    f"{headers[index] if index < len(headers) else f'Column {index + 1}'}: {value}"
                    for index, value in enumerate(values)
                    if value
                )
                row_text = "; ".join(row_text_parts)
                item = EvidenceItem(
                    evidence_id=stable_id(
                        "evidence",
                        source_version_id,
                        table.table_id,
                        row_index,
                    ),
                    release_id=release_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    source_document_id=source_document_id,
                    source_version_id=source_version_id,
                    source_sha256=document.source_sha256,
                    source_type=source_type,
                    title=title,
                    content=row_text,
                    coordinate=SourceCoordinate(
                        page_number=page.index + 1,
                        printed_page_label=page.printed_label,
                        bounding_box=_bbox(table.bbox),
                        table_id=table.table_id,
                        row_index=row_index,
                    ),
                    language=document.language,
                    table_headers=headers,
                    table_values=values,
                    footnotes=tuple(
                        cell.text.strip()
                        for cell in table.cells
                        if cell.role == "note" and cell.text.strip()
                    ),
                    metadata={
                        **common_metadata,
                        "group": group or "",
                        "canonical_table_id": table.table_id,
                    },
                )
                terms = _identifiers(" ".join(values))
                evidence.append(item)
                units.append(
                    SearchUnit(
                        search_unit_id=stable_id(
                            "search_unit",
                            release_id,
                            source_version_id,
                            table.table_id,
                            row_index,
                        ),
                        evidence=item,
                        unit_type="table_row",
                        search_text=row_text,
                        exact_terms=terms,
                        navigation_path=category_path,
                    )
                )
                exact_terms.extend(
                    _exact_term_rows(
                        item.evidence_id,
                        (("identifier", term) for term in terms),
                    )
                )
                row_facts = _facts_from_row(
                    item,
                    artifact_set_id=stable_id(
                        "artifact_set",
                        source_version_id,
                        document.parser_id,
                        document.parser_version,
                    ),
                    headers=headers,
                    cells=row_cells,
                    group=group,
                )
                facts.extend(row_facts)
                for fact in row_facts:
                    units.append(
                        SearchUnit(
                            search_unit_id=stable_id(
                                "search_unit",
                                release_id,
                                fact.fact_id,
                            ),
                            evidence=item,
                            unit_type="fact",
                            search_text=(
                                f"{fact.subject} {fact.predicate} {fact.raw_value} "
                                f"{fact.raw_unit or ''}"
                            ),
                            exact_terms=terms,
                            subject=fact.subject,
                            predicate=fact.predicate,
                            numeric_value=(
                                Decimal(fact.numeric_value)
                                if fact.numeric_value is not None
                                else None
                            ),
                            unit=fact.normalized_unit,
                            navigation_path=category_path,
                        )
                    )

    representative_index = next(
        (
            index
            for index, unit in enumerate(units)
            if unit.evidence.is_citable
            and not unit.evidence.generated_summary
            and unit.unit_type != "fact"
        ),
        None,
    )
    if representative_index is not None and source_terms:
        representative = units[representative_index]
        units[representative_index] = replace(
            representative,
            exact_terms=_merge_exact_values(
                representative.exact_terms,
                tuple(term.raw_term for term in source_terms),
            ),
        )
        exact_terms.extend(
            _exact_term_rows(
                representative.evidence.evidence_id,
                (
                    (term.term_type, term.raw_term)
                    for term in source_terms
                ),
            )
        )

    navigation: list[NavigationNode] = []
    description_links: list[NavigationDescriptionLink] = []
    if category_path:
        for index, label in enumerate(category_path):
            path = category_path[: index + 1]
            node = NavigationNode(
                node_id=stable_id(
                    "navigation",
                    release_id,
                    source_document_id,
                    "category",
                    *path,
                ),
                release_id=release_id,
                label=label,
                description=(
                    f"Category path: {' / '.join(path)}. "
                    f"Contains artifact: {title}."
                ),
                aliases=(),
                linked_evidence_ids=tuple(
                    item.evidence_id
                    for item in evidence
                    if item.is_citable and not item.generated_summary
                ),
            )
            navigation.append(node)
            summary_evidence, summary_unit = _navigation_summary(
                node,
                node_kind="category",
                document=document,
                release_id=release_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                source_document_id=source_document_id,
                source_version_id=source_version_id,
                source_type=source_type,
                external_file_id=external_file_id,
                title=title,
                navigation_path=path,
            )
            evidence.append(summary_evidence)
            units.append(summary_unit)
            description_links.append(
                NavigationDescriptionLink(
                    node_id=node.node_id,
                    search_unit_id=summary_unit.search_unit_id,
                )
            )

    artifact_aliases = _artifact_aliases(source_terms, title)
    artifact_node = NavigationNode(
        node_id=stable_id(
            "navigation",
            release_id,
            source_document_id,
            "artifact",
        ),
        release_id=release_id,
        label=title,
        description=_artifact_description(
            title=title,
            category_path=category_path,
            source_terms=source_terms,
        ),
        aliases=artifact_aliases,
        linked_evidence_ids=tuple(
            item.evidence_id
            for item in evidence
            if item.is_citable and not item.generated_summary
        ),
    )
    navigation.append(artifact_node)
    summary_evidence, summary_unit = _navigation_summary(
        artifact_node,
        node_kind="artifact",
        document=document,
        release_id=release_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_document_id=source_document_id,
        source_version_id=source_version_id,
        source_type=source_type,
        external_file_id=external_file_id,
        title=title,
        navigation_path=category_path,
    )
    evidence.append(summary_evidence)
    units.append(summary_unit)
    description_links.append(
        NavigationDescriptionLink(
            node_id=artifact_node.node_id,
            search_unit_id=summary_unit.search_unit_id,
        )
    )
    return ProjectionBundle(
        evidence=tuple(evidence),
        search_units=tuple(units),
        facts=tuple(facts),
        exact_terms=tuple(_deduplicate_terms(exact_terms)),
        navigation_nodes=tuple(navigation),
        navigation_descriptions=tuple(description_links),
    )


@dataclass(frozen=True, slots=True)
class _SourceTerm:
    term_type: str
    raw_term: str
    normalized_term: str


_SOURCE_METADATA_FIELDS = {
    "title": ("title", "document_title"),
    "filename": ("filename", "file_name"),
    "document_number": (
        "document_number",
        "document_no",
        "document_num",
        "document_code",
    ),
    "product_tag": (
        "product_tag",
        "product_tags",
        "product_code",
        "product_codes",
    ),
    "alias": ("alias", "aliases"),
}
_MAX_METADATA_TERMS_PER_TYPE = 64
_MAX_METADATA_TERM_LENGTH = 512


def _source_native_terms(
    document: Document,
    *,
    external_file_id: str | None,
    source_metadata: Mapping[str, object] | None,
) -> tuple[_SourceTerm, ...]:
    values: list[tuple[str, str]] = []
    if external_file_id and external_file_id.strip():
        values.append(("external_file_id", external_file_id))

    filename = _source_basename(document.source_name)
    if filename:
        values.append(("filename", filename))
        stem = filename.rsplit(".", 1)[0].strip() if "." in filename else filename
        if stem and stem != filename:
            values.append(("filename", stem))

    if document.title and document.title.strip():
        values.append(("title", document.title))

    normalized_metadata = {
        _metadata_key(key): value
        for key, value in (source_metadata or {}).items()
    }
    for term_type, aliases in _SOURCE_METADATA_FIELDS.items():
        for alias in aliases:
            for value in _metadata_values(normalized_metadata.get(alias)):
                values.append((term_type, value))

    by_key: dict[tuple[str, str], _SourceTerm] = {}
    counts: dict[str, int] = {}
    for term_type, raw_value in values:
        raw_term = " ".join(str(raw_value).split())
        if not raw_term or len(raw_term) > _MAX_METADATA_TERM_LENGTH:
            continue
        normalized_term = normalize_text(raw_term)
        if not normalized_term:
            continue
        key = (term_type, normalized_term)
        if key in by_key:
            continue
        if counts.get(term_type, 0) >= _MAX_METADATA_TERMS_PER_TYPE:
            continue
        by_key[key] = _SourceTerm(
            term_type=term_type,
            raw_term=raw_term,
            normalized_term=normalized_term,
        )
        counts[term_type] = counts.get(term_type, 0) + 1
    return tuple(
        by_key[key]
        for key in sorted(by_key, key=lambda item: (item[0], item[1]))
    )


def _metadata_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")


def _metadata_values(value: object) -> tuple[str, ...]:
    if value is None or isinstance(value, (bool, Mapping)):
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray)
    ):
        return tuple(
            str(item)
            for item in value
            if item is not None
            and not isinstance(item, (bool, Mapping, list, tuple, set))
        )
    if isinstance(value, (int, float)):
        return (str(value),)
    return ()


def _source_basename(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    return PurePath(normalized).name.strip()


def _merge_exact_values(
    existing: tuple[str, ...],
    additions: tuple[str, ...],
) -> tuple[str, ...]:
    values: dict[str, str] = {}
    for value in (*existing, *additions):
        normalized = normalize_text(value)
        if normalized and normalized not in values:
            values[normalized] = value
    return tuple(values[key] for key in sorted(values))


def _artifact_aliases(
    source_terms: tuple[_SourceTerm, ...],
    title: str,
) -> tuple[str, ...]:
    allowed = {"alias", "document_number", "product_tag", "filename"}
    values = {
        term.normalized_term: term.raw_term
        for term in source_terms
        if term.term_type in allowed
        and term.normalized_term != normalize_text(title)
    }
    return tuple(values[key] for key in sorted(values))


def _artifact_description(
    *,
    title: str,
    category_path: tuple[str, ...],
    source_terms: tuple[_SourceTerm, ...],
) -> str:
    parts = [f"Artifact: {title}."]
    if category_path:
        parts.append(f"Category path: {' / '.join(category_path)}.")
    labels = (
        ("filename", "Source filename"),
        ("document_number", "Document number"),
        ("product_tag", "Product tags"),
        ("alias", "Aliases"),
    )
    for term_type, label in labels:
        values = tuple(
            term.raw_term for term in source_terms if term.term_type == term_type
        )
        if values:
            parts.append(f"{label}: {', '.join(values)}.")
    return " ".join(parts)


def _navigation_summary(
    node: NavigationNode,
    *,
    node_kind: str,
    document: Document,
    release_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    source_document_id: str,
    source_version_id: str,
    source_type: str,
    external_file_id: str | None,
    title: str,
    navigation_path: tuple[str, ...],
) -> tuple[EvidenceItem, SearchUnit]:
    evidence_id = stable_id(
        "evidence",
        release_id,
        source_version_id,
        node.node_id,
        "generated_navigation_description",
    )
    metadata = {
        "canonical_document_id": document.document_id,
        "navigation_node_id": node.node_id,
        "navigation_node_kind": node_kind,
    }
    if external_file_id:
        metadata["external_file_id"] = external_file_id
    summary_evidence = EvidenceItem(
        evidence_id=evidence_id,
        release_id=release_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_document_id=source_document_id,
        source_version_id=source_version_id,
        source_sha256=document.source_sha256,
        source_type=source_type,
        title=title,
        content=node.description,
        coordinate=SourceCoordinate(page_number=1),
        language=document.language,
        is_citable=False,
        generated_summary=True,
        metadata=metadata,
    )
    search_unit = SearchUnit(
        search_unit_id=stable_id(
            "search_unit",
            release_id,
            source_document_id,
            node.node_id,
            "generated_navigation_description",
        ),
        evidence=summary_evidence,
        unit_type="navigation_summary",
        search_text=" ".join(
            value for value in (node.label, node.description, *node.aliases) if value
        ),
        navigation_path=navigation_path,
    )
    return summary_evidence, search_unit


def _block_evidence(
    block: Block,
    *,
    page_number: int,
    printed_page_label: str | None,
    release_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    source_document_id: str,
    source_version_id: str,
    source_sha256: str,
    source_type: str,
    title: str,
    metadata: dict[str, str],
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=stable_id("evidence", source_version_id, block.block_id),
        release_id=release_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_document_id=source_document_id,
        source_version_id=source_version_id,
        source_sha256=source_sha256,
        source_type=source_type,
        title=title,
        content=block.text.strip(),
        coordinate=SourceCoordinate(
            page_number=page_number,
            printed_page_label=printed_page_label,
            bounding_box=_bbox(block.bbox),
            block_id=block.block_id,
        ),
        metadata={**metadata, "canonical_block_id": block.block_id},
    )


def _table_rows(table: Table) -> list[tuple[int, list[Cell]]]:
    rows: dict[int, list[Cell]] = {}
    for cell in sorted(table.cells, key=lambda item: (item.row, item.column)):
        rows.setdefault(cell.row, []).append(cell)
    return sorted(rows.items())


def _table_headers(table: Table, rows: list[tuple[int, list[Cell]]]) -> tuple[str, ...]:
    for _, cells in rows:
        if cells and all(cell.role in {"header", "row_header"} for cell in cells):
            headers = [""] * table.column_count
            for cell in cells:
                for column in range(cell.column, min(table.column_count, cell.column + cell.column_span)):
                    headers[column] = cell.text.strip()
            return tuple(
                value or f"Column {index + 1}" for index, value in enumerate(headers)
            )
    return tuple(f"Column {index + 1}" for index in range(table.column_count))


def _facts_from_row(
    evidence: EvidenceItem,
    *,
    artifact_set_id: str,
    headers: tuple[str, ...],
    cells: list[Cell],
    group: str | None,
) -> list[TypedFact]:
    subject = next(
        (
            cell.text.strip()
            for cell in cells
            if cell.text.strip() and cell.role in {"row_header", "body"}
        ),
        group or evidence.title,
    )
    if group:
        subject = f"{group} / {subject}"
    facts: list[TypedFact] = []
    for cell in cells:
        if not cell.text.strip() or cell.role in {"header", "group_header", "note"}:
            continue
        match = _NUMBER_UNIT_RE.fullmatch(cell.text.strip())
        if not match:
            continue
        raw_number = match.group("number").replace(",", ".")
        try:
            numeric = Decimal(raw_number)
        except InvalidOperation:
            continue
        header = headers[cell.column] if cell.column < len(headers) else f"column_{cell.column + 1}"
        predicate = normalize_text(header).replace(" ", "_") or f"column_{cell.column + 1}"
        raw_unit = match.group("unit")
        normalized_unit = normalize_text(raw_unit or "") or None
        fact_id = stable_id(
            "fact",
            artifact_set_id,
            evidence.evidence_id,
            predicate,
            str(numeric),
            normalized_unit or "",
        )
        facts.append(
            TypedFact(
                fact_id=fact_id,
                artifact_set_id=artifact_set_id,
                subject=subject,
                predicate=predicate,
                value_kind="numeric",
                raw_value=cell.text.strip(),
                numeric_value=str(numeric),
                raw_unit=raw_unit,
                normalized_unit=normalized_unit,
                provenance_evidence_ids=(evidence.evidence_id,),
                qualifiers={"group": group} if group else {},
                confidence=cell.confidence if cell.confidence is not None else 1.0,
                review_status="candidate",
            )
        )
    return facts


def _identifiers(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(match.group(0).strip().upper() for match in _IDENTIFIER_RE.finditer(text))
    )


def _exact_term_rows(
    evidence_id: str,
    terms: Iterable[tuple[str, str]],
) -> list[ExactTerm]:
    rows: list[ExactTerm] = []
    for term_type, raw_term in terms:
        normalized_term = normalize_text(raw_term)
        if not normalized_term:
            continue
        term_id = (
            stable_id("exact_term", evidence_id, raw_term)
            if term_type == "identifier"
            else stable_id(
                "exact_term",
                evidence_id,
                term_type,
                normalized_term,
            )
        )
        rows.append(
            ExactTerm(
                term_id=term_id,
                evidence_id=evidence_id,
                term_type=term_type,
                raw_term=raw_term,
                normalized_term=normalized_term,
            )
        )
    return rows


def _deduplicate_terms(terms: list[ExactTerm]) -> list[ExactTerm]:
    by_key: dict[tuple[str, str, str], ExactTerm] = {}
    for term in terms:
        by_key[
            (term.evidence_id, term.term_type, term.normalized_term)
        ] = term
    return [by_key[key] for key in sorted(by_key)]


def _bbox(value: tuple[float, float, float, float] | None) -> BoundingBox | None:
    return BoundingBox(*value) if value is not None else None
