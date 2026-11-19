"""Tests for Semantic Claim Compatibility and Evidence Integrity in Discovery Copilot.

Covers:
- Propagation of claim_type into GroundedCitation, AuthoritativeEvidenceChunk, and EvidenceReference.
- Rejection of ClaimType mismatch between reference and authoritative chunk.
- Enforcement that proposed architecture cannot be asserted as implemented solution.
- Enforcement that projected financial outcomes cannot be asserted as verified historical results.
- Successful verification of properly qualified projection statements.
"""

import pytest

from app.synthesis.citation_validator import CitationValidator
from app.synthesis.models import (
    AuthoritativeCardEvidence,
    AuthoritativeEvidenceChunk,
    ClaimType,
    ClaimValidationStatus,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
)


def _build_test_evidence_pack() -> EvidencePack:
    card = AuthoritativeCardEvidence(
        card_id="card_uber",
        document_id="doc_uber",
        project_name="UberCab 2008",
        customer_name="UberCab",
        business_problem="Traditional cabs use aging technology.",
        proposed_solution="NetJets of car services.",
        implemented_solution="Foundational filings in 2008.",
        outcome="Potential $1B yearly revenue in best-case scenario.",
        verification_status="APPROVED",
    )

    chunk_prob = AuthoritativeEvidenceChunk(
        chunk_id="chunk_prob",
        document_id="doc_uber",
        card_id="card_uber",
        claim_type="BUSINESS_PROBLEM",
        customer_name="UberCab",
        page_number=2,
        text_content="Most use aging & inefficient technology — Radio dispatch, no 2-way communication",
    )

    chunk_prop = AuthoritativeEvidenceChunk(
        chunk_id="chunk_prop",
        document_id="doc_uber",
        card_id="card_uber",
        claim_type="PROPOSED_SOLUTION",
        customer_name="UberCab",
        page_number=4,
        text_content="UberCab Concept: A fast & efficient on-demand car service. Automated dispatch and mobile app.",
    )

    chunk_impl = AuthoritativeEvidenceChunk(
        chunk_id="chunk_impl",
        document_id="doc_uber",
        card_id="card_uber",
        claim_type="IMPLEMENTED_SOLUTION",
        customer_name="UberCab",
        page_number=25,
        text_content="Progress to Date: California LLC + trademark filed; Provisional patent filed; Iphone dev license applied for.",
    )

    chunk_outcome = AuthoritativeEvidenceChunk(
        chunk_id="chunk_outcome",
        document_id="doc_uber",
        card_id="card_uber",
        claim_type="OUTCOME",
        customer_name="UberCab",
        page_number=20,
        text_content="Potential Outcomes: Best-Case Scenario — Becomes market leader, $1B+ in yearly revenue; Realistic Success Scenario — Gets 5% of top 5 US Cities.",
    )

    chunks = {
        "chunk_prob": chunk_prob,
        "chunk_prop": chunk_prop,
        "chunk_impl": chunk_impl,
        "chunk_outcome": chunk_outcome,
    }

    return EvidencePack(
        query="Tell me about Uber",
        intent="ARCHITECTURAL_DISCOVERY",
        cards=[card],
        chunks_by_id=chunks,
        total_cards=1,
        total_chunks=len(chunks),
    )


def test_claim_type_mismatch_rejected():
    """Verify citation validator rejects reference when requested claim_type differs from chunk tag."""
    pack = _build_test_evidence_pack()
    validator = CitationValidator()

    # Reference claims it is citing an IMPLEMENTED_SOLUTION, but chunk_prob is BUSINESS_PROBLEM
    claim = GroundedClaim(
        statement="Traditional dispatch causes significant dead time.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="UberCab",
        card_id="card_uber",
        document_id="doc_uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_prob",
                document_id="doc_uber",
                claim_type="IMPLEMENTED_SOLUTION",  # Mismatch: chunk is BUSINESS_PROBLEM
                customer_name="UberCab",
                exact_source_quote="Radio dispatch, no 2-way communication",
            )
        ],
    )

    res = validator.validate_synthesis(SynthesisResult(summary="Test", claims=[claim]), pack)
    assert not res.is_valid
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.CLAIM_TYPE_MISMATCH
    assert "ClaimType mismatch" in res.rejected_claims[0].reason


