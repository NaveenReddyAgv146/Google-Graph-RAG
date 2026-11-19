"""Unit tests for Document Upload API endpoint and Authentication in Ingestion Copilot:
- POST /api/v1/documents/upload
- Security middleware (IAP JWT validation, test auth handling, fail-closed enforcement)
- File validation (extension, MIME type, magic bytes, size limits)
- Idempotency & GCS / Spanner / PubSub integration
- Document reconciliation & Pub/Sub push webhook OIDC authentication
- Durable orphan tracking GCS persistence
"""

import base64
import hashlib
import json
import os
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app.app_utils.orphan_tracker import OrphanAssetRecord, OrphanAssetRegistry
from app.fast_api_app import MAX_UPLOAD_SIZE_BYTES, app

CLIENT = TestClient(app)
VALID_PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
AUTHORIZED_SA = "ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com"


# ============================================================================
# Authentication and Security Middleware Tests
# ============================================================================


def test_upload_missing_auth_returns_401():
    """Unauthenticated requests must be immediately rejected with 401 Unauthorized."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "false"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            files={"file": ("test.pdf", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 401
        assert "Authentication required" in res.json()["detail"]


def test_upload_page_unauthenticated_returns_401():
    """Accessing GET /upload without authentication must be rejected with 401."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "false"}):
        res = CLIENT.get("/upload")
        assert res.status_code == 401
        assert "Authentication required" in res.json()["detail"]


def test_upload_page_authenticated_success():
    """Accessing GET /upload with valid authentication returns HTML with UI components."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.get(
            "/upload",
            headers={"X-Enterprise-Test-Principal": "data_engineer@enterprise.internal"},
        )
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        body = res.text
        # Assert key branding and principal injection
        assert "Enterprise GraphRAG" in body
        assert "Ingestion Portal" in body
        assert "data_engineer@enterprise.internal" in body
        # Assert drag-and-drop & file selection elements
        assert 'id="dropzone"' in body
        assert 'id="fileInput"' in body
        assert 'accept="application/pdf, .pdf"' in body
        # Assert progress bar & result sections
        assert 'id="progressSection"' in body
        assert 'id="resultCard"' in body
        assert 'id="uploadBtn"' in body
        # Assert same-origin endpoint path in JS
        assert "/api/v1/documents/upload" in body
        assert "50 * 1024 * 1024" in body


def test_upload_page_template_missing_returns_500():
    """If the template file is unexpectedly missing, GET /upload fails with 500."""
    from pathlib import Path
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}), \
         patch("app.fast_api_app._UPLOAD_TEMPLATE_PATH", Path("/tmp/nonexistent_path/upload_ui.html")):
        res = CLIENT.get(
            "/upload",
            headers={"X-Enterprise-Test-Principal": "data_engineer@enterprise.internal"},
        )
        assert res.status_code == 500
        assert "Upload UI template file not found" in res.json()["detail"]


def test_upload_invalid_test_principal_rejected():
    """Malformed test principal email is rejected with 401 even if test auth is enabled."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers={"X-Enterprise-Test-Principal": "not-an-email"},
            files={"file": ("test.pdf", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 401


def test_session_user_mismatch_returns_403():
    """Accessing another user's ADK session route returns 403 Forbidden."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.get(
            "/apps/app/users/target_user@example.com/sessions",
            headers={"X-Enterprise-Test-Principal": "attacker@example.com"},
        )
        assert res.status_code == 403
        assert "Identity mismatch" in res.json()["detail"]


# ============================================================================
# Validation Tests (Extension, MIME Type, Magic Bytes, File Size)
# ============================================================================


@pytest.fixture
def auth_headers():
    return {"X-Enterprise-Test-Principal": "cloud_architect@example.com"}


def test_upload_non_pdf_extension_rejected(auth_headers):
    """Files with non-PDF extension must be rejected with 400 Bad Request."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("presentation.pptx", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 400
        assert "Only PDF documents are supported" in res.json()["detail"]


def test_upload_invalid_mime_type_rejected(auth_headers):
    """Files with unsupported content-type must be rejected with 400 Bad Request."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("test.pdf", VALID_PDF_BYTES, "image/png")},
        )
        assert res.status_code == 400
        assert "Unsupported content type 'image/png'" in res.json()["detail"]


def test_upload_invalid_magic_bytes_rejected(auth_headers):
    """Files not starting with '%PDF-' magic bytes must be rejected with 400 Bad Request."""
    fake_pdf = b"NOT_A_PDF_DOCUMENT_CONTENT"
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("test.pdf", fake_pdf, "application/pdf")},
        )
        assert res.status_code == 400
        assert "magic byte verification" in res.json()["detail"]


def test_upload_empty_file_rejected(auth_headers):
    """Empty files must be rejected with 400 Bad Request."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        assert res.status_code == 400
        assert "Uploaded file is empty" in res.json()["detail"]


def test_upload_size_limit_exceeded(auth_headers):
    """Files exceeding 50MB must be rejected with 413 Request Entity Too Large."""
    oversized_chunk = VALID_PDF_BYTES + (b"X" * (MAX_UPLOAD_SIZE_BYTES + 100))
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("huge.pdf", oversized_chunk, "application/pdf")},
        )
        assert res.status_code == 413
        assert "exceeds maximum allowed size" in res.json()["detail"]


