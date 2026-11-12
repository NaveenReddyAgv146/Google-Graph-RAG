"""Comprehensive unit tests for Google Slides content extraction and normalization."""

import pytest

from common.database.ingestion_writer import (
    validate_chunk_fields,
    validate_document_fields,
)
from common.parsers.slides_extractor import (
    EmptyPresentationExtractionError,
    ExtractedImageMetadata,
    InvalidPresentationDataError,
    NormalizedPresentation,
    SlideElement,
    convert_slides_table_to_markdown,
    extract_plain_text_from_text_content,
    extract_presentation_elements,
    extract_slide_title,
    extract_speaker_notes_from_slide,
    extract_text_from_text_content,
    flatten_page_elements,
    generate_slide_chunk_id,
    generate_slide_document_id,
    generate_slide_visual_id,
    normalize_presentation,
)


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def sample_presentation_json() -> dict:
    """Realistic 4-slide presentation response fixture."""
    return {
        "presentationId": "10KaQ12JImPARhyWvZP7dAZLMqW62bke8",
        "title": "Enterprise Cloud Architecture Strategy",
        "pageSize": {
            "width": {"magnitude": 720.0, "unit": "PT"},
            "height": {"magnitude": 405.0, "unit": "PT"},
        },
        "slides": [
            # Slide 1: Title slide
            {
                "objectId": "slide_01",
                "pageElements": [
                    {
                        "objectId": "elem_title_01",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "CENTERED_TITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Enterprise Cloud Architecture Strategy\n"}}
                                ]
                            },
                        },
                    },
                    {
                        "objectId": "elem_subtitle_01",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "SUBTITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "High-Performance GraphRAG & Spanner Migration\n"}}
                                ]
                            },
                        },
                    },
                ],
            },
            # Slide 2: Bulleted list with nested bullets
            {
                "objectId": "slide_02",
                "pageElements": [
                    {
                        "objectId": "elem_title_02",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "TITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Strategic Objectives\n"}}
                                ]
                            },
                        },
                    },
                    {
                        "objectId": "elem_body_02",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "BODY"},
                            "text": {
                                "textElements": [
                                    {
                                        "startIndex": 0,
                                        "endIndex": 35,
                                        "paragraphMarker": {
                                            "bullet": {"listId": "kix.1", "nestingLevel": 0, "glyph": "•"}
                                        },
                                    },
                                    {
                                        "startIndex": 0,
                                        "endIndex": 35,
                                        "textRun": {"content": "Modernize core database workloads\n"},
                                    },
                                    {
                                        "startIndex": 35,
                                        "endIndex": 72,
                                        "paragraphMarker": {
                                            "bullet": {"listId": "kix.1", "nestingLevel": 1, "glyph": "o"}
                                        },
                                    },
                                    {
                                        "startIndex": 35,
                                        "endIndex": 72,
                                        "textRun": {"content": "Migrate relational data to Spanner\n"},
                                    },
                                    {
                                        "startIndex": 72,
                                        "endIndex": 115,
                                        "paragraphMarker": {
                                            "bullet": {"listId": "kix.1", "nestingLevel": 1, "glyph": "o"}
                                        },
                                    },
                                    {
                                        "startIndex": 72,
                                        "endIndex": 115,
                                        "textRun": {"content": "Leverage ISO GQL Property Graph engine\n"},
                                    },
                                    {
                                        "startIndex": 115,
                                        "endIndex": 150,
                                        "paragraphMarker": {
                                            "bullet": {"listId": "kix.1", "nestingLevel": 0, "glyph": "•"}
                                        },
                                    },
                                    {
                                        "startIndex": 115,
                                        "endIndex": 150,
                                        "textRun": {"content": "Empower automated AI discovery\n"},
                                    },
                                ]
                            },
                        },
                    },
                ],
            },
            # Slide 3: Text, Table, and Embedded Image
            {
                "objectId": "slide_03",
                "pageElements": [
                    {
                        "objectId": "elem_title_03",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "TITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Target Performance Benchmarks\n"}}
                                ]
                            },
                        },
                    },
                    {
                        "objectId": "elem_table_03",
                        "table": {
                            "rows": 3,
                            "columns": 3,
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {"text": {"textElements": [{"textRun": {"content": "Metric\n"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "Legacy Baseline\n"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "Spanner Target\n"}}]}},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {"text": {"textElements": [{"textRun": {"content": "P99 Latency\n"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "450ms\n"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "< 50ms\n"}}]}},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {"text": {"textElements": [{"textRun": {"content": "Multi-hop Traversal | Query\n"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "Timeout\n"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "Sub-second GQL\n"}}]}},
                                    ]
                                },
                            ],
                        },
                    },
                    {
                        "objectId": "elem_img_03",
                        "title": "Architecture Overview Diagram",
                        "description": "Enterprise Knowledge Graph Architecture Diagram",
                        "size": {
                            "width": {"magnitude": 400.0, "unit": "PT"},
                            "height": {"magnitude": 250.0, "unit": "PT"},
                        },
                        "image": {
                            "contentUrl": "https://lh3.googleusercontent.com/docs/diag123",
                            "sourceUrl": "https://storage.googleapis.com/source/diagram.png",
                        },
                    },
                ],
            },
            # Slide 4: Summary with speaker notes
            {
                "objectId": "slide_04",
                "pageElements": [
                    {
                        "objectId": "elem_title_04",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "TITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Next Steps & Roadmap\n"}}
                                ]
                            },
                        },
                    },
                    {
                        "objectId": "elem_body_04",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "placeholder": {"type": "BODY"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Phase 3B implementation is scheduled for immediate rollout.\n"}}
                                ]
                            },
                        },
                    },
                ],
                "slideProperties": {
                    "notesPage": {
                        "objectId": "notes_slide_04",
                        "pageElements": [
                            {
                                "objectId": "notes_elem_body_04",
                                "shape": {
                                    "shapeType": "TEXT_BOX",
                                    "placeholder": {"type": "BODY"},
                                    "text": {
                                        "textElements": [
                                            {
                                                "textRun": {
                                                    "content": "Highlight that Phase 3B requires zero Spanner schema changes.\n"
                                                }
                                            }
                                        ]
                                    },
                                },
                            }
                        ],
                    }
                },
            },
        ],
    }


