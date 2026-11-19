"""Spanner database client, connection pool, and ingestion persistence utilities."""

from common.database.enrichment_writer import (
    ApprovedSolutionCardProtectionError,
    DocumentNotFoundError,
    EnrichmentPayload,
    EnrichmentPersistenceError,
    EnrichmentWriteResult,
    EnrichmentWriter,
    ReferentialIntegrityError,
    SchemaConstraintError,
    format_okf_uri,
    generate_evidence_link_id,
)
from common.database.ingestion_writer import (
    ChunkOwnershipConflictError,
    IngestionPersistenceError,
    IngestionWriteResult,
    SchemaValidationError,
    persist_normalized_document,
    validate_chunk_fields,
    validate_document_fields,
)
from common.database.slides_writer import (
    OwnershipConflictError,
    SlidesPersistenceError,
    SlidesWriteResult,
    VisualAssetOwnershipConflictError,
    persist_normalized_presentation,
    validate_visual_asset_fields,
)
from common.database.spanner import (
    execute_gql_query,
    execute_read_write_transaction,
    get_database,
    get_spanner_client,
)

__all__ = [
    "ApprovedSolutionCardProtectionError",
    "ChunkOwnershipConflictError",
    "DocumentNotFoundError",
    "EnrichmentPayload",
    "EnrichmentPersistenceError",
    "EnrichmentWriteResult",
    "EnrichmentWriter",
    "IngestionPersistenceError",
    "IngestionWriteResult",
    "OwnershipConflictError",
    "ReferentialIntegrityError",
    "SchemaConstraintError",
    "SchemaValidationError",
    "SlidesPersistenceError",
    "SlidesWriteResult",
    "VisualAssetOwnershipConflictError",
    "execute_gql_query",
    "execute_read_write_transaction",
    "format_okf_uri",
    "generate_evidence_link_id",
    "get_database",
    "get_spanner_client",
    "persist_normalized_document",
    "persist_normalized_presentation",
    "validate_chunk_fields",
    "validate_document_fields",
    "validate_visual_asset_fields",
]
