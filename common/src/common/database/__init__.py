"""Spanner database client, connection pool, and ingestion persistence utilities."""

from common.database.ingestion_writer import (
    IngestionPersistenceError,
    IngestionWriteResult,
    SchemaValidationError,
    persist_normalized_document,
    validate_chunk_fields,
    validate_document_fields,
)
from common.database.spanner import (
    execute_gql_query,
    execute_read_write_transaction,
    get_database,
    get_spanner_client,
)

__all__ = [
    "IngestionPersistenceError",
    "IngestionWriteResult",
    "SchemaValidationError",
    "execute_gql_query",
    "execute_read_write_transaction",
    "get_database",
    "get_spanner_client",
    "persist_normalized_document",
    "validate_chunk_fields",
    "validate_document_fields",
]
