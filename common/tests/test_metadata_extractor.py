"""Offline unit tests for Structured Gemini Metadata Extractor.

Tests schema validation, evidence verification, unsupported claims, unknown technologies,
duplicate candidates, relationship grounding, retries, and malformed responses.
"""

from unittest.mock import MagicMock
import pytest

from common.models.schema import ChunkNode
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    ExtractionInvalidInputError,
    ExtractionRateLimitError,
    ExtractionResponseMalformedError,
    ExtractionServiceError,
    MetadataExtractionError,
    MetadataExtractor,
    RawEntityCandidate,
    RawEntityRelationshipCandidate,
    RawMetadataExtractionResponse,
    RawSolutionCardCandidate,
    RawTechnologyCandidate,
    RejectedCandidate,
    generate_card_id,
    verify_evidence_quote,
)
from common.taxonomy import TaxonomyMapper, generate_entity_id


# ============================================================================
# HELPER MOCKS
# ============================================================================


def make_mock_client(response_text: str | None = None, side_effect: Exception | None = None):
    """Create a mock Google GenAI client returning response_text or raising side_effect."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = response_text
    if side_effect:
        mock_client.models.generate_content.side_effect = side_effect
    else:
        mock_client.models.generate_content.return_value = mock_response
    return mock_client


# ============================================================================
# 1. EVIDENCE VERIFICATION (SUBSTRING MATCHING)
# ============================================================================


def test_verify_evidence_quote_exact():
    """Verify exact verbatim substring matches."""
    chunk = "Google Cloud Spanner delivers 99.999% availability for enterprise workloads."
    assert verify_evidence_quote("Google Cloud Spanner", chunk) is True
    assert verify_evidence_quote("99.999% availability", chunk) is True
    assert verify_evidence_quote("enterprise workloads", chunk) is True


def test_verify_evidence_quote_whitespace_collapsed():
    """Verify substring matching handles newline and space variations."""
    chunk = "Google Cloud Spanner delivers\n99.999% availability  for enterprise workloads."
    assert verify_evidence_quote("delivers 99.999% availability", chunk) is True
    assert verify_evidence_quote('"delivers 99.999% availability"', chunk) is True


def test_verify_evidence_quote_unsupported_rejected():
    """Verify ungrounded/hallucinated quotes are rejected."""
    chunk = "Google Cloud Spanner delivers 99.999% availability for enterprise workloads."
    assert verify_evidence_quote("Amazon DynamoDB", chunk) is False
    assert verify_evidence_quote("99.99% availability", chunk) is False  # mismatch
    assert verify_evidence_quote("", chunk) is False
    assert verify_evidence_quote("   ", chunk) is False
    assert verify_evidence_quote("Spanner", "") is False


# ============================================================================
# 2. SCHEMA VALIDATION & GROUNDING
# ============================================================================


def test_metadata_extractor_full_valid_extraction():
    """Verify complete extraction with entities, technologies, solution card, and relationship."""
    chunk_1 = "Acme Retail modernized their inventory using Google Cloud Spanner and BigQuery for real-time analytics."
    chunk_2 = "The Payment Gateway subsystem integrates with the Inventory Core service to process orders."

    raw_response = {
        "entities": [
            {
                "name": "Inventory Core",
                "category": "SUBSYSTEM",
                "description": "Core inventory management service",
                "chunk_id": "chk_2",
                "evidence_quote": "Inventory Core service",
            },
            {
                "name": "Payment Gateway",
                "category": "SUBSYSTEM",
                "description": "Processes payments",
                "chunk_id": "chk_2",
                "evidence_quote": "Payment Gateway subsystem",
            },
        ],
        "technologies": [
            {
                "name": "Cloud Spanner",
                "chunk_id": "chk_1",
                "evidence_quote": "Google Cloud Spanner",
            },
            {
                "name": "BigQuery",
                "chunk_id": "chk_1",
                "evidence_quote": "BigQuery for real-time analytics",
            },
        ],
        "solution_cards": [
            {
                "project_name": "Acme Inventory Modernization",
                "business_problem": "Legacy inventory system bottleneck",
                "proposed_solution": "Modernize with Google Cloud Spanner and BigQuery",
                "implemented_solution": "Inventory service on Spanner and analytics on BigQuery",
                "outcome": "Real-time analytics enabled",
                "chunk_id": "chk_1",
                "evidence_quote": "Acme Retail modernized their inventory using Google Cloud Spanner and BigQuery",
                "technologies": ["Cloud Spanner", "BigQuery"],
            }
        ],
        "relationships": [
            {
                "source_entity_name": "Payment Gateway",
                "source_entity_category": "SUBSYSTEM",
                "target_entity_name": "Inventory Core",
                "target_entity_category": "SUBSYSTEM",
                "relationship_type": "INTEGRATES_WITH",
                "chunk_id": "chk_2",
                "evidence_quote": "Payment Gateway subsystem integrates with the Inventory Core service",
            }
        ],
    }

    import json
    mock_client = make_mock_client(response_text=json.dumps(raw_response))
    extractor = MetadataExtractor(client=mock_client)

    chunks = [
        ChunkNode(chunk_id="chk_1", document_id="doc_acme", text_content=chunk_1),
        ChunkNode(chunk_id="chk_2", document_id="doc_acme", text_content=chunk_2),
    ]

    result = extractor.extract_from_document("doc_acme", chunks)

    # 1. Entities
    assert len(result.entities) == 2
    ent_names = [e.name for e in result.entities]
    assert "Inventory Core" in ent_names
    assert "Payment Gateway" in ent_names
    for e in result.entities:
        assert e.entity_id.startswith("ent_")
        assert len(e.entity_id) <= 64
        # Verify node conversion
        node = e.to_node()
        assert node.entity_id == e.entity_id

    # 2. Technologies
    assert len(result.technologies) == 2
    tech_ids = [t.tech_id for t in result.technologies]
    assert "tech_cloud_spanner" in tech_ids
    assert "tech_bigquery" in tech_ids
    assert result.unresolved_technologies == []

    # 3. Solution Cards
    assert len(result.solution_cards) == 1
    card = result.solution_cards[0]
    assert card.project_name == "Acme Inventory Modernization"
    assert card.card_id.startswith("card_")
    assert len(card.card_id) <= 64
    assert card.technologies == ["tech_bigquery", "tech_cloud_spanner"]
    card_node = card.to_node()
    assert card_node.card_id == card.card_id

    # 4. Relationships
    assert len(result.relationships) == 1
    rel = result.relationships[0]
    assert rel.relationship_type == "INTEGRATES_WITH"
    assert rel.source_name == "Payment Gateway"
    assert rel.target_name == "Inventory Core"

    # 5. Rejected
    assert len(result.rejected_candidates) == 0


# ============================================================================
# 3. UNSUPPORTED CLAIMS & GROUNDING REJECTION
# ============================================================================


def test_unsupported_evidence_quote_rejected():
    """Verify candidates with quotes not found in chunk text are rejected."""
    chunk_1 = "We run on Kubernetes Engine."
    raw_response = {
        "entities": [
            {
                "name": "Ghost Entity",
                "category": "CONCEPT",
                "chunk_id": "chk_1",
                "evidence_quote": "This quote is nowhere in the text",
            }
        ],
        "technologies": [
            {
                "name": "Cloud Spanner",
                "chunk_id": "chk_1",
                "evidence_quote": "Cloud Spanner is our primary database",  # Not in text
            }
        ],
    }

    import json
    mock_client = make_mock_client(response_text=json.dumps(raw_response))
    extractor = MetadataExtractor(client=mock_client)

    result = extractor.extract_from_chunk("chk_1", chunk_1, document_id="doc_1")

    assert len(result.entities) == 0
    assert len(result.technologies) == 0
    assert len(result.rejected_candidates) == 2
    assert all("Evidence quote not found" in r.rejection_reason for r in result.rejected_candidates)


def test_invalid_chunk_id_rejected():
    """Verify candidates citing a non-existent chunk_id are rejected."""
    chunk_1 = "We run on GKE."
    raw_response = {
        "entities": [
            {
                "name": "GKE Cluster",
                "category": "SUBSYSTEM",
                "chunk_id": "non_existent_chunk_999",
                "evidence_quote": "We run on GKE.",
            }
        ]
    }

    import json
    mock_client = make_mock_client(response_text=json.dumps(raw_response))
    extractor = MetadataExtractor(client=mock_client)

    result = extractor.extract_from_chunk("chk_1", chunk_1, document_id="doc_1")
    assert len(result.entities) == 0
    assert len(result.rejected_candidates) == 1
    assert "does not exist in document" in result.rejected_candidates[0].rejection_reason


def test_self_referential_relationship_rejected():
    """Verify self-referential relationships (source == target) are rejected."""
    chunk = "The Auth Service authenticates itself."
    raw_response = {
        "relationships": [
            {
                "source_entity_name": "Auth Service",
                "source_entity_category": "SUBSYSTEM",
                "target_entity_name": "Auth Service",
                "target_entity_category": "SUBSYSTEM",
                "relationship_type": "DEPENDS_ON",
                "chunk_id": "chk_1",
                "evidence_quote": "Auth Service authenticates itself",
            }
        ]
    }

    import json
    mock_client = make_mock_client(response_text=json.dumps(raw_response))
    extractor = MetadataExtractor(client=mock_client)

    result = extractor.extract_from_chunk("chk_1", chunk, document_id="doc_1")
    assert len(result.relationships) == 0
    assert len(result.rejected_candidates) == 1
    assert "Self-referential" in result.rejected_candidates[0].rejection_reason


# ============================================================================
# 4. TAXONOMY INTEGRATION & UNKNOWN TECHNOLOGIES
# ============================================================================


def test_unknown_technologies_remain_unresolved():
    """Verify unknown technologies are marked unresolved and never assigned fake IDs."""
    chunk = "The platform combines BigQuery with QuantumCryptoLib for security."
    raw_response = {
        "technologies": [
            {
                "name": "BigQuery",
                "chunk_id": "chk_1",
                "evidence_quote": "BigQuery",
            },
            {
                "name": "QuantumCryptoLib",
                "chunk_id": "chk_1",
                "evidence_quote": "QuantumCryptoLib",
            },
        ]
    }

    import json
    mock_client = make_mock_client(response_text=json.dumps(raw_response))
    extractor = MetadataExtractor(client=mock_client)

    result = extractor.extract_from_chunk("chk_1", chunk, document_id="doc_1")

    assert len(result.technologies) == 2

    # BigQuery resolved
    bq = next(t for t in result.technologies if t.raw_name == "BigQuery")
    assert bq.is_canonical is True
    assert bq.unresolved is False
    assert bq.tech_id == "tech_bigquery"
    assert bq.canonical_name == "BigQuery"

    # QuantumCryptoLib unresolved
    qc = next(t for t in result.technologies if t.raw_name == "QuantumCryptoLib")
    assert qc.is_canonical is False
    assert qc.unresolved is True
    assert qc.tech_id is None
    assert qc.to_node() is None

    assert result.unresolved_technologies == ["QuantumCryptoLib"]


# ============================================================================
# 5. DEDUPLICATION & DETERMINISTIC SORTING
# ============================================================================


def test_deduplication_and_deterministic_sorting():
    """Verify duplicate candidates are deduplicated and sorted deterministically."""
    chunk_1 = "We use Cloud Spanner."
    chunk_2 = "Cloud Spanner powers our database layer."

    raw_response = {
        "entities": [
            {
                "name": "Beta System",
                "category": "SUBSYSTEM",
                "chunk_id": "chk_1",
                "evidence_quote": "Cloud Spanner",
            },
            {
                "name": "Alpha System",
                "category": "SUBSYSTEM",
                "chunk_id": "chk_1",
                "evidence_quote": "Cloud Spanner",
            },
            {
                "name": "Beta System",
                "category": "SUBSYSTEM",
                "description": "Preserve this description",
                "chunk_id": "chk_2",
                "evidence_quote": "Cloud Spanner powers our database",
            },
        ],
        "technologies": [
            {
                "name": "Spanner",
                "chunk_id": "chk_1",
                "evidence_quote": "Cloud Spanner",
            },
            {
                "name": "Google Cloud Spanner",
                "chunk_id": "chk_1",
                "evidence_quote": "Cloud Spanner",
            },
        ],
    }

    import json
    mock_client = make_mock_client(response_text=json.dumps(raw_response))
    extractor = MetadataExtractor(client=mock_client)

    chunks = [
        ("chk_1", chunk_1),
        ("chk_2", chunk_2),
    ]

    result = extractor.extract_from_document("doc_1", chunks)

    # Entities deduplicated: Alpha System and Beta System (2 total)
    assert len(result.entities) == 2
    # Sorted by entity_id
    ids = [e.entity_id for e in result.entities]
    assert ids == sorted(ids)

    # Beta System description preserved
    beta = next(e for e in result.entities if e.name == "Beta System")
    assert beta.description == "Preserve this description"

    # Technologies deduplicated by (chunk_id, tech_id): exactly 1 entry for chk_1
    assert len(result.technologies) == 1
    assert result.technologies[0].tech_id == "tech_cloud_spanner"


# ============================================================================
# 6. RETRIES & ERROR HANDLING
# ============================================================================


def test_transient_rate_limit_429_retries_and_succeeds():
    """Verify 429 rate limit triggers exponential backoff and succeeds on retry."""
    mock_client = MagicMock()
    mock_sleeper = MagicMock()

    class FakeRateLimitError(Exception):
        code = 429

    mock_response = MagicMock()
    mock_response.text = '{"entities": [], "technologies": []}'

    mock_client.models.generate_content.side_effect = [
        FakeRateLimitError("Quota exceeded"),
        mock_response,
    ]

    extractor = MetadataExtractor(
        client=mock_client,
        sleeper=mock_sleeper,
        max_retries=3,
    )

    result = extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert isinstance(result, ExtractedMetadataResult)
    assert mock_client.models.generate_content.call_count == 2
    assert mock_sleeper.call_count == 1


def test_transient_503_retries_and_succeeds():
    """Verify 503 service unavailable triggers backoff and succeeds."""
    mock_client = MagicMock()
    mock_sleeper = MagicMock()

    class FakeServiceError(Exception):
        status_code = 503

    mock_response = MagicMock()
    mock_response.text = '{"entities": [], "technologies": []}'

    mock_client.models.generate_content.side_effect = [
        FakeServiceError("Backend overloaded"),
        mock_response,
    ]

    extractor = MetadataExtractor(
        client=mock_client,
        sleeper=mock_sleeper,
        max_retries=3,
    )

    result = extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert isinstance(result, ExtractedMetadataResult)
    assert mock_client.models.generate_content.call_count == 2
    assert mock_sleeper.call_count == 1


def test_rate_limit_exhaustion_raises_typed_error():
    """Verify rate limit exhaustion raises ExtractionRateLimitError."""
    mock_client = MagicMock()
    mock_sleeper = MagicMock()

    class FakeRateLimitError(Exception):
        code = 429

    mock_client.models.generate_content.side_effect = FakeRateLimitError("Rate limit exceeded")

    extractor = MetadataExtractor(
        client=mock_client,
        sleeper=mock_sleeper,
        max_retries=2,
    )

    with pytest.raises(ExtractionRateLimitError) as exc_info:
        extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert "rate limit exceeded" in str(exc_info.value).lower()
    assert mock_client.models.generate_content.call_count == 3  # Initial + 2 retries
    assert mock_sleeper.call_count == 2


def test_server_error_exhaustion_raises_typed_error():
    """Verify 5xx exhaustion raises ExtractionServiceError."""
    mock_client = MagicMock()
    mock_sleeper = MagicMock()

    class FakeInternalError(Exception):
        status_code = 500

    mock_client.models.generate_content.side_effect = FakeInternalError("Internal server failure")

    extractor = MetadataExtractor(
        client=mock_client,
        sleeper=mock_sleeper,
        max_retries=2,
    )

    with pytest.raises(ExtractionServiceError):
        extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert mock_client.models.generate_content.call_count == 3
    assert mock_sleeper.call_count == 2


def test_non_retryable_400_fails_immediately():
    """Verify 400 Bad Request aborts immediately without retry."""
    mock_client = MagicMock()
    mock_sleeper = MagicMock()

    class FakeBadRequest(Exception):
        code = 400

    mock_client.models.generate_content.side_effect = FakeBadRequest("Invalid argument")

    extractor = MetadataExtractor(
        client=mock_client,
        sleeper=mock_sleeper,
        max_retries=3,
    )

    with pytest.raises(MetadataExtractionError) as exc_info:
        extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert "code=400" in str(exc_info.value)
    assert mock_client.models.generate_content.call_count == 1
    mock_sleeper.assert_not_called()


# ============================================================================
# 7. MALFORMED RESPONSES & EMPTY INPUTS
# ============================================================================


def test_malformed_json_raises_error():
    """Verify non-JSON response text raises ExtractionResponseMalformedError."""
    mock_client = make_mock_client(response_text="This is not JSON text at all.")
    extractor = MetadataExtractor(client=mock_client)

    with pytest.raises(ExtractionResponseMalformedError) as exc_info:
        extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert "Failed to parse" in str(exc_info.value)


def test_empty_response_text_raises_error():
    """Verify empty or None response text raises ExtractionResponseMalformedError."""
    mock_client = make_mock_client(response_text="")
    extractor = MetadataExtractor(client=mock_client)

    with pytest.raises(ExtractionResponseMalformedError) as exc_info:
        extractor.extract_from_chunk("chk_1", "Sample text", document_id="doc_1")
    assert "empty or whitespace-only" in str(exc_info.value)


def test_empty_chunks_makes_zero_calls():
    """Verify passing empty text chunks returns empty result with 0 API calls."""
    mock_client = MagicMock()
    extractor = MetadataExtractor(client=mock_client)

    # Empty chunk text
    res1 = extractor.extract_from_chunk("chk_1", "   ", document_id="doc_1")
    assert len(res1.entities) == 0
    mock_client.models.generate_content.assert_not_called()

    # Empty document chunks list
    res2 = extractor.extract_from_document("doc_1", [])
    assert len(res2.entities) == 0
    mock_client.models.generate_content.assert_not_called()


def test_invalid_input_parameters():
    """Verify empty document_id or chunk_id raises ExtractionInvalidInputError."""
    extractor = MetadataExtractor(client=MagicMock())
    with pytest.raises(ExtractionInvalidInputError):
        extractor.extract_from_chunk("", "Some text")

    with pytest.raises(ExtractionInvalidInputError):
        extractor.extract_from_document("", [])


def test_generate_card_id_stability():
    """Verify deterministic card_id generation is stable and <= 64 chars."""
    id1 = generate_card_id("doc_123", "Customer Modernization")
    id2 = generate_card_id("doc_123", "  customer modernization  ")
    assert id1 == id2
    assert id1.startswith("card_")
    assert len(id1) == 53
    assert len(id1) <= 64
