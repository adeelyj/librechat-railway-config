from __future__ import annotations

import re
from dataclasses import dataclass

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
from .base import ParseContext
from .text import decode_utf8


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_LIST_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+(.+)$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")


def _split_pipe_row(line: str) -> tuple[str, ...]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]

    cells: list[str] = []
    buffer: list[str] = []
    escaped = False
    code_ticks = 0
    for character in value:
        if escaped:
            buffer.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "`":
            code_ticks = 0 if code_ticks else 1
            buffer.append(character)
            continue
        if character == "|" and not code_ticks:
            cells.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(character)
    if escaped:
        buffer.append("\\")
    cells.append("".join(buffer).strip())
    return tuple(cells)


def _is_pipe_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    header = _split_pipe_row(lines[index])
    separator = _split_pipe_row(lines[index + 1])
    return (
        len(header) >= 2
        and len(separator) == len(header)
        and all(_SEPARATOR_RE.fullmatch(cell.replace(" ", "")) for cell in separator)
    )


@dataclass(frozen=True, slots=True)
class MarkdownParser:
    parser_id: str = "markdown_source"
    parser_version: str = "1"

    def supports(self, probe: SourceProbe) -> bool:
        return probe.format == "markdown"

    def parse(self, payload: bytes, context: ParseContext) -> Document:
        text = decode_utf8(payload)
        pages: list[Page] = []
        document_title: str | None = None

        for page_index, page_text in enumerate(text.split("\f")):
            blocks, tables, page_title = self._parse_page(
                page_text=page_text,
                page_index=page_index,
                source_sha256=context.probe.source_sha256,
            )
            if document_title is None and page_title:
                document_title = page_title
            locator = f"page:{page_index}"
            pages.append(
                Page(
                    page_id=stable_id(
                        "page",
                        context.probe.source_sha256,
                        locator,
                        page_text,
                    ),
                    index=page_index,
                    blocks=tuple(blocks),
                    tables=tuple(tables),
                    source_locator=locator,
                    parser_id=self.parser_id,
                    printed_label=str(page_index + 1),
                    signals=attribute_items(
                        {
                            "native_character_count": len(page_text),
                            "native_line_count": len(page_text.splitlines()),
                            "table_count": len(tables),
                        }
                    ),
                )
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
            pages=tuple(pages),
            title=document_title,
            attributes=attribute_items({"encoding": "utf-8"}),
        )

    def _parse_page(
        self,
        *,
        page_text: str,
        page_index: int,
        source_sha256: str,
    ) -> tuple[list[Block], list[Table], str | None]:
        lines = page_text.split("\n")
        blocks: list[Block] = []
        tables: list[Table] = []
        headings: dict[int, str] = {}
        title: str | None = None
        order = 0
        index = 0

        def section_path() -> tuple[str, ...]:
            return tuple(headings[level] for level in sorted(headings))

        def add_block(
            *,
            kind: str,
            value: str,
            first_line: int,
            last_line: int,
            attributes=(),
        ) -> None:
            nonlocal order
            locator = f"page:{page_index}/lines:{first_line}-{last_line}/{kind}"
            blocks.append(
                Block(
                    block_id=stable_id("block", source_sha256, locator, value),
                    page_index=page_index,
                    order=order,
                    kind=kind,
                    text=value,
                    source_locator=locator,
                    parser_id=self.parser_id,
                    section_path=section_path(),
                    attributes=attributes,
                )
            )
            order += 1

        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue

            heading_match = _HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                value = heading_match.group(2).strip()
                headings = {
                    existing_level: heading
                    for existing_level, heading in headings.items()
                    if existing_level < level
                }
                headings[level] = value
                if title is None:
                    title = value
                add_block(
                    kind="heading",
                    value=value,
                    first_line=index + 1,
                    last_line=index + 1,
                    attributes=attribute_items({"level": level}),
                )
                index += 1
                continue

            if _is_pipe_table_start(lines, index):
                table, consumed = self._parse_pipe_table(
                    lines=lines,
                    start=index,
                    page_index=page_index,
                    order=order,
                    source_sha256=source_sha256,
                    section=section_path(),
                )
                tables.append(table)
                order += 1
                index += consumed
                continue

            fence_match = _FENCE_RE.match(line)
            if fence_match:
                fence = fence_match.group(1)
                language = fence_match.group(2).strip()
                start = index
                content: list[str] = []
                index += 1
                while index < len(lines) and not lines[index].lstrip().startswith(fence):
                    content.append(lines[index])
                    index += 1
                if index < len(lines):
                    index += 1
                add_block(
                    kind="code",
                    value="\n".join(content),
                    first_line=start + 1,
                    last_line=index,
                    attributes=attribute_items({"language": language}),
                )
                continue

            list_match = _LIST_RE.match(line)
            if list_match:
                add_block(
                    kind="list_item",
                    value=list_match.group(1).strip(),
                    first_line=index + 1,
                    last_line=index + 1,
                )
                index += 1
                continue

            start = index
            paragraph = [line]
            index += 1
            while index < len(lines):
                if not lines[index].strip():
                    break
                if (
                    _HEADING_RE.match(lines[index])
                    or _FENCE_RE.match(lines[index])
                    or _LIST_RE.match(lines[index])
                    or _is_pipe_table_start(lines, index)
                ):
                    break
                paragraph.append(lines[index])
                index += 1
            value = "\n".join(paragraph)
            if title is None:
                title = paragraph[0].strip()
            add_block(
                kind="paragraph",
                value=value,
                first_line=start + 1,
                last_line=start + len(paragraph),
            )

        return blocks, tables, title

    def _parse_pipe_table(
        self,
        *,
        lines: list[str],
        start: int,
        page_index: int,
        order: int,
        source_sha256: str,
        section: tuple[str, ...],
    ) -> tuple[Table, int]:
        headers = _split_pipe_row(lines[start])
        rows: list[tuple[str, ...]] = [headers]
        cursor = start + 2
        while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
            rows.append(_split_pipe_row(lines[cursor]))
            cursor += 1

        column_count = max(len(row) for row in rows)
        padded_rows = [row + ("",) * (column_count - len(row)) for row in rows]
        locator = f"page:{page_index}/lines:{start + 1}-{cursor}/table"
        table_content = "\n".join("\t".join(row) for row in padded_rows)
        table_id = stable_id("table", source_sha256, locator, table_content)
        cells: list[Cell] = []
        for row_index, row in enumerate(padded_rows):
            source_line = start + 1 if row_index == 0 else start + row_index + 2
            for column_index, value in enumerate(row):
                cell_locator = (
                    f"{locator}/row:{row_index}/column:{column_index}/line:{source_line}"
                )
                cells.append(
                    Cell(
                        cell_id=stable_id(
                            "cell", source_sha256, cell_locator, value
                        ),
                        row=row_index,
                        column=column_index,
                        text=value,
                        source_locator=cell_locator,
                        parser_id=self.parser_id,
                        role="header" if row_index == 0 else "body",
                    )
                )

        return (
            Table(
                table_id=table_id,
                page_index=page_index,
                order=order,
                row_count=len(padded_rows),
                column_count=column_count,
                cells=tuple(cells),
                source_locator=locator,
                parser_id=self.parser_id,
                caption=section[-1] if section else None,
                section_path=section,
                attributes=attribute_items({"syntax": "gfm_pipe_table"}),
            ),
            cursor - start,
        )
