"""Unit tests for Document AI Layout Parser client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import (
    DeadlineExceeded,
    InvalidArgument,
    ResourceExhausted,
    ServiceUnavailable,
)

from common.config import settings
from common.parsers.documentai_client import (
    DEFAULT_SYNC_PAGE_LIMIT,
    MAX_INLINE_PAYLOAD_BYTES,
    DocumentAiApiError,
    DocumentAiLayoutClient,
    InvalidDocumentError,
    OversizedPayloadError,
    PageLimitExceededError,
    ParsedDocument,
    estimate_pdf_page_count,
    validate_pdf_payload,
)
from common.storage import DownloadedPdf, PdfMetadata

VALID_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"


def make_mock_docai_doc(pages_count: int = 2, text: str = "Extracted text content"):
    """Creates a mock documentai.Document response."""
    mock_doc = MagicMock()
    mock_doc.text = text
    mock_pages = [MagicMock(page_number=i + 1) for i in range(pages_count)]
    mock_doc.pages = mock_pages
    return mock_doc


# ============================================================================
# 1. Payload Validation Tests
# ============================================================================


def test_validate_pdf_payload_valid():
    validate_pdf_payload(VALID_PDF_BYTES)
    validate_pdf_payload(b"  \n\t%PDF-1.7 header")


def test_validate_pdf_payload_empty():
    with pytest.raises(InvalidDocumentError, match="content is empty"):
        validate_pdf_payload(b"")


def test_validate_pdf_payload_invalid_header():
    with pytest.raises(InvalidDocumentError, match="Invalid PDF header"):
        validate_pdf_payload(b"<!DOCTYPE html><html><body>Not a PDF</body></html>")

    with pytest.raises(InvalidDocumentError, match="Invalid PDF header"):
        validate_pdf_payload(b"Plain text file")


def test_validate_pdf_payload_oversized():
    fake_oversized = b"%PDF-1.4" + b"X" * (MAX_INLINE_PAYLOAD_BYTES + 10)
    with pytest.raises(OversizedPayloadError, match="exceeds synchronous inline limit"):
        validate_pdf_payload(fake_oversized)


def test_estimate_pdf_page_count():
    pdf_with_3_pages = (
        b"%PDF-1.4\n"
        b"/Type /Page \n"
        b"/Type /Page \n"
        b"/Type /Page \n"
    )
    assert estimate_pdf_page_count(pdf_with_3_pages) == 3

    assert estimate_pdf_page_count(b"no page objects") is None


# ============================================================================
# 2. Client Initialization & Configuration Tests
# ============================================================================


def test_client_defaults():
    with patch("common.parsers.documentai_client.documentai.DocumentProcessorServiceClient") as mock_cls:
        client = DocumentAiLayoutClient()
        assert client.processor_name == settings.documentai_processor_name
        assert client.endpoint == settings.documentai_endpoint
        assert client.sync_page_limit == DEFAULT_SYNC_PAGE_LIMIT


def test_client_custom_configuration():
    mock_raw_client = MagicMock()
    custom_proc = "projects/my-proj/locations/us/processors/custom-123"
    custom_endpoint = "us-documentai.googleapis.com"

    client = DocumentAiLayoutClient(
        processor_name=custom_proc,
        endpoint=custom_endpoint,
        client=mock_raw_client,
        sync_page_limit=10,
    )

    assert client.processor_name == custom_proc
    assert client.endpoint == custom_endpoint
    assert client.sync_page_limit == 10
    assert client.client == mock_raw_client


# ============================================================================
# 3. Synchronous Processing Success Tests
# ============================================================================


def test_process_with_downloaded_pdf():
    mock_raw_client = MagicMock()
    mock_doc = make_mock_docai_doc(pages_count=3, text="Architecture Overview text")
    mock_response = MagicMock(document=mock_doc)
    mock_raw_client.process_document.return_value = mock_response

    client = DocumentAiLayoutClient(client=mock_raw_client)

    meta = PdfMetadata(
        bucket="sales-discovery-mvp-mumbai",
        name="arch.pdf",
        gcs_uri="gs://sales-discovery-mvp-mumbai/arch.pdf",
        size_bytes=len(VALID_PDF_BYTES),
        generation=1,
        content_type="application/pdf",
        sha256_hash="abc123hash",
    )
    downloaded = DownloadedPdf(metadata=meta, content=VALID_PDF_BYTES)

    result: ParsedDocument = client.process(downloaded)

    assert result.page_count == 3
    assert result.text == "Architecture Overview text"
    assert result.metadata == meta
    assert result.raw_document == mock_doc
    assert result.processor_name == settings.documentai_processor_name

    # Verify request payload
    call_args = mock_raw_client.process_document.call_args
    req = call_args.kwargs["request"]
    assert req.name == settings.documentai_processor_name
    assert req.raw_document.content == VALID_PDF_BYTES
    assert req.raw_document.mime_type == "application/pdf"


def test_process_with_raw_bytes():
    mock_raw_client = MagicMock()
    mock_doc = make_mock_docai_doc(pages_count=1, text="Single page text")
    mock_raw_client.process_document.return_value = MagicMock(document=mock_doc)

    client = DocumentAiLayoutClient(client=mock_raw_client)
    result = client.process(VALID_PDF_BYTES)

    assert result.page_count == 1
    assert result.text == "Single page text"
    assert result.metadata is None


def test_process_unsupported_type():
    client = DocumentAiLayoutClient(client=MagicMock())
    with pytest.raises(TypeError, match="Unsupported document input type"):
        client.process(["not", "a", "valid", "type"])  # type: ignore


# ============================================================================
# 4. Page Limit Enforcement Tests
# ============================================================================


def test_page_limit_exceeded_in_response():
    mock_raw_client = MagicMock()
    # Mock document returning 16 pages (exceeds default limit of 15)
    mock_doc = make_mock_docai_doc(pages_count=16, text="Oversized doc")
    mock_raw_client.process_document.return_value = MagicMock(document=mock_doc)

    client = DocumentAiLayoutClient(client=mock_raw_client, sync_page_limit=15)

    with pytest.raises(PageLimitExceededError, match="contains 16 pages, which exceeds the synchronous limit of 15"):
        client.process(VALID_PDF_BYTES)


def test_page_limit_detected_in_api_error():
    mock_raw_client = MagicMock()
    mock_raw_client.process_document.side_effect = InvalidArgument("Document contains 25 pages. Limit is 15 pages.")

    client = DocumentAiLayoutClient(client=mock_raw_client)

    with pytest.raises(PageLimitExceededError, match="Batch processing is required"):
        client.process(VALID_PDF_BYTES)


def test_page_limit_preflight_heuristic():
    # 20 page markers in binary
    pages_20_pdf = b"%PDF-1.4\n" + b"/Type /Page \n" * 20
    client = DocumentAiLayoutClient(client=MagicMock(), sync_page_limit=15)

    with pytest.raises(PageLimitExceededError, match="Batch processing is required"):
        client.process(pages_20_pdf)


# ============================================================================
# 5. Retry & Error Handling Tests
# ============================================================================


def test_transient_retry_success():
    mock_raw_client = MagicMock()
    mock_doc = make_mock_docai_doc(pages_count=2, text="Recovered")

    # Fails twice with transient errors, then succeeds on 3rd attempt
    mock_raw_client.process_document.side_effect = [
        ServiceUnavailable("Service temporarily unavailable"),
        ResourceExhausted("Rate limit exceeded"),
        MagicMock(document=mock_doc),
    ]

    client = DocumentAiLayoutClient(
        client=mock_raw_client,
        max_retries=3,
        initial_backoff=0.01,  # Fast backoff for tests
    )

    result = client.process(VALID_PDF_BYTES)
    assert result.text == "Recovered"
    assert mock_raw_client.process_document.call_count == 3


def test_transient_retry_exhaustion():
    mock_raw_client = MagicMock()
    mock_raw_client.process_document.side_effect = ServiceUnavailable("Always down")

    client = DocumentAiLayoutClient(
        client=mock_raw_client,
        max_retries=2,
        initial_backoff=0.01,
    )

    with pytest.raises(DocumentAiApiError, match="failed after 2 retries"):
        client.process(VALID_PDF_BYTES)

    assert mock_raw_client.process_document.call_count == 3  # initial + 2 retries


def test_non_retryable_api_error():
    mock_raw_client = MagicMock()
    mock_raw_client.process_document.side_effect = InvalidArgument("Corrupt input file")

    client = DocumentAiLayoutClient(
        client=mock_raw_client,
        max_retries=3,
    )

    with pytest.raises(DocumentAiApiError, match="invalid argument error"):
        client.process(VALID_PDF_BYTES)

    # Non-retryable error should fail on first attempt
    assert mock_raw_client.process_document.call_count == 1

