"""Mocked unit and failure coordination tests for Google Slides Ingestion Orchestrator."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.slides_orchestrator import (
    DryRunSpannerDatabase,
    GoogleSlidesOrchestrator,
    SlidesIngestionResult,
    SlidesIngestionStage,
    SlidesIngestionStatus,
)
from common.config import Settings
from common.database.slides_writer import (
    ChunkOwnershipConflictError,
    SlidesPersistenceError,
    SlidesWriteResult,
)
from common.parsers.slides_client import (
    GoogleSlidesClient,
    SlidesAccessDeniedError,
    SlidesNotFoundError,
    SlidesRateLimitExceededError,
    ThumbnailDownloadError,
)
from common.parsers.slides_extractor import EmptyPresentationExtractionError
from common.storage.asset_uploader import (
    AssetUploadError,
    UploadedVisualAsset,
    VisualAssetUploader,
)

SAMPLE_PRES_ID = "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"

VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_sample_raw_presentation(
    presentation_id: str = SAMPLE_PRES_ID,
    title: str = "Enterprise Cloud Strategy",
) -> dict[str, Any]:
    return {
        "presentationId": presentation_id,
        "title": title,
        "slides": [
            {
                "objectId": "slide_p01",
                "pageElements": [
                    {
                        "objectId": "title_01",
                        "shape": {
                            "placeholder": {"type": "TITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Enterprise Cloud Architecture\n"}}
                                ]
                            },
                        },
                    },
                    {
                        "objectId": "body_01",
                        "shape": {
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Scalable graph analytics in Spanner.\n"}}
                                ]
                            }
                        },
                    },
                ],
            },
            {
                "objectId": "slide_p02",
                "pageElements": [
                    {
                        "objectId": "title_02",
                        "shape": {
                            "placeholder": {"type": "TITLE"},
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Benchmark Performance\n"}}
                                ]
                            },
                        },
                    },
                    {
                        "objectId": "table_02",
                        "table": {
                            "rows": 2,
                            "columns": 2,
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {"text": {"textElements": [{"textRun": {"content": "Metric"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "Value"}}]}},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {"text": {"textElements": [{"textRun": {"content": "Throughput"}}]}},
                                        {"text": {"textElements": [{"textRun": {"content": "100k QPS"}}]}},
                                    ]
                                },
                            ],
                        },
                    },
                ],
            },
        ],
    }


def make_mock_uploaded_assets(doc_id: str, count: int = 2, action: str = "CREATED") -> list[UploadedVisualAsset]:
    assets = []
    for i in range(1, count + 1):
        assets.append(
            UploadedVisualAsset(
                visual_id=f"vis_{doc_id[:45]}_s0{i}_v000",
                document_id=doc_id,
                slide_number=i,
                gcs_uri=f"gs://sales-discovery-mvp-mumbai/extracted-images/{doc_id}/slides/slide-{i}-render.png",
                bucket="sales-discovery-mvp-mumbai",
                object_name=f"extracted-images/{doc_id}/slides/slide-{i}-render.png",
                sha256_hash=f"hash_{i}_abc",
                size_bytes=len(VALID_PNG_BYTES),
                content_type="image/png",
                asset_type="SLIDE",
                description=f"Slide {i} render",
                action=action,
            )
        )
    return assets


class MockSpannerTransaction:
    """Mock Spanner read-write transaction."""

    def __init__(self, query_responses: dict[str, list[list[Any]]] | None = None):
        self.query_responses = query_responses or {}
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.executed_queries: list[str] = []

    def execute_sql(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[Any]]:
        self.executed_queries.append(sql)
        for key, resp in self.query_responses.items():
            if key in sql:
                return resp
        return []

    def insert(self, table: str, columns: list[str], values: list[list[Any]]):
        self.inserts.append({"table": table, "columns": columns, "values": values})

    def update(self, table: str, columns: list[str], values: list[list[Any]]):
        self.updates.append({"table": table, "columns": columns, "values": values})


class MockSpannerDatabase:
    """Mock Spanner database implementing run_in_transaction."""

    def __init__(self, txn: MockSpannerTransaction | None = None):
        self.txn = txn or MockSpannerTransaction()
        self.transaction_calls = 0

    def run_in_transaction(self, callback):
        self.transaction_calls += 1
        return callback(self.txn)


# ============================================================================
# 1. SUCCESS FLOW TESTS
# ============================================================================


def test_slides_ingestion_success():
    """Verify complete end-to-end success flow."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    raw_data = make_sample_raw_presentation()
    mock_client.get_presentation.return_value = raw_data
    mock_client.get_slide_thumbnail.return_value = VALID_PNG_BYTES

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_uploader.upload_presentation_assets.side_effect = (
        lambda document_id, slide_renders, **kwargs: make_mock_uploaded_assets(document_id, len(slide_renders))
    )

    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.SUCCESS
    assert result.stage == SlidesIngestionStage.COMPLETED
    assert result.slide_count == 2
    assert result.chunks_count >= 2
    assert result.visual_assets_count == 2
    assert result.dry_run is False
    assert result.error_message is None

    # Verify write result
    assert result.write_result is not None
    assert result.write_result.document_action == "INSERTED"
    assert result.write_result.total_mutations > 0

    # Verify client & uploader calls
    mock_client.get_presentation.assert_called_once_with(SAMPLE_PRES_ID)
    assert mock_client.get_slide_thumbnail.call_count == 2
    mock_uploader.upload_presentation_assets.assert_called_once()
    assert mock_db.transaction_calls == 1


