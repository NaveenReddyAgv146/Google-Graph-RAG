"""Comprehensive Unit and Integration Tests for Phase 4.2 Reasoning & Query Planning.

Tests:
1. All 7 QueryIntents:
   - SOLUTION_DISCOVERY
   - TECHNICAL_LOOKUP
   - COMPARISON
   - ARCHITECTURE_EXPLORATION
   - EVIDENCE_VERIFICATION
   - FOLLOW_UP
   - CLARIFICATION_NEEDED
2. Entity & Technology Extraction:
   - Canonical alias resolution (e.g. 'docai' -> 'Document AI', 'spanner' -> 'Google Cloud Spanner').
   - Unknown technologies tagged is_canonical=False, tech_id=None (NEVER invented).
   - Customers (Uber, Canva, Dropbox) and domains extracted separately.
3. Query Decomposition:
   - Comparison decomposed into multi-customer or multi-tech subtasks.
   - Evidence verification decomposed into candidate lookup + evidence inspection with dependencies.
4. Ambiguous Queries:
   - Requires clarification with zero tool calls and helpful prompt.
5. Plan Validation & Invariants:
   - Maximum 4 tool calls enforced.
   - Cycle detection and self-referential dependency rejection.
   - Invalid / non-existent task dependencies rejected.
   - Dependency depth > 2 rejected.
   - Invalid tool names and arguments rejected.
6. Plan Orchestration & Execution:
   - Dynamic resolution of dependent card IDs from actual previous results.
   - Preserves ToolContext and immutable principal identity.
   - Timeout budget enforcement (15s).
   - Empty retrieval handling returning grounded negative message.
"""

import json
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock, patch

from google.adk.tools import ToolContext
from pydantic import ValidationError
import pytest

# Ensure sys.path includes project roots
REPO_ROOT = Path(__file__).resolve().parents[3]
DISCOVERY_DIR = Path(__file__).resolve().parents[2]
COMMON_SRC = REPO_ROOT / "common" / "src"

for p in [str(REPO_ROOT), str(DISCOVERY_DIR), str(COMMON_SRC)]:
    if p not in sys.path:
        sys.path.insert(0, p)

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
from app.planning.orchestrator import (
    PlanExecutionResult,
    PlanExecutionTimeoutError,
    PlanOrchestrator,
    PlanValidationError,
)
from app.planning.planner import QueryPlanner


# ============================================================================
# 1. ENTITY EXTRACTION & TAXONOMY TESTS
# ============================================================================


def test_entity_extractor_canonical_alias_resolution():
    """Verify technology aliases resolve deterministically to canonical names and TechIds."""
    extractor = EntityExtractor()
    query = "How did Canva use docai, spanner, and k8s for media processing?"
    entities = extractor.extract_entities(query)

    tech_map = {e.raw_mention.lower(): e for e in entities if e.category == EntityCategory.TECHNOLOGY}

    # Verify 'docai' resolved to Document AI
    assert "docai" in tech_map
    assert tech_map["docai"].canonical_name == "Document AI"
    assert tech_map["docai"].tech_id == "tech_document_ai"
    assert tech_map["docai"].is_canonical is True

    # Verify 'spanner' resolved to Cloud Spanner
    assert "spanner" in tech_map
    assert tech_map["spanner"].canonical_name == "Cloud Spanner"
    assert tech_map["spanner"].tech_id == "tech_cloud_spanner"
    assert tech_map["spanner"].is_canonical is True

    # Verify 'k8s' resolved to Kubernetes
    assert "k8s" in tech_map
    assert tech_map["k8s"].canonical_name == "Kubernetes"
    assert tech_map["k8s"].tech_id == "tech_kubernetes"
    assert tech_map["k8s"].is_canonical is True


def test_entity_extractor_customer_and_domain_separation():
    """Verify customers and domains are cleanly separated from technologies."""
    extractor = EntityExtractor()
    query = "Uber and Dropbox architecture for banking and storage"
    entities = extractor.extract_entities(query)

    customers = [e.canonical_name for e in entities if e.category == EntityCategory.CUSTOMER]
    assert "Uber" in customers
    assert "Dropbox" in customers

    domains = [e.raw_mention for e in entities if e.category == EntityCategory.BUSINESS_DOMAIN]
    assert "banking" in domains
    assert "storage" in domains


