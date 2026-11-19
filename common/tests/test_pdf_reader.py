"""Mocked unit tests for GCS PDF discovery and retrieval utility."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import Forbidden, NotFound

from common.config import settings
from common.storage.pdf_reader import (
    DownloadedPdf,
    GcsAccessError,
    PdfMetadata,
    UnapprovedBucketError,
    compute_sha256,
    download_pdf,
    download_pdf_from_uri,
    get_approved_buckets,
    is_pdf_blob,
    list_pdf_blobs,
    parse_gcs_uri,
    validate_bucket_approved,
)


class MockBlob:
    """Mock GCS Blob."""

    def __init__(
        self,
        name: str,
        content: bytes = b"%PDF-1.4 mock pdf data",
        content_type: str = "application/pdf",
        generation: int = 123456789,
        updated: datetime | None = None,
    ):
        self.name = name
        self.content = content
        self.size = len(content)
        self.content_type = content_type
        self.generation = generation
        self.updated = updated or datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)

    def download_as_bytes(self) -> bytes:
        return self.content


# ============================================================================
# 1. URL & Filter Helper Tests
# ============================================================================


def test_is_pdf_blob():
    assert is_pdf_blob("report.pdf") is True
    assert is_pdf_blob("ARCH.PDF") is True
    assert is_pdf_blob("folder/doc.Pdf") is True
    assert is_pdf_blob("no-ext-doc", content_type="application/pdf") is True
    assert is_pdf_blob("no-ext-doc", content_type="APPLICATION/PDF") is True

    # Negative checks
    assert is_pdf_blob("document.docx") is False
    assert is_pdf_blob("slides.pptx") is False
    assert is_pdf_blob("notes.txt") is False
    assert is_pdf_blob("image.png") is False
    assert is_pdf_blob("folder/") is False
    assert is_pdf_blob("folder/subfolder/") is False
    assert is_pdf_blob("") is False
    assert is_pdf_blob(None) is False


def test_parse_gcs_uri():
    bucket, name = parse_gcs_uri("gs://sales-discovery-mvp/pdfs/sample.pdf")
    assert bucket == "sales-discovery-mvp"
    assert name == "pdfs/sample.pdf"

    b2, n2 = parse_gcs_uri("gs://sales-discovery-mvp-mumbai/file.pdf")
    assert b2 == "sales-discovery-mvp-mumbai"
    assert n2 == "file.pdf"

    # Malformed URIs
    with pytest.raises(ValueError, match="Invalid GCS URI"):
        parse_gcs_uri("https://storage.googleapis.com/bucket/file.pdf")

    with pytest.raises(ValueError, match="Invalid GCS URI"):
        parse_gcs_uri("s3://bucket/file.pdf")

    with pytest.raises(ValueError, match="Invalid GCS URI"):
        parse_gcs_uri("")


def test_approved_buckets():
    approved = get_approved_buckets()
    assert settings.gcs_legacy_bucket in approved
    assert settings.gcs_mumbai_bucket in approved

    validate_bucket_approved(settings.gcs_legacy_bucket)
    validate_bucket_approved(settings.gcs_mumbai_bucket)

    with pytest.raises(UnapprovedBucketError, match="not an approved bucket"):
        validate_bucket_approved("unauthorized-public-bucket")


# ============================================================================
# 2. PDF Listing & Filtering Tests
# ============================================================================


def test_list_pdf_blobs_filtering():
    mock_blobs = [
        MockBlob("whitepaper.pdf", b"%PDF-1.4 w1"),
        MockBlob("DECK.PDF", b"%PDF-1.4 d1"),
        MockBlob("raw-docs/", b""),  # Directory marker
        MockBlob("notes.docx", b"PK docx content", content_type="application/vnd.openxmlformats"),
        MockBlob("data.json", b"{}", content_type="application/json"),
        MockBlob("unnamed-report", b"%PDF-1.4 custom", content_type="application/pdf"),
        MockBlob("sub/nested.pdf", b"%PDF-1.4 sub"),
    ]

    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    mock_client.list_blobs.return_value = mock_blobs

    results = list_pdf_blobs(
        bucket_name=settings.gcs_mumbai_bucket,
        prefix="raw-docs",
        client=mock_client,
    )

    # Expected: whitepaper.pdf, DECK.PDF, unnamed-report, sub/nested.pdf
    assert len(results) == 4
    names = [r.name for r in results]
    assert "whitepaper.pdf" in names
    assert "DECK.PDF" in names
    assert "unnamed-report" in names
    assert "sub/nested.pdf" in names

    assert all(r.bucket == settings.gcs_mumbai_bucket for r in results)
    assert all(r.gcs_uri.startswith("gs://sales-discovery-mvp-mumbai/") for r in results)


def test_list_pdf_blobs_both_approved_buckets():
    mock_client = MagicMock()

    mumbai_blobs = [MockBlob("doc_mumbai.pdf")]
    legacy_blobs = [MockBlob("doc_legacy.pdf")]

    def mock_list_blobs(bucket, prefix=None, max_results=None):
        if bucket.name == settings.gcs_mumbai_bucket:
            return mumbai_blobs
        elif bucket.name == settings.gcs_legacy_bucket:
            return legacy_blobs
        return []

    def make_mock_bucket(name):
        b = MagicMock()
        b.name = name
        return b

    mock_client.bucket.side_effect = make_mock_bucket
    mock_client.list_blobs.side_effect = mock_list_blobs

    results = list_pdf_blobs(client=mock_client)
    assert len(results) == 2
    assert results[0].bucket == settings.gcs_mumbai_bucket
    assert results[0].name == "doc_mumbai.pdf"
    assert results[1].bucket == settings.gcs_legacy_bucket
    assert results[1].name == "doc_legacy.pdf"


def test_list_pdf_blobs_pagination_limit():
    mock_blobs = [MockBlob(f"doc_{i}.pdf") for i in range(10)]
    mock_client = MagicMock()
    mock_client.list_blobs.return_value = mock_blobs

    results = list_pdf_blobs(
        bucket_name=settings.gcs_mumbai_bucket,
        client=mock_client,
        max_results=3,
    )
    assert len(results) == 3


def test_list_pdf_blobs_access_errors():
    mock_client = MagicMock()
    mock_client.list_blobs.side_effect = NotFound("Bucket not found")

    with pytest.raises(GcsAccessError, match="not found"):
        list_pdf_blobs(bucket_name=settings.gcs_mumbai_bucket, client=mock_client)

    mock_client.list_blobs.side_effect = Forbidden("Access forbidden")
    with pytest.raises(GcsAccessError, match="Access forbidden"):
        list_pdf_blobs(bucket_name=settings.gcs_mumbai_bucket, client=mock_client)


# ============================================================================
# 3. PDF Downloading & Provenance Tests
# ============================================================================


def test_download_pdf_success():
    raw_content = b"%PDF-1.4 sample content with binary \x00\x01\x02\xff"
    expected_hash = hashlib.sha256(raw_content).hexdigest()
    blob = MockBlob("proposals/arch.pdf", content=raw_content, generation=998877)

    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.get_blob.return_value = blob

    result: DownloadedPdf = download_pdf(
        bucket_name=settings.gcs_mumbai_bucket,
        blob_name="proposals/arch.pdf",
        client=mock_client,
    )

    assert result.content == raw_content
    assert result.metadata.bucket == settings.gcs_mumbai_bucket
    assert result.metadata.name == "proposals/arch.pdf"
    assert result.metadata.gcs_uri == f"gs://{settings.gcs_mumbai_bucket}/proposals/arch.pdf"
    assert result.metadata.size_bytes == len(raw_content)
    assert result.metadata.generation == 998877
    assert result.metadata.content_type == "application/pdf"
    assert result.metadata.sha256_hash == expected_hash
    assert result.metadata.sha256_hash == compute_sha256(raw_content)


def test_download_pdf_from_uri():
    raw_content = b"%PDF-1.4 test uri retrieval"
    blob = MockBlob("whitepaper.pdf", content=raw_content)

    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.get_blob.return_value = blob

    uri = f"gs://{settings.gcs_legacy_bucket}/whitepaper.pdf"
    result = download_pdf_from_uri(uri, client=mock_client)

    assert result.content == raw_content
    assert result.metadata.bucket == settings.gcs_legacy_bucket
    assert result.metadata.name == "whitepaper.pdf"
    assert result.metadata.sha256_hash == compute_sha256(raw_content)


def test_download_pdf_not_found():
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.get_blob.return_value = None  # GCS returns None if blob doesn't exist

    with pytest.raises(GcsAccessError, match="not found"):
        download_pdf(
            bucket_name=settings.gcs_mumbai_bucket,
            blob_name="nonexistent.pdf",
            client=mock_client,
        )


def test_download_pdf_unapproved_bucket():
    with pytest.raises(UnapprovedBucketError, match="not an approved bucket"):
        download_pdf(
            bucket_name="arbitrary-malicious-bucket",
            blob_name="doc.pdf",
        )
