"""Comprehensive unit tests for Document AI layout extraction and chunk normalization."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from common.parsers.documentai_client import ParsedDocument
from common.parsers.layout_extractor import (
    BoundingBox,
    EmptyDocumentExtractionError,
    convert_layout_table_to_markdown,
    convert_table_to_markdown,
    extract_bounding_box,
    extract_elements_from_document_layout,
    extract_page_elements,
    extract_text_from_anchor,
    generate_chunk_id,
    generate_document_id,
    normalize_parsed_document,
    split_oversized_text,
)
from common.storage import PdfMetadata


class MockSegment:
    def __init__(self, start_index: int, end_index: int):
        self.start_index = start_index
        self.end_index = end_index


class MockTextAnchor:
    def __init__(self, segments: list[tuple[int, int]]):
        self.text_segments = [MockSegment(s, e) for s, e in segments]


class MockVertex:
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y


class MockBoundingPoly:
    def __init__(self, vertices: list[tuple[float, float]]):
        self.normalized_vertices = [MockVertex(x, y) for x, y in vertices]


class MockLayout:
    def __init__(self, segments: list[tuple[int, int]] | None = None, vertices: list[tuple[float, float]] | None = None):
        self.text_anchor = MockTextAnchor(segments) if segments else None
        self.bounding_poly = MockBoundingPoly(vertices) if vertices else None


class MockCell:
    def __init__(self, segments: list[tuple[int, int]]):
        self.layout = MockLayout(segments=segments)


class MockRow:
    def __init__(self, cell_segments: list[list[tuple[int, int]]]):
        self.cells = [MockCell(segs) for segs in cell_segments]


class MockTable:
    def __init__(self, header_rows: list[MockRow], body_rows: list[MockRow], vertices: list[tuple[float, float]] | None = None):
        self.header_rows = header_rows
        self.body_rows = body_rows
        self.layout = MockLayout(vertices=vertices)


class MockParagraph:
    def __init__(self, segments: list[tuple[int, int]], vertices: list[tuple[float, float]] | None = None):
        self.layout = MockLayout(segments=segments, vertices=vertices)


class MockPage:
    def __init__(
        self,
        page_number: int,
        paragraphs: list[MockParagraph] | None = None,
        tables: list[MockTable] | None = None,
        blocks: list[MockParagraph] | None = None,
        lines: list[MockParagraph] | None = None,
    ):
        self.page_number = page_number
        self.paragraphs = paragraphs or []
        self.tables = tables or []
        self.blocks = blocks or []
        self.lines = lines or []


class MockLayoutTextBlock:
    def __init__(self, text: str, type_: str = "paragraph", blocks: list | None = None):
        self.text = text
        self.type_ = type_
        self.blocks = blocks or []


class MockLayoutTableCell:
    def __init__(self, text: str):
        self.blocks = [MockLayoutBlock(text_block=MockLayoutTextBlock(text))]


class MockLayoutTableRow:
    def __init__(self, cell_texts: list[str]):
        self.cells = [MockLayoutTableCell(t) for t in cell_texts]


class MockLayoutTableBlock:
    def __init__(self, header_rows: list[list[str]], body_rows: list[list[str]]):
        self.header_rows = [MockLayoutTableRow(r) for r in header_rows]
        self.body_rows = [MockLayoutTableRow(r) for r in body_rows]


class MockListEntry:
    def __init__(self, text: str):
        self.blocks = [MockLayoutBlock(text_block=MockLayoutTextBlock(text))]


class MockLayoutListBlock:
    def __init__(self, entries: list[str], type_: str = "unordered"):
        self.list_entries = [MockListEntry(e) for e in entries]
        self.type_ = type_


class MockPageSpan:
    def __init__(self, page_start: int = 1, page_end: int = 1):
        self.page_start = page_start
        self.page_end = page_end


class MockLayoutBlock:
    def __init__(
        self,
        text_block: MockLayoutTextBlock | None = None,
        table_block: MockLayoutTableBlock | None = None,
        list_block: MockLayoutListBlock | None = None,
        page_start: int = 1,
        vertices: list[tuple[float, float]] | None = None,
    ):
        self.text_block = text_block
        self.table_block = table_block
        self.list_block = list_block
        self.page_span = MockPageSpan(page_start=page_start)
        self.bounding_box = MockBoundingPoly(vertices) if vertices else None


class MockDocumentLayout:
    def __init__(self, blocks: list[MockLayoutBlock]):
        self.blocks = blocks


class MockDocAiDoc:
    def __init__(
        self,
        text: str,
        pages: list[MockPage] | None = None,
        document_layout: MockDocumentLayout | None = None,
    ):
        self.text = text
        self.pages = pages or []
        self.document_layout = document_layout


# ============================================================================
# 1. Text & Anchor Extraction Tests
# ============================================================================


def test_extract_text_from_anchor():
    doc_text = "The quick brown fox jumps over the lazy dog."
    anchor = MockTextAnchor([(0, 3), (10, 19)])
    result = extract_text_from_anchor(doc_text, anchor)
    assert result == "Thebrown fox"

    # Edge cases
    assert extract_text_from_anchor(doc_text, None) == ""
    assert extract_text_from_anchor(doc_text, MockTextAnchor([])) == ""
    # Out of bounds indices clamped safely
    assert extract_text_from_anchor(doc_text, MockTextAnchor([(40, 100)])) == "dog."


def test_extract_bounding_box():
    layout = MockLayout(vertices=[(0.1, 0.2), (0.9, 0.2), (0.9, 0.8), (0.1, 0.8)])
    bbox = extract_bounding_box(layout)
    assert bbox is not None
    assert bbox.x_min == 0.1
    assert bbox.y_min == 0.2
    assert bbox.x_max == 0.9
    assert bbox.y_max == 0.8

    # Missing layout / poly
    assert extract_bounding_box(None) is None
    assert extract_bounding_box(MockLayout()) is None


# ============================================================================
# 2. Table Extraction & Markdown Reconstruction Tests
# ============================================================================


def test_convert_table_to_markdown_standard():
    doc_text = "ServiceRegionTierCloud Spannerasia-south1EnterpriseVertex AIus-central1Standard"
    header = MockRow([[(0, 7)], [(7, 13)], [(13, 17)]])  # Service, Region, Tier
    row1 = MockRow([[(17, 30)], [(30, 41)], [(41, 51)]])  # Cloud Spanner, asia-south1, Enterprise
    row2 = MockRow([[(51, 60)], [(60, 71)], [(71, 79)]])  # Vertex AI, us-central1, Standard

    table = MockTable(header_rows=[header], body_rows=[row1, row2])
    md = convert_table_to_markdown(doc_text, table)

    lines = md.split("\n")
    assert len(lines) == 4
    assert lines[0] == "| Service | Region | Tier |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| Cloud Spanner | asia-south1 | Enterprise |"
    assert lines[3] == "| Vertex AI | us-central1 | Standard |"


def test_convert_table_with_escaped_pipes():
    doc_text = "Input|OutputResult"
    row = MockRow([[(0, 12)], [(12, 18)]])
    table = MockTable(header_rows=[], body_rows=[row])

    md = convert_table_to_markdown(doc_text, table)
    assert "\\|" in md  # Pipe must be escaped


def test_convert_table_empty():
    assert convert_table_to_markdown("text", None) == ""
    assert convert_table_to_markdown("text", MockTable([], [])) == ""


# ============================================================================
# 3. Deterministic ID Generation Tests
# ============================================================================


def test_deterministic_document_id():
    meta = PdfMetadata(
        bucket="sales-discovery-mvp-mumbai",
        name="whitepapers/cloud_spanner_rag.pdf",
        gcs_uri="gs://sales-discovery-mvp-mumbai/whitepapers/cloud_spanner_rag.pdf",
        size_bytes=1024,
        generation=1,
        content_type="application/pdf",
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )

    doc_id1 = generate_document_id(meta)
    doc_id2 = generate_document_id(meta)

    assert doc_id1 == doc_id2
    assert doc_id1 == "doc_e3b0c44298fc1c149afbf4c8996fb924"
    assert len(doc_id1) <= 256


def test_deterministic_chunk_id():
    doc_id = "doc_e3b0c44298fc1c149afbf4c8996fb924"
    cid1 = generate_chunk_id(doc_id, page_number=1, chunk_index=1, modality="TEXT")
    cid2 = generate_chunk_id(doc_id, page_number=1, chunk_index=1, modality="TEXT")
    cid_table = generate_chunk_id(doc_id, page_number=1, chunk_index=2, modality="TABLE")

    assert cid1 == cid2
    # Verify full content-addressed doc_id is preserved without arbitrary truncation
    assert cid1 == "doc_e3b0c44298fc1c149afbf4c8996fb924_p01_t001"
    assert cid_table == "doc_e3b0c44298fc1c149afbf4c8996fb924_p01_b002"
    assert len(cid1) == 45
    assert len(cid1) <= 64
    assert len(cid_table) <= 64


def test_chunk_id_global_uniqueness_across_documents():
    # Two documents sharing the first 24 characters but differing afterwards
    doc_a = "doc_12345678901234567890abcdAAAA"
    doc_b = "doc_12345678901234567890abcdBBBB"

    cid_a = generate_chunk_id(doc_a, page_number=1, chunk_index=1, modality="TEXT")
    cid_b = generate_chunk_id(doc_b, page_number=1, chunk_index=1, modality="TEXT")

    assert cid_a != cid_b
    assert "AAAA" in cid_a
    assert "BBBB" in cid_b
    assert len(cid_a) <= 64
    assert len(cid_b) <= 64


def test_chunk_id_length_boundary_with_large_page_and_index():
    # Very long document ID and large numbers
    long_doc_id = "doc_" + "f" * 100
    cid = generate_chunk_id(long_doc_id, page_number=999, chunk_index=9999, modality="TABLE")
    assert len(cid) <= 64
    assert cid.endswith("_p999_b9999")


# ============================================================================
# 4. Text Splitting & Boundary Tests
# ============================================================================


def test_split_oversized_text():
    short_text = "This is a short paragraph."
    assert split_oversized_text(short_text, max_chars=100, overlap_chars=20) == [short_text]

    long_text = "Paragraph one with some text.\n\nParagraph two with more details.\n\nParagraph three."
    chunks = split_oversized_text(long_text, max_chars=40, overlap_chars=10)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 40


# ============================================================================
# 5. Normalization Tests
# ============================================================================


def test_normalize_single_page_document():
    doc_text = "Introduction to GraphRAG on Cloud Spanner.\nThis architecture enables scalable semantic search."
    p1 = MockParagraph([(0, 42)], vertices=[(0.1, 0.2), (0.9, 0.2), (0.9, 0.3), (0.1, 0.3)])
    p2 = MockParagraph([(43, 91)], vertices=[(0.1, 0.35), (0.9, 0.35), (0.9, 0.45), (0.1, 0.45)])
    page = MockPage(page_number=1, paragraphs=[p1, p2])
    raw_doc = MockDocAiDoc(text=doc_text, pages=[page])

    meta = PdfMetadata(
        bucket="sales-discovery-mvp-mumbai",
        name="overview.pdf",
        gcs_uri="gs://sales-discovery-mvp-mumbai/overview.pdf",
        size_bytes=512,
        generation=1,
        content_type="application/pdf",
        sha256_hash="aabbcc11223344556677889900aabbcc11223344556677889900aabbcc112233",
    )
    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=meta,
        processor_name="projects/123/locations/us/processors/abc",
        page_count=1,
        text=doc_text,
    )

    norm = normalize_parsed_document(parsed)
    assert norm.page_count == 1
    assert norm.table_count == 0
    assert norm.document_node.file_name == "overview.pdf"
    assert norm.document_node.mime_type == "application/pdf"
    assert norm.document_node.status == "PROCESSED"
    assert norm.document_node.gcs_uri == "gs://sales-discovery-mvp-mumbai/overview.pdf"

    assert len(norm.chunks) == 1
    chunk = norm.chunks[0]
    assert chunk.document_id == norm.document_node.document_id
    assert chunk.modality == "TEXT"
    assert chunk.page_number == 1
    assert chunk.slide_number is None
    assert chunk.embedding_768 is None  # STRICT INVARIANT
    assert "Introduction to GraphRAG" in chunk.text_content


def test_normalize_with_tables_and_multi_page():
    doc_text = "Page 1 HeaderPage 1 contentCol ACol BVal 1Val 2Page 2 Content"
    # Page 1
    p1 = MockParagraph([(13, 27)], vertices=[(0.1, 0.2), (0.9, 0.2), (0.9, 0.3), (0.1, 0.3)])
    tbl_h = MockRow([[(27, 32)], [(32, 37)]])
    tbl_b = MockRow([[(37, 42)], [(42, 47)]])
    tbl = MockTable([tbl_h], [tbl_b], vertices=[(0.1, 0.4), (0.9, 0.4), (0.9, 0.6), (0.1, 0.6)])
    page1 = MockPage(page_number=1, paragraphs=[p1], tables=[tbl])

    # Page 2
    p2 = MockParagraph([(47, 61)], vertices=[(0.1, 0.2), (0.9, 0.2), (0.9, 0.3), (0.1, 0.3)])
    page2 = MockPage(page_number=2, paragraphs=[p2])

    raw_doc = MockDocAiDoc(text=doc_text, pages=[page1, page2])
    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=2,
        text=doc_text,
    )

    norm = normalize_parsed_document(parsed)
    assert norm.page_count == 2
    assert norm.table_count == 1
    assert len(norm.chunks) == 3

    # Chunk 0: Page 1 Text
    assert norm.chunks[0].modality == "TEXT"
    assert norm.chunks[0].page_number == 1
    assert norm.chunks[0].embedding_768 is None

    # Chunk 1: Page 1 Table
    assert norm.chunks[1].modality == "TABLE"
    assert norm.chunks[1].page_number == 1
    assert "| Col A | Col B |" in norm.chunks[1].text_content
    assert norm.chunks[1].embedding_768 is None

    # Chunk 2: Page 2 Text
    assert norm.chunks[2].modality == "TEXT"
    assert norm.chunks[2].page_number == 2
    assert norm.chunks[2].embedding_768 is None


def test_empty_page_handling():
    doc_text = "Valid text on page 1"
    p1 = MockParagraph([(0, 20)], vertices=[(0.1, 0.2), (0.9, 0.2), (0.9, 0.3), (0.1, 0.3)])
    page1 = MockPage(page_number=1, paragraphs=[p1])
    page2 = MockPage(page_number=2, paragraphs=[], tables=[])  # Empty page
    page3 = MockPage(page_number=3, paragraphs=[p1])

    raw_doc = MockDocAiDoc(text=doc_text, pages=[page1, page2, page3])
    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=3,
        text=doc_text,
    )

    norm = normalize_parsed_document(parsed)
    assert norm.page_count == 3
    # Only pages 1 and 3 should yield chunks; page 2 is safely skipped
    assert len(norm.chunks) == 2
    assert norm.chunks[0].page_number == 1
    assert norm.chunks[1].page_number == 3


def test_repeated_headers_footers_stripping():
    doc_text = "REPEATED HEADER 2026Body textREPEATED FOOTER"
    # Header paragraph at y_max <= 0.05
    header = MockParagraph([(0, 20)], vertices=[(0.1, 0.01), (0.9, 0.01), (0.9, 0.04), (0.1, 0.04)])
    # Body paragraph
    body = MockParagraph([(20, 29)], vertices=[(0.1, 0.3), (0.9, 0.3), (0.9, 0.5), (0.1, 0.5)])
    # Footer paragraph at y_min >= 0.95
    footer = MockParagraph([(29, 44)], vertices=[(0.1, 0.96), (0.9, 0.96), (0.9, 0.99), (0.1, 0.99)])

    page = MockPage(page_number=1, paragraphs=[header, body, footer])
    raw_doc = MockDocAiDoc(text=doc_text, pages=[page])

    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=1,
        text=doc_text,
    )

    # Stripping enabled
    norm_stripped = normalize_parsed_document(parsed, strip_headers_footers=True)
    assert len(norm_stripped.chunks) == 1
    assert norm_stripped.chunks[0].text_content == "Body text"

    # Stripping disabled
    norm_kept = normalize_parsed_document(parsed, strip_headers_footers=False)
    assert len(norm_kept.chunks) == 1
    assert "REPEATED HEADER" in norm_kept.chunks[0].text_content
    assert "REPEATED FOOTER" in norm_kept.chunks[0].text_content


# ============================================================================
# 5. Document AI Layout Parser & Regression Tests
# ============================================================================


def test_document_layout_blocks_extraction():
    """Tests extracting structured content directly from document_layout.blocks hierarchy."""
    tb1 = MockLayoutTextBlock(text="Professional Summary", type_="heading-1")
    tb2 = MockLayoutTextBlock(text="Experienced Staff AI Engineer specializing in distributed RAG systems.", type_="paragraph")
    block1 = MockLayoutBlock(text_block=tb1, page_start=1, vertices=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.15), (0.1, 0.15)])
    block2 = MockLayoutBlock(text_block=tb2, page_start=1, vertices=[(0.1, 0.16), (0.9, 0.16), (0.9, 0.25), (0.1, 0.25)])

    # List block
    list_block = MockLayoutListBlock(entries=["Designed knowledge plane schema", "Optimized vector search"])
    block3 = MockLayoutBlock(list_block=list_block, page_start=1)

    # Table block
    tbl_block = MockLayoutTableBlock(
        header_rows=[["Language", "Level"]],
        body_rows=[["Python", "Expert"], ["Go", "Proficient"]],
    )
    block4 = MockLayoutBlock(table_block=tbl_block, page_start=2)

    doc_layout = MockDocumentLayout(blocks=[block1, block2, block3, block4])
    raw_doc = MockDocAiDoc(text="", pages=[], document_layout=doc_layout)

    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="projects/123/locations/us/processors/d79bb791cfba8a30",
        page_count=2,
        text="",
    )

    norm = normalize_parsed_document(parsed)
    assert norm.page_count == 2
    assert norm.table_count == 1
    assert len(norm.chunks) == 2  # Page 1 text+list chunk, Page 2 table chunk

    # Page 1 chunk
    p1_chunk = norm.chunks[0]
    assert p1_chunk.page_number == 1
    assert p1_chunk.modality == "TEXT"
    assert "Professional Summary" in p1_chunk.text_content
    assert "Experienced Staff AI Engineer" in p1_chunk.text_content
    assert "- Designed knowledge plane schema" in p1_chunk.text_content
    assert p1_chunk.embedding_768 is None

    # Page 2 table chunk
    p2_chunk = norm.chunks[1]
    assert p2_chunk.page_number == 2
    assert p2_chunk.modality == "TABLE"
    assert "| Language | Level |" in p2_chunk.text_content
    assert "| Python | Expert |" in p2_chunk.text_content
    assert p2_chunk.embedding_768 is None


def test_page_blocks_fallback_when_paragraphs_empty():
    """Tests extracting from page.blocks when page.paragraphs is empty."""
    doc_text = "Section TitleBlock content line 1. Block content line 2."
    b1 = MockParagraph([(0, 13)], vertices=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.15), (0.1, 0.15)])
    b2 = MockParagraph([(13, 56)], vertices=[(0.1, 0.16), (0.9, 0.16), (0.9, 0.3), (0.1, 0.3)])

    # paragraphs is empty, blocks is populated
    page = MockPage(page_number=1, paragraphs=[], blocks=[b1, b2])
    raw_doc = MockDocAiDoc(text=doc_text, pages=[page])

    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=1,
        text=doc_text,
    )

    norm = normalize_parsed_document(parsed)
    assert len(norm.chunks) == 1
    assert "Section Title" in norm.chunks[0].text_content
    assert "Block content line 1" in norm.chunks[0].text_content


def test_page_lines_fallback_when_blocks_and_paragraphs_empty():
    """Tests extracting from page.lines when page.paragraphs and page.blocks are empty."""
    doc_text = "Single line of text"
    line = MockParagraph([(0, 19)], vertices=[(0.1, 0.2), (0.9, 0.2), (0.9, 0.25), (0.1, 0.25)])

    # paragraphs and blocks empty, lines populated
    page = MockPage(page_number=1, paragraphs=[], blocks=[], lines=[line])
    raw_doc = MockDocAiDoc(text=doc_text, pages=[page])

    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=1,
        text=doc_text,
    )

    norm = normalize_parsed_document(parsed)
    assert len(norm.chunks) == 1
    assert norm.chunks[0].text_content == "Single line of text"


def test_raw_text_fallback_when_pages_have_no_text_containers():
    """Tests fallback to doc.text when page structures contain no text containers."""
    doc_text = "Paragraph One.\n\nParagraph Two."
    # Page with no paragraphs, blocks, lines, or tables
    page = MockPage(page_number=1, paragraphs=[], blocks=[], lines=[], tables=[])
    raw_doc = MockDocAiDoc(text=doc_text, pages=[page])

    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=1,
        text=doc_text,
    )

    norm = normalize_parsed_document(parsed)
    assert len(norm.chunks) == 1
    assert "Paragraph One." in norm.chunks[0].text_content
    assert "Paragraph Two." in norm.chunks[0].text_content


def test_empty_document_raises_extraction_error():
    """Tests that a genuinely empty document raises EmptyDocumentExtractionError."""
    # Completely empty doc
    raw_doc = MockDocAiDoc(text="", pages=[MockPage(page_number=1)])
    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=None,
        processor_name="test-proc",
        page_count=1,
        text="",
    )

    with pytest.raises(EmptyDocumentExtractionError) as exc_info:
        normalize_parsed_document(parsed)
    assert "yielded 0 chunks" in str(exc_info.value)


def test_realistic_resume_document_layout_fixture():
    """Tests a realistic Document AI Layout Parser response for a resume with columns and skills table."""
    blocks = [
        # Candidate Header
        MockLayoutBlock(
            text_block=MockLayoutTextBlock(text="Naveen Reddy Kambham | Senior Cloud Architect", type_="title"),
            page_start=1,
            vertices=[(0.1, 0.05), (0.9, 0.05), (0.9, 0.08), (0.1, 0.08)],
        ),
        # Experience Section Header
        MockLayoutBlock(
            text_block=MockLayoutTextBlock(text="Professional Experience", type_="heading-1"),
            page_start=1,
            vertices=[(0.1, 0.12), (0.9, 0.12), (0.9, 0.15), (0.1, 0.15)],
        ),
        # Experience Bullets (List block)
        MockLayoutBlock(
            list_block=MockLayoutListBlock(
                entries=[
                    "Spearheaded enterprise GraphRAG implementation across multi-region GCP Spanner.",
                    "Architected Document AI layout extraction pipelines processing 10k+ PDFs daily.",
                ]
            ),
            page_start=1,
            vertices=[(0.1, 0.16), (0.9, 0.16), (0.9, 0.35), (0.1, 0.35)],
        ),
        # Skills Table
        MockLayoutBlock(
            table_block=MockLayoutTableBlock(
                header_rows=[["Domain", "Technologies"]],
                body_rows=[
                    ["Cloud", "Google Cloud, Cloud Spanner, Document AI, Cloud Run"],
                    ["AI / ML", "Gemini 2.5, OpenTelemetry, Vector Embeddings"],
                ],
            ),
            page_start=1,
            vertices=[(0.1, 0.40), (0.9, 0.40), (0.9, 0.60), (0.1, 0.60)],
        ),
    ]

    doc_layout = MockDocumentLayout(blocks=blocks)
    # Notice pages has 0 paragraphs (simulating real Layout Parser response!)
    raw_doc = MockDocAiDoc(text="", pages=[MockPage(page_number=1, paragraphs=[])], document_layout=doc_layout)

    parsed = ParsedDocument(
        raw_document=raw_doc,
        metadata=PdfMetadata(
            bucket="sales-discovery-mvp",
            name="resumes/naveen_reddy.pdf",
            size_bytes=295000,
            generation=12345,
            content_type="application/pdf",
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            gcs_uri="gs://sales-discovery-mvp/resumes/naveen_reddy.pdf",
        ),
        processor_name="projects/123/locations/us/processors/d79bb791cfba8a30",
        page_count=1,
        text="",
    )

    norm = normalize_parsed_document(parsed)
    assert norm.page_count == 1
    assert norm.table_count == 1
    assert len(norm.chunks) == 2  # Text chunk (title + experience) + Table chunk (skills)
    assert norm.chunks[0].modality == "TEXT"
    assert norm.chunks[1].modality == "TABLE"
    assert "Senior Cloud Architect" in norm.chunks[0].text_content
    assert "Cloud Spanner" in norm.chunks[1].text_content
    assert all(chk.embedding_768 is None for chk in norm.chunks)


