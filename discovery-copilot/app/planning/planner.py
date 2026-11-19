"""Structured Query Planner for Discovery Copilot (Phase 4.2).

Generates validated, deterministic QueryPlans containing:
1. Intent classification across all 7 supported intents.
2. Extracted and validated entities via TaxonomyMapper.
3. Decomposed retrieval subtasks with typed arguments and dependencies.
4. Execution budget and clarification handling.
5. Centralized Settings.reasoning_model configuration.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from common.config import settings
from app.planning.entity_extractor import EntityExtractor
from app.planning.models import (
    EntityCategory,
    GetSolutionEvidenceDetailArgs,
    QueryIntent,
    QueryPlan,
    RetrievalSubtask,
    SearchApprovedSolutionsArgs,
    TargetToolName,
    TraverseTechnologyGraphArgs,
    ValidatedEntity,
)

logger = logging.getLogger("discovery_copilot.planning.planner")


# Ambiguous keywords triggering proactive clarification when unaccompanied by domain context
AMBIGUOUS_GENERIC_PATTERNS = [
    r"^(tell me about|what is|how does|show me|explain|info on)?\s*(the\s+)?(system|platform|architecture|app|solution|project|everything|it)(\s+work)?\??$",
    r"^how does (it|this|the system) work\??$",
    r"^what does (it|this) do\??$",
    r"^tell me about (it|this|the system)\??$",
    r"^(help|hello|hi|hey|test|ping)\b",
]

# Follow-up indicators referencing previous turn entities
FOLLOW_UP_INDICATORS = [
    r"\b(they|it|that|their|this|the same|those|the previous|the above)\b",
    r"\b(what was the outcome|how did they solve it|what tech did they use|show evidence for that)\b",
]


class QueryPlanner:
    """Produces structured, validated query plans using centralized reasoning configuration."""

    def __init__(
        self,
        extractor: EntityExtractor | None = None,
        model_name: str | None = None,
    ) -> None:
        self.extractor = extractor or EntityExtractor()
        # Use centralized Settings.reasoning_model
        self.model_name = model_name or settings.reasoning_model

    def create_plan(
        self,
        query: str,
        session_context: list[dict[str, str]] | None = None,
        resolved_customer: str | None = None,
        resolved_card_id: str | None = None,
        resolved_technologies: list[str] | None = None,
    ) -> QueryPlan:
        """Analyzes query, context, and resolved anaphora to produce a structured QueryPlan."""
        clean_query = query.strip()
        plan_id = f"plan_{hashlib.sha256(clean_query.encode('utf-8')).hexdigest()[:16]}"

        # 1. Entity Extraction
        extracted_entities = self.extractor.extract_entities(clean_query)
        canonical_techs = [
            e for e in extracted_entities
            if e.category == EntityCategory.TECHNOLOGY and e.is_canonical and e.canonical_name
        ]
        customers = [
            e for e in extracted_entities
            if e.category == EntityCategory.CUSTOMER
        ]

        # Augment with resolved entities if not already present
        has_injected_customer = False
        if resolved_customer and not any(c.canonical_name == resolved_customer for c in customers):
            has_injected_customer = True
            customers.append(
                ValidatedEntity(
                    raw_mention=resolved_customer,
                    canonical_name=resolved_customer,
                    category=EntityCategory.CUSTOMER,
                    is_canonical=True,
                )
            )

        if resolved_technologies:
            for rt in resolved_technologies:
                if not any(t.canonical_name == rt for t in canonical_techs):
                    canonical_techs.append(
                        ValidatedEntity(
                            raw_mention=rt,
                            canonical_name=rt,
                            category=EntityCategory.TECHNOLOGY,
                            is_canonical=True,
                        )
                    )

        # 2. Intent Classification
        intent = self._classify_intent(
            clean_query,
            extracted_entities=extracted_entities,
            session_context=session_context,
            has_resolved_context=bool(has_injected_customer or resolved_card_id),
            has_injected_customer=has_injected_customer,
        )

        # 3. Handle Clarification Intent (Zero Tool Calls)
        if intent == QueryIntent.CLARIFICATION_NEEDED:
            return QueryPlan(
                plan_id=plan_id,
                original_query=clean_query,
                intent=intent,
                requires_clarification=True,
                clarification_prompt=(
                    "Could you please specify which customer architecture (e.g. Uber, Canva, Dropbox), "
                    "technology (e.g. Google Cloud Spanner, Document AI, Kubernetes), or business goal "
                    "(e.g. sub-10ms latency, 99.999% availability) you would like to explore?"
                ),
                extracted_entities=extracted_entities,
                subtasks=[],
                execution_budget_seconds=15.0,
                max_tool_calls=0,
            )

        # 4. Decompose Subtasks based on Intent
        subtasks = self._decompose_subtasks(
            query=clean_query,
            intent=intent,
            extracted_entities=extracted_entities,
            canonical_techs=canonical_techs,
            customers=customers,
            session_context=session_context,
            resolved_card_id=resolved_card_id,
        )

        # Cap subtasks at 4
        subtasks = subtasks[:4]

        return QueryPlan(
            plan_id=plan_id,
            original_query=clean_query,
            intent=intent,
            requires_clarification=False,
            clarification_prompt=None,
            extracted_entities=extracted_entities,
            subtasks=subtasks,
            execution_budget_seconds=15.0,
            max_tool_calls=len(subtasks),
        )

    def _classify_intent(
        self,
        query: str,
        extracted_entities: list[ValidatedEntity],
        session_context: list[dict[str, str]] | None = None,
        has_resolved_context: bool = False,
        has_injected_customer: bool = False,
    ) -> QueryIntent:
        """Classifies query into one of the 7 QueryIntent categories."""
        q_lower = query.lower()

        # Check Ambiguous / Underspecified
        for pattern in AMBIGUOUS_GENERIC_PATTERNS:
            if re.match(pattern, q_lower.strip()):
                return QueryIntent.CLARIFICATION_NEEDED

        if len(query.split()) <= 3 and not extracted_entities and not has_resolved_context:
            # Short query without entities or domain context
            return QueryIntent.CLARIFICATION_NEEDED

        # Check Comparison
        comp_keywords = ["compare", "versus", "vs", "difference between", "contrasting", "tradeoff", "trade-off"]
        customers = [e for e in extracted_entities if e.category == EntityCategory.CUSTOMER]
        if any(ck in q_lower for ck in comp_keywords) or len(customers) >= 2:
            return QueryIntent.COMPARISON

        # Check Evidence Verification
        evidence_keywords = ["evidence", "quote", "verbatim", "citation", "slide number", "page number", "proof", "verify"]
        card_id_match = re.search(r"\b(card_[a-zA-Z0-9_-]+)\b", query)
        if any(ek in q_lower for ek in evidence_keywords) or card_id_match:
            return QueryIntent.EVIDENCE_VERIFICATION

        # Check Architecture Exploration (multi-hop / graph relationship inquiry)
        arch_keywords = ["graph", "connect", "connection", "topology", "ecosystem", "supports", "depends on"]
        if any(ak in q_lower for ak in arch_keywords):
            return QueryIntent.ARCHITECTURE_EXPLORATION

        # Check Technical Lookup (focused on specific canonical technology)
        tech_entities = [e for e in extracted_entities if e.category == EntityCategory.TECHNOLOGY]
        if tech_entities and not customers and (len(tech_entities) == 1 or "use" in q_lower or "using" in q_lower or "stack" in q_lower):
            return QueryIntent.TECHNICAL_LOOKUP

        # Check Follow-Up (contextual reference to previous turns or resolved context)
        if has_injected_customer:
            return QueryIntent.FOLLOW_UP

        if (session_context or has_resolved_context) and not bool(customers):
            for pattern in FOLLOW_UP_INDICATORS:
                if re.search(pattern, q_lower):
                    return QueryIntent.FOLLOW_UP
            if has_resolved_context and not extracted_entities:
                return QueryIntent.FOLLOW_UP

        # Default fallback: Solution Discovery
        return QueryIntent.SOLUTION_DISCOVERY

    def _decompose_subtasks(
        self,
        query: str,
        intent: QueryIntent,
        extracted_entities: list[ValidatedEntity],
        canonical_techs: list[ValidatedEntity],
        customers: list[ValidatedEntity],
        session_context: list[dict[str, str]] | None = None,
        resolved_card_id: str | None = None,
    ) -> list[RetrievalSubtask]:
        """Decomposes classified intent into discrete, typed retrieval subtasks."""
        subtasks: list[RetrievalSubtask] = []

        # ---------------------------------------------------------------------
        # 1. COMPARISON
        # ---------------------------------------------------------------------
        if intent == QueryIntent.COMPARISON:
            if len(customers) >= 2:
                # Compare two or more customers independently
                for idx, cust in enumerate(customers[:2], 1):
                    subtasks.append(
                        RetrievalSubtask(
                            task_id=f"task_{idx}",
                            description=f"Search approved architecture and solutions for {cust.canonical_name}",
                            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                            tool_args={
                                "query": f"{cust.canonical_name} architecture solution",
                                "technologies": [t.canonical_name for t in canonical_techs] if canonical_techs else None,
                            },
                            depends_on=[],
                        )
                    )
                return subtasks

            if len(canonical_techs) >= 2:
                # Compare two technologies
                for idx, tech in enumerate(canonical_techs[:2], 1):
                    subtasks.append(
                        RetrievalSubtask(
                            task_id=f"task_{idx}",
                            description=f"Search solutions utilizing {tech.canonical_name}",
                            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                            tool_args={
                                "query": f"{tech.canonical_name} solution implementation",
                                "technologies": [tech.canonical_name],
                            },
                            depends_on=[],
                        )
                    )
                return subtasks

            # General comparison fallback
            subtasks.append(
                RetrievalSubtask(
                    task_id="task_1",
                    description="Search solutions for comparison query",
                    tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                    tool_args={"query": query, "technologies": [t.canonical_name for t in canonical_techs] if canonical_techs else None},
                    depends_on=[],
                )
            )
            return subtasks

        # ---------------------------------------------------------------------
        # 2. TECHNICAL LOOKUP
        # ---------------------------------------------------------------------
        if intent == QueryIntent.TECHNICAL_LOOKUP:
            if canonical_techs:
                primary_tech = canonical_techs[0].canonical_name
                # Step 1: Traverse Spanner Graph for verified solutions supporting this technology
                subtasks.append(
                    RetrievalSubtask(
                        task_id="task_1",
                        description=f"Traverse EnterpriseKnowledgeGraph for verified solutions supporting {primary_tech}",
                        tool_name=TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH,
                        tool_args={"technology_name": primary_tech},
                        depends_on=[],
                    )
                )
                # Step 2: Also perform hybrid search for contextual depth
                subtasks.append(
                    RetrievalSubtask(
                        task_id="task_2",
                        description=f"Hybrid search for deployment details of {primary_tech}",
                        tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                        tool_args={
                            "query": query,
                            "technologies": [primary_tech],
                        },
                        depends_on=[],
                    )
                )
                return subtasks

            # If technology is non-canonical / unknown, use text search (never traverse graph with unknown tech)
            subtasks.append(
                RetrievalSubtask(
                    task_id="task_1",
                    description="Search approved solutions for technology mention",
                    tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                    tool_args={"query": query, "technologies": None},
                    depends_on=[],
                )
            )
            return subtasks

        # ---------------------------------------------------------------------
        # 3. EVIDENCE VERIFICATION
        # ---------------------------------------------------------------------
        if intent == QueryIntent.EVIDENCE_VERIFICATION:
            card_id_match = re.search(r"\b(card_[a-f0-9]+)\b", query)
            if card_id_match:
                cid = card_id_match.group(1)
                subtasks.append(
                    RetrievalSubtask(
                        task_id="task_1",
                        description=f"Retrieve verbatim evidence quotes and slide/page anchors for card '{cid}'",
                        tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                        tool_args={"card_id": cid},
                        depends_on=[],
                    )
                )
                return subtasks

            # If no card ID explicitly provided, first find candidate card, then inspect evidence
            subtasks.append(
                RetrievalSubtask(
                    task_id="task_1",
                    description="Find target solution card matching evidence claim",
                    tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                    tool_args={"query": query, "technologies": [t.canonical_name for t in canonical_techs] if canonical_techs else None},
                    depends_on=[],
                )
            )
            subtasks.append(
                RetrievalSubtask(
                    task_id="task_2",
                    description="Inspect verbatim citations and anchors for the matched solution card",
                    tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                    tool_args={"card_id": "$task_1.card_id"},
                    depends_on=["task_1"],
                )
            )
            return subtasks

        # ---------------------------------------------------------------------
        # 4. ARCHITECTURE EXPLORATION
        # ---------------------------------------------------------------------
        if intent == QueryIntent.ARCHITECTURE_EXPLORATION:
            if canonical_techs:
                subtasks.append(
                    RetrievalSubtask(
                        task_id="task_1",
                        description=f"Traverse graph relationships for {canonical_techs[0].canonical_name}",
                        tool_name=TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH,
                        tool_args={"technology_name": canonical_techs[0].canonical_name},
                        depends_on=[],
                    )
                )
            subtasks.append(
                RetrievalSubtask(
                    task_id=f"task_{len(subtasks) + 1}",
                    description="Search multi-component architectural implementations",
                    tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                    tool_args={"query": query, "technologies": [t.canonical_name for t in canonical_techs] if canonical_techs else None},
                    depends_on=[],
                )
            )
            return subtasks

        # ---------------------------------------------------------------------
        # 5. FOLLOW UP (Conversational Context)
        # ---------------------------------------------------------------------
        if intent == QueryIntent.FOLLOW_UP:
            # 1. If resolved card ID is available from structured memory/context, directly inspect evidence
            if resolved_card_id:
                subtasks.append(
                    RetrievalSubtask(
                        task_id="task_1",
                        description=f"Inspect details for verified active card '{resolved_card_id}'",
                        tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                        tool_args={"card_id": resolved_card_id},
                        depends_on=[],
                    )
                )
                return subtasks

            # 2. Check if query or previous context explicitly mentions a card
            card_id_match = re.search(r"\b(card_[a-f0-9]+)\b", query)
            if card_id_match:
                cid = card_id_match.group(1)
                subtasks.append(
                    RetrievalSubtask(
                        task_id="task_1",
                        description=f"Inspect details for card '{cid}'",
                        tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                        tool_args={"card_id": cid},
                        depends_on=[],
                    )
                )
                return subtasks

            context_query = query
            if session_context:
                last_turn = session_context[-1]
                prev_text = last_turn.get("content", "")
                prev_card_match = re.search(r"\b(card_[a-f0-9]+)\b", prev_text)
                if prev_card_match:
                    cid = prev_card_match.group(1)
                    subtasks.append(
                        RetrievalSubtask(
                            task_id="task_1",
                            description=f"Inspect details for previously discussed card '{cid}'",
                            tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                            tool_args={"card_id": cid},
                            depends_on=[],
                        )
                    )
                    return subtasks
                # Use context query without destructive truncation
                if len(prev_text) > 120:
                    context_query = f"{prev_text[:120]}... {query}"
                else:
                    context_query = f"{prev_text} {query}"

            subtasks.append(
                RetrievalSubtask(
                    task_id="task_1",
                    description="Search approved solutions using conversational context",
                    tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                    tool_args={"query": context_query, "technologies": [t.canonical_name for t in canonical_techs] if canonical_techs else None},
                    depends_on=[],
                )
            )
            return subtasks

        # ---------------------------------------------------------------------
        # 6. SOLUTION DISCOVERY (Default)
        # ---------------------------------------------------------------------
        subtasks.append(
            RetrievalSubtask(
                task_id="task_1",
                description="Search verified customer solutions matching query",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": query, "technologies": [t.canonical_name for t in canonical_techs] if canonical_techs else None},
                depends_on=[],
            )
        )
        return subtasks
