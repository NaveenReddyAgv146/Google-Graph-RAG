"""Mocked unit tests for atomic and idempotent Spanner Google Slides persistence."""

from __future__ import annotations

from typing import Any

import pytest
from google.api_core.exceptions import Aborted

from common.database.slides_writer import (
    ChunkOwnershipConflictError,
    OwnershipConflictError,
    SchemaValidationError,
    SlidesPersistenceError,
    SlidesWriteResult,
    VisualAssetOwnershipConflictError,
    persist_normalized_presentation,
    validate_chunk_fields,
    validate_document_fields,
    validate_visual_asset_fields,
)
from common.models.schema import ChunkNode, DocumentNode, VisualAssetNode
from common.parsers.slides_extractor import NormalizedPresentation


# ============================================================================
# MOCKS
# ============================================================================


class MockTransaction:
    """Mock Spanner read-write transaction tracking queries and buffered mutations."""

    def __init__(self, query_responses: dict[str, list[list[Any]]] | None = None):
        self.query_responses = query_responses or {}
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.executed_queries: list[str] = []

    def execute_sql(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[Any]]:
        self.executed_queries.append(sql)
        for key, resp in self.query_responses.items():
            if key in sql:
                return resp
        return []

    def insert(self, table: str, columns: list[str], values: list[list[Any]]):
        self.inserts.append({"table": table, "columns": columns, "values": values})

    def update(self, table: str, columns: list[str], values: list[list[Any]]):
        self.updates.append({"table": table, "columns": columns, "values": values})


class MockDatabase:
    """Mock Spanner database implementing run_in_transaction with rollback and retry support."""

    def __init__(self, txn: MockTransaction | None = None, fail_attempts: int = 0):
        self.txn = txn or MockTransaction()
        self.fail_attempts = fail_attempts
        self.attempts = 0

    def run_in_transaction(self, callback):
        while True:
            try:
                if self.attempts < self.fail_attempts:
                    self.attempts += 1
                    raise Aborted("Wound-wait conflict: transaction aborted")
                self.attempts += 1
                return callback(self.txn)
            except Aborted:
                continue
            except Exception:
                # Discard buffered uncommitted mutations on transaction failure
                self.txn.inserts.clear()
                self.txn.updates.clear()
                raise


def make_sample_presentation(
    doc_id: str = "doc_slide_10KaQ12JImPARhyWvZP7dAZLMqW62bke8",
) -> NormalizedPresentation:
    doc_node = DocumentNode(
        document_id=doc_id,
        file_name="Enterprise Cloud Strategy.gslides",
        mime_type="application/vnd.google-apps.presentation",
        source_url="https://docs.google.com/presentation/d/10KaQ12JImPARhyWvZP7dAZLMqW62bke8/edit",
        gcs_uri=None,
        status="ACTIVE",
        repo_id=None,
        drive_file_id="10KaQ12JImPARhyWvZP7dAZLMqW62bke8",
    )

    c1 = ChunkNode(
        chunk_id=f"{doc_id}_s01_t001",
        document_id=doc_id,
        file_id=None,
        modality="TEXT",
        text_content="## Enterprise Cloud Strategy\nHigh performance graph architecture.",
        page_number=None,
        slide_number=1,
        commit_sha=None,
        line_range=None,
        source_gcs_uri=None,
        embedding_768=None,
    )

    c2 = ChunkNode(
        chunk_id=f"{doc_id}_s02_b001",
        document_id=doc_id,
        file_id=None,
        modality="TABLE",
        text_content="| Metric | Target |\n| --- | --- |\n| Latency | < 50ms |",
        page_number=None,
        slide_number=2,
        commit_sha=None,
        line_range=None,
        source_gcs_uri=None,
        embedding_768=None,
    )

    v1 = VisualAssetNode(
        visual_id=f"vis_{doc_id[:45]}_s01_v000",
        document_id=doc_id,
        page_number=None,
        slide_number=1,
        image_gcs_uri="gs://sales-discovery-mvp-mumbai/extracted-images/slide-1-render.png",
        description="Slide 1 render",
        asset_type="SLIDE",
    )

    v2 = VisualAssetNode(
        visual_id=f"vis_{doc_id[:45]}_s02_v001",
        document_id=doc_id,
        page_number=None,
        slide_number=2,
        image_gcs_uri="gs://sales-discovery-mvp-mumbai/extracted-images/slide-2-image-1.png",
        description="Architecture Diagram",
        asset_type="IMAGE",
    )

    return NormalizedPresentation(
        document_node=doc_node,
        chunks=[c1, c2],
        visual_assets=[v1, v2],
        slide_count=2,
        table_count=1,
        image_count=1,
        total_characters=len(c1.text_content) + len(c2.text_content),
    )