def test_entity_extractor_unknown_technology_never_invents_id():
    """Verify unknown technologies are marked non-canonical and never invent TechIds."""
    extractor = EntityExtractor()
    candidate = "QuantumHyperScaleDB"
    res = extractor.validate_unknown_technology(candidate)

    assert res.category == EntityCategory.TECHNOLOGY
    assert res.is_canonical is False
    assert res.tech_id is None
    assert res.canonical_name is None


# ============================================================================
# 2. QUERY PLANNER INTENT CLASSIFICATION TESTS (ALL 7 INTENTS)
# ============================================================================


def test_planner_intent_solution_discovery():
    """Verify broad architectural query classifies as SOLUTION_DISCOVERY."""
    planner = QueryPlanner()
    plan = planner.create_plan("How did customers achieve sub-10ms transaction latency?")
    assert plan.intent == QueryIntent.SOLUTION_DISCOVERY
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].tool_name == TargetToolName.SEARCH_APPROVED_SOLUTIONS
    assert plan.requires_clarification is False


def test_planner_intent_technical_lookup():
    """Verify technology-focused inquiry classifies as TECHNICAL_LOOKUP."""
    planner = QueryPlanner()
    plan = planner.create_plan("Which solutions use Google Cloud Spanner?")
    assert plan.intent == QueryIntent.TECHNICAL_LOOKUP
    assert len(plan.subtasks) >= 1
    # First subtask must traverse the graph for the canonical tech
    assert plan.subtasks[0].tool_name == TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH
    assert plan.subtasks[0].tool_args["technology_name"] == "Cloud Spanner"


def test_planner_intent_comparison():
    """Verify multi-customer comparison classifies as COMPARISON and decomposes into subtasks."""
    planner = QueryPlanner()
    plan = planner.create_plan("Compare how Uber and Canva approach data storage.")
    assert plan.intent == QueryIntent.COMPARISON
    assert len(plan.subtasks) == 2
    # Decomposed into two independent searches
    assert plan.subtasks[0].tool_name == TargetToolName.SEARCH_APPROVED_SOLUTIONS
    assert "Uber" in plan.subtasks[0].tool_args["query"]
    assert plan.subtasks[1].tool_name == TargetToolName.SEARCH_APPROVED_SOLUTIONS
    assert "Canva" in plan.subtasks[1].tool_args["query"]
    assert plan.subtasks[0].depends_on == []
    assert plan.subtasks[1].depends_on == []


def test_planner_intent_architecture_exploration():
    """Verify multi-hop relationship inquiry classifies as ARCHITECTURE_EXPLORATION."""
    planner = QueryPlanner()
    plan = planner.create_plan("Explore the graph topology connecting Cloud Spanner to other components.")
    assert plan.intent == QueryIntent.ARCHITECTURE_EXPLORATION
    tool_names = [t.tool_name for t in plan.subtasks]
    assert TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH in tool_names
    assert TargetToolName.SEARCH_APPROVED_SOLUTIONS in tool_names


def test_planner_intent_evidence_verification_with_explicit_card_id():
    """Verify explicit card evidence verification targets get_solution_evidence_detail."""
    planner = QueryPlanner()
    plan = planner.create_plan("Verify the citation and quote for card_f9f52f771219353bdcd834f6aa644ee426e6ba1459037bd8.")
    assert plan.intent == QueryIntent.EVIDENCE_VERIFICATION
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL
    assert plan.subtasks[0].tool_args["card_id"] == "card_f9f52f771219353bdcd834f6aa644ee426e6ba1459037bd8"


def test_planner_intent_evidence_verification_without_card_id_has_dependency():
    """Verify evidence verification without card ID generates search + dependent detail task."""
    planner = QueryPlanner()
    plan = planner.create_plan("Show me the exact quote and slide anchor proving 99.999% SLA.")
    assert plan.intent == QueryIntent.EVIDENCE_VERIFICATION
    assert len(plan.subtasks) == 2
    assert plan.subtasks[0].tool_name == TargetToolName.SEARCH_APPROVED_SOLUTIONS
    assert plan.subtasks[1].tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL
    assert plan.subtasks[1].depends_on == ["task_1"]
    assert plan.subtasks[1].tool_args["card_id"] == "$task_1.card_id"


