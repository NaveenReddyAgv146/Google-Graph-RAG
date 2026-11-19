"""Google Slides content extraction, table reconstruction, and presentation normalization."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from common.models.schema import ChunkNode, DocumentNode, VisualAssetNode
from common.parsers.layout_extractor import (
    DEFAULT_CHUNK_OVERLAP_CHARS,
    DEFAULT_MAX_CHUNK_CHARS,
    split_oversized_text,
)

logger = logging.getLogger("common.parsers.slides_extractor")


class EmptyPresentationExtractionError(ValueError):
    """Raised when presentation extraction yields zero text or table chunks."""

    pass


class InvalidPresentationDataError(ValueError):
    """Raised when presentation data is malformed or invalid."""

    pass


@dataclass(frozen=True)
class ExtractedImageMetadata:
    """Metadata for an embedded image in a slide presentation."""

    object_id: str
    slide_number: int
    content_url: str | None = None
    source_url: str | None = None
    width_pt: float | None = None
    height_pt: float | None = None
    description: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class SlideElement:
    """Extracted layout element from a Google Slides slide."""

    element_type: str  # "TITLE", "TEXT", "TABLE", "NOTES", "IMAGE"
    content: str
    slide_number: int
    object_id: str
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedPresentation:
    """Normalized presentation representation containing DocumentNode, ChunkNodes, and VisualAssetNodes."""

    document_node: DocumentNode
    chunks: list[ChunkNode]
    visual_assets: list[VisualAssetNode]
    slide_count: int
    table_count: int
    image_count: int
    total_characters: int
    extracted_images: list[ExtractedImageMetadata] = field(default_factory=list)


def generate_slide_document_id(
    presentation_id: str = "",
    title: str = "",
    fallback: str = "",
) -> str:
    """Generates a deterministic DocumentId for a Google Slides presentation.

    Guaranteed <= 256 chars (and typically <= 54 chars).
    Format: 'doc_slide_{presentation_id}' (or hash if presentation_id is missing or oversized).
    Example: 'doc_slide_10KaQ12JImPARhyWvZP7dAZLMqW62bke8'
    """
    clean_id = (presentation_id or "").strip()
    if clean_id:
        if len(clean_id) <= 50:
            return f"doc_slide_{clean_id}"
        h = hashlib.sha256(clean_id.encode("utf-8")).hexdigest()[:32]
        return f"doc_slide_{h}"

    key = (title or "").strip() or (fallback or "").strip() or "presentation"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"doc_slide_{h}"


def generate_slide_chunk_id(
    document_id: str,
    slide_number: int,
    chunk_index: int,
    modality: str = "TEXT",
) -> str:
    """Generates a deterministic ChunkId guaranteed <= 64 characters.

    Format: '{doc_prefix}_s{slide_number:02d}_{mod_tag}{chunk_index:03d}'
    Example: 'doc_slide_10KaQ12JImPARhyWvZP7_s01_t001'
    """
    mod = modality.upper()
    if mod == "TABLE":
        mod_tag = "b"
    elif mod == "NOTES":
        mod_tag = "n"
    elif mod == "IMAGE":
        mod_tag = "i"
    else:
        mod_tag = "t"

    suffix = f"_s{slide_number:02d}_{mod_tag}{chunk_index:03d}"
    max_prefix_len = 64 - len(suffix)

    clean_doc = document_id if document_id.startswith("doc_") else f"doc_{document_id}"
    doc_prefix = clean_doc[:max_prefix_len]
    chunk_id = f"{doc_prefix}{suffix}"
    assert len(chunk_id) <= 64, f"ChunkId exceeds 64 chars: {chunk_id}"
    return chunk_id


def generate_slide_visual_id(
    document_id: str,
    slide_number: int,
    asset_index: int = 0,
) -> str:
    """Generates a deterministic VisualId guaranteed <= 64 characters.

    Format: 'vis_{doc_prefix}_s{slide_number:02d}_v{asset_index:03d}'
    Example: 'vis_doc_slide_10KaQ12JImPARhyW_s01_v001'
    """
    suffix = f"_s{slide_number:02d}_v{asset_index:03d}"
    # "vis_" (4 chars) + suffix (9+ chars)
    max_prefix_len = 64 - 4 - len(suffix)

    clean_doc = document_id if document_id.startswith("doc_") else f"doc_{document_id}"
    doc_prefix = clean_doc[:max_prefix_len]
    visual_id = f"vis_{doc_prefix}{suffix}"
    assert len(visual_id) <= 64, f"VisualId exceeds 64 chars: {visual_id}"
    return visual_id


def flatten_page_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively flattens page elements, unpacking elementGroup objects in order."""
    flattened: list[dict[str, Any]] = []
    if not elements:
        return flattened

    for elem in elements:
        if not isinstance(elem, dict):
            continue
        group = elem.get("elementGroup")
        if group and isinstance(group, dict):
            children = group.get("children", [])
            flattened.extend(flatten_page_elements(children))
        else:
            flattened.append(elem)
    return flattened