# ============================================================================
# 1. SCHEMA VALIDATION TESTS
# ============================================================================


def test_schema_validation_valid():
    norm = make_sample_presentation()
    validate_document_fields(norm.document_node)
    for c in norm.chunks:
        validate_chunk_fields(c)
    for v in norm.visual_assets:
        validate_visual_asset_fields(v)


def test_schema_validation_empty_chunks_fails():
    norm = make_sample_presentation()
    empty_norm = NormalizedPresentation(
        document_node=norm.document_node,
        chunks=[],
        visual_assets=norm.visual_assets,
        slide_count=2,
        table_count=0,
        image_count=1,
        total_characters=0,
    )
    mock_db = MockDatabase()
    with pytest.raises(SchemaValidationError) as exc_info:
        persist_normalized_presentation(mock_db, empty_norm)
    assert "0 chunks: empty extraction indicates failure" in str(exc_info.value)


def test_schema_validation_embedding_not_none_fails():
    norm = make_sample_presentation()
    bad_chunk = ChunkNode(
        chunk_id=norm.chunks[0].chunk_id,
        document_id=norm.chunks[0].document_id,
        modality="TEXT",
        text_content="content",
        embedding_768=[0.1] * 768,
    )
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_chunk_fields(bad_chunk)
    assert "embedding_768 must be None" in str(exc_info.value)


def test_schema_validation_oversized_visual_id_fails():
    class DummyVisual:
        visual_id = "v" * 65
        document_id = "doc_123"
        image_gcs_uri = "gs://bucket/test.png"
        asset_type = "SLIDE"
        slide_number = 1

    with pytest.raises(SchemaValidationError) as exc_info:
        validate_visual_asset_fields(DummyVisual())
    assert "Invalid VisualId length" in str(exc_info.value)


# ============================================================================
# 2. INITIAL INSERTION TEST (ALL 5 TABLES)
# ============================================================================


def test_persist_initial_insertion():
    """Verify initial persistence of fresh presentation populates all 5 tables."""
    norm = make_sample_presentation()
    doc_id = norm.document_node.document_id

    # Database is completely empty for this doc
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [],
            "FROM VisualAssets": [],
            "FROM DocumentContainsChunk": [],
            "FROM VisualAssetBelongsToDoc": [],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_presentation(db, norm)

    assert isinstance(result, SlidesWriteResult)
    assert result.document_id == doc_id
    assert result.document_action == "INSERTED"
    assert result.chunks_inserted == 2
    assert result.chunks_updated == 0
    assert result.chunks_unchanged == 0
    assert result.visual_assets_inserted == 2
    assert result.visual_assets_updated == 0
    assert result.visual_assets_unchanged == 0
    assert result.chunk_edges_inserted == 2
    assert result.visual_edges_inserted == 2
    assert result.total_mutations == 1 + 2 + 2 + 2 + 2  # 9

    # Verify buffered inserts across all 5 tables
    inserted_tables = [ins["table"] for ins in txn.inserts]
    assert "Documents" in inserted_tables
    assert "EvidenceChunks" in inserted_tables
    assert "VisualAssets" in inserted_tables
    assert "DocumentContainsChunk" in inserted_tables
    assert "VisualAssetBelongsToDoc" in inserted_tables

    # Check EvidenceChunks values (Embedding768 must be None)
    chunk_inserts = [ins for ins in txn.inserts if ins["table"] == "EvidenceChunks"][0]
    for row in chunk_inserts["values"]:
        assert row[10] is None  # Embedding768
        assert row[6] in (1, 2)  # SlideNumber
        assert row[5] is None  # PageNumber