def test_planner_intent_follow_up():
    """Verify follow-up with conversational context resolves anaphora."""
    planner = QueryPlanner()
    session_context = [
        {"role": "user", "content": "Tell me about UberCab architecture."},
        {"role": "model", "content": "UberCab deployed mobile routing using card_57ba5ac614fce0fffdc6bc82a84046be45e2b0293e797315."},
    ]
    plan = planner.create_plan("What was the outcome of deploying that?", session_context=session_context)
    assert plan.intent == QueryIntent.FOLLOW_UP
    assert len(plan.subtasks) >= 1
    # Dependent or direct detail task referencing the card from previous turn
    assert plan.subtasks[0].tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL
    assert plan.subtasks[0].tool_args["card_id"] == "card_57ba5ac614fce0fffdc6bc82a84046be45e2b0293e797315"


def test_planner_intent_clarification_zero_tool_calls():
    """Verify ambiguous, generic query returns CLARIFICATION_NEEDED with zero tool calls."""
    planner = QueryPlanner()
    for generic_query in ["Tell me about the system", "how does it work?", "help", "hello"]:
        plan = planner.create_plan(generic_query)
        assert plan.intent == QueryIntent.CLARIFICATION_NEEDED
        assert plan.requires_clarification is True
        assert len(plan.subtasks) == 0
        assert plan.max_tool_calls == 0
        assert plan.clarification_prompt is not None
        assert "specify" in plan.clarification_prompt.lower()


# ============================================================================
# 3. PLAN VALIDATOR & SAFETY INVARIANT TESTS
# ============================================================================


def test_plan_validator_max_tool_calls_cap():
    """Verify plan validator rejects plans exceeding 4 tool calls."""
    orchestrator = PlanOrchestrator()
    subtasks = [
        RetrievalSubtask(
            task_id=f"task_{i}",
            description=f"Subtask {i}",
            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
            tool_args={"query": f"query {i}"},
        )
        for i in range(1, 6)  # 5 tasks
    ]
    # 1. Pydantic level cap: max_tool_calls > 4 raises ValidationError
    with pytest.raises(ValidationError):
        QueryPlan(
            plan_id="plan_oversized",
            original_query="big query",
            intent=QueryIntent.SOLUTION_DISCOVERY,
            subtasks=subtasks,
            max_tool_calls=5,
        )

    # 2. Orchestrator level cap: >4 subtasks raises PlanValidationError
    plan_with_5_subtasks = QueryPlan(
        plan_id="plan_oversized",
        original_query="big query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=subtasks,
        max_tool_calls=4,
    )
    with pytest.raises(PlanValidationError, match="maximum tool calls limit"):
        orchestrator.validate_plan(plan_with_5_subtasks)


def test_plan_validator_duplicate_task_ids_rejected():
    """Verify plan validator detects duplicate task IDs."""
    orchestrator = PlanOrchestrator()
    subtasks = [
        RetrievalSubtask(
            task_id="task_1",
            description="First",
            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
            tool_args={"query": "q1"},
        ),
        RetrievalSubtask(
            task_id="task_1",  # Duplicate!
            description="Second",
            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
            tool_args={"query": "q2"},
        ),
    ]
    plan = QueryPlan(
        plan_id="plan_dup",
        original_query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=subtasks,
    )

    with pytest.raises(PlanValidationError, match="Duplicate task ID"):
        orchestrator.validate_plan(plan)


def test_plan_validator_cyclic_dependency_rejected():
    """Verify cyclic dependencies among tasks are detected and rejected."""
    orchestrator = PlanOrchestrator()
    subtasks = [
        RetrievalSubtask(
            task_id="task_1",
            description="First",
            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
            tool_args={"query": "q1"},
            depends_on=["task_2"],  # 1 depends on 2
        ),
        RetrievalSubtask(
            task_id="task_2",
            description="Second",
            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
            tool_args={"query": "q2"},
            depends_on=["task_1"],  # 2 depends on 1 -> CYCLE!
        ),
    ]
    plan = QueryPlan(
        plan_id="plan_cycle",
        original_query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=subtasks,
    )

    with pytest.raises(PlanValidationError, match="Cyclic dependency detected"):
        orchestrator.validate_plan(plan)


def test_plan_validator_nonexistent_dependency_rejected():
    """Verify task referencing a non-existent task_id in depends_on is rejected."""
    orchestrator = PlanOrchestrator()
    subtasks = [
        RetrievalSubtask(
            task_id="task_1",
            description="First",
            tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
            tool_args={"query": "q1"},
            depends_on=["task_missing_99"],
        ),
    ]
    plan = QueryPlan(
        plan_id="plan_missing_dep",
        original_query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=subtasks,
    )

    with pytest.raises(PlanValidationError, match="depends on non-existent task"):
        orchestrator.validate_plan(plan)