# ============================================================================
# 2. DRY-RUN MODE TESTS
# ============================================================================


def test_slides_ingestion_dry_run():
    """Verify dry-run mode skips GCS uploads and intercepts Spanner writes."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.return_value = make_sample_raw_presentation()

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_txn = MockSpannerTransaction()
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID, dry_run=True)

    assert result.status == SlidesIngestionStatus.SUCCESS
    assert result.stage == SlidesIngestionStage.COMPLETED
    assert result.dry_run is True

    # Critical: GCS uploads must be skipped in dry-run
    mock_uploader.upload_presentation_assets.assert_not_called()
    mock_client.get_slide_thumbnail.assert_not_called()

    # Spanner dry run returned write results without real writes committed
    assert result.write_result is not None
    assert result.write_result.document_action == "INSERTED"
    assert len(mock_txn.inserts) == 0
    assert len(mock_txn.updates) == 0


# ============================================================================
# 3. IDEMPOTENCY TESTS
# ============================================================================


def test_slides_ingestion_idempotency_unchanged_rerun():
    """Verify unchanged rerun reports zero mutations and skips unchanged GCS assets."""
    raw_data = make_sample_raw_presentation()
    doc_id = f"doc_slide_{SAMPLE_PRES_ID}"

    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.return_value = raw_data
    mock_client.get_slide_thumbnail.return_value = VALID_PNG_BYTES

    # Uploader returns SKIPPED_EXISTING
    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_uploader.upload_presentation_assets.side_effect = (
        lambda document_id, slide_renders, **kwargs: make_mock_uploaded_assets(
            document_id, len(slide_renders), action="SKIPPED_EXISTING"
        )
    )

    # Pre-populate Spanner transaction with existing identical records
    norm_assets = make_mock_uploaded_assets(doc_id, 2)
    mock_txn = MockSpannerTransaction(
        query_responses={
            "FROM Documents": [
                [
                    doc_id,
                    "Enterprise Cloud Strategy.gslides",
                    "application/vnd.google-apps.presentation",
                    f"https://docs.google.com/presentation/d/{SAMPLE_PRES_ID}/edit",
                    None,
                    "ACTIVE",
                    SAMPLE_PRES_ID,
                    None,
                ]
            ],
            "FROM EvidenceChunks": [
                [f"{doc_id}_s01_t001", doc_id, "## Enterprise Cloud Architecture\n\nScalable graph analytics in Spanner.", "TEXT", 1],
                [f"{doc_id}_s02_t002", doc_id, "## Benchmark Performance", "TEXT", 2],
                [f"{doc_id}_s02_b003", doc_id, "| Metric | Value |\n| --- | --- |\n| Throughput | 100k QPS |", "TABLE", 2],
            ],
            "FROM VisualAssets": [
                [norm_assets[0].visual_id, doc_id, norm_assets[0].gcs_uri, norm_assets[0].description, "SLIDE", 1],
                [norm_assets[1].visual_id, doc_id, norm_assets[1].gcs_uri, norm_assets[1].description, "SLIDE", 2],
            ],
            "FROM DocumentContainsChunk": [
                [f"{doc_id}_s01_t001"],
                [f"{doc_id}_s02_t002"],
                [f"{doc_id}_s02_b003"],
            ],
            "FROM VisualAssetBelongsToDoc": [
                [norm_assets[0].visual_id],
                [norm_assets[1].visual_id],
            ],
        }
    )
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.SUCCESS
    assert result.write_result is not None
    assert result.write_result.document_action == "UNCHANGED"
    assert result.write_result.total_mutations == 0
    assert len(mock_txn.inserts) == 0
    assert len(mock_txn.updates) == 0


# ============================================================================
# 4. SLIDES API FAILURE TESTS
# ============================================================================


def test_slides_ingestion_api_not_found_404():
    """Verify 404 SlidesNotFoundError fails gracefully without calling GCS or Spanner."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.side_effect = SlidesNotFoundError(f"Presentation '{SAMPLE_PRES_ID}' not found.")

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_db = MockSpannerDatabase()

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.FAILED
    assert result.stage == SlidesIngestionStage.FETCHING
    assert "NotFoundError" in result.error_type
    assert "not found" in result.error_message
    mock_uploader.upload_presentation_assets.assert_not_called()
    assert mock_db.transaction_calls == 0