# ============================================================================
# TITLE & TEXT EXTRACTION TESTS
# ============================================================================


def test_extract_slide_title_placeholders():
    """Verifies title extraction from TITLE and CENTERED_TITLE shapes."""
    # TITLE placeholder
    slide_title = {
        "pageElements": [
            {
                "objectId": "t1",
                "shape": {
                    "placeholder": {"type": "TITLE"},
                    "text": {"textElements": [{"textRun": {"content": "System Overview\n"}}]},
                },
            }
        ]
    }
    title, obj_id = extract_slide_title(slide_title)
    assert title == "System Overview"
    assert obj_id == "t1"

    # CENTERED_TITLE placeholder
    slide_centered = {
        "pageElements": [
            {
                "objectId": "c1",
                "shape": {
                    "placeholder": {"type": "CENTERED_TITLE"},
                    "text": {"textElements": [{"textRun": {"content": "Welcome Presentation\n"}}]},
                },
            }
        ]
    }
    title_c, obj_id_c = extract_slide_title(slide_centered)
    assert title_c == "Welcome Presentation"
    assert obj_id_c == "c1"

    # Slide with no title placeholder
    slide_no_title = {
        "pageElements": [
            {
                "objectId": "b1",
                "shape": {
                    "placeholder": {"type": "BODY"},
                    "text": {"textElements": [{"textRun": {"content": "Just body text\n"}}]},
                },
            }
        ]
    }
    title_none, obj_id_none = extract_slide_title(slide_no_title)
    assert title_none is None
    assert obj_id_none is None


def test_extract_text_plain_paragraphs():
    """Verifies plain paragraphs with multiple text runs."""
    text_obj = {
        "textElements": [
            {"textRun": {"content": "Enterprise GraphRAG "}},
            {"textRun": {"content": "delivers high-accuracy discovery.\n"}},
            {"textRun": {"content": "Second paragraph content.\n"}},
        ]
    }
    text = extract_text_from_text_content(text_obj)
    assert "Enterprise GraphRAG delivers high-accuracy discovery." in text
    assert "Second paragraph content." in text