# ============================================================================
# Processing, Idempotency & Cloud Service Integration Tests
# ============================================================================


@patch("app.fast_api_app.get_database")
def test_upload_idempotency_existing_document(mock_get_db, auth_headers):
    """If document hash exists in Spanner, return existing record without re-uploading."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    expected_sha = hashlib.sha256(VALID_PDF_BYTES).hexdigest()
    expected_doc_id = f"doc_{expected_sha[:32]}"
    mock_snapshot.execute_sql.return_value = [(expected_doc_id, "EXTRACTION_COMPLETED", "architecture.pdf")]

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("architecture.pdf", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["is_duplicate"] is True
        assert data["document_id"] == expected_doc_id
        assert data["status"] == "EXTRACTION_COMPLETED"
        assert "already uploaded" in data["message"]


@patch("app.pubsub_worker.pubsub_v1.PublisherClient")
@patch("app.fast_api_app.storage.Client")
@patch("app.fast_api_app.get_database")
def test_upload_success_pipeline(mock_get_db, mock_storage_cls, mock_pubsub_cls, auth_headers):
    """Successful upload streams to GCS, records in Spanner, and publishes to Pub/Sub."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = []  # No existing doc

    # Mock GCS
    mock_gcs_client = MagicMock()
    mock_storage_cls.return_value = mock_gcs_client
    mock_bucket = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_blob = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    # Mock Pub/Sub
    mock_publisher = MagicMock()
    mock_pubsub_cls.return_value = mock_publisher
    mock_future = MagicMock()
    mock_future.result.return_value = "msg_123456789"
    mock_publisher.publish.return_value = mock_future

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("enterprise_wp.pdf", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 201
        data = res.json()

        expected_sha = hashlib.sha256(VALID_PDF_BYTES).hexdigest()
        assert data["is_duplicate"] is False
        assert data["document_id"] == f"doc_{expected_sha[:32]}"
        assert data["status"] == "DISCOVERED"
        assert data["file_name"] == "enterprise_wp.pdf"
        assert data["pubsub_notified"] is True
        assert data["pubsub_warning"] is None

        # Verify GCS upload called
        mock_blob.upload_from_string.assert_called_once_with(VALID_PDF_BYTES, content_type="application/pdf")
        assert mock_blob.metadata["uploaded_by"] == "cloud_architect@example.com"
        assert mock_blob.metadata["sha256"] == expected_sha

        # Verify Spanner transaction called
        mock_db.run_in_transaction.assert_called_once()

        # Verify Pub/Sub called
        mock_publisher.publish.assert_called_once()


@patch("app.pubsub_worker.pubsub_v1.PublisherClient")
@patch("app.fast_api_app.storage.Client")
@patch("app.fast_api_app.get_database")
def test_upload_pubsub_warning_marks_publish_failed(mock_get_db, mock_storage_cls, mock_pubsub_cls, auth_headers):
    """If Pub/Sub publishing fails after retries, document status in Spanner transitions to PUBLISH_FAILED."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = []

    mock_gcs_client = MagicMock()
    mock_storage_cls.return_value = mock_gcs_client
    mock_bucket = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_blob = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    # PubSub raises exception across all retries
    mock_publisher = MagicMock()
    mock_pubsub_cls.return_value = mock_publisher
    mock_publisher.publish.side_effect = RuntimeError("Pub/Sub quota exceeded")

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("whitepaper.pdf", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["status"] == "PUBLISH_FAILED"
        assert data["pubsub_notified"] is False
        assert "Pub/Sub quota exceeded" in data["pubsub_warning"]


@patch("app.fast_api_app.storage.Client")
@patch("app.fast_api_app.get_database")
def test_upload_gcs_failure_returns_502(mock_get_db, mock_storage_cls, auth_headers):
    """If GCS streaming fails, returns 502 Bad Gateway."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = []

    mock_gcs_client = MagicMock()
    mock_storage_cls.return_value = mock_gcs_client
    mock_bucket = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_blob = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_blob.upload_from_string.side_effect = RuntimeError("GCS bucket write access denied")

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("error.pdf", VALID_PDF_BYTES, "application/pdf")},
        )
        assert res.status_code == 502
        assert "Storage upload failed" in res.json()["detail"]


