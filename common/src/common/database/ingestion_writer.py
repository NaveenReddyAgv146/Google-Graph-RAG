"""Idempotent Spanner persistence for normalized documents, chunks, and graph edges."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from google.cloud import spanner

from common.models.schema import ChunkNode, DocumentNode
from common.parsers.layout_extractor import NormalizedDocument

logger = logging.getLogger("common.database.ingestion_writer")


class IngestionPersistenceError(Exception):
    """Base exception for document and chunk persistence errors."""

    pass


class SchemaValidationError(IngestionPersistenceError):
    """Raised when document or chunk fields violate Spanner schema bounds."""

    pass


@dataclass(frozen=True)
class IngestionWriteResult:
    """Detailed summary of the atomic persistence transaction."""

    document_id: str
    document_action: str  # "INSERTED", "UPDATED", "UNCHANGED"
    chunks_inserted: int
    chunks_updated: int
    chunks_unchanged: int
    edges_inserted: int
    edges_unchanged: int
    total_mutations: int


def validate_document_fields(doc: DocumentNode) -> None:
    """Validates document fields against Phase 2C Spanner schema constraints."""
    if not doc.document_id or len(doc.document_id) > 256:
        raise SchemaValidationError(
            f"Invalid DocumentId length ({len(doc.document_id)}): must be 1-256 chars."
        )
    if doc.file_name and len(doc.file_name) > 256:
        raise SchemaValidationError(
            f"Invalid FileName length ({len(doc.file_name)}): max 256 chars."
        )
    if doc.mime_type and len(doc.mime_type) > 128:
        raise SchemaValidationError(
            f"Invalid MimeType length ({len(doc.mime_type)}): max 128 chars."
        )
    if doc.source_url and len(doc.source_url) > 1024:
        raise SchemaValidationError(
            f"Invalid SourceUrl length ({len(doc.source_url)}): max 1024 chars."
        )
    if doc.gcs_uri and len(doc.gcs_uri) > 1024:
        raise SchemaValidationError(
            f"Invalid GcsUri length ({len(doc.gcs_uri)}): max 1024 chars."
        )
    if doc.status and len(doc.status) > 32:
        raise SchemaValidationError(
            f"Invalid Status length ({len(doc.status)}): max 32 chars."
        )
    if doc.repo_id and len(doc.repo_id) > 64:
        raise SchemaValidationError(
            f"Invalid RepoId length ({len(doc.repo_id)}): max 64 chars."
        )
    if doc.drive_file_id and len(doc.drive_file_id) > 256:
        raise SchemaValidationError(
            f"Invalid DriveFileId length ({len(doc.drive_file_id)}): max 256 chars."
        )


def validate_chunk_fields(chunk: ChunkNode) -> None:
    """Validates chunk fields against Phase 2C Spanner schema constraints."""
    if not chunk.chunk_id or len(chunk.chunk_id) > 64:
        raise SchemaValidationError(
            f"Invalid ChunkId length ({len(chunk.chunk_id)}): must be 1-64 chars."
        )
    if not chunk.document_id or len(chunk.document_id) > 256:
        raise SchemaValidationError(
            f"Invalid DocumentId in ChunkNode ({len(chunk.document_id)}): max 256 chars."
        )
    if chunk.file_id and len(chunk.file_id) > 64:
        raise SchemaValidationError(
            f"Invalid FileId length ({len(chunk.file_id)}): max 64 chars."
        )
    if chunk.modality and len(chunk.modality) > 32:
        raise SchemaValidationError(
            f"Invalid Modality length ({len(chunk.modality)}): max 32 chars."
        )
    if chunk.commit_sha and len(chunk.commit_sha) > 64:
        raise SchemaValidationError(
            f"Invalid CommitSha length ({len(chunk.commit_sha)}): max 64 chars."
        )
    if chunk.line_range and len(chunk.line_range) > 64:
        raise SchemaValidationError(
            f"Invalid LineRange length ({len(chunk.line_range)}): max 64 chars."
        )
    if chunk.source_gcs_uri and len(chunk.source_gcs_uri) > 1024:
        raise SchemaValidationError(
            f"Invalid SourceGcsUri length ({len(chunk.source_gcs_uri)}): max 1024 chars."
        )
    if chunk.page_number is not None and not isinstance(chunk.page_number, int):
        raise SchemaValidationError(
            f"PageNumber must be an integer, got: {type(chunk.page_number)}"
        )
    if chunk.embedding_768 is not None:
        raise SchemaValidationError(
            "Phase 3A Invariant Violation: embedding_768 must be None (embeddings deferred to Phase 3D)."
        )


def persist_normalized_document(
    database: Any,
    normalized_doc: NormalizedDocument,
) -> IngestionWriteResult:
    """Atomically and idempotently persists a NormalizedDocument into Cloud Spanner.

    Invariants Enforced:
    1. Entire operation executes inside a single serializable read-write transaction.
    2. Zero duplicates created on identical reruns.
    3. Handles existing document metadata (DriveFileId, RepoId) without overwriting.
    4. Handles existing chunks without erasing vector embeddings.
    5. Leaves Embedding768 strictly NULL on new chunks.
    6. Automatically retries Wound-Wait lock conflicts via database.run_in_transaction.
    """
    doc_node = normalized_doc.document_node
    chunks = normalized_doc.chunks

    # Pre-transaction schema validation
    validate_document_fields(doc_node)
    for chk in chunks:
        validate_chunk_fields(chk)

    def _txn_callback(transaction: Any) -> IngestionWriteResult:
        doc_id = doc_node.document_id

        # 1. Read existing document
        doc_sql = """
        SELECT DocumentId, FileName, MimeType, SourceUrl, GcsUri, Status, DriveFileId, RepoId
        FROM Documents
        WHERE DocumentId = @doc_id
        """
        existing_doc_rows = list(
            transaction.execute_sql(
                doc_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )

        doc_action = "UNCHANGED"
        if not existing_doc_rows:
            # Insert new document
            transaction.insert(
                "Documents",
                columns=[
                    "DocumentId",
                    "FileName",
                    "MimeType",
                    "SourceUrl",
                    "GcsUri",
                    "Status",
                    "DriveFileId",
                    "RepoId",
                ],
                values=[
                    (
                        doc_node.document_id,
                        doc_node.file_name,
                        doc_node.mime_type,
                        doc_node.source_url,
                        doc_node.gcs_uri,
                        doc_node.status,
                        doc_node.drive_file_id,
                        doc_node.repo_id,
                    )
                ],
            )
            doc_action = "INSERTED"
        else:
            # Inspect existing fields; update only if content or status changed,
            # never clobbering unrelated metadata like DriveFileId or RepoId
            ex_row = existing_doc_rows[0]
            ex_file_name, _, _, ex_gcs, ex_status = (
                ex_row[1],
                ex_row[2],
                ex_row[3],
                ex_row[4],
                ex_row[5],
            )

            needs_update = False
            upd_columns = ["DocumentId"]
            upd_values = [doc_id]

            if doc_node.file_name and doc_node.file_name != ex_file_name:
                upd_columns.append("FileName")
                upd_values.append(doc_node.file_name)
                needs_update = True
            if doc_node.gcs_uri and doc_node.gcs_uri != ex_gcs:
                upd_columns.append("GcsUri")
                upd_values.append(doc_node.gcs_uri)
                needs_update = True
            if doc_node.status and doc_node.status != ex_status:
                upd_columns.append("Status")
                upd_values.append(doc_node.status)
                needs_update = True

            if needs_update:
                transaction.update(
                    "Documents",
                    columns=upd_columns,
                    values=[upd_values],
                )
                doc_action = "UPDATED"

        # 2. Read existing chunks for this document
        chunk_sql = """
        SELECT ChunkId, TextContent, Modality, PageNumber
        FROM EvidenceChunks
        WHERE DocumentId = @doc_id
        """
        existing_chunks = {
            r[0]: (r[1], r[2], r[3])
            for r in transaction.execute_sql(
                chunk_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        }

        chunks_inserted = 0
        chunks_updated = 0
        chunks_unchanged = 0

        new_chunks_values = []
        updated_chunks_values = []

        for chk in chunks:
            if chk.chunk_id not in existing_chunks:
                new_chunks_values.append(
                    (
                        chk.chunk_id,
                        chk.document_id,
                        chk.file_id,
                        chk.modality,
                        chk.text_content,
                        chk.page_number,
                        chk.slide_number,
                        chk.commit_sha,
                        chk.line_range,
                        chk.source_gcs_uri,
                        None,  # Embedding768 strictly NULL
                    )
                )
                chunks_inserted += 1
            else:
                ex_text, ex_modality, ex_page = existing_chunks[chk.chunk_id]
                if (
                    chk.text_content != ex_text
                    or chk.modality != ex_modality
                    or chk.page_number != ex_page
                ):
                    # Update content without touching existing Embedding768
                    updated_chunks_values.append(
                        (
                            chk.chunk_id,
                            chk.text_content,
                            chk.modality,
                            chk.page_number,
                        )
                    )
                    chunks_updated += 1
                else:
                    chunks_unchanged += 1

        if new_chunks_values:
            transaction.insert(
                "EvidenceChunks",
                columns=[
                    "ChunkId",
                    "DocumentId",
                    "FileId",
                    "Modality",
                    "TextContent",
                    "PageNumber",
                    "SlideNumber",
                    "CommitSha",
                    "LineRange",
                    "SourceGcsUri",
                    "Embedding768",
                ],
                values=new_chunks_values,
            )

        if updated_chunks_values:
            transaction.update(
                "EvidenceChunks",
                columns=["ChunkId", "TextContent", "Modality", "PageNumber"],
                values=updated_chunks_values,
            )

        # 3. Read existing DocumentContainsChunk edges
        edge_sql = """
        SELECT ChunkId
        FROM DocumentContainsChunk
        WHERE DocumentId = @doc_id
        """
        existing_edge_chunks = set(
            r[0]
            for r in transaction.execute_sql(
                edge_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )

        edges_inserted = 0
        edges_unchanged = 0
        new_edge_values = []

        for chk in chunks:
            if chk.chunk_id not in existing_edge_chunks:
                new_edge_values.append((doc_id, chk.chunk_id))
                edges_inserted += 1
            else:
                edges_unchanged += 1

        if new_edge_values:
            transaction.insert(
                "DocumentContainsChunk",
                columns=["DocumentId", "ChunkId"],
                values=new_edge_values,
            )

        total_mutations = (
            (1 if doc_action in ("INSERTED", "UPDATED") else 0)
            + chunks_inserted
            + chunks_updated
            + edges_inserted
        )

        logger.info(
            f"Document '{doc_id}' persisted: doc={doc_action}, "
            f"chunks(+{chunks_inserted}, ~{chunks_updated}, ={chunks_unchanged}), "
            f"edges(+{edges_inserted}, ={edges_unchanged}), total_mutations={total_mutations}"
        )

        return IngestionWriteResult(
            document_id=doc_id,
            document_action=doc_action,
            chunks_inserted=chunks_inserted,
            chunks_updated=chunks_updated,
            chunks_unchanged=chunks_unchanged,
            edges_inserted=edges_inserted,
            edges_unchanged=edges_unchanged,
            total_mutations=total_mutations,
        )

    return database.run_in_transaction(_txn_callback)
