"""Unit and integration tests for Phase 2C Spanner logical backup and recovery tooling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from common.backup import (
    EXISTING_TABLES,
    BackupManifest,
    BackupVerificationError,
    SpannerSnapshotExporter,
    compute_file_checksums,
    deserialize_value,
    read_table_records,
    restore_table_data,
    serialize_value,
    verify_backup_checksums,
)


class MockField:
    """Mock Spanner Field metadata."""

    def __init__(self, name: str):
        self.name = name


class MockResultSet:
    """Mock Spanner stream/results object."""

    def __init__(self, rows: list[Any], fields: list[str] | None = None):
        self.rows = rows
        if fields:
            self.fields = [MockField(f) for f in fields]
        else:
            self.fields = []

    def __iter__(self):
        return iter(self.rows)


class MockSnapshot:
    """Mock Spanner read-only snapshot pinned at a specific timestamp."""

    def __init__(self, data: dict[str, list[dict[str, Any]]], pinned_ts: datetime):
        self.data = data
        self.read_timestamp = pinned_ts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, query: str, params: dict | None = None, param_types: dict | None = None):
        query_strip = query.strip()

        # Handle information_schema.columns
        if "FROM information_schema.columns" in query_strip:
            tbl = params.get("table_name") if params else None
            if tbl and tbl in self.data and self.data[tbl]:
                sample = self.data[tbl][0]
                rows = []
                for idx, (col_name, val) in enumerate(sample.items(), start=1):
                    col_type = "STRING(256)"
                    if isinstance(val, int):
                        col_type = "INT64"
                    elif isinstance(val, float):
                        col_type = "FLOAT64"
                    elif isinstance(val, list):
                        col_type = "ARRAY<FLOAT32>(vector_length => 768)"
                    elif isinstance(val, datetime):
                        col_type = "TIMESTAMP"
                    elif isinstance(val, bytes):
                        col_type = "BYTES(MAX)"
                    rows.append([col_name, col_type, "YES", idx])
                return MockResultSet(rows)
            return MockResultSet([])

        # Handle information_schema.key_column_usage
        if "FROM information_schema.key_column_usage" in query_strip:
            tbl = params.get("table_name") if params else None
            if tbl and tbl in self.data and self.data[tbl]:
                first_col = list(self.data[tbl][0].keys())[0]
                return MockResultSet([[first_col]])
            return MockResultSet([])

        # Handle SELECT COUNT(1) FROM {table}
        if query_strip.startswith("SELECT COUNT(1) FROM"):
            tbl = query_strip.split()[-1]
            count = len(self.data.get(tbl, []))
            return MockResultSet([[count]])

        # Handle SELECT * FROM {table}
        if query_strip.startswith("SELECT * FROM"):
            tbl = query_strip.split()[-1]
            rows = self.data.get(tbl, [])
            if rows:
                fields = list(rows[0].keys())
                row_tuples = [list(r.values()) for r in rows]
                return MockResultSet(row_tuples, fields=fields)
            return MockResultSet([], fields=[])

        return MockResultSet([])


class MockDatabase:
    """Mock Spanner Database handle."""

    def __init__(self, mock_data: dict[str, list[dict[str, Any]]], pinned_ts: datetime):
        self.mock_data = mock_data
        self.pinned_ts = pinned_ts

    def snapshot(self, **kwargs):
        return MockSnapshot(self.mock_data, self.pinned_ts)

    def run_in_transaction(self, callable_fn):
        mock_txn = MagicMock()
        return callable_fn(mock_txn)


# ============================================================================
# TEST CASES
# ============================================================================

def test_serialization_roundtrip():
    """Tests serialization and deserialization across all Spanner data types."""
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=timezone.utc)
    raw_bytes = b"hello_spanner_binary"
    vector_768 = [0.1234, -0.5678, 0.9999]
    bbox_json = {"x": 10, "y": 20, "w": 100, "h": 200}

    test_payload = {
        "str_col": "text_value",
        "int_col": 42,
        "float_col": 3.14159,
        "bool_col": True,
        "none_col": None,
        "ts_col": now,
        "bytes_col": raw_bytes,
        "vector_col": vector_768,
        "json_col": bbox_json,
    }

    # Serialize
    serialized = {k: serialize_value(v) for k, v in test_payload.items()}
    json_str = json.dumps(serialized)
    assert isinstance(json_str, str)

    # Deserialize
    parsed = json.loads(json_str)
    deserialized = {k: deserialize_value(v) for k, v in parsed.items()}

    assert deserialized["str_col"] == "text_value"
    assert deserialized["int_col"] == 42
    assert deserialized["float_col"] == pytest.approx(3.14159)
    assert deserialized["bool_col"] is True
    assert deserialized["none_col"] is None
    assert deserialized["ts_col"] == now
    assert deserialized["bytes_col"] == raw_bytes
    assert deserialized["vector_col"] == vector_768
    assert deserialized["json_col"] == bbox_json


@pytest.fixture
def mock_spanner_dataset():
    """Provides realistic mock data for all 8 existing tables."""
    pinned_ts = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
    data = {
        "Documents": [
            {
                "DocumentId": "doc_001",
                "FileName": "architecture.pdf",
                "MimeType": "application/pdf",
                "SourceUrl": "https://example.com/arch.pdf",
                "GcsUri": "gs://sales-discovery-mvp/arch.pdf",
                "Status": "ACTIVE",
                "DriveFileId": "drive_001",
                "CreatedAt": pinned_ts,
            },
            {
                "DocumentId": "doc_002",
                "FileName": "pitch_deck.pptx",
                "MimeType": "application/vnd.google-apps.presentation",
                "SourceUrl": "https://example.com/deck",
                "GcsUri": "gs://sales-discovery-mvp/deck.pptx",
                "Status": "ACTIVE",
                "DriveFileId": "drive_002",
                "CreatedAt": pinned_ts,
            },
        ],
        "DocumentAccess": [
            {
                "DocumentId": "doc_001",
                "PrincipalId": "user:alice@company.com",
                "AccessLevel": "READ",
                "GrantedAt": pinned_ts,
            },
            {
                "DocumentId": "doc_002",
                "PrincipalId": "group:sales@company.com",
                "AccessLevel": "READ",
                "GrantedAt": pinned_ts,
            },
        ],
        "DriveSyncDocuments": [
            {
                "DriveFileId": "drive_001",
                "DocumentId": "doc_001",
                "LastSyncedAt": pinned_ts,
                "DriveVersion": "v1.0",
            },
            {
                "DriveFileId": "drive_002",
                "DocumentId": "doc_002",
                "LastSyncedAt": pinned_ts,
                "DriveVersion": "v1.2",
            },
        ],
        "EvidenceChunks": [
            {
                "ChunkId": "chk_001",
                "DocumentId": "doc_001",
                "Modality": "TEXT",
                "TextContent": "Cloud Spanner offers 99.999% SLA.",
                "PageNumber": 1,
                "SlideNumber": None,
                "Confidence": 0.98,
                "Vector": [0.1, 0.2, 0.3],
                "CreatedAt": pinned_ts,
            },
            {
                "ChunkId": "chk_002",
                "DocumentId": "doc_001",
                "Modality": "TABLE",
                "TextContent": "| Region | Latency |\n| asia-south1 | 2ms |",
                "PageNumber": 2,
                "SlideNumber": None,
                "Confidence": 0.95,
                "Vector": [0.4, 0.5, 0.6],
                "CreatedAt": pinned_ts,
            },
            {
                "ChunkId": "chk_003",
                "DocumentId": "doc_002",
                "Modality": "SLIDE",
                "TextContent": "GraphRAG reduces hallucinations by 68%.",
                "PageNumber": None,
                "SlideNumber": 4,
                "Confidence": 0.99,
                "Vector": [0.7, 0.8, 0.9],
                "CreatedAt": pinned_ts,
            },
        ],
        "VisualAssets": [
            {
                "VisualId": "vis_001",
                "DocumentId": "doc_001",
                "PageNumber": 1,
                "SlideNumber": None,
                "ImageGcsUri": "gs://sales-discovery-mvp/vis_001.png",
                "Description": "Architecture topology diagram",
            },
            {
                "VisualId": "vis_002",
                "DocumentId": "doc_002",
                "PageNumber": None,
                "SlideNumber": 4,
                "ImageGcsUri": "gs://sales-discovery-mvp/vis_002.png",
                "Description": "Graph traversal workflow",
            },
        ],
        "SolutionCards": [
            {
                "CardId": "card_001",
                "DocumentId": "doc_001",
                "ProjectName": "Retail Inventory Graph",
                "BusinessProblem": "Slow inventory sync",
                "ProposedSolution": "Spanner GraphRAG",
                "ImplementedSolution": "Spanner Property Graph with Vertex AI",
                "Outcome": "Latency reduced from 4s to 120ms",
            },
            {
                "CardId": "card_002",
                "DocumentId": "doc_002",
                "ProjectName": "Fintech Risk Analytics",
                "BusinessProblem": "Fraud pattern detection",
                "ProposedSolution": "ISO GQL Traversal",
                "ImplementedSolution": "Sub-graph pattern matching",
                "Outcome": "Zero false negatives",
            },
        ],
        "Embeddings": [
            {
                "EmbeddingId": "emb_001",
                "ChunkId": "chk_001",
                "ModelName": "text-embedding-005",
                "Dimensions": 768,
            },
            {
                "EmbeddingId": "emb_002",
                "ChunkId": "chk_002",
                "ModelName": "text-embedding-005",
                "Dimensions": 768,
            },
        ],
        "EvidenceLinks": [],
    }
    return MockDatabase(data, pinned_ts)


def test_spanner_snapshot_exporter_mock(tmp_path, mock_spanner_dataset):
    """
    Tests logical snapshot exporter using mock Spanner database.
    Verifies all 8 tables, schema extraction, pinned timestamp, checksums, and manifest.
    """
    output_dir = str(tmp_path / "backup_export")
    exporter = SpannerSnapshotExporter(
        database=mock_spanner_dataset,
        project_id="xenon-broker-513704-s9",
        instance_id="sales-discovery-trial",
        database_id="sales-knowledge-db",
        expected_row_counts={
            "Documents": 2,
            "DocumentAccess": 2,
            "DriveSyncDocuments": 2,
            "EvidenceChunks": 3,
            "VisualAssets": 2,
            "SolutionCards": 2,
            "Embeddings": 2,
            "EvidenceLinks": 0,
        },
    )

    manifest = exporter.export_snapshot(
        output_dir=output_dir,
        read_timestamp=mock_spanner_dataset.pinned_ts,
        backup_id="test_backup_001",
    )

    # 1. Verify Manifest Metadata
    assert isinstance(manifest, BackupManifest)
    assert manifest.backup_id == "test_backup_001"
    assert manifest.project_id == "xenon-broker-513704-s9"
    assert manifest.instance_id == "sales-discovery-trial"
    assert manifest.database_id == "sales-knowledge-db"
    assert manifest.total_tables == 8
    # Total rows = 2 + 2 + 2 + 3 + 2 + 2 + 2 + 0 = 15
    assert manifest.total_rows == 15
    assert manifest.verification_status == "VERIFIED"
    assert manifest.gcs_target_uri.startswith("gs://sales-discovery-mvp-mumbai/backups/spanner/")

    # 2. Verify Export Files on Disk
    assert os.path.exists(os.path.join(output_dir, "manifest.json"))
    assert os.path.exists(os.path.join(output_dir, "schema.json"))
    assert os.path.exists(os.path.join(output_dir, "restore.py"))

    # Verify all 8 table data files exist
    for table_name in EXISTING_TABLES:
        assert table_name in manifest.tables
        file_path = os.path.join(output_dir, manifest.tables[table_name].file_name)
        assert os.path.exists(file_path)

    # 3. Verify Row-Level Content Deserialization
    cols, rows = read_table_records(output_dir, "data/EvidenceChunks.jsonl")
    assert len(rows) == 3
    assert "TextContent" in cols
    assert rows[0][cols.index("TextContent")] == "Cloud Spanner offers 99.999% SLA."

    # 4. Verify Checksum Integrity
    valid, errors = verify_backup_checksums(output_dir)
    assert valid is True
    assert len(errors) == 0


def test_tamper_detection(tmp_path, mock_spanner_dataset):
    """Verifies that altering any exported file fails checksum validation."""
    output_dir = str(tmp_path / "tamper_test")
    exporter = SpannerSnapshotExporter(
        database=mock_spanner_dataset,
        expected_row_counts={},
    )
    exporter.export_snapshot(output_dir=output_dir)

    # Verify initial valid state
    valid, errors = verify_backup_checksums(output_dir)
    assert valid is True

    # Tamper with EvidenceChunks.jsonl
    chunks_path = os.path.join(output_dir, "data", "EvidenceChunks.jsonl")
    with open(chunks_path, "a") as f:
        f.write('{"tampered": true}\n')

    # Verify that validation detects the tampering
    tampered_valid, tampered_errors = verify_backup_checksums(output_dir)
    assert tampered_valid is False
    assert any("EvidenceChunks" in err and "SHA-256 mismatch" in err for err in tampered_errors)


def test_row_count_mismatch_detection(tmp_path):
    """Verifies that a mismatch between snapshot count and exported rows raises BackupVerificationError."""
    pinned_ts = datetime.now(timezone.utc)
    mock_db = MockDatabase({"Documents": [{"id": "1"}, {"id": "2"}]}, pinned_ts)

    # Corrupt execute_sql to return count 999 instead of 2
    original_exec = mock_db.snapshot().execute_sql

    class CorruptSnapshot(MockSnapshot):
        def execute_sql(self, query: str, **kwargs):
            if "SELECT COUNT(1)" in query:
                return MockResultSet([[999]])
            return original_exec(query, **kwargs)

    mock_db.snapshot = lambda **kw: CorruptSnapshot(mock_db.mock_data, pinned_ts)

    exporter = SpannerSnapshotExporter(database=mock_db, tables=["Documents"])
    output_dir = str(tmp_path / "mismatch_test")

    with pytest.raises(BackupVerificationError, match="Row count mismatch for table 'Documents'"):
        exporter.export_snapshot(output_dir=output_dir)


def test_restore_data_dry_run_and_commit(tmp_path, mock_spanner_dataset):
    """Tests restore_table_data in dry-run and commit modes."""
    output_dir = str(tmp_path / "restore_test")
    exporter = SpannerSnapshotExporter(database=mock_spanner_dataset, expected_row_counts={})
    exporter.export_snapshot(output_dir=output_dir)

    cols, rows = read_table_records(output_dir, "data/Documents.jsonl")

    # Dry-run
    mock_db = MagicMock()
    restored_dry = restore_table_data(mock_db, "Documents", cols, rows, dry_run=True)
    assert restored_dry == 2
    mock_db.run_in_transaction.assert_not_called()

    # Live commit
    mock_txn = MagicMock()
    mock_db.run_in_transaction.side_effect = lambda fn: fn(mock_txn)
    restored_live = restore_table_data(mock_db, "Documents", cols, rows, batch_size=1, dry_run=False)
    assert restored_live == 2
    assert mock_txn.insert_or_update.call_count == 2


def test_standalone_restore_script_execution(tmp_path, mock_spanner_dataset):
    """Tests running the generated standalone restore.py script with --verify-only."""
    output_dir = str(tmp_path / "script_exec_test")
    exporter = SpannerSnapshotExporter(database=mock_spanner_dataset, expected_row_counts={})
    exporter.export_snapshot(output_dir=output_dir)

    restore_script_path = os.path.join(output_dir, "restore.py")
    assert os.path.exists(restore_script_path)

    # Execute restore.py with --verify-only
    res = subprocess.run(
        [sys.executable, restore_script_path, "--backup-dir", output_dir, "--verify-only"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "Backup checksums VERIFIED successfully" in res.stderr or "Backup checksums VERIFIED successfully" in res.stdout
