"""Document AI layout extraction, table reconstruction, and chunk normalization."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from common.models.schema import ChunkNode, DocumentNode
from common.parsers.documentai_client import ParsedDocument
from common.storage import PdfMetadata

logger = logging.getLogger("common.parsers.layout_extractor")

# Default chunking parameters
DEFAULT_MAX_CHUNK_CHARS = 2000
DEFAULT_CHUNK_OVERLAP_CHARS = 200


class EmptyDocumentExtractionError(ValueError):
    """Raised when Document AI layout extraction yields zero chunks."""

    pass


@dataclass(frozen=True)
class BoundingBox:
    """Normalized bounding box coordinates (0.0 to 1.0)."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True)
class LayoutElement:
    """Extracted layout element (paragraph, heading, table, etc.)."""

    element_type: str  # "TEXT", "TABLE", "HEADER", "FOOTER"
    content: str
    page_number: int
    bounding_box: BoundingBox | None = None


@dataclass(frozen=True)
class NormalizedDocument:
    """Normalized document representation containing DocumentNode and ChunkNodes."""

    document_node: DocumentNode
    chunks: list[ChunkNode]
    page_count: int
    table_count: int
    total_characters: int


def extract_text_from_anchor(document_text: str, text_anchor: Any) -> str:
    """Extracts text slices from document_text referenced by text_anchor.text_segments."""
    if not text_anchor or not hasattr(text_anchor, "text_segments"):
        return ""

    segments = text_anchor.text_segments
    if not segments:
        return ""

    chunks = []
    for segment in segments:
        start_index = int(getattr(segment, "start_index", 0) or 0)
        end_index = int(getattr(segment, "end_index", 0) or 0)
        if start_index < len(document_text):
            chunks.append(document_text[start_index: min(end_index, len(document_text))])

    return "".join(chunks)


def extract_bounding_box(layout_or_poly: Any) -> BoundingBox | None:
    """Extracts normalized bounding box from a layout, bounding_poly, or direct vertices."""
    if not layout_or_poly:
        return None

    poly = getattr(layout_or_poly, "bounding_poly", layout_or_poly)
    vertices = getattr(poly, "normalized_vertices", None)
    if not vertices:
        vertices = getattr(poly, "vertices", None)
    if not vertices:
        return None

    try:
        x_coords = [float(getattr(v, "x", 0.0) or 0.0) for v in vertices]
        y_coords = [float(getattr(v, "y", 0.0) or 0.0) for v in vertices]
        if not x_coords or not y_coords:
            return None

        return BoundingBox(
            x_min=min(x_coords),
            y_min=min(y_coords),
            x_max=max(x_coords),
            y_max=max(y_coords),
        )
    except Exception:
        return None


def convert_table_to_markdown(document_text: str, table: Any) -> str:
    """Converts a Document AI Table into a readable, formatted Markdown table."""
    if not table:
        return ""

    header_rows = getattr(table, "header_rows", []) or []
    body_rows = getattr(table, "body_rows", []) or []

    if not header_rows and not body_rows:
        return ""

    def _extract_cells(row: Any) -> list[str]:
        cells = getattr(row, "cells", []) or []
        row_text = []
        for cell in cells:
            layout = getattr(cell, "layout", None)
            anchor = getattr(layout, "text_anchor", None) if layout else None
            cell_str = extract_text_from_anchor(document_text, anchor).strip()
            # Clean newlines and pipes
            clean_str = " ".join(cell_str.replace("|", "\\|").split())
            row_text.append(clean_str or "")
        return row_text

    parsed_headers: list[str] = []
    if header_rows:
        parsed_headers = _extract_cells(header_rows[0])

    parsed_body: list[list[str]] = []
    for r in body_rows:
        parsed_body.append(_extract_cells(r))

    # Calculate column count
    num_cols = len(parsed_headers)
    for row in parsed_body:
        num_cols = max(num_cols, len(row))

    if num_cols == 0:
        return ""

    # Synthesize headers if absent
    if not parsed_headers:
        if parsed_body:
            # Promote first body row to header
            parsed_headers = parsed_body.pop(0)
            while len(parsed_headers) < num_cols:
                parsed_headers.append(f"Column {len(parsed_headers) + 1}")
        else:
            parsed_headers = [f"Column {i + 1}" for i in range(num_cols)]

    while len(parsed_headers) < num_cols:
        parsed_headers.append(f"Column {len(parsed_headers) + 1}")

    # Build Markdown table
    lines = [
        "| " + " | ".join(h or f"Column {i + 1}" for i, h in enumerate(parsed_headers)) + " |",
        "| " + " | ".join(["---"] * num_cols) + " |",
    ]

    for row in parsed_body:
        # Pad row if necessary
        padded_row = list(row)
        while len(padded_row) < num_cols:
            padded_row.append("")
        lines.append("| " + " | ".join(padded_row) + " |")

    return "\n".join(lines)


