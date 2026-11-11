#!/usr/bin/env python3
"""Phase 3A Manual PDF Ingestion Test Runner.

Executes end-to-end PDF ingestion for a single PDF from Cloud Storage:
GCS discovery/download -> Document AI layout parsing -> chunk normalization -> Spanner persistence.

Usage:
    uv run python scripts/ingest_pdf_manual.py gs://sales-discovery-mvp-mumbai/path/to/whitepaper.pdf
    uv run python scripts/ingest_pdf_manual.py gs://sales-discovery-mvp/report.pdf --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

# Ensure workspace packages are importable regardless of invocation directory
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

# Ensure ingestion-copilot precedes sibling packages (e.g. discovery-copilot) in sys.path
while _ingestion_src in sys.path:
    sys.path.remove(_ingestion_src)
sys.path.insert(0, _ingestion_src)

while _common_src in sys.path:
    sys.path.remove(_common_src)
sys.path.insert(0, _common_src)

from app.orchestrator import (
    DocumentIngestionResult,
    IngestionStage,
    IngestionStatus,
    PdfIngestionOrchestrator,
)
from common.config import Settings
from common.database.spanner import get_database
from common.parsers.documentai_client import DocumentAiLayoutClient
from common.storage.pdf_reader import get_storage_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingest_pdf_manual")

# Suppress advisory OpenTelemetry/Cloud Monitoring missing instance_id warnings during local CLI runs
logging.getLogger("opentelemetry").setLevel(logging.ERROR)
logging.getLogger("google.cloud.monitoring").setLevel(logging.ERROR)


class DryRunSpannerTransaction:
    """Read-only transaction wrapper that executes queries against Spanner while capturing and discarding write mutations."""

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
        """Executes read queries using a read-only snapshot or direct mock call, ensuring zero locks."""
        if hasattr(self.real_database, "snapshot"):
            with self.real_database.snapshot() as snapshot:
                return list(snapshot.execute_sql(sql, params=params, param_types=param_types))
        elif hasattr(self.real_database, "execute_sql"):
            return list(self.real_database.execute_sql(sql, params=params, param_types=param_types))
        return []

    def insert(self, table: str, columns: list[str], values: list[list[Any]]) -> None:
        """Intercepts and records inserts in memory without sending them to Spanner."""
        logger.info(f"[DRY-RUN] Intercepted insert into '{table}' ({len(values)} rows) - write omitted.")
        self.inserts.append({"table": table, "columns": columns, "values": values})

    def update(self, table: str, columns: list[str], values: list[list[Any]]) -> None:
        """Intercepts and records updates in memory without sending them to Spanner."""
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


def format_result_report(result: DocumentIngestionResult, dry_run: bool = False) -> str:
    """Formats the structured DocumentIngestionResult into a readable terminal summary."""
    lines = [
        "=" * 64,
        "Phase 3A Manual PDF Ingestion Result",
        "=" * 64,
        f"GCS URI:            {result.gcs_uri}",
        f"Status:             {result.status.value}",
        f"Final Stage:        {result.stage.value}",
        f"Duration:           {result.duration_ms:.1f} ms",
        f"Dry Run:            {'Yes (Spanner mutations avoided)' if dry_run else 'No (Committed to Spanner)'}",
        "",
        "--- Document Metadata ---",
        f"Document ID:        {result.document_id or 'N/A'}",
        f"Total Chunks:       {result.chunks_count}",
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
            f"Edges Inserted:     {wr.edges_inserted}",
            f"Edges Unchanged:    {wr.edges_unchanged}",
            f"Total Mutations:    {wr.total_mutations}",
            f"Superseded Docs:    {wr.superseded_documents if wr.superseded_documents else 'None'}",
        ])

    if result.error_message or result.batch_required:
        lines.extend([
            "",
            "--- Diagnostics & Errors ---",
            f"Batch Required:     {result.batch_required}",
            f"Error Type:         {result.error_type or 'None'}",
            f"Error Message:      {result.error_message or 'None'}",
        ])

    lines.append("=" * 64)
    return "\n".join(lines)


def run_manual_ingestion(
    gcs_uri: str,
    dry_run: bool = False,
    orchestrator: PdfIngestionOrchestrator | None = None,
) -> tuple[DocumentIngestionResult, int]:
    """Runs manual ingestion for one PDF and returns the result and exit code."""
    uri = gcs_uri.strip()

    if not uri.startswith("gs://") or not uri.lower().endswith(".pdf"):
        error_msg = f"Invalid GCS URI '{uri}': must begin with 'gs://' and end with '.pdf'"
        logger.error(error_msg)
        print(f"Error: {error_msg}", file=sys.stderr)
        return (
            DocumentIngestionResult(
                gcs_uri=uri,
                status=IngestionStatus.FAILED,
                stage=IngestionStage.DISCOVERY,
                error_message=error_msg,
                error_type="ValueError",
            ),
            2,
        )

    if orchestrator is None:
        settings = Settings()
        storage_client = get_storage_client(project=settings.project_id)
        docai_client = DocumentAiLayoutClient(
            processor_name=settings.documentai_processor_name,
            endpoint=settings.documentai_endpoint,
        )
        spanner_db = get_database()

        if dry_run:
            logger.info("Dry-run requested: wrapping Spanner database in DryRunSpannerDatabase.")
            database_to_use = DryRunSpannerDatabase(spanner_db)
        else:
            database_to_use = spanner_db

        orchestrator = PdfIngestionOrchestrator(
            docai_client=docai_client,
            spanner_database=database_to_use,
            storage_client=storage_client,
            settings=settings,
        )

    logger.info(f"Dispatching ingestion for: {uri} (dry_run={dry_run})")
    result = orchestrator.ingest_pdf(uri)

    report = format_result_report(result, dry_run=dry_run)
    print(report)

    if result.status == IngestionStatus.SUCCESS:
        return result, 0
    elif result.status == IngestionStatus.BATCH_REQUIRED:
        logger.warning(f"Ingestion requires batch processing: {result.error_message}")
        return result, 1
    else:
        logger.error(f"Ingestion failed: {result.error_message}")
        return result, 2


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Phase 3A Manual PDF Ingestion Test Runner: Ingest a single PDF from GCS into Cloud Spanner."
    )
    parser.add_argument(
        "gcs_uri",
        help="GCS URI of the PDF to ingest (e.g. gs://sales-discovery-mvp-mumbai/whitepapers/sample.pdf)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute download, DocAI layout parsing, and normalization, but intercept and skip all Spanner writes.",
    )

    args = parser.parse_args(argv)
    _, exit_code = run_manual_ingestion(args.gcs_uri, dry_run=args.dry_run)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

