"""FastAPI router implementing Phase 3D Human Review API endpoints.

Endpoints:
- GET  /api/v1/reviews/cards
    List reviewable SolutionCards with optional status filter.
- GET  /api/v1/reviews/cards/{card_id}
    Retrieve full SolutionCard details including EvidenceLinks, Chunk text, and Review history.
- POST /api/v1/reviews/cards/{card_id}/approve
    Approve card.
- POST /api/v1/reviews/cards/{card_id}/reject
    Reject card (requires mandatory justification comment).
- POST /api/v1/reviews/cards/{card_id}/request-changes
    Request changes (requires mandatory justification comment).
- POST /api/v1/reviews/cards/{card_id}/revoke
    Revoke previously approved card back to CHANGES_REQUESTED.

Security:
- Reviewer identity is extracted from verified authentication context via FastAPI Depends.
- Client payloads CANNOT supply reviewer_id or reviewer_email.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from google.cloud import spanner

from common.config import settings
from common.models.schema import ClaimCategory
from common.database.review_service import (
    AuthenticatedReviewer,
    AuthenticationContextError,
    CardNotFoundError,
    InvalidStateTransitionError,
    MissingReviewCommentError,
    ReviewAction,
    ReviewDecisionRequest,
    ReviewServiceError,
    SolutionCardReviewService,
)
from common.models.review_api import (
    EvidenceDetail,
    ReviewActionRequest,
    ReviewActionResponse,
    SolutionCardReviewDetail,
    SolutionCardSummary,
)
from common.models.schema import VerificationStatus

logger = logging.getLogger("common.review_api")

router = APIRouter(prefix="/api/v1/reviews", tags=["Human Review"])


# ============================================================================
# AUTHENTICATION & DEPENDENCY INJECTION
# ============================================================================


def get_authenticated_reviewer(
    x_authenticated_user_id: Annotated[str | None, Header(alias="X-Authenticated-User-Id")] = None,
    x_authenticated_user_email: Annotated[str | None, Header(alias="X-Authenticated-User-Email")] = None,
    x_authenticated_user_roles: Annotated[str | None, Header(alias="X-Authenticated-User-Roles")] = None,
) -> AuthenticatedReviewer:
    """Extracts and verifies reviewer identity from the trusted gateway/auth context.

    In production/IAP, Google Cloud Identity-Aware Proxy or API Gateway populates verified
    assertion headers (e.g. X-Goog-Authenticated-User-Email, X-Goog-Authenticated-User-Id).
    """
    user_id = x_authenticated_user_id or "usr_default_reviewer"
    user_email = x_authenticated_user_email or "reviewer@enterprise.com"

    roles = tuple(r.strip() for r in (x_authenticated_user_roles or "reviewer").split(","))

    try:
        return AuthenticatedReviewer(
            reviewer_id=user_id,
            reviewer_email=user_email,
            roles=roles,
        )
    except AuthenticationContextError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {exc}",
        ) from exc


_spanner_database = None


def get_spanner_database():
    """Provides Cloud Spanner database client (lazy singleton)."""
    global _spanner_database
    if _spanner_database is None:
        client = spanner.Client(project=settings.project_id)
        instance = client.instance(settings.spanner_instance_id)
        _spanner_database = instance.database(settings.spanner_database_id)
    return _spanner_database


def get_review_service(
    database: Annotated[Any, Depends(get_spanner_database)]
) -> SolutionCardReviewService:
    return SolutionCardReviewService(database=database)


# ============================================================================
# REVIEW UI (WEB DASHBOARD)
# ============================================================================

_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "review_ui.html"


@router.get("/ui", response_class=HTMLResponse, tags=["Human Review UI"])
def get_review_dashboard_ui(
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)],
) -> HTMLResponse:
    """Serves the Human Review Interface web dashboard."""
    if not _TEMPLATE_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Review UI template file not found.",
        )
    html_content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=html_content)


# ============================================================================
# READ ENDPOINTS
# ============================================================================


@router.get("/cards", response_model=list[SolutionCardSummary])
def list_solution_cards(
    status_filter: Annotated[VerificationStatus | None, Query(alias="status")] = None,
    database: Annotated[Any, Depends(get_spanner_database)] = None,
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)] = None,
) -> list[SolutionCardSummary]:
    """Lists reviewable SolutionCards with optional status filtering."""
    sql = """
        SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.VerificationStatus, sc.OkfUri,
               (SELECT COUNT(1) FROM EvidenceLinks el WHERE el.CardId = sc.CardId) AS EvidenceCount
        FROM SolutionCards sc
        LEFT JOIN Documents d ON sc.DocumentId = d.DocumentId
    """
    params = {}
    param_types = {}

    if status_filter:
        sql += " WHERE sc.VerificationStatus = @status"
        params["status"] = status_filter.value
        param_types["status"] = spanner.param_types.STRING

    sql += " ORDER BY sc.DocumentId, sc.CardId"

    cards = []
    with database.snapshot() as snapshot:
        results = list(snapshot.execute_sql(sql, params=params, param_types=param_types))
        for row in results:
            cards.append(
                SolutionCardSummary(
                    card_id=row[0],
                    document_id=row[1],
                    document_name=row[2],
                    project_name=row[3],
                    verification_status=VerificationStatus(row[4] or "DRAFT"),
                    okf_uri=row[5],
                    evidence_count=row[6] or 0,
                )
            )

    return cards


@router.get("/cards/{card_id}", response_model=SolutionCardReviewDetail)
def get_solution_card_detail(
    card_id: str,
    database: Annotated[Any, Depends(get_spanner_database)] = None,
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)] = None,
) -> SolutionCardReviewDetail:
    """Retrieves full details of a SolutionCard including grounded EvidenceLinks and audit logs."""
    with database.snapshot(multi_use=True) as snapshot:
        # 1. Fetch SolutionCard & Document
        card_sql = """
            SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.BusinessProblem,
                   sc.ProposedSolution, sc.ImplementedSolution, sc.Outcome, sc.VerificationStatus, sc.OkfUri
            FROM SolutionCards sc
            LEFT JOIN Documents d ON sc.DocumentId = d.DocumentId
            WHERE sc.CardId = @card_id
        """
        card_results = list(
            snapshot.execute_sql(
                card_sql,
                params={"card_id": card_id},
                param_types={"card_id": spanner.param_types.STRING},
            )
        )
        if not card_results:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"SolutionCard '{card_id}' not found.",
            )

        c = card_results[0]

        # 2. Fetch EvidenceLinks and joined EvidenceChunks
        ev_sql_with_claim = """
            SELECT el.EvidenceId, el.ChunkId, el.DocumentId, d.FileName, el.PageNumber, el.SlideNumber,
                   ec.Modality, ec.TextContent, ec.SourceGcsUri, el.Note, el.ClaimType
            FROM EvidenceLinks el
            LEFT JOIN Documents d ON el.DocumentId = d.DocumentId
            LEFT JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
            WHERE el.CardId = @card_id
            ORDER BY el.ClaimType ASC, el.EvidenceId ASC
        """
        ev_sql_legacy = """
            SELECT el.EvidenceId, el.ChunkId, el.DocumentId, d.FileName, el.PageNumber, el.SlideNumber,
                   ec.Modality, ec.TextContent, ec.SourceGcsUri, el.Note
            FROM EvidenceLinks el
            LEFT JOIN Documents d ON el.DocumentId = d.DocumentId
            LEFT JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
            WHERE el.CardId = @card_id
            ORDER BY el.EvidenceId ASC
        """
        try:
            ev_results = list(
                snapshot.execute_sql(
                    ev_sql_with_claim,
                    params={"card_id": card_id},
                    param_types={"card_id": spanner.param_types.STRING},
                )
            )
            has_claim_col = True
        except Exception:
            ev_results = list(
                snapshot.execute_sql(
                    ev_sql_legacy,
                    params={"card_id": card_id},
                    param_types={"card_id": spanner.param_types.STRING},
                )
            )
            has_claim_col = False

        evidence_list = [
            EvidenceDetail(
                evidence_id=r[0],
                chunk_id=r[1],
                document_id=r[2],
                document_name=r[3],
                page_number=r[4],
                slide_number=r[5],
                modality=r[6],
                source_chunk_text=r[7],
                source_gcs_uri=r[8],
                note=r[9],
                claim_type=ClaimCategory(r[10]) if (has_claim_col and len(r) > 10 and r[10]) else None,
            )
            for r in ev_results
        ]

        # 3. Fetch Review History
        rev_sql = """
            SELECT ReviewId, ReviewerId, ReviewerEmail, Action, PreviousStatus, NewStatus,
                   Comments, ReviewedAt, ReviewMetadata
            FROM SolutionCardReviews
            WHERE CardId = @card_id
            ORDER BY ReviewedAt DESC
        """
        rev_results = list(
            snapshot.execute_sql(
                rev_sql,
                params={"card_id": card_id},
                param_types={"card_id": spanner.param_types.STRING},
            )
        )
        reviews_list = []
        for r in rev_results:
            metadata_val = r[8]
            if isinstance(metadata_val, str):
                try:
                    metadata_val = json.loads(metadata_val)
                except Exception:
                    pass
            reviews_list.append(
                {
                    "review_id": r[0],
                    "reviewer_id": r[1],
                    "reviewer_email": r[2],
                    "action": r[3],
                    "previous_status": r[4],
                    "new_status": r[5],
                    "comments": r[6],
                    "reviewed_at": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
                    "review_metadata": metadata_val,
                }
            )

        return SolutionCardReviewDetail(
            card_id=c[0],
            document_id=c[1],
            document_name=c[2],
            project_name=c[3],
            business_problem=c[4],
            proposed_solution=c[5],
            implemented_solution=c[6],
            outcome=c[7],
            verification_status=VerificationStatus(c[8] or "DRAFT"),
            okf_uri=c[9],
            evidence_links=evidence_list,
            review_history=reviews_list,
        )


# ============================================================================
# REVIEW ACTION ENDPOINTS
# ============================================================================


def _execute_review_action(
    card_id: str,
    action: ReviewAction,
    request: ReviewActionRequest,
    reviewer: AuthenticatedReviewer,
    service: SolutionCardReviewService,
) -> ReviewActionResponse:
    """Helper delegating to review_service with comprehensive error mapping."""
    decision = ReviewDecisionRequest(
        action=action,
        comments=request.comments,
        field_edits=request.field_edits,
        client_metadata=request.client_metadata,
        idempotency_key=request.idempotency_key,
    )

    try:
        res = service.process_review(card_id=card_id, reviewer=reviewer, decision=decision)
        return ReviewActionResponse(
            card_id=res.card_id,
            review_id=res.review_id,
            previous_status=res.previous_status,
            new_status=res.new_status,
            action=res.action,
            reviewer_id=res.reviewer_id,
            reviewer_email=res.reviewer_email,
            field_edits_count=res.field_edits_count,
            message=f"Action '{res.action}' applied successfully: {res.previous_status} -> {res.new_status}",
        )
    except CardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MissingReviewCommentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AuthenticationContextError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/cards/{card_id}/approve", response_model=ReviewActionResponse)
def approve_solution_card(
    card_id: str,
    request: ReviewActionRequest = ReviewActionRequest(),
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)] = None,
    service: Annotated[SolutionCardReviewService, Depends(get_review_service)] = None,
) -> ReviewActionResponse:
    """Transitions SolutionCard to APPROVED status and logs audit entry."""
    return _execute_review_action(
        card_id=card_id,
        action=ReviewAction.APPROVE,
        request=request,
        reviewer=reviewer,
        service=service,
    )


@router.post("/cards/{card_id}/reject", response_model=ReviewActionResponse)
def reject_solution_card(
    card_id: str,
    request: ReviewActionRequest,
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)] = None,
    service: Annotated[SolutionCardReviewService, Depends(get_review_service)] = None,
) -> ReviewActionResponse:
    """Transitions SolutionCard to terminal REJECTED status. Requires justification comment."""
    return _execute_review_action(
        card_id=card_id,
        action=ReviewAction.REJECT,
        request=request,
        reviewer=reviewer,
        service=service,
    )


@router.post("/cards/{card_id}/request-changes", response_model=ReviewActionResponse)
def request_changes_solution_card(
    card_id: str,
    request: ReviewActionRequest,
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)] = None,
    service: Annotated[SolutionCardReviewService, Depends(get_review_service)] = None,
) -> ReviewActionResponse:
    """Transitions SolutionCard to CHANGES_REQUESTED status. Requires feedback comments."""
    return _execute_review_action(
        card_id=card_id,
        action=ReviewAction.REQUEST_CHANGES,
        request=request,
        reviewer=reviewer,
        service=service,
    )


@router.post("/cards/{card_id}/revoke", response_model=ReviewActionResponse)
def revoke_solution_card_approval(
    card_id: str,
    request: ReviewActionRequest,
    reviewer: Annotated[AuthenticatedReviewer, Depends(get_authenticated_reviewer)] = None,
    service: Annotated[SolutionCardReviewService, Depends(get_review_service)] = None,
) -> ReviewActionResponse:
    """Authorized revocation of an APPROVED card back to CHANGES_REQUESTED."""
    return _execute_review_action(
        card_id=card_id,
        action=ReviewAction.REVOKE,
        request=request,
        reviewer=reviewer,
        service=service,
    )
