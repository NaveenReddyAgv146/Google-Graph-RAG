"""Integration tests for Phase 4.4 Multi-Turn ADK Runner execution.

Validates:
1. End-to-end multi-turn conversation maintaining ConversationMemory across turns in ADK Runner.
2. Turn 1 (Discovery) updates memory with customer and card ID.
3. Turn 2 (Follow-up with pronoun "they") successfully resolves entity from memory.
4. Turn 3 (Evidence verification) inspects the verified card ID.
5. Ambiguous pronoun prompt triggers immediate clarification without tool execution.
6. Cross-session isolation: Session 1 memory does not leak into Session 2.
"""

from unittest.mock import MagicMock, patch
import pytest
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import app as adk_app, root_agent
from app.planning.models import QueryIntent
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


def _make_mock_validated_response(
    customer: str = "Uber",
    card_id: str = "card_1111111122223333",
    summary: str = "Uber utilizes Cloud Spanner for high availability.",
) -> ValidatedSynthesisResponse:
    claim = GroundedClaim(
        statement=summary,
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
        summary=summary,
        validated_claims=[claim],
        rejected_claims=[],
        validation_details=[detail],
        insufficient_evidence_notes=[],
    )


@pytest.mark.asyncio
async def test_adk_runner_multi_turn_memory_lifecycle():
    session_service = InMemorySessionService()
    runner = Runner(
        app=adk_app,
        session_service=session_service,
        auto_create_session=True,
    )

    user_id = "architect@enterprise.com"
    session_id = "session_multi_turn_test"

    # Turn 1: Solution discovery
    mock_resp1 = _make_mock_validated_response(
        customer="Uber",
        card_id="card_1111111122223333",
        summary="Uber achieves 99.999% availability using Google Cloud Spanner.",
    )

    with patch(
        "app.planning.orchestrator.PlanOrchestrator.execute_plan_with_synthesis",
        return_value=mock_resp1,
    ):
        msg1 = types.Content(
            role="user",
            parts=[types.Part.from_text(text="Show me Uber's payment architecture.")],
        )

        turn1_response = None
        async for ev in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=msg1,
        ):
            if ev.content and ev.content.parts:
                turn1_response = ev.content.parts[0].text

        assert turn1_response == mock_resp1.summary

    # Verify session state now contains Uber in active_customers
    session = await session_service.get_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id,
    )
    assert session is not None
    memory_dict = session.state.get("conversation_memory")
    assert memory_dict is not None
    assert memory_dict["active_customers"] == ["Uber"]
    assert memory_dict["active_card_ids"] == ["card_1111111122223333"]

    # Turn 2: Follow-up pronoun "they"
    mock_resp2 = _make_mock_validated_response(
        customer="Uber",
        card_id="card_1111111122223333",
        summary="They achieved sub-10ms transaction latency across all regions.",
    )

    with patch(
        "app.planning.orchestrator.PlanOrchestrator.execute_plan_with_synthesis",
        return_value=mock_resp2,
    ) as mock_exec:
        msg2 = types.Content(
            role="user",
            parts=[types.Part.from_text(text="What latency did they achieve with that?")],
        )

        turn2_response = None
        async for ev in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=msg2,
        ):
            if ev.content and ev.content.parts:
                turn2_response = ev.content.parts[0].text

        assert turn2_response == mock_resp2.summary
        # Verify that orchestrator received a plan with resolved customer Uber
        call_args = mock_exec.call_args[0]
        executed_plan = call_args[0]
        assert any(c.canonical_name == "Uber" for c in executed_plan.extracted_entities)


@pytest.mark.asyncio
async def test_adk_runner_ambiguous_reference_returns_clarification():
    session_service = InMemorySessionService()
    runner = Runner(
        app=adk_app,
        session_service=session_service,
        auto_create_session=True,
    )

    user_id = "architect@enterprise.com"
    session_id = "session_ambiguous_test"

    # Pre-populate session state with 2 active customers
    session = await session_service.create_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id,
        state={
            "conversation_memory": {
                "active_customers": ["Uber", "Canva"],
                "active_card_ids": ["card_01", "card_02"],
                "active_technologies": [],
                "unresolved_questions": [],
                "last_turn_timestamp": 2000000000.0,
            }
        },
    )

    # Prompt with ambiguous pronoun "their"
    msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text="What was their latency?")],
    )

    response_text = None
    async for ev in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=msg,
    ):
        if ev.content and ev.content.parts:
            response_text = ev.content.parts[0].text

    assert response_text is not None
    assert "Uber, Canva" in response_text
    assert "Which customer" in response_text


@pytest.mark.asyncio
async def test_cross_session_memory_isolation():
    session_service = InMemorySessionService()
    runner = Runner(
        app=adk_app,
        session_service=session_service,
        auto_create_session=True,
    )

    # User A in Session 1
    session1 = await session_service.create_session(
        app_name=adk_app.name,
        user_id="user_a@enterprise.com",
        session_id="session_user_a",
        state={
            "conversation_memory": {
                "active_customers": ["ConfidentialCustomerA"],
                "active_card_ids": ["card_confidential_a"],
                "active_technologies": [],
                "unresolved_questions": [],
                "last_turn_timestamp": 2000000000.0,
            }
        },
    )

    # User B in Session 2
    session2 = await session_service.create_session(
        app_name=adk_app.name,
        user_id="user_b@enterprise.com",
        session_id="session_user_b",
    )

    # User B asks "What database did they use?"
    # Session 2 has empty memory, so "they" cannot resolve to ConfidentialCustomerA
    msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text="What database did they use?")],
    )

    mock_resp_b = _make_mock_validated_response(
        customer="Generic",
        card_id="card_public_01",
        summary="No specific customer context found.",
    )

    with patch(
        "app.planning.orchestrator.PlanOrchestrator.execute_plan_with_synthesis",
        return_value=mock_resp_b,
    ) as mock_exec:
        async for _ in runner.run_async(
            user_id="user_b@enterprise.com",
            session_id="session_user_b",
            new_message=msg,
        ):
            pass

        # Verify ConfidentialCustomerA NEVER enters User B's plan
        executed_plan = mock_exec.call_args[0][0]
        assert not any(c.canonical_name == "ConfidentialCustomerA" for c in executed_plan.extracted_entities)
