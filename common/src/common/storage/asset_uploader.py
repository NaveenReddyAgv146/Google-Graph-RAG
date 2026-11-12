"""Google Slides visual asset GCS storage utility with idempotency and compensation."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from google.api_core.exceptions import (
    BadGateway,
    Forbidden,
    GatewayTimeout,
    GoogleAPICallError,
    InternalServerError,
    NotFound,
    ServiceUnavailable,
    TooManyRequests,
)
from google.cloud import storage

from common.config import settings
from common.models.schema import VisualAssetNode
from common.storage.pdf_reader import get_approved_buckets, parse_gcs_uri

logger = logging.getLogger("common.storage.asset_uploader")

RETRYABLE_EXCEPTIONS = (
    TooManyRequests,
    InternalServerError,
    BadGateway,
    ServiceUnavailable,
    GatewayTimeout,
)

IMAGE_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"BM", "image/bmp", "bmp"),
    (b"II*\x00", "image/tiff", "tiff"),
    (b"MM\x00*", "image/tiff", "tiff"),
]


class AssetStorageError(Exception):
    """Base exception for visual asset storage operations."""

    pass


class UnapprovedBucketError(AssetStorageError):
    """Raised when target bucket is not in approved buckets list."""

    pass


class InvalidImagePayloadError(AssetStorageError, ValueError):
    """Raised when image content is empty or not bytes."""

    pass


class InvalidImageSignatureError(AssetStorageError, ValueError):
    """Raised when image content does not match a recognized signature or expected MIME type."""

    pass


class AssetUploadError(AssetStorageError):
    """Raised when GCS upload fails after retry exhaustion."""

    pass


class AssetCleanupError(AssetStorageError):
    """Raised when asset rollback or cleanup fails."""

    pass


class AssetAccessDeniedError(AssetStorageError):
    """Raised when caller lacks permission to access or write to GCS bucket."""

    pass


@dataclass(frozen=True)
class UploadedVisualAsset:
    """Provenance and metadata of a visual asset in Cloud Storage."""

    visual_id: str
    document_id: str
    slide_number: int
    gcs_uri: str
    bucket: str
    object_name: str
    sha256_hash: str
    size_bytes: int
    content_type: str
    asset_type: str  # "SLIDE" or "IMAGE"
    description: str | None = None
    action: str = "UPLOADED"  # "UPLOADED", "SKIPPED_EXISTING"

    def to_visual_asset_node(self) -> VisualAssetNode:
        """Converts uploaded asset into a validated VisualAssetNode."""
        return VisualAssetNode(
            visual_id=self.visual_id,
            document_id=self.document_id,
            page_number=None,
            slide_number=self.slide_number,
            image_gcs_uri=self.gcs_uri,
            description=self.description,
            asset_type=self.asset_type,
        )


def generate_slide_visual_id(
    document_id: str,
    slide_number: int,
    asset_index: int = 0,
) -> str:
    """Generates a deterministic VisualId guaranteed <= 64 characters."""
    suffix = f"_s{slide_number:02d}_v{asset_index:03d}"
    max_prefix_len = 64 - 4 - len(suffix)
    clean_doc = document_id if document_id.startswith("doc_") else f"doc_{document_id}"
    doc_prefix = clean_doc[:max_prefix_len]
    return f"vis_{doc_prefix}{suffix}"


def build_slide_render_path(document_id: str, slide_number: int) -> str:
    """Builds deterministic object path for a full-slide render.

    Format: 'extracted-images/{document_id}/slides/slide-{N}-render.png'
    """
    clean_doc = document_id.strip()
    return f"extracted-images/{clean_doc}/slides/slide-{slide_number}-render.png"


def build_embedded_image_path(
    document_id: str, slide_number: int, image_index: int, extension: str = "png"
) -> str:
    """Builds deterministic object path for an embedded image.

    Format: 'extracted-images/{document_id}/slides/slide-{N}-image-{M}.{ext}'
    """
    clean_doc = document_id.strip()
    clean_ext = extension.lstrip(".").lower() or "png"
    return f"extracted-images/{clean_doc}/slides/slide-{slide_number}-image-{image_index}.{clean_ext}"


def detect_image_mime_type(content: bytes) -> tuple[str, str]:
    """Detects MIME type and extension from raw image byte signatures.

    Returns (mime_type, file_extension).
    Raises InvalidImagePayloadError or InvalidImageSignatureError.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise InvalidImagePayloadError(f"Expected bytes or bytearray, got {type(content).__name__}.")

    if len(content) == 0:
        raise InvalidImagePayloadError("Image content cannot be empty (0 bytes).")

    # Check magic byte signatures
    for prefix, mime, ext in IMAGE_SIGNATURES:
        if content.startswith(prefix):
            return mime, ext

    # Check WebP: starts with "RIFF" and has "WEBP" at bytes 8..12
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", "webp"

    # Check SVG: starts with XML or SVG tag
    stripped = content.lstrip()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<svg"):
        return "image/svg+xml", "svg"

    raise InvalidImageSignatureError(
        f"Unrecognized image byte signature (first 16 bytes: {content[:16]!r})."
    )


