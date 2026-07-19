from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deduplicate and normalize Bauer PDF/HTML sources for LibreChat file search."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(r"D:\02_Code\Bauer Kompressoren Demo"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(r"D:\02_Code\LibreChat_Setup\tmp\corpora\bauer-kompressoren"),
    )
    parser.add_argument(
        "--deps",
        type=Path,
        default=Path(r"D:\02_Code\LibreChat_Setup\tmp\python-deps"),
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_stem(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return (value or "document")[:90].rstrip("-._")


def normalized_lines(text: str) -> str:
    lines: list[str] = []
    previous = None
    for raw in text.replace("\x00", "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if line == previous:
            continue
        lines.append(line)
        previous = line
    return "\n".join(lines).strip()


def find_sources(root: Path) -> list[Path]:
    allowed = {".pdf", ".html", ".htm"}
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in allowed),
        key=lambda path: str(path.relative_to(root)).casefold(),
    )


def deduplicate(paths: list[Path]) -> list[dict]:
    groups: OrderedDict[str, dict] = OrderedDict()
    for path in paths:
        checksum = sha256_file(path)
        if checksum not in groups:
            groups[checksum] = {"checksum": checksum, "path": path, "duplicates": []}
        else:
            groups[checksum]["duplicates"].append(path)
    return list(groups.values())


def extract_html(path: Path) -> tuple[str, str]:
    from bs4 import BeautifulSoup

    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "form", "nav"]):
        tag.decompose()
    title = normalized_lines(soup.title.get_text(" ", strip=True)) if soup.title else path.stem
    body = soup.body if soup.body is not None else soup
    text = normalized_lines(body.get_text("\n", strip=True))
    return title or path.stem, text


class PdfExtractor:
    def __init__(self) -> None:
        self._ocr = None

    @property
    def ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr = RapidOCR()
        return self._ocr

    def ocr_page(self, page) -> str:
        import fitz
        import numpy as np

        pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )
        result, _ = self.ocr(image)
        if not result:
            return ""
        return normalized_lines("\n".join(str(item[1]) for item in result if len(item) > 1))

    def extract(self, path: Path) -> tuple[list[str], bool]:
        import fitz

        document = fitz.open(path)
        if document.needs_pass and not document.authenticate(""):
            raise RuntimeError(f"PDF requires a non-empty password: {path}")

        page_text = [normalized_lines(page.get_text("text")) for page in document]
        total_chars = sum(len(text) for text in page_text)
        used_ocr = False

        if total_chars < 100:
            page_text = []
            for page in document:
                text = self.ocr_page(page)
                page_text.append(text)
            used_ocr = True

        document.close()
        return page_text, used_ocr


def make_markdown(
    title: str,
    relative_path: str,
    checksum: str,
    content_sections: list[tuple[str, str]],
) -> str:
    output = [
        f"# {title}",
        "",
        f"- Original source: `{relative_path}`",
        f"- Original SHA-256: `{checksum}`",
        "",
    ]
    for heading, text in content_sections:
        output.extend([f"## {heading}", "", text or "[No readable text was found.]", ""])
    return "\n".join(output).strip() + "\n"


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(args.deps))

    if not args.source.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {args.source}")
    if not args.deps.is_dir():
        raise FileNotFoundError(f"Workspace OCR dependencies do not exist: {args.deps}")

    if args.output.exists() and any(args.output.iterdir()):
        raise RuntimeError(f"Output directory is not empty: {args.output}")
    args.output.mkdir(parents=True, exist_ok=True)

    sources = find_sources(args.source)
    unique = deduplicate(sources)
    pdf_extractor = PdfExtractor()
    manifest: list[dict] = []

    for ordinal, item in enumerate(unique, start=1):
        source: Path = item["path"]
        relative = str(source.relative_to(args.source))
        checksum = item["checksum"]
        duplicate_paths = [str(path.relative_to(args.source)) for path in item["duplicates"]]
        kind = "pdf" if source.suffix.lower() == ".pdf" else "html"
        used_ocr = False
        page_count = None

        if kind == "pdf":
            pages, used_ocr = pdf_extractor.extract(source)
            page_count = len(pages)
            title = source.stem
            sections = [(f"Page {index}", text) for index, text in enumerate(pages, start=1)]
        else:
            title, text = extract_html(source)
            sections = [("Web page content", text)]

        content = make_markdown(title, relative, checksum, sections)
        filename = f"{ordinal:04d}-{safe_stem(source.stem)}-{checksum[:10]}.md"
        destination = args.output / filename
        destination.write_text(content, encoding="utf-8", newline="\n")
        exported_checksum = sha256_file(destination)

        manifest.append(
            {
                "filename": filename,
                "kind": kind,
                "sourcePath": relative,
                "sourceChecksum": checksum,
                "exportedChecksum": exported_checksum,
                "duplicatePaths": duplicate_paths,
                "pageCount": page_count,
                "ocrUsed": used_ocr,
                "characterCount": len(content),
            }
        )

    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )

    summary = {
        "sourceFiles": len(sources),
        "uniqueDocuments": len(unique),
        "duplicateFilesRemoved": len(sources) - len(unique),
        "pdfDocuments": sum(1 for item in manifest if item["kind"] == "pdf"),
        "htmlDocuments": sum(1 for item in manifest if item["kind"] == "html"),
        "ocrDocuments": sum(1 for item in manifest if item["ocrUsed"]),
        "ocrPages": sum(item["pageCount"] or 0 for item in manifest if item["ocrUsed"]),
        "outputDirectory": str(args.output),
        "manifestPath": str(manifest_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
