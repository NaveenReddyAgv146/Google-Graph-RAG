"""Mocked unit and end-to-end tests for the Ingestion Copilot PDF Orchestrator."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.orchestrator import (
    DocumentIngestionResult,
    IngestionStage,
    IngestionStatus,
    PdfIngestionOrchestrator,
    PipelineExecutionSummary,
)
from common.config import Settings
from common.database.ingestion_writer import ChunkOwnershipConflictError
from common.parsers.documentai_client import (
    DocumentAiApiError,
    DocumentAiLayoutClient,
    PageLimitExceededError,
    ParsedDocument,
)
from common.parsers.layout_extractor import generate_chunk_id
from common.storage.pdf_reader import DownloadedPdf, PdfMetadata

# ============================================================================
# Mock Helpers for Storage, Document AI, and Spanner
# ============================================================================


class MockSpannerTransaction:
    """Simulates a Spanner read-write transaction."""

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


class MockSpannerDatabase:
    """Simulates a Cloud Spanner Database instance."""

    def __init__(self, txn: MockSpannerTransaction | None = None):
        self.txn = txn or MockSpannerTransaction()
        self.transaction_calls = 0

    def run_in_transaction(self, callback):
        self.transaction_calls += 1
        return callback(self.txn)


def make_sample_pdf_bytes(title: str = "Test Document") -> bytes:
    """Generates synthetic valid PDF bytes."""
    content = f"%PDF-1.7\n1 0 obj\n<< /Title ({title}) >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return content.encode("utf-8")


def make_mock_storage_client(pdf_map: dict[str, bytes] | None = None) -> MagicMock:
    """Creates a mock google.cloud.storage.Client serving a map of {gcs_uri: bytes}."""
    pdf_map = pdf_map or {}
    mock_client = MagicMock()

    def get_bucket(bucket_name: str):
        mock_bucket = MagicMock()
        mock_bucket.name = bucket_name

        def get_blob(blob_name: str):
            uri = f"gs://{bucket_name}/{blob_name}"
            if uri not in pdf_map:
                return None
            data = pdf_map[uri]
            mock_blob = MagicMock()
            mock_blob.name = blob_name
            mock_blob.bucket = mock_bucket
            mock_blob.size = len(data)
            mock_blob.generation = 12345
            mock_blob.content_type = "application/pdf"
            mock_blob.download_as_bytes.return_value = data
            return mock_blob

        mock_bucket.get_blob.side_effect = get_blob
        return mock_bucket

    mock_client.bucket.side_effect = get_bucket

    def list_blobs(bucket_or_name: Any, prefix: str | None = None, **kwargs):
        target_name = bucket_or_name.name if hasattr(bucket_or_name, "name") else str(bucket_or_name)
        blobs = []
        for uri, data in pdf_map.items():
            b_name = uri.replace("gs://", "").split("/")[0]
            obj_name = uri.replace(f"gs://{b_name}/", "")
            if b_name == target_name:
                if not prefix or obj_name.startswith(prefix):
                    b = MagicMock()
                    b.name = obj_name
                    b.bucket = MagicMock()
                    b.bucket.name = b_name
                    b.size = len(data)
                    b.generation = 12345
                    b.content_type = "application/pdf"
                    blobs.append(b)
        return blobs

    mock_client.list_blobs.side_effect = list_blobs
    return mock_client


def make_mock_docai_client(
    response_texts: dict[str, str] | None = None,
    failing_uris: dict[str, Exception] | None = None,
) -> MagicMock:
    """Creates a mock DocumentAiLayoutClient."""
    mock_client = MagicMock(spec=DocumentAiLayoutClient)
    response_texts = response_texts or {}
    failing_uris = failing_uris or {}

    def process_pdf(downloaded_pdf: DownloadedPdf) -> ParsedDocument:
        uri = downloaded_pdf.metadata.gcs_uri
        if uri in failing_uris:
            raise failing_uris[uri]

        raw_text = response_texts.get(
            uri,
            "Overview of Cloud Spanner GraphRAG Architecture.\nScalable graph search across enterprises.",
        )

        # Mock DocAI Document structure
        mock_p1 = MagicMock()
        mock_seg = MagicMock()
        mock_seg.start_index = 0
        mock_seg.end_index = len(raw_text)
        mock_p1.layout.text_anchor.text_segments = [mock_seg]
        mock_p1.layout.bounding_poly.normalized_vertices = [
            MagicMock(x=0.1, y=0.2),
            MagicMock(x=0.9, y=0.2),
            MagicMock(x=0.9, y=0.8),
            MagicMock(x=0.1, y=0.8),
        ]

        mock_page = MagicMock()
        mock_page.page_number = 1
        mock_page.paragraphs = [mock_p1]
        mock_page.tables = []

        mock_doc = MagicMock()
        mock_doc.text = raw_text
        mock_doc.pages = [mock_page]

        return ParsedDocument(
            raw_document=mock_doc,
            metadata=downloaded_pdf.metadata,
            processor_name="projects/123/locations/us/processors/abc",
            page_count=1,
            text=raw_text,
        )

    mock_client.process.side_effect = process_pdf
    return mock_client


# ============================================================================
# 1. Orchestrator Initialization Tests
# ============================================================================


def test_orchestrator_initialization():
    settings = Settings(
        project_id="test-project",
        documentai_location="us",
        documentai_processor_id="test-processor",
    )
    mock_docai = MagicMock(spec=DocumentAiLayoutClient)
    mock_db = MockSpannerDatabase()
    mock_storage = MagicMock()

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
        settings=settings,
    )

    assert orchestrator.settings.project_id == "test-project"
    assert orchestrator.docai_client is mock_docai
    assert orchestrator.spanner_database is mock_db
    assert orchestrator.storage_client is mock_storage


# ============================================================================
# 2. Single PDF Success End-to-End
# ============================================================================


def test_single_pdf_success_end_to_end():
    uri = "gs://sales-discovery-mvp-mumbai/whitepapers/spanner_rag.pdf"
    pdf_bytes = make_sample_pdf_bytes("Spanner GraphRAG")

    mock_storage = make_mock_storage_client({uri: pdf_bytes})
    mock_docai = make_mock_docai_client({uri: "Introduction to Spanner GraphRAG\nEnterprise graph reasoning."})
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.SUCCESS
    assert result.stage == IngestionStage.COMPLETED
    assert result.gcs_uri == uri
    assert result.document_id is not None
    assert result.document_id.startswith("doc_")
    assert result.chunks_count >= 1
    assert result.batch_required is False
    assert result.duration_ms > 0
    assert result.write_result is not None
    assert result.write_result.document_action == "INSERTED"

    # Verify Spanner mutations
    assert len(mock_txn.inserts) >= 3  # Documents, EvidenceChunks, DocumentContainsChunk
    doc_ins = next(i for i in mock_txn.inserts if i["table"] == "Documents")
    assert doc_ins["values"][0][0] == result.document_id
    assert doc_ins["values"][0][4] == uri  # GcsUri

    chk_ins = next(i for i in mock_txn.inserts if i["table"] == "EvidenceChunks")
    # Verify strict invariant: Embedding768 is None
    for row in chk_ins["values"]:
        assert row[-1] is None


# ============================================================================
# 3. Synchronous Page Limit Handling (Batch Required)
# ============================================================================


def test_single_pdf_page_limit_exceeded_returns_batch_required():
    uri = "gs://sales-discovery-mvp/large_annual_report.pdf"
    pdf_bytes = make_sample_pdf_bytes("Large Document")

    mock_storage = make_mock_storage_client({uri: pdf_bytes})
    mock_docai = make_mock_docai_client(
        failing_uris={
            uri: PageLimitExceededError(
                "PDF exceeds synchronous limit (45 > 15 pages); batch processing required."
            )
        }
    )
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.BATCH_REQUIRED
    assert result.stage == IngestionStage.PARSING
    assert result.batch_required is True
    assert result.error_type == "PageLimitExceededError"
    assert "batch processing required" in result.error_message
    assert result.document_id is not None

    # Spanner should NOT be touched
    assert len(mock_txn.inserts) == 0
    assert len(mock_txn.updates) == 0
    assert mock_db.transaction_calls == 0


# ============================================================================
# 4. GCS Download Failure Handling
# ============================================================================


def test_single_pdf_gcs_download_failure():
    uri = "gs://sales-discovery-mvp-mumbai/missing_file.pdf"
    mock_storage = make_mock_storage_client({})  # Empty storage
    mock_docai = make_mock_docai_client()
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.FAILED
    assert result.stage == IngestionStage.DOWNLOADING
    assert "not found" in result.error_message.lower()
    assert result.document_id is None
    assert mock_db.transaction_calls == 0


# ============================================================================
# 5. Document AI Parsing Failure Handling
# ============================================================================


def test_single_pdf_documentai_api_failure():
    uri = "gs://sales-discovery-mvp/failing_doc.pdf"
    pdf_bytes = make_sample_pdf_bytes("Failing")

    mock_storage = make_mock_storage_client({uri: pdf_bytes})
    mock_docai = make_mock_docai_client(
        failing_uris={uri: DocumentAiApiError("Processor service unavailable (503)")}
    )
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.FAILED
    assert result.stage == IngestionStage.PARSING
    assert result.error_type == "DocumentAiApiError"
    assert "503" in result.error_message
    assert mock_db.transaction_calls == 0


# ============================================================================
# 6. Spanner Persistence Conflict Handling
# ============================================================================


def test_single_pdf_spanner_chunk_ownership_conflict_fails_gracefully():
    uri = "gs://sales-discovery-mvp-mumbai/conflict.pdf"
    pdf_bytes = make_sample_pdf_bytes("Conflict Doc")

    mock_storage = make_mock_storage_client({uri: pdf_bytes})
    mock_docai = make_mock_docai_client({uri: "Conflict chunk content."})

    # Pre-populate DB with existing chunk owned by a foreign document
    h = hashlib.sha256(pdf_bytes).hexdigest()
    expected_doc_id = f"doc_{h[:32]}"
    conflicting_chunk_id = generate_chunk_id(expected_doc_id, page_number=1, chunk_index=1, modality="TEXT")
    mock_txn = MockSpannerTransaction(
        query_responses={
            "FROM EvidenceChunks": [
                [
                    conflicting_chunk_id,
                    "doc_foreign_owner_999",  # Foreign owner
                    "Old content",
                    "TEXT",
                    1,
                ]
            ]
        }
    )
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.FAILED
    assert result.stage == IngestionStage.PERSISTING
    assert result.error_type == "ChunkOwnershipConflictError"
    assert "already owned by Document 'doc_foreign_owner_999'" in result.error_message


# ============================================================================
# 7. Multi-PDF Batch Ingestion with Isolation & Mixed Outcomes
# ============================================================================


def test_multi_pdf_batch_isolation():
    uri_success_1 = "gs://sales-discovery-mvp/doc1.pdf"
    uri_batch_req = "gs://sales-discovery-mvp/large2.pdf"
    uri_download_err = "gs://sales-discovery-mvp/missing3.pdf"
    uri_success_2 = "gs://sales-discovery-mvp/doc4.pdf"

    pdf_map = {
        uri_success_1: make_sample_pdf_bytes("Doc 1"),
        uri_batch_req: make_sample_pdf_bytes("Large 2"),
        uri_success_2: make_sample_pdf_bytes("Doc 4"),
    }
    mock_storage = make_mock_storage_client(pdf_map)
    mock_docai = make_mock_docai_client(
        response_texts={
            uri_success_1: "Doc 1 parsed text.",
            uri_success_2: "Doc 4 parsed text.",
        },
        failing_uris={
            uri_batch_req: PageLimitExceededError(
                "Document exceeds synchronous limit of 15 pages. Batch processing required."
            ),
        },
    )
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    summary: PipelineExecutionSummary = orchestrator.ingest_pdf_batch(
        [uri_success_1, uri_batch_req, uri_download_err, uri_success_2]
    )

    assert summary.total_discovered == 4
    assert summary.successful == 2
    assert summary.batch_required == 1
    assert summary.failed == 1
    assert len(summary.results) == 4

    assert summary.results[0].status == IngestionStatus.SUCCESS
    assert summary.results[1].status == IngestionStatus.BATCH_REQUIRED
    assert summary.results[2].status == IngestionStatus.FAILED
    assert summary.results[3].status == IngestionStatus.SUCCESS

    # Verify that failure of doc 2 & 3 did not prevent doc 4 from succeeding
    assert summary.results[3].document_id is not None
    assert summary.results[3].write_result.document_action == "INSERTED"


# ============================================================================
# 8. Unchanged Rerun Idempotency
# ============================================================================


def test_unchanged_rerun_idempotency():
    uri = "gs://sales-discovery-mvp-mumbai/existing.pdf"
    pdf_bytes = make_sample_pdf_bytes("Existing Document")
    h = hashlib.sha256(pdf_bytes).hexdigest()
    doc_id = f"doc_{h[:32]}"
    chunk_id = generate_chunk_id(doc_id, page_number=1, chunk_index=1, modality="TEXT")
    content = "Existing document content."

    mock_storage = make_mock_storage_client({uri: pdf_bytes})
    mock_docai = make_mock_docai_client({uri: content})

    # DB already contains exact records
    mock_txn = MockSpannerTransaction(
        query_responses={
            "FROM Documents": [
                [doc_id, "existing.pdf", "application/pdf", None, uri, "PROCESSED", None, None]
            ],
            "FROM EvidenceChunks": [
                [chunk_id, doc_id, content, "TEXT", 1]
            ],
            "FROM DocumentContainsChunk": [
                [chunk_id]
            ],
        }
    )
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.SUCCESS
    assert result.write_result is not None
    assert result.write_result.document_action == "UNCHANGED"
    assert result.write_result.chunks_inserted == 0
    assert result.write_result.chunks_unchanged == 1
    assert result.write_result.total_mutations == 0
    assert len(mock_txn.inserts) == 0
    assert len(mock_txn.updates) == 0


# ============================================================================
# 9. Changed PDF Versioning and Atomic Supersession
# ============================================================================


def test_changed_pdf_versioning_and_atomic_supersession():
    uri = "gs://sales-discovery-mvp-mumbai/versioned_guide.pdf"
    new_pdf_bytes = make_sample_pdf_bytes("Guide Version 2")
    old_doc_id = "doc_previous_v1_version_hash12345"

    mock_storage = make_mock_storage_client({uri: new_pdf_bytes})
    mock_docai = make_mock_docai_client({uri: "Revised guide content for version 2."})

    # DB state: new document not found, older version found under same GcsUri
    mock_txn = MockSpannerTransaction(
        query_responses={
            "WHERE DocumentId = @doc_id": [],
            "WHERE GcsUri = @gcs_uri": [[old_doc_id]],
            "FROM EvidenceChunks": [],
            "FROM DocumentContainsChunk": [],
        }
    )
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.SUCCESS
    assert result.write_result is not None
    assert result.write_result.document_action == "INSERTED"
    assert result.write_result.superseded_documents == [old_doc_id]

    # Verify older version update to SUPERSEDED
    doc_upds = [u for u in mock_txn.updates if u["table"] == "Documents"]
    assert len(doc_upds) == 1
    assert doc_upds[0]["columns"] == ["DocumentId", "Status"]
    assert doc_upds[0]["values"] == [[old_doc_id, "SUPERSEDED"]]


# ============================================================================
# 10. Bucket Discovery & Empty Bucket Handling
# ============================================================================


def test_ingest_bucket_pdfs_discovery():
    bucket = "sales-discovery-mvp-mumbai"
    uri1 = f"gs://{bucket}/whitepapers/p1.pdf"
    uri2 = f"gs://{bucket}/whitepapers/p2.pdf"

    pdf_map = {
        uri1: make_sample_pdf_bytes("P1"),
        uri2: make_sample_pdf_bytes("P2"),
    }
    mock_storage = make_mock_storage_client(pdf_map)
    mock_docai = make_mock_docai_client()
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    summary = orchestrator.ingest_bucket_pdfs(bucket, prefix="whitepapers/")

    assert summary.total_discovered == 2
    assert summary.successful == 2
    assert summary.failed == 0
    assert summary.batch_required == 0


def test_ingest_bucket_pdfs_empty():
    bucket = "sales-discovery-mvp"
    mock_storage = make_mock_storage_client({})
    mock_docai = make_mock_docai_client()
    mock_db = MockSpannerDatabase()

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    summary = orchestrator.ingest_bucket_pdfs(bucket, prefix="empty_folder/")

    assert summary.total_discovered == 0
    assert summary.successful == 0
    assert summary.failed == 0
    assert len(summary.results) == 0


# ============================================================================
# 11. Empty Extraction Guard & Document Layout Orchestration
# ============================================================================


def test_empty_extraction_fails_without_persisting():
    """Verifies that an empty extraction (0 chunks) fails immediately and aborts Spanner persistence."""
    uri = "gs://sales-discovery-mvp/empty.pdf"
    pdf_bytes = make_sample_pdf_bytes("Empty")
    mock_storage = make_mock_storage_client({uri: pdf_bytes})

    # Mock DocAI returning completely empty Document (0 text, 0 paragraphs, 0 layout blocks)
    mock_docai = MagicMock(spec=DocumentAiLayoutClient)
    empty_doc = MagicMock()
    empty_doc.text = ""
    empty_doc.pages = [MagicMock(page_number=1, paragraphs=[], blocks=[], lines=[], tables=[])]
    empty_doc.document_layout = None

    def empty_process(downloaded_pdf: DownloadedPdf) -> ParsedDocument:
        return ParsedDocument(
            raw_document=empty_doc,
            metadata=downloaded_pdf.metadata,
            processor_name="projects/123/locations/us/processors/layout",
            page_count=1,
            text="",
        )

    mock_docai.process.side_effect = empty_process
    mock_db = MockSpannerDatabase()

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.FAILED
    assert result.stage == IngestionStage.NORMALIZING
    assert result.chunks_count == 0
    assert "EmptyDocumentExtractionError" in result.error_type
    assert "0 chunks" in result.error_message
    # Critical invariant: Spanner transaction must NEVER have been called!
    assert mock_db.transaction_calls == 0


def test_document_layout_blocks_orchestrated_successfully():
    """Verifies that a document with modern DocumentLayout blocks (and empty page.paragraphs) ingests successfully."""
    uri = "gs://sales-discovery-mvp/layout_resume.pdf"
    pdf_bytes = make_sample_pdf_bytes("Resume")
    mock_storage = make_mock_storage_client({uri: pdf_bytes})

    # Build Document with document_layout.blocks
    mock_docai = MagicMock(spec=DocumentAiLayoutClient)

    tb = MagicMock()
    tb.text = "Senior Cloud Architect - Resume Content"
    tb.type_ = "paragraph"
    tb.blocks = []

    block = MagicMock()
    block.text_block = tb
    block.table_block = None
    block.list_block = None
    block.page_span.page_start = 1
    block.bounding_box = None

    doc_layout = MagicMock()
    doc_layout.blocks = [block]

    mock_doc = MagicMock()
    mock_doc.text = ""
    mock_doc.pages = [MagicMock(page_number=1, paragraphs=[])]  # 0 paragraphs
    mock_doc.document_layout = doc_layout

    def layout_process(downloaded_pdf: DownloadedPdf) -> ParsedDocument:
        return ParsedDocument(
            raw_document=mock_doc,
            metadata=downloaded_pdf.metadata,
            processor_name="projects/123/locations/us/processors/layout",
            page_count=1,
            text="",
        )

    mock_docai.process.side_effect = layout_process
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = PdfIngestionOrchestrator(
        docai_client=mock_docai,
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    result = orchestrator.ingest_pdf(uri)

    assert result.status == IngestionStatus.SUCCESS
    assert result.stage == IngestionStage.COMPLETED
    assert result.chunks_count == 1
    assert mock_db.transaction_calls == 1
    # Check that chunks and documents were inserted into Spanner
    tables_inserted = [ins["table"] for ins in mock_txn.inserts]
    assert "Documents" in tables_inserted
    assert "EvidenceChunks" in tables_inserted
    assert "DocumentContainsChunk" in tables_inserted

