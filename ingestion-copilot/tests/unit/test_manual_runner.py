"""Mocked unit tests for Phase 3A Manual PDF Ingestion Test Runner (scripts/ingest_pdf_manual.py)."""

from __future__ import annotations

import io
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import scripts.ingest_pdf_manual as manual_runner
from app.orchestrator import (
    DocumentIngestionResult,
    IngestionStage,
    IngestionStatus,
    PdfIngestionOrchestrator,
)
from common.database.ingestion_writer import IngestionWriteResult


def make_mock_write_result(doc_id: str = "doc_test123") -> IngestionWriteResult:
    return IngestionWriteResult(
        document_id=doc_id,
        document_action="INSERTED",
        chunks_inserted=5,
        chunks_updated=0,
        chunks_unchanged=0,
        edges_inserted=5,
        edges_unchanged=0,
        total_mutations=11,
        superseded_documents=[],
    )


# ============================================================================
# 1. Argument Validation Tests
# ============================================================================


def test_invalid_gcs_uri_scheme(capsys):
    res, exit_code = manual_runner.run_manual_ingestion("https://storage.googleapis.com/bucket/doc.pdf")
    assert exit_code == 2
    assert res.status == IngestionStatus.FAILED
    assert res.stage == IngestionStage.DISCOVERY
    assert "must begin with 'gs://'" in res.error_message


def test_invalid_file_extension(capsys):
    res, exit_code = manual_runner.run_manual_ingestion("gs://sales-discovery-mvp-mumbai/whitepaper.docx")
    assert exit_code == 2
    assert res.status == IngestionStatus.FAILED
    assert "must begin with 'gs://' and end with '.pdf'" in res.error_message


# ============================================================================
# 2. Ingestion Result Formatting & Output Tests
# ============================================================================


def test_manual_runner_success_flow(capsys):
    uri = "gs://sales-discovery-mvp-mumbai/whitepapers/spanner_rag.pdf"
    doc_id = "doc_aabbccddeeff11223344"
    mock_result = DocumentIngestionResult(
        gcs_uri=uri,
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        document_id=doc_id,
        chunks_count=5,
        write_result=make_mock_write_result(doc_id),
        duration_ms=450.5,
    )

    mock_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_orch.ingest_pdf.return_value = mock_result

    res, exit_code = manual_runner.run_manual_ingestion(uri, dry_run=False, orchestrator=mock_orch)

    assert exit_code == 0
    assert res.status == IngestionStatus.SUCCESS
    assert res.document_id == doc_id

    captured = capsys.readouterr().out
    assert "Phase 3A Manual PDF Ingestion Result" in captured
    assert f"GCS URI:            {uri}" in captured
    assert "Status:             SUCCESS" in captured
    assert "Final Stage:        COMPLETED" in captured
    assert f"Document ID:        {doc_id}" in captured
    assert "Total Chunks:       5" in captured
    assert "Document Action:    INSERTED" in captured
    assert "Total Mutations:    11" in captured
    assert "Dry Run:            No (Committed to Spanner)" in captured


def test_manual_runner_batch_required_exit_code_1(capsys):
    uri = "gs://sales-discovery-mvp/large_40_page_spec.pdf"
    doc_id = "doc_large123"
    mock_result = DocumentIngestionResult(
        gcs_uri=uri,
        status=IngestionStatus.BATCH_REQUIRED,
        stage=IngestionStage.PARSING,
        document_id=doc_id,
        chunks_count=0,
        batch_required=True,
        error_type="PageLimitExceededError",
        error_message="Document exceeds synchronous 15-page limit (40 pages). Batch processing required.",
        duration_ms=120.0,
    )

    mock_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_orch.ingest_pdf.return_value = mock_result

    res, exit_code = manual_runner.run_manual_ingestion(uri, dry_run=False, orchestrator=mock_orch)

    assert exit_code == 1  # Required exit code on BATCH_REQUIRED
    assert res.status == IngestionStatus.BATCH_REQUIRED
    assert res.batch_required is True

    captured = capsys.readouterr().out
    assert "Status:             BATCH_REQUIRED" in captured
    assert "Batch Required:     True" in captured
    assert "Error Type:         PageLimitExceededError" in captured
    assert "Batch processing required" in captured


def test_manual_runner_failure_exit_code_2(capsys):
    uri = "gs://sales-discovery-mvp-mumbai/missing.pdf"
    mock_result = DocumentIngestionResult(
        gcs_uri=uri,
        status=IngestionStatus.FAILED,
        stage=IngestionStage.DOWNLOADING,
        error_type="NotFound",
        error_message="Blob 'missing.pdf' does not exist in bucket 'sales-discovery-mvp-mumbai'",
        duration_ms=50.0,
    )

    mock_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_orch.ingest_pdf.return_value = mock_result

    res, exit_code = manual_runner.run_manual_ingestion(uri, dry_run=False, orchestrator=mock_orch)

    assert exit_code == 2  # Required exit code on failure
    assert res.status == IngestionStatus.FAILED

    captured = capsys.readouterr().out
    assert "Status:             FAILED" in captured
    assert "Final Stage:        DOWNLOADING" in captured
    assert "Blob 'missing.pdf' does not exist" in captured


# ============================================================================
# 3. Dry-Run Database & Mutation Avoidance Tests
# ============================================================================


