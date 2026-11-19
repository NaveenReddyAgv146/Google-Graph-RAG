"""End-to-End Google Slides Ingestion Orchestrator for Ingestion Copilot.

Coordinates the complete Google Slides ingestion pipeline:
Google Slides API fetch -> layout & content extraction -> normalization ->
visual asset GCS upload -> atomic Spanner persistence -> failure compensation.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from common.config import Settings, settings as global_settings
from common.database.slides_writer import (
    OwnershipConflictError,
    SchemaValidationError,
    SlidesPersistenceError,
    SlidesWriteResult,
    persist_normalized_presentation,
)
from common.parsers.slides_client import (
    GoogleSlidesClient,
    SlidesAccessDeniedError,
    SlidesApiError,
    SlidesClientError,
    SlidesNotFoundError,
    SlidesRateLimitExceededError,
    ThumbnailDownloadError,
)
from common.parsers.slides_extractor import (
    EmptyPresentationExtractionError,
    InvalidPresentationDataError,
    NormalizedPresentation,
    normalize_presentation,
)
from common.storage.asset_uploader import (
    AssetStorageError,
    AssetUploadError,
    UploadedVisualAsset,
    VisualAssetUploader,
)
from app.app_utils.orphan_tracker import OrphanAssetRecord, orphan_tracker

logger = logging.getLogger("ingestion_copilot.slides_orchestrator")

# Regex to extract presentation ID from Google Slides URLs or bare strings
SLIDES_URL_PATTERN = re.compile(
    r"(?:https?://docs\.google\.com/presentation/(?:u/\d+/)?d/)?([a-zA-Z0-9_-]{20,})",
    re.IGNORECASE,
)


def extract_presentation_id(input_str: str) -> str:
    """Extracts and validates a Google Slides presentation ID from a URL or raw ID string.

    Examples:
        - "https://docs.google.com/presentation/d/10KaQ12JImPARhyWvZP7dAZLMqW62bke8/edit" -> "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"
        - "10KaQ12JImPARhyWvZP7dAZLMqW62bke8" -> "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"
    """
    cleaned = (input_str or "").strip()
    if not cleaned:
        raise ValueError("Presentation ID or URL cannot be empty.")

    # Match URL pattern
    match = SLIDES_URL_PATTERN.search(cleaned)
    if match:
        return match.group(1)

    # Validate bare alphanumeric ID with reasonable length (min 20 chars)
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", cleaned) and not cleaned.startswith("http") and not cleaned.startswith("gs://"):
        return cleaned

    raise ValueError(
        f"Could not extract a valid Google Slides presentation ID from: '{input_str}'. "
        "Expected a full Google Slides URL or a presentation ID (min 20 chars)."
    )


class SlidesIngestionStage(str, Enum):
    """Stages of Google Slides ingestion pipeline execution."""

    DISCOVERY = "DISCOVERY"
    FETCHING = "FETCHING"
    EXTRACTING = "EXTRACTING"
    UPLOADING = "UPLOADING"
    PERSISTING = "PERSISTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SlidesIngestionStatus(str, Enum):
    """Overall outcome status for a presentation ingestion run."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class SlidesIngestionResult:
    """Structured ingestion result for a Google Slides presentation."""

    presentation_id: str
    status: SlidesIngestionStatus
    stage: SlidesIngestionStage
    document_id: str | None = None
    title: str | None = None
    slide_count: int = 0
    chunks_count: int = 0
    visual_assets_count: int = 0
    write_result: SlidesWriteResult | None = None
    uploaded_assets: list[UploadedVisualAsset] = field(default_factory=list)
    compensated_assets: dict[str, list[str]] | None = None
    orphaned_assets: list[str] = field(default_factory=list)
    compensation_error: str | None = None
    dry_run: bool = False
    error_message: str | None = None
    error_type: str | None = None
    duration_ms: float = 0.0


