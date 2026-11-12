#!/usr/bin/env python3
"""Phase 3B Manual Google Slides Ingestion Test Runner.

Executes end-to-end Google Slides ingestion for a single presentation:
Google Slides API fetch -> layout & content extraction -> normalization ->
visual asset GCS upload -> atomic Spanner persistence -> failure compensation.

Usage:
    uv run python scripts/ingest_slides_manual.py <presentation_id_or_url>
    uv run python scripts/ingest_slides_manual.py 10KaQ12JImPARhyWvZP7dAZLMqW62bke8 --dry-run
    uv run python scripts/ingest_slides_manual.py https://docs.google.com/presentation/d/10KaQ12JImPARhyWvZP7dAZLMqW62bke8/edit --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure workspace packages are importable regardless of invocation directory
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

# Ensure ingestion-copilot precedes sibling packages in sys.path
while _ingestion_src in sys.path:
    sys.path.remove(_ingestion_src)
sys.path.insert(0, _ingestion_src)

while _common_src in sys.path:
    sys.path.remove(_common_src)
sys.path.insert(0, _common_src)

from app.slides_orchestrator import (
    DryRunSpannerDatabase,
    GoogleSlidesOrchestrator,
    SlidesIngestionResult,
    SlidesIngestionStage,
    SlidesIngestionStatus,
    extract_presentation_id,
)
from common.config import Settings
from common.database.spanner import get_database
from common.parsers.slides_client import GoogleSlidesClient
from common.storage.asset_uploader import VisualAssetUploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingest_slides_manual")

# Suppress noisy telemetry warnings during local CLI runs
logging.getLogger("opentelemetry").setLevel(logging.ERROR)
logging.getLogger("google.cloud.monitoring").setLevel(logging.ERROR)


def format_slides_result_report(result: SlidesIngestionResult, dry_run: bool = False) -> str:
    """Formats structured SlidesIngestionResult into a readable terminal summary."""
    lines = [
        "=" * 64,
        "Phase 3B Manual Google Slides Ingestion Result",
        "=" * 64,
        f"Presentation ID:    {result.presentation_id}",
        f"Status:             {result.status.value}",
        f"Final Stage:        {result.stage.value}",
        f"Duration:           {result.duration_ms:.1f} ms",
        f"Dry Run:            {'Yes (GCS & Spanner writes omitted)' if dry_run or result.dry_run else 'No (Committed to GCS & Spanner)'}",
        "",
        "--- Presentation Metadata ---",
        f"Document ID:        {result.document_id or 'N/A'}",
        f"Title:              {result.title or 'N/A'}",
        f"Slide Count:        {result.slide_count}",
        f"Total Chunks:       {result.chunks_count}",
        f"Visual Assets:      {result.visual_assets_count}",
    ]

    if result.write_result:
        wr = result.write_result
        lines.extend([
            "",
            "--- Spanner Persistence Stats ---",
            f"Document Action:    {wr.document_action}",
            f"Chunks Inserted:    {wr.chunks_inserted}",
            f"Chunks Updated:     {wr.chunks_updated}",
            f"Chunks Unchanged:   {wr.chunks_unchanged}",
            f"Visuals Inserted:   {wr.visual_assets_inserted}",
            f"Visuals Updated:    {wr.visual_assets_updated}",
            f"Visuals Unchanged:  {wr.visual_assets_unchanged}",
            f"Chunk Edges Added:  {wr.chunk_edges_inserted}",
            f"Visual Edges Added: {wr.visual_edges_inserted}",
            f"Total Mutations:    {wr.total_mutations}",
        ])

    if result.uploaded_assets:
        created = sum(1 for a in result.uploaded_assets if a.action == "CREATED")
        skipped = sum(1 for a in result.uploaded_assets if a.action == "SKIPPED_EXISTING")
        overwritten = sum(1 for a in result.uploaded_assets if a.action == "OVERWRITTEN")
        lines.extend([
            "",
            "--- Cloud Storage Assets ---",
            f"Total Tracked:      {len(result.uploaded_assets)}",
            f"Newly Created:      {created}",
            f"Skipped Existing:   {skipped}",
            f"Overwritten:        {overwritten}",
        ])

    if result.compensated_assets:
        lines.extend([
            "",
            "--- GCS Failure Compensation ---",
            f"Blobs Deleted:      {result.compensated_assets.get('deleted', [])}",
            f"Blobs Restored:     {result.compensated_assets.get('restored', [])}",
        ])

    if result.error_message:
        lines.extend([
            "",
            "--- Diagnostics & Errors ---",
            f"Error Type:         {result.error_type or 'None'}",
            f"Error Message:      {result.error_message or 'None'}",
        ])

    lines.append("=" * 64)
    return "\n".join(lines)


def run_manual_slides_ingestion(
    presentation_id_or_url: str,
    dry_run: bool = False,
    skip_renders: bool = False,
    orchestrator: GoogleSlidesOrchestrator | None = None,
) -> tuple[SlidesIngestionResult, int]:
    """Runs manual ingestion for one Google Slides presentation and returns result and exit code."""
    raw_input = (presentation_id_or_url or "").strip()

    try:
        pres_id = extract_presentation_id(raw_input)
    except ValueError as err:
        error_msg = str(err)
        logger.error(error_msg)
        print(f"Error: {error_msg}", file=sys.stderr)
        res = SlidesIngestionResult(
            presentation_id=raw_input,
            status=SlidesIngestionStatus.FAILED,
            stage=SlidesIngestionStage.DISCOVERY,
            error_message=error_msg,
            error_type="ValueError",
            dry_run=dry_run,
        )
        return res, 2

    if orchestrator is None:
        settings = Settings()
        slides_client = GoogleSlidesClient()
        asset_uploader = VisualAssetUploader(bucket_name=settings.gcs_mumbai_bucket)
        spanner_db = get_database()

        if dry_run:
            logger.info("Dry-run requested: wrapping Spanner in DryRunSpannerDatabase.")
            database_to_use = DryRunSpannerDatabase(spanner_db)
        else:
            database_to_use = spanner_db

        orchestrator = GoogleSlidesOrchestrator(
            slides_client=slides_client,
            asset_uploader=asset_uploader,
            spanner_database=database_to_use,
            settings=settings,
        )

    logger.info(f"Dispatching Slides ingestion for: {pres_id} (dry_run={dry_run}, skip_renders={skip_renders})")
    result = orchestrator.ingest_presentation(
        presentation_id_or_url=pres_id,
        dry_run=dry_run,
        skip_renders=skip_renders,
    )

    report = format_slides_result_report(result, dry_run=dry_run)
    print(report)

    if result.status == SlidesIngestionStatus.SUCCESS:
        return result, 0
    else:
        logger.error(f"Slides ingestion failed: {result.error_message}")
        return result, 2


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Phase 3B Manual Google Slides Ingestion Test Runner: Ingest a Google Slides presentation into Cloud Spanner & GCS."
    )
    parser.add_argument(
        "presentation_id_or_url",
        help="Google Slides Presentation ID or URL (e.g. 10KaQ12JImPARhyWvZP7dAZLMqW62bke8 or https://docs.google.com/presentation/d/...)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute presentation fetch, content extraction, and normalization, but omit GCS uploads and intercept Spanner writes.",
    )
    parser.add_argument(
        "--skip-renders",
        action="store_true",
        help="Skip downloading and uploading slide thumbnail renders.",
    )

    args = parser.parse_args(argv)
    _, exit_code = run_manual_slides_ingestion(
        args.presentation_id_or_url,
        dry_run=args.dry_run,
        skip_renders=args.skip_renders,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
