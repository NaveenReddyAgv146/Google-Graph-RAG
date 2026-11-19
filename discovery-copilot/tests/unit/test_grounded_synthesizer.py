"""Unit tests for Phase 4.3 GroundedAnswerSynthesizer."""

import json
from unittest.mock import MagicMock
import pytest

from common.config import settings
from app.synthesis.models import (
    AuthoritativeCardEvidence,
    AuthoritativeEvidenceChunk,
    ClaimType,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
)
from app.synthesis.synthesizer import GroundedAnswerSynthesizer


@pytest.fixture
def populated_evidence_pack():
    """Builds a sample EvidencePack with an Uber solution card."""
    chunk = AuthoritativeEvidenceChunk(
        chunk_id="chunk_u1",
        document_id="doc_uber",
        card_id="card_uber",
        customer_name="Uber",
        page_number=20,
        text_content="Realistic Success Scenario gets 5% of top 5 US Cities, generates 20-30M+ per year profit.",
    )
    card = AuthoritativeCardEvidence(
        card_id="card_uber",
        document_id="doc_uber",
        project_name="UberCab Expansion",
        customer_name="Uber",
        implemented_solution="SMS and mobile 1-Click dispatch",
        outcome="Projected 20-30M profit in realistic scenario",
        evidence_chunks=[chunk],
    )
    return EvidencePack(
        query="What revenue does Uber generate and what is their RTO disaster recovery SLA?",
        intent="SOLUTION_DISCOVERY",
        cards=[card],
        chunks_by_id={"chunk_u1": chunk},
        total_cards=1,
        total_chunks=1,
    )


def test_synthesizer_uses_centralized_reasoning_model():
    """Verify synthesizer defaults to settings.reasoning_model."""
    synth = GroundedAnswerSynthesizer()
    assert synth._model_name == settings.reasoning_model


def test_empty_evidence_pack_returns_zero_hallucination_result():
    """Verify empty EvidencePack immediately returns insufficient evidence response."""
    synth = GroundedAnswerSynthesizer()
    empty_pack = EvidencePack(query="What is the latency?", intent="TECHNICAL_LOOKUP")

    res = synth.synthesize("What is the latency?", empty_pack)

    assert len(res.claims) == 0
    assert len(res.insufficient_evidence_notes) >= 1
    assert "No human-approved solutions" in res.summary
    assert res.latency_ms is not None


def test_missing_rto_attribute_reported_as_insufficient_evidence(populated_evidence_pack):
    """Verify query asking for missing attributes (RTO/RPO) flags insufficient evidence."""
    # Custom mock generator simulating Gemini adhering to the zero-hallucination prompt
    def mock_gemini_generator(query: str, pack: EvidencePack) -> SynthesisResult:
        claim = GroundedClaim(
            statement="Uber projected generating $20-30M+ per year profit in a realistic success scenario.",
            claim_type=ClaimType.VERIFIED_FACT,
            customer_name="Uber",
            card_id="card_uber",
            document_id="doc_uber",
            evidence_refs=[
                EvidenceReference(
                    card_id="card_uber",
                    chunk_id="chunk_u1",
                    document_id="doc_uber",
                    page_number=20,
                    exact_source_quote="generates 20-30M+ per year profit",
                )
            ],
        )
        return SynthesisResult(
            summary="Uber projected $20-30M profit; no disaster recovery RTO data is available in the verified records.",
            claims=[claim],
            insufficient_evidence_notes=["No disaster recovery or RTO/RPO metrics exist in the approved solution card."],
            unsupported_aspects=["RTO disaster recovery SLA"],
        )

    synth = GroundedAnswerSynthesizer(generator_override=mock_gemini_generator)
    res = synth.synthesize(populated_evidence_pack.query, populated_evidence_pack)

    assert len(res.claims) == 1
    assert res.claims[0].claim_type == ClaimType.VERIFIED_FACT
    assert "RTO" in res.insufficient_evidence_notes[0]
    assert "RTO disaster recovery SLA" in res.unsupported_aspects


def test_projected_vs_achieved_modal_preservation(populated_evidence_pack):
    """Verify projected outcomes are generated as projections, not achieved historical facts."""
    def mock_gemini_generator(query: str, pack: EvidencePack) -> SynthesisResult:
        claim = GroundedClaim(
            statement="The 2008 pitch realistic success scenario projected capturing 5% of top cities generating $20-30M profit.",
            claim_type=ClaimType.VERIFIED_FACT,
            customer_name="Uber",
            card_id="card_uber",
            document_id="doc_uber",
            evidence_refs=[
                EvidenceReference(
                    card_id="card_uber",
                    chunk_id="chunk_u1",
                    document_id="doc_uber",
                    page_number=20,
                    exact_source_quote="Realistic Success Scenario gets 5% of top 5 US Cities, generates 20-30M+ per year profit",
                )
            ],
        )
        return SynthesisResult(
            summary="Uber's realistic pitch scenario projected $20-30M profit.",
            claims=[claim],
            insufficient_evidence_notes=[],
            unsupported_aspects=[],
        )

    synth = GroundedAnswerSynthesizer(generator_override=mock_gemini_generator)
    res = synth.synthesize("What was the outcome?", populated_evidence_pack)

    assert "projected" in res.claims[0].statement.lower()
    assert "achieved" not in res.claims[0].statement.lower()


def test_deterministic_fallback_synthesis_on_model_exception(populated_evidence_pack):
    """Verify fallback response assembled from verified cards when model call fails."""
    mock_bad_client = MagicMock()
    mock_bad_client.models.generate_content.side_effect = RuntimeError("Vertex AI rate limit 429")

    synth = GroundedAnswerSynthesizer(genai_client=mock_bad_client)
    res = synth.synthesize("query", populated_evidence_pack)

    assert "compiled from verified customer solutions" in res.summary
    assert len(res.claims) >= 1
    assert res.claims[0].card_id == "card_uber"
    assert "429" in res.insufficient_evidence_notes[0]

