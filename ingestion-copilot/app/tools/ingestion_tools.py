"""Production ADK Ingestion Tools for Ingestion Copilot.

Exposes:
1. trigger_pdf_ingestion:
   - Validates GCS URI against approved buckets.
   - Enforces authenticated principal resolution via ToolContext.
   - Invokes PdfIngestionOrchestrator to parse via Document AI and persist to Spanner.
2. trigger_slides_ingestion:
   - Validates presentation ID / Google Slides URL format.
   - Enforces authenticated principal resolution via ToolContext.
   - Invokes GoogleSlidesOrchestrator to extract slides, upload assets, and persist to Spanner.
3. check_document_ingestion_status:
   - Checks extraction, enrichment, and human-review states for a document ID or document name.
   - Queries Spanner Documents, EvidenceChunks, and SolutionCards.
   - Never claims approval unless SolutionCards.VerificationStatus == 'APPROVED'.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google.adk.tools import ToolContext
from google.cloud import spanner

from app.orchestrator import (
    DocumentIngestionResult,
    IngestionStatus,
    PdfIngestionOrchestrator,
)
from app.slides_orchestrator import (
    GoogleSlidesOrchestrator,
    SlidesIngestionResult,
    extract_presentation_id,
)
from common.config import settings
from common.database.spanner import get_database
from common.models.schema import VerificationStatus
from common.storage.pdf_reader import parse_gcs_uri, validate_bucket_approved

logger = logging.getLogger("ingestion_copilot.tools")

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def get_principal_from_tool_context(tool_context: ToolContext | None) -> str:
    """Extracts authenticated principal email from the trusted ADK ToolContext.

    Fails closed if authentication context is missing, empty, or not a valid email.
    """
    candidate: str | None = None
    if tool_context is not None:
        if hasattr(tool_context, "user_id") and tool_context.user_id:
            candidate = str(tool_context.user_id).strip()
        elif hasattr(tool_context, "session") and tool_context.session and hasattr(tool_context.session, "user_id"):
            if tool_context.session.user_id:
                candidate = str(tool_context.session.user_id).strip()

    if candidate and EMAIL_REGEX.match(candidate):
        return candidate.lower()

    if candidate:
        raise PermissionError(f"Authentication required: Invalid or malformed principal identity '{candidate}'")

    raise PermissionError("Authentication required: Missing trusted user identity in ToolContext")


def trigger_pdf_ingestion(
    gcs_uri: str,
    tool_context: ToolContext | None = None,
) -> str:
    """Triggers the production PDF ingestion pipeline for an approved GCS document.

    Args:
        gcs_uri: The Google Cloud Storage URI (e.g. gs://sales-discovery-mvp-mumbai/raw/report.pdf).
        tool_context: Trusted ADK ToolContext carrying the authenticated caller identity.

    Returns:
        JSON string reporting document_id, status, chunks_count, and execution summary.
    """
    try:
        principal = get_principal_from_tool_context(tool_context)
    except Exception as exc:
        logger.warning("trigger_pdf_ingestion authentication failure: %s", exc)
        return json.dumps({
            "status": "UNAUTHORIZED",
            "error": "Authentication required: Caller identity could not be verified.",
        })

    logger.info("Principal '%s' triggered PDF ingestion for '%s'", principal, gcs_uri)

    # 1. Validate GCS URI format and approved bucket whitelist
    try:
        bucket, blob = parse_gcs_uri(gcs_uri)
        validate_bucket_approved(bucket)
        if not blob or not blob.lower().endswith(".pdf"):
            return json.dumps({
                "status": "INVALID_ARGUMENT",
                "error": f"The provided URI '{gcs_uri}' must point to a valid .pdf file.",
            })
    except Exception as exc:
        return json.dumps({
            "status": "VALIDATION_ERROR",
            "error": str(exc),
        })

    # 2. Invoke Orchestrator
    try:
        orchestrator = PdfIngestionOrchestrator()
        result: DocumentIngestionResult = orchestrator.ingest_pdf(gcs_uri)
        return json.dumps({
            "status": result.status.value,
            "stage": result.stage.value,
            "document_id": result.document_id,
            "gcs_uri": result.gcs_uri,
            "chunks_count": result.chunks_count,
            "batch_required": result.batch_required,
            "duration_ms": round(result.duration_ms, 2),
            "error_message": result.error_message,
        })
    except Exception as exc:
        logger.exception("Failed to execute PDF ingestion pipeline: %s", exc)
        return json.dumps({
            "status": "FAILED",
            "error": str(exc),
        })


def trigger_slides_ingestion(
    presentation_id_or_url: str,
    tool_context: ToolContext | None = None,
) -> str:
    """Triggers the production Google Slides ingestion pipeline for an enterprise presentation.

    Args:
        presentation_id_or_url: Google Slides presentation URL or raw alphanumeric presentation ID.
        tool_context: Trusted ADK ToolContext carrying the authenticated caller identity.

    Returns:
        JSON string reporting document_id, presentation_id, slide_count, chunks_count, and status.
    """
    try:
        principal = get_principal_from_tool_context(tool_context)
    except Exception as exc:
        logger.warning("trigger_slides_ingestion authentication failure: %s", exc)
        return json.dumps({
            "status": "UNAUTHORIZED",
            "error": "Authentication required: Caller identity could not be verified.",
        })

    logger.info("Principal '%s' triggered Google Slides ingestion for '%s'", principal, presentation_id_or_url)

    # 1. Validate Presentation ID
    try:
        pres_id = extract_presentation_id(presentation_id_or_url)
    except Exception as exc:
        return json.dumps({
            "status": "INVALID_ARGUMENT",
            "error": f"Invalid Google Slides identifier: {exc}",
        })

    # 2. Invoke Slides Orchestrator
    try:
        orchestrator = GoogleSlidesOrchestrator()
        result: SlidesIngestionResult = orchestrator.ingest_presentation(pres_id)
        return json.dumps({
            "status": result.status.value,
            "stage": result.stage.value,
            "document_id": result.document_id,
            "presentation_id": result.presentation_id,
            "slide_count": result.slide_count,
            "chunks_count": result.chunks_count,
            "visual_assets_count": result.visual_assets_count,
            "duration_ms": round(result.duration_ms, 2),
            "error_message": result.error_message,
        })
    except Exception as exc:
        logger.exception("Failed to execute Google Slides ingestion pipeline: %s", exc)
        return json.dumps({
            "status": "FAILED",
            "error": str(exc),
        })


def check_document_ingestion_status(
    document_id: str | None = None,
    document_name: str | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Checks the end-to-end extraction, enrichment, and human-review status of a document.

    Args:
        document_id: Explicit DocumentId (e.g. doc_0123456789abcdef...).
        document_name: Optional file name or presentation title to search for.
        tool_context: Trusted ADK ToolContext carrying authenticated identity.

    Returns:
        JSON string with extraction_status, enrichment_status, and human_review_status.
    """
    try:
        principal = get_principal_from_tool_context(tool_context)
    except Exception as exc:
        logger.warning("check_document_ingestion_status authentication failure: %s", exc)
        return json.dumps({
            "status": "UNAUTHORIZED",
            "error": "Authentication required: Caller identity could not be verified.",
        })

    if not document_id and not document_name:
        return json.dumps({
            "status": "INVALID_ARGUMENT",
            "error": "Either document_id or document_name must be supplied.",
        })

    try:
        database = get_database()
        with database.snapshot() as snapshot:
            # 1. Lookup Document record
            if document_id:
                query = "SELECT DocumentId, FileName, MimeType, GcsUri, Status FROM Documents WHERE DocumentId = @doc_id"
                params = {"doc_id": document_id}
                param_types = {"doc_id": spanner.param_types.STRING}
            else:
                query = "SELECT DocumentId, FileName, MimeType, GcsUri, Status FROM Documents WHERE LOWER(FileName) LIKE @name LIMIT 1"
                params = {"name": f"%{document_name.lower().strip()}%"}
                param_types = {"name": spanner.param_types.STRING}

            doc_rows = list(snapshot.execute_sql(query, params=params, param_types=param_types))
            if not doc_rows:
                return json.dumps({
                    "found": False,
                    "message": f"No document found matching criteria (id={document_id}, name={document_name}).",
                })

            target_doc_id, file_name, mime_type, gcs_uri, extraction_status = doc_rows[0]

            # 2. Count chunks and check embedding status
            chunk_query = """
            SELECT COUNT(1), COUNTIF(Embedding768 IS NOT NULL)
            FROM EvidenceChunks
            WHERE DocumentId = @doc_id
            """
            chunk_res = list(snapshot.execute_sql(
                chunk_query,
                params={"doc_id": target_doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            ))
            total_chunks = chunk_res[0][0] if chunk_res else 0
            embedded_chunks = chunk_res[0][1] if chunk_res else 0

            # 3. Query SolutionCards and Human Verification Status
            card_query = """
            SELECT CardId, ProjectName, VerificationStatus
            FROM SolutionCards
            WHERE DocumentId = @doc_id
            """
            card_rows = list(snapshot.execute_sql(
                card_query,
                params={"doc_id": target_doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            ))

            cards_summary = []
            has_approved_card = False
            for row in card_rows:
                cid, proj_name, v_status = row[0], row[1], row[2] or "DRAFT"
                cards_summary.append({
                    "card_id": cid,
                    "project_name": proj_name,
                    "verification_status": v_status,
                })
                if v_status == VerificationStatus.APPROVED.value:
                    has_approved_card = True

            # Determine enrichment status
            if embedded_chunks > 0 and cards_summary:
                enrichment_status = "COMPLETED"
            elif total_chunks > 0:
                enrichment_status = "PENDING_ENRICHMENT"
            else:
                enrichment_status = "NOT_STARTED"

            # Determine human review status
            if not cards_summary:
                review_status = "NO_CARDS_GENERATED"
            elif has_approved_card:
                review_status = "APPROVED"
            elif any(c["verification_status"] == VerificationStatus.PENDING.value for c in cards_summary):
                review_status = "PENDING_REVIEW"
            elif any(c["verification_status"] == VerificationStatus.CHANGES_REQUESTED.value for c in cards_summary):
                review_status = "CHANGES_REQUESTED"
            elif all(c["verification_status"] == VerificationStatus.REJECTED.value for c in cards_summary):
                review_status = "REJECTED"
            else:
                review_status = "DRAFT"

            return json.dumps({
                "found": True,
                "document_id": target_doc_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "gcs_uri": gcs_uri,
                "extraction_status": extraction_status or "COMPLETED",
                "chunks_count": total_chunks,
                "embedded_chunks_count": embedded_chunks,
                "enrichment_status": enrichment_status,
                "human_review_status": review_status,
                "solution_cards": cards_summary,
                "can_be_queried_by_discovery_copilot": has_approved_card,
            })

    except Exception as exc:
        logger.exception("Failed to query document ingestion status: %s", exc)
        return json.dumps({
            "status": "ERROR",
            "error": f"Database query failed: {exc}",
        })

