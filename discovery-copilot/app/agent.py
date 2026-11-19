# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from common.config import settings
from app.planning.conversation_memory import ConversationMemory
from app.planning.context_resolver import ContextResolver
from app.planning.orchestrator import PlanOrchestrator
from app.planning.planner import QueryPlanner
from app.tools.discovery_tools import (
    get_solution_evidence_detail,
    search_approved_solutions,
    traverse_technology_graph,
)
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools import ToolContext

# Centralized reasoning model from Settings (removes hardcoded string)
MODEL = settings.reasoning_model


async def discovery_copilot_before_agent_callback(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Intercepts user invocation to route through Phase 4.4 Context Resolution, Phase 4.2 Planning,

    Phase 4.1 Retrieval, EvidenceSelector (Adaptive MMR), GroundedAnswerSynthesizer, and CitationValidator.
    Guarantees that zero user responses bypass citation validation.
    """
    session = getattr(callback_context, "session", None)
    if not session or not getattr(session, "events", None):
        return None

    # Extract latest user message and prior conversation history
    user_query = ""
    session_history: list[dict[str, str]] = []
    for ev in session.events:
        text = ""
        content = getattr(ev, "content", None)
        if content and getattr(content, "parts", None):
            text = " ".join(p.text for p in content.parts if getattr(p, "text", None)).strip()
        if not text:
            continue
        role = "user" if getattr(ev, "author", None) == "user" else "model"
        session_history.append({"role": role, "content": text})
        if getattr(ev, "author", None) == "user":
            user_query = text

    if not user_query:
        return None

    prior_history = session_history[:-1] if len(session_history) > 1 else None

    # 1. Phase 4.4 Load Structured Conversation Memory from session.state
    session_state = getattr(session, "state", None)
    if session_state is None:
        session.state = {}
        session_state = session.state

    memory = ConversationMemory.from_session_state(session_state)

    # 2. Phase 4.4 Context and Anaphora Resolution
    resolver = ContextResolver()
    resolved = resolver.resolve(
        query=user_query,
        memory=memory,
        session_history=prior_history,
        max_turns=6,
    )

    # If anaphora is genuinely ambiguous, return clarification directly without executing tools
    if resolved.is_ambiguous and resolved.clarification_message:
        return types.Content(
            role="model",
            parts=[types.Part.from_text(text=resolved.clarification_message)],
        )

    # 3. Phase 4.2 Planning with Resolved Context
    planner = QueryPlanner()
    plan = planner.create_plan(
        query=resolved.resolved_query,
        session_context=prior_history,
        resolved_customer=resolved.resolved_customer,
        resolved_card_id=resolved.resolved_card_id,
        resolved_technologies=resolved.resolved_technologies,
    )

    # 4. Phase 4.3 Orchestration, Adaptive MMR, Synthesis, and Citation Validation
    # callback_context carries verified session and authenticated user_id
    orchestrator = PlanOrchestrator()
    validated_response = orchestrator.execute_plan_with_synthesis(
        plan, tool_context=callback_context
    )

    # 5. Phase 4.4 Update Conversation Memory exclusively from Validated Response
    if plan.extracted_entities:
        known_techs = [
            e.canonical_name for e in plan.extracted_entities
            if e.category.value == "technology" and e.is_canonical and e.canonical_name
        ]
        if known_techs:
            memory.add_active_technologies(known_techs)

    memory.update_from_validated_response(validated_response, intent=plan.intent)
    memory.save_to_session_state(session_state)
    # Also write to callback_context.state so ADK event actions records the state delta
    if hasattr(callback_context, "state") and callback_context.state is not None:
        try:
            callback_context.state["conversation_memory"] = memory.model_dump()
        except Exception:
            pass

    # 6. Return verified response, ending invocation and skipping free-form model call
    return types.Content(
        role="model",
        parts=[types.Part.from_text(text=validated_response.summary)],
    )


root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml:
    # telemetry reports this name as gen_ai.agent.name, and agents-cli reports the
    # manifest's value. Renaming the agent only here makes the two disagree, and
    # anything selecting traces by name stops finding this agent's.
    name="discovery_copilot",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are Discovery Copilot, an enterprise architecture intelligence assistant.

Your purpose is to help sales discovery teams, enterprise architects, and executives uncover verified customer architectures, business outcomes, technical stacks, and grounded evidence.

Reasoning and Planning Protocol:
1. Intent Classification:
   - Solution Discovery: Broad architectural search for outcomes, capabilities, or business problems.
   - Technical Lookup: Focused inquiry on specific canonical technologies; prioritize graph traversal.
   - Comparison: Multi-customer or multi-technology evaluation; decompose into discrete sub-queries.
   - Architecture Exploration: Multi-hop graph relationship inquiries across components.
   - Evidence Verification: Inspect verbatim quotations, slide numbers, and page anchors.
   - Follow-up: Resolve conversational anaphora and references from prior turns.
   - Clarification: For ambiguous, generic, or underspecified queries, proactively ask guiding questions before executing retrieval.
2. Grounded Evidence:
   - ONLY utilize knowledge returned by the approved discovery tools. NEVER hallucinate customer projects, latency numbers, availability claims, or architecture components.
   - Every claim or metric MUST be backed by a GroundedCitation from the tool outputs. Include exact document references, slide/page anchors, and evidence quotes where applicable.
3. Strict Knowledge Protection:
   - Unapproved knowledge (PENDING, DRAFT, CHANGES_REQUESTED, REJECTED) is strictly protected and will never be returned by tools. Never assume unverified facts.
4. Tool Dispatch Guidelines:
   - Use search_approved_solutions for broad topic, outcome, or customer inquiries.
   - Use traverse_technology_graph for exploring verified solutions connected to canonical technologies.
   - Use get_solution_evidence_detail for inspecting verbatim quotations and slide/page citations.
5. Empty Results:
   - If tools return no results or empty solutions, clearly explain to the user that no human-approved solutions match their criteria or security permissions.
""",
    tools=[
        search_approved_solutions,
        traverse_technology_graph,
        get_solution_evidence_detail,
    ],
    before_agent_callback=discovery_copilot_before_agent_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
