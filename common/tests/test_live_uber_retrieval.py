"""Live read-only integration test for approved Uber SolutionCard retrieval and citation grounding."""

import pytest
from common.database.spanner import get_database
from common.search.discovery_service import DiscoveryRetrievalService


@pytest.mark.integration
def test_live_uber_solution_card_retrieval_and_grounded_citations():
    """Verify live approved Uber card retrieval, all 6 claim-level citations, and RBAC enforcement."""
    db = get_database()
    service = DiscoveryRetrievalService(database=db)

    # 1. Authorized user retrieves Uber SolutionCard
    cards = service.search_approved_solutions(
        principal_email="aiagent.testing125@gmail.com",
        keyword="Uber",
    )
    assert len(cards) == 1, f"Expected 1 card for Uber, got {len(cards)}"
    uber_card = cards[0]

    # Verify Card Metadata
    assert uber_card.card_id == "card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d"
    assert uber_card.document_id == "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
    assert uber_card.project_name == "UberCab Next-Generation Car Service"
    assert uber_card.verification_status == "APPROVED"
    assert uber_card.document_name == "Uber - 2008 (1).pdf"

    # 2. Verify all 6 citations are returned
    assert len(uber_card.citations) == 6, f"Expected 6 citations, got {len(uber_card.citations)}"

    # 3. Verify Claim-Level Evidence Breakdown & Grounding
    claim_types = []
    expected_claims = {"BUSINESS_PROBLEM", "PROPOSED_SOLUTION", "IMPLEMENTED_SOLUTION", "OUTCOME"}

    for cit in uber_card.citations:
        assert cit.evidence_id.startswith("evl_"), f"Invalid evidence ID format: {cit.evidence_id}"
        assert cit.chunk_id.startswith("chunk-"), f"Invalid chunk ID format: {cit.chunk_id}"
        assert cit.document_id == "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
        assert cit.page_number is not None and cit.page_number >= 1
        assert cit.claim_type in expected_claims, f"Unexpected claim type: {cit.claim_type}"
        assert cit.source_chunk_text is not None and len(cit.source_chunk_text) > 20, (
            f"Expected non-empty grounded source text for citation {cit.evidence_id}"
        )
        claim_types.append(cit.claim_type)

    assert set(claim_types) == expected_claims, f"Missing claim categories: {expected_claims - set(claim_types)}"
    assert claim_types.count("BUSINESS_PROBLEM") == 3
    assert claim_types.count("PROPOSED_SOLUTION") == 1
    assert claim_types.count("IMPLEMENTED_SOLUTION") == 1
    assert claim_types.count("OUTCOME") == 1

    # 4. Unauthorized identity receives zero results
    unauth_cards = service.search_approved_solutions(
        principal_email="unauthorized_intruder@external.com",
        keyword="Uber",
    )
    assert len(unauth_cards) == 0, f"Expected 0 cards for unauthorized user, got {len(unauth_cards)}"

    # 5. Missing / empty identity fails closed
    empty_cards = service.search_approved_solutions(
        principal_email="",
        keyword="Uber",
    )
    assert len(empty_cards) == 0, f"Expected 0 cards for empty identity, got {len(empty_cards)}"

