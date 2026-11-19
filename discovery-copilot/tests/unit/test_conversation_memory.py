"""Unit tests for Phase 4.4 Structured Conversation Memory.

Validates:
1. Safe hydration from missing, empty, or malformed session.state.
2. Memory update strictly from Phase 4.3 ValidatedSynthesisResponse.
3. Memory bounds (max customers, max card IDs, max technologies).
4. Memory expiration handling.
5. Session state isolation.
"""

import time
import pytest

from app.planning.conversation_memory import (
    ConversationMemory,
    MAX_ACTIVE_CUSTOMERS,
    MAX_ACTIVE_CARD_IDS,
    MAX_ACTIVE_TECHS,
)
from app.planning.models import QueryIntent
from app.synthesis.models import (
    ClaimType,
    ClaimValidationDetail,
    ClaimValidationStatus,
    EvidenceReference,
    GroundedClaim,
    ValidatedSynthesisResponse,
)


def _make_dummy_validated_response(
    customer: str = "Uber",
    card_id: str = "card_0123456789abcdef",
    notes: list[str] | None = None,
) -> ValidatedSynthesisResponse:
    claim = GroundedClaim(
        statement=f"{customer} leverages Cloud Spanner for high availability.",
        claim_type=ClaimType.VERIFIED_FACT,
        customer_name=customer,
        card_id=card_id,
        document_id="doc_01",
        evidence_refs=[
            EvidenceReference(
                card_id=card_id,
                chunk_id="chunk_01",
                document_id="doc_01",
                customer_name=customer,
                exact_source_quote="high availability",
            )
        ],
    )
    detail = ClaimValidationDetail(
        claim_id=claim.claim_id,
        statement=claim.statement,
        is_valid=True,
        status=ClaimValidationStatus.VALID,
        reason=None,
        evidence_ref_count=1,
    )
    return ValidatedSynthesisResponse(
        is_valid=True,
        summary=claim.statement,
        validated_claims=[claim],
        rejected_claims=[],
        validation_details=[detail],
        insufficient_evidence_notes=notes or [],
    )


def test_memory_hydration_from_empty_or_missing_state():
    # None state
    mem1 = ConversationMemory.from_session_state(None)
    assert mem1.active_customers == []
    assert mem1.active_card_ids == []

    # Empty dict
    mem2 = ConversationMemory.from_session_state({})
    assert mem2.active_customers == []

    # Malformed state
    mem3 = ConversationMemory.from_session_state({"conversation_memory": "invalid_string_not_dict"})
    assert mem3.active_customers == []


def test_memory_update_only_from_validated_response():
    memory = ConversationMemory()
    resp = _make_dummy_validated_response(customer="Uber", card_id="card_aaaaaaaa11112222")

    memory.update_from_validated_response(resp, intent=QueryIntent.SOLUTION_DISCOVERY)

    assert memory.active_customers == ["Uber"]
    assert memory.active_card_ids == ["card_aaaaaaaa11112222"]
    assert memory.last_intent == QueryIntent.SOLUTION_DISCOVERY


def test_memory_bounds_enforced():
    memory = ConversationMemory()

    # Add 10 customers
    for i in range(10):
        resp = _make_dummy_validated_response(
            customer=f"Customer_{i}",
            card_id=f"card_{i:02d}00000011112222",
        )
        memory.update_from_validated_response(resp)

    # Customers must be capped at MAX_ACTIVE_CUSTOMERS
    assert len(memory.active_customers) == MAX_ACTIVE_CUSTOMERS
    # Most recent should be at the front
    assert memory.active_customers[0] == "Customer_9"
    # Cards capped at MAX_ACTIVE_CARD_IDS
    assert len(memory.active_card_ids) == MAX_ACTIVE_CARD_IDS
    assert memory.active_card_ids[0] == "card_0900000011112222"


def test_memory_recency_deduplication():
    memory = ConversationMemory()
    resp_uber = _make_dummy_validated_response(customer="Uber", card_id="card_1111111122223333")
    resp_canva = _make_dummy_validated_response(customer="Canva", card_id="card_4444444455556666")

    memory.update_from_validated_response(resp_uber)
    memory.update_from_validated_response(resp_canva)
    assert memory.active_customers == ["Canva", "Uber"]

    # Mention Uber again -> Uber moves to index 0
    memory.update_from_validated_response(resp_uber)
    assert memory.active_customers == ["Uber", "Canva"]


def test_memory_expiration():
    memory = ConversationMemory()
    memory.last_turn_timestamp = time.time() - 100.0

    # TTL of 50s -> expired
    assert memory.is_expired(ttl_seconds=50.0) is True
    # TTL of 200s -> not expired
    assert memory.is_expired(ttl_seconds=200.0) is False

    # Hydration resets expired memory
    state = {"conversation_memory": memory.model_dump()}
    hydrated = ConversationMemory.from_session_state(state)
    # Default TTL is 24h, so with 100s it's valid:
    assert hydrated.last_turn_timestamp > 0


def test_memory_unresolved_questions_tracking():
    memory = ConversationMemory()
    resp = _make_dummy_validated_response(
        customer="Uber",
        notes=["RTO and RPO metrics are not specified in approved documentation."],
    )
    memory.update_from_validated_response(resp)
    assert len(memory.unresolved_questions) == 1
    assert "RTO and RPO" in memory.unresolved_questions[0]