# ============================================================================
# 3. IDEMPOTENT UNCHANGED RERUN TEST (ZERO MUTATIONS)
# ============================================================================


def test_persist_unchanged_rerun_zero_mutations():
    """Verify that an identical rerun produces 0 mutations across all 5 tables."""
    norm = make_sample_presentation()
    doc_id = norm.document_node.document_id

    # Database already contains identical rows
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc_id,
                    norm.document_node.file_name,
                    norm.document_node.mime_type,
                    norm.document_node.source_url,
                    norm.document_node.gcs_uri,
                    norm.document_node.status,
                    norm.document_node.drive_file_id,
                    None,
                ]
            ],
            "FROM EvidenceChunks": [
                [norm.chunks[0].chunk_id, doc_id, norm.chunks[0].text_content, "TEXT", 1],
                [norm.chunks[1].chunk_id, doc_id, norm.chunks[1].text_content, "TABLE", 2],
            ],
            "FROM VisualAssets": [
                [
                    norm.visual_assets[0].visual_id,
                    doc_id,
                    norm.visual_assets[0].image_gcs_uri,
                    norm.visual_assets[0].description,
                    "SLIDE",
                    1,
                ],
                [
                    norm.visual_assets[1].visual_id,
                    doc_id,
                    norm.visual_assets[1].image_gcs_uri,
                    norm.visual_assets[1].description,
                    "IMAGE",
                    2,
                ],
            ],
            "FROM DocumentContainsChunk": [
                [norm.chunks[0].chunk_id],
                [norm.chunks[1].chunk_id],
            ],
            "FROM VisualAssetBelongsToDoc": [
                [norm.visual_assets[0].visual_id],
                [norm.visual_assets[1].visual_id],
            ],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_presentation(db, norm)

    assert result.document_action == "UNCHANGED"
    assert result.chunks_inserted == 0
    assert result.chunks_updated == 0
    assert result.chunks_unchanged == 2
    assert result.visual_assets_inserted == 0
    assert result.visual_assets_updated == 0
    assert result.visual_assets_unchanged == 2
    assert result.chunk_edges_inserted == 0
    assert result.chunk_edges_unchanged == 2
    assert result.visual_edges_inserted == 0
    assert result.visual_edges_unchanged == 2
    assert result.total_mutations == 0

    # ZERO inserts and ZERO updates!
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


# ============================================================================
# 4. CONTENT UPDATE TESTS
# ============================================================================


