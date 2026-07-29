from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Iterable
from urllib.parse import urlparse

from ..canonical.models import (
    CanonicalBlock,
    CanonicalCell,
    CanonicalDocument,
    CanonicalFact,
    CanonicalRecord,
    CanonicalTable,
    Footnote,
    Provenance,
)
from ..canonical.normalize import (
    clean_text,
    document_number_from_text,
    header_label_and_markers,
    inherited_header_paths,
    language_from_filename,
    normalize_unit,
    numeric_value,
    predicate_slug,
    sorted_attributes,
    stable_id,
)


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_EXCLUDED_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "nav",
    "form",
    "aside",
    "footer",
}
_EVIDENCE_DESCENDANTS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "table",
}
_FOOTNOTE_RE = re.compile(r"^\s*([¹²³⁰-⁹])\s*(.+)$", re.DOTALL)
_FILE_NUMBER_RE = re.compile(r"/file/(\d+)/?$")


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: tuple[tuple[str, str], ...] = ()
    children: list["_Node | str"] = field(default_factory=list)
    line: int = 1
    column: int = 0
    parent: "_Node | None" = None

    def attr(self, name: str) -> str | None:
        wanted = name.casefold()
        return next((value for key, value in self.attrs if key == wanted), None)

    def classes(self) -> frozenset[str]:
        return frozenset((self.attr("class") or "").split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self._stack = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        line, column = self.getpos()
        node = _Node(
            tag=tag.casefold(),
            attrs=tuple(
                (key.casefold(), "" if value is None else value)
                for key, value in attrs
            ),
            line=line,
            column=column,
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.casefold():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == wanted:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _iter_nodes(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _iter_nodes(child)


def _first_node(node: _Node, tag: str) -> _Node | None:
    return next((item for item in _iter_nodes(node) if item.tag == tag), None)


def _has_descendant(node: _Node, tags: set[str]) -> bool:
    return any(item.tag in tags for item in _iter_nodes(node))


def _visible_text(node: _Node, *, skip_nested: set[str] | None = None) -> str:
    parts: list[str] = []

    def collect(current: _Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if child.tag in _EXCLUDED_TAGS:
                continue
            if skip_nested and child is not node and child.tag in skip_nested:
                continue
            if child.tag == "br":
                parts.append("\n")
                continue
            collect(child)
            if child.tag in {"p", "div", "li", "tr", "section"}:
                parts.append(" ")

    collect(node)
    return clean_text("".join(parts))


def _positive_span(value: str | None) -> int:
    try:
        return min(max(int(value or "1"), 1), 10_000)
    except ValueError:
        return 1


def _node_locator(node: _Node, kind: str) -> str:
    element_id = node.attr("id")
    suffix = f"/id:{element_id}" if element_id else ""
    return (
        f"page:0/html:{node.tag}/line:{node.line}"
        f"/column:{node.column}/{kind}{suffix}"
    )


def _source_provenance(
    *,
    source_sha256: str,
    source_path: str,
    parser_id: str,
    parser_version: str,
    locator: str,
) -> Provenance:
    return Provenance(
        source_sha256=source_sha256,
        source_path=source_path,
        parser_id=parser_id,
        parser_version=parser_version,
        locator=locator,
        physical_page=1,
        printed_page="1",
    )


@dataclass(frozen=True, slots=True)
class HtmlDomParser:
    parser_id: str = "html_dom_grid"
    parser_version: str = "1"

    def parse(
        self,
        payload: bytes,
        *,
        source_path: str,
        source_sha256: str | None = None,
    ) -> CanonicalDocument:
        source_sha256 = source_sha256 or hashlib.sha256(payload).hexdigest()
        source = payload.decode("utf-8-sig")
        # The source pages use semicolon-terminated superscript entities. Keep
        # this explicit repair so broken upstream entity handling cannot turn a
        # footnote marker into an unrelated Unicode letter.
        source = (
            source.replace("&sup1;", "¹")
            .replace("&sup2;", "²")
            .replace("&sup3;", "³")
        )
        builder = _TreeBuilder()
        builder.feed(source)
        builder.close()

        content_root = (
            _first_node(builder.root, "main")
            or _first_node(builder.root, "article")
            or _first_node(builder.root, "body")
            or builder.root
        )
        title_node = _first_node(builder.root, "title")
        html_node = _first_node(builder.root, "html")
        title = _visible_text(title_node) if title_node else None
        language = (html_node.attr("lang") if html_node else None) or (
            language_from_filename(PurePosixPath(source_path).name)
        )

        blocks: list[CanonicalBlock] = []
        tables: list[CanonicalTable] = []
        records = self._records(
            builder.root,
            source_sha256=source_sha256,
            source_path=source_path,
        )
        headings: dict[int, str] = {}

        def section_path() -> tuple[str, ...]:
            return tuple(headings[level] for level in sorted(headings))

        def add_block(node: _Node, kind: str, text: str) -> None:
            locator = _node_locator(node, kind)
            blocks.append(
                CanonicalBlock(
                    block_id=stable_id("block", source_sha256, locator, text),
                    kind=kind,  # type: ignore[arg-type]
                    text=text,
                    section_path=section_path(),
                    provenance=_source_provenance(
                        source_sha256=source_sha256,
                        source_path=source_path,
                        parser_id=self.parser_id,
                        parser_version=self.parser_version,
                        locator=locator,
                    ),
                )
            )

        def visit(node: _Node) -> None:
            if node.tag in _EXCLUDED_TAGS:
                return
            if node.tag == "table":
                table, facts = self._table(
                    node,
                    source_sha256=source_sha256,
                    source_path=source_path,
                    section_path=section_path(),
                )
                if table is not None:
                    tables.append(table)
                    all_facts.extend(facts)
                return
            if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                text = _visible_text(node)
                if text:
                    level = int(node.tag[1])
                    for key in tuple(headings):
                        if key >= level:
                            del headings[key]
                    headings[level] = text
                    add_block(node, "heading", text)
                return
            if node.tag == "p":
                text = _visible_text(node)
                if text:
                    add_block(node, "paragraph", text)
                return
            if node.tag == "li":
                text = _visible_text(node, skip_nested={"ul", "ol"})
                if text:
                    add_block(node, "list_item", text)
                for child in node.children:
                    if isinstance(child, _Node) and child.tag in {"ul", "ol"}:
                        visit(child)
                return
            if (
                node.tag in {"div", "section", "dd", "dt"}
                and not _has_descendant(node, _EVIDENCE_DESCENDANTS)
                and "am-fileline" not in node.classes()
            ):
                text = _visible_text(node)
                if text and len(text) <= 4000:
                    add_block(node, "paragraph", text)
                return
            for child in node.children:
                if isinstance(child, _Node):
                    visit(child)

        all_facts: list[CanonicalFact] = []
        visit(content_root)

        footnotes = self._footnotes(blocks)
        if footnotes:
            tables = [
                dataclasses.replace(
                    table,
                    footnotes=tuple(
                        footnote
                        for footnote, footnote_section in footnotes
                        if self._same_table_section(
                            table.section_path,
                            footnote_section,
                        )
                    ),
                )
                for table in tables
            ]

        filename = PurePosixPath(source_path).name
        locator = f"source:{source_path}"
        return CanonicalDocument(
            document_id=stable_id(
                "document",
                source_sha256,
                locator,
                self.parser_id,
                self.parser_version,
            ),
            source_sha256=source_sha256,
            source_path=source_path,
            media_type="text/html",
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            title=title,
            language=language,
            document_number=document_number_from_text(filename, title),
            subject=None,
            source_filename=filename,
            page_count=1,
            blocks=tuple(blocks),
            tables=tuple(tables),
            records=tuple(records),
            facts=tuple(all_facts),
            attributes=sorted_attributes(
                {
                    "block_count": len(blocks),
                    "table_count": len(tables),
                    "record_count": len(records),
                }
            ),
        )

    def _table(
        self,
        node: _Node,
        *,
        source_sha256: str,
        source_path: str,
        section_path: tuple[str, ...],
    ) -> tuple[CanonicalTable | None, list[CanonicalFact]]:
        row_nodes = self._table_rows(node)
        placed: list[tuple[_Node, int, int, int, int, str]] = []
        occupied: set[tuple[int, int]] = set()
        row_count = len(row_nodes)
        column_count = 0
        for row_index, row_node in enumerate(row_nodes):
            column = 0
            cells = [
                child
                for child in row_node.children
                if isinstance(child, _Node) and child.tag in {"td", "th"}
            ]
            for cell_node in cells:
                while (row_index, column) in occupied:
                    column += 1
                row_span = _positive_span(cell_node.attr("rowspan"))
                column_span = _positive_span(cell_node.attr("colspan"))
                text = _visible_text(cell_node)
                placed.append(
                    (
                        cell_node,
                        row_index,
                        column,
                        row_span,
                        column_span,
                        text,
                    )
                )
                for occupied_row in range(row_index, row_index + row_span):
                    for occupied_column in range(
                        column, column + column_span
                    ):
                        occupied.add((occupied_row, occupied_column))
                column += column_span
                column_count = max(column_count, column)
                row_count = max(row_count, row_index + row_span)
        if not placed or row_count < 2 or column_count < 2:
            return None, []

        header_rows: set[int] = set()
        for row_index in range(row_count):
            row = [item for item in placed if item[1] == row_index]
            if row and all(item[0].tag == "th" for item in row):
                is_group = (
                    len(row) == 1
                    and row[0][2] == 0
                    and row[0][4] >= column_count
                )
                if not is_group:
                    header_rows.add(row_index)
                continue
            break
        header_paths = inherited_header_paths(
            column_count=column_count,
            header_cells=(
                (row, column, column_span, text)
                for _, row, column, _, column_span, text in placed
                if row in header_rows
            ),
        )

        table_locator = _node_locator(node, "table")
        table_id = stable_id(
            "table",
            source_sha256,
            table_locator,
            *(
                f"{row}:{column}:{row_span}:{column_span}:{text}"
                for _, row, column, row_span, column_span, text in placed
            ),
        )
        origins_per_row: dict[int, int] = {}
        for _, row, *_ in placed:
            origins_per_row[row] = origins_per_row.get(row, 0) + 1
        cells: list[CanonicalCell] = []
        facts: list[CanonicalFact] = []
        current_group: str | None = None
        subject_by_row: dict[int, str] = {}

        for cell_node, row, column, row_span, column_span, text in placed:
            full_width_header = (
                cell_node.tag == "th"
                and origins_per_row[row] == 1
                and column == 0
                and column_span >= column_count
            )
            if full_width_header:
                role = "group_header"
                current_group = text or None
                group = None
            elif row in header_rows:
                role = "header"
                group = None
            elif column == 0:
                role = "row_header"
                group = current_group
                if text:
                    subject_by_row[row] = text
            else:
                role = "body"
                group = current_group

            path = header_paths[column] if column < len(header_paths) else ()
            unit_raw = next(
                (
                    label
                    for label in reversed(path)
                    if normalize_unit(label)[1] is not None
                ),
                None,
            )
            unit_raw, unit_ucum = normalize_unit(unit_raw)
            header_markers: tuple[str, ...] = ()
            cleaned_path: list[str] = []
            for label in path:
                clean_label, markers = header_label_and_markers(label)
                if clean_label and normalize_unit(clean_label)[1] is None:
                    cleaned_path.append(clean_label)
                header_markers += markers
            kind, number = numeric_value(text) if role == "body" else ("text", None)
            if not text:
                kind = "empty"
            locator = (
                f"{table_locator}/row:{row}/column:{column}"
                f"/line:{cell_node.line}/column:{cell_node.column}"
            )
            cell = CanonicalCell(
                cell_id=stable_id("cell", source_sha256, locator, text),
                row=row,
                column=column,
                row_span=row_span,
                column_span=column_span,
                role=role,  # type: ignore[arg-type]
                text=text,
                value_kind=kind,  # type: ignore[arg-type]
                numeric_value=number,
                header_path=tuple(cleaned_path),
                unit_raw=unit_raw,
                unit_ucum=unit_ucum,
                qualifier_markers=tuple(dict.fromkeys(header_markers)),
                group=group,
                provenance=_source_provenance(
                    source_sha256=source_sha256,
                    source_path=source_path,
                    parser_id=self.parser_id,
                    parser_version=self.parser_version,
                    locator=locator,
                ),
            )
            cells.append(cell)

        for cell in cells:
            if cell.role != "body" or cell.numeric_value is None:
                continue
            subject = subject_by_row.get(cell.row) or cell.group or "table row"
            predicate = predicate_slug(
                next(
                    (
                        label
                        for label in cell.header_path
                        if normalize_unit(label)[1] is None
                    ),
                    f"column_{cell.column + 1}",
                )
            )
            fact_id = stable_id(
                "fact",
                table_id,
                subject,
                predicate,
                cell.text,
                cell.unit_ucum or "",
                cell.group or "",
            )
            facts.append(
                CanonicalFact(
                    fact_id=fact_id,
                    subject=subject,
                    predicate=predicate,
                    value_kind=cell.value_kind,  # type: ignore[arg-type]
                    raw_value=cell.text,
                    numeric_value=cell.numeric_value,
                    minimum_value=None,
                    maximum_value=None,
                    unit_raw=cell.unit_raw,
                    unit_ucum=cell.unit_ucum,
                    qualifiers=tuple(
                        (key, value)
                        for key, value in (
                            ("group", cell.group or ""),
                            (
                                "footnote_markers",
                                ",".join(cell.qualifier_markers),
                            ),
                        )
                        if value
                    ),
                    provenance_ids=(cell.cell_id,),
                    confidence=1.0,
                    review_status="candidate",
                )
            )

        caption_node = next(
            (
                child
                for child in node.children
                if isinstance(child, _Node) and child.tag == "caption"
            ),
            None,
        )
        caption = _visible_text(caption_node) if caption_node else (
            section_path[-1] if section_path else None
        )
        return (
            CanonicalTable(
                table_id=table_id,
                caption=caption,
                section_path=section_path,
                row_count=row_count,
                column_count=column_count,
                cells=tuple(cells),
                footnotes=(),
                provenance=_source_provenance(
                    source_sha256=source_sha256,
                    source_path=source_path,
                    parser_id=self.parser_id,
                    parser_version=self.parser_version,
                    locator=table_locator,
                ),
            ),
            facts,
        )

    def _records(
        self,
        root: _Node,
        *,
        source_sha256: str,
        source_path: str,
    ) -> list[CanonicalRecord]:
        records: list[CanonicalRecord] = []
        for node in _iter_nodes(root):
            if "am-fileline" not in node.classes():
                continue
            anchor = next(
                (
                    candidate
                    for candidate in _iter_nodes(node)
                    if candidate.tag == "a"
                    and "am-filelink" in candidate.classes()
                ),
                None,
            )
            if anchor is None:
                continue
            title = _visible_text(anchor)
            href = anchor.attr("href") or ""
            size_node = next(
                (
                    candidate
                    for candidate in _iter_nodes(node)
                    if "am-filesize" in candidate.classes()
                ),
                None,
            )
            size = (_visible_text(size_node) if size_node else "").strip("()")
            match = _FILE_NUMBER_RE.search(urlparse(href).path)
            file_number = match.group(1) if match else ""
            fields = {
                "title": title.removesuffix(" EN").removesuffix(" DE"),
                "language": (
                    "en"
                    if title.endswith(" EN")
                    else "de"
                    if title.endswith(" DE")
                    else ""
                ),
                "file_number": file_number,
                "file_size": size,
                "download_path": urlparse(href).path,
            }
            certificate_match = re.match(
                r"^(?:EN ISO 3834-2 Certificate|EN ISO 3834-2 Zertifikat):\s*"
                r"(?P<organization>.+? GmbH),\s*(?P<address>.+)$",
                fields["title"],
                re.IGNORECASE,
            )
            if certificate_match:
                fields["organization"] = certificate_match.group(
                    "organization"
                )
                fields["address"] = certificate_match.group("address")
            locator = _node_locator(node, "record")
            records.append(
                CanonicalRecord(
                    record_id=stable_id(
                        "record",
                        source_sha256,
                        locator,
                        *(f"{key}:{value}" for key, value in sorted(fields.items())),
                    ),
                    record_type="download",
                    fields=tuple(
                        (key, value)
                        for key, value in sorted(fields.items())
                        if value
                    ),
                    section_path=("Certificate",),
                    provenance=_source_provenance(
                        source_sha256=source_sha256,
                        source_path=source_path,
                        parser_id=self.parser_id,
                        parser_version=self.parser_version,
                        locator=locator,
                    ),
                )
            )
        return records

    @staticmethod
    def _table_rows(table: _Node) -> list[_Node]:
        rows: list[_Node] = []

        def visit(node: _Node) -> None:
            for child in node.children:
                if not isinstance(child, _Node):
                    continue
                if child.tag == "table":
                    continue
                if child.tag == "tr":
                    rows.append(child)
                else:
                    visit(child)

        visit(table)
        return rows

    @staticmethod
    def _footnotes(
        blocks: list[CanonicalBlock],
    ) -> list[tuple[Footnote, tuple[str, ...]]]:
        result: list[tuple[Footnote, tuple[str, ...]]] = []
        for block in blocks:
            if block.kind != "paragraph":
                continue
            # Some source pages place both footnotes in one paragraph.
            parts = re.split(r"(?=[¹²³⁰-⁹]\s)", block.text)
            for part in parts:
                match = _FOOTNOTE_RE.match(part)
                if not match:
                    continue
                marker = match.group(1).translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"))
                result.append(
                    (
                        Footnote(
                            marker=marker,
                            text=clean_text(match.group(2)),
                            provenance=block.provenance,
                        ),
                        block.section_path,
                    )
                )
        return result

    @staticmethod
    def _same_table_section(
        table_section: tuple[str, ...],
        footnote_section: tuple[str, ...],
    ) -> bool:
        if not table_section or not footnote_section:
            return True
        return (
            table_section == footnote_section
            or table_section[:2] == footnote_section[:2]
        )


@dataclass(frozen=True, slots=True)
class HtmlTextParser:
    """A deliberately simple independent candidate used for parser adjudication."""

    parser_id: str = "html_visible_text"
    parser_version: str = "1"

    def parse(
        self,
        payload: bytes,
        *,
        source_path: str,
        source_sha256: str | None = None,
    ) -> CanonicalDocument:
        source_sha256 = source_sha256 or hashlib.sha256(payload).hexdigest()
        source = payload.decode("utf-8-sig")
        builder = _TreeBuilder()
        builder.feed(source)
        builder.close()
        title_node = _first_node(builder.root, "title")
        html_node = _first_node(builder.root, "html")
        body = _first_node(builder.root, "body") or builder.root
        text = _visible_text(body)
        locator = "page:0/visible_text"
        block = CanonicalBlock(
            block_id=stable_id("block", source_sha256, locator, text),
            kind="paragraph",
            text=text,
            section_path=(),
            provenance=_source_provenance(
                source_sha256=source_sha256,
                source_path=source_path,
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                locator=locator,
            ),
        )
        filename = PurePosixPath(source_path).name
        title = _visible_text(title_node) if title_node else None
        return CanonicalDocument(
            document_id=stable_id(
                "document",
                source_sha256,
                source_path,
                self.parser_id,
            ),
            source_sha256=source_sha256,
            source_path=source_path,
            media_type="text/html",
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            title=title,
            language=(html_node.attr("lang") if html_node else None),
            document_number=document_number_from_text(filename, title),
            subject=None,
            source_filename=filename,
            page_count=1,
            blocks=(block,),
            tables=(),
            records=(),
            facts=(),
            attributes=sorted_attributes({"character_count": len(text)}),
        )
