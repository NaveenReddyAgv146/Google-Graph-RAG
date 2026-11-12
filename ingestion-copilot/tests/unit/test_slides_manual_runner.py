"""Mocked unit tests for Phase 3B Manual Google Slides Ingestion Test Runner (scripts/ingest_slides_manual.py)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import scripts.ingest_slides_manual as manual_runner
from app.slides_orchestrator import (
    DryRunSpannerDatabase,
    GoogleSlidesOrchestrator,
    SlidesIngestionResult,
    SlidesIngestionStage,
    SlidesIngestionStatus,
    extract_presentation_id,
)
from common.database.slides_writer import SlidesWriteResult

SAMPLE_PRES_ID = "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"


def make_mock_slides_write_result(doc_id: str = "doc_slide_test") -> SlidesWriteResult:
    return SlidesWriteResult(
        document_id=doc_id,
        document_action="INSERTED",
        chunks_inserted=5,
        chunks_updated=0,
        chunks_unchanged=0,
        visual_assets_inserted=2,
        visual_assets_updated=0,
        visual_assets_unchanged=0,
        chunk_edges_inserted=5,
        chunk_edges_unchanged=0,
        visual_edges_inserted=2,
        visual_edges_unchanged=0,
        total_mutations=15,
        chunk_ids=[f"{doc_id}_c{i}" for i in range(5)],
        visual_ids=[f"vis_{doc_id}_v{i}" for i in range(2)],
    )


# ============================================================================
# 1. URL & Presentation ID Extraction Tests
# ============================================================================


def test_extract_presentation_id_valid_formats():
    # Full edit URL
    url1 = f"https://docs.google.com/presentation/d/{SAMPLE_PRES_ID}/edit"
    assert extract_presentation_id(url1) == SAMPLE_PRES_ID

    # Full edit URL with slide hash
    url2 = f"https://docs.google.com/presentation/d/{SAMPLE_PRES_ID}/edit#slide=id.p1"
    assert extract_presentation_id(url2) == SAMPLE_PRES_ID

    # User-indexed URL
    url3 = f"https://docs.google.com/presentation/u/0/d/{SAMPLE_PRES_ID}/edit"
    assert extract_presentation_id(url3) == SAMPLE_PRES_ID

    # Bare ID
    assert extract_presentation_id(SAMPLE_PRES_ID) == SAMPLE_PRES_ID


def test_extract_presentation_id_invalid_formats():
    with pytest.raises(ValueError):
        extract_presentation_id("")

    with pytest.raises(ValueError):
        extract_presentation_id("   ")

    with pytest.raises(ValueError):
        extract_presentation_id("gs://sales-discovery-mvp/presentation.pptx")

    with pytest.raises(ValueError):
        extract_presentation_id("https://example.com/not_slides")

    with pytest.raises(ValueError):
        extract_presentation_id("short_invalid_id")


# ============================================================================
# 2. Argument Validation & Execution Tests
# ============================================================================


def test_manual_runner_invalid_argument_exit_code_2(capsys):
    res, exit_code = manual_runner.run_manual_slides_ingestion("invalid_short_id")
    assert exit_code == 2
    assert res.status == SlidesIngestionStatus.FAILED
    assert res.stage == SlidesIngestionStage.DISCOVERY
    assert "Expected a full Google Slides URL or a presentation ID" in res.error_message


def test_manual_runner_success_flow(capsys):
    doc_id = f"doc_slide_{SAMPLE_PRES_ID}"
    mock_result = SlidesIngestionResult(
        presentation_id=SAMPLE_PRES_ID,
        status=SlidesIngestionStatus.SUCCESS,
        stage=SlidesIngestionStage.COMPLETED,
        document_id=doc_id,
        title="Enterprise Strategy.gslides",
        slide_count=3,
        chunks_count=5,
        visual_assets_count=2,
        write_result=make_mock_slides_write_result(doc_id),
        duration_ms=250.0,
    )

    mock_orch = MagicMock(spec=GoogleSlidesOrchestrator)
    mock_orch.ingest_presentation.return_value = mock_result

    res, exit_code = manual_runner.run_manual_slides_ingestion(
        SAMPLE_PRES_ID,
        dry_run=False,
        orchestrator=mock_orch,
    )

    assert exit_code == 0
    assert res.status == SlidesIngestionStatus.SUCCESS
    assert res.document_id == doc_id

    captured = capsys.readouterr().out
    assert "Phase 3B Manual Google Slides Ingestion Result" in captured
    assert f"Presentation ID:    {SAMPLE_PRES_ID}" in captured
    assert "Status:             SUCCESS" in captured
    assert "Final Stage:        COMPLETED" in captured
    assert f"Document ID:        {doc_id}" in captured
    assert "Total Chunks:       5" in captured
    assert "Visual Assets:      2" in captured
    assert "Document Action:    INSERTED" in captured
    assert "Total Mutations:    15" in captured
    assert "Dry Run:            No (Committed to GCS & Spanner)" in captured


def test_manual_runner_dry_run_flag(capsys):
    doc_id = f"doc_slide_{SAMPLE_PRES_ID}"
    mock_result = SlidesIngestionResult(
        presentation_id=SAMPLE_PRES_ID,
        status=SlidesIngestionStatus.SUCCESS,
        stage=SlidesIngestionStage.COMPLETED,
        document_id=doc_id,
        title="Enterprise Strategy.gslides",
        slide_count=3,
        chunks_count=5,
        visual_assets_count=2,
        write_result=make_mock_slides_write_result(doc_id),
        dry_run=True,
        duration_ms=150.0,
    )

    mock_orch = MagicMock(spec=GoogleSlidesOrchestrator)
    mock_orch.ingest_presentation.return_value = mock_result

    res, exit_code = manual_runner.run_manual_slides_ingestion(
        SAMPLE_PRES_ID,
        dry_run=True,
        orchestrator=mock_orch,
    )

    assert exit_code == 0
    mock_orch.ingest_presentation.assert_called_once_with(
        presentation_id_or_url=SAMPLE_PRES_ID,
        dry_run=True,
        skip_renders=False,
    )

    captured = capsys.readouterr().out
    assert "Dry Run:            Yes (GCS & Spanner writes omitted)" in captured


def test_manual_runner_failure_exit_code_2(capsys):
    mock_result = SlidesIngestionResult(
        presentation_id=SAMPLE_PRES_ID,
        status=SlidesIngestionStatus.FAILED,
        stage=SlidesIngestionStage.FETCHING,
        error_type="SlidesNotFoundError",
        error_message=f"Presentation '{SAMPLE_PRES_ID}' not found (404).",
        duration_ms=80.0,
    )

    mock_orch = MagicMock(spec=GoogleSlidesOrchestrator)
    mock_orch.ingest_presentation.return_value = mock_result

    res, exit_code = manual_runner.run_manual_slides_ingestion(
        SAMPLE_PRES_ID,
        dry_run=False,
        orchestrator=mock_orch,
    )

    assert exit_code == 2
    assert res.status == SlidesIngestionStatus.FAILED

    captured = capsys.readouterr().out
    assert "Status:             FAILED" in captured
    assert "Final Stage:        FETCHING" in captured
    assert "Error Type:         SlidesNotFoundError" in captured
    assert "not found (404)" in captured


# ============================================================================
# 3. CLI Dispatch & Default Construction Tests
# ============================================================================


def test_cli_main_dispatch():
    with patch("scripts.ingest_slides_manual.run_manual_slides_ingestion") as mock_run:
        mock_run.return_value = (MagicMock(), 0)

        exit_code = manual_runner.main([SAMPLE_PRES_ID, "--dry-run", "--skip-renders"])

        assert exit_code == 0
        mock_run.assert_called_once_with(
            SAMPLE_PRES_ID,
            dry_run=True,
            skip_renders=True,
        )


def test_manual_runner_default_client_initialization(capsys):
    """Verifies that running without an explicit orchestrator instantiates clients with valid default parameters."""
    with patch("scripts.ingest_slides_manual.GoogleSlidesClient") as mock_client_cls, \
         patch("scripts.ingest_slides_manual.VisualAssetUploader") as mock_uploader_cls, \
         patch("scripts.ingest_slides_manual.get_database") as mock_get_db, \
         patch("scripts.ingest_slides_manual.GoogleSlidesOrchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.ingest_presentation.return_value = SlidesIngestionResult(
            presentation_id=SAMPLE_PRES_ID,
            status=SlidesIngestionStatus.SUCCESS,
            stage=SlidesIngestionStage.COMPLETED,
            document_id="doc_slide_test",
            chunks_count=2,
            duration_ms=100.0,
        )
        mock_orch_cls.return_value = mock_orch

        res, exit_code = manual_runner.run_manual_slides_ingestion(
            SAMPLE_PRES_ID,
            dry_run=False,
            orchestrator=None,
        )

        assert exit_code == 0
        assert res.status == SlidesIngestionStatus.SUCCESS

        mock_client_cls.assert_called_once_with()
        mock_uploader_cls.assert_called_once()
        mock_get_db.assert_called_once_with()
        mock_orch_cls.assert_called_once()


def test_manual_runner_default_client_initialization_dry_run():
    """Verifies that dry-run wraps Spanner in DryRunSpannerDatabase."""
    with patch("scripts.ingest_slides_manual.GoogleSlidesClient"), \
         patch("scripts.ingest_slides_manual.VisualAssetUploader"), \
         patch("scripts.ingest_slides_manual.get_database") as mock_get_db, \
         patch("scripts.ingest_slides_manual.GoogleSlidesOrchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.ingest_presentation.return_value = SlidesIngestionResult(
            presentation_id=SAMPLE_PRES_ID,
            status=SlidesIngestionStatus.SUCCESS,
            stage=SlidesIngestionStage.COMPLETED,
            document_id="doc_slide_test",
            chunks_count=2,
            duration_ms=100.0,
        )
        mock_orch_cls.return_value = mock_orch

        res, exit_code = manual_runner.run_manual_slides_ingestion(
            SAMPLE_PRES_ID,
            dry_run=True,
            orchestrator=None,
        )

        assert exit_code == 0
        _, kwargs = mock_orch_cls.call_args
        assert isinstance(kwargs["spanner_database"], DryRunSpannerDatabase)
