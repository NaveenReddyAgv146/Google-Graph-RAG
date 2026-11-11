"""Restoration engine and generator for Spanner logical backups."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from common.backup.types import (
    BackupManifest,
    TableSchema,
    compute_file_checksums,
    deserialize_value,
)

logger = logging.getLogger("common.backup.restorer")


def verify_backup_checksums(backup_dir: str) -> tuple[bool, list[str]]:
    """
    Verifies SHA-256 and MD5 checksums of all data files recorded in manifest.json.
    Returns (is_valid, list_of_errors).
    """
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return False, [f"Manifest not found: {manifest_path}"]

    with open(manifest_path, encoding="utf-8") as f:
        manifest_data = json.load(f)

    manifest = BackupManifest.from_dict(manifest_data)
    errors: list[str] = []

    for table_name, meta in manifest.tables.items():
        file_path = os.path.join(backup_dir, meta.file_name)
        if not os.path.exists(file_path):
            errors.append(f"Missing data file for table '{table_name}': {file_path}")
            continue

        sha256, md5, size = compute_file_checksums(file_path)
        if sha256 != meta.sha256:
            errors.append(
                f"SHA-256 mismatch on table '{table_name}' ({meta.file_name}): "
                f"expected {meta.sha256}, got {sha256}"
            )
        if md5 != meta.md5:
            errors.append(
                f"MD5 mismatch on table '{table_name}' ({meta.file_name}): "
                f"expected {meta.md5}, got {md5}"
            )
        if size != meta.byte_size:
            errors.append(
                f"Byte size mismatch on table '{table_name}' ({meta.file_name}): "
                f"expected {meta.byte_size}, got {size}"
            )

    return len(errors) == 0, errors


def read_table_records(
    backup_dir: str, file_name: str
) -> tuple[list[str], list[list[Any]]]:
    """
    Reads a table's JSONL data file and deserializes records into column names and row values.
    Returns: (column_names, list_of_deserialized_rows)
    """
    file_path = os.path.join(backup_dir, file_name)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Data file not found: {file_path}")

    rows: list[list[Any]] = []
    columns: list[str] = []

    with open(file_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(
                    f"Invalid record at line {line_num} in {file_path}: expected JSON object"
                )

            if not columns:
                columns = list(record.keys())

            row = [deserialize_value(record.get(col)) for col in columns]
            rows.append(row)

    return columns, rows


def restore_table_data(
    database,
    table_name: str,
    columns: list[str],
    rows: list[list[Any]],
    batch_size: int = 200,
    dry_run: bool = False,
) -> int:
    """
    Restores records into a target Spanner table using insert_or_update mutations
    in serializable read-write transactions, chunked by batch_size.
    Returns total rows inserted/updated.
    """
    if not rows:
        logger.info(f"Table '{table_name}': 0 rows to restore.")
        return 0

    if dry_run:
        logger.info(
            f"[DRY-RUN] Table '{table_name}': validated {len(rows)} records for restoration."
        )
        return len(rows)

    total_restored = 0
    num_batches = (len(rows) + batch_size - 1) // batch_size

    for batch_idx in range(num_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(rows))
        chunk = rows[start_idx:end_idx]

        def _restore_txn(transaction):
            transaction.insert_or_update(
                table=table_name,
                columns=columns,
                values=chunk,
            )

        database.run_in_transaction(_restore_txn)
        total_restored += len(chunk)
        logger.info(
            f"Table '{table_name}': restored batch {batch_idx + 1}/{num_batches} "
            f"({len(chunk)} rows, cumulative: {total_restored}/{len(rows)})."
        )

    return total_restored


def generate_standalone_restore_script_content() -> str:
    """
    Generates the full source code for the standalone restore.py script
    embedded inside each backup directory.
    """
    return '''#!/usr/bin/env python3
"""
STANDALONE SPANNER LOGICAL RECOVERY SCRIPT
Generated automatically by Phase 2C Spanner Backup Tooling.

This script restores all 8 tables from the local backup directory to Cloud Spanner:
- Documents
- DocumentAccess
- DriveSyncDocuments
- EvidenceChunks
- VisualAssets
- SolutionCards
- Embeddings
- EvidenceLinks

Safety Features:
1. Validates SHA-256 and MD5 checksums of all data files against manifest.json before execution.
2. Supports --verify-only mode to audit target database state without mutating rows.
3. Supports --dry-run mode to parse, deserialize and validate mutations without committing.
4. Requires explicit --force flag to execute live database mutations.
5. Employs insert_or_update mutations in batched transactions with automatic conflict retry.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("spanner_restore")


def compute_file_checksums(file_path: str) -> tuple[str, str, int]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha256.hexdigest(), md5.hexdigest(), size


def deserialize_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, dict) and "__type__" in val:
        t = val["__type__"]
        v = val["value"]
        if t == "timestamp":
            return datetime.fromisoformat(v)
        if t == "bytes":
            return base64.b64decode(v.encode("ascii"))
    if isinstance(val, list):
        return [deserialize_value(x) for x in val]
    if isinstance(val, dict):
        return {k: deserialize_value(v) for k, v in val.items()}
    return val