def test_extract_text_bullet_lists_nesting():
    """Verifies bullet points with hierarchical indentation."""
    text_obj = {
        "textElements": [
            {
                "paragraphMarker": {"bullet": {"nestingLevel": 0}},
                "textRun": {"content": "Primary goal\n"},
            },
            {
                "paragraphMarker": {"bullet": {"nestingLevel": 1}},
                "textRun": {"content": "Sub-goal item 1\n"},
            },
            {
                "paragraphMarker": {"bullet": {"nestingLevel": 2}},
                "textRun": {"content": "Deep nested detail\n"},
            },
        ]
    }
    extracted = extract_text_from_text_content(text_obj)
    lines = extracted.split("\n")
    assert lines[0] == "- Primary goal"
    assert lines[1] == "  - Sub-goal item 1"
    assert lines[2] == "    - Deep nested detail"


# ============================================================================
# TABLE & SPEAKER NOTES TESTS
# ============================================================================


def test_convert_slides_table_to_markdown():
    """Verifies table conversion to Markdown with headers, padding, and pipe escaping."""
    table = {
        "rows": 2,
        "columns": 2,
        "tableRows": [
            {
                "tableCells": [
                    {"text": {"textElements": [{"textRun": {"content": "Category\n"}}]}},
                    {"text": {"textElements": [{"textRun": {"content": "Value | Score\n"}}]}},
                ]
            },
            {
                "tableCells": [
                    {"text": {"textElements": [{"textRun": {"content": "Performance\n"}}]}},
                    {"text": {"textElements": [{"textRun": {"content": "High\n"}}]}},
                ]
            },
        ],
    }
    md = convert_slides_table_to_markdown(table)
    assert "| Category | Value \\| Score |" in md
    assert "| --- | --- |" in md
    assert "| Performance | High |" in md


def test_convert_slides_table_edge_cases():
    """Verifies empty or malformed table handling."""
    assert convert_slides_table_to_markdown({}) == ""
    assert convert_slides_table_to_markdown({"tableRows": []}) == ""

    # Synthesize headers when row 0 cells are empty
    table_empty_header = {
        "tableRows": [
            {"tableCells": [{"text": {}}, {"text": {}}]},
            {"tableCells": [{"text": {"textElements": [{"textRun": {"content": "Data 1\n"}}]}}, {"text": {"textElements": [{"textRun": {"content": "Data 2\n"}}]}}]},
        ]
    }
    md = convert_slides_table_to_markdown(table_empty_header)
    assert "| Column 1 | Column 2 |" in md
    assert "| Data 1 | Data 2 |" in md


def test_extract_speaker_notes():
    """Verifies speaker notes extraction from notesPage."""
    slide = {
        "slideProperties": {
            "notesPage": {
                "pageElements": [
                    {
                        "shape": {
                            "placeholder": {"type": "SLIDE_IMAGE"},
                            "text": {"textElements": []},
                        }
                    },
                    {
                        "shape": {
                            "placeholder": {"type": "BODY"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Remember to highlight the 90% latency reduction.\n"}}
                                ]
                            },
                        }
                    },
                ]
            }
        }
    }
    notes = extract_speaker_notes_from_slide(slide)
    assert notes == "Remember to highlight the 90% latency reduction."


def test_flatten_page_elements_grouped():
    """Verifies recursive expansion of elementGroup containers."""
    elements = [
        {"objectId": "shape1", "shape": {"shapeType": "TEXT_BOX"}},
        {
            "objectId": "group1",
            "elementGroup": {
                "children": [
                    {"objectId": "nested_shape", "shape": {"shapeType": "RECTANGLE"}},
                    {"objectId": "nested_table", "table": {"rows": 1, "columns": 1}},
                ]
            },
        },
    ]
    flattened = flatten_page_elements(elements)
    obj_ids = [e["objectId"] for e in flattened]
    assert obj_ids == ["shape1", "nested_shape", "nested_table"]


# ============================================================================
# DETERMINISTIC ID TESTS
# ============================================================================


def test_deterministic_id_generation():
    """Verifies deterministic DocumentId, ChunkId, and VisualId generation and bounds."""
    pres_id = "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"
    doc_id = generate_slide_document_id(presentation_id=pres_id)
    assert doc_id == f"doc_slide_{pres_id}"
    assert len(doc_id) <= 64

    # ChunkId formatting and limit
    chunk_id_text = generate_slide_chunk_id(doc_id, slide_number=2, chunk_index=1, modality="TEXT")
    assert chunk_id_text == f"{doc_id}_s02_t001"
    assert len(chunk_id_text) <= 64

    chunk_id_table = generate_slide_chunk_id(doc_id, slide_number=3, chunk_index=2, modality="TABLE")
    assert chunk_id_table == f"{doc_id}_s03_b002"
    assert len(chunk_id_table) <= 64

    # VisualId formatting and limit
    vis_id_render = generate_slide_visual_id(doc_id, slide_number=1, asset_index=0)
    assert vis_id_render.startswith("vis_")
    assert "_s01_v000" in vis_id_render
    assert len(vis_id_render) <= 64

    vis_id_img = generate_slide_visual_id(doc_id, slide_number=3, asset_index=1)
    assert "_s03_v001" in vis_id_img
    assert len(vis_id_img) <= 64


