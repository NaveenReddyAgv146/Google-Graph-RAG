"""Type definitions, constants, and serialization helpers for Spanner logical backups."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

# The exact 8 existing tables in sales-knowledge-db
EXISTING_TABLES: list[str] = [
    "Documents",
    "DocumentAccess",
    "DriveSyncDocuments",
    "EvidenceChunks",
    "VisualAssets",
    "SolutionCards",
    "Embeddings",
    "EvidenceLinks",
]

# Baseline row counts verified during Phase 2B final audit (Total = 426)
EXPECTED_BASELINE_ROW_COUNTS: dict[str, int] = {
    "Documents": 5,
    "DocumentAccess": 10,
    "DriveSyncDocuments": 5,
    "EvidenceChunks": 195,
    "VisualAssets": 125,
    "SolutionCards": 5,
    "Embeddings": 81,
    "EvidenceLinks": 0,
}

DEFAULT_GCS_BACKUP_BASE: str = "gs://sales-discovery-mvp-mumbai/backups/spanner"


def serialize_value(val: Any) -> Any:
    """
    Serializes a Spanner column value to a JSON-compatible primitive.
    Handles timestamps, byte arrays, lists/arrays, nested dictionaries, and scalars.
    """
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return {"__type__": "timestamp", "value": val.isoformat()}
    if isinstance(val, (bytes, bytearray)):
        return {
            "__type__": "bytes",
            "value": base64.b64encode(val).decode("ascii"),
        }
    if isinstance(val, list):
        return [serialize_value(x) for x in val]
    if isinstance(val, dict):
        return {str(k): serialize_value(v) for k, v in val.items()}
    if isinstance(val, (int, float, str, bool)):
        return val
    # Fallback to string representation for other Spanner objects
    return str(val)


def deserialize_value(val: Any) -> Any:
    """
    Deserializes a JSON-compatible primitive back to native Python/Spanner types.
    """
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


def compute_file_checksums(file_path: str) -> tuple[str, str, int]:
    """
    Computes SHA-256 and MD5 hex digests and total byte size of a file.
    Returns: (sha256_hex, md5_hex, byte_size)
    """
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha256.hexdigest(), md5.hexdigest(), size


@dataclass
class ColumnDefinition:
    name: str
    spanner_type: str
    is_nullable: bool
    ordinal_position: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColumnDefinition:
        return cls(**data)


@dataclass
class TableSchema:
    table_name: str
    columns: list[ColumnDefinition]
    primary_key: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "columns": [col.to_dict() for col in self.columns],
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableSchema:
        return cls(
            table_name=data["table_name"],
            columns=[ColumnDefinition.from_dict(c) for c in data["columns"]],
            primary_key=list(data.get("primary_key", [])),
        )


@dataclass
class TableBackupMetadata:
    table_name: str
    file_name: str
    row_count: int
    sha256: str
    md5: str
    byte_size: int
    primary_key: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableBackupMetadata:
        return cls(**data)


@dataclass
class BackupManifest:
    manifest_version: str
    backup_id: str
    project_id: str
    instance_id: str
    database_id: str
    gcs_target_uri: str
    read_timestamp: str
    created_at: str
    tables: dict[str, TableBackupMetadata]
    files: dict[str, dict[str, Any]]
    total_tables: int
    total_rows: int
    total_bytes: int
    verification_status: str  # 'VERIFIED', 'MISMATCH', 'FAILED'
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tables"] = {k: v.to_dict() for k, v in self.tables.items()}
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        tables = {
            k: TableBackupMetadata.from_dict(v) for k, v in data["tables"].items()
        }
        return cls(
            manifest_version=data["manifest_version"],
            backup_id=data["backup_id"],
            project_id=data["project_id"],
            instance_id=data["instance_id"],
            database_id=data["database_id"],
            gcs_target_uri=data["gcs_target_uri"],
            read_timestamp=data["read_timestamp"],
            created_at=data["created_at"],
            tables=tables,
            files=dict(data.get("files", {})),
            total_tables=data["total_tables"],
            total_rows=data["total_rows"],
            total_bytes=data["total_bytes"],
            verification_status=data["verification_status"],
            details=dict(data.get("details", {})),
        )

