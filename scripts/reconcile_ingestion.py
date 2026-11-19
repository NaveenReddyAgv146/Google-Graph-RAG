#!/usr/bin/env python3
"""Cloud Scheduler and Administrative Reconciliation Runner for Ingestion Copilot.

Recovers abandoned DISCOVERED and PUBLISH_FAILED documents idempotently.
Can be executed as:
1. Cloud Run Job triggered by Cloud Scheduler.
2. Local administrative maintenance CLI.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from common.config import settings
from common.database.spanner import get_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reconcile_ingestion")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile abandoned DISCOVERED and PUBLISH_FAILED documents in Spanner."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Maximum number of documents to inspect and reconcile in this batch (1-200).",
    )
    args = parser.parse_args()

    try:
        from ingestion_copilot.app.pubsub_worker import reconcile_unprocessed_documents
    except ImportError:
        # Fallback to sys.path manipulation if running directly from repo root
        import os
        sys.path.insert(0, os.path.abspath("ingestion-copilot"))
        from app.pubsub_worker import reconcile_unprocessed_documents

    database = get_database()
    logger.info("Starting ingestion reconciliation run (batch_size=%d)...", args.batch_size)

    result = reconcile_unprocessed_documents(database=database, batch_size=args.batch_size)
    print(json.dumps(result, indent=2))

    if result.get("status") == "SUCCESS":
        logger.info(
            "Reconciliation finished successfully. Inspected: %d, Repaired Completed: %d, Republished: %d, Failed: %d",
            result.get("candidates_inspected", 0),
            result.get("recovered_already_extracted", 0),
            result.get("republished_for_ingestion", 0),
            result.get("failed_republish", 0),
        )
        return 0
    else:
        logger.error("Reconciliation failed: %s", result.get("error"))
        return 1


if __name__ == "__main__":
    sys.exit(main())