def test_slides_ingestion_api_access_denied_403():
    """Verify 403 SlidesAccessDeniedError fails gracefully without calling GCS or Spanner."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.side_effect = SlidesAccessDeniedError(f"Access denied to presentation '{SAMPLE_PRES_ID}'.")

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_db = MockSpannerDatabase()

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.FAILED
    assert result.stage == SlidesIngestionStage.FETCHING
    assert "AccessDeniedError" in result.error_type
    mock_uploader.upload_presentation_assets.assert_not_called()
    assert mock_db.transaction_calls == 0


def test_slides_ingestion_api_rate_limit_429():
    """Verify 429 SlidesRateLimitExceededError fails gracefully without calling GCS or Spanner."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.side_effect = SlidesRateLimitExceededError("Rate limit exceeded.")

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_db = MockSpannerDatabase()

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.FAILED
    assert result.stage == SlidesIngestionStage.FETCHING
    assert "SlidesRateLimitExceededError" in result.error_type
    mock_uploader.upload_presentation_assets.assert_not_called()
    assert mock_db.transaction_calls == 0


def test_slides_ingestion_empty_presentation_extraction():
    """Verify empty presentation (0 slides) fails at extraction without calling GCS or Spanner."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.return_value = {
        "presentationId": SAMPLE_PRES_ID,
        "title": "Empty Deck",
        "slides": [],
    }

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_db = MockSpannerDatabase()

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.FAILED
    assert result.stage == SlidesIngestionStage.EXTRACTING
    assert "EmptyPresentationExtractionError" in result.error_type
    mock_uploader.upload_presentation_assets.assert_not_called()
    assert mock_db.transaction_calls == 0


# ============================================================================
# 5. ASSET UPLOAD FAILURE TESTS
# ============================================================================


def test_slides_ingestion_upload_failure_aborts_before_spanner():
    """Verify that failure during asset upload aborts ingestion before Spanner is invoked."""
    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.return_value = make_sample_raw_presentation()
    mock_client.get_slide_thumbnail.return_value = VALID_PNG_BYTES

    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_uploader.upload_presentation_assets.side_effect = AssetUploadError("GCS network timeout during batch upload.")

    mock_db = MockSpannerDatabase()

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.FAILED
    assert result.stage == SlidesIngestionStage.UPLOADING
    assert "AssetUploadError" in result.error_type
    # Spanner persistence must NEVER have been called!
    assert mock_db.transaction_calls == 0


# ============================================================================
# 6. SPANNER FAILURE & GCS COMPENSATION TESTS (INVARIANT 4)
# ============================================================================


def test_slides_ingestion_spanner_failure_triggers_gcs_compensation():
    """Verify that if Spanner persistence fails after GCS uploads succeed, GCS compensation is triggered."""
    raw_data = make_sample_raw_presentation()
    expected_doc_id = f"doc_slide_{SAMPLE_PRES_ID}"

    mock_client = MagicMock(spec=GoogleSlidesClient)
    mock_client.get_presentation.return_value = raw_data
    mock_client.get_slide_thumbnail.return_value = VALID_PNG_BYTES

    uploaded_assets = make_mock_uploaded_assets(expected_doc_id, count=2, action="CREATED")
    mock_uploader = MagicMock(spec=VisualAssetUploader)
    mock_uploader.upload_presentation_assets.return_value = uploaded_assets
    mock_uploader.compensate_upload_batch.return_value = {
        "deleted": [a.gcs_uri for a in uploaded_assets],
        "restored": [],
    }

    # Simulate ownership conflict in Spanner
    mock_txn = MockSpannerTransaction(
        query_responses={
            "FROM Documents": [],
            "FROM EvidenceChunks": [
                [f"{expected_doc_id}_s01_t001", "doc_OTHER_OWNER", "Conflicting chunk text", "TEXT", 1]
            ],
        }
    )
    mock_db = MockSpannerDatabase(mock_txn)

    orchestrator = GoogleSlidesOrchestrator(
        slides_client=mock_client,
        asset_uploader=mock_uploader,
        spanner_database=mock_db,
    )

    result = orchestrator.ingest_presentation(SAMPLE_PRES_ID)

    assert result.status == SlidesIngestionStatus.FAILED
    assert result.stage == SlidesIngestionStage.PERSISTING
    assert "ChunkOwnershipConflictError" in result.error_type

    # CRITICAL INVARIANT: GCS compensation must have been invoked with uploaded_assets
    mock_uploader.compensate_upload_batch.assert_called_once_with(uploaded_assets)
    assert result.compensated_assets is not None
    assert len(result.compensated_assets["deleted"]) == 2
