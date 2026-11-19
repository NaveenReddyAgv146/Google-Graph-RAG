"""Deterministic Query Plan Validator and Execution Orchestrator (Phase 4.2).

Enforces:
1. Allow only the 3 existing Phase 4.1 discovery tools.
2. Hard limits: Maximum 4 tool calls per turn, maximum dependency depth of 2.
3. Execution timeout cap of 15 seconds.
4. Cycle detection, invalid dependency detection, and duplicate task ID prevention.
5. Dynamic resolution of dependent card IDs exclusively from actual previous tool results (never invented).
6. Fail-closed error handling and grounded empty results when retrieval yields zero matches.
7. Preserves immutable principal identity and ToolContext propagation.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
from typing import Any

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field

from app.planning.models import (
    GetSolutionEvidenceDetailArgs,
    QueryIntent,
    QueryPlan,
    RetrievalSubtask,
    SearchApprovedSolutionsArgs,
    TargetToolName,
    TraverseTechnologyGraphArgs,
)
from app.tools.discovery_tools import (
    get_solution_evidence_detail,
    search_approved_solutions,
    traverse_technology_graph,
)

logger = logging.getLogger("discovery_copilot.planning.orchestrator")

MAX_TOOL_CALLS_LIMIT = 4
CARD_ID_PATTERN = re.compile(r"^card_[a-zA-Z0-9_-]{8,64}$")
DYNAMIC_REF_PATTERN = re.compile(r"^\$([a-zA-Z0-9_]+)\.card_id$")
MAX_DEPENDENCY_DEPTH = 2
EXECUTION_TIMEOUT_SECONDS = 15.0


class PlanValidationError(Exception):
    """Raised when a QueryPlan violates execution limits, schema constraints, or safety invariants."""


class PlanExecutionTimeoutError(Exception):
    """Raised when query execution exceeds the 15-second budget."""


class PlanExecutionResult(BaseModel):
    """Outcome of orchestrating a QueryPlan."""

    status: str = Field(..., description="'success', 'empty', or 'clarification'")
    message: str | None = None
    plan_id: str
    intent: QueryIntent
    subtask_results: dict[str, Any] = Field(default_factory=dict)
    solutions: list[dict[str, Any]] = Field(default_factory=list)
    total_tool_calls: int = 0
    duration_seconds: float = 0.0


class PlanOrchestrator:
    """Validates and executes structured QueryPlans against Phase 4.1 discovery tools."""

    def __init__(self, timeout_seconds: float = EXECUTION_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def validate_plan(self, plan: QueryPlan) -> list[RetrievalSubtask]:
        """Validates query plan against strict limits, topological dependencies, and tool contracts.

        Returns topologically ordered list of subtasks.
        """
        # 1. Clarification Check
        if plan.requires_clarification or plan.intent == QueryIntent.CLARIFICATION_NEEDED:
            if len(plan.subtasks) > 0:
                raise PlanValidationError("Clarification plans must execute zero retrieval calls.")
            return []

        # 2. Maximum Tool Call Cap
        if len(plan.subtasks) > MAX_TOOL_CALLS_LIMIT:
            raise PlanValidationError(
                f"QueryPlan exceeds maximum tool calls limit ({len(plan.subtasks)} > {MAX_TOOL_CALLS_LIMIT})."
            )
        if plan.max_tool_calls > MAX_TOOL_CALLS_LIMIT:
            raise PlanValidationError(
                f"QueryPlan max_tool_calls exceeds limit ({plan.max_tool_calls} > {MAX_TOOL_CALLS_LIMIT})."
            )

        task_map: dict[str, RetrievalSubtask] = {}
        for t in plan.subtasks:
            # Check duplicate task IDs
            if t.task_id in task_map:
                raise PlanValidationError(f"Duplicate task ID '{t.task_id}' detected in QueryPlan.")
            task_map[t.task_id] = t

            # Validate tool name against allowlist
            if t.tool_name not in TargetToolName:
                raise PlanValidationError(f"Invalid tool '{t.tool_name}' in task '{t.task_id}'.")

            # Validate typed arguments model
            try:
                t.get_typed_args()
            except Exception as exc:
                raise PlanValidationError(
                    f"Invalid arguments for tool '{t.tool_name}' in task '{t.task_id}': {exc}"
                ) from exc

            # Invariant 1: Non-canonical/unknown technologies cannot trigger canonical GQL graph traversal
            if t.tool_name == TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH:
                tech_arg = t.tool_args.get("technology_name", "").strip()
                from common.taxonomy.taxonomy_mapper import build_default_catalog
                catalog = build_default_catalog()
                if not catalog.lookup(tech_arg):
                    raise PlanValidationError(
                        f"Non-canonical/unknown technology '{tech_arg}' cannot trigger canonical GQL graph traversal in task '{t.task_id}'."
                    )

            # Invariant 2: Dynamic references must follow $task_X.card_id and declared in depends_on; static card_ids must be valid format
            if t.tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL:
                cid = t.tool_args.get("card_id", "").strip()
                if cid.startswith("$"):
                    match = DYNAMIC_REF_PATTERN.match(cid)
                    if not match:
                        raise PlanValidationError(
                            f"Malformed dynamic reference '{cid}' in task '{t.task_id}'. Expected format '$task_X.card_id'."
                        )
                    dep_task_id = match.group(1)
                    if dep_task_id not in t.depends_on:
                        raise PlanValidationError(
                            f"Dynamic reference '{cid}' in task '{t.task_id}' references undeclared dependency '{dep_task_id}'. "
                            f"Task must explicitly declare '{dep_task_id}' in depends_on."
                        )
                else:
                    if not CARD_ID_PATTERN.match(cid):
                        raise PlanValidationError(
                            f"Invalid or fabricated card_id '{cid}' in task '{t.task_id}'. "
                            f"Expected valid format 'card_<hash>'."
                        )

        # 3. Dependency Validation, Cycle Detection & Depth Checks
        in_degree: dict[str, int] = {tid: 0 for tid in task_map}
        adj_list: dict[str, list[str]] = {tid: [] for tid in task_map}

        for tid, task in task_map.items():
            for dep in task.depends_on:
                if dep not in task_map:
                    raise PlanValidationError(
                        f"Task '{tid}' depends on non-existent task '{dep}'."
                    )
                if dep == tid:
                    raise PlanValidationError(f"Self-referential dependency detected in task '{tid}'.")
                adj_list[dep].append(tid)
                in_degree[tid] += 1

        # Kahn's algorithm for topological sort and cycle detection
        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        ordered_tids: list[str] = []

        # Depth tracking: depth of each node in DAG
        node_depth: dict[str, int] = {tid: 0 for tid in queue}

        while queue:
            curr = queue.pop(0)
            ordered_tids.append(curr)
            curr_depth = node_depth[curr]

            for neighbor in adj_list[curr]:
                in_degree[neighbor] -= 1
                new_depth = curr_depth + 1
                if new_depth > node_depth.get(neighbor, 0):
                    node_depth[neighbor] = new_depth

                if new_depth > MAX_DEPENDENCY_DEPTH:
                    raise PlanValidationError(
                        f"Dependency depth exceeds maximum limit of {MAX_DEPENDENCY_DEPTH} at task '{neighbor}'."
                    )

                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(ordered_tids) != len(task_map):
            raise PlanValidationError("Cyclic dependency detected among retrieval subtasks.")

        return [task_map[tid] for tid in ordered_tids]

    def execute_plan(
        self,
        plan: QueryPlan,
        tool_context: ToolContext | None = None,
    ) -> PlanExecutionResult:
        """Executes a validated QueryPlan using Phase 4.1 retrieval tools within a 15-second budget."""
        start_time = time.monotonic()

        # 1. Handle Clarification
        if plan.requires_clarification or plan.intent == QueryIntent.CLARIFICATION_NEEDED:
            return PlanExecutionResult(
                status="clarification",
                message=plan.clarification_prompt or "Please provide more details on your inquiry.",
                plan_id=plan.plan_id,
                intent=plan.intent,
                subtask_results={},
                solutions=[],
                total_tool_calls=0,
                duration_seconds=round(time.monotonic() - start_time, 3),
            )

        # 2. Validate Plan & Obtain Topological Execution Order
        effective_timeout = min(self.timeout_seconds, plan.execution_budget_seconds)
        ordered_subtasks = self.validate_plan(plan)
        completed_results: dict[str, Any] = {}
        all_solutions: list[dict[str, Any]] = []
        seen_card_ids: set[str] = set()

        # 3. Sequential Execution with Budget Cap and Real-Time Deadline
        for task in ordered_subtasks:
            elapsed = time.monotonic() - start_time
            remaining = effective_timeout - elapsed
            if remaining <= 0:
                raise PlanExecutionTimeoutError(
                    f"QueryPlan execution timed out after {elapsed:.2f}s (budget: {effective_timeout}s)."
                )

            # Resolve dependent card IDs strictly from actual previous outputs
            resolved_args = dict(task.tool_args)
            if task.tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL:
                card_id_val = resolved_args.get("card_id", "").strip()
                if card_id_val.startswith("$"):
                    match = DYNAMIC_REF_PATTERN.match(card_id_val)
                    if not match:
                        raise PlanValidationError(f"Malformed dynamic reference: '{card_id_val}'")
                    dep_task_id = match.group(1)
                    if dep_task_id not in task.depends_on:
                        raise PlanValidationError(
                            f"Task '{task.task_id}' references undeclared dependency '{dep_task_id}'"
                        )

                    # Lookup parent task result
                    dep_output = completed_results.get(dep_task_id)
                    resolved_cid = None
                    if dep_output and isinstance(dep_output, dict) and dep_output.get("status") == "success":
                        solutions_list = dep_output.get("solutions", [])
                        if solutions_list and isinstance(solutions_list[0], dict):
                            cand_cid = solutions_list[0].get("card_id", "")
                            if CARD_ID_PATTERN.match(cand_cid):
                                resolved_cid = cand_cid

                    if not resolved_cid:
                        logger.warning(
                            "Dependent task '%s' did not produce a valid solution card for task '%s'. Skipping.",
                            dep_task_id,
                            task.task_id,
                        )
                        completed_results[task.task_id] = {
                            "status": "skipped",
                            "message": f"Dependent task '{dep_task_id}' yielded no valid card_id.",
                        }
                        continue

                    resolved_args["card_id"] = resolved_cid

            # Dispatch tool call using existing Phase 4.1 tools under deadline
            def _dispatch_tool():
                if task.tool_name == TargetToolName.SEARCH_APPROVED_SOLUTIONS:
                    return search_approved_solutions(
                        query=resolved_args["query"],
                        technologies=resolved_args.get("technologies"),
                        tool_context=tool_context,
                    )
                elif task.tool_name == TargetToolName.TRAVERSE_TECHNOLOGY_GRAPH:
                    return traverse_technology_graph(
                        technology_name=resolved_args["technology_name"],
                        tool_context=tool_context,
                    )
                elif task.tool_name == TargetToolName.GET_SOLUTION_EVIDENCE_DETAIL:
                    return get_solution_evidence_detail(
                        card_id=resolved_args["card_id"],
                        tool_context=tool_context,
                    )
                raise ValueError(f"Unknown tool: {task.tool_name}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_dispatch_tool)
                try:
                    raw_res_str = future.result(timeout=max(0.001, remaining))
                except concurrent.futures.TimeoutError as exc:
                    elapsed_total = time.monotonic() - start_time
                    raise PlanExecutionTimeoutError(
                        f"Subtask '{task.task_id}' timed out after {elapsed_total:.2f}s "
                        f"(effective budget: {effective_timeout}s)."
                    ) from exc

            # Parse tool output
            try:
                parsed_res = json.loads(raw_res_str)
            except Exception as e:
                logger.error("Failed to parse tool output JSON in task '%s': %s", task.task_id, e)
                parsed_res = {"status": "error", "message": str(e)}

            completed_results[task.task_id] = parsed_res

            # Accumulate solutions
            if isinstance(parsed_res, dict) and parsed_res.get("status") == "success":
                # For search or traverse
                if "solutions" in parsed_res and isinstance(parsed_res["solutions"], list):
                    for sol in parsed_res["solutions"]:
                        cid = sol.get("card_id")
                        if cid and cid not in seen_card_ids:
                            seen_card_ids.add(cid)
                            all_solutions.append(sol)
                # For evidence detail
                elif "card_id" in parsed_res:
                    cid = parsed_res.get("card_id")
                    if cid and cid not in seen_card_ids:
                        seen_card_ids.add(cid)
                        all_solutions.append(parsed_res)

        total_duration = round(time.monotonic() - start_time, 3)

        # 4. Handle Empty Results
        if not all_solutions:
            return PlanExecutionResult(
                status="empty",
                message="No human-approved solutions found matching query and security permissions.",
                plan_id=plan.plan_id,
                intent=plan.intent,
                subtask_results=completed_results,
                solutions=[],
                total_tool_calls=len(ordered_subtasks),
                duration_seconds=total_duration,
            )

        return PlanExecutionResult(
            status="success",
            message=f"Retrieved {len(all_solutions)} approved solution card(s).",
            plan_id=plan.plan_id,
            intent=plan.intent,
            subtask_results=completed_results,
            solutions=all_solutions,
            total_tool_calls=len(ordered_subtasks),
            duration_seconds=total_duration,
        )

    def execute_plan_with_synthesis(
        self,
        plan: QueryPlan,
        tool_context: ToolContext | None = None,
        evidence_selector: Any | None = None,
        synthesizer: Any | None = None,
        validator: Any | None = None,
    ) -> ValidatedSynthesisResponse:
        """Executes the complete Phase 4.3 End-to-End Pipeline:

        Plan Execution -> EvidenceSelector (Adaptive MMR) -> GroundedAnswerSynthesizer -> CitationValidator.
        """
        from app.synthesis.citation_validator import CitationValidator
        from app.synthesis.evidence_selector import EvidenceSelector
        from app.synthesis.models import ValidatedSynthesisResponse
        from app.synthesis.synthesizer import GroundedAnswerSynthesizer

        ev_selector = evidence_selector or EvidenceSelector()
        ans_synthesizer = synthesizer or GroundedAnswerSynthesizer()
        cit_validator = validator or CitationValidator()

        # Step 1: Execute query plan
        plan_result = self.execute_plan(plan, tool_context=tool_context)

        # Step 2: Handle clarification
        if plan_result.status == "clarification":
            clarification_msg = plan.clarification_prompt or plan_result.message or "Please clarify your inquiry."
            return ValidatedSynthesisResponse(
                is_valid=True,
                summary=clarification_msg,
                validated_claims=[],
                rejected_claims=[],
                insufficient_evidence_notes=[clarification_msg],
                unsupported_aspects=[],
                validation_details=[],
                precision=1.0,
                quote_fidelity=1.0,
                misattribution_count=0,
                latency_ms=round(plan_result.duration_seconds * 1000.0, 2),
            )

        # Step 3: Handle empty results
        if plan_result.status == "empty" or not plan_result.solutions:
            empty_msg = plan_result.message or "No human-approved solutions found matching query."
            return ValidatedSynthesisResponse(
                is_valid=True,
                summary=empty_msg,
                validated_claims=[],
                rejected_claims=[],
                insufficient_evidence_notes=[empty_msg],
                unsupported_aspects=[plan.original_query],
                validation_details=[],
                precision=1.0,
                quote_fidelity=1.0,
                misattribution_count=0,
                latency_ms=round(plan_result.duration_seconds * 1000.0, 2),
            )

        # Step 4: Evidence Selection & Adaptive MMR
        evidence_pack = ev_selector.select_evidence(
            query=plan.original_query,
            intent=plan.intent,
            subtask_results=plan_result.subtask_results,
        )

        # Step 5: Grounded Answer Synthesis
        synthesis_result = ans_synthesizer.synthesize(
            query=plan.original_query,
            evidence_pack=evidence_pack,
        )

        # Step 6: Deterministic Citation Validation
        validated_response = cit_validator.validate_synthesis(
            synthesis_result=synthesis_result,
            evidence_pack=evidence_pack,
        )

        return validated_response


