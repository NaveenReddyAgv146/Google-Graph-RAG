"""GCS PDF discovery, metadata extraction, and safe byte retrieval utility."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from google.api_core.exceptions import Forbidden, GoogleAPICallError, NotFound
from google.cloud import storage

from common.config import settings

logger = logging.getLogger("common.storage.pdf_reader")


class GcsStorageError(Exception):
    """Base exception for GCS storage operations."""

    pass


class UnapprovedBucketError(GcsStorageError):
    """Raised when an operation targets a bucket outside approved buckets."""

    pass


class GcsAccessError(GcsStorageError):
    """Raised when an object or bucket is inaccessible or not found."""

    pass


@dataclass(frozen=True)
class PdfMetadata:
    """Provenance and file metadata for a PDF in Cloud Storage."""

    bucket: str
    name: str
    gcs_uri: str
    size_bytes: int
    generation: int | None
    content_type: str | None
    sha256_hash: str | None = None
    updated: datetime | None = None


@dataclass(frozen=True)
class DownloadedPdf:
    """In-memory representation of downloaded PDF with full provenance."""

    metadata: PdfMetadata
    content: bytes


def get_approved_buckets() -> set[str]:
    """Returns the set of approved GCS buckets for enterprise discovery."""
    return {settings.gcs_legacy_bucket, settings.gcs_mumbai_bucket}


def validate_bucket_approved(bucket_name: str) -> None:
    """Validates that bucket_name is in the approved buckets whitelist."""
    approved = get_approved_buckets()
    if bucket_name not in approved:
        raise UnapprovedBucketError(
            f"Bucket '{bucket_name}' is not an approved bucket. Approved: {sorted(approved)}"
        )


def parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    """
    Parses 'gs://bucket_name/blob_name' into (bucket_name, blob_name).
    Raises ValueError if URI is malformed.
    """
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI (must start with gs://): '{gcs_uri}'")

    path = gcs_uri[5:]
    parts = path.split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""
    return bucket_name, blob_name


def is_pdf_blob(blob_name: str, content_type: str | None = None) -> bool:
    """
    Determines whether a blob represents a PDF object:
    - Ends with .pdf (case-insensitive)
    - Or content_type is 'application/pdf'
    - Excludes directory markers ending in '/'
    """
    if not blob_name or blob_name.endswith("/"):
        return False

    if blob_name.lower().endswith(".pdf"):
        return True

    if content_type and content_type.lower() == "application/pdf":
        return True

    return False


def compute_sha256(content: bytes) -> str:
    """Computes the hex SHA-256 digest of binary content."""
    return hashlib.sha256(content).hexdigest()


def get_storage_client(project: str | None = None) -> storage.Client:
    """Returns a storage.Client using ADC and project configuration."""
    return storage.Client(project=project or settings.project_id)


def _blob_to_metadata(blob: Any, bucket_name: str) -> PdfMetadata:
    """Constructs PdfMetadata from a GCS Blob object."""
    name = getattr(blob, "name", "")
    size = getattr(blob, "size", 0) or 0
    generation = getattr(blob, "generation", None)
    content_type = getattr(blob, "content_type", None)
    updated = getattr(blob, "updated", None)

    return PdfMetadata(
        bucket=bucket_name,
        name=name,
        gcs_uri=f"gs://{bucket_name}/{name}",
        size_bytes=size,
        generation=int(generation) if generation is not None else None,
        content_type=content_type,
        sha256_hash=None,
        updated=updated,
    )


def list_pdf_blobs(
    bucket_name: str | None = None,
    prefix: str | None = None,
    client: storage.Client | None = None,
    max_results: int | None = None,
) -> list[PdfMetadata]:
    """
    Discovers and lists PDF objects from approved GCS bucket(s).

    If bucket_name is None, scans both approved buckets:
    - settings.gcs_mumbai_bucket
    - settings.gcs_legacy_bucket

    Safely filters non-PDF objects, empty directory markers, and handles pagination.
    """
    target_buckets: list[str]
    if bucket_name:
        validate_bucket_approved(bucket_name)
        target_buckets = [bucket_name]
    else:
        # Scan approved buckets in priority order (regional Mumbai, then legacy)
        target_buckets = [settings.gcs_mumbai_bucket, settings.gcs_legacy_bucket]

    storage_client = client or get_storage_client()
    pdf_results: list[PdfMetadata] = []

    clean_prefix = prefix.strip() if prefix else None
    if clean_prefix == "":
        clean_prefix = None

    for b_name in target_buckets:
        try:
            bucket = storage_client.bucket(b_name)
            blobs = storage_client.list_blobs(
                bucket,
                prefix=clean_prefix,
                max_results=max_results,
            )

            for blob in blobs:
                b_name_str = getattr(blob, "name", "")
                b_type = getattr(blob, "content_type", None)
                if is_pdf_blob(b_name_str, b_type):
                    meta = _blob_to_metadata(blob, b_name)
                    pdf_results.append(meta)
                    if max_results and len(pdf_results) >= max_results:
                        return pdf_results

        except NotFound as e:
            logger.warning(f"GCS bucket '{b_name}' not found: {e}")
            raise GcsAccessError(f"GCS bucket '{b_name}' not found.") from e
        except Forbidden as e:
            logger.warning(f"GCS bucket '{b_name}' access forbidden: {e}")
            raise GcsAccessError(f"Access forbidden to GCS bucket '{b_name}'.") from e
        except GoogleAPICallError as e:
            logger.error(f"GCS API error while listing bucket '{b_name}': {e}")
            raise GcsAccessError(f"Error accessing bucket '{b_name}': {e}") from e

    return pdf_results


def download_pdf(
    bucket_name: str,
    blob_name: str,
    client: storage.Client | None = None,
) -> DownloadedPdf:
    """
    Downloads PDF content as bytes from GCS without modifying or deleting source objects.
    Computes SHA-256 hash and returns DownloadedPdf with complete provenance.
    """
    validate_bucket_approved(bucket_name)

    storage_client = client or get_storage_client()
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.get_blob(blob_name)
        if blob is None:
            raise GcsAccessError(f"Object '{blob_name}' not found in bucket '{bucket_name}'.")

        if not is_pdf_blob(blob.name, blob.content_type):
            logger.warning(f"Object '{blob_name}' in bucket '{bucket_name}' is not a PDF.")

        # Read-only byte retrieval
        content = blob.download_as_bytes()
        sha256 = compute_sha256(content)

        meta = PdfMetadata(
            bucket=bucket_name,
            name=blob.name,
            gcs_uri=f"gs://{bucket_name}/{blob.name}",
            size_bytes=len(content),
            generation=int(blob.generation) if blob.generation is not None else None,
            content_type=blob.content_type,
            sha256_hash=sha256,
            updated=getattr(blob, "updated", None),
        )

        return DownloadedPdf(metadata=meta, content=content)

    except (NotFound, GcsAccessError) as e:
        logger.error(f"Failed to download '{blob_name}' from '{bucket_name}': {e}")
        raise GcsAccessError(f"Object '{blob_name}' not found in '{bucket_name}'.") from e
    except Forbidden as e:
        logger.error(f"Forbidden access to '{blob_name}' in '{bucket_name}': {e}")
        raise GcsAccessError(f"Access forbidden to '{blob_name}' in '{bucket_name}'.") from e
    except GoogleAPICallError as e:
        logger.error(f"GCS API error downloading '{blob_name}' from '{bucket_name}': {e}")
        raise GcsAccessError(f"Error downloading '{blob_name}' from '{bucket_name}': {e}") from e


def download_pdf_from_uri(
    gcs_uri: str,
    client: storage.Client | None = None,
) -> DownloadedPdf:
    """Downloads PDF using full 'gs://bucket/object' URI."""
    bucket_name, blob_name = parse_gcs_uri(gcs_uri)
    return download_pdf(bucket_name, blob_name, client=client)