def test_plan_validator_excessive_dependency_depth_rejected():
    """Verify dependency chains deeper than depth 2 are rejected."""
    orchestrator = PlanOrchestrator()
    subtasks = [
        RetrievalSubtask(task_id="task_1", description="1", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "1"}),
        RetrievalSubtask(task_id="task_2", description="2", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "2"}, depends_on=["task_1"]),
        RetrievalSubtask(task_id="task_3", description="3", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "3"}, depends_on=["task_2"]),
        # task_4 has depth 3 (1 -> 2 -> 3 -> 4)
        RetrievalSubtask(task_id="task_4", description="4", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "4"}, depends_on=["task_3"]),
    ]
    plan = QueryPlan(
        plan_id="plan_deep",
        original_query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=subtasks,
    )

    with pytest.raises(PlanValidationError, match="Dependency depth exceeds maximum limit of 2"):
        orchestrator.validate_plan(plan)


def test_plan_validator_invalid_tool_arguments_rejected():
    """Verify subtasks with missing or invalid tool arguments are caught."""
    orchestrator = PlanOrchestrator()
    subtasks = [
        RetrievalSubtask(
            task_id="task_1",
            description="Bad args",
            tool_name=TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH,
            tool_args={"wrong_key": "some_value"},  # Missing 'technology_name'
        ),
    ]
    plan = QueryPlan(
        plan_id="plan_bad_args",
        original_query="query",
        intent=QueryIntent.TECHNICAL_LOOKUP,
        subtasks=subtasks,
    )

    with pytest.raises(PlanValidationError, match="Invalid arguments for tool"):
        orchestrator.validate_plan(plan)


# ============================================================================
# 4. ORCHESTRATION & EXECUTION TESTS
# ============================================================================


def test_orchestrator_clarification_executes_zero_tool_calls():
    """Verify executing a clarification plan immediately returns without calling tools."""
    orchestrator = PlanOrchestrator()
    plan = QueryPlan(
        plan_id="plan_clarify",
        original_query="help",
        intent=QueryIntent.CLARIFICATION_NEEDED,
        requires_clarification=True,
        clarification_prompt="What technology would you like to explore?",
        subtasks=[],
        max_tool_calls=0,
    )

    result = orchestrator.execute_plan(plan)
    assert result.status == "clarification"
    assert result.total_tool_calls == 0
    assert result.solutions == []
    assert "technology" in result.message


def test_orchestrator_resolves_dependent_card_id(monkeypatch):
    """Verify orchestrator dynamically resolves $task_1.card_id from actual output of task_1."""
    import app.planning.orchestrator as orch_mod

    # Mock tool responses
    mock_search = MagicMock(return_value=json.dumps({
        "status": "success",
        "count": 1,
        "solutions": [{"card_id": "card_resolved_123", "project_name": "Resolved App"}]
    }))
    mock_detail = MagicMock(return_value=json.dumps({
        "status": "success",
        "card_id": "card_resolved_123",
        "project_name": "Resolved App",
        "verification_status": "APPROVED",
        "citations": [{"evidence_id": "ev_1", "source_chunk_text": "Verbatim quote"}]
    }))

    monkeypatch.setattr(orch_mod, "search_approved_solutions", mock_search)
    monkeypatch.setattr(orch_mod, "get_solution_evidence_detail", mock_detail)

    orchestrator = PlanOrchestrator()
    plan = QueryPlan(
        plan_id="plan_dep_test",
        original_query="show quote for top solution",
        intent=QueryIntent.EVIDENCE_VERIFICATION,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Find top card",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "top solution"},
            ),
            RetrievalSubtask(
                task_id="task_2",
                description="Get evidence",
                tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                tool_args={"card_id": "$task_1.card_id"},
                depends_on=["task_1"],
            ),
        ]
    )

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "test@enterprise.com"

    result = orchestrator.execute_plan(plan, tool_context=ctx)

    assert result.status == "success"
    assert result.total_tool_calls == 2
    # Verify mock_detail was called with the actual resolved card ID, NOT the string '$task_1.card_id'
    mock_detail.assert_called_once_with(card_id="card_resolved_123", tool_context=ctx)