def test_dry_run_database_intercepts_writes_and_avoids_mutations():
    mock_real_db = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.execute_sql.return_value = [["doc_existing", "test.pdf", "application/pdf"]]
    mock_real_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    dry_db = manual_runner.DryRunSpannerDatabase(mock_real_db)

    def dummy_txn_callback(txn):
        # Execute query (should delegate safely)
        rows = txn.execute_sql("SELECT * FROM Documents WHERE DocumentId = @doc_id")
        assert len(rows) == 1
        # Perform write mutations (must be captured in memory without modifying database)
        txn.insert("Documents", ["DocumentId", "Status"], [["doc_1", "PROCESSED"]])
        txn.update("EvidenceChunks", ["ChunkId", "TextContent"], [["c1", "Updated"]])
        return "CALLBACK_SUCCESS"

    result = dry_db.run_in_transaction(dummy_txn_callback)
    assert result == "CALLBACK_SUCCESS"

    last_txn = dry_db.last_txn
    assert last_txn is not None
    assert len(last_txn.inserts) == 1
    assert last_txn.inserts[0]["table"] == "Documents"
    assert len(last_txn.updates) == 1
    assert last_txn.updates[0]["table"] == "EvidenceChunks"

    # Verify real database was NEVER called with write/insert/update
    assert not hasattr(mock_real_db, "insert") or mock_real_db.insert.call_count == 0
    assert not hasattr(mock_real_db, "update") or mock_real_db.update.call_count == 0


def test_manual_runner_dry_run_flag_output(capsys):
    uri = "gs://sales-discovery-mvp-mumbai/whitepapers/sample.pdf"
    doc_id = "doc_dryrun123"
    mock_result = DocumentIngestionResult(
        gcs_uri=uri,
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        document_id=doc_id,
        chunks_count=3,
        write_result=make_mock_write_result(doc_id),
        duration_ms=300.0,
    )

    mock_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_orch.ingest_pdf.return_value = mock_result

    res, exit_code = manual_runner.run_manual_ingestion(uri, dry_run=True, orchestrator=mock_orch)

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "Dry Run:            Yes (Spanner mutations avoided)" in captured


# ============================================================================
# 4. CLI Entrypoint Integration Tests
# ============================================================================


def test_cli_main_dispatch():
    with patch("scripts.ingest_pdf_manual.run_manual_ingestion") as mock_run:
        mock_run.return_value = (MagicMock(), 0)

        exit_code = manual_runner.main(["gs://sales-discovery-mvp-mumbai/whitepapers/test.pdf", "--dry-run"])

        assert exit_code == 0
        mock_run.assert_called_once_with(
            "gs://sales-discovery-mvp-mumbai/whitepapers/test.pdf",
            dry_run=True,
        )


def test_manual_runner_default_client_initialization(capsys):
    """Verifies that running without an explicit orchestrator instantiates clients with exact matching arguments."""
    with patch("scripts.ingest_pdf_manual.get_storage_client") as mock_get_storage, \
         patch("scripts.ingest_pdf_manual.DocumentAiLayoutClient") as mock_docai_cls, \
         patch("scripts.ingest_pdf_manual.get_database") as mock_get_db, \
         patch("scripts.ingest_pdf_manual.PdfIngestionOrchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.ingest_pdf.return_value = DocumentIngestionResult(
            gcs_uri="gs://sales-discovery-mvp-mumbai/whitepapers/test.pdf",
            status=IngestionStatus.SUCCESS,
            stage=IngestionStage.COMPLETED,
            document_id="doc_test123",
            chunks_count=1,
            write_result=make_mock_write_result("doc_test123"),
            duration_ms=100.0,
        )
        mock_orch_cls.return_value = mock_orch

        res, exit_code = manual_runner.run_manual_ingestion(
            "gs://sales-discovery-mvp-mumbai/whitepapers/test.pdf",
            dry_run=False,
            orchestrator=None,
        )

        assert exit_code == 0
        assert res.status == IngestionStatus.SUCCESS

        # Verify DocumentAiLayoutClient was called with processor_name and endpoint (NOT project_id)
        mock_docai_cls.assert_called_once()
        _, kwargs = mock_docai_cls.call_args
        assert "processor_name" in kwargs
        assert "endpoint" in kwargs
        assert "project_id" not in kwargs

        # Verify get_database was called with 0 arguments
        mock_get_db.assert_called_once_with()


def test_manual_runner_default_client_initialization_dry_run():
    """Verifies that dry-run wraps Spanner in DryRunSpannerDatabase."""
    with patch("scripts.ingest_pdf_manual.get_storage_client"), \
         patch("scripts.ingest_pdf_manual.DocumentAiLayoutClient"), \
         patch("scripts.ingest_pdf_manual.get_database") as mock_get_db, \
         patch("scripts.ingest_pdf_manual.PdfIngestionOrchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.ingest_pdf.return_value = DocumentIngestionResult(
            gcs_uri="gs://sales-discovery-mvp-mumbai/whitepapers/test.pdf",
            status=IngestionStatus.SUCCESS,
            stage=IngestionStage.COMPLETED,
            document_id="doc_test123",
            chunks_count=1,
            write_result=make_mock_write_result("doc_test123"),
            duration_ms=100.0,
        )
        mock_orch_cls.return_value = mock_orch

        res, exit_code = manual_runner.run_manual_ingestion(
            "gs://sales-discovery-mvp-mumbai/whitepapers/test.pdf",
            dry_run=True,
            orchestrator=None,
        )

        assert exit_code == 0
        # Verify orchestrator received DryRunSpannerDatabase
        _, kwargs = mock_orch_cls.call_args
        assert isinstance(kwargs["spanner_database"], manual_runner.DryRunSpannerDatabase)
