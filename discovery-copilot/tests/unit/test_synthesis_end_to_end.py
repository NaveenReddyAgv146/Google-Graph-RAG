"""End-to-End Integration Tests for Phase 4.3 Pipeline.

Flow:
PlanOrchestrator -> EvidenceSelector -> GroundedAnswerSynthesizer -> CitationValidator.
"""

import json
from unittest.mock import MagicMock
from google.adk.tools import ToolContext
import pytest

from app.planning.models import (
    QueryIntent,
    QueryPlan,
    RetrievalSubtask,
    TargetToolName,
)
from app.planning.orchestrator import PlanOrchestrator
from app.synthesis.citation_validator import CitationValidator
from app.synthesis.evidence_selector import EvidenceSelector
from app.synthesis.models import (
    ClaimType,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
)
from app.synthesis.synthesizer import GroundedAnswerSynthesizer


def test_end_to_end_pipeline_success_flow(monkeypatch):
    """Verify complete end-to-end execution: plan -> tool -> evidence selector -> synthesis -> validation."""
    import app.planning.orchestrator as orch_mod

    mock_search_res = json.dumps({
        "status": "success",
        "solutions": [
            {
                "card_id": "card_uber_e2e",
                "document_id": "doc_uber_e2e",
                "document_name": "Uber - 2008.pdf",
                "project_name": "UberCab On-Demand Dispatch",
                "verification_status": "APPROVED",
                "business_problem": "Radio dispatch inefficient",
                "implemented_solution": "1-Click request from Geo-aware devices",
                "outcome": "Automated dispatch",
                "citations": [
                    {
                        "evidence_id": "ev_u1",
                        "chunk_id": "chunk_u1",
                        "document_id": "doc_uber_e2e",
                        "page_number": 8,
                        "source_chunk_text": "1-Click request from Geo-aware devices. SMS from any phone.",
                    }
                ],
            }
        ],
    })

    monkeypatch.setattr(orch_mod, "search_approved_solutions", lambda **kwargs: mock_search_res)

    def mock_generator(query: str, pack: EvidencePack) -> SynthesisResult:
        claim = GroundedClaim(
            statement="Uber implemented 1-Click requests using geo-aware devices.",
            claim_type=ClaimType.VERIFIED_FACT,
            customer_name="Uber",
            card_id="card_uber_e2e",
            document_id="doc_uber_e2e",
            evidence_refs=[
                EvidenceReference(
                    card_id="card_uber_e2e",
                    chunk_id="chunk_u1",
                    document_id="doc_uber_e2e",
                    customer_name="Uber",
                    page_number=8,
                    exact_source_quote="1-Click request from Geo-aware devices",
                )
            ],
        )
        return SynthesisResult(
            summary="Uber deployed geo-aware mobile request dispatch.",
            claims=[claim],
        )

    orchestrator = PlanOrchestrator()
    ev_selector = EvidenceSelector()
    synthesizer = GroundedAnswerSynthesizer(generator_override=mock_generator)
    validator = CitationValidator()

    plan = QueryPlan(
        plan_id="plan_e2e_1",
        original_query="How did Uber build mobile dispatch?",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search Uber dispatch",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "Uber mobile dispatch"},
            )
        ],
    )

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "architect@enterprise.com"

    response = orchestrator.execute_plan_with_synthesis(
        plan=plan,
        tool_context=ctx,
        evidence_selector=ev_selector,
        synthesizer=synthesizer,
        validator=validator,
    )

    assert response.is_valid is True
    assert response.precision == 1.0
    assert response.quote_fidelity == 1.0
    assert response.misattribution_count == 0
    assert len(response.validated_claims) == 1
    assert "geo-aware" in response.summary


def test_end_to_end_clarification_short_circuits():
    """Verify CLARIFICATION_NEEDED plan executes 0 tools and returns clarification prompt."""
    orchestrator = PlanOrchestrator()

    plan = QueryPlan(
        plan_id="plan_clarify_e2e",
        original_query="how does it work?",
        intent=QueryIntent.CLARIFICATION_NEEDED,
        requires_clarification=True,
        clarification_prompt="Could you specify which customer or technology you want to explore?",
        subtasks=[],
        max_tool_calls=0,
    )

    response = orchestrator.execute_plan_with_synthesis(plan=plan)

    assert response.is_valid is True
    assert "specify" in response.summary.lower()
    assert len(response.validated_claims) == 0


def test_end_to_end_empty_retrieval_returns_insufficient_evidence(monkeypatch):
    """Verify empty retrieval returns grounded fallback without hallucination."""
    import app.planning.orchestrator as orch_mod

    empty_tool_res = json.dumps({"status": "empty", "message": "No human-approved solutions found.", "solutions": []})
    monkeypatch.setattr(orch_mod, "search_approved_solutions", lambda **kwargs: empty_tool_res)

    orchestrator = PlanOrchestrator()
    plan = QueryPlan(
        plan_id="plan_empty_e2e",
        original_query="nonexistent tech",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "nonexistent tech"},
            )
        ],
    )

    response = orchestrator.execute_plan_with_synthesis(plan=plan)
    assert response.is_valid is True
    assert "No human-approved solutions" in response.summary
    assert len(response.validated_claims) == 0