def test_persist_content_updates():
    """Verify selective updates to Document, Chunk, and VisualAsset without clobbering other fields."""
    norm = make_sample_presentation()
    doc_id = norm.document_node.document_id

    # Existing DB has older title, older chunk text, and older visual uri
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc_id,
                    "Old Title.gslides",
                    norm.document_node.mime_type,
                    norm.document_node.source_url,
                    norm.document_node.gcs_uri,
                    "ACTIVE",
                    norm.document_node.drive_file_id,
                    "repo_preserved_123",  # RepoId must be preserved!
                ]
            ],
            "FROM EvidenceChunks": [
                [norm.chunks[0].chunk_id, doc_id, "Old Chunk 1 text", "TEXT", 1],
                [norm.chunks[1].chunk_id, doc_id, norm.chunks[1].text_content, "TABLE", 2],
            ],
            "FROM VisualAssets": [
                [
                    norm.visual_assets[0].visual_id,
                    doc_id,
                    "gs://old-bucket/old.png",
                    "Old description",
                    "SLIDE",
                    1,
                ],
                [
                    norm.visual_assets[1].visual_id,
                    doc_id,
                    norm.visual_assets[1].image_gcs_uri,
                    norm.visual_assets[1].description,
                    "IMAGE",
                    2,
                ],
            ],
            "FROM DocumentContainsChunk": [
                [norm.chunks[0].chunk_id],
                [norm.chunks[1].chunk_id],
            ],
            "FROM VisualAssetBelongsToDoc": [
                [norm.visual_assets[0].visual_id],
                [norm.visual_assets[1].visual_id],
            ],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_presentation(db, norm)

    assert result.document_action == "UPDATED"
    assert result.chunks_updated == 1
    assert result.chunks_unchanged == 1
    assert result.visual_assets_updated == 1
    assert result.visual_assets_unchanged == 1
    assert result.total_mutations == 3  # 1 doc update + 1 chunk update + 1 visual update

    # Verify updates
    updated_tables = [u["table"] for u in txn.updates]
    assert "Documents" in updated_tables
    assert "EvidenceChunks" in updated_tables
    assert "VisualAssets" in updated_tables

    # Verify Document update does NOT clobber RepoId
    doc_upd = [u for u in txn.updates if u["table"] == "Documents"][0]
    assert "RepoId" not in doc_upd["columns"]


# ============================================================================
# 5. OWNERSHIP PROTECTION TESTS
# ============================================================================


def test_chunk_ownership_conflict_aborts_transaction():
    """Verify that attempting to update another document's chunk raises ChunkOwnershipConflictError."""
    norm = make_sample_presentation()

    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [
                # Chunk already owned by doc_OTHER
                [norm.chunks[0].chunk_id, "doc_OTHER_DOCUMENT", "Other text", "TEXT", 1],
            ],
        }
    )
    db = MockDatabase(txn)

    with pytest.raises(ChunkOwnershipConflictError) as exc_info:
        persist_normalized_presentation(db, norm)
    assert "already owned by Document 'doc_OTHER_DOCUMENT'" in str(exc_info.value)
    # Zero inserts or updates committed
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


def test_visual_asset_ownership_conflict_aborts_transaction():
    """Verify that attempting to update another document's visual asset raises VisualAssetOwnershipConflictError."""
    norm = make_sample_presentation()

    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [],
            "FROM VisualAssets": [
                # Visual asset already owned by doc_OTHER
                [
                    norm.visual_assets[0].visual_id,
                    "doc_OTHER_DOCUMENT",
                    "gs://b/other.png",
                    "Other desc",
                    "SLIDE",
                    1,
                ],
            ],
        }
    )
    db = MockDatabase(txn)

    with pytest.raises(VisualAssetOwnershipConflictError) as exc_info:
        persist_normalized_presentation(db, norm)
    assert "already owned by Document 'doc_OTHER_DOCUMENT'" in str(exc_info.value)
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


# ============================================================================
# 6. TRANSACTION RETRY ON ABORTED ERROR
# ============================================================================


def test_persist_retry_on_aborted_conflict():
    """Verify database.run_in_transaction retries on Wound-Wait conflict and succeeds."""
    norm = make_sample_presentation()

    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [],
            "FROM VisualAssets": [],
            "FROM DocumentContainsChunk": [],
            "FROM VisualAssetBelongsToDoc": [],
        }
    )
    # Simulate 2 aborts before success
    db = MockDatabase(txn, fail_attempts=2)

    result = persist_normalized_presentation(db, norm)

    assert result.document_action == "INSERTED"
    assert db.attempts == 3  # 2 failed + 1 successful


# ============================================================================
# 7. AUDIT VERIFICATION TESTS
# ============================================================================


