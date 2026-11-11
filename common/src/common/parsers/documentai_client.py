"""Document AI Layout Parser client for synchronous PDF processing."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Union

from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import (
    DeadlineExceeded,
    GoogleAPICallError,
    InvalidArgument,
    ResourceExhausted,
    ServiceUnavailable,
)
from google.cloud import documentai_v1 as documentai

from common.config import settings
from common.storage import DownloadedPdf, PdfMetadata

logger = logging.getLogger("common.parsers.documentai")

# Google Cloud Document AI limits for synchronous online processing
MAX_INLINE_PAYLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
DEFAULT_SYNC_PAGE_LIMIT = 15  # Layout Parser online synchronous limit


class DocumentAiError(Exception):
    """Base exception for Document AI parsing operations."""

    pass


class InvalidDocumentError(DocumentAiError):
    """Raised when the document content is empty or does not have a valid PDF header."""

    pass


class OversizedPayloadError(DocumentAiError):
    """Raised when the document payload exceeds the 20MB synchronous inline limit."""

    pass


class PageLimitExceededError(DocumentAiError):
    """Raised when document page count exceeds the 15-page synchronous limit.

    Indicates that batch asynchronous processing via GCS is required.
    """

    pass


class DocumentAiApiError(DocumentAiError):
    """Raised when the Document AI API call fails after retries."""

    pass


@dataclass(frozen=True)
class ParsedDocument:
    """Parsed document response bundled with source metadata and provenance."""

    raw_document: Any  # documentai.Document
    metadata: PdfMetadata | None
    processor_name: str
    page_count: int
    text: str


def validate_pdf_payload(
    content: bytes,
    max_payload_bytes: int = MAX_INLINE_PAYLOAD_BYTES,
) -> None:
    """Validates PDF byte content:

    - Must not be empty.
    - Must begin with PDF magic header (%PDF-).
    - Must not exceed maximum synchronous inline payload size (20MB).
    """
    if not content or len(content) == 0:
        raise InvalidDocumentError("PDF document content is empty.")

    if len(content) > max_payload_bytes:
        raise OversizedPayloadError(
            f"PDF content size ({len(content)} bytes) exceeds synchronous inline limit of "
            f"{max_payload_bytes} bytes (20MB). Asynchronous batch processing is required."
        )

    # Magic byte check: allow leading whitespace / BOM
    stripped_prefix = content[:1024].lstrip()
    if not stripped_prefix.startswith(b"%PDF-"):
        raise InvalidDocumentError(
            "Invalid PDF header: file does not start with standard '%PDF-' magic bytes."
        )


def estimate_pdf_page_count(content: bytes) -> int | None:
    """Heuristically estimates page count from PDF binary structure.

    Returns page count if deterministically found, or None if complex/compressed.
    """
    try:
        # Fast scan for /Type /Page (excluding /Pages)
        matches = re.findall(rb"/Type\s*/Page\b", content)
        if matches:
            return len(matches)
    except Exception:
        pass
    return None


class DocumentAiLayoutClient:
    """Client for processing PDFs via Google Cloud Document AI Layout Parser."""

    def __init__(
        self,
        processor_name: str | None = None,
        endpoint: str | None = None,
        client: documentai.DocumentProcessorServiceClient | None = None,
        sync_page_limit: int = DEFAULT_SYNC_PAGE_LIMIT,
        max_retries: int = 3,
        initial_backoff: float = 0.5,
    ):
        self.processor_name = processor_name or settings.documentai_processor_name
        self.endpoint = endpoint or settings.documentai_endpoint
        self.sync_page_limit = sync_page_limit
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff

        if client is not None:
            self._client = client
        else:
            client_options = ClientOptions(api_endpoint=self.endpoint)
            self._client = documentai.DocumentProcessorServiceClient(
                client_options=client_options
            )

    @property
    def client(self) -> documentai.DocumentProcessorServiceClient:
        return self._client

    def process(
        self,
        document_input: Union[DownloadedPdf, bytes],
        metadata: PdfMetadata | None = None,
    ) -> ParsedDocument:
        """Synchronously processes a PDF document using the Layout Parser processor.

        Accepts either a DownloadedPdf instance (from common.storage) or raw bytes.
        Enforces payload validation, bounded retries on transient errors, and synchronous page limits.
        """
        if isinstance(document_input, DownloadedPdf):
            content = document_input.content
            meta = document_input.metadata
        elif isinstance(document_input, (bytes, bytearray)):
            content = bytes(document_input)
            meta = metadata
        else:
            raise TypeError(
                f"Unsupported document input type: {type(document_input)}. Expected DownloadedPdf or bytes."
            )

        # 1. Payload validation
        validate_pdf_payload(content)

        # 2. Pre-flight heuristic page count check (if detectable)
        estimated_pages = estimate_pdf_page_count(content)
        if estimated_pages is not None and estimated_pages > self.sync_page_limit:
            raise PageLimitExceededError(
                f"Document contains approximately {estimated_pages} pages, which exceeds the synchronous "
                f"limit of {self.sync_page_limit} pages. Batch processing is required."
            )

        # 3. Construct Document AI request
        raw_doc = documentai.RawDocument(
            content=content,
            mime_type="application/pdf",
        )
        request = documentai.ProcessRequest(
            name=self.processor_name,
            raw_document=raw_doc,
        )

        # 4. Execute with bounded retries for transient failures
        attempt = 0
        backoff = self.initial_backoff
        last_error = None

        while attempt <= self.max_retries:
            try:
                logger.info(
                    f"Submitting document ({len(content)} bytes) to Document AI processor: {self.processor_name}"
                )
                response = self._client.process_document(request=request)
                doc = response.document

                # 5. Enforce synchronous page limit on returned document
                actual_pages = len(doc.pages)
                if actual_pages > self.sync_page_limit:
                    raise PageLimitExceededError(
                        f"Document contains {actual_pages} pages, which exceeds the synchronous "
                        f"limit of {self.sync_page_limit} pages. Batch processing is required."
                    )

                text = doc.text or ""
                return ParsedDocument(
                    raw_document=doc,
                    metadata=meta,
                    processor_name=self.processor_name,
                    page_count=actual_pages,
                    text=text,
                )

            except InvalidArgument as e:
                err_msg = str(e)
                # Check if Document AI rejected due to page limit
                if "page" in err_msg.lower() and (
                    "limit" in err_msg.lower() or "exceed" in err_msg.lower()
                ):
                    raise PageLimitExceededError(
                        f"Document AI rejected request due to page limits: {err_msg}. Batch processing is required."
                    ) from e
                raise DocumentAiApiError(
                    f"Document AI invalid argument error: {e}"
                ) from e

            except (ResourceExhausted, ServiceUnavailable, DeadlineExceeded) as e:
                last_error = e
                attempt += 1
                if attempt > self.max_retries:
                    logger.error(
                        f"Document AI transient failure after {attempt} attempts: {e}"
                    )
                    break
                logger.warning(
                    f"Transient Document AI error (attempt {attempt}/{self.max_retries}): {e}. Retrying in {backoff}s..."
                )
                time.sleep(backoff)
                backoff *= 2

            except GoogleAPICallError as e:
                logger.error(f"Non-retryable Document AI API error: {e}")
                raise DocumentAiApiError(f"Document AI API error: {e}") from e
            except PageLimitExceededError:
                raise
            except Exception as e:
                logger.error(f"Unexpected error during Document AI processing: {e}")
                raise DocumentAiApiError(
                    f"Unexpected Document AI processing error: {e}"
                ) from e

        raise DocumentAiApiError(
            f"Document AI processing failed after {self.max_retries} retries: {last_error}"
        ) from last_error
