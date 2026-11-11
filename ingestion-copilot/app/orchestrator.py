"""End-to-End PDF Ingestion Orchestrator for Ingestion Copilot.

Coordinates the complete PDF ingestion pipeline:
GCS discovery -> PDF download -> Document AI layout parsing ->
chunk normalization -> transactional Spanner persistence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from google.cloud import storage

from common.config import Settings, settings
from common.database.ingestion_writer import (
    IngestionPersistenceError,
    IngestionWriteResult,
    persist_normalized_document,
)
from common.parsers.documentai_client import (
    DocumentAiApiError,
    DocumentAiLayoutClient,
    InvalidDocumentError,
    OversizedPayloadError,
    PageLimitExceededError,
)
from common.parsers.layout_extractor import (
    EmptyDocumentExtractionError,
    NormalizedDocument,
    generate_document_id,
    normalize_parsed_document,
)
from common.storage.pdf_reader import (
    DownloadedPdf,
    download_pdf_from_uri,
    list_pdf_blobs,
    parse_gcs_uri,
)

logger = logging.getLogger("ingestion_copilot.orchestrator")


class IngestionStage(str, Enum):
    """Stages of document ingestion pipeline execution."""

    DISCOVERY = "DISCOVERY"
    DOWNLOADING = "DOWNLOADING"
    PARSING = "PARSING"
    NORMALIZING = "NORMALIZING"
    PERSISTING = "PERSISTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IngestionStatus(str, Enum):
    """Overall outcome status for a document."""

    SUCCESS = "SUCCESS"
    BATCH_REQUIRED = "BATCH_REQUIRED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class DocumentIngestionResult:
    """Structured ingestion result for a single PDF document."""

    gcs_uri: str
    status: IngestionStatus
    stage: IngestionStage
    document_id: str | None = None
    chunks_count: int = 0
    write_result: IngestionWriteResult | None = None
    error_message: str | None = None
    error_type: str | None = None
    batch_required: bool = False
    duration_ms: float = 0.0


@dataclass(frozen=True)
class PipelineExecutionSummary:
    """Aggregated execution summary for a multi-document or bucket-level ingestion run."""

    total_discovered: int
    successful: int
    failed: int
    batch_required: int
    results: list[DocumentIngestionResult] = field(default_factory=list)
    total_duration_ms: float = 0.0


class PdfIngestionOrchestrator:
    """Orchestrates end-to-end PDF discovery, layout parsing, and Spanner persistence.

    Enforces Phase 3A Invariants:
    1. Offline mockability: All cloud dependencies (GCS, Document AI, Spanner) can be injected.
    2. Fault isolation: Failure of one document does not stop batch processing.
    3. Document AI limits: Large documents (> 15 pages) return BATCH_REQUIRED without crash.
    4. Immutable versioning: Changed GCS PDFs supersede prior PROCESSED versions via Spanner writer.
    5. Embeddings: embedding_768 remains strictly None (deferred to Phase 3D).
    6. Idempotency: Duplicate runs produce zero Spanner mutations.
    """

    def __init__(
        self,
        docai_client: DocumentAiLayoutClient | None = None,
        spanner_database: Any | None = None,
        storage_client: storage.Client | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._docai_client = docai_client
        self._spanner_database = spanner_database
        self._storage_client = storage_client

    @property
    def docai_client(self) -> DocumentAiLayoutClient:
        if self._docai_client is None:
            self._docai_client = DocumentAiLayoutClient(
                processor_name=self.settings.documentai_processor_name,
                endpoint=self.settings.documentai_endpoint,
            )
        return self._docai_client

    @property
    def spanner_database(self) -> Any:
        if self._spanner_database is None:
            from common.database.spanner import get_database

            self._spanner_database = get_database()
        return self._spanner_database

    @property
    def storage_client(self) -> storage.Client:
        if self._storage_client is None:
            self._storage_client = storage.Client(project=self.settings.project_id)
        return self._storage_client

    def ingest_downloaded_pdf(
        self,
        downloaded_pdf: DownloadedPdf,
    ) -> DocumentIngestionResult:
        """Processes an already downloaded PDF through Document AI, normalization, and Spanner persistence."""
        start_time = time.perf_counter()
        gcs_uri = (
            downloaded_pdf.metadata.gcs_uri
            or f"gs://{downloaded_pdf.metadata.bucket}/{downloaded_pdf.metadata.name}"
        )
        current_stage = IngestionStage.PARSING
        doc_id = generate_document_id(downloaded_pdf.metadata)

        # 1. Document AI Parsing
        try:
            parsed_doc = self.docai_client.process(downloaded_pdf)
        except PageLimitExceededError as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.warning(
                f"Document '{gcs_uri}' exceeds synchronous page limit; "
                f"batch processing required: {exc}"
            )
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.BATCH_REQUIRED,
                stage=IngestionStage.PARSING,
                document_id=doc_id,
                batch_required=True,
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )
        except (InvalidDocumentError, OversizedPayloadError, DocumentAiApiError, Exception) as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Failed to parse document '{gcs_uri}' at stage {current_stage}: {exc}")
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.PARSING,
                document_id=doc_id,
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )

        # 2. Layout Extraction and Chunk Normalization
        current_stage = IngestionStage.NORMALIZING
        try:
            normalized_doc: NormalizedDocument = normalize_parsed_document(parsed_doc)
        except EmptyDocumentExtractionError as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Empty extraction for document '{gcs_uri}' at stage {current_stage}: {exc}")
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.NORMALIZING,
                document_id=doc_id,
                chunks_count=0,
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Failed to normalize document '{gcs_uri}' at stage {current_stage}: {exc}")
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.NORMALIZING,
                document_id=doc_id,
                chunks_count=0,
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )

        if not normalized_doc.chunks:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            msg = (
                f"Document AI extraction yielded 0 chunks for '{gcs_uri}' "
                f"(pages={normalized_doc.page_count}, raw_text_len={len(parsed_doc.text)}). "
                f"Aborting persistence."
            )
            logger.error(msg)
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.NORMALIZING,
                document_id=doc_id,
                chunks_count=0,
                error_message=msg,
                error_type="EmptyDocumentExtractionError",
                duration_ms=elapsed_ms,
            )

        logger.info(
            f"Normalized '{gcs_uri}': {len(normalized_doc.chunks)} chunks, "
            f"{normalized_doc.page_count} pages, {normalized_doc.table_count} tables, "
            f"{normalized_doc.total_characters} characters."
        )

        # 3. Transactional Spanner Persistence
        current_stage = IngestionStage.PERSISTING
        try:
            write_result: IngestionWriteResult = persist_normalized_document(
                self.spanner_database,
                normalized_doc,
            )
        except IngestionPersistenceError as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Failed to persist document '{gcs_uri}' in Spanner: {exc}")
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.PERSISTING,
                document_id=doc_id,
                chunks_count=len(normalized_doc.chunks),
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Unexpected error persisting document '{gcs_uri}' in Spanner: {exc}")
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.PERSISTING,
                document_id=doc_id,
                chunks_count=len(normalized_doc.chunks),
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        logger.info(
            f"Successfully ingested '{gcs_uri}' -> DocumentId: '{doc_id}' "
            f"({len(normalized_doc.chunks)} chunks, action: {write_result.document_action}, "
            f"superseded: {len(write_result.superseded_documents)}) in {elapsed_ms:.1f}ms"
        )
        return DocumentIngestionResult(
            gcs_uri=gcs_uri,
            status=IngestionStatus.SUCCESS,
            stage=IngestionStage.COMPLETED,
            document_id=doc_id,
            chunks_count=len(normalized_doc.chunks),
            write_result=write_result,
            duration_ms=elapsed_ms,
        )

    def ingest_pdf(self, gcs_uri: str) -> DocumentIngestionResult:
        """Downloads and ingests a single PDF from a GCS URI."""
        start_time = time.perf_counter()
        current_stage = IngestionStage.DOWNLOADING

        try:
            downloaded = download_pdf_from_uri(
                gcs_uri,
                client=self.storage_client,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Failed to download PDF from '{gcs_uri}': {exc}")
            return DocumentIngestionResult(
                gcs_uri=gcs_uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.DOWNLOADING,
                error_message=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms,
            )

        return self.ingest_downloaded_pdf(downloaded)

    def ingest_pdf_batch(self, gcs_uris: list[str]) -> PipelineExecutionSummary:
        """Executes full pipeline across multiple PDF GCS URIs.

        Maintains isolation: failure of one PDF does not abort processing of remaining PDFs.
        """
        start_time = time.perf_counter()
        results: list[DocumentIngestionResult] = []
        successful = 0
        failed = 0
        batch_required = 0

        for uri in gcs_uris:
            try:
                res = self.ingest_pdf(uri)
            except Exception as exc:
                logger.exception(f"Unexpected fatal error processing '{uri}': {exc}")
                res = DocumentIngestionResult(
                    gcs_uri=uri,
                    status=IngestionStatus.FAILED,
                    stage=IngestionStage.FAILED,
                    error_message=str(exc),
                    error_type=type(exc).__name__,
                )

            results.append(res)
            if res.status == IngestionStatus.SUCCESS:
                successful += 1
            elif res.status == IngestionStatus.BATCH_REQUIRED:
                batch_required += 1
            else:
                failed += 1

        total_elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return PipelineExecutionSummary(
            total_discovered=len(gcs_uris),
            successful=successful,
            failed=failed,
            batch_required=batch_required,
            results=results,
            total_duration_ms=total_elapsed_ms,
        )

    def ingest_bucket_pdfs(
        self,
        bucket_name: str,
        prefix: str | None = None,
    ) -> PipelineExecutionSummary:
        """Discovers and ingests all PDFs from a GCS bucket prefix."""
        start_time = time.perf_counter()
        try:
            pdf_metas = list_pdf_blobs(
                bucket_name,
                prefix=prefix,
                client=self.storage_client,
            )
        except Exception as exc:
            total_elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Failed to list PDF blobs in gs://{bucket_name}/{prefix or ''}: {exc}")
            return PipelineExecutionSummary(
                total_discovered=0,
                successful=0,
                failed=1,
                batch_required=0,
                results=[
                    DocumentIngestionResult(
                        gcs_uri=f"gs://{bucket_name}/{prefix or ''}",
                        status=IngestionStatus.FAILED,
                        stage=IngestionStage.DISCOVERY,
                        error_message=str(exc),
                        error_type=type(exc).__name__,
                    )
                ],
                total_duration_ms=total_elapsed_ms,
            )

        if not pdf_metas:
            total_elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return PipelineExecutionSummary(
                total_discovered=0,
                successful=0,
                failed=0,
                batch_required=0,
                results=[],
                total_duration_ms=total_elapsed_ms,
            )

        uris = [m.gcs_uri for m in pdf_metas]
        return self.ingest_pdf_batch(uris)
