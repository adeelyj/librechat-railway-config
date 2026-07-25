from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from ..ids import sha256_bytes
from .parsers.base import ParserUnavailable


@dataclass(frozen=True, slots=True)
class PageRender:
    page_index: int
    image_bytes: bytes
    width_pixels: int
    height_pixels: int
    dpi: int
    media_type: str = "image/png"

    def __post_init__(self) -> None:
        if self.page_index < 0:
            raise ValueError("page_index must be non-negative")
        if not self.image_bytes:
            raise ValueError("rendered page image cannot be empty")
        if self.width_pixels < 1 or self.height_pixels < 1:
            raise ValueError("rendered page dimensions must be positive")
        if not 72 <= self.dpi <= 300:
            raise ValueError("render DPI must be between 72 and 300")
        if self.media_type != "image/png":
            raise ValueError("canonical page renders must use image/png")

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.image_bytes)


class PageRenderer(Protocol):
    renderer_id: str
    renderer_version: str

    def render_pages(
        self,
        payload: bytes,
        *,
        page_indexes: tuple[int, ...],
        dpi: int,
    ) -> tuple[PageRender, ...]: ...


class PdfPlumberPageRenderer:
    """Render explicitly requested PDF pages using the pinned PDF backend."""

    renderer_id = "pdfplumber_page_image"

    def __init__(self) -> None:
        try:
            self.renderer_version = importlib.metadata.version("pdfplumber")
        except importlib.metadata.PackageNotFoundError:
            self.renderer_version = "unknown"

    def render_pages(
        self,
        payload: bytes,
        *,
        page_indexes: tuple[int, ...],
        dpi: int = 150,
    ) -> tuple[PageRender, ...]:
        if not 72 <= dpi <= 300:
            raise ValueError("render DPI must be between 72 and 300")
        requested = tuple(sorted(set(page_indexes)))
        if any(index < 0 for index in requested):
            raise ValueError("page indexes must be non-negative")
        if not requested:
            return ()
        try:
            pdfplumber = importlib.import_module("pdfplumber")
        except ImportError as exc:
            raise ParserUnavailable(
                "PDF page rendering requires the pinned pdfplumber dependency"
            ) from exc

        rendered: list[PageRender] = []
        try:
            with pdfplumber.open(BytesIO(payload)) as document:
                page_count = len(document.pages)
                invalid = [index for index in requested if index >= page_count]
                if invalid:
                    raise ValueError(
                        f"render page index exceeds PDF page count: {invalid[0]}"
                    )
                for index in requested:
                    page_image = document.pages[index].to_image(
                        resolution=dpi,
                        antialias=True,
                    )
                    image = page_image.original
                    output = BytesIO()
                    image.save(output, format="PNG", optimize=False)
                    width, height = image.size
                    rendered.append(
                        PageRender(
                            page_index=index,
                            image_bytes=output.getvalue(),
                            width_pixels=int(width),
                            height_pixels=int(height),
                            dpi=dpi,
                        )
                    )
        except ParserUnavailable:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise ParserUnavailable("PDF page rendering failed") from exc
        return tuple(rendered)