def validate_image_content(
    content: bytes, expected_mime_type: str | None = None
) -> tuple[str, str]:
    """Validates image payload and signature, optionally verifying expected MIME type."""
    mime, ext = detect_image_mime_type(content)
    if expected_mime_type and mime.lower() != expected_mime_type.lower():
        raise InvalidImageSignatureError(
            f"Expected MIME type '{expected_mime_type}', but detected '{mime}' from signature."
        )
    return mime, ext


class VisualAssetUploader:
    """Manages idempotent upload, retrieval, and compensating cleanup of slide visual assets."""

    def __init__(
        self,
        storage_client: storage.Client | None = None,
        bucket_name: str | None = None,
        max_retries: int = 3,
        initial_delay: float = 0.1,
    ) -> None:
        self.bucket_name = bucket_name or settings.gcs_mumbai_bucket
        self._validate_bucket()

        self.client = storage_client or storage.Client(project=settings.project_id)
        self.bucket = self.client.bucket(self.bucket_name)
        self.max_retries = max_retries
        self.initial_delay = initial_delay

    def _validate_bucket(self) -> None:
        approved = get_approved_buckets()
        if self.bucket_name not in approved:
            raise UnapprovedBucketError(
                f"Bucket '{self.bucket_name}' is not in approved list: {sorted(approved)}"
            )

    def _execute_with_retry(self, operation_name: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Executes a callable with bounded exponential backoff on transient errors."""
        attempt = 0
        while attempt <= self.max_retries:
            try:
                return func(*args, **kwargs)
            except RETRYABLE_EXCEPTIONS as e:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error(
                        "Exceeded max retries (%d) for %s: %s",
                        self.max_retries,
                        operation_name,
                        e,
                    )
                    raise AssetUploadError(
                        f"Failed {operation_name} after {self.max_retries} retries: {e}"
                    ) from e
                sleep_time = self.initial_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Transient error in %s: %s. Retrying in %.2fs (attempt %d/%d)...",
                    operation_name,
                    e,
                    sleep_time,
                    attempt,
                    self.max_retries,
                )
                time.sleep(sleep_time)
            except Forbidden as e:
                logger.error("Access denied for %s on bucket '%s': %s", operation_name, self.bucket_name, e)
                raise AssetAccessDeniedError(
                    f"Access denied to bucket '{self.bucket_name}' for {operation_name}: {e}"
                ) from e
            except GoogleAPICallError as e:
                logger.error("GCS API error in %s: %s", operation_name, e)
                raise AssetUploadError(f"GCS API error during {operation_name}: {e}") from e

    def _upload_blob_idempotent(
        self,
        object_name: str,
        content: bytes,
        mime_type: str,
        metadata: dict[str, str],
        visual_id: str,
        document_id: str,
        slide_number: int,
        asset_type: str,
        description: str | None = None,
    ) -> UploadedVisualAsset:
        """Idempotently uploads a blob to GCS, skipping if identical content exists."""
        sha256_hash = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)
        gcs_uri = f"gs://{self.bucket_name}/{object_name}"

        blob = self.bucket.blob(object_name)

        # Check existing blob for idempotency
        try:
            exists = self._execute_with_retry(
                f"check existence of {object_name}", blob.exists
            )
            if exists:
                self._execute_with_retry(f"reload metadata for {object_name}", blob.reload)
                existing_meta = blob.metadata or {}
                existing_sha = existing_meta.get("sha256")
                if (existing_sha and existing_sha == sha256_hash) or (
                    not existing_sha and blob.size == size_bytes
                ):
                    logger.info(
                        "Blob '%s' already exists with matching SHA-256 (%s). Skipping upload.",
                        gcs_uri,
                        sha256_hash,
                    )
                    return UploadedVisualAsset(
                        visual_id=visual_id,
                        document_id=document_id,
                        slide_number=slide_number,
                        gcs_uri=gcs_uri,
                        bucket=self.bucket_name,
                        object_name=object_name,
                        sha256_hash=sha256_hash,
                        size_bytes=size_bytes,
                        content_type=mime_type,
                        asset_type=asset_type,
                        description=description,
                        action="SKIPPED_EXISTING",
                    )
        except AssetStorageError:
            raise
        except Exception as e:
            logger.warning(
                "Could not check existing blob '%s': %s. Proceeding with upload.",
                object_name,
                e,
            )

        # Perform upload
        upload_meta = dict(metadata)
        upload_meta["sha256"] = sha256_hash
        blob.metadata = upload_meta

        def _do_upload() -> None:
            blob.upload_from_string(content, content_type=mime_type)

        self._execute_with_retry(f"upload {object_name}", _do_upload)

        logger.info(
            "Uploaded visual asset '%s' (%d bytes, sha256=%s)",
            gcs_uri,
            size_bytes,
            sha256_hash[:12],
        )

        return UploadedVisualAsset(
            visual_id=visual_id,
            document_id=document_id,
            slide_number=slide_number,
            gcs_uri=gcs_uri,
            bucket=self.bucket_name,
            object_name=object_name,
            sha256_hash=sha256_hash,
            size_bytes=size_bytes,
            content_type=mime_type,
            asset_type=asset_type,
            description=description,
            action="UPLOADED",
        )

    def upload_slide_render(
        self,
        document_id: str,
        slide_number: int,
        image_bytes: bytes,
        visual_id: str | None = None,
        description: str | None = None,
    ) -> UploadedVisualAsset:
        """Uploads a slide thumbnail render PNG to GCS."""
        mime, _ = validate_image_content(image_bytes, expected_mime_type="image/png")
        object_name = build_slide_render_path(document_id, slide_number)
        vid = visual_id or generate_slide_visual_id(document_id, slide_number, asset_index=0)
        desc = description or f"Slide {slide_number} render"

        metadata = {
            "document_id": document_id,
            "slide_number": str(slide_number),
            "asset_type": "SLIDE",
        }

        return self._upload_blob_idempotent(
            object_name=object_name,
            content=image_bytes,
            mime_type=mime,
            metadata=metadata,
            visual_id=vid,
            document_id=document_id,
            slide_number=slide_number,
            asset_type="SLIDE",
            description=desc,
        )

    def upload_embedded_image(
        self,
        document_id: str,
        slide_number: int,
        image_index: int,
        image_bytes: bytes,
        visual_id: str | None = None,
        description: str | None = None,
        source_url: str | None = None,
        object_id: str | None = None,
    ) -> UploadedVisualAsset:
        """Uploads an extracted embedded image to GCS."""
        mime, ext = validate_image_content(image_bytes)
        object_name = build_embedded_image_path(document_id, slide_number, image_index, extension=ext)
        vid = visual_id or generate_slide_visual_id(document_id, slide_number, asset_index=image_index)
        desc = description or f"Embedded image on slide {slide_number}"

        metadata = {
            "document_id": document_id,
            "slide_number": str(slide_number),
            "image_index": str(image_index),
            "asset_type": "IMAGE",
            "source_url": source_url or "",
            "object_id": object_id or "",
        }

        return self._upload_blob_idempotent(
            object_name=object_name,
            content=image_bytes,
            mime_type=mime,
            metadata=metadata,
            visual_id=vid,
            document_id=document_id,
            slide_number=slide_number,
            asset_type="IMAGE",
            description=desc,
        )

    def upload_presentation_assets(
        self,
        document_id: str,
        slide_renders: dict[int, bytes],
        embedded_images: list[dict[str, Any]] | None = None,
        rollback_on_failure: bool = True,
    ) -> list[UploadedVisualAsset]:
        """Coordinates upload of all slide renders and embedded images for a presentation.

        If rollback_on_failure is True, newly uploaded blobs are deleted if any step fails.
        """
        newly_uploaded: list[UploadedVisualAsset] = []
        results: list[UploadedVisualAsset] = []

        try:
            # 1. Slide renders
            for slide_number in sorted(slide_renders.keys()):
                render_bytes = slide_renders[slide_number]
                res = self.upload_slide_render(
                    document_id=document_id,
                    slide_number=slide_number,
                    image_bytes=render_bytes,
                )
                results.append(res)
                if res.action == "UPLOADED":
                    newly_uploaded.append(res)

            # 2. Embedded images
            if embedded_images:
                for img_info in embedded_images:
                    slide_num = int(img_info.get("slide_number", 1))
                    img_idx = int(img_info.get("image_index", 1))
                    content = img_info.get("image_bytes", b"")
                    source_url = img_info.get("source_url")
                    obj_id = img_info.get("object_id")
                    desc = img_info.get("description")
                    v_id = img_info.get("visual_id")

                    res = self.upload_embedded_image(
                        document_id=document_id,
                        slide_number=slide_num,
                        image_index=img_idx,
                        image_bytes=content,
                        visual_id=v_id,
                        description=desc,
                        source_url=source_url,
                        object_id=obj_id,
                    )
                    results.append(res)
                    if res.action == "UPLOADED":
                        newly_uploaded.append(res)

            return results

        except Exception as e:
            logger.error(
                "Upload batch failed for presentation doc '%s': %s",
                document_id,
                e,
            )
            if rollback_on_failure and newly_uploaded:
                logger.info(
                    "Rollback on failure: cleaning up %d newly uploaded blobs for doc '%s'...",
                    len(newly_uploaded),
                    document_id,
                )
                self.cleanup_uploaded_assets(newly_uploaded)
            raise

    def cleanup_uploaded_assets(self, assets: list[UploadedVisualAsset]) -> list[str]:
        """Compensating cleanup: deletes uploaded blobs from GCS.

        Returns the list of successfully deleted GCS URIs.
        """
        deleted_uris: list[str] = []
        for asset in assets:
            try:
                blob = self.bucket.blob(asset.object_name)

                def _do_delete() -> None:
                    try:
                        blob.delete()
                    except NotFound:
                        pass

                self._execute_with_retry(f"delete {asset.object_name}", _do_delete)
                deleted_uris.append(asset.gcs_uri)
                logger.info("Deleted compensating blob: %s", asset.gcs_uri)
            except Exception as e:
                logger.warning(
                    "Failed to delete blob '%s' during cleanup: %s",
                    asset.gcs_uri,
                    e,
                )
        return deleted_uris

    def delete_asset_by_uri(self, gcs_uri: str) -> bool:
        """Deletes a specific blob by its GCS URI."""
        bucket_name, blob_name = parse_gcs_uri(gcs_uri)
        if bucket_name != self.bucket_name:
            raise UnapprovedBucketError(
                f"Cannot delete blob from bucket '{bucket_name}': expected '{self.bucket_name}'."
            )
        blob = self.bucket.blob(blob_name)
        exists = self._execute_with_retry(f"check existence of {blob_name}", blob.exists)
        if exists:
            self._execute_with_retry(f"delete {blob_name}", blob.delete)
            return True
        return False
