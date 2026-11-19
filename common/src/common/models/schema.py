"""Pydantic definitions for all 8 Node tables and 11 Edge tables in EnterpriseKnowledgeGraph, plus Review and Audit domain models."""

from datetime import datetime
from enum import Enum
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# REVIEW DOMAIN ENUMS & VALIDATORS
# ============================================================================


class VerificationStatus(str, Enum):
    """Permitted verification statuses for SolutionCard in EnterpriseKnowledgeGraph."""

    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ReviewAction(str, Enum):
    """Permitted human review actions."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REVOKE = "REVOKE"


class ClaimCategory(str, Enum):
    """Permitted claim categories for claim-level EvidenceLinks."""

    BUSINESS_PROBLEM = "BUSINESS_PROBLEM"
    PROPOSED_SOLUTION = "PROPOSED_SOLUTION"
    IMPLEMENTED_SOLUTION = "IMPLEMENTED_SOLUTION"
    OUTCOME = "OUTCOME"


# Regular expression for RFC 5322 simplified email validation (eliminates external email_validator dependency)
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


# ============================================================================
# NODE MODELS
# ============================================================================


class DocumentNode(BaseModel):
    """Document node in EnterpriseKnowledgeGraph (PDFs, Slide decks, Repositories)."""

    document_id: str = Field(..., max_length=256, description="Document identifier")
    file_name: str = Field(..., max_length=256)
    mime_type: str = Field(..., max_length=128)
    source_url: str | None = Field(None, max_length=1024)
    gcs_uri: str | None = Field(None, max_length=1024)
    status: str | None = Field(None, max_length=32)
    repo_id: str | None = Field(None, max_length=64)
    drive_file_id: str | None = Field(None, max_length=256)


class ChunkNode(BaseModel):
    """Evidence chunk node (text, code, tables) with vector embedding and provenance."""

    chunk_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    file_id: str | None = Field(None, max_length=64)
    modality: str = Field(default="TEXT", max_length=32)
    text_content: str
    page_number: int | None = None
    slide_number: int | None = None
    commit_sha: str | None = Field(None, max_length=64)
    line_range: str | None = Field(None, max_length=64)
    source_gcs_uri: str | None = Field(None, max_length=1024)
    embedding_768: list[float] | None = None


class SolutionCardNode(BaseModel):
    """Business solution card representing a customer architecture or project.

    Preserves legacy DRAFT statuses. Does not use a default status that would
    misrepresent existing or legacy records without an explicit status.
    """

    card_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    project_name: str = Field(..., max_length=256)
    business_problem: str | None = None
    proposed_solution: str | None = None
    implemented_solution: str | None = None
    outcome: str | None = None
    verification_status: VerificationStatus | None = Field(
        None,
        description="Current verification status. None/DRAFT for unverified/legacy cards, PENDING for extracted, APPROVED/REJECTED for audited.",
    )
    okf_uri: str | None = Field(None, max_length=1024)


class VisualAssetNode(BaseModel):
    """Visual asset (slide render, diagram, or figure)."""

    visual_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    page_number: int | None = None
    slide_number: int | None = None
    image_gcs_uri: str = Field(..., max_length=1024)
    description: str | None = None
    asset_type: str | None = Field("SLIDE", max_length=32)


class RepositoryNode(BaseModel):
    """GitHub repository node."""

    repo_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    url: str = Field(..., max_length=1024)
    default_branch: str | None = Field("main", max_length=64)
    last_synced_commit: str | None = Field(None, max_length=64)
    created_at: datetime | None = None


class SourceFileNode(BaseModel):
    """Source file within a repository."""

    file_id: str = Field(..., max_length=64)
    repo_id: str = Field(..., max_length=64)
    file_path: str = Field(..., max_length=1024)
    language: str | None = Field(None, max_length=64)
    commit_sha: str = Field(..., max_length=64)
    created_at: datetime | None = None


class EntityNode(BaseModel):
    """Discovered business entity, concept, subsystem, or API."""

    entity_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    category: str = Field(..., max_length=64)
    description: str | None = None


class TechnologyNode(BaseModel):
    """Approved enterprise technology catalog entry."""

    tech_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    category: str = Field(..., max_length=64)
    canonical_version: str | None = Field(None, max_length=64)


class EvidenceLinkNode(BaseModel):
    """Grounded evidence link connecting a SolutionCard to an EvidenceChunk."""

    evidence_id: str = Field(..., max_length=64)
    card_id: str = Field(..., max_length=64)
    chunk_id: str | None = Field(None, max_length=64)
    document_id: str = Field(..., max_length=256)
    claim_type: ClaimCategory | None = Field(
        default=None,
        description="The specific card claim grounded by this citation (None for legacy links).",
    )
    page_number: int | None = None
    slide_number: int | None = None
    note: str | None = None


# ============================================================================
# AUDIT & REVIEW DOMAIN MODELS
# ============================================================================


class FieldEdit(BaseModel):
    """Auditable field-level human modification."""

    field_name: str
    old_value: Any
    new_value: Any


class ReviewMetadataPayload(BaseModel):
    """Structured audit metadata stored in ReviewMetadata JSON column."""

    field_edits: list[FieldEdit] = Field(default_factory=list)
    reason: str | None = None
    client_info: dict[str, Any] = Field(default_factory=dict)


class SolutionCardReviewNode(BaseModel):
    """Immutable audit record for a human review action in SolutionCardReviews."""

    card_id: str = Field(..., max_length=64)
    review_id: str = Field(..., max_length=64)
    reviewer_id: str = Field(
        ...,
        max_length=128,
        description="Authenticated principal ID (e.g., subject/sub claim from verified token).",
    )
    reviewer_email: str = Field(
        ...,
        max_length=256,
        description="Authenticated principal email address.",
    )
    action: ReviewAction
    previous_status: VerificationStatus
    new_status: VerificationStatus
    comments: str | None = None
    reviewed_at: datetime | None = None
    review_metadata: ReviewMetadataPayload | None = None

    @field_validator("reviewer_email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        if not EMAIL_REGEX.match(v):
            raise ValueError(f"Invalid email address format: '{v}'")
        return v.lower()


# ============================================================================
# EDGE MODELS
# ============================================================================


class DocumentContainsChunkEdge(BaseModel):
    document_id: str
    chunk_id: str


class RepoContainsFileEdge(BaseModel):
    repo_id: str
    file_id: str


class SourceFileContainsChunkEdge(BaseModel):
    file_id: str
    chunk_id: str


class ChunkReferencesEntityEdge(BaseModel):
    chunk_id: str
    entity_id: str


class EntityReferencesEntityEdge(BaseModel):
    source_entity_id: str
    target_entity_id: str


class ChunkMentionsTechEdge(BaseModel):
    chunk_id: str
    tech_id: str


class VisualAssetBelongsToDocEdge(BaseModel):
    visual_id: str
    document_id: str


class SolutionSupportsTechEdge(BaseModel):
    card_id: str
    tech_id: str


class SourceFileDefinesEntityEdge(BaseModel):
    file_id: str
    entity_id: str


class SourceFileCallsFileEdge(BaseModel):
    caller_file_id: str
    callee_file_id: str


class SolutionImplementsDocEdge(BaseModel):
    card_id: str
    document_id: str

