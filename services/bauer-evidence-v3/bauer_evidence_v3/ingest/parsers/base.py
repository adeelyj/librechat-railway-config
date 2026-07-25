from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..canonical import Attributes, Document
from ..probe import SourceProbe


class ParseError(ValueError):
    """The selected parser could not produce trustworthy canonical evidence."""


class UnsupportedFormat(ParseError):
    """No configured parser supports the probed source format."""


class ParserUnavailable(ParseError):
    """An optional parser dependency is not installed in this worker."""


@dataclass(frozen=True, slots=True)
class ParseContext:
    probe: SourceProbe
    attributes: Attributes = ()


class DocumentParser(Protocol):
    parser_id: str
    parser_version: str

    def supports(self, probe: SourceProbe) -> bool: ...

    def parse(self, payload: bytes, context: ParseContext) -> Document: ...