def extract_text_from_layout_blocks(blocks: Any) -> str:
    """Recursively extracts plain text from a list of DocumentLayoutBlock objects."""
    if not blocks:
        return ""
    texts: list[str] = []
    for b in blocks:
        tb = getattr(b, "text_block", None)
        if tb:
            t = getattr(tb, "text", "") or ""
            if t.strip():
                texts.append(t.strip())
            # Child blocks inside text_block
            child_blocks = getattr(tb, "blocks", None)
            if child_blocks:
                sub = extract_text_from_layout_blocks(child_blocks)
                if sub:
                    texts.append(sub)

        lb = getattr(b, "list_block", None)
        if lb:
            for entry in getattr(lb, "list_entries", []) or []:
                entry_blocks = getattr(entry, "blocks", []) or []
                entry_text = extract_text_from_layout_blocks(entry_blocks)
                if entry_text:
                    texts.append(f"- {entry_text}")

    return "\n".join(texts)


def convert_layout_table_to_markdown(table_block: Any) -> str:
    """Converts a DocumentLayout.DocumentLayoutBlock.LayoutTableBlock into Markdown."""
    if not table_block:
        return ""

    header_rows = getattr(table_block, "header_rows", []) or []
    body_rows = getattr(table_block, "body_rows", []) or []

    if not header_rows and not body_rows:
        return ""

    def _extract_cells(row: Any) -> list[str]:
        cells = getattr(row, "cells", []) or []
        row_text = []
        for cell in cells:
            cell_blocks = getattr(cell, "blocks", []) or []
            cell_str = extract_text_from_layout_blocks(cell_blocks)
            clean_str = " ".join(cell_str.replace("|", "\\|").split())
            row_text.append(clean_str or "")
        return row_text

    parsed_headers: list[str] = []
    if header_rows:
        parsed_headers = _extract_cells(header_rows[0])

    parsed_body: list[list[str]] = [_extract_cells(r) for r in body_rows]

    num_cols = len(parsed_headers)
    for r in parsed_body:
        num_cols = max(num_cols, len(r))

    if num_cols == 0:
        return ""

    if not parsed_headers:
        if parsed_body:
            parsed_headers = parsed_body.pop(0)
            while len(parsed_headers) < num_cols:
                parsed_headers.append(f"Column {len(parsed_headers) + 1}")
        else:
            parsed_headers = [f"Column {i + 1}" for i in range(num_cols)]

    while len(parsed_headers) < num_cols:
        parsed_headers.append(f"Column {len(parsed_headers) + 1}")

    lines = [
        "| " + " | ".join(h or f"Column {i + 1}" for i, h in enumerate(parsed_headers)) + " |",
        "| " + " | ".join(["---"] * num_cols) + " |",
    ]

    for row in parsed_body:
        padded_row = list(row)
        while len(padded_row) < num_cols:
            padded_row.append("")
        lines.append("| " + " | ".join(padded_row) + " |")

    return "\n".join(lines)


