from __future__ import annotations

from dataclasses import dataclass

from ..canonical import (
    Block,
    Document,
    Page,
    attribute_items,
    clean_text,
    stable_id,
)
from ..probe import SourceProbe
from .base import ParseContext, ParseError


def decode_utf8(payload: bytes) -> str:
    try:
        return clean_text(payload.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ParseError(
            f"source is not deterministic UTF-8 at byte {error.start}"
        ) from error


@dataclass(frozen=True, slots=True)
class TextParser:
    parser_id: str = "text_utf8"
    parser_version: str = "1"

    def supports(self, probe: SourceProbe) -> bool:
        return probe.format == "text"

    def parse(self, payload: bytes, context: ParseContext) -> Document:
        text = decode_utf8(payload)
        page_texts = text.split("\f")
        pages: list[Page] = []
        title: str | None = None

        for page_index, page_text in enumerate(page_texts):
            blocks = self._blocks_for_page(
                page_text=page_text,
                page_index=page_index,
                source_sha256=context.probe.source_sha256,
            )
            if title is None:
                title = next(
                    (
                        block.text.splitlines()[0].strip()
                        for block in blocks
                        if block.text.strip()
                    ),
                    None,
                )
            page_locator = f"page:{page_index}"
            pages.append(
                Page(
                    page_id=stable_id(
                        "page",
                        context.probe.source_sha256,
                        page_locator,
                        page_text,
                    ),
                    index=page_index,
                    blocks=tuple(blocks),
                    tables=(),
                    source_locator=page_locator,
                    parser_id=self.parser_id,
                    printed_label=str(page_index + 1),
                    signals=attribute_items(
                        {
                            "native_character_count": len(page_text),
                            "native_line_count": len(page_text.splitlines()),
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
            title=title,
            attributes=attribute_items(
                {
                    "encoding": "utf-8",
                    "form_feed_pages": len(pages),
                }
            ),
        )

    def _blocks_for_page(
        self,
        *,
        page_text: str,
        page_index: int,
        source_sha256: str,
    ) -> list[Block]:
        lines = page_text.split("\n")
        blocks: list[Block] = []
        paragraph: list[str] = []
        paragraph_start = 1

        def flush(end_line: int) -> None:
            nonlocal paragraph
            if not paragraph:
                return
            value = "\n".join(paragraph)
            locator = f"page:{page_index}/lines:{paragraph_start}-{end_line}"
            blocks.append(
                Block(
                    block_id=stable_id("block", source_sha256, locator, value),
                    page_index=page_index,
                    order=len(blocks),
                    kind="paragraph",
                    text=value,
                    source_locator=locator,
                    parser_id=self.parser_id,
                )
            )
            paragraph = []

        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                flush(line_number - 1)
                continue
            if not paragraph:
                paragraph_start = line_number
            paragraph.append(line)
        flush(len(lines))
        return blocks
