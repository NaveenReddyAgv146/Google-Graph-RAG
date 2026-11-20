"""FastAPI Application for Ingestion Copilot.

Includes:
1. ADK Agent & A2A Endpoints.
2. Google Cloud IAP Cryptographic Authentication Middleware (EnforceAuthenticatedPrincipalMiddleware).
3. Document Upload Ingress API (POST /api/v1/documents/upload):
   - PDF validation (mime-type, magic bytes, extension, file size <= 50MB).
   - Safe streaming upload to gs://sales-discovery-mvp-mumbai/raw/{sanitized_filename}.
   - Stable deterministic document ID generation (doc_{sha256[:32]}).
   - Idempotency check against Spanner Documents table.
   - Spanner DocumentNode registration (status: "DISCOVERED").
   - Pub/Sub notification to ingestion-events topic.
   - Safe error handling and cleanup on partial failure.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from collections.abc import AsyncIterator

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.cloud import pubsub_v1, spanner, storage

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.security import (
    AuthenticationError,
    AuthorizationError,
    EnforceAuthenticatedPrincipalMiddleware,
    extract_and_verify_pubsub_principal,
)
from app.pubsub_worker import (
    process_ingestion_event,
    publish_ingestion_event,
    reconcile_unprocessed_documents,
)
from common.config import settings
from common.database.spanner import get_database

load_dotenv()
logger = logging.getLogger("ingestion_copilot.fast_api")

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)
otel_to_cloud = os.environ.get(
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", ""
).lower() in ("true", "1")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Maximum upload file size: 50 MB
MAX_UPLOAD_SIZE_BYTES = 50 * 1024 * 1024
# Whitelisted MIME types
ALLOWED_MIME_TYPES = frozenset({"application/pdf"})
# PDF Magic Bytes: %PDF-
PDF_MAGIC_BYTES = b"%PDF-"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "ingestion-copilot"
app.description = "API for interacting with the Agent ingestion-copilot"

# Security: Enforce cryptographic IAP identity validation and prevent session spoofing
app.add_middleware(EnforceAuthenticatedPrincipalMiddleware)

# Path to the HTML upload UI template
_UPLOAD_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "upload_ui.html"


# ============================================================================
# DOCUMENT UPLOAD BROWSER UI ENDPOINT
# ============================================================================


@app.get("/upload", response_class=HTMLResponse, tags=["Document Upload UI"])
async def get_upload_page(request: Request) -> HTMLResponse:
    """Renders the browser-based PDF Document Upload web interface.

    Enforces IAP authentication via EnforceAuthenticatedPrincipalMiddleware.
    """
    authenticated_user = getattr(request.state, "authenticated_user_email", "Authorized User")
    if not _UPLOAD_TEMPLATE_PATH.exists():
        logger.error("Upload UI template not found at '%s'", _UPLOAD_TEMPLATE_PATH)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload UI template file not found.",
        )
    html_content = _UPLOAD_TEMPLATE_PATH.read_text(encoding="utf-8")
    html_content = html_content.replace("{{ authenticated_user }}", authenticated_user)
    return HTMLResponse(content=html_content)


# ============================================================================
# DOCUMENT UPLOAD INGRESS ENDPOINT
# ============================================================================


def sanitize_filename(filename: str) -> str:
    """Sanitizes user-provided filename to prevent path traversal and unsafe characters."""
    clean = os.path.basename(filename.strip())
    # Replace any character other than alphanumeric, dots, underscores, hyphens
    clean = re.sub(r"[^a-zA-Z0-9._-]", "_", clean)
    if not clean.lower().endswith(".pdf"):
        clean += ".pdf"
    return clean


@app.post("/api/v1/documents/upload", status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Secure document upload endpoint for PDF ingestion.

    Validates:
    - User authentication (authenticated principal attached to request.state).
    - File extension (.pdf).
    - MIME type (application/pdf).
    - Magic bytes (%PDF-).
    - File size (<= 50MB).
    - Computes SHA-256 for deterministic doc_id generation and idempotency.
    - Streams file to gs://sales-discovery-mvp-mumbai/raw/{sha256[:16]}_{clean_name}.
    - Inserts Document record into Spanner with status DISCOVERED.
    - Publishes ingestion event to Cloud Pub/Sub topic ingestion-events.
    """
    authenticated_user = getattr(request.state, "authenticated_user_email", None)
    if not authenticated_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: No verified principal identity found.",
        )

    # 1. Validate file extension
    raw_name = file.filename or "upload.pdf"
    if not raw_name.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file extension. Only PDF documents are supported for upload (received '{raw_name}').",
        )

    # 2. Validate MIME type
    if file.content_type and file.content_type.lower() not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported content type '{file.content_type}'. Must be 'application/pdf'.",
        )

    clean_filename = sanitize_filename(raw_name)

    # 3. Read content in chunks, validate size, magic bytes, and compute SHA-256
    hasher = hashlib.sha256()
    total_size = 0
    first_chunk = True
    content_chunks: list[bytes] = []

    try:
        while chunk := await file.read(64 * 1024):  # 64KB chunk
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File exceeds maximum allowed size of {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB.",
                )
            if first_chunk:
                if not chunk.startswith(PDF_MAGIC_BYTES):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="File content failed PDF magic byte verification (%PDF-).",
                    )
                first_chunk = False

            hasher.update(chunk)
            content_chunks.append(chunk)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error reading uploaded file: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error reading upload stream: {exc}",
        ) from exc

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty (0 bytes).",
        )

    full_content = b"".join(content_chunks)
    sha256_hash = hasher.hexdigest()
    document_id = f"doc_{sha256_hash[:32]}"
    target_bucket = settings.gcs_mumbai_bucket
    target_blob_name = f"raw/{sha256_hash[:16]}_{clean_filename}"
    gcs_uri = f"gs://{target_bucket}/{target_blob_name}"

    # 4. Check Spanner for existing Document (Idempotency)
    database = get_database()
    try:
        with database.snapshot() as snapshot:
            existing = list(
                snapshot.execute_sql(
                    "SELECT DocumentId, Status, FileName FROM Documents WHERE DocumentId = @doc_id",
                    params={"doc_id": document_id},
                    param_types={"doc_id": spanner.param_types.STRING},
                )
            )
            if existing:
                existing_doc_id, existing_status, existing_name = existing[0]
                logger.info("Document '%s' already exists in Spanner with status '%s'", existing_doc_id, existing_status)
                return {
                    "document_id": existing_doc_id,
                    "file_name": existing_name,
                    "status": existing_status or "DISCOVERED",
                    "gcs_uri": gcs_uri,
                    "size_bytes": total_size,
                    "message": "Document already uploaded and registered.",
                    "is_duplicate": True,
                }
    except Exception as exc:
        logger.warning("Spanner snapshot query check error: %s", exc)

    # 5. Stream content to GCS
    try:
        storage_client = storage.Client(project=settings.project_id)
        bucket = storage_client.bucket(target_bucket)
        blob = bucket.blob(target_blob_name)
        blob.metadata = {
            "uploaded_by": authenticated_user,
            "original_filename": clean_filename,
            "sha256": sha256_hash,
            "size_bytes": str(total_size),
        }
        blob.upload_from_string(full_content, content_type="application/pdf")
        logger.info("Streamed %d bytes to '%s'", total_size, gcs_uri)
    except Exception as exc:
        logger.error("Failed to stream uploaded file to GCS '%s': %s", gcs_uri, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage upload failed: {exc}",
        ) from exc

    # 6. Insert Document record into Spanner
    try:
        def _insert_doc_txn(transaction):
            transaction.insert_or_update(
                table="Documents",
                columns=["DocumentId", "FileName", "MimeType", "SourceUrl", "GcsUri", "Status"],
                values=[[document_id, clean_filename, "application/pdf", None, gcs_uri, "DISCOVERED"]],
            )

        database.run_in_transaction(_insert_doc_txn)
        logger.info("Document record '%s' registered in Spanner.", document_id)
    except Exception as exc:
        logger.error("Failed to persist Document record in Spanner: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to record document metadata in database: {exc}",
        ) from exc

    # 7. Publish Ingestion Notification to Cloud Pub/Sub with retry
    pubsub_published, pubsub_result = publish_ingestion_event(
        document_id=document_id,
        file_name=clean_filename,
        gcs_uri=gcs_uri,
        size_bytes=total_size,
        sha256_hash=sha256_hash,
        uploaded_by=authenticated_user,
        max_retries=3,
    )

    doc_status = "DISCOVERED"
    pubsub_error = None
    if not pubsub_published:
        pubsub_error = pubsub_result
        logger.warning(
            "Pub/Sub publishing failed after retries for '%s': %s. Updating status to 'PUBLISH_FAILED'.",
            document_id,
            pubsub_error,
        )
        try:
            def _mark_publish_failed(transaction):
                transaction.update(
                    table="Documents",
                    columns=["DocumentId", "Status"],
                    values=[[document_id, "PUBLISH_FAILED"]],
                )
            database.run_in_transaction(_mark_publish_failed)
            doc_status = "PUBLISH_FAILED"
        except Exception as update_err:
            logger.error("Failed to update status to PUBLISH_FAILED for '%s': %s", document_id, update_err)

    return {
        "document_id": document_id,
        "file_name": clean_filename,
        "status": doc_status,
        "gcs_uri": gcs_uri,
        "size_bytes": total_size,
        "pubsub_notified": pubsub_published,
        "pubsub_warning": pubsub_error,
        "message": (
            "Document successfully uploaded and queued for ingestion."
            if pubsub_published
            else "Document uploaded to GCS and registered in database, but Pub/Sub notification failed. Status marked as PUBLISH_FAILED for reconciliation."
        ),
        "is_duplicate": False,
    }