def extract_plain_text_from_text_content(text_dict: dict[str, Any]) -> str:
    """Extracts plain unformatted text from a Google Slides TextContent structure."""
    if not text_dict or not isinstance(text_dict, dict):
        return ""
    runs: list[str] = []
    for elem in text_dict.get("textElements", []) or []:
        if not isinstance(elem, dict):
            continue
        tr = elem.get("textRun")
        at = elem.get("autoText")
        if tr and isinstance(tr, dict):
            runs.append(tr.get("content", ""))
        elif at and isinstance(at, dict):
            runs.append(at.get("content", ""))
    return "".join(runs).strip()


def extract_text_from_text_content(text_dict: dict[str, Any]) -> str:
    """Extracts plain or markdown-formatted text from a Google Slides TextContent structure.

    Preserves paragraphs, bullet lists, nesting levels, and element ordering.
    Handles paragraphMarkers with or without startIndex/endIndex offsets.
    """
    if not text_dict or not isinstance(text_dict, dict):
        return ""

    text_elements = text_dict.get("textElements", [])
    if not text_elements:
        return ""

    # Pass 1: Build range-based bullet index if indices are present
    range_bullets: list[tuple[int, int, dict[str, Any]]] = []
    for elem in text_elements:
        if not isinstance(elem, dict):
            continue
        pm = elem.get("paragraphMarker")
        if pm and isinstance(pm, dict):
            bullet = pm.get("bullet")
            if bullet and "startIndex" in elem and "endIndex" in elem:
                try:
                    s = int(elem["startIndex"])
                    e = int(elem["endIndex"])
                    range_bullets.append((s, e, bullet))
                except (ValueError, TypeError):
                    pass

    def _find_bullet_for_range(start: int, end: int) -> dict[str, Any] | None:
        for s, e, b in range_bullets:
            if s <= start and end <= e:
                return b
            if s <= start < e or s < end <= e:
                return b
        return None

    # Pass 2: Extract paragraphs and their text
    paragraphs: list[dict[str, Any]] = []
    current_runs: list[str] = []
    current_bullet: dict[str, Any] | None = None
    current_start = 0

    for elem in text_elements:
        if not isinstance(elem, dict):
            continue

        elem_start = int(elem.get("startIndex", 0) or 0)
        elem_end = int(elem.get("endIndex", 0) or 0)

        # Check for paragraphMarker
        pm = elem.get("paragraphMarker")
        if pm and isinstance(pm, dict):
            bullet = pm.get("bullet")
            if bullet:
                current_bullet = bullet

        # Check for textRun or autoText
        tr = elem.get("textRun")
        at = elem.get("autoText")
        content = ""
        if tr and isinstance(tr, dict):
            content = tr.get("content", "")
        elif at and isinstance(at, dict):
            content = at.get("content", "")

        if content:
            parts = content.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    # Newline reached: close current paragraph
                    full_text = "".join(current_runs)
                    para_bullet = (
                        _find_bullet_for_range(current_start, elem_start)
                        if range_bullets
                        else current_bullet
                    )
                    if para_bullet is None:
                        para_bullet = current_bullet

                    paragraphs.append({
                        "text": full_text,
                        "bullet": para_bullet,
                    })
                    current_runs = []
                    current_bullet = None
                    current_start = elem_start

                if part:
                    current_runs.append(part)

    if current_runs:
        full_text = "".join(current_runs)
        para_bullet = (
            _find_bullet_for_range(current_start, current_start + len(full_text))
            if range_bullets
            else current_bullet
        )
        if para_bullet is None:
            para_bullet = current_bullet
        paragraphs.append({
            "text": full_text,
            "bullet": para_bullet,
        })

    # Format final lines
    formatted_lines: list[str] = []
    for p in paragraphs:
        raw_text = p["text"].strip()
        if not raw_text:
            continue

        bullet = p.get("bullet")
        if bullet and isinstance(bullet, dict):
            nesting = int(bullet.get("nestingLevel", 0) or 0)
            indent = "  " * nesting
            formatted_lines.append(f"{indent}- {raw_text}")
        else:
            formatted_lines.append(raw_text)

    return "\n".join(formatted_lines)


