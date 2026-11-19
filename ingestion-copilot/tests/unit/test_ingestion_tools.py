"""Unit tests for Ingestion Copilot ADK tools:
- trigger_pdf_ingestion
- trigger_slides_ingestion
- check_document_ingestion_status
- get_principal_from_tool_context
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from google.adk.tools import ToolContext

from app.orchestrator import (
    DocumentIngestionResult,
    IngestionStage,
    IngestionStatus,
)
from app.slides_orchestrator import SlidesIngestionResult
from app.tools.ingestion_tools import (
    check_document_ingestion_status,
    get_principal_from_tool_context,
    trigger_pdf_ingestion,
    trigger_slides_ingestion,
)


class MockToolContext:
    """Mock ADK ToolContext providing user_id."""

    def __init__(self, user_id: str | None = "architect@example.com"):
        self.user_id = user_id
        self.session = MagicMock()
        self.session.user_id = user_id


class MockSessionOnlyToolContext:
    """Mock ADK ToolContext providing user_id only on session."""

    def __init__(self, user_id: str = "session_user@example.com"):
        self.user_id = None
        self.session = MagicMock()
        self.session.user_id = user_id


# ============================================================================
# ToolContext Principal Resolution Tests
# ============================================================================


def test_get_principal_from_tool_context_direct():
    ctx = MockToolContext("user@example.com")
    assert get_principal_from_tool_context(ctx) == "user@example.com"


def test_get_principal_from_tool_context_via_session():
    ctx = MockSessionOnlyToolContext("session_user@example.com")
    assert get_principal_from_tool_context(ctx) == "session_user@example.com"


def test_get_principal_from_tool_context_invalid_email_format():
    invalid_ctx = MockToolContext("not-an-email")
    with pytest.raises(PermissionError, match="Invalid or malformed principal identity"):
        get_principal_from_tool_context(invalid_ctx)

    empty_str_ctx = MockToolContext("   ")
    with pytest.raises(PermissionError, match="Missing trusted user identity"):
        get_principal_from_tool_context(empty_str_ctx)

    trailing_at_ctx = MockToolContext("user@domain")
    # domain needs a dot per EMAIL_REGEX
    with pytest.raises(PermissionError, match="Invalid or malformed principal identity"):
        get_principal_from_tool_context(trailing_at_ctx)


def test_get_principal_from_tool_context_missing():
    with pytest.raises(PermissionError, match="Missing trusted user identity"):
        get_principal_from_tool_context(None)

    empty_ctx = MockToolContext(user_id=None)
    empty_ctx.session.user_id = None
    with pytest.raises(PermissionError, match="Missing trusted user identity"):
        get_principal_from_tool_context(empty_ctx)


# ============================================================================
# trigger_pdf_ingestion Tests
# ============================================================================


def test_trigger_pdf_ingestion_unauthorized():
    res_str = trigger_pdf_ingestion("gs://sales-discovery-mvp-mumbai/raw/report.pdf", tool_context=None)
    res = json.loads(res_str)
    assert res["status"] == "UNAUTHORIZED"
    assert "Caller identity could not be verified" in res["error"]


def test_trigger_pdf_ingestion_unapproved_bucket():
    ctx = MockToolContext()
    res_str = trigger_pdf_ingestion("gs://unapproved-bucket/test.pdf", tool_context=ctx)
    res = json.loads(res_str)
    assert res["status"] == "VALIDATION_ERROR"
    assert "is not an approved bucket" in res["error"]


def test_trigger_pdf_ingestion_invalid_extension():
    ctx = MockToolContext()
    res_str = trigger_pdf_ingestion("gs://sales-discovery-mvp-mumbai/raw/report.docx", tool_context=ctx)
    res = json.loads(res_str)
    assert res["status"] == "INVALID_ARGUMENT"
    assert "must point to a valid .pdf file" in res["error"]


@patch("app.tools.ingestion_tools.PdfIngestionOrchestrator")
def test_trigger_pdf_ingestion_success(mock_orchestrator_cls):
    mock_instance = MagicMock()
    mock_orchestrator_cls.return_value = mock_instance
    mock_instance.ingest_pdf.return_value = DocumentIngestionResult(
        document_id="doc_1234567890abcdef",
        gcs_uri="gs://sales-discovery-mvp-mumbai/raw/arch_review.pdf",
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        chunks_count=12,
        batch_required=False,
        duration_ms=450.5,
    )

    ctx = MockToolContext("lead_architect@example.com")
    res_str = trigger_pdf_ingestion("gs://sales-discovery-mvp-mumbai/raw/arch_review.pdf", tool_context=ctx)
    res = json.loads(res_str)

    assert res["status"] == "SUCCESS"
    assert res["stage"] == "COMPLETED"
    assert res["document_id"] == "doc_1234567890abcdef"
    assert res["chunks_count"] == 12
    assert res["batch_required"] is False
    mock_instance.ingest_pdf.assert_called_once_with("gs://sales-discovery-mvp-mumbai/raw/arch_review.pdf")


@patch("app.tools.ingestion_tools.PdfIngestionOrchestrator")
def test_trigger_pdf_ingestion_failure(mock_orchestrator_cls):
    mock_instance = MagicMock()
    mock_orchestrator_cls.return_value = mock_instance
    mock_instance.ingest_pdf.side_effect = RuntimeError("Document AI parser service unavailable")

    ctx = MockToolContext()
    res_str = trigger_pdf_ingestion("gs://sales-discovery-mvp-mumbai/raw/report.pdf", tool_context=ctx)
    res = json.loads(res_str)

    assert res["status"] == "FAILED"
    assert "Document AI parser service unavailable" in res["error"]


# ============================================================================
# trigger_slides_ingestion Tests
# ============================================================================


def test_trigger_slides_ingestion_unauthorized():
    res_str = trigger_slides_ingestion("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms", tool_context=None)
    res = json.loads(res_str)
    assert res["status"] == "UNAUTHORIZED"


def test_trigger_slides_ingestion_invalid_identifier():
    ctx = MockToolContext()
    res_str = trigger_slides_ingestion("invalid!presentation!id", tool_context=ctx)
    res = json.loads(res_str)
    assert res["status"] == "INVALID_ARGUMENT"
    assert "Invalid Google Slides identifier" in res["error"]


@patch("app.tools.ingestion_tools.GoogleSlidesOrchestrator")
def test_trigger_slides_ingestion_success(mock_orchestrator_cls):
    mock_instance = MagicMock()
    mock_orchestrator_cls.return_value = mock_instance
    mock_instance.ingest_presentation.return_value = SlidesIngestionResult(
        document_id="doc_slides_abcdef123456",
        presentation_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        slide_count=25,
        chunks_count=30,
        visual_assets_count=8,
        duration_ms=1250.0,
    )

    ctx = MockToolContext()
    url = "https://docs.google.com/presentation/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit"
    res_str = trigger_slides_ingestion(url, tool_context=ctx)
    res = json.loads(res_str)

    assert res["status"] == "SUCCESS"
    assert res["stage"] == "COMPLETED"
    assert res["document_id"] == "doc_slides_abcdef123456"
    assert res["presentation_id"] == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
    assert res["slide_count"] == 25
    assert res["chunks_count"] == 30
    assert res["visual_assets_count"] == 8
    mock_instance.ingest_presentation.assert_called_once_with("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms")


@patch("app.tools.ingestion_tools.GoogleSlidesOrchestrator")
def test_trigger_slides_ingestion_failure(mock_orchestrator_cls):
    mock_instance = MagicMock()
    mock_orchestrator_cls.return_value = mock_instance
    mock_instance.ingest_presentation.side_effect = ValueError("Drive API returned 404 presentation not found")

    ctx = MockToolContext()
    res_str = trigger_slides_ingestion("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms", tool_context=ctx)
    res = json.loads(res_str)

    assert res["status"] == "FAILED"
    assert "Drive API returned 404" in res["error"]


# ============================================================================
# check_document_ingestion_status Tests
# ============================================================================


def test_check_document_ingestion_status_unauthorized():
    res_str = check_document_ingestion_status("doc_123", tool_context=None)
    res = json.loads(res_str)
    assert res["status"] == "UNAUTHORIZED"


def test_check_document_ingestion_status_invalid_argument():
    ctx = MockToolContext()
    res_str = check_document_ingestion_status(None, None, tool_context=ctx)
    res = json.loads(res_str)
    assert res["status"] == "INVALID_ARGUMENT"


@patch("app.tools.ingestion_tools.get_database")
def test_check_document_ingestion_status_not_found(mock_get_db):
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = []

    ctx = MockToolContext()
    res_str = check_document_ingestion_status(document_id="doc_missing", tool_context=ctx)
    res = json.loads(res_str)

    assert res["found"] is False
    assert "No document found" in res["message"]


@patch("app.tools.ingestion_tools.get_database")
def test_check_document_ingestion_status_pending_review(mock_get_db):
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    # Return: 1) doc row, 2) chunk count row, 3) card rows (PENDING)
    mock_snapshot.execute_sql.side_effect = [
        [("doc_pending_123", "arch_doc.pdf", "application/pdf", "gs://sales-discovery-mvp-mumbai/raw/arch_doc.pdf", "COMPLETED")],
        [(10, 10)],  # total chunks, embedded chunks
        [("card_001", "Project Apollo", "PENDING")],
    ]

    ctx = MockToolContext()
    res_str = check_document_ingestion_status(document_id="doc_pending_123", tool_context=ctx)
    res = json.loads(res_str)

    assert res["found"] is True
    assert res["document_id"] == "doc_pending_123"
    assert res["extraction_status"] == "COMPLETED"
    assert res["chunks_count"] == 10
    assert res["embedded_chunks_count"] == 10
    assert res["enrichment_status"] == "COMPLETED"
    assert res["human_review_status"] == "PENDING_REVIEW"
    assert res["can_be_queried_by_discovery_copilot"] is False
    assert len(res["solution_cards"]) == 1
    assert res["solution_cards"][0]["verification_status"] == "PENDING"


@patch("app.tools.ingestion_tools.get_database")
def test_check_document_ingestion_status_approved(mock_get_db):
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    # Return: 1) doc row, 2) chunk count row, 3) card rows (one APPROVED, one PENDING)
    mock_snapshot.execute_sql.side_effect = [
        [("doc_appr_123", "approved_doc.pdf", "application/pdf", "gs://sales-discovery-mvp-mumbai/raw/approved_doc.pdf", "COMPLETED")],
        [(15, 15)],
        [("card_001", "Project Apollo", "APPROVED"), ("card_002", "Project Artemis", "PENDING")],
    ]

    ctx = MockToolContext()
    res_str = check_document_ingestion_status(document_id="doc_appr_123", tool_context=ctx)
    res = json.loads(res_str)

    assert res["found"] is True
    assert res["human_review_status"] == "APPROVED"
    assert res["can_be_queried_by_discovery_copilot"] is True


@patch("app.tools.ingestion_tools.get_database")
def test_check_document_ingestion_status_rejected(mock_get_db):
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    # Return: 1) doc row, 2) chunk count row, 3) card rows (all REJECTED)
    mock_snapshot.execute_sql.side_effect = [
        [("doc_rej_123", "rejected_doc.pdf", "application/pdf", "gs://sales-discovery-mvp-mumbai/raw/rejected_doc.pdf", "COMPLETED")],
        [(5, 5)],
        [("card_001", "Project Bad", "REJECTED")],
    ]

    ctx = MockToolContext()
    res_str = check_document_ingestion_status(document_name="rejected_doc.pdf", tool_context=ctx)
    res = json.loads(res_str)

    assert res["found"] is True
    assert res["human_review_status"] == "REJECTED"
    assert res["can_be_queried_by_discovery_copilot"] is False


@patch("app.tools.ingestion_tools.get_database")
def test_check_document_ingestion_status_db_error(mock_get_db):
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_db.snapshot.side_effect = RuntimeError("Spanner cluster unreachable")

    ctx = MockToolContext()
    res_str = check_document_ingestion_status("doc_err", tool_context=ctx)
    res = json.loads(res_str)

    assert res["status"] == "ERROR"
    assert "Database query failed" in res["error"]
