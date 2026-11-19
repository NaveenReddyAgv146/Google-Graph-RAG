#!/usr/bin/env python3
"""
CLI Tool for Phase 2C Spanner Logical Snapshot Backup.

Exports all 8 existing tables in sales-knowledge-db at a single pinned read timestamp:
- Documents
- DocumentAccess
- DriveSyncDocuments
- EvidenceChunks
- VisualAssets
- SolutionCards
- Embeddings
- EvidenceLinks

Target GCS Location:
gs://sales-discovery-mvp-mumbai/backups/spanner/<timestamp>/
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

# Ensure common is importable regardless of working directory
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
if _common_src not in sys.path:
    sys.path.insert(0, _common_src)

from common.backup import (
    DEFAULT_GCS_BACKUP_BASE,
    EXISTING_TABLES,
    EXPECTED_BASELINE_ROW_COUNTS,
    SpannerSnapshotExporter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("backup_spanner_cli")


def upload_directory_to_gcs(local_dir: str, gcs_target_uri: str) -> None:
    """Uploads all files in local_dir to GCS."""
    from google.cloud import storage

    if not gcs_target_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {gcs_target_uri}")

    parts = gcs_target_uri[5:].split("/", 1)
    bucket_name = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    logger.info(f"Uploading backup from {local_dir} to gs://{bucket_name}/{prefix}/...")
    for root, _, files in os.walk(local_dir):
        for file in files:
            local_path = os.path.join(root, file)
            rel_path = os.path.relpath(local_path, local_dir)
            blob_path = f"{prefix}/{rel_path}".lstrip("/")
            blob = bucket.blob(blob_path)
            blob.upload_from_filename(local_path)
            logger.info(f"Uploaded: gs://{bucket_name}/{blob_path}")

    logger.info(f"Successfully uploaded entire backup to {gcs_target_uri}")


def main():
    parser = argparse.ArgumentParser(description="Phase 2C Spanner Logical Backup Exporter")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", "xenon-broker-513704-s9"),
                        help="GCP Project ID")
    parser.add_argument("--instance", default=os.getenv("SPANNER_INSTANCE_ID", "sales-discovery-trial"),
                        help="Spanner Instance ID")
    parser.add_argument("--database", default=os.getenv("SPANNER_DATABASE_ID", "sales-knowledge-db"),
                        help="Spanner Database ID")
    parser.add_argument("--output-dir", default=None,
                        help="Local output directory. Defaults to ./backups/spanner/<timestamp>")
    parser.add_argument("--gcs-base-uri", default=DEFAULT_GCS_BACKUP_BASE,
                        help=f"Target GCS prefix. Default: {DEFAULT_GCS_BACKUP_BASE}")
    parser.add_argument("--read-timestamp", default=None,
                        help="ISO 8601 read timestamp for pinned snapshot. Defaults to current server time.")
    parser.add_argument("--upload-gcs", action="store_true",
                        help="Upload exported backup directly to GCS")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned operations without connecting or exporting")

    args = parser.parse_args()

    if args.dry_run:
        logger.info("[DRY-RUN] Spanner Logical Snapshot Exporter:")
        logger.info(f"  Project:        {args.project}")
        logger.info(f"  Instance:       {args.instance}")
        logger.info(f"  Database:       {args.database}")
        logger.info(f"  Tables (8):     {', '.join(EXISTING_TABLES)}")
        logger.info(f"  Expected Rows:  {sum(EXPECTED_BASELINE_ROW_COUNTS.values())} total")
        logger.info(f"  Target GCS:     {args.gcs_base_uri}/<timestamp>/")
        logger.info(f"  Upload to GCS:  {args.upload_gcs}")
        logger.info("[DRY-RUN] Execution completed successfully with zero mutations.")
        return

    ts_now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or os.path.join(".", "backups", "spanner", ts_now)
    os.makedirs(output_dir, exist_ok=True)

    from google.cloud import spanner
    client = spanner.Client(project=args.project)
    instance = client.instance(args.instance)
    database = instance.database(args.database)

    parsed_ts = None
    if args.read_timestamp:
        parsed_ts = datetime.fromisoformat(args.read_timestamp.replace("Z", "+00:00"))

    exporter = SpannerSnapshotExporter(
        database=database,
        project_id=args.project,
        instance_id=args.instance,
        database_id=args.database,
        gcs_base_uri=args.gcs_base_uri,
    )

    manifest = exporter.export_snapshot(
        output_dir=output_dir,
        read_timestamp=parsed_ts,
    )

    print("\n" + "=" * 60)
    print(f"BACKUP COMPLETE: {manifest.backup_id}")
    print(f"Local Path:     {output_dir}")
    print(f"Target GCS:     {manifest.gcs_target_uri}")
    print(f"Read Timestamp: {manifest.read_timestamp}")
    print(f"Total Rows:     {manifest.total_rows}")
    print(f"Status:         {manifest.verification_status}")
    print("=" * 60 + "\n")

    if args.upload_gcs:
        upload_directory_to_gcs(output_dir, manifest.gcs_target_uri)


if __name__ == "__main__":
    main()
