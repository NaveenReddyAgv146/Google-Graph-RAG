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


def extract_bounding_box(layout: Any) -> BoundingBox | None:
    """Extracts normalized bounding box from a layout's bounding_poly if available."""
    if not layout or not hasattr(layout, "bounding_poly"):
        return None

    poly = layout.bounding_poly
    vertices = getattr(poly, "normalized_vertices", None)
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
    """
    Generates a deterministic ChunkId guaranteed <= 64 characters.
    Example: 'doc_1234567890abcdef_p01_t001'
    """
    doc_prefix = document_id[:24] if document_id.startswith("doc_") else f"doc_{document_id[:20]}"
    mod_tag = "b" if modality.upper() == "TABLE" else "t"
    chunk_id = f"{doc_prefix}_p{page_number:02d}_{mod_tag}{chunk_index:03d}"
    return chunk_id[:64]


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
    """Extracts ordered layout elements (text paragraphs and tables) for a single page."""
    page_number = int(getattr(page, "page_number", 1) or 1)
    elements: list[LayoutElement] = []

    # 1. Extract tables
    tables = getattr(page, "tables", []) or []
    table_anchors = set()

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

    # 2. Extract paragraphs / blocks
    paragraphs = getattr(page, "paragraphs", []) or []
    for para in paragraphs:
        layout = getattr(para, "layout", None)
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
    """
    Normalizes a ParsedDocument into a DocumentNode and layout-aware ChunkNode records.

    Invariants:
    - Stable, deterministic DocumentId and ChunkId.
    - Preserves page numbers and layout modalities ('TEXT' vs 'TABLE').
    - Leaves embedding_768 = None strictly (zero dummy embeddings).
    - Safely skips empty pages; splits oversized blocks.
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

    chunks: list[ChunkNode] = []
    chunk_counter = 1
    table_count = 0
    total_chars = 0

    doc = parsed_doc.raw_document
    pages = getattr(doc, "pages", []) or []
    doc_text = getattr(doc, "text", "") or ""

    for page in pages:
        p_num = int(getattr(page, "page_number", 1) or 1)
        elements = extract_page_elements(
            doc_text,
            page,
            strip_headers_footers=strip_headers_footers,
        )

        current_text_buf: list[str] = []
        current_text_len = 0

        for el in elements:
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

    return NormalizedDocument(
        document_node=doc_node,
        chunks=chunks,
        page_count=len(pages),
        table_count=table_count,
        total_characters=total_chars,
    )