def test_deterministic_id_oversized_bounds():
    """Verifies strict 64-character limit even when document_id is very long."""
    long_doc_id = "doc_" + "a" * 120
    chunk_id = generate_slide_chunk_id(long_doc_id, slide_number=12, chunk_index=99, modality="TEXT")
    assert len(chunk_id) <= 64
    assert chunk_id.endswith("_s12_t099")

    vis_id = generate_slide_visual_id(long_doc_id, slide_number=12, asset_index=99)
    assert len(vis_id) <= 64
    assert vis_id.startswith("vis_")
    assert vis_id.endswith("_s12_v099")


# ============================================================================
# END-TO-END EXTRACTION & NORMALIZATION TESTS
# ============================================================================


def test_extract_presentation_elements_end_to_end(sample_presentation_json):
    """Verifies extract_presentation_elements returns correctly typed SlideElements."""
    elements = extract_presentation_elements(sample_presentation_json)
    assert len(elements) >= 7

    elem_types = [e.element_type for e in elements]
    assert "TITLE" in elem_types
    assert "TEXT" in elem_types
    assert "TABLE" in elem_types
    assert "IMAGE" in elem_types
    assert "NOTES" in elem_types

    # Provenance assertions
    for el in elements:
        assert isinstance(el, SlideElement)
        assert 1 <= el.slide_number <= 4
        assert el.object_id


def test_normalize_presentation_end_to_end(sample_presentation_json):
    """Verifies full normalization of realistic presentation into DocumentNode, ChunkNodes, VisualAssetNodes."""
    norm = normalize_presentation(
        sample_presentation_json,
        source_url="https://docs.google.com/presentation/d/10KaQ12JImPARhyWvZP7dAZLMqW62bke8/edit",
        gcs_uri=None,
    )

    assert isinstance(norm, NormalizedPresentation)
    assert norm.slide_count == 4
    assert norm.table_count == 1
    assert norm.image_count == 1
    assert norm.total_characters > 0

    # 1. DocumentNode verification
    doc = norm.document_node
    assert doc.document_id == "doc_slide_10KaQ12JImPARhyWvZP7dAZLMqW62bke8"
    assert doc.file_name == "Enterprise Cloud Architecture Strategy.gslides"
    assert doc.mime_type == "application/vnd.google-apps.presentation"
    assert doc.drive_file_id == "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"
    assert doc.status == "ACTIVE"

    # 2. ChunkNode verification
    assert len(norm.chunks) >= 4
    for chk in norm.chunks:
        assert chk.document_id == doc.document_id
        assert chk.slide_number is not None
        assert 1 <= chk.slide_number <= 4
        assert chk.page_number is None  # Strict provenance: slide presentation, not PDF
        assert chk.embedding_768 is None  # Strict: Embedding belongs to Phase 3D
        assert len(chk.chunk_id) <= 64
        assert chk.modality in ("TEXT", "TABLE")

    # Verify table chunk
    table_chunks = [c for c in norm.chunks if c.modality == "TABLE"]
    assert len(table_chunks) == 1
    assert table_chunks[0].slide_number == 3
    assert "| Metric | Legacy Baseline | Spanner Target |" in table_chunks[0].text_content

    # Verify speaker notes chunk
    notes_chunks = [c for c in norm.chunks if "Speaker Notes:" in c.text_content]
    assert len(notes_chunks) == 1
    assert notes_chunks[0].slide_number == 4
    assert "Highlight that Phase 3B requires zero Spanner schema changes." in notes_chunks[0].text_content

    # 3. VisualAssetNode verification
    # 4 slide renders (one per slide) + 1 embedded image on slide 3 = 5 visual assets
    assert len(norm.visual_assets) == 5

    renders = [v for v in norm.visual_assets if v.asset_type == "SLIDE"]
    assert len(renders) == 4
    for idx, r in enumerate(renders, start=1):
        assert r.slide_number == idx
        assert r.page_number is None
        assert len(r.visual_id) <= 64
        assert r.image_gcs_uri.endswith(f"slide_{idx:02d}_render.png")

    images = [v for v in norm.visual_assets if v.asset_type == "IMAGE"]
    assert len(images) == 1
    assert images[0].slide_number == 3
    assert len(images[0].visual_id) <= 64
    assert "elem_img_03" in images[0].image_gcs_uri

    # 4. Extracted Image Metadata verification
    assert len(norm.extracted_images) == 1
    img_meta = norm.extracted_images[0]
    assert isinstance(img_meta, ExtractedImageMetadata)
    assert img_meta.object_id == "elem_img_03"
    assert img_meta.slide_number == 3
    assert img_meta.width_pt == 400.0
    assert img_meta.height_pt == 250.0
    assert img_meta.content_url == "https://lh3.googleusercontent.com/docs/diag123"
    assert img_meta.source_url == "https://storage.googleapis.com/source/diagram.png"


