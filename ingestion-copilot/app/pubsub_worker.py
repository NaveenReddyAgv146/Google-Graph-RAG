"""Pub/Sub Publishing, Worker Handler, and Reconciliation for Ingestion Copilot.

Provides:
1. publish_ingestion_event: Publishes DOCUMENT_UPLOADED events with exponential backoff retries.
2. reconcile_unprocessed_documents: Bounded, idempotent reconciliation worker. Recovers abandoned
   DISCOVERED and PUBLISH_FAILED documents.
3. process_ingestion_event: Idempotent worker event handler for DOCUMENT_UPLOADED Pub/Sub messages.
   Checks Spanner for existing extraction/chunks before processing to prevent duplicate work.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from google.cloud import pubsub_v1, spanner

from common.config import settings
from common.database.spanner import get_database
from app.enrichment_orchestrator import EnrichmentOrchestrator, EnrichmentStatus
from app.orchestrator import IngestionStatus, PdfIngestionOrchestrator

logger = logging.getLogger("ingestion_copilot.pubsub_worker")

MAX_RECONCILIATION_BATCH_SIZE = 200


def publish_ingestion_event(
    document_id: str,
    file_name: str,
    gcs_uri: str,
    size_bytes: int = 0,
    sha256_hash: str = "",
    uploaded_by: str = "system",
    max_retries: int = 3,
    publisher_client: Any = None,
) -> tuple[bool, str | None]:
    """Publishes a DOCUMENT_UPLOADED event to Pub/Sub with exponential backoff retries.

    Returns:
        (success: bool, error_or_message_id: str | None)
    """
    publisher = publisher_client or pubsub_v1.PublisherClient()
    topic_path = settings.ingestion_pubsub_topic
    payload = json.dumps({
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": document_id,
        "file_name": file_name,
        "gcs_uri": gcs_uri,
        "size_bytes": size_bytes,
        "sha256": sha256_hash,
        "uploaded_by": uploaded_by,
    }).encode("utf-8")

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            future = publisher.publish(
                topic_path,
                data=payload,
                document_id=document_id,
                event_type="DOCUMENT_UPLOADED",
            )
            message_id = future.result(timeout=10.0)
            logger.info(
                "Published ingestion event for '%s' (attempt %d, message_id=%s)",
                document_id,
                attempt,
                message_id,
            )
            return True, str(message_id)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Pub/Sub publish attempt %d failed for '%s': %s", attempt, document_id, exc
            )
            if attempt < max_retries:
                time.sleep(0.2 * (2 ** (attempt - 1)))

    return False, str(last_error)


def process_ingestion_event(
    event_payload: dict[str, Any],
    database: Any = None,
    pdf_orchestrator: Any = None,
    enrichment_orchestrator: Any = None,
) -> dict[str, Any]:
    """Worker event handler for DOCUMENT_UPLOADED Pub/Sub messages.

    Workflow:
    1. Idempotency & Recovery Check:
       - If document is already fully enriched and verified, returns SKIPPED_DUPLICATE.
       - If chunks already exist in Spanner, skips redundant PDF extraction and Document AI.
    2. PDF Ingestion (if not already extracted):
       - Parses layout and persists EvidenceChunks in Spanner.
       - Returns BATCH_REQUIRED or FAILED if extraction does not succeed.
    3. Automatic Enrichment (Phase 3C):
       - Triggers EnrichmentOrchestrator to generate embeddings, extract metadata,
         stage sidecar, atomically persist across 11 Spanner tables, and promote/verify canonical GCS sidecar.
       - Recovers interrupted enrichment before invoking expensive model calls.
       - Does not report completion if enrichment or sidecar verification fails.
    """
    event_type = event_payload.get("event_type")
    if event_type != "DOCUMENT_UPLOADED":
        return {"status": "IGNORED", "reason": f"Unsupported event type '{event_type}'"}

    doc_id = event_payload.get("document_id")
    gcs_uri = event_payload.get("gcs_uri")

    if not doc_id or not gcs_uri:
        return {
            "status": "INVALID_ARGUMENT",
            "error": "Missing 'document_id' or 'gcs_uri' in payload",
        }

    db = database or get_database()

    enricher = enrichment_orchestrator or EnrichmentOrchestrator(spanner_database=db)

    # 1. Spanner inspection: Check existing extraction, chunk count, and enrichment status
    doc_status = None
    chunk_count = 0
    try:
        with db.snapshot(multi_use=True) as snapshot:
            sql = """
            SELECT Status, (SELECT COUNT(1) FROM EvidenceChunks WHERE DocumentId = @doc_id)
            FROM Documents
            WHERE DocumentId = @doc_id
            """
            rows = list(
                snapshot.execute_sql(
                    sql,
                    params={"doc_id": doc_id},
                    param_types={"doc_id": spanner.param_types.STRING},
                )
            )
            if rows:
                doc_status = rows[0][0]
                chunk_count = rows[0][1] or 0
    except Exception as exc:
        logger.warning(
            "Spanner idempotency check failed for '%s': %s. Proceeding with caution.",
            doc_id,
            exc,
        )

    # 2. Duplicate delivery check for already completed and verified documents
    if doc_status == "ENRICHED":
        try:
            is_complete, checkpoint_details = enricher.check_completion_checkpoint(doc_id)
            if is_complete:
                logger.info(
                    "Document '%s' already fully enriched and verified (%d chunks, status=%s). Skipping duplicate event.",
                    doc_id,
                    chunk_count,
                    doc_status,
                )
                return {
                    "status": "SKIPPED_DUPLICATE",
                    "stage": "COMPLETED",
                    "document_id": doc_id,
                    "chunks_count": chunk_count,
                    "message": "Document already fully enriched and verified.",
                }
            logger.info(
                "Document '%s' marked ENRICHED but completion checkpoint failed (%s). Proceeding with recovery.",
                doc_id,
                checkpoint_details,
            )
        except Exception as exc:
            logger.warning(
                "Completion checkpoint inspection failed for '%s': %s. Proceeding with recovery.",
                doc_id,
                exc,
            )

    # 3. PDF Ingestion Phase (skip if chunks already exist in Spanner)
    if chunk_count > 0 or doc_status in ("EXTRACTION_COMPLETED", "PROCESSED", "ENRICHED"):
        logger.info(
            "Document '%s' already has %d chunks in Spanner (status=%s). Skipping PDF extraction, proceeding directly to enrichment.",
            doc_id,
            chunk_count,
            doc_status,
        )
        target_doc_id = doc_id
        chunks_persisted = chunk_count
    else:
        try:
            orchestrator = pdf_orchestrator or PdfIngestionOrchestrator(spanner_database=db)
            ingest_result = orchestrator.ingest_pdf(gcs_uri)
        except Exception as exc:
            logger.exception("Worker ingestion failed for doc '%s': %s", doc_id, exc)
            return {
                "status": "FAILED",
                "stage": "DOWNLOADING",
                "document_id": doc_id,
                "error": str(exc),
            }

        if ingest_result.status == IngestionStatus.BATCH_REQUIRED:
            logger.warning(
                "Document '%s' requires batch processing: %s",
                doc_id,
                ingest_result.error_message,
            )
            return {
                "status": ingest_result.status.value,
                "stage": ingest_result.stage.value,
                "document_id": ingest_result.document_id,
                "chunks_count": ingest_result.chunks_count,
                "error_message": ingest_result.error_message,
            }

        if ingest_result.status != IngestionStatus.SUCCESS:
            logger.error(
                "Ingestion failed for doc '%s' at stage %s: %s",
                doc_id,
                ingest_result.stage.value,
                ingest_result.error_message,
            )
            return {
                "status": ingest_result.status.value,
                "stage": ingest_result.stage.value,
                "document_id": ingest_result.document_id,
                "chunks_count": ingest_result.chunks_count,
                "error_message": ingest_result.error_message,
            }

        target_doc_id = ingest_result.document_id or doc_id
        chunks_persisted = ingest_result.chunks_count

    # 4. Automatic Enrichment Phase
    try:
        enrich_result = enricher.run(target_doc_id)
    except Exception as exc:
        logger.exception("Worker enrichment failed for doc '%s': %s", target_doc_id, exc)
        return {
            "status": "FAILED",
            "stage": "ENRICHMENT",
            "document_id": target_doc_id,
            "chunks_count": chunks_persisted,
            "error": str(exc),
        }

    if enrich_result.status in (EnrichmentStatus.SUCCESS, EnrichmentStatus.SKIPPED):
        logger.info(
            "Automatic enrichment succeeded for doc '%s' (status=%s, sidecar=%s)",
            target_doc_id,
            enrich_result.status.value,
            enrich_result.sidecar_uri,
        )
        return {
            "status": "SUCCESS",
            "stage": "COMPLETED",
            "document_id": target_doc_id,
            "chunks_count": chunks_persisted,
            "enrichment_status": enrich_result.status.value,
            "embeddings_generated_count": enrich_result.embeddings_generated_count,
            "entities_count": enrich_result.entities_count,
            "technologies_count": enrich_result.technologies_count,
            "solution_cards_count": enrich_result.solution_cards_count,
            "sidecar_uri": enrich_result.sidecar_uri,
            "sidecar_sha256": enrich_result.sidecar_sha256,
        }
    else:
        logger.error(
            "Automatic enrichment failed for doc '%s' at stage %s: %s",
            target_doc_id,
            enrich_result.stage.value if hasattr(enrich_result.stage, "value") else str(enrich_result.stage),
            enrich_result.error_message,
        )
        return {
            "status": "FAILED",
            "stage": enrich_result.stage.value if hasattr(enrich_result.stage, "value") else str(enrich_result.stage),
            "document_id": target_doc_id,
            "chunks_count": chunks_persisted,
            "error_message": enrich_result.error_message,
        }


def reconcile_unprocessed_documents(
    database: Any = None,
    publisher_client: Any = None,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Bounded, idempotent reconciliation worker.

    Recovers abandoned DISCOVERED and PUBLISH_FAILED documents:
    1. If a document already has chunks in EvidenceChunks, automatically repairs
       its status to 'EXTRACTION_COMPLETED' without redundant reprocessing.
    2. If a document has 0 chunks, re-publishes the ingestion event to Pub/Sub with backoff.
    3. Strictly bounded by MAX_RECONCILIATION_BATCH_SIZE (default: 50, max: 200).
    """
    bounded_limit = min(max(1, batch_size), MAX_RECONCILIATION_BATCH_SIZE)
    db = database or get_database()

    recovered_completed_count = 0
    republished_count = 0
    failed_count = 0
    processed_docs: list[dict[str, Any]] = []

    try:
        with db.snapshot() as snapshot:
            query = """
            SELECT d.DocumentId, d.FileName, d.GcsUri, d.Status,
                   (SELECT COUNT(1) FROM EvidenceChunks c WHERE c.DocumentId = d.DocumentId) AS ChunkCount
            FROM Documents d
            WHERE d.Status IN ('PUBLISH_FAILED', 'DISCOVERED')
            LIMIT @limit
            """
            rows = list(
                snapshot.execute_sql(
                    query,
                    params={"limit": bounded_limit},
                    param_types={"limit": spanner.param_types.INT64},
                )
            )

        for row in rows:
            doc_id, fname, gcs_uri, current_status, chunk_count = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4] or 0,
            )

            # Case A: Document already extracted; repair status to EXTRACTION_COMPLETED
            if chunk_count > 0:

                def _repair_status(txn: Any) -> None:
                    txn.update(
                        table="Documents",
                        columns=["DocumentId", "Status"],
                        values=[[doc_id, "EXTRACTION_COMPLETED"]],
                    )

                db.run_in_transaction(_repair_status)
                recovered_completed_count += 1
                processed_docs.append({
                    "document_id": doc_id,
                    "action": "STATUS_REPAIRED_COMPLETED",
                    "chunk_count": chunk_count,
                    "previous_status": current_status,
                })
                logger.info(
                    "Reconciliation: Repaired doc '%s' status to EXTRACTION_COMPLETED (%d chunks present).",
                    doc_id,
                    chunk_count,
                )
                continue

            # Case B: Document has 0 chunks; re-publish event to Pub/Sub
            if not gcs_uri:
                logger.warning("Reconciliation: Doc '%s' missing GcsUri. Skipping re-publish.", doc_id)
                continue

            success, err_or_id = publish_ingestion_event(
                document_id=doc_id,
                file_name=fname or "document.pdf",
                gcs_uri=gcs_uri,
                size_bytes=0,
                sha256_hash="",
                uploaded_by="reconciliation_worker",
                publisher_client=publisher_client,
            )
            if success:

                def _update_to_discovered(txn: Any) -> None:
                    txn.update(
                        table="Documents",
                        columns=["DocumentId", "Status"],
                        values=[[doc_id, "DISCOVERED"]],
                    )

                db.run_in_transaction(_update_to_discovered)
                republished_count += 1
                processed_docs.append({
                    "document_id": doc_id,
                    "action": "REPUBLISHED_FOR_INGESTION",
                    "message_id": err_or_id,
                    "previous_status": current_status,
                })
            else:

                def _mark_failed(txn: Any) -> None:
                    txn.update(
                        table="Documents",
                        columns=["DocumentId", "Status"],
                        values=[[doc_id, "PUBLISH_FAILED"]],
                    )

                db.run_in_transaction(_mark_failed)
                failed_count += 1
                processed_docs.append({
                    "document_id": doc_id,
                    "action": "REPUBLISH_FAILED",
                    "error": err_or_id,
                    "previous_status": current_status,
                })

        return {
            "status": "SUCCESS",
            "candidates_inspected": len(rows),
            "recovered_already_extracted": recovered_completed_count,
            "republished_for_ingestion": republished_count,
            "failed_republish": failed_count,
            "processed_documents": processed_docs,
        }
    except Exception as exc:
        logger.exception("Reconciliation worker execution failed: %s", exc)
        return {
            "status": "ERROR",
            "error": str(exc),
        }
