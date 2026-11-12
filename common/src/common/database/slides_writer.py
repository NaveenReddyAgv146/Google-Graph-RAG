"""Atomic and idempotent Spanner persistence for Google Slides presentations, chunks, and visual assets."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from google.cloud import spanner

from common.models.schema import ChunkNode, DocumentNode, VisualAssetNode
from common.parsers.slides_extractor import NormalizedPresentation

logger = logging.getLogger("common.database.slides_writer")


class SlidesPersistenceError(Exception):
    """Base exception for slides persistence errors."""

    pass


class SchemaValidationError(SlidesPersistenceError):
    """Raised when document, chunk, or visual asset fields violate Spanner schema bounds."""

    pass


class OwnershipConflictError(SlidesPersistenceError):
    """Base exception for cross-document ID ownership conflicts."""

    pass


class ChunkOwnershipConflictError(OwnershipConflictError):
    """Raised when a chunk ID matches an existing chunk belonging to a different DocumentId."""

    pass


class VisualAssetOwnershipConflictError(OwnershipConflictError):
    """Raised when a visual asset ID matches an existing visual asset belonging to a different DocumentId."""

    pass


@dataclass(frozen=True)
class SlidesWriteResult:
    """Detailed summary of the atomic slides persistence transaction."""

    document_id: str
    document_action: str  # "INSERTED", "UPDATED", "UNCHANGED"
    chunks_inserted: int
    chunks_updated: int
    chunks_unchanged: int
    visual_assets_inserted: int
    visual_assets_updated: int
    visual_assets_unchanged: int
    chunk_edges_inserted: int
    chunk_edges_unchanged: int
    visual_edges_inserted: int
    visual_edges_unchanged: int
    total_mutations: int
    chunk_ids: list[str] = field(default_factory=list)
    visual_ids: list[str] = field(default_factory=list)


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
    if chunk.slide_number is not None and not isinstance(chunk.slide_number, int):
        raise SchemaValidationError(
            f"SlideNumber must be an integer, got: {type(chunk.slide_number)}"
        )
    if chunk.embedding_768 is not None:
        raise SchemaValidationError(
            "Phase 3B Invariant Violation: embedding_768 must be None (embeddings deferred to Phase 3D)."
        )


def validate_visual_asset_fields(asset: Any) -> None:
    """Validates visual asset fields against Phase 2C Spanner schema constraints."""
    v_id = getattr(asset, "visual_id", None)
    if not v_id or len(v_id) > 64:
        raise SchemaValidationError(
            f"Invalid VisualId length ({len(v_id) if v_id else 0}): must be 1-64 chars."
        )
    doc_id = getattr(asset, "document_id", None)
    if not doc_id or len(doc_id) > 256:
        raise SchemaValidationError(
            f"Invalid DocumentId in VisualAssetNode ({len(doc_id) if doc_id else 0}): max 256 chars."
        )
    uri = getattr(asset, "image_gcs_uri", None)
    if not uri or len(uri) > 1024:
        raise SchemaValidationError(
            f"Invalid ImageGcsUri length ({len(uri) if uri else 0}): must be 1-1024 chars."
        )
    a_type = getattr(asset, "asset_type", None)
    if a_type and len(a_type) > 32:
        raise SchemaValidationError(
            f"Invalid AssetType length ({len(a_type)}): max 32 chars."
        )
    slide_num = getattr(asset, "slide_number", None)
    if slide_num is not None and not isinstance(slide_num, int):
        raise SchemaValidationError(
            f"SlideNumber in VisualAssetNode must be an integer, got: {type(slide_num)}"
        )


def persist_normalized_presentation(
    database: Any,
    normalized_presentation: NormalizedPresentation,
) -> SlidesWriteResult:
    """Atomically and idempotently persists a NormalizedPresentation into Cloud Spanner.

    Invariants Enforced:
    1. Entire operation executes inside a single serializable read-write transaction.
    2. Zero duplicates created on identical reruns (idempotent, 0 mutations).
    3. Persists across all five tables: Documents, EvidenceChunks, VisualAssets,
       DocumentContainsChunk, and VisualAssetBelongsToDoc.
    4. Handles existing document metadata without overwriting unowned fields.
    5. Preserves existing chunk embeddings without modification or erasure.
    6. Leaves Embedding768 strictly NULL on new chunks.
    7. Retries Wound-Wait lock conflicts automatically via database.run_in_transaction.
    8. Enforces Ownership Protection for both EvidenceChunks and VisualAssets before mutations.
    9. Never deletes historical or unrelated records.
    """
    if not isinstance(normalized_presentation, NormalizedPresentation):
        raise SchemaValidationError(
            f"Expected NormalizedPresentation, got {type(normalized_presentation).__name__}"
        )

    doc_node = normalized_presentation.document_node
    chunks = normalized_presentation.chunks
    visual_assets = normalized_presentation.visual_assets

    # Pre-transaction schema validation
    if not chunks:
        raise SchemaValidationError(
            f"Cannot persist NormalizedPresentation for '{doc_node.document_id}' with 0 chunks: "
            "empty extraction indicates failure."
        )
    validate_document_fields(doc_node)
    for chk in chunks:
        validate_chunk_fields(chk)
    for vis in visual_assets:
        validate_visual_asset_fields(vis)

    def _txn_callback(transaction: Any) -> SlidesWriteResult:
        doc_id = doc_node.document_id

        # ====================================================================
        # Phase 1: Reads & Strict Ownership Verification (BEFORE any writes)
        # ====================================================================

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

        # 2. Read existing EvidenceChunks and verify Ownership Protection
        existing_chunks: dict[str, tuple[str, str, str, int | None]] = {}
        chunk_ids = [chk.chunk_id for chk in chunks]
        if chunk_ids:
            chunk_sql = """
            SELECT ChunkId, DocumentId, TextContent, Modality, SlideNumber
            FROM EvidenceChunks
            WHERE ChunkId IN UNNEST(@chunk_ids)
            """
            for r in transaction.execute_sql(
                chunk_sql,
                params={"chunk_ids": chunk_ids},
                param_types={"chunk_ids": spanner.param_types.Array(spanner.param_types.STRING)},
            ):
                c_id, stored_doc_id, text, modality, slide_num = r[0], r[1], r[2], r[3], r[4]
                if stored_doc_id != doc_id:
                    raise ChunkOwnershipConflictError(
                        f"Chunk '{c_id}' is already owned by Document '{stored_doc_id}', "
                        f"cannot be reassigned or overwritten by Document '{doc_id}'."
                    )
                existing_chunks[c_id] = (stored_doc_id, text, modality, slide_num)

        # 3. Read existing VisualAssets and verify Ownership Protection
        existing_visuals: dict[str, tuple[str, str, str | None, str | None, int | None]] = {}
        visual_ids = [vis.visual_id for vis in visual_assets]
        if visual_ids:
            vis_sql = """
            SELECT VisualId, DocumentId, ImageGcsUri, Description, AssetType, SlideNumber
            FROM VisualAssets
            WHERE VisualId IN UNNEST(@visual_ids)
            """
            for r in transaction.execute_sql(
                vis_sql,
                params={"visual_ids": visual_ids},
                param_types={"visual_ids": spanner.param_types.Array(spanner.param_types.STRING)},
            ):
                v_id, stored_doc_id, uri, desc, a_type, slide_num = (
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                )
                if stored_doc_id != doc_id:
                    raise VisualAssetOwnershipConflictError(
                        f"VisualAsset '{v_id}' is already owned by Document '{stored_doc_id}', "
                        f"cannot be reassigned or overwritten by Document '{doc_id}'."
                    )
                existing_visuals[v_id] = (stored_doc_id, uri, desc, a_type, slide_num)

        # 4. Read existing DocumentContainsChunk edges
        chunk_edge_sql = """
        SELECT ChunkId
        FROM DocumentContainsChunk
        WHERE DocumentId = @doc_id
        """
        existing_chunk_edges = set(
            r[0]
            for r in transaction.execute_sql(
                chunk_edge_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )

        # 5. Read existing VisualAssetBelongsToDoc edges
        visual_edge_sql = """
        SELECT VisualId
        FROM VisualAssetBelongsToDoc
        WHERE DocumentId = @doc_id
        """
        existing_visual_edges = set(
            r[0]
            for r in transaction.execute_sql(
                visual_edge_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )

        # ====================================================================
        # Phase 2: Compute Mutations & Buffer Writes (Only after all validations pass)
        # ====================================================================

        # 1. Documents Table
        doc_action = "UNCHANGED"
        if not existing_doc_rows:
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
            ex_row = existing_doc_rows[0]
            ex_file_name, _, ex_src_url, ex_gcs, ex_status, ex_drive = (
                ex_row[1],
                ex_row[2],
                ex_row[3],
                ex_row[4],
                ex_row[5],
                ex_row[6],
            )

            needs_update = False
            upd_columns = ["DocumentId"]
            upd_values = [doc_id]

            if doc_node.file_name and doc_node.file_name != ex_file_name:
                upd_columns.append("FileName")
                upd_values.append(doc_node.file_name)
                needs_update = True
            if doc_node.source_url and doc_node.source_url != ex_src_url:
                upd_columns.append("SourceUrl")
                upd_values.append(doc_node.source_url)
                needs_update = True
            if doc_node.gcs_uri and doc_node.gcs_uri != ex_gcs:
                upd_columns.append("GcsUri")
                upd_values.append(doc_node.gcs_uri)
                needs_update = True
            if doc_node.status and doc_node.status != ex_status:
                upd_columns.append("Status")
                upd_values.append(doc_node.status)
                needs_update = True
            if doc_node.drive_file_id and doc_node.drive_file_id != ex_drive:
                upd_columns.append("DriveFileId")
                upd_values.append(doc_node.drive_file_id)
                needs_update = True

            if needs_update:
                transaction.update(
                    "Documents",
                    columns=upd_columns,
                    values=[upd_values],
                )
                doc_action = "UPDATED"

        # 2. EvidenceChunks Table
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
                _, ex_text, ex_modality, ex_slide = existing_chunks[chk.chunk_id]
                if (
                    chk.text_content != ex_text
                    or chk.modality != ex_modality
                    or chk.slide_number != ex_slide
                ):
                    updated_chunks_values.append(
                        (
                            chk.chunk_id,
                            chk.text_content,
                            chk.modality,
                            chk.slide_number,
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
                columns=["ChunkId", "TextContent", "Modality", "SlideNumber"],
                values=updated_chunks_values,
            )

        # 3. VisualAssets Table
        visual_assets_inserted = 0
        visual_assets_updated = 0
        visual_assets_unchanged = 0

        new_visuals_values = []
        updated_visuals_values = []

        for vis in visual_assets:
            if vis.visual_id not in existing_visuals:
                new_visuals_values.append(
                    (
                        vis.visual_id,
                        vis.document_id,
                        vis.page_number,
                        vis.slide_number,
                        vis.image_gcs_uri,
                        vis.description,
                        vis.asset_type,
                    )
                )
                visual_assets_inserted += 1
            else:
                _, ex_uri, ex_desc, ex_type, ex_slide = existing_visuals[vis.visual_id]
                if (
                    vis.image_gcs_uri != ex_uri
                    or vis.description != ex_desc
                    or vis.asset_type != ex_type
                    or vis.slide_number != ex_slide
                ):
                    updated_visuals_values.append(
                        (
                            vis.visual_id,
                            vis.image_gcs_uri,
                            vis.description,
                            vis.asset_type,
                            vis.slide_number,
                        )
                    )
                    visual_assets_updated += 1
                else:
                    visual_assets_unchanged += 1

        if new_visuals_values:
            transaction.insert(
                "VisualAssets",
                columns=[
                    "VisualId",
                    "DocumentId",
                    "PageNumber",
                    "SlideNumber",
                    "ImageGcsUri",
                    "Description",
                    "AssetType",
                ],
                values=new_visuals_values,
            )

        if updated_visuals_values:
            transaction.update(
                "VisualAssets",
                columns=["VisualId", "ImageGcsUri", "Description", "AssetType", "SlideNumber"],
                values=updated_visuals_values,
            )

        # 4. DocumentContainsChunk Edge Table
        chunk_edges_inserted = 0
        chunk_edges_unchanged = 0
        new_chunk_edge_values = []

        for chk in chunks:
            if chk.chunk_id not in existing_chunk_edges:
                new_chunk_edge_values.append((doc_id, chk.chunk_id))
                chunk_edges_inserted += 1
            else:
                chunk_edges_unchanged += 1

        if new_chunk_edge_values:
            transaction.insert(
                "DocumentContainsChunk",
                columns=["DocumentId", "ChunkId"],
                values=new_chunk_edge_values,
            )

        # 5. VisualAssetBelongsToDoc Edge Table
        visual_edges_inserted = 0
        visual_edges_unchanged = 0
        new_visual_edge_values = []

        for vis in visual_assets:
            if vis.visual_id not in existing_visual_edges:
                new_visual_edge_values.append((vis.visual_id, doc_id))
                visual_edges_inserted += 1
            else:
                visual_edges_unchanged += 1

        if new_visual_edge_values:
            transaction.insert(
                "VisualAssetBelongsToDoc",
                columns=["VisualId", "DocumentId"],
                values=new_visual_edge_values,
            )

        # Total mutations calculated across all 5 tables
        total_mutations = (
            (1 if doc_action in ("INSERTED", "UPDATED") else 0)
            + chunks_inserted
            + chunks_updated
            + visual_assets_inserted
            + visual_assets_updated
            + chunk_edges_inserted
            + visual_edges_inserted
        )

        logger.info(
            f"Slides presentation '{doc_id}' persisted: doc={doc_action}, "
            f"chunks(+{chunks_inserted}, ~{chunks_updated}, ={chunks_unchanged}), "
            f"visual_assets(+{visual_assets_inserted}, ~{visual_assets_updated}, ={visual_assets_unchanged}), "
            f"chunk_edges(+{chunk_edges_inserted}, ={chunk_edges_unchanged}), "
            f"visual_edges(+{visual_edges_inserted}, ={visual_edges_unchanged}), "
            f"total_mutations={total_mutations}"
        )

        return SlidesWriteResult(
            document_id=doc_id,
            document_action=doc_action,
            chunks_inserted=chunks_inserted,
            chunks_updated=chunks_updated,
            chunks_unchanged=chunks_unchanged,
            visual_assets_inserted=visual_assets_inserted,
            visual_assets_updated=visual_assets_updated,
            visual_assets_unchanged=visual_assets_unchanged,
            chunk_edges_inserted=chunk_edges_inserted,
            chunk_edges_unchanged=chunk_edges_unchanged,
            visual_edges_inserted=visual_edges_inserted,
            visual_edges_unchanged=visual_edges_unchanged,
            total_mutations=total_mutations,
            chunk_ids=chunk_ids,
            visual_ids=visual_ids,
        )

    return database.run_in_transaction(_txn_callback)
