"""Cloud Storage utilities for dual-bucket resolution and PDF retrieval."""

from common.storage.pdf_reader import (
    DownloadedPdf,
    GcsAccessError,
    GcsStorageError,
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
from common.storage.resolver import resolve_gcs_asset_uri

__all__ = [
    "DownloadedPdf",
    "GcsAccessError",
    "GcsStorageError",
    "PdfMetadata",
    "UnapprovedBucketError",
    "compute_sha256",
    "download_pdf",
    "download_pdf_from_uri",
    "get_approved_buckets",
    "is_pdf_blob",
    "list_pdf_blobs",
    "parse_gcs_uri",
    "resolve_gcs_asset_uri",
    "validate_bucket_approved",
]
