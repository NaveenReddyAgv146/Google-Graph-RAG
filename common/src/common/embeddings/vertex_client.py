"""Vertex AI Embeddings client with ADC authentication, bounded retries, and batching.

Generates 768-dimensional text embeddings for EvidenceChunks using Vertex AI
embedding models (e.g., text-embedding-005 or text-embedding-004).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Sequence

from common.config import Settings, settings as global_settings

logger = logging.getLogger("common.embeddings.vertex_client")

DEFAULT_BATCH_SIZE = 25
DEFAULT_EMBEDDING_DIMENSIONS = 768
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# ============================================================================
# Typed Domain Exceptions
# ============================================================================


class VertexEmbeddingsError(Exception):
    """Base exception for Vertex AI embeddings operations."""

    pass


class InvalidInputTextError(VertexEmbeddingsError, ValueError):
    """Raised when input text payload is invalid (non-string or malformed)."""

    pass


class EmptyTextEmbeddingError(VertexEmbeddingsError, ValueError):
    """Raised when input text is empty or whitespace-only."""

    pass


class EmbeddingDimensionalityError(VertexEmbeddingsError, ValueError):
    """Raised when generated embedding does not match expected dimensions (768)."""

    pass


class EmbeddingModelNotFoundError(VertexEmbeddingsError):
    """Raised when the specified embedding model is not found (HTTP 404)."""

    pass


class EmbeddingAccessDeniedError(VertexEmbeddingsError):
    """Raised when caller lacks permission to use Vertex AI embedding model (HTTP 403)."""

    pass


class EmbeddingRateLimitError(VertexEmbeddingsError):
    """Raised when embedding API rate limits (HTTP 429) are exhausted after retries."""

    pass


class EmbeddingServiceError(VertexEmbeddingsError):
    """Raised when transient Vertex AI errors (HTTP 5xx) persist after retry exhaustion."""

    pass


class EmbeddingResponseMalformedError(VertexEmbeddingsError):
    """Raised when the API response structure is missing or malformed."""

    pass


# ============================================================================
# Client Implementation
# ============================================================================


class VertexEmbeddingsClient:
    """Client for generating 768-dimensional embeddings via Google Vertex AI.

    Invariants:
    1. Fixed Dimensionality: Strictly generates and enforces 768-dimensional embeddings.
    2. Input Validation: Explicitly rejects empty, whitespace, or non-string inputs.
    3. Batched Execution: Partitions large inputs into deterministic batches (default size 25).
    4. Ordering Preservation: Output vectors strictly match input text order 1:1.
    5. Bounded Exponential Retries: Retries transient 429 and 5xx errors with jitter.
    6. Offline Testability: Fully supports dependency injection for client, models, and sleeper.
    7. Zero Side Effects: Performs read-only embedding calls; never writes to Spanner or GCS.
    """

    def __init__(
        self,
        client: Any | None = None,
        model_name: str | None = None,
        dimensions: int | None = None,
        project_id: str | None = None,
        location: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or global_settings
        self.project_id = project_id or cfg.project_id
        self.location = location or cfg.region
        self.model_name = model_name or cfg.embedding_model
        self.dimensions = dimensions or cfg.embedding_dimensions or DEFAULT_EMBEDDING_DIMENSIONS
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self._sleeper = sleeper
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily initializes the google.genai Client configured for Vertex AI."""
        if self._client is None:
            from google import genai

            logger.info(
                "Initializing Vertex AI GenAI Client (project=%s, location=%s)",
                self.project_id,
                self.location,
            )
            self._client = genai.Client(
                vertexai=True,
                project=self.project_id,
                location=self.location,
            )
        return self._client

    def _classify_error(self, exc: Exception) -> tuple[int | None, str]:
        """Extracts HTTP status code and message from various Google/GenAI exception types."""
        code: int | None = None
        message = str(exc)

        # Check for google.genai.errors.APIError code attribute
        if hasattr(exc, "code") and isinstance(exc.code, int):
            code = exc.code
        elif hasattr(exc, "status_code") and isinstance(exc.status_code, int):
            code = exc.status_code

        # Fallback inspection of error message for HTTP status codes
        if code is None:
            if "429" in message or "ResourceExhausted" in type(exc).__name__:
                code = 429
            elif "404" in message or "NotFound" in type(exc).__name__:
                code = 404
            elif "403" in message or "PermissionDenied" in type(exc).__name__:
                code = 403
            elif "400" in message or "InvalidArgument" in type(exc).__name__:
                code = 400
            elif "503" in message or "Unavailable" in type(exc).__name__:
                code = 503
            elif "504" in message or "DeadlineExceeded" in type(exc).__name__:
                code = 504
            elif "500" in message or "InternalServerError" in type(exc).__name__:
                code = 500

        return code, message

    def _execute_batch_with_retry(
        self,
        batch_texts: list[str],
        task_type: str | None = None,
    ) -> list[list[float]]:
        """Invokes embed_content on a single batch with bounded retries and exponential backoff."""
        from google.genai import types

        config = types.EmbedContentConfig(
            output_dimensionality=self.dimensions,
            task_type=task_type,
        )

        last_exception: Exception | None = None
        response: Any = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.models.embed_content(
                    model=self.model_name,
                    contents=batch_texts,
                    config=config,
                )
                break
            except (
                EmbeddingDimensionalityError,
                EmbeddingResponseMalformedError,
                EmptyTextEmbeddingError,
                InvalidInputTextError,
            ):
                raise
            except Exception as exc:
                last_exception = exc
                code, msg = self._classify_error(exc)

                # Non-transient client errors: abort immediately without retry
                if code == 404:
                    raise EmbeddingModelNotFoundError(
                        f"Embedding model '{self.model_name}' not found: {msg}"
                    ) from exc
                if code == 403:
                    raise EmbeddingAccessDeniedError(
                        f"Permission denied accessing embedding model '{self.model_name}': {msg}"
                    ) from exc
                if code == 400:
                    raise InvalidInputTextError(
                        f"Invalid embedding request payload: {msg}"
                    ) from exc

                # Check if transient retryable error (429 or 5xx)
                is_retryable = code in RETRYABLE_STATUS_CODES if code else True
                if not is_retryable or attempt >= self.max_retries:
                    logger.error(
                        "Vertex AI embedding call failed after %d/%d retries (code=%s): %s",
                        attempt,
                        self.max_retries,
                        code,
                        msg,
                    )
                    if code == 429:
                        raise EmbeddingRateLimitError(
                            f"Rate limit exceeded (HTTP 429) for model '{self.model_name}' after {attempt} retries: {msg}"
                        ) from exc
                    raise EmbeddingServiceError(
                        f"Vertex AI embedding service error (code={code}) after {attempt} retries: {msg}"
                    ) from exc

                # Calculate backoff with jitter
                backoff = self.initial_backoff * (2**attempt) + random.uniform(0.0, 0.1)
                logger.warning(
                    "Transient Vertex AI error (code=%s, attempt %d/%d). Retrying in %.2fs: %s",
                    code,
                    attempt + 1,
                    self.max_retries,
                    backoff,
                    msg,
                )
                self._sleeper(backoff)
        else:
            raise EmbeddingServiceError(
                f"Vertex AI embedding failed after {self.max_retries} retries: {last_exception}"
            )

        return self._parse_and_validate_batch_response(response, expected_count=len(batch_texts))

    def _parse_and_validate_batch_response(
        self,
        response: Any,
        expected_count: int,
    ) -> list[list[float]]:
        """Validates API response structure, item counts, and embedding dimensionalities."""
        if response is None:
            raise EmbeddingResponseMalformedError("Received null response from Vertex AI embedding API.")

        embeddings_list = getattr(response, "embeddings", None)
        if embeddings_list is None:
            raise EmbeddingResponseMalformedError(
                "Malformed response: 'embeddings' attribute is missing from response."
            )

        if not isinstance(embeddings_list, (list, tuple)):
            raise EmbeddingResponseMalformedError(
                f"Malformed response: 'embeddings' must be a list, got {type(embeddings_list).__name__}."
            )

        if len(embeddings_list) != expected_count:
            raise EmbeddingResponseMalformedError(
                f"Response count mismatch: expected {expected_count} embeddings, received {len(embeddings_list)}."
            )

        validated_vectors: list[list[float]] = []

        for idx, item in enumerate(embeddings_list):
            values = getattr(item, "values", None)
            if values is None and isinstance(item, (list, tuple)):
                values = item
            elif values is None:
                raise EmbeddingResponseMalformedError(
                    f"Malformed embedding item at index {idx}: missing 'values' vector."
                )

            if len(values) != self.dimensions:
                raise EmbeddingDimensionalityError(
                    f"Embedding at index {idx} has invalid dimensionality {len(values)}; "
                    f"expected exactly {self.dimensions} dimensions."
                )

            # Validate that vector components are valid finite floats
            try:
                float_vector = [float(v) for v in values]
            except (TypeError, ValueError) as conv_err:
                raise EmbeddingResponseMalformedError(
                    f"Embedding at index {idx} contains non-numeric values: {conv_err}"
                ) from conv_err

            validated_vectors.append(float_vector)

        return validated_vectors

    def embed_texts(
        self,
        texts: Sequence[str],
        batch_size: int = DEFAULT_BATCH_SIZE,
        task_type: str | None = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """Generates 768-dimensional embeddings for a sequence of texts with automatic batching.

        Args:
            texts: Sequence of non-empty text strings to embed.
            batch_size: Number of texts per API batch call (default 25).
            task_type: Vertex AI embedding task type (default 'RETRIEVAL_DOCUMENT').

        Returns:
            list[list[float]]: List of 768-dimensional float vectors matching input order.

        Raises:
            EmptyTextEmbeddingError: If any input text is empty or whitespace-only.
            InvalidInputTextError: If any input item is not a string or batch_size < 1.
            EmbeddingDimensionalityError: If any generated embedding is not 768-dimensional.
            EmbeddingRateLimitError: If rate limits (429) persist across retries.
            EmbeddingServiceError: If backend errors (5xx) persist across retries.
        """
        if not texts:
            return []

        if batch_size < 1:
            raise InvalidInputTextError(f"batch_size must be >= 1, got {batch_size}.")

        # 1. Validate all input items before making any network calls
        for idx, text in enumerate(texts):
            if not isinstance(text, str):
                raise InvalidInputTextError(
                    f"Input at index {idx} is not a string (type: {type(text).__name__})."
                )
            if not text.strip():
                raise EmptyTextEmbeddingError(
                    f"Input text at index {idx} is empty or whitespace-only. "
                    "Empty text must never be submitted for embedding."
                )

        # 2. Partition into deterministic batches preserving order
        all_embeddings: list[list[float]] = []
        total_texts = len(texts)

        for start_idx in range(0, total_texts, batch_size):
            end_idx = min(start_idx + batch_size, total_texts)
            batch = list(texts[start_idx:end_idx])
            logger.debug(
                "Embedding batch [%d..%d] of %d texts using '%s'",
                start_idx,
                end_idx - 1,
                total_texts,
                self.model_name,
            )
            batch_vectors = self._execute_batch_with_retry(batch, task_type=task_type)
            all_embeddings.extend(batch_vectors)

        # 3. Final invariant assertion: input count == output count
        assert len(all_embeddings) == total_texts, (
            f"Embedding count invariant violation: {len(all_embeddings)} != {total_texts}"
        )

        return all_embeddings

    def embed_text(
        self,
        text: str,
        task_type: str | None = "RETRIEVAL_DOCUMENT",
    ) -> list[float]:
        """Convenience method to generate an embedding for a single text string.

        Args:
            text: Non-empty text string to embed.
            task_type: Vertex AI embedding task type.

        Returns:
            list[float]: Exactly 768-dimensional float vector.
        """
        if not isinstance(text, str):
            raise InvalidInputTextError(f"Input is not a string (type: {type(text).__name__}).")
        if not text.strip():
            raise EmptyTextEmbeddingError("Input text is empty or whitespace-only.")

        results = self.embed_texts([text], batch_size=1, task_type=task_type)
        return results[0]