# ============================================================================
# Pub/Sub Push Webhook Security & OIDC Verification Tests
# ============================================================================


def test_pubsub_push_unauthenticated_returns_401():
    """Missing Authorization header on /api/v1/pubsub/push returns 401 Unauthorized."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "false"}):
        res = CLIENT.post("/api/v1/pubsub/push", json={"message": {"data": "dGVzdA=="}})
        assert res.status_code == 401
        assert "Pub/Sub OIDC authentication required" in res.json()["detail"]


def test_pubsub_push_invalid_bearer_token_returns_401():
    """Malformed or invalid JWT Bearer token on /api/v1/pubsub/push returns 401 Unauthorized."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "false"}):
        res = CLIENT.post(
            "/api/v1/pubsub/push",
            headers={"Authorization": "Bearer not-a-valid-jwt"},
            json={"message": {"data": "dGVzdA=="}},
        )
        assert res.status_code == 401


def test_pubsub_push_unauthorized_service_account_returns_403():
    """Unrecognized or unauthorized publisher service account returns 403 Forbidden."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/pubsub/push",
            headers={"X-Enterprise-Test-PubSub-Principal": "malicious-sa@unauthorized-domain.com"},
            json={"message": {"data": "dGVzdA=="}},
        )
        assert res.status_code == 403
        assert "Unauthorized Pub/Sub" in res.json()["detail"]


@patch("app.fast_api_app.extract_and_verify_pubsub_principal")
@patch("app.fast_api_app.get_database")
@patch("app.orchestrator.PdfIngestionOrchestrator")
def test_pubsub_push_authenticated_oidc_success(mock_orch_cls, mock_get_db, mock_extract_sa):
    """Valid Pub/Sub OIDC token executes ingestion pipeline successfully."""
    mock_extract_sa.return_value = AUTHORIZED_SA
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = [("DISCOVERED", 0)]

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    from app.orchestrator import DocumentIngestionResult, IngestionStage, IngestionStatus
    mock_orch.ingest_pdf.return_value = DocumentIngestionResult(
        document_id="doc_oidc_123",
        gcs_uri="gs://sales-discovery-mvp-mumbai/raw/oidc.pdf",
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        chunks_count=10,
    )

    event_payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": "doc_oidc_123",
        "gcs_uri": "gs://sales-discovery-mvp-mumbai/raw/oidc.pdf",
    }
    encoded = base64.b64encode(json.dumps(event_payload).encode("utf-8")).decode("utf-8")

    res = CLIENT.post(
        "/api/v1/pubsub/push",
        headers={"Authorization": "Bearer valid.mock.jwt"},
        json={"message": {"data": encoded, "messageId": "msg_oidc_1"}},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCESS"
    assert data["document_id"] == "doc_oidc_123"
    assert data["chunks_count"] == 10


@patch("app.fast_api_app.extract_and_verify_pubsub_principal")
@patch("app.fast_api_app.get_database")
@patch("app.orchestrator.PdfIngestionOrchestrator")
def test_pubsub_push_endpoint_duplicate_skipped(mock_orch_cls, mock_get_db, mock_extract_sa):
    """Duplicate Pub/Sub events for already-extracted documents are safely skipped without re-extraction."""
    mock_extract_sa.return_value = AUTHORIZED_SA
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    # Document already has status EXTRACTION_COMPLETED and 15 chunks
    mock_snapshot.execute_sql.return_value = [("EXTRACTION_COMPLETED", 15)]

    event_payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": "doc_dup_123",
        "gcs_uri": "gs://sales-discovery-mvp-mumbai/raw/dup.pdf",
    }
    encoded_data = base64.b64encode(json.dumps(event_payload).encode("utf-8")).decode("utf-8")

    res = CLIENT.post(
        "/api/v1/pubsub/push",
        headers={"Authorization": "Bearer valid.mock.jwt"},
        json={"message": {"data": encoded_data, "messageId": "msg_dup"}},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SKIPPED_DUPLICATE"
    assert data["document_id"] == "doc_dup_123"
    assert data["chunks_count"] == 15
    mock_orch_cls.assert_not_called()


# ============================================================================
# Document Reconciliation & Automatic Recovery Tests
# ============================================================================


@patch("app.fast_api_app.get_database")
@patch("app.pubsub_worker.publish_ingestion_event")
def test_reconcile_automatic_recovery_flow(mock_publish, mock_get_db, auth_headers):
    """Reconciliation repairs already-extracted documents and republishes true un-ingested docs."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    # Candidate 1: Stuck in DISCOVERED but already has 8 chunks -> repair to EXTRACTION_COMPLETED
    # Candidate 2: PUBLISH_FAILED with 0 chunks -> republish to Pub/Sub
    mock_snapshot.execute_sql.return_value = [
        ("doc_abandoned_1", "extracted.pdf", "gs://sales-discovery-mvp-mumbai/raw/extracted.pdf", "DISCOVERED", 8),
        ("doc_stuck_2", "unprocessed.pdf", "gs://sales-discovery-mvp-mumbai/raw/unprocessed.pdf", "PUBLISH_FAILED", 0),
    ]
    mock_publish.return_value = (True, "msg_republished_2")

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true"}):
        res = CLIENT.post(
            "/api/v1/reconciliation/trigger?batch_size=50",
            headers=auth_headers,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "SUCCESS"
        assert data["candidates_inspected"] == 2
        assert data["recovered_already_extracted"] == 1
        assert data["republished_for_ingestion"] == 1
        assert data["failed_republish"] == 0

        # Verify Spanner update was executed for both documents
        assert mock_db.run_in_transaction.call_count == 2


def test_reconcile_documents_unauthenticated_returns_401():
    """Unauthenticated call to reconciliation endpoint returns 401 Unauthorized."""
    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "false"}):
        res = CLIENT.post("/api/v1/reconciliation/trigger")
        assert res.status_code == 401


