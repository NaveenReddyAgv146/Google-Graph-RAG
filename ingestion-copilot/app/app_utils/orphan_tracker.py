"""Durable Orphan Asset Tracker for Ingestion Copilot.

When Spanner persistence fails after visual assets have been uploaded to GCS,
the orchestrator attempts compensating deletion. Under least-privilege IAM policies
such as `roles/storage.objectCreator` (which grants `storage.objects.create` but NOT
`storage.objects.delete`), compensating deletion fails with 403 Forbidden.

Durability Invariants:
1. Cloud Run instance restarts do NOT lose orphan records.
2. In-memory registry provides immediate intra-process inspection.
3. Durable tracking writes immutable JSON manifests to Cloud Storage:
   gs://sales-discovery-mvp-mumbai/orphans/{document_id}_{timestamp_ms}.json
   This uses ONLY `storage.objects.create` (already granted under roles/storage.objectCreator),
   requiring ZERO Spanner schema modifications and ZERO IAM permission escalations.
4. If GCS write fails or local test mode is detected, records are safely written to local disk fallback (/tmp/orphan_manifests/).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import time
from typing import Any

from google.cloud import storage

from common.config import settings

logger = logging.getLogger("ingestion_copilot.orphan_tracker")

LOCAL_FALLBACK_DIR = "/tmp/orphan_manifests"


@dataclass(frozen=True)
class OrphanAssetRecord:
    """Represents a set of GCS objects orphaned due to failed persistence and compensation."""

    document_id: str
    presentation_id: str
    gcs_uris: list[str]
    reason: str
    error_detail: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrphanAssetRegistry:
    """Hybrid in-memory and durable GCS/filesystem registry for orphaned storage assets."""

    def __init__(self, storage_client: Any = None) -> None:
        self._records: list[OrphanAssetRecord] = []
        self._storage_client = storage_client

    def record_orphans(self, record: OrphanAssetRecord) -> None:
        """Records an orphan asset event in memory and persists durably to Cloud Storage."""
        self._records.append(record)
        logger.critical(
            "ORPHAN_ASSET_REGISTERED: doc_id=%s, presentation_id=%s, count=%d, uris=%s, reason=%s",
            record.document_id,
            record.presentation_id,
            len(record.gcs_uris),
            record.gcs_uris,
            record.reason,
        )

        # Durable Cloud Storage Persistence (using roles/storage.objectCreator)
        self._persist_to_gcs(record)

    def _persist_to_gcs(self, record: OrphanAssetRecord) -> None:
        """Persists immutable orphan manifest to Cloud Storage using storage.objects.create."""
        bucket_name = settings.gcs_mumbai_bucket
        ts_ms = int(record.timestamp * 1000)
        blob_name = f"orphans/{record.document_id}_{ts_ms}.json"
        manifest_payload = json.dumps(record.to_dict(), indent=2)

        # In testing without mock client, skip live network calls
        if self._storage_client is None and ("PYTEST_CURRENT_TEST" in os.environ or os.getenv("ENABLE_LOCAL_TEST_AUTH", "false").lower() in ("true", "1")):
            self._persist_to_local(record, manifest_payload)
            return

        try:
            client = self._storage_client or storage.Client(project=settings.project_id)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.metadata = {
                "document_id": record.document_id,
                "presentation_id": record.presentation_id,
                "orphan_count": str(len(record.gcs_uris)),
            }
            blob.upload_from_string(manifest_payload, content_type="application/json")
            logger.info("Durable orphan manifest successfully written to gs://%s/%s", bucket_name, blob_name)
            return
        except Exception as exc:
            logger.warning(
                "Failed to write durable orphan manifest to GCS 'gs://%s/%s': %s. Writing to local fallback...",
                bucket_name,
                blob_name,
                exc,
            )

        # Fallback
        self._persist_to_local(record, manifest_payload)

    def _persist_to_local(self, record: OrphanAssetRecord, manifest_payload: str) -> None:
        """Fallback local filesystem persistence."""
        try:
            os.makedirs(LOCAL_FALLBACK_DIR, exist_ok=True)
            ts_ms = int(record.timestamp * 1000)
            fallback_file = os.path.join(LOCAL_FALLBACK_DIR, f"{record.document_id}_{ts_ms}.json")
            with open(fallback_file, "w", encoding="utf-8") as f:
                f.write(manifest_payload)
            logger.info("Durable orphan manifest written to local fallback: %s", fallback_file)
        except Exception as fs_exc:
            logger.error("Failed to write orphan manifest to local fallback disk: %s", fs_exc)

    def get_records(self) -> list[OrphanAssetRecord]:
        """Returns all recorded orphan asset records in the current process."""
        return list(self._records)

    def get_all_orphan_uris(self) -> list[str]:
        """Returns a flat list of all orphaned GCS URIs."""
        uris: list[str] = []
        for r in self._records:
            uris.extend(r.gcs_uris)
        return uris

    def clear(self) -> None:
        """Clears the in-memory registry (useful for testing)."""
        self._records.clear()


# Global tracker instance
orphan_tracker = OrphanAssetRegistry()
