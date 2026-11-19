"""Pydantic models and schemas for Discovery Copilot query planning and reasoning (Phase 4.2).

Defines:
1. QueryIntent: 7 distinct user intents.
2. ValidatedEntity: Canonical/non-canonical entity metadata.
3. Tool-specific typed argument models.
4. RetrievalSubtask: Individual retrieval task with dependencies.
5. QueryPlan: Complete, validated query plan schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class QueryIntent(str, Enum):
    """Classified user query intent."""

    SOLUTION_DISCOVERY = "SOLUTION_DISCOVERY"          # Broad keyword/outcome/architecture search
    TECHNICAL_LOOKUP = "TECHNICAL_LOOKUP"              # Specific technology inquiry or stack search
    COMPARISON = "COMPARISON"                          # Multi-customer or multi-technology comparison
    ARCHITECTURE_EXPLORATION = "ARCHITECTURE_EXPLORATION" # Multi-hop relationship inquiry in knowledge graph
    EVIDENCE_VERIFICATION = "EVIDENCE_VERIFICATION"    # Inspecting verbatim quotations and slide/page anchors
    FOLLOW_UP = "FOLLOW_UP"                            # Anaphora / contextual continuation from prior turns
    CLARIFICATION_NEEDED = "CLARIFICATION_NEEDED"      # Ambiguous or underspecified query requiring user input


class EntityCategory(str, Enum):
    """Category of extracted entity."""

    TECHNOLOGY = "TECHNOLOGY"
    CUSTOMER = "CUSTOMER"
    BUSINESS_DOMAIN = "BUSINESS_DOMAIN"
    METRIC_OR_GOAL = "METRIC_OR_GOAL"


class ValidatedEntity(BaseModel):
    """Entity extracted from query, validated against deterministic taxonomy where applicable."""

    raw_mention: str = Field(..., description="Entity mention as it appeared in the query")
    category: EntityCategory = Field(..., description="High-level category of entity")
    canonical_name: str | None = Field(default=None, description="Official canonical name from catalog if recognized")
    tech_id: str | None = Field(default=None, description="Deterministic canonical TechId (<= 64 chars)")
    is_canonical: bool = Field(default=False, description="True if verified against TechnologyCatalog")


class TargetToolName(str, Enum):
    """Existing Phase 4.1 ADK discovery tools eligible for execution."""

    SEARCH_APPROVED_SOLUTIONS = "search_approved_solutions"
    TRAVERSE_TECHNOLOGY_GRAPH = "traverse_technology_graph"
    GET_SOLUTION_EVIDENCE_DETAIL = "get_solution_evidence_detail"


# ============================================================================
# TOOL-SPECIFIC TYPED ARGUMENT MODELS
# ============================================================================


class SearchApprovedSolutionsArgs(BaseModel):
    """Typed arguments for search_approved_solutions tool."""

    query: str = Field(..., min_length=1, max_length=512, description="Natural language search phrase or keywords")
    technologies: list[str] | None = Field(
        default=None, description="Optional list of canonical technology names"
    )


class TraverseTechnologyGraphArgs(BaseModel):
    """Typed arguments for traverse_technology_graph tool."""

    technology_name: str = Field(
        ..., min_length=1, max_length=128, description="Exact canonical technology name to traverse (e.g. 'Cloud Spanner')"
    )


class GetSolutionEvidenceDetailArgs(BaseModel):
    """Typed arguments for get_solution_evidence_detail tool."""

    card_id: str = Field(
        ...,
        min_length=3,
        max_length=128,
        description="Target card ID or dependency placeholder (e.g. '$task_1.card_id' or exact 'card_xxx')",
    )


class RetrievalSubtask(BaseModel):
    """Independently executable retrieval subtask with explicit dependencies."""

    task_id: str = Field(..., description="Unique subtask ID, e.g. 'task_1', 'task_2'")
    description: str = Field(..., description="Brief natural language rationale for the subtask")
    tool_name: TargetToolName = Field(..., description="Target retrieval tool to invoke")
    tool_args: dict[str, Any] = Field(..., description="Tool arguments matching tool's typed schema")
    depends_on: list[str] = Field(default_factory=list, description="IDs of tasks that must execute before this task")

    def get_typed_args(self) -> SearchApprovedSolutionsArgs | TraverseTechnologyGraphArgs | GetSolutionEvidenceDetailArgs:
        """Validates and returns the strongly typed argument model for this task."""
        if self.tool_name == TargetToolName.SEARCH_APPROVED_SOLUTIONS:
            return SearchApprovedSolutionsArgs(**self.tool_args)
        elif self.tool_name == TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH:
            return TraverseTechnologyGraphArgs(**self.tool_args)
        elif self.tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL:
            return GetSolutionEvidenceDetailArgs(**self.tool_args)
        else:
            raise ValueError(f"Unsupported tool name: {self.tool_name}")


class QueryPlan(BaseModel):
    """Complete, structured query plan generated for a user query."""

    plan_id: str = Field(..., description="Unique deterministic identifier for this query plan")
    original_query: str = Field(..., description="Original user prompt text")
    intent: QueryIntent = Field(..., description="Primary classified query intent")
    requires_clarification: bool = Field(
        default=False, description="True if query is too underspecified to retrieve verified knowledge"
    )
    clarification_prompt: str | None = Field(
        default=None, description="Guiding clarification prompt returned to user when clarification is needed"
    )
    extracted_entities: list[ValidatedEntity] = Field(
        default_factory=list, description="Extracted entities and technologies validated via TaxonomyMapper"
    )
    subtasks: list[RetrievalSubtask] = Field(
        default_factory=list, description="Ordered retrieval subtasks (max 4 per execution budget)"
    )
    execution_budget_seconds: float = Field(default=15.0, description="Execution timeout cap in seconds")
    max_tool_calls: int = Field(default=4, ge=0, le=4, description="Maximum number of tool calls allowed per turn")

    @model_validator(mode="after")
    def validate_clarification_subtasks(self) -> QueryPlan:
        """Enforce that clarification plans must execute zero retrieval calls."""
        if self.requires_clarification or self.intent == QueryIntent.CLARIFICATION_NEEDED:
            if len(self.subtasks) > 0:
                raise ValueError("Clarification plans must execute zero retrieval calls.")
        return self

