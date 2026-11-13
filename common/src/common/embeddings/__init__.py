"""Vertex AI Embeddings module for Enterprise GraphRAG."""

from common.embeddings.vertex_client import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_INITIAL_BACKOFF_SECONDS,
    DEFAULT_MAX_RETRIES,
    EmbeddingAccessDeniedError,
    EmbeddingDimensionalityError,
    EmbeddingModelNotFoundError,
    EmbeddingRateLimitError,
    EmbeddingResponseMalformedError,
    EmbeddingServiceError,
    EmptyTextEmbeddingError,
    InvalidInputTextError,
    VertexEmbeddingsClient,
    VertexEmbeddingsError,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_INITIAL_BACKOFF_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "EmbeddingAccessDeniedError",
    "EmbeddingDimensionalityError",
    "EmbeddingModelNotFoundError",
    "EmbeddingRateLimitError",
    "EmbeddingResponseMalformedError",
    "EmbeddingServiceError",
    "EmptyTextEmbeddingError",
    "InvalidInputTextError",
    "VertexEmbeddingsClient",
    "VertexEmbeddingsError",
]
