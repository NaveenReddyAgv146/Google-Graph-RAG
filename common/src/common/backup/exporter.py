"""Logical snapshot exporter for Cloud Spanner tables using a pinned read timestamp."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from typing import Any

from common.backup.restorer import generate_standalone_restore_script_content
from common.backup.types import (
    DEFAULT_GCS_BACKUP_BASE,
    EXISTING_TABLES,
    EXPECTED_BASELINE_ROW_COUNTS,
    BackupManifest,
    ColumnDefinition,
    TableBackupMetadata,
    TableSchema,
    compute_file_checksums,
    serialize_value,
)

logger = logging.getLogger("common.backup.exporter")


class BackupVerificationError(Exception):
    """Raised when backup row-count or checksum verification fails."""

    pass


class SpannerSnapshotExporter:
    """
    Exports Cloud Spanner tables to a consistent logical snapshot at a pinned read timestamp.
    Extracts table schemas, serializes records to JSONL, computes cryptographic checksums,
    verifies row counts, writes a manifest, and generates a standalone restore script.
    """

    def __init__(
        self,
        database: Any,
        project_id: str = "xenon-broker-513704-s9",
        instance_id: str = "sales-discovery-trial",
        database_id: str = "sales-knowledge-db",
        tables: list[str] | None = None,
        expected_row_counts: dict[str, int] | None = None,
        gcs_base_uri: str = DEFAULT_GCS_BACKUP_BASE,
    ):
        self.database = database
        self.project_id = project_id
        self.instance_id = instance_id
        self.database_id = database_id
        self.tables = tables or list(EXISTING_TABLES)
        self.expected_row_counts = (
            expected_row_counts
            if expected_row_counts is not None
            else dict(EXPECTED_BASELINE_ROW_COUNTS)
        )
        self.gcs_base_uri = gcs_base_uri.rstrip("/")

    def extract_table_schema(
        self, snapshot: Any, table_name: str
    ) -> TableSchema:
        """
        Extracts column definitions and primary key from information_schema.
        """
        cols_query = """
        SELECT column_name, spanner_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_name = @table_name
        ORDER BY ordinal_position
        """
        columns: list[ColumnDefinition] = []
        try:
            results = list(
                snapshot.execute_sql(
                    cols_query,
                    params={"table_name": table_name},
                    param_types={"table_name": "STRING"},
                )
            )
            for row in results:
                columns.append(
                    ColumnDefinition(
                        name=str(row[0]),
                        spanner_type=str(row[1]),
                        is_nullable=(str(row[2]).upper() == "YES"),
                        ordinal_position=int(row[3]),
                    )
                )
        except Exception as e:
            logger.warning(
                f"Could not read information_schema.columns for {table_name}: {e}. Falling back."
            )

        pk_query = """
        SELECT k.column_name
        FROM information_schema.key_column_usage k
        JOIN information_schema.table_constraints t
          ON k.constraint_name = t.constraint_name
        WHERE k.table_name = @table_name
          AND t.constraint_type = 'PRIMARY KEY'
        ORDER BY k.ordinal_position
        """
        pk_columns: list[str] = []
        try:
            pk_results = list(
                snapshot.execute_sql(
                    pk_query,
                    params={"table_name": table_name},
                    param_types={"table_name": "STRING"},
                )
            )
            pk_columns = [str(r[0]) for r in pk_results]
        except Exception as e:
            logger.warning(
                f"Could not read primary key info for {table_name}: {e}."
            )

        return TableSchema(
            table_name=table_name,
            columns=columns,
            primary_key=pk_columns,
        )

    def export_table_data(
        self,
        snapshot: Any,
        table_name: str,
        output_data_dir: str,
    ) -> tuple[str, int, list[str]]:
        """
        Queries all rows for a table within the pinned snapshot,
        serializes them to JSONL, and records the row count.
        Returns: (relative_file_name, row_count, column_names)
        """
        os.makedirs(output_data_dir, exist_ok=True)
        file_name = f"{table_name}.jsonl"
        rel_path = os.path.join("data", file_name)
        full_path = os.path.join(output_data_dir, file_name)

        query = f"SELECT * FROM {table_name}"
        results = snapshot.execute_sql(query)

        row_count = 0
        column_names: list[str] = []

        with open(full_path, "w", encoding="utf-8") as f:
            for row in results:
                if not column_names:
                    # In google-cloud-spanner, results.fields contains the column metadata
                    if hasattr(results, "fields") and results.fields:
                        column_names = [str(getattr(field, "name", field)) for field in results.fields]
                    elif hasattr(row, "_fields"):
                        column_names = [str(c) for c in row._fields]
                    elif isinstance(row, dict):
                        column_names = [str(c) for c in row.keys()]
                    else:
                        column_names = [f"col_{i}" for i in range(len(row))]

                if isinstance(row, dict):
                    record = {
                        str(k): serialize_value(v) for k, v in row.items()
                    }
                else:
                    record = {
                        str(col): serialize_value(val)
                        for col, val in zip(column_names, row)
                    }

                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                row_count += 1

        logger.info(f"Exported {row_count} rows for table '{table_name}' to {rel_path}.")
        return rel_path, row_count, column_names

    def verify_table_row_count(
        self, snapshot: Any, table_name: str, exported_count: int
    ) -> None:
        """
        Verifies that COUNT(1) from the snapshot strictly matches the exported row count.
        """
        count_query = f"SELECT COUNT(1) FROM {table_name}"
        count_res = list(snapshot.execute_sql(count_query))
        snapshot_count = int(count_res[0][0]) if count_res else 0

        if snapshot_count != exported_count:
            raise BackupVerificationError(
                f"Row count mismatch for table '{table_name}': "
                f"snapshot count is {snapshot_count}, but exported count is {exported_count}."
            )

        if table_name in self.expected_row_counts:
            expected = self.expected_row_counts[table_name]
            if exported_count != expected:
                logger.warning(
                    f"Table '{table_name}' exported {exported_count} rows, "
                    f"which differs from baseline expected {expected}."
                )

    def export_snapshot(
        self,
        output_dir: str,
        read_timestamp: datetime | None = None,
        backup_id: str | None = None,
    ) -> BackupManifest:
        """
        Executes a complete logical snapshot export of all target tables.
        Pins the Spanner read timestamp, extracts schema and data, validates row counts,
        computes cryptographic hashes, writes manifest.json and schema.json,
        and generates the standalone restore.py script.
        """
        os.makedirs(output_dir, exist_ok=True)
        data_dir = os.path.join(output_dir, "data")
        os.makedirs(data_dir, exist_ok=True)

        now_utc = datetime.now(timezone.utc)
        ts_slug = now_utc.strftime("%Y%m%dT%H%M%SZ")
        b_id = backup_id or f"spanner_backup_{ts_slug}"
        gcs_target_uri = f"{self.gcs_base_uri}/{ts_slug}/"

        snapshot_kwargs: dict[str, Any] = {"multi_use": True}
        if read_timestamp is not None:
            snapshot_kwargs["read_timestamp"] = read_timestamp

        # Open pinned snapshot across all tables
        with self.database.snapshot(**snapshot_kwargs) as snapshot:
            # Capture actual pinned read timestamp from snapshot
            pinned_ts = getattr(snapshot, "read_timestamp", None)
            if pinned_ts is None:
                pinned_ts = read_timestamp or now_utc
            pinned_ts_str = (
                pinned_ts.isoformat()
                if isinstance(pinned_ts, datetime)
                else str(pinned_ts)
            )

            logger.info(
                f"Opened Spanner snapshot pinned at read_timestamp={pinned_ts_str} "
                f"for {len(self.tables)} tables."
            )

            table_schemas: dict[str, Any] = {}
            tables_meta: dict[str, TableBackupMetadata] = {}
            total_rows = 0
            total_bytes = 0

            # 1. Export each table
            for table_name in self.tables:
                logger.info(f"Processing table: {table_name}...")
                schema = self.extract_table_schema(snapshot, table_name)
                table_schemas[table_name] = schema.to_dict()

                rel_path, row_count, discovered_cols = self.export_table_data(
                    snapshot, table_name, data_dir
                )

                # Update columns in schema if information_schema was empty
                if not schema.columns and discovered_cols:
                    schema.columns = [
                        ColumnDefinition(col, "UNKNOWN", True, idx + 1)
                        for idx, col in enumerate(discovered_cols)
                    ]
                    table_schemas[table_name] = schema.to_dict()

                # Verify row count inside the exact same snapshot
                self.verify_table_row_count(snapshot, table_name, row_count)

                # Compute checksums
                full_file_path = os.path.join(output_dir, rel_path)
                sha256, md5, byte_size = compute_file_checksums(full_file_path)

                tables_meta[table_name] = TableBackupMetadata(
                    table_name=table_name,
                    file_name=rel_path,
                    row_count=row_count,
                    sha256=sha256,
                    md5=md5,
                    byte_size=byte_size,
                    primary_key=schema.primary_key,
                )

                total_rows += row_count
                total_bytes += byte_size

        # 2. Write schema.json
        schema_file_path = os.path.join(output_dir, "schema.json")
        with open(schema_file_path, "w", encoding="utf-8") as f:
            json.dump(table_schemas, f, indent=2)
        schema_sha, schema_md5, schema_size = compute_file_checksums(schema_file_path)

        # 3. Generate and write standalone restore.py
        restore_file_path = os.path.join(output_dir, "restore.py")
        restore_content = generate_standalone_restore_script_content()
        with open(restore_file_path, "w", encoding="utf-8") as f:
            f.write(restore_content)
        # Make restore.py executable
        try:
            st = os.stat(restore_file_path)
            os.chmod(restore_file_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass
        restore_sha, restore_md5, restore_size = compute_file_checksums(restore_file_path)

        files_meta = {
            "schema.json": {
                "sha256": schema_sha,
                "md5": schema_md5,
                "byte_size": schema_size,
            },
            "restore.py": {
                "sha256": restore_sha,
                "md5": restore_md5,
                "byte_size": restore_size,
            },
        }

        # 4. Construct and write manifest.json
        manifest = BackupManifest(
            manifest_version="1.0",
            backup_id=b_id,
            project_id=self.project_id,
            instance_id=self.instance_id,
            database_id=self.database_id,
            gcs_target_uri=gcs_target_uri,
            read_timestamp=pinned_ts_str,
            created_at=now_utc.isoformat(),
            tables=tables_meta,
            files=files_meta,
            total_tables=len(self.tables),
            total_rows=total_rows,
            total_bytes=total_bytes,
            verification_status="VERIFIED",
            details={
                "baseline_expected_total_rows": sum(self.expected_row_counts.values()),
                "baseline_matches": (total_rows == sum(self.expected_row_counts.values())),
            },
        )

        manifest_file_path = os.path.join(output_dir, "manifest.json")
        with open(manifest_file_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)

        logger.info(
            f"Backup complete: {total_rows} total rows across {len(self.tables)} tables. "
            f"Manifest and restore script written to {output_dir}."
        )
        return manifest
