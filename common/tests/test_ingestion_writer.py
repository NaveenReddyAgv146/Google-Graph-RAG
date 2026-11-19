"""Mocked unit tests for idempotent Spanner document, chunk, and graph edge persistence."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import Aborted

from common.database.ingestion_writer import (
    ChunkOwnershipConflictError,
    IngestionWriteResult,
    SchemaValidationError,
    persist_normalized_document,
    validate_chunk_fields,
    validate_document_fields,
)
from common.models.schema import ChunkNode, DocumentNode
from common.parsers.layout_extractor import NormalizedDocument


class MockTransaction:
    """Mock Spanner read-write transaction tracking queries and buffered mutations."""

    def __init__(self, query_responses: dict[str, list[list[Any]]] | None = None):
        self.query_responses = query_responses or {}
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.executed_queries: list[str] = []

    def execute_sql(self, sql: str, params: dict[str, Any] | None = None, param_types: Any = None) -> list[list[Any]]:
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
    """Mock Spanner database implementing run_in_transaction."""

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


def make_sample_doc() -> NormalizedDocument:
    doc_node = DocumentNode(
        document_id="doc_1234567890abcdef1234567890abcdef",
        file_name="resilience.pdf",
        mime_type="application/pdf",
        source_url=None,
        gcs_uri="gs://sales-discovery-mvp-mumbai/resilience.pdf",
        status="PROCESSED",
        repo_id=None,
        drive_file_id=None,
    )

    c1 = ChunkNode(
        chunk_id="doc_1234567890abcdef12345678_p01_t001",
        document_id="doc_1234567890abcdef1234567890abcdef",
        file_id=None,
        modality="TEXT",
        text_content="Page 1 introduction text.",
        page_number=1,
        slide_number=None,
        commit_sha=None,
        line_range=None,
        source_gcs_uri="gs://sales-discovery-mvp-mumbai/resilience.pdf",
        embedding_768=None,
    )

    c2 = ChunkNode(
        chunk_id="doc_1234567890abcdef12345678_p01_b002",
        document_id="doc_1234567890abcdef1234567890abcdef",
        file_id=None,
        modality="TABLE",
        text_content="| Metric | Target |\n| --- | --- |\n| SLA | 99.99% |",
        page_number=1,
        slide_number=None,
        commit_sha=None,
        line_range=None,
        source_gcs_uri="gs://sales-discovery-mvp-mumbai/resilience.pdf",
        embedding_768=None,
    )

    return NormalizedDocument(
        document_node=doc_node,
        chunks=[c1, c2],
        page_count=1,
        table_count=1,
        total_characters=len(c1.text_content) + len(c2.text_content),
    )


# ============================================================================
# 1. Schema Validation Tests
# ============================================================================


def test_schema_validation_success():
    sample = make_sample_doc()
    validate_document_fields(sample.document_node)
    for c in sample.chunks:
        validate_chunk_fields(c)


def test_schema_validation_oversized_document_id():
    mock_doc = MagicMock()
    mock_doc.document_id = "d" * 257
    mock_doc.file_name = "test.pdf"
    mock_doc.mime_type = "application/pdf"
    with pytest.raises(SchemaValidationError, match="Invalid DocumentId length"):
        validate_document_fields(mock_doc)


def test_schema_validation_oversized_chunk_id():
    mock_chunk = MagicMock()
    mock_chunk.chunk_id = "c" * 65
    mock_chunk.document_id = "doc_123"
    with pytest.raises(SchemaValidationError, match="Invalid ChunkId length"):
        validate_chunk_fields(mock_chunk)


def test_schema_validation_embedding_must_be_null():
    chunk = ChunkNode(
        chunk_id="chk_123",
        document_id="doc_123",
        text_content="some text",
        embedding_768=[0.1] * 768,  # Prohibited in Phase 3A
    )
    with pytest.raises(SchemaValidationError, match="Phase 3A Invariant Violation"):
        validate_chunk_fields(chunk)


# ============================================================================
# 2. First-time Ingestion (Clean Write)
# ============================================================================


def test_first_time_write_success():
    sample = make_sample_doc()
    # Mock clean empty database
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [],
            "FROM DocumentContainsChunk": [],
        }
    )
    db = MockDatabase(txn)

    result: IngestionWriteResult = persist_normalized_document(db, sample)

    assert result.document_id == sample.document_node.document_id
    assert result.document_action == "INSERTED"
    assert result.chunks_inserted == 2
    assert result.chunks_updated == 0
    assert result.chunks_unchanged == 0
    assert result.edges_inserted == 2
    assert result.edges_unchanged == 0
    assert result.total_mutations == 5  # 1 doc + 2 chunks + 2 edges

    # Verify buffered inserts
    tables = [ins["table"] for ins in txn.inserts]
    assert "Documents" in tables
    assert "EvidenceChunks" in tables
    assert "DocumentContainsChunk" in tables

    # Verify EvidenceChunks values have NULL embedding_768
    chunk_insert = next(ins for ins in txn.inserts if ins["table"] == "EvidenceChunks")
    for row in chunk_insert["values"]:
        assert row[-1] is None  # Embedding768 strictly None


# ============================================================================
# 3. Idempotent Rerun (Zero Changes)
# ============================================================================


def test_rerun_unchanged_idempotency():
    sample = make_sample_doc()
    doc = sample.document_node
    c1, c2 = sample.chunks

    # Mock database already containing exact same records
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc.document_id,
                    doc.file_name,
                    doc.mime_type,
                    doc.source_url,
                    doc.gcs_uri,
                    doc.status,
                    "existing-drive-id-123",  # Unrelated metadata
                    None,
                ]
            ],
            "FROM EvidenceChunks": [
                [c1.chunk_id, c1.document_id, c1.text_content, c1.modality, c1.page_number],
                [c2.chunk_id, c2.document_id, c2.text_content, c2.modality, c2.page_number],
            ],
            "FROM DocumentContainsChunk": [
                [c1.chunk_id],
                [c2.chunk_id],
            ],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_document(db, sample)

    assert result.document_action == "UNCHANGED"
    assert result.chunks_inserted == 0
    assert result.chunks_updated == 0
    assert result.chunks_unchanged == 2
    assert result.edges_inserted == 0
    assert result.edges_unchanged == 2
    assert result.total_mutations == 0
    assert result.superseded_documents == []

    # Zero mutation buffers
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


# ============================================================================
# 4. Partial Updates & Existing Field Preservation
# ============================================================================


def test_partial_updates_and_field_preservation():
    sample = make_sample_doc()
    doc = sample.document_node
    c1, c2 = sample.chunks

    # DB state:
    # - Document exists with old status "PENDING" and existing DriveFileId="keep-me-drive-id"
    # - c1 exists with OLD text content
    # - c2 does not exist yet (brand new)
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc.document_id,
                    doc.file_name,
                    doc.mime_type,
                    doc.source_url,
                    doc.gcs_uri,
                    "PENDING",  # Status needs update to "PROCESSED"
                    "keep-me-drive-id",  # Must NOT be overwritten
                    "repo-123",  # Must NOT be overwritten
                ]
            ],
            "FROM EvidenceChunks": [
                [c1.chunk_id, c1.document_id, "Old outdated chunk text", c1.modality, c1.page_number],
                # c2 not in DB
            ],
            "FROM DocumentContainsChunk": [
                [c1.chunk_id],
                # edge for c2 not in DB
            ],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_document(db, sample)

    assert result.document_action == "UPDATED"
    assert result.chunks_inserted == 1  # c2 inserted
    assert result.chunks_updated == 1   # c1 updated
    assert result.chunks_unchanged == 0
    assert result.edges_inserted == 1   # edge for c2 inserted
    assert result.edges_unchanged == 1  # edge for c1 unchanged
    assert result.total_mutations == 4  # 1 doc upd + 1 chk ins + 1 chk upd + 1 edge ins
    assert result.superseded_documents == []

    # Check document update columns: must NOT include DriveFileId or RepoId
    doc_upd = next(u for u in txn.updates if u["table"] == "Documents")
    assert "DriveFileId" not in doc_upd["columns"]
    assert "RepoId" not in doc_upd["columns"]
    assert "Status" in doc_upd["columns"]

    # Check EvidenceChunks update: updates text without touching embedding
    chk_upd = next(u for u in txn.updates if u["table"] == "EvidenceChunks")
    assert "Embedding768" not in chk_upd["columns"]
    assert "TextContent" in chk_upd["columns"]


# ============================================================================
# 5. Transaction Retries & Aborted Conflict
# ============================================================================


def test_transaction_retry_on_aborted():
    sample = make_sample_doc()
    txn = MockTransaction()
    # Fails once with Aborted, succeeds on retry
    db = MockDatabase(txn, fail_attempts=1)

    result = persist_normalized_document(db, sample)
    assert result.document_action == "INSERTED"
    assert db.attempts == 2  # 1 retry performed cleanly


def test_transaction_failure_raises():
    sample = make_sample_doc()
    mock_db = MagicMock()
    mock_db.run_in_transaction.side_effect = RuntimeError("Spanner internal error")

    with pytest.raises(RuntimeError, match="Spanner internal error"):
        persist_normalized_document(mock_db, sample)


# ============================================================================
# 6. Chunk Ownership Protection Tests
# ============================================================================


def test_chunk_ownership_conflict_raises():
    """Verify attempting to overwrite or reassign a chunk owned by another document raises ChunkOwnershipConflictError."""
    sample = make_sample_doc()
    doc = sample.document_node
    c1, _ = sample.chunks

    # DB state: chunk c1 exists, but DocumentId in DB is "doc_foreign_owner" (not doc.document_id)
    txn = MockTransaction(
        query_responses={
            "FROM EvidenceChunks": [
                [c1.chunk_id, "doc_foreign_owner", c1.text_content, c1.modality, c1.page_number],
            ],
        }
    )
    db = MockDatabase(txn)

    with pytest.raises(ChunkOwnershipConflictError) as exc_info:
        persist_normalized_document(db, sample)

    assert f"Chunk '{c1.chunk_id}' is already owned by Document 'doc_foreign_owner'" in str(exc_info.value)
    assert f"cannot be reassigned or overwritten by Document '{doc.document_id}'" in str(exc_info.value)


# ============================================================================
# 7. Changed-Source PDF Versioning & Atomic Supersession Tests
# ============================================================================


def test_supersedes_previous_processed_version_atomically():
    """Verify committing a new content version for an existing GCS URI atomically marks previous processed versions SUPERSEDED."""
    sample = make_sample_doc()
    old_doc_id = "doc_previous_version_hash12345"

    txn = MockTransaction(
        query_responses={
            # New document not in DB yet
            "WHERE DocumentId = @doc_id": [],
            # Prior processed version exists with same GcsUri
            "WHERE GcsUri = @gcs_uri": [
                [old_doc_id],
            ],
            # Chunks are new
            "FROM EvidenceChunks": [],
            # Edges are new
            "FROM DocumentContainsChunk": [],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_document(db, sample)

    assert result.document_action == "INSERTED"
    assert result.superseded_documents == [old_doc_id]
    assert result.chunks_inserted == 2
    assert result.edges_inserted == 2
    # 1 new doc insert + 1 old doc supersede update + 2 chunk inserts + 2 edge inserts = 6
    assert result.total_mutations == 6

    # Verify atomic update to SUPERSEDED
    doc_upds = [u for u in txn.updates if u["table"] == "Documents"]
    assert len(doc_upds) == 1
    assert doc_upds[0]["columns"] == ["DocumentId", "Status"]
    assert doc_upds[0]["values"] == [[old_doc_id, "SUPERSEDED"]]

    # Verify new document is inserted with PROCESSED status
    doc_ins = next(i for i in txn.inserts if i["table"] == "Documents")
    status_idx = doc_ins["columns"].index("Status")
    assert doc_ins["values"][0][status_idx] == "PROCESSED"


def test_supersession_skips_different_gcs_uri_or_no_prior_versions():
    """Verify supersession does not trigger if no prior processed version exists for the GCS URI."""
    sample = make_sample_doc()

    txn = MockTransaction(
        query_responses={
            "WHERE DocumentId = @doc_id": [],
            "WHERE GcsUri = @gcs_uri": [],  # No prior versions
            "FROM EvidenceChunks": [],
            "FROM DocumentContainsChunk": [],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_document(db, sample)

    assert result.document_action == "INSERTED"
    assert result.superseded_documents == []
    assert result.total_mutations == 5  # 1 doc + 2 chunks + 2 edges
    assert len(txn.updates) == 0


def test_historical_records_preserved_on_supersession():
    """Verify historical documents, chunks, and edges are never deleted during supersession."""
    sample = make_sample_doc()
    old_doc_id = "doc_v1_historical"

    txn = MockTransaction(
        query_responses={
            "WHERE DocumentId = @doc_id": [],
            "WHERE GcsUri = @gcs_uri": [[old_doc_id]],
            "FROM EvidenceChunks": [],
            "FROM DocumentContainsChunk": [],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_document(db, sample)

    assert result.superseded_documents == [old_doc_id]
    # Verify no delete operations were called or tracked
    for ins in txn.inserts:
        assert ins["table"] in ("Documents", "EvidenceChunks", "DocumentContainsChunk")
    for upd in txn.updates:
        assert upd["table"] == "Documents"
        assert upd["values"] == [[old_doc_id, "SUPERSEDED"]]


def test_embedding_preservation_on_chunk_update():
    """Verify that updating chunk content does not clobber existing Embedding768."""
    sample = make_sample_doc()
    doc = sample.document_node
    c1, c2 = sample.chunks

    # DB state: doc exists unchanged, c1 exists with changed text, c2 exists unchanged
    txn = MockTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc.document_id,
                    doc.file_name,
                    doc.mime_type,
                    doc.source_url,
                    doc.gcs_uri,
                    doc.status,
                    None,
                    None,
                ]
            ],
            "FROM EvidenceChunks": [
                [c1.chunk_id, c1.document_id, "Old text before revision", c1.modality, c1.page_number],
                [c2.chunk_id, c2.document_id, c2.text_content, c2.modality, c2.page_number],
            ],
            "FROM DocumentContainsChunk": [
                [c1.chunk_id],
                [c2.chunk_id],
            ],
        }
    )
    db = MockDatabase(txn)

    result = persist_normalized_document(db, sample)

    assert result.document_action == "UNCHANGED"
    assert result.chunks_updated == 1
    assert result.chunks_unchanged == 1
    assert result.total_mutations == 1

    chk_upd = next(u for u in txn.updates if u["table"] == "EvidenceChunks")
    assert "Embedding768" not in chk_upd["columns"]
    assert chk_upd["columns"] == ["ChunkId", "TextContent", "Modality", "PageNumber"]
    assert chk_upd["values"][0][0] == c1.chunk_id
    assert chk_upd["values"][0][1] == c1.text_content
