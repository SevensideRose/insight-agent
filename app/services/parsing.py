"""Document parsing.

docx/html/md go through unstructured. PDFs use a page-level pipeline:
PyMuPDF text layer -> pdfplumber tables -> RapidOCR fallback for scanned
pages. No Tesseract/Poppler requirement on Windows.

Known limits (surfaced to the user):
- Handwritten/complex formulas in scanned PDFs are not reliably recognized.
- Cross-page table merging is conservative (same column count, page edges).
"""
from __future__ import annotations

import io
import json
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import settings
from app.core.text import format_heading_path, normalize_whitespace

SUPPORTED_TYPES = {"docx", "html", "htm", "md", "markdown", "pdf"}

_HEADING_MAX_CHARS = 80
_SENTENCE_END = "。！？!?；;"
_OCR_ENGINE = None


@dataclass
class Block:
    text: str
    category: str = "NarrativeText"
    heading_path: str = ""
    page: int | None = None
    order: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    blocks: list[Block]
    page_count: int = 0
    char_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _sort_and_number(blocks: list[Block]) -> list[Block]:
    blocks.sort(key=lambda b: (b.page or 0, b.order))
    for index, block in enumerate(blocks):
        block.order = index
    return blocks


def _finish(blocks: list[Block], page_count: int, warnings: list[str]) -> ParseResult:
    blocks = [b for b in _sort_and_number(blocks) if b.text.strip()]
    total = sum(len(b.text) for b in blocks)
    return ParseResult(blocks=blocks, page_count=page_count, char_count=total, warnings=warnings)


