"""Unit tests for Phase 3C.2 Vertex AI Embeddings Client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from common.config import Settings
from common.embeddings.vertex_client import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EMBEDDING_DIMENSIONS,
    EmbeddingAccessDeniedError,
    EmbeddingDimensionalityError,
    EmbeddingModelNotFoundError,
    EmbeddingRateLimitError,
    EmbeddingResponseMalformedError,
    EmbeddingServiceError,
    EmptyTextEmbeddingError,
    InvalidInputTextError,
    VertexEmbeddingsClient,
)


# ============================================================================
# Helpers & Mocks
# ============================================================================


def make_vector(val: float = 0.1, length: int = DEFAULT_EMBEDDING_DIMENSIONS) -> list[float]:
    """Generates a dummy float vector of exact length."""
    return [val] * length


def make_mock_embedding_item(vector: list[float]) -> MagicMock:
    """Creates a mock ContentEmbedding item with values attribute."""
    item = MagicMock()
    item.values = vector
    return item


def make_mock_response(vectors: list[list[float]]) -> MagicMock:
    """Creates a mock EmbedContentResponse containing a list of ContentEmbedding items."""
    resp = MagicMock()
    resp.embeddings = [make_mock_embedding_item(v) for v in vectors]
    return resp


class MockAPIError(Exception):
    """Simulates google.genai.errors.APIError with HTTP status code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ============================================================================
# 1. Initialization & Configuration Tests
# ============================================================================


def test_client_initialization_defaults():
    """Verify default initialization takes settings values."""
    mock_settings = Settings(
        project_id="test-proj-123",
        region="us-central1",
        embedding_model="text-embedding-004",
        embedding_dimensions=768,
    )
    mock_genai_client = MagicMock()
    client = VertexEmbeddingsClient(client=mock_genai_client, settings=mock_settings)

    assert client.project_id == "test-proj-123"
    assert client.location == "us-central1"
    assert client.model_name == "text-embedding-004"
    assert client.dimensions == 768
    assert client.max_retries == 3


def test_client_initialization_custom_overrides():
    """Verify explicit parameters override settings."""
    mock_genai_client = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai_client,
        model_name="custom-embedder",
        dimensions=768,
        project_id="custom-proj",
        location="europe-west1",
        max_retries=5,
        initial_backoff=1.0,
    )

    assert client.model_name == "custom-embedder"
    assert client.dimensions == 768
    assert client.project_id == "custom-proj"
    assert client.location == "europe-west1"
    assert client.max_retries == 5
    assert client.initial_backoff == 1.0


# ============================================================================
# 2. Single & Batched Embedding Generation Tests
# ============================================================================


def test_embed_single_text_success():
    """Verify embed_text returns a single 768-dim vector."""
    mock_genai = MagicMock()
    expected_vec = make_vector(0.42, 768)
    mock_genai.models.embed_content.return_value = make_mock_response([expected_vec])

    client = VertexEmbeddingsClient(client=mock_genai)
    result = client.embed_text("Cloud Spanner is a globally distributed database.")

    assert len(result) == 768
    assert result == expected_vec
    mock_genai.models.embed_content.assert_called_once()
    call_kwargs = mock_genai.models.embed_content.call_args[1]
    assert call_kwargs["contents"] == ["Cloud Spanner is a globally distributed database."]
    assert call_kwargs["config"].output_dimensionality == 768


def test_embed_texts_empty_list():
    """Verify empty input list returns empty list with zero API calls."""
    mock_genai = MagicMock()
    client = VertexEmbeddingsClient(client=mock_genai)

    result = client.embed_texts([])
    assert result == []
    mock_genai.models.embed_content.assert_not_called()


def test_embed_texts_batching_and_ordering():
    """Verify multi-batch input partitions correctly and preserves 1:1 order."""
    mock_genai = MagicMock()
    total_items = 65
    batch_size = 25
    input_texts = [f"Chunk content item #{i}" for i in range(total_items)]

    # Mock responses for 3 batches: 25, 25, 15 items
    def mock_embed(model, contents, config):
        vectors = [make_vector(float(text.split("#")[1]), 768) for text in contents]
        return make_mock_response(vectors)

    mock_genai.models.embed_content.side_effect = mock_embed

    client = VertexEmbeddingsClient(client=mock_genai)
    results = client.embed_texts(input_texts, batch_size=batch_size)

    assert len(results) == 65
    assert mock_genai.models.embed_content.call_count == 3

    # Check batch slice sizes
    calls = mock_genai.models.embed_content.call_args_list
    assert len(calls[0][1]["contents"]) == 25
    assert len(calls[1][1]["contents"]) == 25
    assert len(calls[2][1]["contents"]) == 15

    # Check order preservation: result[i] corresponds to input_texts[i]
    for i in range(total_items):
        assert results[i][0] == pytest.approx(float(i))
        assert len(results[i]) == 768


