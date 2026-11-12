"""Mocked unit tests for Google Slides visual asset GCS storage utility."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, call

import pytest
from google.api_core.exceptions import Forbidden, InternalServerError, ServiceUnavailable

from common.config import settings
from common.models.schema import VisualAssetNode
from common.storage.asset_uploader import (
    AssetAccessDeniedError,
    AssetUploadError,
    InvalidImagePayloadError,
    InvalidImageSignatureError,
    PreviousBlobState,
    UnapprovedBucketError,
    UploadedVisualAsset,
    VisualAssetUploader,
    build_embedded_image_path,
    build_slide_render_path,
    detect_image_mime_type,
    is_temporary_content_url,
    validate_image_content,
)

VALID_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
VALID_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
VALID_GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
VALID_WEBP_BYTES = b"RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00\x01\x00\x01\x00\x9d\x01*\x01"


# ============================================================================
# 1. PATH & IMAGE VALIDATION TESTS
# ============================================================================


def test_build_slide_render_path():
    path = build_slide_render_path("doc_slide_123", slide_number=1)
    assert path == "extracted-images/doc_slide_123/slides/slide-1-render.png"

    path_s10 = build_slide_render_path("doc_slide_456", slide_number=10)
    assert path_s10 == "extracted-images/doc_slide_456/slides/slide-10-render.png"


def test_build_embedded_image_path():
    path_png = build_embedded_image_path("doc_slide_123", slide_number=2, image_index=1, extension="png")
    assert path_png == "extracted-images/doc_slide_123/slides/slide-2-image-1.png"

    path_jpg = build_embedded_image_path("doc_slide_123", slide_number=3, image_index=2, extension=".jpeg")
    assert path_jpg == "extracted-images/doc_slide_123/slides/slide-3-image-2.jpeg"


def test_detect_image_mime_type_valid():
    assert detect_image_mime_type(VALID_PNG_BYTES) == ("image/png", "png")
    assert detect_image_mime_type(VALID_JPEG_BYTES) == ("image/jpeg", "jpg")
    assert detect_image_mime_type(VALID_GIF_BYTES) == ("image/gif", "gif")
    assert detect_image_mime_type(VALID_WEBP_BYTES) == ("image/webp", "webp")


def test_detect_image_mime_type_invalid():
    # Empty bytes
    with pytest.raises(InvalidImagePayloadError) as exc_info:
        detect_image_mime_type(b"")
    assert "cannot be empty" in str(exc_info.value)

    # Not bytes
    with pytest.raises(InvalidImagePayloadError):
        detect_image_mime_type("string_payload")  # type: ignore

    # Corrupted / non-image signature
    with pytest.raises(InvalidImageSignatureError) as exc_sig:
        detect_image_mime_type(b"NOT_AN_IMAGE_PAYLOAD")
    assert "Unrecognized image byte signature" in str(exc_sig.value)


def test_validate_image_content_expected_mime():
    # Correct expected MIME
    mime, ext = validate_image_content(VALID_PNG_BYTES, expected_mime_type="image/png")
    assert mime == "image/png"
    assert ext == "png"

    # Mismatched expected MIME
    with pytest.raises(InvalidImageSignatureError) as exc_info:
        validate_image_content(VALID_JPEG_BYTES, expected_mime_type="image/png")
    assert "Expected MIME type 'image/png', but detected 'image/jpeg'" in str(exc_info.value)


# ============================================================================
# 2. BUCKET APPROVAL TESTS
# ============================================================================


def test_uploader_bucket_approval():
    mock_storage = MagicMock()

    # Approved Mumbai bucket (default)
    uploader = VisualAssetUploader(storage_client=mock_storage, bucket_name=settings.gcs_mumbai_bucket)
    assert uploader.bucket_name == settings.gcs_mumbai_bucket

    # Approved Legacy bucket
    uploader_legacy = VisualAssetUploader(storage_client=mock_storage, bucket_name=settings.gcs_legacy_bucket)
    assert uploader_legacy.bucket_name == settings.gcs_legacy_bucket

    # Unapproved bucket
    with pytest.raises(UnapprovedBucketError):
        VisualAssetUploader(storage_client=mock_storage, bucket_name="unapproved-bucket-xyz")


# ============================================================================
# 3. UPLOAD & IDEMPOTENCY TESTS
# ============================================================================


def test_upload_slide_render_success():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    result = uploader.upload_slide_render(
        document_id="doc_slide_123",
        slide_number=1,
        image_bytes=VALID_PNG_BYTES,
        description="Slide 1 Architecture Render",
    )

    assert isinstance(result, UploadedVisualAsset)
    assert result.action == "CREATED"
    assert result.document_id == "doc_slide_123"
    assert result.slide_number == 1
    assert result.asset_type == "SLIDE"
    assert result.content_type == "image/png"
    assert result.object_name == "extracted-images/doc_slide_123/slides/slide-1-render.png"
    assert result.gcs_uri == f"gs://{settings.gcs_mumbai_bucket}/extracted-images/doc_slide_123/slides/slide-1-render.png"
    assert result.sha256_hash == hashlib.sha256(VALID_PNG_BYTES).hexdigest()

    # Verify upload was called
    mock_blob.upload_from_string.assert_called_once_with(VALID_PNG_BYTES, content_type="image/png")
    assert mock_blob.metadata["sha256"] == result.sha256_hash
    assert mock_blob.metadata["slide_number"] == "1"

    # Verify to_visual_asset_node
    node = result.to_visual_asset_node()
    assert isinstance(node, VisualAssetNode)
    assert node.visual_id == result.visual_id
    assert node.slide_number == 1
    assert node.page_number is None
    assert node.image_gcs_uri == result.gcs_uri
    assert node.asset_type == "SLIDE"


def test_upload_embedded_image_success():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    result = uploader.upload_embedded_image(
        document_id="doc_slide_123",
        slide_number=2,
        image_index=1,
        image_bytes=VALID_JPEG_BYTES,
        description="Architecture Diagram JPEG",
        source_url="https://source.com/img.jpg",
        object_id="elem_img_01",
    )

    assert result.action == "CREATED"
    assert result.asset_type == "IMAGE"
    assert result.content_type == "image/jpeg"
    assert result.object_name == "extracted-images/doc_slide_123/slides/slide-2-image-1.jpg"
    mock_blob.upload_from_string.assert_called_once_with(VALID_JPEG_BYTES, content_type="image/jpeg")
    assert mock_blob.metadata["object_id"] == "elem_img_01"


def test_upload_idempotency_skip_existing():
    """Verifies that an identical upload is skipped with zero mutation."""
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    content_hash = hashlib.sha256(VALID_PNG_BYTES).hexdigest()
    mock_blob.exists.return_value = True
    mock_blob.metadata = {"sha256": content_hash}
    mock_blob.size = len(VALID_PNG_BYTES)
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    result = uploader.upload_slide_render(
        document_id="doc_slide_123",
        slide_number=1,
        image_bytes=VALID_PNG_BYTES,
    )

    assert result.action == "SKIPPED_EXISTING"
    assert result.sha256_hash == content_hash
    # upload_from_string MUST NOT be called
    mock_blob.upload_from_string.assert_not_called()


def test_upload_overwrite_when_hash_differs():
    """Verifies that a blob is re-uploaded when content has changed and previous state is captured."""
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    mock_blob.exists.return_value = True
    mock_blob.metadata = {"sha256": "different_old_hash_123"}
    mock_blob.size = 999
    mock_blob.download_as_bytes.return_value = b"OLD_BYTES"
    mock_blob.content_type = "image/png"
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    result = uploader.upload_slide_render(
        document_id="doc_slide_123",
        slide_number=1,
        image_bytes=VALID_PNG_BYTES,
    )

    assert result.action == "OVERWRITTEN"
    assert result.previous_state is not None
    assert result.previous_state.content == b"OLD_BYTES"
    assert result.previous_state.metadata == {"sha256": "different_old_hash_123"}
    mock_blob.upload_from_string.assert_called_once()


# ============================================================================
# 4. RETRY & ERROR HANDLING TESTS
# ============================================================================


def test_upload_retry_on_transient_error():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False

    # Fail on first call with 503, succeed on second call
    mock_blob.upload_from_string.side_effect = [
        ServiceUnavailable("Backend unavailable"),
        None,
    ]
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage, max_retries=2, initial_delay=0.01)
    result = uploader.upload_slide_render(
        document_id="doc_slide_123",
        slide_number=1,
        image_bytes=VALID_PNG_BYTES,
    )

    assert result.action == "CREATED"
    assert mock_blob.upload_from_string.call_count == 2


def test_upload_retry_exhaustion_raises_error():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False

    mock_blob.upload_from_string.side_effect = InternalServerError("Persistent 500 error")
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage, max_retries=2, initial_delay=0.01)
    with pytest.raises(AssetUploadError) as exc_info:
        uploader.upload_slide_render(
            document_id="doc_slide_123",
            slide_number=1,
            image_bytes=VALID_PNG_BYTES,
        )
    assert "after 2 retries" in str(exc_info.value)
    assert mock_blob.upload_from_string.call_count == 3  # initial + 2 retries


def test_upload_access_denied_raises_error():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.side_effect = Forbidden("Access denied")
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    with pytest.raises(AssetAccessDeniedError) as exc_info:
        uploader.upload_slide_render(
            document_id="doc_slide_123",
            slide_number=1,
            image_bytes=VALID_PNG_BYTES,
        )
    assert "Access denied" in str(exc_info.value)


# ============================================================================
# 5. COORDINATION & ROLLBACK TESTS
# ============================================================================


def test_upload_presentation_assets_success():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    slide_renders = {1: VALID_PNG_BYTES, 2: VALID_PNG_BYTES}
    embedded_images = [
        {
            "slide_number": 1,
            "image_index": 1,
            "image_bytes": VALID_JPEG_BYTES,
            "description": "Chart 1",
        }
    ]

    assets = uploader.upload_presentation_assets(
        document_id="doc_pres_001",
        slide_renders=slide_renders,
        embedded_images=embedded_images,
    )

    assert len(assets) == 3
    assert assets[0].slide_number == 1 and assets[0].asset_type == "SLIDE"
    assert assets[1].slide_number == 2 and assets[1].asset_type == "SLIDE"
    assert assets[2].slide_number == 1 and assets[2].asset_type == "IMAGE"
    assert all(a.action == "CREATED" for a in assets)


def test_cleanup_uploaded_assets():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = True
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    asset = UploadedVisualAsset(
        visual_id="vis_123",
        document_id="doc_123",
        slide_number=1,
        gcs_uri=f"gs://{settings.gcs_mumbai_bucket}/test.png",
        bucket=settings.gcs_mumbai_bucket,
        object_name="test.png",
        sha256_hash="hash123",
        size_bytes=100,
        content_type="image/png",
        asset_type="SLIDE",
        action="CREATED",
    )

    deleted = uploader.cleanup_uploaded_assets([asset])
    assert len(deleted) == 1
    assert deleted[0] == asset.gcs_uri
    mock_blob.delete.assert_called_once()


def test_delete_asset_by_uri():
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = True
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    target_uri = f"gs://{settings.gcs_mumbai_bucket}/extracted-images/doc_1/slides/slide-1-render.png"
    deleted = uploader.delete_asset_by_uri(target_uri)

    assert deleted is True
    mock_blob.delete.assert_called_once()

    # Wrong bucket raises error
    with pytest.raises(UnapprovedBucketError):
        uploader.delete_asset_by_uri(f"gs://{settings.gcs_legacy_bucket}/other.png")


# ============================================================================
# 6. AUDIT SPECIFIC REGRESSION TESTS
# ============================================================================


def test_audit_temporary_content_url_never_stored_in_metadata():
    """Verify temporary Google Slides contentUrls are never saved to persistent metadata or DB references."""
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)

    # 1. Temporary URL passed directly: must be stripped from metadata
    temp_url = "https://lh3.googleusercontent.com/docs/securesc/30min_temp_token_xyz"
    assert is_temporary_content_url(temp_url) is True

    res = uploader.upload_embedded_image(
        document_id="doc_slide_sec",
        slide_number=1,
        image_index=1,
        image_bytes=VALID_PNG_BYTES,
        source_url=temp_url,
    )
    # Blob metadata must NOT contain the temporary URL
    assert mock_blob.metadata["source_url"] == ""

    # Database reference must be a permanent GCS URI, NEVER a contentUrl
    node = res.to_visual_asset_node()
    assert node.image_gcs_uri.startswith("gs://")
    assert "googleusercontent.com" not in node.image_gcs_uri

    # 2. Permanent source URL: must be preserved
    perm_url = "https://example.com/assets/permanent-architecture-diagram.png"
    assert is_temporary_content_url(perm_url) is False

    uploader.upload_embedded_image(
        document_id="doc_slide_sec",
        slide_number=1,
        image_index=2,
        image_bytes=VALID_PNG_BYTES,
        source_url=perm_url,
    )
    assert mock_blob.metadata["source_url"] == perm_url


def test_audit_compensation_preserves_and_restores_overwritten_object_on_failure():
    """Verify compensation restores an overwritten object back to original state if a later upload fails."""
    mock_storage = MagicMock()
    mock_bucket = MagicMock()

    # Blob 1: exists in GCS with original content
    blob1 = MagicMock()
    blob1.exists.return_value = True
    blob1.metadata = {"sha256": "orig_sha_123", "author": "original"}
    blob1.size = 50
    blob1.download_as_bytes.return_value = b"ORIGINAL_BYTES_DATA"
    blob1.content_type = "image/png"

    # Blob 2: upload will fail with 500 error
    blob2 = MagicMock()
    blob2.exists.return_value = False
    blob2.upload_from_string.side_effect = InternalServerError("Upload exploded on slide 2")

    def _blob_factory(name: str):
        if "slide-1" in name:
            return blob1
        return blob2

    mock_bucket.blob.side_effect = _blob_factory
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage, max_retries=1, initial_delay=0.01)
    slide_renders = {1: VALID_PNG_BYTES, 2: VALID_PNG_BYTES}

    with pytest.raises(AssetUploadError):
        uploader.upload_presentation_assets(
            document_id="doc_pres_comp",
            slide_renders=slide_renders,
            rollback_on_failure=True,
        )

    # 1. blob1 was overwritten, so it MUST NOT be deleted!
    blob1.delete.assert_not_called()

    # 2. blob1 MUST be restored with original content and metadata!
    # Expected call: upload_from_string(b"ORIGINAL_BYTES_DATA", content_type="image/png")
    restore_calls = [
        c for c in blob1.upload_from_string.call_args_list if c[0][0] == b"ORIGINAL_BYTES_DATA"
    ]
    assert len(restore_calls) == 1
    assert blob1.metadata["sha256"] == "orig_sha_123"
    assert blob1.metadata["author"] == "original"


def test_audit_rollback_deletes_only_newly_created_objects():
    """Verify rollback compensation deletes only newly created objects and leaves existing ones untouched."""
    mock_storage = MagicMock()
    mock_bucket = MagicMock()

    # Blob 1: newly created by batch (did not exist)
    blob1 = MagicMock()
    blob1.exists.return_value = False

    # Blob 2: already existed with identical content (skipped)
    blob2 = MagicMock()
    blob2.exists.return_value = True
    blob2.metadata = {"sha256": hashlib.sha256(VALID_PNG_BYTES).hexdigest()}
    blob2.size = len(VALID_PNG_BYTES)

    # Blob 3: fails upload
    blob3 = MagicMock()
    blob3.exists.return_value = False
    blob3.upload_from_string.side_effect = InternalServerError("Slide 3 failure")

    def _blob_factory(name: str):
        if "slide-1" in name:
            return blob1
        if "slide-2" in name:
            return blob2
        return blob3

    mock_bucket.blob.side_effect = _blob_factory
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage, max_retries=1, initial_delay=0.01)
    slide_renders = {1: VALID_PNG_BYTES, 2: VALID_PNG_BYTES, 3: VALID_PNG_BYTES}

    with pytest.raises(AssetUploadError):
        uploader.upload_presentation_assets(
            document_id="doc_pres_selective",
            slide_renders=slide_renders,
            rollback_on_failure=True,
        )

    # Blob 1: newly created -> MUST be deleted!
    blob1.delete.assert_called_once()

    # Blob 2: existed beforehand -> MUST NOT be deleted!
    blob2.delete.assert_not_called()

    # Blob 3: failed upload -> MUST NOT be deleted!
    blob3.delete.assert_not_called()


def test_audit_repeated_identical_uploads_remain_idempotent():
    """Verify that repeated identical uploads skip re-uploading and cause zero mutations."""
    mock_storage = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    # Simulate blob already existing with matching SHA-256
    matching_sha = hashlib.sha256(VALID_PNG_BYTES).hexdigest()
    mock_blob.exists.return_value = True
    mock_blob.metadata = {"sha256": matching_sha}
    mock_blob.size = len(VALID_PNG_BYTES)
    mock_bucket.blob.return_value = mock_blob
    mock_storage.bucket.return_value = mock_bucket

    uploader = VisualAssetUploader(storage_client=mock_storage)
    slide_renders = {1: VALID_PNG_BYTES, 2: VALID_PNG_BYTES}

    # Run upload
    assets = uploader.upload_presentation_assets(
        document_id="doc_pres_idemp",
        slide_renders=slide_renders,
    )

    # Both must be SKIPPED_EXISTING
    assert len(assets) == 2
    assert all(a.action == "SKIPPED_EXISTING" for a in assets)

    # upload_from_string MUST NOT have been called at all!
    mock_blob.upload_from_string.assert_not_called()