def test_orchestrator_preserves_tool_context_and_principal_identity(monkeypatch):
    """Verify tool_context is faithfully propagated to Phase 4.1 discovery tools."""
    import app.planning.orchestrator as orch_mod

    mock_search = MagicMock(return_value=json.dumps({"status": "success", "solutions": []}))
    monkeypatch.setattr(orch_mod, "search_approved_solutions", mock_search)

    orchestrator = PlanOrchestrator()
    plan = QueryPlan(
        plan_id="plan_auth_test",
        original_query="find app",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "app"},
            )
        ]
    )

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "verified.architect@enterprise.com"

    orchestrator.execute_plan(plan, tool_context=ctx)
    mock_search.assert_called_once_with(query="app", technologies=None, tool_context=ctx)


def test_orchestrator_empty_result_handling(monkeypatch):
    """Verify empty retrieval results return grounded empty message without infinite retry."""
    import app.planning.orchestrator as orch_mod

    mock_search = MagicMock(return_value=json.dumps({
        "status": "empty",
        "message": "No human-approved solutions found.",
        "solutions": []
    }))
    monkeypatch.setattr(orch_mod, "search_approved_solutions", mock_search)

    orchestrator = PlanOrchestrator()
    plan = QueryPlan(
        plan_id="plan_empty_test",
        original_query="nonexistent solution",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "nonexistent solution"},
            )
        ]
    )

    result = orchestrator.execute_plan(plan, tool_context=None)
    assert result.status == "empty"
    assert result.solutions == []
    assert "No human-approved solutions found" in result.message


def test_orchestrator_timeout_budget_enforcement(monkeypatch):
    """Verify orchestrator aborts with PlanExecutionTimeoutError when execution exceeds budget."""
    import app.planning.orchestrator as orch_mod

    def slow_tool(**kwargs):
        time.sleep(0.1)
        return json.dumps({"status": "success", "solutions": []})

    monkeypatch.setattr(orch_mod, "search_approved_solutions", slow_tool)

    # Set orchestrator timeout to 0.05 seconds
    orchestrator = PlanOrchestrator(timeout_seconds=0.05)
    plan = QueryPlan(
        plan_id="plan_timeout_test",
        original_query="slow query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(task_id="task_1", description="1", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "1"}),
            RetrievalSubtask(task_id="task_2", description="2", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "2"}),
        ]
    )

    with pytest.raises(PlanExecutionTimeoutError, match="timed out"):
        orchestrator.execute_plan(plan)


# ============================================================================
# 5. PHASE 4.2 ACCEPTANCE AUDIT INVARIANT REGRESSION TESTS
# ============================================================================


def test_invariant1_unknown_tech_cannot_trigger_graph_traversal():
    """Invariant 1: Non-canonical/unknown technologies use approved search, never canonical GQL graph traversal."""
    planner = QueryPlanner()
    plan = planner.create_plan("Which architectures use QuantumHyperScaleDB?")
    
    # Planner must use SEARCH_APPROVED_SOLUTIONS, never TRAVERSE_TECHNOLOGY_GRAPH
    tool_names = [s.tool_name for s in plan.subtasks]
    assert TargetToolName.SEARCH_APPROVED_SOLUTIONS in tool_names
    assert TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH not in tool_names

    # If an adversarial plan directly crafts a TRAVERSE_TECHNOLOGY_GRAPH subtask with an unknown tech,
    # PlanOrchestrator must reject it with PlanValidationError
    orchestrator = PlanOrchestrator()
    invalid_plan = QueryPlan(
        plan_id="plan_invalid_gql",
        original_query="hack query",
        intent=QueryIntent.TECHNICAL_LOOKUP,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Invalid GQL traversal",
                tool_name=TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH,
                tool_args={"technology_name": "QuantumHyperScaleDB"},
            )
        ],
    )
    with pytest.raises(PlanValidationError, match="Non-canonical/unknown technology"):
        orchestrator.validate_plan(invalid_plan)


