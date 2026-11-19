"""Domain models for Human Review API and CLI in Phase 3D."""

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field

from common.models.schema import (
    ClaimCategory,
    FieldEdit,
    ReviewAction,
    VerificationStatus,
)


class EvidenceDetail(BaseModel):
    """Detailed evidence chunk and citation grounding for a SolutionCard."""

    evidence_id: str
    chunk_id: str | None
    document_id: str
    claim_type: ClaimCategory | None = None
    document_name: str | None = None
    page_number: int | None = None
    slide_number: int | None = None
    modality: str | None = None
    source_chunk_text: str | None = None
    source_gcs_uri: str | None = None
    note: str | None = None


class SolutionCardSummary(BaseModel):
    """Card summary item for list review queue."""

    card_id: str
    document_id: str
    document_name: str | None = None
    project_name: str | None = None
    verification_status: VerificationStatus
    evidence_count: int = 0
    okf_uri: str | None = None


class SolutionCardReviewDetail(BaseModel):
    """Complete SolutionCard payload with full evidence and audit history."""

    card_id: str
    document_id: str
    document_name: str | None = None
    project_name: str | None = None
    business_problem: str | None = None
    proposed_solution: str | None = None
    implemented_solution: str | None = None
    outcome: str | None = None
    verification_status: VerificationStatus
    okf_uri: str | None = None
    evidence_links: list[EvidenceDetail] = Field(default_factory=list)
    review_history: list[dict[str, Any]] = Field(default_factory=list)


class ReviewActionRequest(BaseModel):
    """Payload for submitting a review decision on a SolutionCard.

    Reviewer identity is strictly omitted; it is derived from verified authentication.
    """

    comments: str | None = Field(
        None,
        description="Justification comments. Mandatory for REJECT and REQUEST_CHANGES.",
    )
    field_edits: list[FieldEdit] = Field(
        default_factory=list,
        description="Optional human corrections to card fields (e.g. project_name, business_problem).",
    )
    client_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Client audit attributes (e.g. client_version). Sensitive keys are sanitized.",
    )
    idempotency_key: str | None = Field(
        None,
        max_length=64,
        description="Optional client-supplied idempotency key used as ReviewId.",
    )


class ReviewActionResponse(BaseModel):
    """Response returned upon completing a review action."""

    card_id: str
    review_id: str
    previous_status: str
    new_status: str
    action: str
    reviewer_id: str
    reviewer_email: str
    field_edits_count: int
    message: str

