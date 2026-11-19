#!/usr/bin/env python3
"""Phase 3C Manual Metadata, Taxonomy & Embeddings Enrichment CLI Runner.

Executes or dry-runs the complete Phase 3C enrichment pipeline for an ingested document:
  1. Discovery: Read existing EvidenceChunks from Spanner (excluding IMAGE chunks)
  2. Extraction: Gemini extraction of entities, technologies, solutions, and relationships
  3. Taxonomy: Deterministic taxonomy mapping of technologies against approved catalog
  4. Embeddings: Vertex AI 768-dimensional embeddings for eligible text chunks
  5. Checkpoint: Skip already-enriched documents without model API calls
  6. Staged GCS: Sidecar preparation, SHA-256 computation, staging and promotion
  7. Spanner: Atomic 11-table serializable persistence transaction

Usage:
    uv run python scripts/enrich_metadata_manual.py --document-id <document_id>
    uv run python scripts/enrich_metadata_manual.py --document-id doc_123 --dry-run
    uv run python scripts/enrich_metadata_manual.py --document-id doc_123 --force-recompute
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure default Google Cloud project is set to avoid external gcloud CLI queries
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "xenon-broker-513704-s9")

# Ensure workspace packages are importable regardless of invocation directory
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

while _ingestion_src in sys.path:
    sys.path.remove(_ingestion_src)
sys.path.insert(0, _ingestion_src)

while _common_src in sys.path:
    sys.path.remove(_common_src)
sys.path.insert(0, _common_src)

from app.enrichment_orchestrator import (
    EnrichmentOrchestrator,
    EnrichmentOrchestratorResult,
    EnrichmentStage,
    EnrichmentStatus,
    format_enrichment_result_report,
)
from common.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("enrich_metadata_manual")

# Suppress noisy telemetry warnings during local CLI runs
logging.getLogger("opentelemetry").setLevel(logging.ERROR)
logging.getLogger("google.cloud.monitoring").setLevel(logging.ERROR)


def main() -> int:
    """Entry point for the manual metadata enrichment CLI runner."""
    parser = argparse.ArgumentParser(
        description="Phase 3C Manual Metadata, Taxonomy & Embeddings Enrichment CLI",
    )
    parser.add_argument(
        "--document-id",
        required=True,
        help="Target DocumentId already ingested in Spanner (PDF or Google Slides)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run discovery, extraction, and embedding projection without committing to Spanner or GCS",
    )
    parser.add_argument(
        "--force-recompute",
        action="store_true",
        help="Bypass the four-point completion checkpoint and re-enrich the document",
    )
    parser.add_argument(
        "--expected-mutations",
        type=int,
        default=None,
        help="Enforce exact expected mutation count before persisting to Spanner or GCS",
    )
    parser.add_argument(
        "--enriched-at",
        type=str,
        default=None,
        help="Explicit immutable timezone-aware ISO-8601 timestamp for deterministic sidecar generation",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    doc_id = args.document_id.strip()

    logger.info("Initializing EnrichmentOrchestrator for document '%s'...", doc_id)
    orchestrator = EnrichmentOrchestrator()

    result = orchestrator.run(
        document_id=doc_id,
        dry_run=args.dry_run,
        force_recompute=args.force_recompute,
        expected_mutations=args.expected_mutations,
        enriched_at=args.enriched_at,
    )

    report = format_enrichment_result_report(result)
    print("\n" + report + "\n")

    if result.status == EnrichmentStatus.FAILED:
        logger.error("Enrichment failed at stage %s: %s", result.stage.value, result.error_message)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