def test_proposed_architecture_cannot_be_asserted_as_implemented():
    """Verify validator strictly rejects claiming that proposed architecture is implemented."""
    pack = _build_test_evidence_pack()
    validator = CitationValidator()

    # Statement asserts that mobile apps are implemented and in production
    claim = GroundedClaim(
        statement="UberCab implemented an on-demand car service with automated dispatch in production.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="UberCab",
        card_id="card_uber",
        document_id="doc_uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_prop",  # Chunk is tagged PROPOSED_SOLUTION
                document_id="doc_uber",
                customer_name="UberCab",
                exact_source_quote="UberCab Concept: A fast & efficient on-demand car service.",
            )
        ],
    )

    res = validator.validate_synthesis(SynthesisResult(summary="Test", claims=[claim]), pack)
    assert not res.is_valid
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.CLAIM_TYPE_MISMATCH
    assert "Proposed architecture cannot be presented as implemented solution" in res.rejected_claims[0].reason


def test_projected_financial_outcomes_cannot_be_asserted_as_actual_results():
    """Verify validator strictly rejects claiming projected revenue scenarios as realized historical facts."""
    pack = _build_test_evidence_pack()
    validator = CitationValidator()

    # Statement asserts that Uber generated $1B in revenue as realized fact without qualification
    claim = GroundedClaim(
        statement="Uber generated $1B+ in yearly revenue.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="UberCab",
        card_id="card_uber",
        document_id="doc_uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_outcome",  # Chunk is tagged OUTCOME and says "Potential Outcomes: Best-Case Scenario"
                document_id="doc_uber",
                customer_name="UberCab",
                exact_source_quote="Best-Case Scenario — Becomes market leader, $1B+ in yearly revenue",
            )
        ],
    )

    res = validator.validate_synthesis(SynthesisResult(summary="Test", claims=[claim]), pack)
    assert not res.is_valid
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.CLAIM_TYPE_MISMATCH
    assert "Financial projections cannot be presented as verified historical outcomes" in res.rejected_claims[0].reason


def test_qualified_projection_statement_passes_validation():
    """Verify validator passes projection claims that explicitly preserve modal qualification."""
    pack = _build_test_evidence_pack()
    validator = CitationValidator()

    # Statement accurately qualifies that this was a projected scenario
    claim = GroundedClaim(
        statement="Uber's best-case scenario projected becoming market leader with $1B+ in yearly revenue.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="UberCab",
        card_id="card_uber",
        document_id="doc_uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_outcome",
                document_id="doc_uber",
                claim_type="OUTCOME",
                customer_name="UberCab",
                exact_source_quote="Best-Case Scenario — Becomes market leader, $1B+ in yearly revenue",
            )
        ],
    )

    res = validator.validate_synthesis(SynthesisResult(summary="Test", claims=[claim]), pack)
    assert res.is_valid
    assert len(res.validated_claims) == 1
    assert len(res.rejected_claims) == 0


def test_verified_implementation_milestone_passes_validation():
    """Verify validator passes actual implemented progress to date cited from Page 25."""
    pack = _build_test_evidence_pack()
    validator = CitationValidator()

    claim = GroundedClaim(
        statement="Foundational milestones implemented included filing California LLC and provisional patent.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="UberCab",
        card_id="card_uber",
        document_id="doc_uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_impl",
                document_id="doc_uber",
                claim_type="IMPLEMENTED_SOLUTION",
                customer_name="UberCab",
                exact_source_quote="Progress to Date: California LLC + trademark filed; Provisional patent filed",
            )
        ],
    )

    res = validator.validate_synthesis(SynthesisResult(summary="Test", claims=[claim]), pack)
    assert res.is_valid
    assert len(res.validated_claims) == 1
    assert len(res.rejected_claims) == 0