def extract_elements_from_document_layout(
    document_layout: Any,
    strip_headers_footers: bool = True,
) -> list[LayoutElement]:
    """Extracts LayoutElements from Document AI document_layout.blocks hierarchy."""
    if not document_layout or not hasattr(document_layout, "blocks"):
        return []

    blocks = document_layout.blocks
    if not blocks:
        return []

    elements: list[LayoutElement] = []

    def _process_block(block: Any) -> None:
        page_span = getattr(block, "page_span", None)
        page_number = int(getattr(page_span, "page_start", 1) or 1)
        bbox = extract_bounding_box(getattr(block, "bounding_box", None))

        is_extreme_bbox = False
        if strip_headers_footers and bbox:
            if bbox.y_max <= 0.07 or bbox.y_min >= 0.93:
                is_extreme_bbox = True

        # 1. Text block
        tb = getattr(block, "text_block", None)
        if tb:
            text = getattr(tb, "text", "") or ""
            b_type = (getattr(tb, "type_", "TEXT") or "TEXT").upper()
            if strip_headers_footers and (b_type in ("HEADER", "FOOTER") or is_extreme_bbox):
                pass
            elif text.strip():
                elements.append(
                    LayoutElement(
                        element_type="TEXT",
                        content=text.strip(),
                        page_number=page_number,
                        bounding_box=bbox,
                    )
                )

            # Process nested child blocks inside text_block if present
            for child in getattr(tb, "blocks", []) or []:
                _process_block(child)

        # 2. Table block
        tbl = getattr(block, "table_block", None)
        if tbl:
            md_tbl = convert_layout_table_to_markdown(tbl)
            if md_tbl.strip():
                elements.append(
                    LayoutElement(
                        element_type="TABLE",
                        content=md_tbl.strip(),
                        page_number=page_number,
                        bounding_box=bbox,
                    )
                )

        # 3. List block
        lb = getattr(block, "list_block", None)
        if lb:
            list_items: list[str] = []
            for entry in getattr(lb, "list_entries", []) or []:
                entry_blocks = getattr(entry, "blocks", []) or []
                entry_text = extract_text_from_layout_blocks(entry_blocks)
                if entry_text.strip():
                    list_items.append(f"- {entry_text.strip()}")
            if list_items:
                elements.append(
                    LayoutElement(
                        element_type="TEXT",
                        content="\n".join(list_items),
                        page_number=page_number,
                        bounding_box=bbox,
                    )
                )

    for block in blocks:
        _process_block(block)

    return elements


def generate_document_id(
    metadata: PdfMetadata | None = None,
    fallback_name: str = "",
) -> str:
    """Generates a stable, deterministic DocumentId from file provenance."""
    if metadata and metadata.sha256_hash:
        return f"doc_{metadata.sha256_hash[:32]}"

    if metadata and metadata.gcs_uri:
        h = hashlib.sha256(metadata.gcs_uri.encode("utf-8")).hexdigest()
        return f"doc_{h[:32]}"

    h = hashlib.sha256(fallback_name.encode("utf-8")).hexdigest()
    return f"doc_{h[:32]}"


def generate_chunk_id(
    document_id: str,
    page_number: int,
    chunk_index: int,
    modality: str = "TEXT",
) -> str:
    """Generates a deterministic ChunkId guaranteed <= 64 characters.

    Retains the full content-addressed document ID prefix without unnecessary truncation.
    Example: 'doc_e3b0c44298fc1c149afbf4c8996fb924_p01_t001'
    """
    mod_tag = "b" if modality.upper() == "TABLE" else "t"
    suffix = f"_p{page_number:02d}_{mod_tag}{chunk_index:03d}"
    max_prefix_len = 64 - len(suffix)

    clean_doc = document_id if document_id.startswith("doc_") else f"doc_{document_id}"
    doc_prefix = clean_doc[:max_prefix_len]
    return f"{doc_prefix}{suffix}"


