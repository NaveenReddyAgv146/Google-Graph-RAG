"""Pydantic data models for Phase 4.3 Grounded Synthesis and Citation Validation.

Enforces structured claim-level citation models, strict provenance metadata,
and fail-closed validation contracts for Discovery Copilot.
"""

from __future__ import annotations

from enum import Enum
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    """Classification of grounded claims."""

    VERIFIED_FACT = "VERIFIED_FACT"
    ARCHITECTURAL_INFERENCE = "ARCHITECTURAL_INFERENCE"


class EvidenceReference(BaseModel):
    """Pointer linking a claim to exact authoritative source evidence."""

    card_id: str = Field(..., description="Authoritative SolutionCard ID (card_xxx)")
    chunk_id: str = Field(..., description="Authoritative EvidenceChunk ID (chunk-xxx)")
    document_id: str = Field(..., description="Owning Document ID")
    claim_type: str | None = Field(default=None, description="Semantic claim category: BUSINESS_PROBLEM, PROPOSED_SOLUTION, IMPLEMENTED_SOLUTION, OUTCOME")
    customer_name: str | None = Field(default=None, description="Customer / project name (e.g. Uber, Canva)")
    document_name: str | None = Field(default=None, description="Source document file name")
    page_number: int | None = Field(default=None, description="1-based page number if PDF")
    slide_number: int | None = Field(default=None, description="1-based slide number if presentation")
    exact_source_quote: str = Field(
        ...,
        min_length=3,
        description="Verbatim text quote from source chunk supporting the claim",
    )


class GroundedClaim(BaseModel):
    """Discrete factual claim or architectural deduction with explicit grounding."""

    claim_id: str = Field(
        default_factory=lambda: f"claim_{uuid.uuid4().hex[:12]}",
        description="Unique identifier for the claim",
    )
    statement: str = Field(..., min_length=5, description="Clear, discrete assertion statement")
    claim_type: ClaimType = Field(..., description="Fact or architectural inference")
    customer_name: str | None = Field(default=None, description="Target customer/company name")
    card_id: str | None = Field(default=None, description="Primary SolutionCard ID")
    document_id: str | None = Field(default=None, description="Primary Document ID")
    evidence_refs: list[EvidenceReference] = Field(
        default_factory=list,
        description="Authoritative evidence references supporting this claim",
    )
    supporting_fact_statements: list[str] = Field(
        default_factory=list,
        description="Statements of verified facts that this inference relies upon (required for inferences)",
    )


class AuthoritativeEvidenceChunk(BaseModel):
    """Immutable evidence chunk loaded from verified Spanner/GCS storage."""

    chunk_id: str
    document_id: str
    card_id: str
    claim_type: str | None = None
    document_name: str | None = None
    customer_name: str | None = None
    page_number: int | None = None
    slide_number: int | None = None
    modality: str = "TEXT"
    text_content: str
    note: str | None = None


class AuthoritativeCardEvidence(BaseModel):
    """Aggregated verified SolutionCard containing verified citations."""

    card_id: str
    document_id: str
    project_name: str
    customer_name: str
    document_name: str | None = None
    business_problem: str | None = None
    proposed_solution: str | None = None
    implemented_solution: str | None = None
    outcome: str | None = None
    verification_status: str = "APPROVED"
    okf_uri: str | None = None
    technologies: list[str] = Field(default_factory=list)
    evidence_chunks: list[AuthoritativeEvidenceChunk] = Field(default_factory=list)


class EvidencePack(BaseModel):
    """Container holding only authenticated, RBAC-authorized, APPROVED evidence for synthesis."""

    pack_id: str = Field(default_factory=lambda: f"pack_{uuid.uuid4().hex[:12]}")
    query: str
    intent: str
    cards: list[AuthoritativeCardEvidence] = Field(default_factory=list)
    chunks_by_id: dict[str, AuthoritativeEvidenceChunk] = Field(default_factory=dict)
    total_cards: int = 0
    total_chunks: int = 0


class SynthesisResult(BaseModel):
    """Raw structured model synthesis prior to citation validation."""

    summary: str = Field(..., description="Comprehensive answer synthesized from verified claims")
    claims: list[GroundedClaim] = Field(
        default_factory=list,
        description="List of discrete claims extracted during synthesis",
    )
    insufficient_evidence_notes: list[str] = Field(
        default_factory=list,
        description="Explicit notes identifying user questions or aspects not present in evidence",
    )
    unsupported_aspects: list[str] = Field(
        default_factory=list,
        description="Questions or constraints requested by user that have no approved data",
    )
    latency_ms: float | None = Field(default=None, description="Synthesis generation latency in milliseconds")


class ClaimValidationStatus(str, Enum):
    """Validation verdict for a single GroundedClaim."""

    VALID = "VALID"
    INVALID_CARD_ID = "INVALID_CARD_ID"
    INVALID_CHUNK_ID = "INVALID_CHUNK_ID"
    DOCUMENT_MISMATCH = "DOCUMENT_MISMATCH"
    CUSTOMER_MISATTRIBUTION = "CUSTOMER_MISATTRIBUTION"
    QUOTE_MISMATCH = "QUOTE_MISMATCH"
    MISSING_CITATION = "MISSING_CITATION"
    UNSUPPORTED_INFERENCE = "UNSUPPORTED_INFERENCE"
    CLAIM_TYPE_MISMATCH = "CLAIM_TYPE_MISMATCH"


class ClaimValidationDetail(BaseModel):
    """Evaluation result for an individual claim."""

    claim_id: str
    statement: str
    is_valid: bool
    status: ClaimValidationStatus
    reason: str | None = None
    evidence_ref_count: int = 0


class ValidatedSynthesisResponse(BaseModel):
    """Final verified response delivered to the user or caller."""

    is_valid: bool = Field(..., description="True if response meets 100% precision and fidelity criteria")
    summary: str = Field(..., description="Final answer text built exclusively from validated claims")
    validated_claims: list[GroundedClaim] = Field(default_factory=list)
    rejected_claims: list[ClaimValidationDetail] = Field(default_factory=list)
    insufficient_evidence_notes: list[str] = Field(default_factory=list)
    unsupported_aspects: list[str] = Field(default_factory=list)
    validation_details: list[ClaimValidationDetail] = Field(default_factory=list)
    precision: float = Field(default=1.0, description="Proportion of claims that passed validation (0.0 to 1.0)")
    quote_fidelity: float = Field(default=1.0, description="Proportion of quotes matching verbatim (0.0 to 1.0)")
    misattribution_count: int = Field(default=0, description="Count of detected cross-customer misattributions")
    latency_ms: float | None = Field(default=None, description="Total pipeline latency in milliseconds")