@app.post("/api/v1/reconciliation/trigger", status_code=status.HTTP_200_OK)
@app.post("/api/v1/documents/reconcile", status_code=status.HTTP_200_OK)
async def trigger_reconciliation(
    request: Request,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Cloud Scheduler and administrative reconciliation trigger endpoint.

    Recovers abandoned DISCOVERED and PUBLISH_FAILED documents idempotently.
    Bounded by MAX_RECONCILIATION_BATCH_SIZE (max 200).
    """
    authenticated_user = getattr(request.state, "authenticated_user_email", None)
    if not authenticated_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: No verified principal identity found.",
        )

    database = get_database()
    return reconcile_unprocessed_documents(database=database, batch_size=batch_size)


@app.post("/api/v1/pubsub/push", status_code=status.HTTP_200_OK)
async def pubsub_push_handler(request: Request) -> dict[str, Any]:
    """Cloud Pub/Sub push subscription webhook handler.

    Independently authenticates Pub/Sub OIDC ID token:
    - Verifies cryptographic signature against Google OAuth2 public certs.
    - Verifies issuer is https://accounts.google.com.
    - Verifies audience matches expected endpoint.
    - Verifies service account identity matches authorized Pub/Sub publisher SA.
    """
    try:
        authenticated_sa = extract_and_verify_pubsub_principal(request)
        logger.info("Pub/Sub push authenticated as service account: '%s'", authenticated_sa)
    except AuthorizationError as exc:
        logger.warning("Pub/Sub push authorization forbidden: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except AuthenticationError as exc:
        logger.warning("Pub/Sub push authentication unauthorized: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON in push envelope: {exc}",
        ) from exc

    message = body.get("message", {})
    data_b64 = message.get("data")
    if not data_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'data' in Pub/Sub message envelope",
        )

    try:
        data_json = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode base64 message data: {exc}",
        ) from exc

    database = get_database()
    result = process_ingestion_event(data_json, database=database)
    if result.get("status") == "FAILED":
        err_msg = result.get("error_message") or result.get("error") or "Worker processing failed"
        logger.error(
            "Pub/Sub processing failed for document '%s' at stage %s: %s",
            result.get("document_id"),
            result.get("stage"),
            err_msg,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Pub/Sub processing failed: {err_msg}",
        )
    return result


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
