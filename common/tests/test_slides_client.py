"""Comprehensive unit tests for Google Slides API client with mock dependencies."""

from __future__ import annotations

from unittest.mock import MagicMock

import httplib2
import pytest
from google.auth.exceptions import DefaultCredentialsError
from googleapiclient.errors import HttpError

from common.parsers.slides_client import (
    PNG_MAGIC_BYTES,
    GoogleSlidesClient,
    InvalidPresentationIdError,
    PresentationAccessDeniedError,
    PresentationNotFoundError,
    SlidesApiError,
    SlidesClientError,
    SlidesRateLimitExceededError,
    ThumbnailDownloadError,
)


def make_http_error(status: int, reason: str = "Error") -> HttpError:
    """Helper to create a realistic googleapiclient HttpError."""
    resp = httplib2.Response({"status": str(status)})
    resp.status = status
    resp.reason = reason
    return HttpError(resp, content=f'{{"error": {{"message": "{reason}"}}}}'.encode("utf-8"))


# ============================================================================
# 1. Successful Retrieval Tests
# ============================================================================


def test_get_presentation_success():
    """Verifies successful retrieval of presentation structure."""
    mock_service = MagicMock()
    mock_presentations = MagicMock()
    mock_get_req = MagicMock()

    sample_presentation = {
        "presentationId": "test_presentation_123",
        "title": "Architecture Overview",
        "slides": [
            {"objectId": "slide_1", "pageElements": []},
            {"objectId": "slide_2", "pageElements": []},
        ],
    }
    mock_get_req.execute.return_value = sample_presentation
    mock_presentations.get.return_value = mock_get_req
    mock_service.presentations.return_value = mock_presentations

    client = GoogleSlidesClient(service=mock_service)
    result = client.get_presentation("test_presentation_123")

    assert result["presentationId"] == "test_presentation_123"
    assert result["title"] == "Architecture Overview"
    assert len(result["slides"]) == 2
    mock_presentations.get.assert_called_once_with(presentationId="test_presentation_123")
    mock_get_req.execute.assert_called_once()


def test_get_presentation_empty_id():
    """Verifies that an empty or whitespace ID raises InvalidPresentationIdError."""
    client = GoogleSlidesClient(service=MagicMock())

    with pytest.raises(InvalidPresentationIdError) as exc_info:
        client.get_presentation("")
    assert "must not be empty" in str(exc_info.value)

    with pytest.raises(InvalidPresentationIdError):
        client.get_presentation("   ")


# ============================================================================
# 2. Error Mapping & Non-Transient Error Tests
# ============================================================================


def test_get_presentation_not_found_404():
    """Verifies HTTP 404 raises PresentationNotFoundError without retrying."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(404, "Presentation not found")
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=3, initial_backoff=0.01)

    with pytest.raises(PresentationNotFoundError) as exc_info:
        client.get_presentation("nonexistent_id")

    assert "Presentation not found (HTTP 404)" in str(exc_info.value)
    # Critical: 404 must not retry
    assert mock_req.execute.call_count == 1


def test_get_presentation_access_denied_403():
    """Verifies HTTP 403 raises PresentationAccessDeniedError with remediation instruction."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(403, "Caller does not have permission")
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=3, initial_backoff=0.01)

    with pytest.raises(PresentationAccessDeniedError) as exc_info:
        client.get_presentation("private_id")

    assert "Access denied to presentation (HTTP 403)" in str(exc_info.value)
    assert "Verify the presentation is shared with the service account" in str(exc_info.value)
    # Critical: 403 must not retry
    assert mock_req.execute.call_count == 1


def test_get_presentation_invalid_request_400():
    """Verifies HTTP 400 raises InvalidPresentationIdError."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(400, "Bad Request")
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=3, initial_backoff=0.01)

    with pytest.raises(InvalidPresentationIdError) as exc_info:
        client.get_presentation("bad_id")
    assert "Invalid request or ID (HTTP 400)" in str(exc_info.value)
    assert mock_req.execute.call_count == 1


# ============================================================================
# 3. Rate Limit & Transient Retry Tests
# ============================================================================


def test_get_presentation_rate_limit_retry_and_succeed():
    """Verifies HTTP 429 rate limit retries with exponential backoff and succeeds."""
    mock_service = MagicMock()
    mock_req = MagicMock()

    sample_presentation = {"presentationId": "p_id", "title": "Deck"}
    # Fail twice with 429, then succeed on 3rd attempt
    mock_req.execute.side_effect = [
        make_http_error(429, "Rate limit exceeded"),
        make_http_error(429, "Rate limit exceeded"),
        sample_presentation,
    ]
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=3, initial_backoff=0.01)
    result = client.get_presentation("p_id")

    assert result["title"] == "Deck"
    assert mock_req.execute.call_count == 3


def test_get_presentation_rate_limit_exhausted():
    """Verifies consecutive HTTP 429s exhaust retries and raise SlidesRateLimitExceededError."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(429, "Quota exhausted")
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=3, initial_backoff=0.01)

    with pytest.raises(SlidesRateLimitExceededError) as exc_info:
        client.get_presentation("p_id")

    assert "Rate limit exceeded (HTTP 429)" in str(exc_info.value)
    assert "after 3 attempts" in str(exc_info.value)
    assert mock_req.execute.call_count == 3