def split_oversized_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Splits oversized text cleanly at paragraph, sentence, or word boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current_chunk = ""

    for para in paragraphs:
        para_clean = para.strip()
        if not para_clean:
            continue

        if len(current_chunk) + len(para_clean) + 2 <= max_chars:
            current_chunk = f"{current_chunk}\n\n{para_clean}" if current_chunk else para_clean
        else:
            if current_chunk:
                chunks.append(current_chunk)

            # If paragraph itself is larger than max_chars, split by words
            if len(para_clean) > max_chars:
                words = para_clean.split(" ")
                sub_chunk = ""
                for word in words:
                    if len(sub_chunk) + len(word) + 1 <= max_chars:
                        sub_chunk = f"{sub_chunk} {word}" if sub_chunk else word
                    else:
                        if sub_chunk:
                            chunks.append(sub_chunk)
                        sub_chunk = word
                if sub_chunk:
                    current_chunk = sub_chunk
                else:
                    current_chunk = ""
            else:
                current_chunk = para_clean

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def extract_page_elements(
    document_text: str,
    page: Any,
    strip_headers_footers: bool = True,
) -> list[LayoutElement]:
    """Extracts ordered layout elements (text paragraphs/blocks/lines and tables) for a single page."""
    page_number = int(getattr(page, "page_number", 1) or 1)
    elements: list[LayoutElement] = []

    # 1. Extract tables
    tables = getattr(page, "tables", []) or []
    for tbl in tables:
        md_table = convert_table_to_markdown(document_text, tbl)
        if md_table.strip():
            bbox = extract_bounding_box(getattr(tbl, "layout", None))
            elements.append(
                LayoutElement(
                    element_type="TABLE",
                    content=md_table,
                    page_number=page_number,
                    bounding_box=bbox,
                )
            )

    # 2. Extract text: paragraphs -> blocks -> lines (hierarchy of fallback)
    text_containers = getattr(page, "paragraphs", []) or []
    if not text_containers:
        text_containers = getattr(page, "blocks", []) or []
    if not text_containers:
        text_containers = getattr(page, "lines", []) or []

    for item in text_containers:
        layout = getattr(item, "layout", None)
        anchor = getattr(layout, "text_anchor", None) if layout else None
        text_content = extract_text_from_anchor(document_text, anchor).strip()
        if not text_content:
            continue

        bbox = extract_bounding_box(layout)

        # Detect header / footer if bbox is at extremes of the page
        element_type = "TEXT"
        if strip_headers_footers and bbox:
            if bbox.y_max <= 0.07:
                element_type = "HEADER"
            elif bbox.y_min >= 0.93:
                element_type = "FOOTER"

        if strip_headers_footers and element_type in ("HEADER", "FOOTER"):
            continue

        elements.append(
            LayoutElement(
                element_type=element_type,
                content=text_content,
                page_number=page_number,
                bounding_box=bbox,
            )
        )

    # Sort elements by vertical position on page (top-to-bottom reading order)
    elements.sort(key=lambda el: el.bounding_box.y_min if el.bounding_box else 0.0)

    return elements