def test_audit_transaction_failure_aborts_all_mutations():
    """Invariant 1: If any mutation or transaction operation fails, no partial Spanner writes are committed."""
    norm = make_sample_presentation()

    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [],
            "FROM VisualAssets": [],
            "FROM DocumentContainsChunk": [],
            "FROM VisualAssetBelongsToDoc": [],
        }
    )

    # Simulate an unexpected failure midway through mutation buffering
    original_insert = txn.insert

    def faulty_insert(table: str, columns: list[str], values: list[list[Any]]):
        if table == "VisualAssets":
            raise RuntimeError("Simulated transient Spanner mutation failure")
        original_insert(table, columns, values)

    txn.insert = faulty_insert
    db = MockDatabase(txn)

    with pytest.raises(RuntimeError) as exc_info:
        persist_normalized_presentation(db, norm)
    assert "Simulated transient Spanner mutation failure" in str(exc_info.value)

    # Verify MockDatabase discarded any buffered mutations on failure
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


def test_audit_unrelated_records_and_embeddings_preserved():
    """Invariant 2: Unrelated documents, chunks, visual assets, metadata, and embeddings remain untouched."""
    norm = make_sample_presentation()
    doc_id = norm.document_node.document_id

    # Document exists with RepoId and pre-existing chunk with an existing Embedding768 vector
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc_id,
                    "Old Title.gslides",
                    norm.document_node.mime_type,
                    norm.document_node.source_url,
                    norm.document_node.gcs_uri,
                    "ACTIVE",
                    norm.document_node.drive_file_id,
                    "important_repo_001",  # RepoId
                ]
            ],
            "FROM EvidenceChunks": [
                # Chunk 0 has changed text; chunk 1 unchanged
                [norm.chunks[0].chunk_id, doc_id, "Old text content", "TEXT", 1],
                [norm.chunks[1].chunk_id, doc_id, norm.chunks[1].text_content, "TABLE", 2],
            ],
            "FROM VisualAssets": [
                [
                    norm.visual_assets[0].visual_id,
                    doc_id,
                    norm.visual_assets[0].image_gcs_uri,
                    norm.visual_assets[0].description,
                    "SLIDE",
                    1,
                ],
                [
                    norm.visual_assets[1].visual_id,
                    doc_id,
                    norm.visual_assets[1].image_gcs_uri,
                    norm.visual_assets[1].description,
                    "IMAGE",
                    2,
                ],
            ],
            "FROM DocumentContainsChunk": [
                [norm.chunks[0].chunk_id],
                [norm.chunks[1].chunk_id],
            ],
            "FROM VisualAssetBelongsToDoc": [
                [norm.visual_assets[0].visual_id],
                [norm.visual_assets[1].visual_id],
            ],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_presentation(db, norm)
    assert result.chunks_updated == 1

    # Verify that EvidenceChunks update never touches Embedding768
    chunk_updates = [u for u in txn.updates if u["table"] == "EvidenceChunks"]
    assert len(chunk_updates) == 1
    assert "Embedding768" not in chunk_updates[0]["columns"]

    # Verify that Documents update never touches RepoId
    doc_updates = [u for u in txn.updates if u["table"] == "Documents"]
    assert len(doc_updates) == 1
    assert "RepoId" not in doc_updates[0]["columns"]