def test_normalize_presentation_schema_compliance(sample_presentation_json):
    """Verifies that all normalized models pass Phase 2C Spanner schema bounds."""
    norm = normalize_presentation(sample_presentation_json)

    # Validate DocumentNode
    validate_document_fields(norm.document_node)

    # Validate all ChunkNodes
    for chk in norm.chunks:
        validate_chunk_fields(chk)

    # Validate VisualAssetNodes bounds
    for vis in norm.visual_assets:
        assert len(vis.visual_id) <= 64
        assert len(vis.document_id) <= 256
        assert len(vis.image_gcs_uri) <= 1024
        assert len(vis.asset_type) <= 32


def test_normalize_presentation_oversized_text_split():
    """Verifies split_oversized_text integration for slides with large text content."""
    long_para = "This is a detailed enterprise architecture paragraph. " * 50  # ~2750 chars
    presentation = {
        "presentationId": "oversized_presentation_01",
        "title": "Oversized Presentation",
        "slides": [
            {
                "objectId": "s1",
                "pageElements": [
                    {
                        "objectId": "sh1",
                        "shape": {
                            "placeholder": {"type": "BODY"},
                            "text": {"textElements": [{"textRun": {"content": f"{long_para}\n"}}]},
                        },
                    }
                ],
            }
        ],
    }

    norm = normalize_presentation(presentation, max_chunk_chars=1000, chunk_overlap_chars=100)
    assert len(norm.chunks) >= 3
    for c in norm.chunks:
        assert len(c.text_content) <= 1000
        assert c.slide_number == 1
        assert len(c.chunk_id) <= 64


def test_normalize_presentation_idempotency(sample_presentation_json):
    """Verifies normalization is completely deterministic on repeated runs."""
    run1 = normalize_presentation(sample_presentation_json)
    run2 = normalize_presentation(sample_presentation_json)

    assert run1.document_node.document_id == run2.document_node.document_id
    assert [c.chunk_id for c in run1.chunks] == [c.chunk_id for c in run2.chunks]
    assert [c.text_content for c in run1.chunks] == [c.text_content for c in run2.chunks]
    assert [v.visual_id for v in run1.visual_assets] == [v.visual_id for v in run2.visual_assets]
    assert [v.image_gcs_uri for v in run1.visual_assets] == [v.image_gcs_uri for v in run2.visual_assets]


def test_normalize_presentation_error_conditions():
    """Verifies typed error handling for malformed or empty presentation structures."""
    # Invalid data
    with pytest.raises(InvalidPresentationDataError):
        normalize_presentation({})

    with pytest.raises(InvalidPresentationDataError):
        normalize_presentation(None)  # type: ignore

    # Missing or empty slides
    with pytest.raises(EmptyPresentationExtractionError):
        normalize_presentation({"presentationId": "p1", "slides": None})

    with pytest.raises(EmptyPresentationExtractionError):
        normalize_presentation({"presentationId": "p1", "slides": []})

    # Presentation with 0 text/table chunks (e.g. only blank slides)
    blank_pres = {
        "presentationId": "p_blank",
        "title": "Blank Presentation",
        "slides": [{"objectId": "blank_s1", "pageElements": []}],
    }
    with pytest.raises(EmptyPresentationExtractionError) as exc_info:
        normalize_presentation(blank_pres)
    assert "yielded 0 chunks" in str(exc_info.value)
