"""Unit tests for Phase 4.3 CitationValidator."""

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


@pytest.fixture
def sample_evidence_pack():
    """Constructs a verified two-customer EvidencePack (Uber and Canva)."""
    uber_chunk_1 = AuthoritativeEvidenceChunk(
        chunk_id="chunk_uber_1",
        document_id="doc_uber",
        card_id="card_uber",
        customer_name="Uber",
        page_number=2,
        text_content="Most use aging & inefficient technology. Radio dispatch, no 2-way communication.",
    )
    uber_chunk_2 = AuthoritativeEvidenceChunk(
        chunk_id="chunk_uber_2",
        document_id="doc_uber",
        card_id="card_uber",
        customer_name="Uber",
        page_number=8,
        text_content="1-Click request from Geo-aware devices. SMS from any phone: pickup @work in 5.",
    )
    canva_chunk_1 = AuthoritativeEvidenceChunk(
        chunk_id="chunk_canva_1",
        document_id="doc_canva",
        card_id="card_canva",
        customer_name="Canva",
        slide_number=5,
        text_content="Export cluster scaling to 500 nodes on Kubernetes with Document AI parsing.",
    )

    card_uber = AuthoritativeCardEvidence(
        card_id="card_uber",
        document_id="doc_uber",
        project_name="UberCab Dispatch Platform",
        customer_name="Uber",
        evidence_chunks=[uber_chunk_1, uber_chunk_2],
    )
    card_canva = AuthoritativeCardEvidence(
        card_id="card_canva",
        document_id="doc_canva",
        project_name="Canva Media Service",
        customer_name="Canva",
        evidence_chunks=[canva_chunk_1],
    )

    pack = EvidencePack(
        query="Compare Uber and Canva architectures",
        intent="COMPARISON",
        cards=[card_uber, card_canva],
        chunks_by_id={
            "chunk_uber_1": uber_chunk_1,
            "chunk_uber_2": uber_chunk_2,
            "chunk_canva_1": canva_chunk_1,
        },
        total_cards=2,
        total_chunks=3,
    )
    return pack


def test_valid_claims_pass_with_100_percent_precision(sample_evidence_pack):
    """Verify strictly compliant claims achieve 100% precision and quote fidelity."""
    validator = CitationValidator()

    claim_1 = GroundedClaim(
        statement="Uber implemented 1-Click requests from geo-aware devices.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="Uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_2",
                document_id="doc_uber",
                customer_name="Uber",
                page_number=8,
                exact_source_quote="1-Click request from Geo-aware devices",
            )
        ],
    )
    claim_2 = GroundedClaim(
        statement="Canva scaled worker export clusters on Kubernetes.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="Canva",
        evidence_refs=[
            EvidenceReference(
                card_id="card_canva",
                chunk_id="chunk_canva_1",
                document_id="doc_canva",
                customer_name="Canva",
                slide_number=5,
                exact_source_quote="Kubernetes with Document AI parsing",
            )
        ],
    )

    synthesis = SynthesisResult(
        summary="Uber built geo-aware dispatch and Canva deployed Kubernetes pipelines.",
        claims=[claim_1, claim_2],
    )

    res = validator.validate_synthesis(synthesis, sample_evidence_pack)
    assert res.is_valid is True
    assert res.precision == 1.0
    assert res.quote_fidelity == 1.0
    assert len(res.validated_claims) == 2
    assert len(res.rejected_claims) == 0


def test_fabricated_card_id_rejected(sample_evidence_pack):
    """Verify claim with non-existent card_id is rejected."""
    validator = CitationValidator()

    claim = GroundedClaim(
        statement="Uber built mobile apps.",
        claim_type=ClaimType.VERIFIED_FACT,
        evidence_refs=[
            EvidenceReference(
                card_id="card_fabricated_999",  # Does not exist!
                chunk_id="chunk_uber_1",
                document_id="doc_uber",
                exact_source_quote="Radio dispatch, no 2-way communication",
            )
        ],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is False
    assert res.precision == 0.0
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.INVALID_CARD_ID


def test_fabricated_chunk_id_rejected(sample_evidence_pack):
    """Verify claim with non-existent chunk_id is rejected."""
    validator = CitationValidator()

    claim = GroundedClaim(
        statement="Uber built mobile apps.",
        claim_type=ClaimType.VERIFIED_FACT,
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_fabricated_xyz",  # Does not exist!
                document_id="doc_uber",
                exact_source_quote="Radio dispatch",
            )
        ],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is False
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.INVALID_CHUNK_ID