# ============================================================================
# Durable Orphan Tracking GCS Persistence Tests
# ============================================================================


def test_orphan_tracker_durable_gcs_persistence():
    """OrphanAssetRegistry writes durable JSON manifest to Cloud Storage on failure."""
    mock_storage_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_storage_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    tracker = OrphanAssetRegistry(storage_client=mock_storage_client)
    now = time.time()
    record = OrphanAssetRecord(
        document_id="doc_orphan_test",
        presentation_id="pres_1234567890abcdef",
        gcs_uris=["gs://sales-discovery-mvp-mumbai/visual_assets/thumb1.png"],
        reason="Spanner conflict under objectCreator policy",
        error_detail="403 Forbidden: storage.objects.delete required",
        timestamp=now,
    )

    tracker.record_orphans(record)

    # Verify GCS write
    mock_storage_client.bucket.assert_called_once_with("sales-discovery-mvp-mumbai")
    expected_blob_name = f"orphans/doc_orphan_test_{int(now * 1000)}.json"
    mock_bucket.blob.assert_called_once_with(expected_blob_name)
    mock_blob.upload_from_string.assert_called_once()
    assert mock_blob.metadata["document_id"] == "doc_orphan_test"
    assert mock_blob.metadata["orphan_count"] == "1"

    # Verify also available in-memory
    assert len(tracker.get_records()) == 1
    assert tracker.get_all_orphan_uris() == ["gs://sales-discovery-mvp-mumbai/visual_assets/thumb1.png"]
