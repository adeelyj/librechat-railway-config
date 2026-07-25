from __future__ import annotations

import importlib
import importlib.metadata
import hashlib
import re
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Protocol

from ..ids import sha256_json
from .canonical import Block, Document, Page, attribute_items, rounded_bbox, stable_id
from .parsers.base import ParserUnavailable


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("OCR words cannot be empty")
        if not 0 <= self.confidence <= 1:
            raise ValueError("OCR confidence must be between zero and one")
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
            raise ValueError("OCR bounding boxes must be normalized")


@dataclass(frozen=True, slots=True)
class OcrResult:
    engine_id: str
    engine_version: str
    words: tuple[OcrWord, ...]

    @property
    def average_confidence(self) -> float:
        if not self.words:
            return 0.0
        return sum(word.confidence for word in self.words) / len(self.words)


class OcrEngine(Protocol):
    engine_id: str
    engine_version: str

    def recognize(
        self,
        image: bytes,
        *,
        languages: tuple[str, ...],
    ) -> OcrResult: ...


class RapidOcrEngine:
    engine_id = "rapidocr_onnxruntime"

    def __init__(
        self,
        *,
        detection_model: str | Path,
        recognition_model: str | Path,
        classification_model: str | Path,
        detection_model_sha256: str,
        recognition_model_sha256: str,
        classification_model_sha256: str,
    ) -> None:
        paths = {
            "det_model_path": Path(detection_model).resolve(),
            "rec_model_path": Path(recognition_model).resolve(),
            "cls_model_path": Path(classification_model).resolve(),
        }
        expected_hashes = {
            "det_model_path": detection_model_sha256,
            "rec_model_path": recognition_model_sha256,
            "cls_model_path": classification_model_sha256,
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise ValueError(
                "OCR model files must be baked into the worker image: "
                + ", ".join(missing)
            )
        verified_hashes: dict[str, str] = {}
        for name, path in paths.items():
            expected = expected_hashes[name].casefold()
            if not _SHA256_RE.fullmatch(expected):
                raise ValueError(f"{name} SHA-256 must be 64 lowercase hex characters")
            actual = _sha256_file(path)
            if actual != expected:
                raise ValueError(f"OCR model checksum mismatch: {path}")
            verified_hashes[name] = actual
        try:
            package_version = importlib.metadata.version("rapidocr-onnxruntime")
        except importlib.metadata.PackageNotFoundError as exc:
            raise ParserUnavailable(
                "RapidOCR worker dependency is not installed"
            ) from exc
        self.package_version = package_version
        self.model_fingerprint = sha256_json(verified_hashes)
        self.engine_version = (
            f"{package_version}+models.{self.model_fingerprint[:16]}"
        )
        try:
            module = importlib.import_module("rapidocr_onnxruntime")
        except ImportError as exc:
            raise ParserUnavailable("RapidOCR worker dependency is not installed") from exc
        self._engine = module.RapidOCR(
            **{name: str(path) for name, path in paths.items()}
        )

    def recognize(
        self,
        image: bytes,
        *,
        languages: tuple[str, ...],
    ) -> OcrResult:
        try:
            pillow = importlib.import_module("PIL.Image")
        except ImportError as exc:
            raise ParserUnavailable("RapidOCR provenance normalization requires Pillow") from exc
        with pillow.open(BytesIO(image)) as rendered:
            width, height = rendered.size
        return self.recognize_with_dimensions(
            image,
            width=width,
            height=height,
            languages=languages,
        )

    def recognize_with_dimensions(
        self,
        image: bytes,
        *,
        width: int,
        height: int,
        languages: tuple[str, ...],
    ) -> OcrResult:
        del languages
        if width < 1 or height < 1:
            raise ValueError("rendered OCR image dimensions must be positive")
        result, _elapsed = self._engine(image)
        words: list[OcrWord] = []
        for item in result or ():
            polygon, text, confidence = item
            x_values = [float(point[0]) for point in polygon]
            y_values = [float(point[1]) for point in polygon]
            words.append(
                OcrWord(
                    text=str(text).strip(),
                    bbox=rounded_bbox(
                        (
                            min(x_values) / width,
                            min(y_values) / height,
                            max(x_values) / width,
                            max(y_values) / height,
                        )
                    ),
                    confidence=float(confidence),
                )
            )
        return OcrResult(self.engine_id, self.engine_version, tuple(words))


def apply_page_ocr(
    document: Document,
    *,
    page_images: dict[int, bytes],
    engine: OcrEngine,
    languages: tuple[str, ...] = ("de", "en"),
    minimum_average_confidence: float = 0.80,
) -> Document:
    """Enrich only deficient pages and preserve every valid native block."""

    if not 0 <= minimum_average_confidence <= 1:
        raise ValueError("minimum_average_confidence must be between zero and one")
    pages: list[Page] = []
    for page in document.pages:
        if not page.ocr_needed:
            pages.append(page)
            continue
        if page.index not in page_images:
            raise ValueError(f"missing rendered image for OCR page {page.index + 1}")
        result = engine.recognize(page_images[page.index], languages=languages)
        accepted = (
            bool(result.words)
            and result.average_confidence >= minimum_average_confidence
        )
        ocr_blocks: list[Block] = []
        if accepted:
            for index, word in enumerate(result.words):
                locator = f"page:{page.index}/ocr:{result.engine_id}/word:{index}"
                ocr_blocks.append(
                    Block(
                        block_id=stable_id(
                            "block",
                            document.source_sha256,
                            locator,
                            word.text,
                        ),
                        page_index=page.index,
                        order=len(page.blocks) + index,
                        kind="paragraph",
                        text=word.text,
                        source_locator=locator,
                        parser_id=result.engine_id,
                        bbox=word.bbox,
                        confidence=word.confidence,
                        attributes=attribute_items(
                            {"ocr_engine_version": result.engine_version}
                        ),
                    )
                )
        signals = dict(page.signals)
        signals.update(
            {
                "ocr_engine": result.engine_id,
                "ocr_engine_version": result.engine_version,
                "ocr_word_count": len(result.words),
                "ocr_average_confidence": result.average_confidence,
                "ocr_accepted": accepted,
            }
        )
        pages.append(
            replace(
                page,
                blocks=page.blocks + tuple(ocr_blocks),
                ocr_needed=not accepted,
                signals=attribute_items(signals),
            )
        )
    return replace(
        document,
        pages=tuple(pages),
        attributes=attribute_items(
            {
                **dict(document.attributes),
                "ocr_engine": engine.engine_id,
                "ocr_engine_version": engine.engine_version,
                "ocr_languages": ",".join(languages),
            }
        ),
    )