def test_cross_customer_misattribution_rejected(sample_evidence_pack):
    """Verify Canva claim cited against Uber evidence is flagged as CUSTOMER_MISATTRIBUTION."""
    validator = CitationValidator()

    # Model asserts that CANVA used 1-Click geo dispatch, citing Uber's chunk!
    claim = GroundedClaim(
        statement="Canva implemented 1-Click geo-aware dispatching.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="Canva",  # Attributed to Canva!
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_2",
                document_id="doc_uber",
                customer_name="Canva",  # Contaminated attribution!
                exact_source_quote="1-Click request from Geo-aware devices",
            )
        ],
    )

    synthesis = SynthesisResult(summary="Canva dispatch", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is False
    assert res.misattribution_count == 1
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.CUSTOMER_MISATTRIBUTION
    assert "Customer attribution mismatch" in res.rejected_claims[0].reason


def test_modified_quote_mismatch_rejected(sample_evidence_pack):
    """Verify quote with altered or hallucinated wording fails verbatim check."""
    validator = CitationValidator()

    claim = GroundedClaim(
        statement="Uber eliminated all traditional radio dispatch.",
        claim_type=ClaimType.VERIFIED_FACT,
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_1",
                document_id="doc_uber",
                # The real text is "Radio dispatch, no 2-way communication"
                # Hallucinated replacement:
                exact_source_quote="Completely eliminated aging radio dispatch systems",
            )
        ],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is False
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.QUOTE_MISMATCH


def test_whitespace_and_case_normalization_passes_valid_quote(sample_evidence_pack):
    """Verify quotes with extra spaces or linebreaks pass canonical normalization."""
    validator = CitationValidator()

    # Quote has multiple internal spaces, newlines, and mixed casing
    raw_quote = "  Radio   dispatch, \n\t  no 2-way   COMMUNICATION  "

    claim = GroundedClaim(
        statement="Uber noted that traditional cabs rely on radio dispatch.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="Uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_1",
                document_id="doc_uber",
                exact_source_quote=raw_quote,
            )
        ],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is True
    assert len(res.validated_claims) == 1


def test_multiple_citations_per_claim_all_must_pass(sample_evidence_pack):
    """Verify claim with 2 citations passes only when both quotes match."""
    validator = CitationValidator()

    # One good citation, one bad citation
    claim = GroundedClaim(
        statement="Uber replaced radio dispatch with geo-aware mobile apps.",
        claim_type=ClaimType.VERIFIED_FACT,
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_1",
                document_id="doc_uber",
                exact_source_quote="Radio dispatch, no 2-way communication",  # Valid!
            ),
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_2",
                document_id="doc_uber",
                exact_source_quote="Fabricated nonexistent quote",  # Invalid!
            ),
        ],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is False
    assert len(res.rejected_claims) == 1
    assert res.rejected_claims[0].status == ClaimValidationStatus.QUOTE_MISMATCH


def test_unsupported_numerical_metric_rejected(sample_evidence_pack):
    """Verify claim asserting numerical metrics not supported by the quote is rejected."""
    validator = CitationValidator()

    # Statement claims "99.999% SLA", but quote only says "1-Click request from Geo-aware devices"
    claim = GroundedClaim(
        statement="Uber guaranteed a 99.999% SLA across all dispatch services.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="Uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_2",
                document_id="doc_uber",
                exact_source_quote="1-Click request from Geo-aware devices",
            )
        ],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[claim])
    res = validator.validate_synthesis(synthesis, sample_evidence_pack)

    assert res.is_valid is False
    assert len(res.rejected_claims) == 1
    assert "Numeric metric(s)" in res.rejected_claims[0].reason


def test_inference_requires_supporting_facts():
    """Verify ARCHITECTURAL_INFERENCE without supporting facts is rejected."""
    validator = CitationValidator()
    empty_pack = EvidencePack(query="q", intent="SOLUTION_DISCOVERY")

    inference = GroundedClaim(
        statement="Modern geo-distributed messaging reduces taxi dead-time.",
        claim_type=ClaimType.ARCHITECTURAL_INFERENCE,
        supporting_fact_statements=[],  # Empty!
        evidence_refs=[],
    )

    synthesis = SynthesisResult(summary="Summary", claims=[inference])
    res = validator.validate_synthesis(synthesis, empty_pack)

    assert res.is_valid is False
    assert res.rejected_claims[0].status == ClaimValidationStatus.UNSUPPORTED_INFERENCE


def test_summary_rebuilt_cleanly_when_invalid_claims_purged(sample_evidence_pack):
    """Verify summary is rebuilt exclusively from validated claims if an ungrounded claim was purged."""
    validator = CitationValidator()

    valid_claim = GroundedClaim(
        statement="Uber enabled 1-Click requests from geo devices.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name="Uber",
        evidence_refs=[
            EvidenceReference(
                card_id="card_uber",
                chunk_id="chunk_uber_2",
                document_id="doc_uber",
                exact_source_quote="1-Click request from Geo-aware devices",
            )
        ],
    )
    invalid_claim = GroundedClaim(
        statement="Canva used quantum database clusters.",
        claim_type=ClaimType.VERIFIED_FACT,
        evidence_refs=[
            EvidenceReference(
                card_id="card_canva",
                chunk_id="chunk_canva_1",
                document_id="doc_canva",
                exact_source_quote="Nonexistent quantum quote",
            )
        ],
    )

    synthesis = SynthesisResult(
        summary="Original dirty summary containing quantum database claim.",
        claims=[valid_claim, invalid_claim],
    )

    res = validator.validate_synthesis(synthesis, sample_evidence_pack)
    assert len(res.validated_claims) == 1
    assert len(res.rejected_claims) == 1

    # Final summary should have removed the ungrounded quantum claim and rebuilt from valid claims
    assert "quantum" not in res.summary.lower()
    assert "Uber enabled 1-Click requests" in res.summary