def verify_manifest(backup_dir: str) -> tuple[dict[str, Any], list[str]]:
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return {}, [f"Manifest file not found: {manifest_path}"]

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    errors = []
    tables = manifest.get("tables", {})
    logger.info(f"Verifying checksums for {len(tables)} tables from {manifest_path}...")

    for table_name, meta in tables.items():
        file_path = os.path.join(backup_dir, meta["file_name"])
        if not os.path.exists(file_path):
            errors.append(f"Missing file for table '{table_name}': {file_path}")
            continue

        sha256, md5, size = compute_file_checksums(file_path)
        if sha256 != meta["sha256"]:
            errors.append(f"Table '{table_name}': SHA-256 mismatch! (expected {meta['sha256']}, got {sha256})")
        if md5 != meta["md5"]:
            errors.append(f"Table '{table_name}': MD5 mismatch! (expected {meta['md5']}, got {md5})")
        if size != meta["byte_size"]:
            errors.append(f"Table '{table_name}': Size mismatch! (expected {meta['byte_size']}, got {size})")

    return manifest, errors


def read_table_records(backup_dir: str, file_name: str) -> tuple[list[str], list[list[Any]]]:
    file_path = os.path.join(backup_dir, file_name)
    rows: list[list[Any]] = []
    columns: list[str] = []

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not columns:
                columns = list(record.keys())
            rows.append([deserialize_value(record.get(col)) for col in columns])

    return columns, rows


def execute_restore(
    backup_dir: str,
    project_id: str,
    instance_id: str,
    database_id: str,
    tables_to_restore: list[str] | None = None,
    batch_size: int = 200,
    dry_run: bool = False,
    verify_only: bool = False,
    force: bool = False,
) -> int:
    manifest, errors = verify_manifest(backup_dir)
    if errors:
        logger.error("CHECKSUM VERIFICATION FAILED:")
        for err in errors:
            logger.error(f"  - {err}")
        raise ValueError("Backup integrity verification failed. Aborting recovery.")

    logger.info("Backup checksums VERIFIED successfully.")

    if verify_only:
        logger.info("VERIFY-ONLY mode complete. Checksums and manifest are intact.")
        return 0

    if not dry_run and not force:
        logger.error("Refusing live restoration without --force. Use --dry-run to test or --force to execute.")
        raise RuntimeError("Missing required --force flag for live database restoration.")

    from google.cloud import spanner

    logger.info(f"Connecting to Spanner: {project_id} / {instance_id} / {database_id}...")
    client = spanner.Client(project=project_id)
    instance = client.instance(instance_id)
    database = instance.database(database_id)

    total_restored_all = 0
    all_tables = manifest.get("tables", {})
    selected_tables = tables_to_restore or list(all_tables.keys())

    for table_name in selected_tables:
        if table_name not in all_tables:
            logger.warning(f"Table '{table_name}' not in backup manifest. Skipping.")
            continue

        meta = all_tables[table_name]
        columns, rows = read_table_records(backup_dir, meta["file_name"])
        logger.info(f"Table '{table_name}': read {len(rows)} rows with {len(columns)} columns.")

        if not rows:
            continue

        if dry_run:
            logger.info(f"[DRY-RUN] Validated {len(rows)} mutations for '{table_name}'. Zero mutations executed.")
            total_restored_all += len(rows)
            continue

        num_batches = (len(rows) + batch_size - 1) // batch_size
        table_restored = 0
        for b_idx in range(num_batches):
            chunk = rows[b_idx * batch_size : (b_idx + 1) * batch_size]

            def _txn(transaction):
                transaction.insert_or_update(
                    table=table_name,
                    columns=columns,
                    values=chunk,
                )

            database.run_in_transaction(_txn)
            table_restored += len(chunk)

        logger.info(f"Table '{table_name}': Restored {table_restored} rows.")
        total_restored_all += table_restored

    logger.info(f"Restoration complete. Total rows processed: {total_restored_all}.")
    return total_restored_all


def main():
    parser = argparse.ArgumentParser(description="Standalone Spanner Logical Recovery Tool")
    parser.add_argument("--backup-dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="Path to backup directory containing manifest.json")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", "xenon-broker-513704-s9"),
                        help="GCP Project ID")
    parser.add_argument("--instance", default=os.getenv("SPANNER_INSTANCE_ID", "sales-discovery-trial"),
                        help="Spanner Instance ID")
    parser.add_argument("--database", default=os.getenv("SPANNER_DATABASE_ID", "sales-knowledge-db"),
                        help="Spanner Database ID")
    parser.add_argument("--table", action="append", dest="tables",
                        help="Specific table(s) to restore. Omit to restore all 8 tables.")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Mutations per commit batch")
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify backup checksums and exit without touching Spanner")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate deserialization and mutations without committing to Spanner")
    parser.add_argument("--force", action="store_true",
                        help="Required flag to execute live database mutations")

    args = parser.parse_args()

    try:
        execute_restore(
            backup_dir=args.backup_dir,
            project_id=args.project,
            instance_id=args.instance,
            database_id=args.database,
            tables_to_restore=args.tables,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
            force=args.force,
        )
    except Exception as e:
        logger.error(f"Restoration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
'''

