"""Google Slides API Client with ADC authentication, bounded retries, and typed exceptions."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import requests
from google.auth import default as google_auth_default
from google.auth.exceptions import DefaultCredentialsError
from googleapiclient.discovery import build as build_google_service
from googleapiclient.errors import HttpError

logger = logging.getLogger("common.parsers.slides_client")

SLIDES_READONLY_SCOPE = "https://www.googleapis.com/auth/presentations.readonly"
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.5
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"


# ============================================================================
# Typed Exceptions
# ============================================================================


class SlidesClientError(Exception):
    """Base exception for Google Slides client errors."""

    pass


class InvalidPresentationIdError(SlidesClientError):
    """Raised when a presentation ID is missing or malformed."""

    pass


class PresentationNotFoundError(SlidesClientError):
    """Raised when the target presentation does not exist (HTTP 404)."""

    pass


class PresentationAccessDeniedError(SlidesClientError):
    """Raised when caller lacks permission to access the presentation (HTTP 403)."""

    pass


class SlidesRateLimitExceededError(SlidesClientError):
    """Raised when Google Slides API rate limits (HTTP 429) are exhausted."""

    pass


class SlidesApiError(SlidesClientError):
    """Raised for non-transient or 5xx Google Slides API errors."""

    pass


class ThumbnailDownloadError(SlidesClientError):
    """Raised when downloading slide thumbnail bytes fails."""

    pass


# Semantic aliases for exception naming consistency
SlidesNotFoundError = PresentationNotFoundError
SlidesAccessDeniedError = PresentationAccessDeniedError
SlidesRateLimitError = SlidesRateLimitExceededError


# ============================================================================
# Client Implementation
# ============================================================================


class GoogleSlidesClient:
    """Client for interacting with Google Slides API v1 via Application Default Credentials.

    Invariants:
    1. Read-only least privilege: Uses 'presentations.readonly' OAuth scope exclusively.
    2. Offline testability: Supports injecting mock service and HTTP downloader.
    3. Bounded exponential retries: Retries transient HTTP 429, 500, 502, 503, 504.
    4. Typed exceptions: Converts raw HttpError into explicit domain exceptions.
    5. Direct byte delivery: get_slide_thumbnail returns downloaded PNG bytes.
    """

    def __init__(
        self,
        service: Any | None = None,
        http_client: Callable[[str], bytes] | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        credentials: Any | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self._service = service
        self._http_client = http_client
        self._credentials = credentials

    @property
    def service(self) -> Any:
        """Lazily initializes the Google Slides API service resource if not injected."""
        if self._service is None:
            try:
                creds = self._credentials
                if creds is None:
                    creds, _ = google_auth_default(scopes=[SLIDES_READONLY_SCOPE])
                self._service = build_google_service(
                    "slides",
                    "v1",
                    credentials=creds,
                    cache_discovery=False,
                )
            except DefaultCredentialsError as exc:
                raise SlidesClientError(
                    f"Failed to resolve Application Default Credentials: {exc}"
                ) from exc
            except Exception as exc:
                raise SlidesClientError(
                    f"Failed to initialize Google Slides API service: {exc}"
                ) from exc
        return self._service

    def _execute_with_retry(self, request: Any, operation_name: str) -> dict[str, Any]:
        """Executes a Google API request with bounded exponential backoff for transient errors."""
        backoff = self.initial_backoff
        for attempt in range(1, self.max_retries + 1):
            try:
                return request.execute()
            except HttpError as exc:
                status_code = int(exc.resp.status if hasattr(exc, "resp") else 0)
                reason = getattr(exc, "reason", "") or str(exc)

                # Non-transient errors: fail immediately without retry
                if status_code == 404:
                    raise PresentationNotFoundError(
                        f"{operation_name} failed: Presentation not found (HTTP 404): {reason}"
                    ) from exc
                if status_code == 403:
                    raise PresentationAccessDeniedError(
                        f"{operation_name} failed: Access denied to presentation (HTTP 403). "
                        f"Verify the presentation is shared with the service account. Reason: {reason}"
                    ) from exc
                if status_code == 400:
                    raise InvalidPresentationIdError(
                        f"{operation_name} failed: Invalid request or ID (HTTP 400): {reason}"
                    ) from exc

                # Transient errors: 429 (rate limit) or 5xx server errors
                is_transient = status_code in (429, 500, 502, 503, 504)
                if not is_transient or attempt >= self.max_retries:
                    if status_code == 429:
                        raise SlidesRateLimitExceededError(
                            f"{operation_name} failed after {attempt} attempts: Rate limit exceeded (HTTP 429): {reason}"
                        ) from exc
                    raise SlidesApiError(
                        f"{operation_name} failed after {attempt} attempts (HTTP {status_code}): {reason}"
                    ) from exc

                logger.warning(
                    f"Transient HTTP {status_code} in {operation_name} (attempt {attempt}/{self.max_retries}). "
                    f"Retrying in {backoff:.2f}s..."
                )
                time.sleep(backoff)
                backoff *= 2.0
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise SlidesApiError(
                        f"{operation_name} failed after {attempt} attempts: {exc}"
                    ) from exc
                logger.warning(
                    f"Unexpected error in {operation_name} (attempt {attempt}/{self.max_retries}): {exc}. "
                    f"Retrying in {backoff:.2f}s..."
                )
                time.sleep(backoff)
                backoff *= 2.0

        raise SlidesApiError(f"{operation_name} failed: retry budget exhausted.")

    def get_presentation(self, presentation_id: str) -> dict[str, Any]:
        """Retrieves raw presentation metadata, structure, and slide elements.

        Args:
            presentation_id: The unique Google Drive file ID of the presentation.

        Returns:
            dict: The full Google Slides Presentation resource.
        """
        clean_id = (presentation_id or "").strip()
        if not clean_id:
            raise InvalidPresentationIdError("presentation_id must not be empty.")

        req = self.service.presentations().get(presentationId=clean_id)
        return self._execute_with_retry(req, f"get_presentation('{clean_id}')")

    def _default_download_bytes(self, url: str) -> bytes:
        """Default HTTP downloader for thumbnail images."""
        try:
            resp = requests.get(url, timeout=30.0)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            raise ThumbnailDownloadError(
                f"Failed to download slide thumbnail from URL: {exc}"
            ) from exc

    def get_slide_thumbnail(
        self,
        presentation_id: str,
        page_object_id: str,
        thumbnail_size: str = "LARGE",
    ) -> bytes:
        """Retrieves rendered slide thumbnail as raw PNG bytes.

        Args:
            presentation_id: Google presentation ID.
            page_object_id: Unique object ID of the slide page.
            thumbnail_size: Target size ('LARGE', 'MEDIUM', 'SMALL').

        Returns:
            bytes: The downloaded PNG image bytes.
        """
        clean_doc_id = (presentation_id or "").strip()
        clean_page_id = (page_object_id or "").strip()

        if not clean_doc_id:
            raise InvalidPresentationIdError("presentation_id must not be empty.")
        if not clean_page_id:
            raise InvalidPresentationIdError("page_object_id must not be empty.")

        req = self.service.presentations().pages().getThumbnail(
            presentationId=clean_doc_id,
            pageObjectId=clean_page_id,
            thumbnailProperties_thumbnailSize=thumbnail_size,
        )

        resp = self._execute_with_retry(
            req,
            f"get_slide_thumbnail('{clean_doc_id}', '{clean_page_id}')",
        )

        content_url = resp.get("contentUrl")
        if not content_url:
            raise ThumbnailDownloadError(
                f"Google Slides API response did not contain 'contentUrl' for slide '{clean_page_id}'."
            )

        downloader = self._http_client or self._default_download_bytes
        image_bytes = downloader(content_url)

        if not image_bytes or len(image_bytes) == 0:
            raise ThumbnailDownloadError(
                f"Downloaded thumbnail for slide '{clean_page_id}' is empty."
            )

        # Basic PNG magic byte validation
        if not image_bytes.startswith(PNG_MAGIC_BYTES):
            logger.warning(
                f"Thumbnail bytes for slide '{clean_page_id}' do not start with standard PNG header."
            )

        return image_bytes