class DryRunSpannerTransaction:
    """Read-only transaction wrapper executing queries while intercepting and discarding write mutations."""

    def __init__(self, real_database: Any) -> None:
        self.real_database = real_database
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def execute_sql(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[Any]]:
        """Executes read queries against Spanner state."""
        if hasattr(self.real_database, "snapshot"):
            with self.real_database.snapshot() as snapshot:
                return list(snapshot.execute_sql(sql, params=params, param_types=param_types))
        elif hasattr(self.real_database, "execute_sql"):
            return list(self.real_database.execute_sql(sql, params=params, param_types=param_types))
        return []

    def insert(self, table: str, columns: list[str], values: list[list[Any]]) -> None:
        """Intercepts and records inserts without mutating Spanner."""
        logger.info(f"[DRY-RUN] Intercepted insert into '{table}' ({len(values)} rows) - write omitted.")
        self.inserts.append({"table": table, "columns": columns, "values": values})

    def update(self, table: str, columns: list[str], values: list[list[Any]]) -> None:
        """Intercepts and records updates without mutating Spanner."""
        logger.info(f"[DRY-RUN] Intercepted update into '{table}' ({len(values)} rows) - write omitted.")
        self.updates.append({"table": table, "columns": columns, "values": values})


class DryRunSpannerDatabase:
    """Spanner Database wrapper that intercepts run_in_transaction and guarantees zero database mutations."""

    def __init__(self, real_database: Any) -> None:
        self.real_database = real_database
        self.last_txn: DryRunSpannerTransaction | None = None

    def run_in_transaction(self, callback):
        txn = DryRunSpannerTransaction(self.real_database)
        self.last_txn = txn
        return callback(txn)