# --------------------------------------------------------------------------- html table
class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(normalize_whitespace("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_markdown(html: str) -> str:
    if not html:
        return ""
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        return ""
    return rows_to_markdown(parser.rows)


def rows_to_markdown(rows: list[list[Any]]) -> str:
    cleaned: list[list[str]] = []
    for row in rows or []:
        values = [normalize_whitespace(str(cell)) if cell is not None else "" for cell in row]
        if any(values):
            cleaned.append(values)
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    header = cleaned[0]
    lines = ["| " + " | ".join(cell.replace("|", "\\|") for cell in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- unstructured
def _heading_depth(element, fallback: int) -> int:
    metadata = getattr(element, "metadata", None)
    depth = getattr(metadata, "category_depth", None)
    if isinstance(depth, int) and depth >= 0:
        return depth
    return fallback


def _parse_with_unstructured(path: Path, file_type: str) -> ParseResult:
    warnings: list[str] = []
    elements = []
    try:
        if file_type == "docx":
            from unstructured.partition.docx import partition_docx

            elements = partition_docx(filename=str(path), include_page_breaks=False)
        elif file_type == "html":
            from unstructured.partition.html import partition_html

            elements = partition_html(filename=str(path), include_page_breaks=False)
        elif file_type in {"md", "markdown"}:
            from unstructured.partition.md import partition_md

            elements = partition_md(filename=str(path), include_page_breaks=False)
        else:
            raise ValueError(f"unsupported type: {file_type}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("unstructured parsing failed: {}", path)
        raise RuntimeError(f"解析失败（{file_type}）：{exc}") from exc

    blocks: list[Block] = []
    heading_stack: list[tuple[int, str]] = []

    def current_heading() -> str:
        return format_heading_path([text for _, text in heading_stack])

    for order, element in enumerate(elements):
        category = str(getattr(element, "category", "NarrativeText") or "NarrativeText")
        text = normalize_whitespace(str(element))
        if not text:
            continue
        metadata = getattr(element, "metadata", None)
        page = getattr(metadata, "page_number", None)
        page = int(page) if isinstance(page, int) and page > 0 else None

        if category in {"Title", "Header"} and len(text) <= _HEADING_MAX_CHARS:
            depth = _heading_depth(element, 1)
            while heading_stack and heading_stack[-1][0] >= depth:
                heading_stack.pop()
            if not heading_stack or heading_stack[-1][1] != text:
                heading_stack.append((depth, text))
            blocks.append(
                Block(
                    text=text,
                    category="Title",
                    heading_path=current_heading(),
                    page=page,
                    order=order,
                )
            )
            continue

        if category == "Table":
            html = getattr(metadata, "text_as_html", None)
            markdown = html_table_to_markdown(html) if html else text
            blocks.append(
                Block(
                    text=markdown or text,
                    category="Table",
                    heading_path=current_heading(),
                    page=page,
                    order=order,
                    meta={"table": True},
                )
            )
            continue

        blocks.append(
            Block(
                text=text,
                category=category,
                heading_path=current_heading(),
                page=page,
                order=order,
                meta={"source": "unstructured"},
            )
        )

    if not blocks:
        warnings.append("文档中没有解析出可用文本")
    page_count = max((b.page or 0) for b in blocks) if blocks else 0
    return _finish(blocks, page_count, warnings)


# --------------------------------------------------------------------------- pdf
def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
        logger.info("RapidOCR loaded")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RapidOCR unavailable: {}", exc)
        _OCR_ENGINE = False
    return _OCR_ENGINE


def _ocr_page(page) -> str:  # noqa: ANN001
    engine = _ocr_engine()
    if not engine:
        return ""
    try:
        import numpy as np
        from PIL import Image

        pix = page.get_pixmap(dpi=settings.ocr_dpi)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        result, _ = engine(np.array(image))
        if not result:
            return ""
        lines = [str(item[1]).strip() for item in result if len(item) >= 2 and str(item[1]).strip()]
        return normalize_whitespace("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR failed on page {}: {}", getattr(page, "number", "?"), exc)
        return ""


def _bbox_center_inside(block_bbox, table_bbox) -> bool:  # noqa: ANN001
    try:
        x0, y0, x1, y1 = block_bbox
        tx0, ty0, tx1, ty1 = table_bbox
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return tx0 <= cx <= tx1 and ty0 <= cy <= ty1
    except Exception:  # noqa: BLE001
        return False


def _looks_like_heading(text: str, size: float, median_size: float) -> bool:
    if not text or len(text) > _HEADING_MAX_CHARS:
        return False
    if text.endswith(tuple(_SENTENCE_END)) and len(text) > 30:
        return False
    return size >= max(median_size * 1.15, median_size + 1.0)


def _parse_pdf(path: Path) -> ParseResult:
    import fitz

    warnings: list[str] = []
    blocks: list[Block] = []
    page_count = 0
    heading_stack: list[tuple[int, str]] = []

    def current_heading() -> str:
        return format_heading_path([text for _, text in heading_stack])

    doc = fitz.open(path)
    page_count = doc.page_count
    try:
        # Median font size across the document, used for heading detection.
        sizes: list[float] = []
        for page in doc:
            try:
                data = page.get_text("dict")
                for block in data.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if span.get("text", "").strip():
                                sizes.append(float(span.get("size", 0)))
            except Exception:  # noqa: BLE001
                continue
        median_size = statistics.median(sizes) if sizes else 10.0

        pdfplumber_doc = None
        try:
            import pdfplumber

            pdfplumber_doc = pdfplumber.open(str(path))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"表格提取不可用：{exc}")

        order = 0
        for page_index, page in enumerate(doc):
            page_number = page_index + 1
            page_height = float(page.rect.height)
            page_text = ""
            table_bboxes: list[tuple[float, float, float, float]] = []
            page_tables: list[tuple[list[list[str]], tuple[float, float, float, float]]] = []
            # (top_y, insertion_seq, block); sorted so text and tables keep reading order.
            page_items: list[tuple[float, int, Block]] = []
            item_seq = 0

            if pdfplumber_doc is not None:
                try:
                    plumber_page = pdfplumber_doc.pages[page_index]
                    for table in plumber_page.find_tables():
                        try:
                            rows = table.extract() or []
                        except Exception:  # noqa: BLE001
                            continue
                        markdown = rows_to_markdown(rows)
                        if not markdown:
                            continue
                        bbox = tuple(float(v) for v in table.bbox)
                        table_bboxes.append(bbox)
                        page_tables.append((rows, bbox))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("pdfplumber page {} failed: {}", page_number, exc)

            try:
                data = page.get_text("dict")
            except Exception:  # noqa: BLE001
                data = {"blocks": []}

            page_lines: list[str] = []
            for block in data.get("blocks", []):
                block_bbox = block.get("bbox")
                if block_bbox and any(_bbox_center_inside(block_bbox, tb) for tb in table_bboxes):
                    continue
                line_parts: list[str] = []
                max_size = 0.0
                for line in block.get("lines", []):
                    line_text = "".join(
                        span.get("text", "") for span in line.get("spans", [])
                    )
                    line_text = normalize_whitespace(line_text)
                    if not line_text:
                        continue
                    line_parts.append(line_text)
                    max_size = max(
                        max_size,
                        max((float(span.get("size", 0)) for span in line.get("spans", [])), default=0),
                    )
                text = normalize_whitespace("\n".join(line_parts))
                if not text:
                    continue
                page_lines.append(text)
                category = "NarrativeText"
                meta: dict[str, Any] = {"source": "pdf_text"}
                if _looks_like_heading(text, max_size, median_size):
                    category = "Title"
                    meta["heading_depth"] = 1 if max_size >= median_size * 1.6 else 2
                top = float(block_bbox[1]) if block_bbox else float(item_seq)
                page_items.append(
                    (
                        top,
                        item_seq,
                        Block(
                            text=text,
                            category=category,
                            heading_path="",
                            page=page_number,
                            meta=meta,
                        ),
                    )
                )
                item_seq += 1

            page_text = "\n".join(page_lines)

            # Scanned / image-only page -> OCR fallback.
            if settings.ocr_enabled and len(re.sub(r"\s+", "", page_text)) < settings.ocr_min_chars:
                ocr_text = _ocr_page(page)
                if ocr_text:
                    page_items.append(
                        (
                            0.0,
                            item_seq,
                            Block(
                                text=ocr_text,
                                category="NarrativeText",
                                heading_path="",
                                page=page_number,
                                meta={"source": "ocr", "ocr": True},
                            ),
                        )
                    )
                    item_seq += 1
                elif len(re.sub(r"\s+", "", page_text)) == 0:
                    warnings.append(f"第 {page_number} 页没有可提取文本，OCR 不可用")

            for rows, bbox in page_tables:
                markdown = rows_to_markdown(rows)
                if not markdown:
                    continue
                page_items.append(
                    (
                        float(bbox[1]),
                        item_seq,
                        Block(
                            text=markdown,
                            category="Table",
                            heading_path="",
                            page=page_number,
                            meta={
                                "source": "pdf_table",
                                "table": True,
                                "bbox": [round(v, 1) for v in bbox],
                                "near_top": bbox[1] <= page_height * 0.15,
                                "near_bottom": bbox[3] >= page_height * 0.85,
                                "col_count": max((len(r) for r in rows), default=0),
                            },
                        ),
                    )
                )
                item_seq += 1

            # Assign heading paths in true reading order, including tables.
            for _, _, item in sorted(page_items, key=lambda pair: (pair[0], pair[1])):
                if item.category == "Title":
                    depth = int(item.meta.get("heading_depth", 1))
                    while heading_stack and heading_stack[-1][0] >= depth:
                        heading_stack.pop()
                    if not heading_stack or heading_stack[-1][1] != item.text:
                        heading_stack.append((depth, item.text))
                item.heading_path = current_heading()
                item.order = order
                blocks.append(item)
                order += 1

        if pdfplumber_doc is not None:
            try:
                pdfplumber_doc.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        doc.close()

    blocks = _merge_cross_page_tables(blocks, warnings)
    blocks = _drop_repeated_margins(blocks, page_count)
    if not blocks:
        warnings.append("PDF 中没有解析出可用文本")
    return _finish(blocks, page_count, warnings)


def _previous_table(merged: list[Block], block: Block) -> Block | None:
    """Find the table a top-of-page block may continue.

    Only heading blocks may sit between the two table fragments; any narrative
    text on the new page means the table is a separate table.
    """
    for previous in reversed(merged):
        if previous.category == "Table":
            return previous
        if previous.category != "Title":
            return None
    return None


def _merge_cross_page_tables(blocks: list[Block], warnings: list[str]) -> list[Block]:
    if not settings.pdf_merge_cross_page_tables:
        return blocks
    merged: list[Block] = []
    for block in blocks:
        prev = _previous_table(merged, block) if block.category == "Table" else None
        can_merge = (
            prev is not None
            and prev.meta.get("source") == "pdf_table"
            and block.meta.get("source") == "pdf_table"
            and prev.meta.get("col_count") == block.meta.get("col_count")
            and prev.meta.get("near_bottom")
            and block.meta.get("near_top")
            and (prev.page or 0) + 1 == (block.page or 0)
        )
        if not can_merge:
            merged.append(block)
            continue
        prev_lines = prev.text.splitlines()
        block_lines = block.text.splitlines()
        # Drop the repeated header row + separator from the continuation.
        if len(block_lines) > 2:
            block_lines = block_lines[2:]
        prev.text = "\n".join(prev_lines + block_lines)
        prev.meta["page_end"] = block.page
        prev.meta["cross_page_merged"] = True
        warnings.append(f"已合并第 {prev.page}-{block.page} 页的跨页表格")
    return merged


def _drop_repeated_margins(blocks: list[Block], page_count: int) -> list[Block]:
    if page_count < 3:
        return blocks
    candidates: Counter[str] = Counter()
    for block in blocks:
        text = block.text.strip()
        if 0 < len(text) <= 80 and block.meta.get("source") == "pdf_text":
            candidates[text] += 1
    repeated = {
        text for text, count in candidates.items() if count / max(page_count, 1) >= 0.6
    }
    if not repeated:
        return blocks
    return [
        block
        for block in blocks
        if not (block.category != "Title" and block.text.strip() in repeated)
    ]


# --------------------------------------------------------------------------- public
def parse_document(path: str | Path, file_type: str, document_id: str) -> ParseResult:
    path = Path(path)
    file_type = (file_type or "").lower()
    if file_type == "markdown":
        file_type = "md"
    if file_type == "htm":
        file_type = "html"
    if file_type not in SUPPORTED_TYPES:
        raise ValueError(f"不支持的文件类型：{file_type}")
    if not path.exists():
        raise FileNotFoundError(path)

    if file_type == "pdf":
        result = _parse_pdf(path)
    else:
        result = _parse_with_unstructured(path, file_type)

    # Persist parsed blocks for chunk previews and debugging.
    settings.ensure_dirs()
    target = settings.parsed_path / f"{document_id}.json"
    target.write_text(
        json.dumps(
            {
                "document_id": document_id,
                "file_type": file_type,
                "page_count": result.page_count,
                "char_count": result.char_count,
                "warnings": result.warnings,
                "blocks": [asdict(block) for block in result.blocks],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


def load_parsed(document_id: str) -> dict | None:
    target = settings.parsed_path / f"{document_id}.json"
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