def convert_slides_table_to_markdown(table: dict[str, Any]) -> str:
    """Converts a Google Slides table dictionary into a readable, formatted Markdown table."""
    if not table or not isinstance(table, dict):
        return ""

    table_rows = table.get("tableRows", [])
    if not table_rows:
        return ""

    extracted_rows: list[list[str]] = []
    for row in table_rows:
        if not isinstance(row, dict):
            continue
        row_cells: list[str] = []
        cells = row.get("tableCells", []) or []
        for cell in cells:
            if not isinstance(cell, dict):
                row_cells.append("")
                continue
            text_obj = cell.get("text")
            if text_obj:
                raw_text = extract_plain_text_from_text_content(text_obj)
                clean_cell = " ".join(raw_text.replace("|", "\\|").split())
            else:
                clean_cell = ""
            row_cells.append(clean_cell)
        extracted_rows.append(row_cells)

    if not extracted_rows:
        return ""

    num_cols = max(len(r) for r in extracted_rows)
    if num_cols == 0:
        return ""

    headers = list(extracted_rows[0])
    body_rows = [list(r) for r in extracted_rows[1:]]

    # Pad headers to num_cols
    while len(headers) < num_cols:
        headers.append(f"Column {len(headers) + 1}")

    # If all headers are empty, synthesize header names
    if not any(h.strip() for h in headers):
        headers = [f"Column {i + 1}" for i in range(num_cols)]

    lines = [
        "| " + " | ".join(h or f"Column {i + 1}" for i, h in enumerate(headers)) + " |",
        "| " + " | ".join(["---"] * num_cols) + " |",
    ]

    for row in body_rows:
        padded_row = list(row)
        while len(padded_row) < num_cols:
            padded_row.append("")
        lines.append("| " + " | ".join(padded_row) + " |")

    return "\n".join(lines)