def test_embed_texts_exact_batch_boundary():
    """Verify batching when total items is an exact multiple of batch_size."""
    mock_genai = MagicMock()
    total_items = 50
    batch_size = 25
    input_texts = [f"Text {i}" for i in range(total_items)]

    mock_genai.models.embed_content.side_effect = lambda model, contents, config: make_mock_response(
        [make_vector(0.1, 768) for _ in contents]
    )

    client = VertexEmbeddingsClient(client=mock_genai)
    results = client.embed_texts(input_texts, batch_size=batch_size)

    assert len(results) == 50
    assert mock_genai.models.embed_content.call_count == 2


# ============================================================================
# 3. Input Validation Tests
# ============================================================================


@pytest.mark.parametrize(
    "empty_val",
    [
        "",
        "   ",
        "\t\n\r",
    ],
)
def test_embed_text_empty_rejected(empty_val):
    """Verify empty or whitespace-only text raises EmptyTextEmbeddingError."""
    mock_genai = MagicMock()
    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(EmptyTextEmbeddingError, match="empty or whitespace-only"):
        client.embed_text(empty_val)

    with pytest.raises(EmptyTextEmbeddingError, match="empty or whitespace-only"):
        client.embed_texts(["Valid text 1", empty_val, "Valid text 2"])

    mock_genai.models.embed_content.assert_not_called()


@pytest.mark.parametrize(
    "invalid_input",
    [
        12345,
        None,
        {"text": "dict"},
        b"raw bytes",
    ],
)
def test_embed_text_invalid_types_rejected(invalid_input):
    """Verify non-string inputs raise InvalidInputTextError."""
    mock_genai = MagicMock()
    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(InvalidInputTextError, match="not a string"):
        client.embed_text(invalid_input)

    with pytest.raises(InvalidInputTextError, match="not a string"):
        client.embed_texts(["Valid string", invalid_input])

    mock_genai.models.embed_content.assert_not_called()


def test_embed_texts_invalid_batch_size():
    """Verify batch_size < 1 raises InvalidInputTextError."""
    mock_genai = MagicMock()
    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(InvalidInputTextError, match="batch_size must be >= 1"):
        client.embed_texts(["Valid text"], batch_size=0)

    with pytest.raises(InvalidInputTextError, match="batch_size must be >= 1"):
        client.embed_texts(["Valid text"], batch_size=-5)


# ============================================================================
# 4. Dimensionality & Malformed Response Tests
# ============================================================================


def test_dimensionality_mismatch_raises_error():
    """Verify response with non-768 length raises EmbeddingDimensionalityError."""
    mock_genai = MagicMock()
    # Model returns 512-dim vector instead of expected 768
    wrong_vec = make_vector(0.1, 512)
    mock_genai.models.embed_content.return_value = make_mock_response([wrong_vec])

    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(EmbeddingDimensionalityError, match="invalid dimensionality 512; expected exactly 768"):
        client.embed_text("Test content")