def test_audit_all_five_tables_participate_in_same_transaction():
    """Invariant 3: All five tables participate in the same serializable transaction."""
    norm = make_sample_presentation()

    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [],
            "FROM VisualAssets": [],
            "FROM DocumentContainsChunk": [],
            "FROM VisualAssetBelongsToDoc": [],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_presentation(db, norm)
    # 1 doc + 2 chunks + 2 visuals + 2 chunk edges + 2 visual edges = 9 total mutations
    assert result.total_mutations == 9

    # Confirm all 5 tables were queried on this exact txn
    executed_sql = " ".join(txn.executed_queries)
    assert "FROM Documents" in executed_sql
    assert "FROM EvidenceChunks" in executed_sql
    assert "FROM VisualAssets" in executed_sql
    assert "FROM DocumentContainsChunk" in executed_sql
    assert "FROM VisualAssetBelongsToDoc" in executed_sql

    # Confirm all 5 tables received mutations on this exact txn
    tables_inserted = {ins["table"] for ins in txn.inserts}
    assert tables_inserted == {
        "Documents",
        "EvidenceChunks",
        "VisualAssets",
        "DocumentContainsChunk",
        "VisualAssetBelongsToDoc",
    }


def test_audit_intra_presentation_duplicate_ids_rejected():
    """Invariant 5 & Pre-transaction validation: Duplicate chunk/visual IDs and doc mismatches are rejected."""
    norm = make_sample_presentation()

    # Duplicate ChunkId
    dup_chunk = ChunkNode(
        chunk_id=norm.chunks[0].chunk_id,  # Same chunk ID!
        document_id=norm.document_node.document_id,
        file_id=None,
        modality="TEXT",
        text_content="Duplicate chunk text",
        page_number=None,
        slide_number=3,
        commit_sha=None,
        line_range=None,
        source_gcs_uri=None,
        embedding_768=None,
    )
    invalid_norm_chunks = NormalizedPresentation(
        document_node=norm.document_node,
        chunks=norm.chunks + [dup_chunk],
        visual_assets=norm.visual_assets,
        slide_count=norm.slide_count,
        table_count=norm.table_count,
        image_count=norm.image_count,
        total_characters=norm.total_characters + len(dup_chunk.text_content),
    )
    db = MockDatabase()
    with pytest.raises(SchemaValidationError) as exc_info:
        persist_normalized_presentation(db, invalid_norm_chunks)
    assert "Duplicate ChunkId detected" in str(exc_info.value)
    assert db.attempts == 0  # Pre-transaction check: no transaction initiated

    # Duplicate VisualId
    dup_visual = VisualAssetNode(
        visual_id=norm.visual_assets[0].visual_id,  # Same visual ID!
        document_id=norm.document_node.document_id,
        page_number=None,
        slide_number=3,
        image_gcs_uri="gs://bucket/dup.png",
        description="Duplicate visual",
        asset_type="SLIDE",
    )
    invalid_norm_visuals = NormalizedPresentation(
        document_node=norm.document_node,
        chunks=norm.chunks,
        visual_assets=norm.visual_assets + [dup_visual],
        slide_count=norm.slide_count,
        table_count=norm.table_count,
        image_count=norm.image_count + 1,
        total_characters=norm.total_characters,
    )
    with pytest.raises(SchemaValidationError) as exc_info:
        persist_normalized_presentation(db, invalid_norm_visuals)
    assert "Duplicate VisualId detected" in str(exc_info.value)
    assert db.attempts == 0

    # Mismatched DocumentId in chunk
    mismatched_chunk = ChunkNode(
        chunk_id="chunk_mismatch_001",
        document_id="doc_DIFFERENT_ID",
        file_id=None,
        modality="TEXT",
        text_content="Mismatched doc id chunk",
        page_number=None,
        slide_number=1,
        commit_sha=None,
        line_range=None,
        source_gcs_uri=None,
        embedding_768=None,
    )
    invalid_mismatch_norm = NormalizedPresentation(
        document_node=norm.document_node,
        chunks=[mismatched_chunk],
        visual_assets=[],
        slide_count=1,
        table_count=0,
        image_count=0,
        total_characters=len(mismatched_chunk.text_content),
    )
    with pytest.raises(SchemaValidationError) as exc_info:
        persist_normalized_presentation(db, invalid_mismatch_norm)
    assert "does not match presentation document_id" in str(exc_info.value)
    assert db.attempts == 0