def test_invariant2_malformed_and_undeclared_card_id_rejected():
    """Invariant 2: Reject malformed dynamic references, undeclared dependencies, and fabricated IDs."""
    orchestrator = PlanOrchestrator()

    # 1. Malformed dynamic reference ($task_1.wrong_prop)
    malformed_plan = QueryPlan(
        plan_id="plan_malformed_ref",
        original_query="query",
        intent=QueryIntent.EVIDENCE_VERIFICATION,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "q"},
            ),
            RetrievalSubtask(
                task_id="task_2",
                description="Malformed ref",
                tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                tool_args={"card_id": "$task_1.invalid_field"},
                depends_on=["task_1"],
            ),
        ],
    )
    with pytest.raises(PlanValidationError, match="Malformed dynamic reference"):
        orchestrator.validate_plan(malformed_plan)

    # 2. Dynamic reference with undeclared dependency
    undeclared_dep_plan = QueryPlan(
        plan_id="plan_undeclared_dep",
        original_query="query",
        intent=QueryIntent.EVIDENCE_VERIFICATION,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "q"},
            ),
            RetrievalSubtask(
                task_id="task_2",
                description="Ref without depends_on",
                tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                tool_args={"card_id": "$task_1.card_id"},
                depends_on=[],  # Missing task_1!
            ),
        ],
    )
    with pytest.raises(PlanValidationError, match="references undeclared dependency"):
        orchestrator.validate_plan(undeclared_dep_plan)

    # 3. Fabricated static card ID
    fabricated_id_plan = QueryPlan(
        plan_id="plan_fabricated_id",
        original_query="query",
        intent=QueryIntent.EVIDENCE_VERIFICATION,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Fabricated ID",
                tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                tool_args={"card_id": "fabricated_id_not_a_valid_hash"},
            ),
        ],
    )
    with pytest.raises(PlanValidationError, match="Invalid or fabricated card_id"):
        orchestrator.validate_plan(fabricated_id_plan)


def test_invariant2_failed_parent_output_skips_cleanly_without_inventing_id(monkeypatch):
    """Invariant 2: When declared parent task yields no valid card ID, dependent task skips cleanly."""
    import app.planning.orchestrator as orch_mod

    mock_search = MagicMock(return_value=json.dumps({"status": "error", "message": "Backend failure"}))
    mock_detail = MagicMock()

    monkeypatch.setattr(orch_mod, "search_approved_solutions", mock_search)
    monkeypatch.setattr(orch_mod, "get_solution_evidence_detail", mock_detail)

    orchestrator = PlanOrchestrator()
    plan = QueryPlan(
        plan_id="plan_parent_failed",
        original_query="query",
        intent=QueryIntent.EVIDENCE_VERIFICATION,
        subtasks=[
            RetrievalSubtask(
                task_id="task_1",
                description="Search parent",
                tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS,
                tool_args={"query": "query"},
            ),
            RetrievalSubtask(
                task_id="task_2",
                description="Dependent detail",
                tool_name=TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL,
                tool_args={"card_id": "$task_1.card_id"},
                depends_on=["task_1"],
            ),
        ],
    )

    result = orchestrator.execute_plan(plan, tool_context=None)
    assert result.subtask_results["task_2"]["status"] == "skipped"
    # Crucial: child tool was NEVER called with a fabricated ID
    mock_detail.assert_not_called()


def test_invariant3_timeout_cancels_subsequent_tool_calls(monkeypatch):
    """Invariant 3: Real deadline enforcement terminates timed-out operations and prevents subsequent calls."""
    import app.planning.orchestrator as orch_mod

    def slow_first_tool(**kwargs):
        time.sleep(0.15)
        return json.dumps({"status": "success", "solutions": []})

    mock_second_tool = MagicMock()

    monkeypatch.setattr(orch_mod, "search_approved_solutions", slow_first_tool)
    monkeypatch.setattr(orch_mod, "traverse_technology_graph", mock_second_tool)

    orchestrator = PlanOrchestrator(timeout_seconds=0.05)
    plan = QueryPlan(
        plan_id="plan_realtime_timeout",
        original_query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtasks=[
            RetrievalSubtask(task_id="task_1", description="Slow", tool_name=TargetToolName.SEARCH_APPROVED_SOLUTIONS, tool_args={"query": "q1"}),
            RetrievalSubtask(task_id="task_2", description="Should never run", tool_name=TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH, tool_args={"technology_name": "Cloud Spanner"}),
        ],
        execution_budget_seconds=0.05,
    )

    with pytest.raises(PlanExecutionTimeoutError, match="timed out"):
        orchestrator.execute_plan(plan)

    # Invariant 3: No additional calls after timeout!
    mock_second_tool.assert_not_called()