class GoogleSlidesOrchestrator:
    """Orchestrates end-to-end Google Slides discovery, extraction, asset storage, and persistence.

    Enforces Phase 3B Invariants:
    1. Dependency Injection: Google Slides client, Asset uploader, Spanner database are mockable.
    2. Safe Failure Coordination: If Spanner persistence fails after GCS uploads succeed,
       the orchestrator invokes uploader compensation to delete newly created blobs and restore overwritten blobs.
    3. Dry-Run Safety: In dry-run mode, zero GCS uploads occur and Spanner writes are intercepted.
    4. Deterministic Idempotency: Duplicate runs produce 0 Spanner mutations and skip unchanged GCS assets.
    5. Embeddings: embedding_768 remains strictly None.
    """

    def __init__(
        self,
        slides_client: GoogleSlidesClient | None = None,
        asset_uploader: VisualAssetUploader | None = None,
        spanner_database: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or global_settings
        self._slides_client = slides_client
        self._asset_uploader = asset_uploader
        self._spanner_database = spanner_database

    @property
    def slides_client(self) -> GoogleSlidesClient:
        if self._slides_client is None:
            self._slides_client = GoogleSlidesClient()
        return self._slides_client

    @property
    def asset_uploader(self) -> VisualAssetUploader:
        if self._asset_uploader is None:
            self._asset_uploader = VisualAssetUploader(bucket_name=self.settings.gcs_mumbai_bucket)
        return self._asset_uploader

    @property
    def spanner_database(self) -> Any:
        if self._spanner_database is None:
            from common.database.spanner import get_database

            self._spanner_database = get_database()
        return self._spanner_database

    def ingest_presentation(
        self,
        presentation_id_or_url: str,
        dry_run: bool = False,
        skip_renders: bool = False,
    ) -> SlidesIngestionResult:
        """Executes the full Google Slides ingestion pipeline for a single presentation."""
        start_time = time.perf_counter()

        # --------------------------------------------------------------------
        # Stage 1: Discovery & Argument Validation
        # --------------------------------------------------------------------
        try:
            presentation_id = extract_presentation_id(presentation_id_or_url)
        except ValueError as val_err:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Discovery error: {val_err}")
            return SlidesIngestionResult(
                presentation_id=presentation_id_or_url,
                status=SlidesIngestionStatus.FAILED,
                stage=SlidesIngestionStage.DISCOVERY,
                error_message=str(val_err),
                error_type=type(val_err).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )

        # --------------------------------------------------------------------
        # Stage 2: Fetching Presentation Content via Google Slides API
        # --------------------------------------------------------------------
        current_stage = SlidesIngestionStage.FETCHING
        try:
            logger.info(f"Fetching presentation '{presentation_id}' from Google Slides API...")
            raw_presentation = self.slides_client.get_presentation(presentation_id)
        except SlidesNotFoundError as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Presentation not found (404): {e}")
            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                error_message=str(e),
                error_type=type(e).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )
        except SlidesAccessDeniedError as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Access denied to presentation (403): {e}")
            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                error_message=str(e),
                error_type=type(e).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )
        except SlidesRateLimitExceededError as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Rate limit exceeded (429): {e}")
            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                error_message=str(e),
                error_type=type(e).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Slides API retrieval failed for '{presentation_id}': {e}")
            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                error_message=str(e),
                error_type=type(e).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )

        # --------------------------------------------------------------------
        # Stage 3: Content Extraction & Normalization
        # --------------------------------------------------------------------
        current_stage = SlidesIngestionStage.EXTRACTING
        try:
            logger.info(f"Extracting and normalizing content for presentation '{presentation_id}'...")
            normalized_presentation = normalize_presentation(
                raw_presentation,
                presentation_id=presentation_id,
            )
        except EmptyPresentationExtractionError as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Empty presentation extraction for '{presentation_id}': {e}")
            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                error_message=str(e),
                error_type=type(e).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Normalization failed for '{presentation_id}': {e}")
            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                error_message=str(e),
                error_type=type(e).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )

        doc_id = normalized_presentation.document_node.document_id
        slides_list = raw_presentation.get("slides", [])

        # --------------------------------------------------------------------
        # Stage 4: Visual Asset Storage & GCS Upload
        # --------------------------------------------------------------------
        current_stage = SlidesIngestionStage.UPLOADING
        uploaded_assets: list[UploadedVisualAsset] = []

        if dry_run:
            logger.info(f"[DRY-RUN] Skipping GCS visual asset uploads for doc '{doc_id}'.")
        else:
            slide_renders: dict[int, bytes] = {}
            if not skip_renders:
                logger.info(f"Downloading slide thumbnail renders for {len(slides_list)} slides...")
                try:
                    for idx, slide in enumerate(slides_list, start=1):
                        page_id = slide.get("objectId")
                        if page_id:
                            thumb_bytes = self.slides_client.get_slide_thumbnail(
                                presentation_id, page_id
                            )
                            slide_renders[idx] = thumb_bytes
                except Exception as thumb_err:
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    logger.error(f"Failed to fetch slide thumbnails for '{presentation_id}': {thumb_err}")
                    return SlidesIngestionResult(
                        presentation_id=presentation_id,
                        status=SlidesIngestionStatus.FAILED,
                        stage=current_stage,
                        document_id=doc_id,
                        title=normalized_presentation.document_node.file_name,
                        slide_count=normalized_presentation.slide_count,
                        error_message=str(thumb_err),
                        error_type=type(thumb_err).__name__,
                        dry_run=dry_run,
                        duration_ms=elapsed_ms,
                    )

            embedded_images_payload: list[dict[str, Any]] = []
            if getattr(normalized_presentation, "extracted_images", None):
                logger.info(f"Downloading {len(normalized_presentation.extracted_images)} embedded images...")
                for img_idx, img_meta in enumerate(normalized_presentation.extracted_images, start=1):
                    if img_meta.content_url:
                        try:
                            if hasattr(self.slides_client, "_default_download_bytes"):
                                img_bytes = self.slides_client._default_download_bytes(img_meta.content_url)
                            else:
                                import requests
                                resp = requests.get(img_meta.content_url, timeout=30.0)
                                resp.raise_for_status()
                                img_bytes = resp.content
                            embedded_images_payload.append({
                                "slide_number": img_meta.slide_number,
                                "image_index": img_idx,
                                "image_bytes": img_bytes,
                                "source_url": img_meta.source_url,
                                "object_id": img_meta.object_id,
                                "description": img_meta.description or img_meta.title,
                            })
                        except Exception as img_err:
                            logger.warning(f"Failed to download embedded image {img_meta.object_id}: {img_err}")

            try:
                logger.info(f"Uploading visual assets to GCS for doc '{doc_id}'...")
                uploaded_assets = self.asset_uploader.upload_presentation_assets(
                    document_id=doc_id,
                    slide_renders=slide_renders,
                    embedded_images=embedded_images_payload if embedded_images_payload else None,
                    rollback_on_failure=True,
                )
                # Synchronize normalized presentation with uploaded visual asset nodes
                if uploaded_assets:
                    normalized_presentation = NormalizedPresentation(
                        document_node=normalized_presentation.document_node,
                        chunks=normalized_presentation.chunks,
                        visual_assets=[a.to_visual_asset_node() for a in uploaded_assets],
                        slide_count=normalized_presentation.slide_count,
                        table_count=normalized_presentation.table_count,
                        image_count=normalized_presentation.image_count,
                        total_characters=normalized_presentation.total_characters,
                    )
            except Exception as upload_err:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                logger.error(
                    f"Asset upload failed for '{presentation_id}' (internal rollback executed): {upload_err}"
                )
                return SlidesIngestionResult(
                    presentation_id=presentation_id,
                    status=SlidesIngestionStatus.FAILED,
                    stage=current_stage,
                    document_id=doc_id,
                    title=normalized_presentation.document_node.file_name,
                    slide_count=normalized_presentation.slide_count,
                    chunks_count=len(normalized_presentation.chunks),
                    error_message=str(upload_err),
                    error_type=type(upload_err).__name__,
                    dry_run=dry_run,
                    duration_ms=elapsed_ms,
                )

        # --------------------------------------------------------------------
        # Stage 5: Atomic Spanner Persistence (with GCS Compensation on Failure)
        # --------------------------------------------------------------------
        current_stage = SlidesIngestionStage.PERSISTING
        database_to_use = (
            DryRunSpannerDatabase(self.spanner_database)
            if dry_run and not isinstance(self.spanner_database, DryRunSpannerDatabase)
            else self.spanner_database
        )

        try:
            logger.info(f"Persisting presentation '{doc_id}' across 5 Spanner tables (dry_run={dry_run})...")
            write_result = persist_normalized_presentation(
                database=database_to_use,
                normalized_presentation=normalized_presentation,
            )
        except Exception as db_exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Spanner persistence failed for '{doc_id}': {db_exc}")

            # Critical Invariant: Compensate GCS assets if Spanner transaction fails
            compensated_summary = None
            orphaned_assets: list[str] = []
            compensation_error: str | None = None
            if uploaded_assets and not dry_run:
                logger.info(
                    f"Executing GCS compensating rollback for {len(uploaded_assets)} assets of doc '{doc_id}'..."
                )
                try:
                    compensated_summary = self.asset_uploader.compensate_upload_batch(uploaded_assets)
                    logger.info(f"GCS compensation completed for doc '{doc_id}': {compensated_summary}")
                except Exception as comp_err:
                    compensation_error = str(comp_err)
                    orphaned_assets = [a.gcs_uri for a in uploaded_assets if a.action == "CREATED"]
                    logger.critical(
                        f"GCS compensation failed for doc '{doc_id}' (presentation '{presentation_id}'). "
                        f"Missing 'storage.objects.delete' permission under least-privilege 'roles/storage.objectCreator' policy. "
                        f"Tracking {len(orphaned_assets)} orphaned assets for reconciliation: {orphaned_assets}. "
                        f"Underlying error: {comp_err}"
                    )
                    orphan_tracker.record_orphans(
                        OrphanAssetRecord(
                            document_id=doc_id,
                            presentation_id=presentation_id,
                            gcs_uris=orphaned_assets,
                            reason="Spanner persistence failed; GCS compensation deletion denied under storage.objectCreator policy",
                            error_detail=str(comp_err),
                            timestamp=time.time(),
                        )
                    )

            return SlidesIngestionResult(
                presentation_id=presentation_id,
                status=SlidesIngestionStatus.FAILED,
                stage=current_stage,
                document_id=doc_id,
                title=normalized_presentation.document_node.file_name,
                slide_count=normalized_presentation.slide_count,
                chunks_count=len(normalized_presentation.chunks),
                visual_assets_count=len(normalized_presentation.visual_assets),
                uploaded_assets=uploaded_assets,
                compensated_assets=compensated_summary,
                orphaned_assets=orphaned_assets,
                compensation_error=compensation_error,
                error_message=str(db_exc),
                error_type=type(db_exc).__name__,
                dry_run=dry_run,
                duration_ms=elapsed_ms,
            )

        # --------------------------------------------------------------------
        # Stage 6: Completed
        # --------------------------------------------------------------------
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        logger.info(
            f"Successfully ingested presentation '{presentation_id}' -> DocumentId: '{doc_id}' "
            f"({len(normalized_presentation.chunks)} chunks, {len(normalized_presentation.visual_assets)} visuals, "
            f"mutations: {write_result.total_mutations}, action: {write_result.document_action}) in {elapsed_ms:.1f}ms"
        )

        return SlidesIngestionResult(
            presentation_id=presentation_id,
            status=SlidesIngestionStatus.SUCCESS,
            stage=SlidesIngestionStage.COMPLETED,
            document_id=doc_id,
            title=normalized_presentation.document_node.file_name,
            slide_count=normalized_presentation.slide_count,
            chunks_count=len(normalized_presentation.chunks),
            visual_assets_count=len(normalized_presentation.visual_assets),
            write_result=write_result,
            uploaded_assets=uploaded_assets,
            dry_run=dry_run,
            duration_ms=elapsed_ms,
        )