def test_malformed_response_none_raised():
    """Verify None response raises EmbeddingResponseMalformedError."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.return_value = None

    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(EmbeddingResponseMalformedError, match="Received null response"):
        client.embed_text("Test content")


def test_malformed_response_missing_embeddings_attribute():
    """Verify response missing 'embeddings' raises EmbeddingResponseMalformedError."""
    mock_genai = MagicMock()
    resp = MagicMock(spec=[])  # Empty object with no attributes
    mock_genai.models.embed_content.return_value = resp

    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(EmbeddingResponseMalformedError, match="'embeddings' attribute is missing"):
        client.embed_text("Test content")


def test_malformed_response_count_mismatch():
    """Verify response with fewer embeddings than requested raises error."""
    mock_genai = MagicMock()
    # Sent 2 texts, but response contains only 1 embedding
    mock_genai.models.embed_content.return_value = make_mock_response([make_vector(0.1, 768)])

    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(EmbeddingResponseMalformedError, match="Response count mismatch: expected 2 embeddings, received 1"):
        client.embed_texts(["Text 1", "Text 2"])


def test_malformed_response_non_numeric_values():
    """Verify non-numeric items in vector raise error."""
    mock_genai = MagicMock()
    item = MagicMock()
    item.values = ["not_a_float"] * 768
    resp = MagicMock()
    resp.embeddings = [item]
    mock_genai.models.embed_content.return_value = resp

    client = VertexEmbeddingsClient(client=mock_genai)

    with pytest.raises(EmbeddingResponseMalformedError, match="contains non-numeric values"):
        client.embed_text("Test text")


# ============================================================================
# 5. Retry, Backoff, and Failure Tests
# ============================================================================


def test_transient_rate_limit_429_retries_and_succeeds():
    """Verify transient 429 retries with backoff and succeeds on subsequent attempt."""
    mock_genai = MagicMock()
    expected_vec = make_vector(0.99, 768)

    # Fail twice with 429, then succeed
    mock_genai.models.embed_content.side_effect = [
        MockAPIError(429, "Resource exhausted (quota exceeded)"),
        MockAPIError(429, "Rate limit exceeded"),
        make_mock_response([expected_vec]),
    ]

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=3,
        initial_backoff=0.2,
        sleeper=mock_sleeper,
    )

    result = client.embed_text("Test query")

    assert result == expected_vec
    assert mock_genai.models.embed_content.call_count == 3
    assert mock_sleeper.call_count == 2
    # Verify exponential backoff: attempt 0 backoff ~0.2, attempt 1 backoff ~0.4
    first_sleep = mock_sleeper.call_args_list[0][0][0]
    second_sleep = mock_sleeper.call_args_list[1][0][0]
    assert 0.2 <= first_sleep < 0.35
    assert 0.4 <= second_sleep < 0.55


def test_transient_server_error_503_retries_and_succeeds():
    """Verify transient 503 retries and succeeds."""
    mock_genai = MagicMock()
    expected_vec = make_vector(0.5, 768)

    mock_genai.models.embed_content.side_effect = [
        MockAPIError(503, "The service is temporarily unavailable"),
        make_mock_response([expected_vec]),
    ]

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=3,
        sleeper=mock_sleeper,
    )

    result = client.embed_text("Test query")

    assert result == expected_vec
    assert mock_genai.models.embed_content.call_count == 2
    assert mock_sleeper.call_count == 1


def test_rate_limit_429_exhaustion_raises_typed_error():
    """Verify repeated 429 errors exhaust retries and raise EmbeddingRateLimitError."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.side_effect = MockAPIError(429, "Rate limit reached")

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=2,
        sleeper=mock_sleeper,
    )

    with pytest.raises(EmbeddingRateLimitError, match="Rate limit exceeded \\(HTTP 429\\)"):
        client.embed_text("Test query")

    # 1 initial + 2 retries = 3 total calls
    assert mock_genai.models.embed_content.call_count == 3
    assert mock_sleeper.call_count == 2


def test_server_error_5xx_exhaustion_raises_typed_error():
    """Verify repeated 500 errors exhaust retries and raise EmbeddingServiceError."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.side_effect = MockAPIError(500, "Internal Server Error")

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=2,
        sleeper=mock_sleeper,
    )

    with pytest.raises(EmbeddingServiceError, match="Vertex AI embedding service error \\(code=500\\)"):
        client.embed_text("Test query")

    assert mock_genai.models.embed_content.call_count == 3
    assert mock_sleeper.call_count == 2


def test_non_retryable_404_model_not_found():
    """Verify HTTP 404 raises EmbeddingModelNotFoundError immediately without retries."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.side_effect = MockAPIError(404, "Model 'text-embedding-999' not found")

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=3,
        sleeper=mock_sleeper,
    )

    with pytest.raises(EmbeddingModelNotFoundError, match="not found"):
        client.embed_text("Test query")

    assert mock_genai.models.embed_content.call_count == 1
    mock_sleeper.assert_not_called()


def test_non_retryable_403_permission_denied():
    """Verify HTTP 403 raises EmbeddingAccessDeniedError immediately without retries."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.side_effect = MockAPIError(403, "Permission denied for caller identity")

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=3,
        sleeper=mock_sleeper,
    )

    with pytest.raises(EmbeddingAccessDeniedError, match="Permission denied"):
        client.embed_text("Test query")

    assert mock_genai.models.embed_content.call_count == 1
    mock_sleeper.assert_not_called()


def test_non_retryable_400_invalid_argument():
    """Verify HTTP 400 raises InvalidInputTextError immediately without retries."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.side_effect = MockAPIError(400, "Invalid argument: text too long")

    mock_sleeper = MagicMock()
    client = VertexEmbeddingsClient(
        client=mock_genai,
        max_retries=3,
        sleeper=mock_sleeper,
    )

    with pytest.raises(InvalidInputTextError, match="Invalid embedding request payload"):
        client.embed_text("Test query")

    assert mock_genai.models.embed_content.call_count == 1
    mock_sleeper.assert_not_called()