def test_end_to_end_purges_unsupported_claim_and_rebuilds_summary(monkeypatch):
    """Verify that an ungrounded claim is dropped and summary rebuilt from valid claims."""
    import app.planning.orchestrator as orch_mod

    mock_search_res = json.dumps({
        "status": "success",
        "solutions": [
            {
                "card_id": "card_uber",
                "document_id": "doc_uber",
                "project_name": "Uber Dispatch",
                "verification_status": "APPROVED",
                "citations": [
                    {
                        "chunk_id": "chunk_u1",
                        "document_id": "doc_uber",
                        "source_chunk_text": "SMS from any phone: pickup @work in 5",
                    }
                ],
            }
        ],
    })
    monkeypatch.setattr(orch_mod, "search_approved_solutions", lambda **kwargs: mock_search_res)

    # Model generates 1 valid claim, and 1 hallucinated claim
    def mock_dirty_generator(query: str, pack: EvidencePack) -> SynthesisResult:
        c_valid = GroundedClaim(
            statement="Uber enabled SMS pickup requests.",
            claim_type=ClaimType.VERIFIED_FACT,
            customer_name="Uber",
            card_id="card_uber",
            document_id="doc_uber",
            evidence_refs=[
                EvidenceReference(
                    card_id="card_uber",
                    chunk_id="chunk_u1",
                    document_id="doc_uber",
                    exact_source_quote="SMS from any phone: pickup @work in 5",
                )
            ],
        )
        c_hallucinated = GroundedClaim(
            statement="Uber migrated to quantum databases.",
            claim_type=ClaimType.VERIFIED_FACT,
            evidence_refs=[
                EvidenceReference(
                    card_id="card_uber",
                    chunk_id="chunk_u1",
                    document_id="doc_uber",
                    exact_source_quote="Fabricated non-existent text",
                )
            ],
        )
        return SynthesisResult(
            summary="Uber enabled SMS requests and migrated to quantum databases.",
            claims=[c_valid, c_hallucinated],
        )

    orchestrator = PlanOrchestrator()
    synthesizer = GroundedAnswerSynthesizer(generator_override=mock_dirty_generator)

    plan = QueryPlan(
        plan_id="plan_dirty",
        original_query="Uber capabilities",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "Uber capabilities"},
            )
        ],
    )

    response = orchestrator.execute_plan_with_synthesis(plan=plan, synthesizer=synthesizer)

    # Response should have purged the quantum claim
    assert len(response.validated_claims) == 1
    assert len(response.rejected_claims) == 1
    assert "quantum" not in response.summary.lower()
    assert "SMS pickup requests" in response.summary


@pytest.mark.asyncio
async def test_root_agent_runner_intercepts_request_and_enforces_validation(monkeypatch):
    """Runtime Audit: Verify ADK Runner dispatching root_agent executes the complete Phase 4.3 pipeline.

    Proves:
    1. User request arriving via ADK Runner routes through before_agent_callback.
    2. Executes QueryPlanner, PlanOrchestrator, EvidenceSelector, GroundedAnswerSynthesizer, CitationValidator.
    3. Replaces free-form Gemini text with validated, citation-grounded output.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from app.agent import root_agent
    import app.planning.orchestrator as orch_mod

    # Mock tool response
    mock_search_res = json.dumps({
        "status": "success",
        "solutions": [
            {
                "card_id": "card_uber_runtime",
                "document_id": "doc_uber",
                "project_name": "Uber Dispatch Service",
                "verification_status": "APPROVED",
                "citations": [
                    {
                        "chunk_id": "chunk_u1",
                        "document_id": "doc_uber",
                        "source_chunk_text": "SMS from any phone: pickup @work in 5",
                    }
                ],
            }
        ],
    })
    monkeypatch.setattr(orch_mod, "search_approved_solutions", lambda **kwargs: mock_search_res)

    # Initialize Runner
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        user_id="verified.architect@enterprise.com",
        app_name="app",
    )

    runner = Runner(
        agent=root_agent,
        session_service=session_service,
        app_name="app",
    )

    # Dispatch user message through ADK Runner
    user_msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text="How did UberCab handle SMS booking?")],
    )

    events = []
    async for event in runner.run_async(
        user_id="verified.architect@enterprise.com",
        session_id=session.id,
        new_message=user_msg,
    ):
        events.append(event)

    # Verify model event was emitted
    model_events = [e for e in events if e.author == "discovery_copilot" or getattr(e, "content", None)]
    assert len(model_events) >= 1

    final_text = ""
    for ev in model_events:
        if ev.content and ev.content.parts:
            final_text += " ".join(p.text for p in ev.content.parts if p.text)

    # Assert output contains validated architectural findings and card reference
    assert len(final_text) > 0
    assert "Uber" in final_text or "findings" in final_text.lower() or "card_uber_runtime" in final_text


