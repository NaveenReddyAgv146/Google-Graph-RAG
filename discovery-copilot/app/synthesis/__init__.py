"""Phase 4.3 Synthesis, Adaptive MMR & Citation Validation Package."""

from app.synthesis.citation_validator import CitationValidator
from app.synthesis.evidence_selector import EvidenceSelector
from app.synthesis.models import (
    AuthoritativeCardEvidence,
    AuthoritativeEvidenceChunk,
    ClaimType,
    ClaimValidationDetail,
    ClaimValidationStatus,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
    ValidatedSynthesisResponse,
)
from app.synthesis.synthesizer import GroundedAnswerSynthesizer

__all__ = [
    "AuthoritativeCardEvidence",
    "AuthoritativeEvidenceChunk",
    "CitationValidator",
    "ClaimType",
    "ClaimValidationDetail",
    "ClaimValidationStatus",
    "EvidencePack",
    "EvidenceReference",
    "EvidenceSelector",
    "GroundedAnswerSynthesizer",
    "GroundedClaim",
    "SynthesisResult",
    "ValidatedSynthesisResponse",
]