def normalize_parsed_document(
    parsed_doc: ParsedDocument,
    document_id: str | None = None,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    chunk_overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
    strip_headers_footers: bool = True,
) -> NormalizedDocument:
    """Normalizes a ParsedDocument into a DocumentNode and layout-aware ChunkNode records.

    Extraction hierarchy:
    1. Document AI Layout Parser document_layout.blocks (headings, paragraphs, lists, tables).
    2. Page-level extraction: page.tables + (page.paragraphs -> page.blocks -> page.lines).
    3. Global document text fallback: split doc.text into paragraphs if structure is absent.

    Invariants:
    - Stable, deterministic DocumentId and ChunkId.
    - Preserves page numbers and layout modalities ('TEXT' vs 'TABLE').
    - Leaves embedding_768 = None strictly (zero dummy embeddings).
    - Safely skips empty pages; splits oversized blocks.
    - Emits structured diagnostic statistics.
    - Raises EmptyDocumentExtractionError if extraction yields 0 chunks.
    """
    meta = parsed_doc.metadata
    doc_id = document_id or generate_document_id(meta, fallback_name=parsed_doc.processor_name)

    # Determine file name and GCS URI
    file_name = "document.pdf"
    gcs_uri = None
    if meta:
        file_name = meta.name.split("/")[-1] if meta.name else "document.pdf"
        gcs_uri = meta.gcs_uri

    doc_node = DocumentNode(
        document_id=doc_id,
        file_name=file_name,
        mime_type="application/pdf",
        source_url=None,
        gcs_uri=gcs_uri,
        status="PROCESSED",
        repo_id=None,
        drive_file_id=None,
    )

    doc = parsed_doc.raw_document
    doc_text = getattr(doc, "text", "") or parsed_doc.text or ""
    pages = getattr(doc, "pages", []) or []
    layout_blocks_count = 0

    all_elements: list[LayoutElement] = []

    # Strategy 1: Document AI Layout Parser document_layout.blocks
    doc_layout = getattr(doc, "document_layout", None)
    if doc_layout and hasattr(doc_layout, "blocks") and len(doc_layout.blocks) > 0:
        layout_blocks_count = len(doc_layout.blocks)
        layout_elements = extract_elements_from_document_layout(
            doc_layout,
            strip_headers_footers=strip_headers_footers,
        )
        if layout_elements:
            all_elements.extend(layout_elements)

    # Strategy 2: Page-level extraction (paragraphs -> blocks -> lines + tables)
    if not all_elements and pages:
        for page in pages:
            page_elements = extract_page_elements(
                doc_text,
                page,
                strip_headers_footers=strip_headers_footers,
            )
            all_elements.extend(page_elements)

    # Strategy 3: Global document text fallback
    if not all_elements and doc_text.strip():
        logger.warning(
            f"No layout elements or page paragraphs found for document '{doc_id}'; "
            f"falling back to splitting raw document text ({len(doc_text)} chars)."
        )
        for para in doc_text.split("\n\n"):
            clean_para = para.strip()
            if clean_para:
                all_elements.append(
                    LayoutElement(
                        element_type="TEXT",
                        content=clean_para,
                        page_number=1,
                        bounding_box=None,
                    )
                )

    element_text_count = sum(1 for el in all_elements if el.element_type == "TEXT")
    element_table_count = sum(1 for el in all_elements if el.element_type == "TABLE")

    # Group elements by page
    elements_by_page: dict[int, list[LayoutElement]] = {}
    for el in all_elements:
        elements_by_page.setdefault(el.page_number, []).append(el)

    detected_page_count = parsed_doc.page_count
    if not detected_page_count or detected_page_count == 0:
        detected_page_count = len(pages) if pages else (max(elements_by_page.keys()) if elements_by_page else 1)

    # Order of pages to process
    if elements_by_page:
        sorted_page_nums = sorted(elements_by_page.keys())
    elif pages:
        sorted_page_nums = [int(getattr(p, "page_number", i + 1) or (i + 1)) for i, p in enumerate(pages)]
    else:
        sorted_page_nums = [1]

    chunks: list[ChunkNode] = []
    chunk_counter = 1
    table_count = 0
    total_chars = 0

    for p_num in sorted_page_nums:
        page_elements = elements_by_page.get(p_num, [])
        # Sort elements by vertical position on page (top-to-bottom reading order)
        page_elements.sort(key=lambda el: el.bounding_box.y_min if el.bounding_box else 0.0)

        current_text_buf: list[str] = []
        current_text_len = 0

        for el in page_elements:
            if el.element_type == "TABLE":
                # Flush pending text buffer before table
                if current_text_buf:
                    merged_text = "\n\n".join(current_text_buf)
                    sub_chunks = split_oversized_text(merged_text, max_chunk_chars, chunk_overlap_chars)
                    for sc in sub_chunks:
                        cid = generate_chunk_id(doc_id, p_num, chunk_counter, modality="TEXT")
                        chunks.append(
                            ChunkNode(
                                chunk_id=cid,
                                document_id=doc_id,
                                file_id=None,
                                modality="TEXT",
                                text_content=sc,
                                page_number=p_num,
                                slide_number=None,
                                commit_sha=None,
                                line_range=None,
                                source_gcs_uri=gcs_uri,
                                embedding_768=None,
                            )
                        )
                        chunk_counter += 1
                        total_chars += len(sc)
                    current_text_buf = []
                    current_text_len = 0

                # Add table as an independent chunk
                table_count += 1
                table_chunks = split_oversized_text(el.content, max_chunk_chars, chunk_overlap_chars)
                for tc in table_chunks:
                    cid = generate_chunk_id(doc_id, p_num, chunk_counter, modality="TABLE")
                    chunks.append(
                        ChunkNode(
                            chunk_id=cid,
                            document_id=doc_id,
                            file_id=None,
                            modality="TABLE",
                            text_content=tc,
                            page_number=p_num,
                            slide_number=None,
                            commit_sha=None,
                            line_range=None,
                            source_gcs_uri=gcs_uri,
                            embedding_768=None,
                        )
                    )
                    chunk_counter += 1
                    total_chars += len(tc)

            elif el.element_type == "TEXT":
                el_len = len(el.content)
                if current_text_len + el_len + 2 <= max_chunk_chars:
                    current_text_buf.append(el.content)
                    current_text_len += el_len + 2
                else:
                    if current_text_buf:
                        merged_text = "\n\n".join(current_text_buf)
                        sub_chunks = split_oversized_text(merged_text, max_chunk_chars, chunk_overlap_chars)
                        for sc in sub_chunks:
                            cid = generate_chunk_id(doc_id, p_num, chunk_counter, modality="TEXT")
                            chunks.append(
                                ChunkNode(
                                    chunk_id=cid,
                                    document_id=doc_id,
                                    file_id=None,
                                    modality="TEXT",
                                    text_content=sc,
                                    page_number=p_num,
                                    slide_number=None,
                                    commit_sha=None,
                                    line_range=None,
                                    source_gcs_uri=gcs_uri,
                                    embedding_768=None,
                                )
                            )
                            chunk_counter += 1
                            total_chars += len(sc)

                    current_text_buf = [el.content]
                    current_text_len = el_len

        # Flush any remaining text at end of page
        if current_text_buf:
            merged_text = "\n\n".join(current_text_buf)
            sub_chunks = split_oversized_text(merged_text, max_chunk_chars, chunk_overlap_chars)
            for sc in sub_chunks:
                cid = generate_chunk_id(doc_id, p_num, chunk_counter, modality="TEXT")
                chunks.append(
                    ChunkNode(
                        chunk_id=cid,
                        document_id=doc_id,
                        file_id=None,
                        modality="TEXT",
                        text_content=sc,
                        page_number=p_num,
                        slide_number=None,
                        commit_sha=None,
                        line_range=None,
                        source_gcs_uri=gcs_uri,
                        embedding_768=None,
                    )
                )
                chunk_counter += 1
                total_chars += len(sc)

    logger.info(
        "Document parsing diagnostics: doc_id='%s', pages=%d, raw_text_len=%d, "
        "layout_blocks=%d, text_elements=%d, table_elements=%d, normalized_chunks=%d, total_chunk_chars=%d",
        doc_id,
        detected_page_count,
        len(doc_text),
        layout_blocks_count,
        element_text_count,
        element_table_count,
        len(chunks),
        total_chars,
    )

    if len(chunks) == 0:
        raise EmptyDocumentExtractionError(
            f"Document AI layout extraction yielded 0 chunks for document '{doc_id}' "
            f"(pages={detected_page_count}, raw_text_len={len(doc_text)}). "
            f"Extraction cannot produce empty document records."
        )

    return NormalizedDocument(
        document_node=doc_node,
        chunks=chunks,
        page_count=detected_page_count,
        table_count=table_count,
        total_characters=total_chars,
    )