def extract_slide_title(slide: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extracts the slide title and the objectId of the title shape.

    Returns (title_text, title_object_id).
    """
    page_elements = flatten_page_elements(slide.get("pageElements", []))

    # 1. Search for explicit TITLE or CENTERED_TITLE placeholder
    for elem in page_elements:
        shape = elem.get("shape")
        if not shape or not isinstance(shape, dict):
            continue
        ph = shape.get("placeholder")
        if ph and isinstance(ph, dict):
            ph_type = (ph.get("type") or "").upper()
            if ph_type in ("TITLE", "CENTERED_TITLE"):
                text = extract_plain_text_from_text_content(shape.get("text", {}))
                if text:
                    return text, elem.get("objectId")

    # 2. Fallback: check placeholder containing 'TITLE'
    for elem in page_elements:
        shape = elem.get("shape")
        if not shape or not isinstance(shape, dict):
            continue
        ph = shape.get("placeholder")
        if ph and isinstance(ph, dict):
            ph_type = (ph.get("type") or "").upper()
            if "TITLE" in ph_type:
                text = extract_plain_text_from_text_content(shape.get("text", {}))
                if text:
                    return text, elem.get("objectId")

    return None, None


def extract_speaker_notes_from_slide(slide: dict[str, Any]) -> str:
    """Extracts speaker notes text from slide.slideProperties.notesPage."""
    slide_props = slide.get("slideProperties")
    if not slide_props or not isinstance(slide_props, dict):
        return ""

    notes_page = slide_props.get("notesPage")
    if not notes_page or not isinstance(notes_page, dict):
        return ""

    page_elements = notes_page.get("pageElements", [])
    if not page_elements:
        return ""

    body_shapes: list[dict[str, Any]] = []
    other_shapes: list[dict[str, Any]] = []

    for elem in page_elements:
        if not isinstance(elem, dict):
            continue
        shape = elem.get("shape")
        if not shape or not isinstance(shape, dict):
            continue
        ph = shape.get("placeholder")
        if ph and isinstance(ph, dict):
            ph_type = (ph.get("type") or "").upper()
            if ph_type == "BODY":
                body_shapes.append(shape)
            elif ph_type in ("SLIDE_IMAGE", "PAGE_NUMBER"):
                continue
            else:
                other_shapes.append(shape)
        else:
            other_shapes.append(shape)

    target_shapes = body_shapes if body_shapes else other_shapes
    notes_parts: list[str] = []
    for s in target_shapes:
        text_content = s.get("text")
        if text_content and isinstance(text_content, dict):
            txt = extract_text_from_text_content(text_content).strip()
            if txt:
                notes_parts.append(txt)

    return "\n\n".join(notes_parts)


def extract_presentation_elements(
    presentation: dict[str, Any],
) -> list[SlideElement]:
    """Extracts ordered SlideElement objects from a raw presentation response."""
    if not presentation or not isinstance(presentation, dict):
        raise InvalidPresentationDataError("Presentation data must be a non-empty dictionary.")

    slides = presentation.get("slides")
    if slides is None or not isinstance(slides, list):
        raise EmptyPresentationExtractionError("Presentation contains no slides list.")

    elements: list[SlideElement] = []

    for slide_idx, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        slide_number = slide_idx
        slide_title, title_obj_id = extract_slide_title(slide)

        page_elements = flatten_page_elements(slide.get("pageElements", []))
        for elem in page_elements:
            elem_id = elem.get("objectId", "")

            # 1. Shape
            shape = elem.get("shape")
            if shape and isinstance(shape, dict):
                text_obj = shape.get("text")
                if text_obj:
                    if elem_id == title_obj_id:
                        title_text = extract_plain_text_from_text_content(text_obj)
                        if title_text:
                            elements.append(
                                SlideElement(
                                    element_type="TITLE",
                                    content=f"## {title_text}",
                                    slide_number=slide_number,
                                    object_id=elem_id,
                                    title=title_text,
                                    metadata={"placeholder_type": "TITLE"},
                                )
                            )
                    else:
                        shape_text = extract_text_from_text_content(text_obj)
                        if shape_text.strip():
                            elements.append(
                                SlideElement(
                                    element_type="TEXT",
                                    content=shape_text.strip(),
                                    slide_number=slide_number,
                                    object_id=elem_id,
                                    title=slide_title,
                                )
                            )

            # 2. Table
            table = elem.get("table")
            if table and isinstance(table, dict):
                md_table = convert_slides_table_to_markdown(table)
                if md_table.strip():
                    elements.append(
                        SlideElement(
                            element_type="TABLE",
                            content=md_table.strip(),
                            slide_number=slide_number,
                            object_id=elem_id,
                            title=slide_title,
                        )
                    )

            # 3. Image
            img = elem.get("image")
            if img and isinstance(img, dict):
                size = elem.get("size", {}) or {}
                w = size.get("width", {}).get("magnitude") if isinstance(size.get("width"), dict) else None
                h = size.get("height", {}).get("magnitude") if isinstance(size.get("height"), dict) else None
                elements.append(
                    SlideElement(
                        element_type="IMAGE",
                        content="",
                        slide_number=slide_number,
                        object_id=elem_id,
                        title=elem.get("title") or slide_title,
                        metadata={
                            "content_url": img.get("contentUrl"),
                            "source_url": img.get("sourceUrl"),
                            "width_pt": w,
                            "height_pt": h,
                            "description": elem.get("description"),
                        },
                    )
                )

        # 4. Speaker Notes
        notes = extract_speaker_notes_from_slide(slide)
        if notes.strip():
            elements.append(
                SlideElement(
                    element_type="NOTES",
                    content=notes.strip(),
                    slide_number=slide_number,
                    object_id=f"notes_{slide.get('objectId', slide_number)}",
                    title=slide_title,
                )
            )

    return elements


def normalize_presentation(
    presentation: dict[str, Any],
    presentation_id: str | None = None,
    source_url: str | None = None,
    gcs_uri: str | None = None,
    gcs_asset_prefix: str = "gs://sales-discovery-mvp-mumbai/extracted-images",
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    chunk_overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> NormalizedPresentation:
    """Parses raw presentation response and normalizes into DocumentNode, ChunkNodes, and VisualAssetNodes."""
    if not presentation or not isinstance(presentation, dict):
        raise InvalidPresentationDataError("Presentation data must be a non-empty dictionary.")

    pres_id = presentation_id or presentation.get("presentationId", "")
    title = presentation.get("title", "")
    slides = presentation.get("slides")

    if slides is None or not isinstance(slides, list):
        raise EmptyPresentationExtractionError(f"Presentation '{pres_id}' contains no slides.")

    if len(slides) == 0:
        raise EmptyPresentationExtractionError(f"Presentation '{pres_id}' has 0 slides.")

    doc_id = generate_slide_document_id(presentation_id=pres_id, title=title)
    file_name = f"{title}.gslides" if title and not title.endswith(".gslides") else (title or f"{doc_id}.gslides")
    clean_src_url = source_url or (f"https://docs.google.com/presentation/d/{pres_id}/edit" if pres_id else None)

    doc_node = DocumentNode(
        document_id=doc_id,
        file_name=file_name,
        mime_type="application/vnd.google-apps.presentation",
        source_url=clean_src_url,
        gcs_uri=gcs_uri,
        status="ACTIVE",
        repo_id=None,
        drive_file_id=pres_id or None,
    )

    chunks: list[ChunkNode] = []
    visual_assets: list[VisualAssetNode] = []
    extracted_images: list[ExtractedImageMetadata] = []

    chunk_counter = 1
    total_chars = 0
    table_count = 0
    image_count = 0

    for slide_idx, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        slide_number = slide_idx
        slide_title, title_obj_id = extract_slide_title(slide)

        # 1. Slide Render Visual Asset
        render_vid = generate_slide_visual_id(doc_id, slide_number, asset_index=0)
        render_uri = f"{gcs_asset_prefix}/{doc_id}/slide_{slide_number:02d}_render.png"
        render_desc = f"Slide {slide_number}: {slide_title}" if slide_title else f"Slide {slide_number} render"
        visual_assets.append(
            VisualAssetNode(
                visual_id=render_vid,
                document_id=doc_id,
                page_number=None,
                slide_number=slide_number,
                image_gcs_uri=render_uri,
                description=render_desc,
                asset_type="SLIDE",
            )
        )

        page_elements = flatten_page_elements(slide.get("pageElements", []))
        current_text_buf: list[str] = []
        current_text_len = 0
        asset_counter = 1

        def _flush_text_buffer() -> None:
            nonlocal chunk_counter, total_chars, current_text_buf, current_text_len
            if not current_text_buf:
                return
            merged_text = "\n\n".join(current_text_buf)
            sub_chunks = split_oversized_text(merged_text, max_chunk_chars, chunk_overlap_chars)
            for sc in sub_chunks:
                cid = generate_slide_chunk_id(doc_id, slide_number, chunk_counter, modality="TEXT")
                chunks.append(
                    ChunkNode(
                        chunk_id=cid,
                        document_id=doc_id,
                        file_id=None,
                        modality="TEXT",
                        text_content=sc,
                        page_number=None,
                        slide_number=slide_number,
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

        for elem in page_elements:
            elem_id = elem.get("objectId", "")

            # Shape
            shape = elem.get("shape")
            if shape and isinstance(shape, dict):
                text_obj = shape.get("text")
                if text_obj:
                    if elem_id == title_obj_id:
                        title_text = extract_plain_text_from_text_content(text_obj)
                        if title_text:
                            formatted_title = f"## {title_text}"
                            if current_text_len + len(formatted_title) + 2 <= max_chunk_chars:
                                current_text_buf.append(formatted_title)
                                current_text_len += len(formatted_title) + 2
                            else:
                                _flush_text_buffer()
                                current_text_buf = [formatted_title]
                                current_text_len = len(formatted_title)
                    else:
                        shape_text = extract_text_from_text_content(text_obj)
                        if shape_text.strip():
                            text_len = len(shape_text)
                            if current_text_len + text_len + 2 <= max_chunk_chars:
                                current_text_buf.append(shape_text.strip())
                                current_text_len += text_len + 2
                            else:
                                _flush_text_buffer()
                                current_text_buf = [shape_text.strip()]
                                current_text_len = text_len

            # Table
            table = elem.get("table")
            if table and isinstance(table, dict):
                _flush_text_buffer()
                md_table = convert_slides_table_to_markdown(table)
                if md_table.strip():
                    table_count += 1
                    table_chunks = split_oversized_text(md_table.strip(), max_chunk_chars, chunk_overlap_chars)
                    for tc in table_chunks:
                        cid = generate_slide_chunk_id(doc_id, slide_number, chunk_counter, modality="TABLE")
                        chunks.append(
                            ChunkNode(
                                chunk_id=cid,
                                document_id=doc_id,
                                file_id=None,
                                modality="TABLE",
                                text_content=tc,
                                page_number=None,
                                slide_number=slide_number,
                                commit_sha=None,
                                line_range=None,
                                source_gcs_uri=gcs_uri,
                                embedding_768=None,
                            )
                        )
                        chunk_counter += 1
                        total_chars += len(tc)

            # Embedded Image
            img = elem.get("image")
            if img and isinstance(img, dict):
                image_count += 1
                size = elem.get("size", {}) or {}
                w = size.get("width", {}).get("magnitude") if isinstance(size.get("width"), dict) else None
                h = size.get("height", {}).get("magnitude") if isinstance(size.get("height"), dict) else None
                img_id = elem_id or f"img_s{slide_number}_{asset_counter}"
                img_meta = ExtractedImageMetadata(
                    object_id=img_id,
                    slide_number=slide_number,
                    content_url=img.get("contentUrl"),
                    source_url=img.get("sourceUrl"),
                    width_pt=w,
                    height_pt=h,
                    description=elem.get("description"),
                    title=elem.get("title"),
                )
                extracted_images.append(img_meta)

                img_vid = generate_slide_visual_id(doc_id, slide_number, asset_index=asset_counter)
                img_uri = f"{gcs_asset_prefix}/{doc_id}/slide_{slide_number:02d}_{img_id}.png"
                img_desc = img_meta.description or img_meta.title or f"Embedded image on slide {slide_number}"
                visual_assets.append(
                    VisualAssetNode(
                        visual_id=img_vid,
                        document_id=doc_id,
                        page_number=None,
                        slide_number=slide_number,
                        image_gcs_uri=img_uri,
                        description=img_desc,
                        asset_type="IMAGE",
                    )
                )
                asset_counter += 1

        # Flush any remaining text elements at the end of the slide
        _flush_text_buffer()

        # Speaker Notes
        notes = extract_speaker_notes_from_slide(slide)
        if notes.strip():
            notes_content = f"Speaker Notes:\n{notes.strip()}"
            sub_notes = split_oversized_text(notes_content, max_chunk_chars, chunk_overlap_chars)
            for sn in sub_notes:
                cid = generate_slide_chunk_id(doc_id, slide_number, chunk_counter, modality="TEXT")
                chunks.append(
                    ChunkNode(
                        chunk_id=cid,
                        document_id=doc_id,
                        file_id=None,
                        modality="TEXT",
                        text_content=sn,
                        page_number=None,
                        slide_number=slide_number,
                        commit_sha=None,
                        line_range=None,
                        source_gcs_uri=gcs_uri,
                        embedding_768=None,
                    )
                )
                chunk_counter += 1
                total_chars += len(sn)

    logger.info(
        "Slides extraction diagnostics: doc_id='%s', slides=%d, chunks=%d, "
        "tables=%d, visual_assets=%d, images=%d, total_chars=%d",
        doc_id,
        len(slides),
        len(chunks),
        table_count,
        len(visual_assets),
        image_count,
        total_chars,
    )

    if len(chunks) == 0:
        raise EmptyPresentationExtractionError(
            f"Google Slides extraction yielded 0 chunks for presentation '{doc_id}' "
            f"(slides={len(slides)}). Extraction cannot produce empty document records."
        )

    return NormalizedPresentation(
        document_node=doc_node,
        chunks=chunks,
        visual_assets=visual_assets,
        slide_count=len(slides),
        table_count=table_count,
        image_count=image_count,
        total_characters=total_chars,
        extracted_images=extracted_images,
    )