def test_get_presentation_transient_503_retry():
    """Verifies transient 503 service unavailable retries and succeeds."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = [
        make_http_error(503, "Service Unavailable"),
        {"presentationId": "p_id", "title": "Recovered"},
    ]
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=3, initial_backoff=0.01)
    result = client.get_presentation("p_id")

    assert result["title"] == "Recovered"
    assert mock_req.execute.call_count == 2


def test_get_presentation_server_error_exhausted():
    """Verifies persistent 500 server error raises SlidesApiError."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(500, "Internal Server Error")
    mock_service.presentations().get.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, max_retries=2, initial_backoff=0.01)

    with pytest.raises(SlidesApiError) as exc_info:
        client.get_presentation("p_id")

    assert "HTTP 500" in str(exc_info.value)
    assert mock_req.execute.call_count == 2


# ============================================================================
# 4. Thumbnail Download Tests
# ============================================================================


def test_get_slide_thumbnail_success():
    """Verifies thumbnail URL resolution and byte downloading."""
    mock_service = MagicMock()
    mock_thumbnail_req = MagicMock()
    mock_thumbnail_req.execute.return_value = {
        "width": 1600,
        "height": 900,
        "contentUrl": "https://lh3.googleusercontent.com/thumbnail-sample",
    }
    mock_service.presentations().pages().getThumbnail.return_value = mock_thumbnail_req

    # Injected downloader returns valid PNG bytes
    fake_png = PNG_MAGIC_BYTES + b"IMAGE_DATA_12345"
    mock_downloader = MagicMock(return_value=fake_png)

    client = GoogleSlidesClient(
        service=mock_service,
        http_client=mock_downloader,
    )

    image_bytes = client.get_slide_thumbnail("pres_123", "slide_01", thumbnail_size="LARGE")

    assert image_bytes == fake_png
    assert image_bytes.startswith(PNG_MAGIC_BYTES)
    mock_service.presentations().pages().getThumbnail.assert_called_once_with(
        presentationId="pres_123",
        pageObjectId="slide_01",
        thumbnailProperties_thumbnailSize="LARGE",
    )
    mock_downloader.assert_called_once_with("https://lh3.googleusercontent.com/thumbnail-sample")


def test_get_slide_thumbnail_missing_content_url():
    """Verifies error when API returns response missing contentUrl."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {"width": 100}  # Missing contentUrl
    mock_service.presentations().pages().getThumbnail.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, http_client=MagicMock())

    with pytest.raises(ThumbnailDownloadError) as exc_info:
        client.get_slide_thumbnail("pres_123", "slide_01")
    assert "did not contain 'contentUrl'" in str(exc_info.value)


def test_get_slide_thumbnail_empty_bytes_downloaded():
    """Verifies error when downloader returns 0 bytes."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {"contentUrl": "https://sample.url"}
    mock_service.presentations().pages().getThumbnail.return_value = mock_req

    client = GoogleSlidesClient(service=mock_service, http_client=lambda url: b"")

    with pytest.raises(ThumbnailDownloadError) as exc_info:
        client.get_slide_thumbnail("pres_123", "slide_01")
    assert "is empty" in str(exc_info.value)


def test_get_slide_thumbnail_downloader_network_exception():
    """Verifies error when downloader raises a network exception."""
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {"contentUrl": "https://sample.url"}
    mock_service.presentations().pages().getThumbnail.return_value = mock_req

    def failing_downloader(url):
        raise ConnectionResetError("Connection dropped")

    client = GoogleSlidesClient(service=mock_service, http_client=failing_downloader)

    with pytest.raises(Exception):
        client.get_slide_thumbnail("pres_123", "slide_01")


def test_get_slide_thumbnail_invalid_arguments():
    """Verifies validation of presentation_id and page_object_id."""
    client = GoogleSlidesClient(service=MagicMock())

    with pytest.raises(InvalidPresentationIdError):
        client.get_slide_thumbnail("", "slide_01")

    with pytest.raises(InvalidPresentationIdError):
        client.get_slide_thumbnail("pres_123", "")
