from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

from ..canonical import (
    Block,
    Cell,
    Document,
    Page,
    Table,
    attribute_items,
    stable_id,
)
from ..probe import SourceProbe
from .base import ParseContext, ParseError
from .text import decode_utf8


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
    return " ".join("".join(parts).split())


def _positive_span(value: str | None) -> int:
    if not value:
        return 1
    try:
        parsed = int(value)
    except ValueError:
        return 1
    return min(max(parsed, 1), 10_000)


@dataclass(frozen=True, slots=True)
class HtmlParser:
    parser_id: str = "html_source"
    parser_version: str = "1"

    def supports(self, probe: SourceProbe) -> bool:
        return probe.format == "html"

    def parse(self, payload: bytes, context: ParseContext) -> Document:
        source = decode_utf8(payload)
        builder = _TreeBuilder()
        try:
            builder.feed(source)
            builder.close()
        except (AssertionError, ValueError) as error:
            raise ParseError(f"invalid HTML source: {error}") from error

        content_root = (
            _first_node(builder.root, "main")
            or _first_node(builder.root, "article")
            or _first_node(builder.root, "body")
            or builder.root
        )
        title_node = _first_node(builder.root, "title")
        html_node = _first_node(builder.root, "html")
        title = _visible_text(title_node) if title_node else None
        language = html_node.attr("lang") if html_node else None

        blocks: list[Block] = []
        tables: list[Table] = []
        headings: dict[int, str] = {}
        order = 0

        def section_path() -> tuple[str, ...]:
            return tuple(headings[level] for level in sorted(headings))

        def add_block(node: _Node, kind: str, text: str, **attrs: object) -> None:
            nonlocal order, title
            locator = self._locator(node, kind)
            blocks.append(
                Block(
                    block_id=stable_id(
                        "block", context.probe.source_sha256, locator, text
                    ),
                    page_index=0,
                    order=order,
                    kind=kind,
                    text=text,
                    source_locator=locator,
                    parser_id=self.parser_id,
                    section_path=section_path(),
                    attributes=attribute_items(attrs),
                )
            )
            order += 1
            if not title and text:
                title = text

        def visit(node: _Node) -> None:
            nonlocal order
            if node.tag in _EXCLUDED_TAGS:
                return
            if node.tag == "table":
                table = self._parse_table(
                    node=node,
                    source_sha256=context.probe.source_sha256,
                    order=order,
                    section=section_path(),
                )
                if table is not None:
                    tables.append(table)
                    order += 1
                return
            if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                text = _visible_text(node)
                if text:
                    level = int(node.tag[1])
                    ancestor_headings = dict(headings)
                    headings.clear()
                    headings.update(
                        {
                            key: value
                            for key, value in ancestor_headings.items()
                            if key < level
                        }
                    )
                    headings[level] = text
                    add_block(node, "heading", text, level=level, tag=node.tag)
                return
            if node.tag == "p":
                text = _visible_text(node)
                if text:
                    add_block(node, "paragraph", text, tag=node.tag)
                return
            if node.tag == "li":
                text = _visible_text(node, skip_nested={"ul", "ol"})
                if text:
                    add_block(node, "list_item", text, tag=node.tag)
                for child in node.children:
                    if isinstance(child, _Node) and child.tag in {"ul", "ol"}:
                        visit(child)
                return
            if (
                node.tag in {"div", "section", "dd", "dt"}
                and not _has_descendant(node, _EVIDENCE_DESCENDANTS)
            ):
                text = _visible_text(node)
                if text:
                    add_block(node, "paragraph", text, tag=node.tag)
                return
            for child in node.children:
                if isinstance(child, _Node):
                    visit(child)

        visit(content_root)

        page_locator = "page:0"
        page = Page(
            page_id=stable_id(
                "page", context.probe.source_sha256, page_locator, source
            ),
            index=0,
            blocks=tuple(blocks),
            tables=tuple(tables),
            source_locator=page_locator,
            parser_id=self.parser_id,
            printed_label="1",
            signals=attribute_items(
                {
                    "native_character_count": len(source),
                    "table_count": len(tables),
                }
            ),
        )
        document_locator = f"source:{context.probe.source_name}"
        return Document(
            document_id=stable_id(
                "document",
                context.probe.source_sha256,
                document_locator,
                context.probe.media_type,
            ),
            source_sha256=context.probe.source_sha256,
            source_name=context.probe.source_name,
            media_type=context.probe.media_type,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            pages=(page,),
            title=title or None,
            language=language or None,
            attributes=attribute_items({"encoding": "utf-8"}),
        )

    def _parse_table(
        self,
        *,
        node: _Node,
        source_sha256: str,
        order: int,
        section: tuple[str, ...],
    ) -> Table | None:
        rows = self._table_rows(node)
        placed: list[tuple[_Node, int, int, int, int, str]] = []
        occupied: set[tuple[int, int]] = set()
        column_count = 0
        row_count = len(rows)

        for row_index, row_node in enumerate(rows):
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

        if not placed or column_count == 0:
            return None

        caption_node = next(
            (
                child
                for child in node.children
                if isinstance(child, _Node) and child.tag == "caption"
            ),
            None,
        )
        caption = _visible_text(caption_node) if caption_node else None
        table_locator = self._locator(node, "table")
        table_content = "\n".join(
            f"{row}:{column}:{row_span}:{column_span}:{text}"
            for _, row, column, row_span, column_span, text in placed
        )
        table_id = stable_id(
            "table", source_sha256, table_locator, table_content
        )
        origins_per_row: dict[int, int] = {}
        for _, row, *_ in placed:
            origins_per_row[row] = origins_per_row.get(row, 0) + 1

        cells: list[Cell] = []
        current_group: str | None = None
        for cell_node, row, column, row_span, column_span, text in placed:
            full_width_header = (
                cell_node.tag == "th"
                and origins_per_row[row] == 1
                and column == 0
                and column_span >= column_count
            )
            if full_width_header:
                role = "group_header"
                group = None
                current_group = text or None
            else:
                role = "header" if cell_node.tag == "th" else "body"
                group = current_group
            locator = (
                f"{table_locator}/row:{row}/column:{column}"
                f"/line:{cell_node.line}/column:{cell_node.column}"
            )
            cells.append(
                Cell(
                    cell_id=stable_id("cell", source_sha256, locator, text),
                    row=row,
                    column=column,
                    text=text,
                    source_locator=locator,
                    parser_id=self.parser_id,
                    row_span=row_span,
                    column_span=column_span,
                    role=role,
                    group=group,
                    attributes=attribute_items({"tag": cell_node.tag}),
                )
            )

        return Table(
            table_id=table_id,
            page_index=0,
            order=order,
            row_count=row_count,
            column_count=column_count,
            cells=tuple(cells),
            source_locator=table_locator,
            parser_id=self.parser_id,
            caption=caption or (section[-1] if section else None),
            section_path=section,
            attributes=attribute_items({"syntax": "html_table"}),
        )

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
    def _locator(node: _Node, kind: str) -> str:
        element_id = node.attr("id")
        suffix = f"/id:{element_id}" if element_id else ""
        return (
            f"page:0/html:{node.tag}/line:{node.line}"
            f"/column:{node.column}/{kind}{suffix}"
        )
