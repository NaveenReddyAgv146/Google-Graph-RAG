"""Context and Anaphora Resolver for Discovery Copilot (Phase 4.4).

Resolves conversational pronouns, demonstratives, and references to previous
customers, solution cards, and technologies using:
1. Current user query.
2. Structured ConversationMemory from ADK session.state.
3. Bounded sliding window of recent conversation turns.

Replaces fragile string slicing (prev_text[:60]). Returns clarification request
when references are genuinely ambiguous rather than arbitrarily guessing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from app.planning.conversation_memory import ConversationMemory
from app.planning.entity_extractor import EntityExtractor
from app.planning.models import EntityCategory, ValidatedEntity

logger = logging.getLogger("discovery_copilot.planning.context_resolver")

# Pronoun and anaphora patterns
CUSTOMER_PRONOUNS = [
    r"\b(they|them|their|the customer|that customer|this customer|the client|that client|the company)\b",
]
SOLUTION_PRONOUNS = [
    r"\b(it|that solution|this solution|that architecture|this architecture|the architecture|the system|the platform)\b",
]
TECHNOLOGY_PRONOUNS = [
    r"\b(that tech|that database|that engine|the database|the stack|the technology)\b",
]
EVIDENCE_PRONOUNS = [
    r"\b(evidence for that|proof for that|citations for that|quote for that|where was that written|for that claim)\b",
]

CARD_ID_REGEX = re.compile(r"\b(card_[a-f0-9]{8,64})\b")

# Maximum interaction turns (user+model pairs = 6 turns = 12 events) to consider
DEFAULT_MAX_TURNS_WINDOW = 6
# Character limit on conversational history to prevent context bloat (approx ~1000 tokens)
MAX_HISTORY_CHAR_LIMIT = 4000


class ResolvedContext(BaseModel):
    """Result of conversational anaphora and context resolution."""

    original_query: str
    resolved_query: str
    resolved_customer: str | None = None
    resolved_card_id: str | None = None
    resolved_technologies: list[str] = Field(default_factory=list)
    is_ambiguous: bool = False
    clarification_message: str | None = None
    applied_resolutions: list[str] = Field(default_factory=list)


class ContextResolver:
    """Resolves conversational anaphora using structured memory and sliding window turns."""

    def __init__(self, extractor: EntityExtractor | None = None) -> None:
        self.extractor = extractor or EntityExtractor()

    def resolve(
        self,
        query: str,
        memory: ConversationMemory,
        session_history: list[dict[str, str]] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS_WINDOW,
    ) -> ResolvedContext:
        """Resolves pronouns and references against structured memory and recent turns.

        Args:
            query: The current user message.
            memory: Validated ConversationMemory from ADK session.state.
            session_history: List of {"role": "user"|"model", "content": str}.
            max_turns: Number of interaction turns to inspect (default 6).

        Returns:
            ResolvedContext with augmented query, resolved entities, or clarification request.
        """
        clean_query = query.strip()
        q_lower = clean_query.lower()

        # Step 1: Extract entities explicitly mentioned in the CURRENT query
        current_entities = self.extractor.extract_entities(clean_query)
        current_customers = [
            e.canonical_name for e in current_entities if e.category == EntityCategory.CUSTOMER and e.canonical_name
        ]
        current_techs = [
            e.canonical_name for e in current_entities if e.category == EntityCategory.TECHNOLOGY and e.canonical_name
        ]
        explicit_card_match = CARD_ID_REGEX.search(clean_query)
        explicit_card_id = explicit_card_match.group(1) if explicit_card_match else None

        # Step 2: Bound conversation history (sliding window of max_turns pairs, with char limit)
        bounded_history: list[dict[str, str]] = []
        if session_history:
            # Keep last 2 * max_turns events
            sliced = session_history[-(max_turns * 2):]
            total_chars = 0
            # From newest to oldest, accumulate up to MAX_HISTORY_CHAR_LIMIT
            accumulated: list[dict[str, str]] = []
            for ev in reversed(sliced):
                c = ev.get("content", "")
                if total_chars + len(c) > MAX_HISTORY_CHAR_LIMIT and accumulated:
                    break
                accumulated.append(ev)
                total_chars += len(c)
            bounded_history = list(reversed(accumulated))

        # Step 3: Detect if the query contains anaphora / pronouns needing resolution
        has_cust_pronoun = any(re.search(pat, q_lower) for pat in CUSTOMER_PRONOUNS)
        has_sol_pronoun = any(re.search(pat, q_lower) for pat in SOLUTION_PRONOUNS)
        has_tech_pronoun = any(re.search(pat, q_lower) for pat in TECHNOLOGY_PRONOUNS)
        has_evid_pronoun = any(re.search(pat, q_lower) for pat in EVIDENCE_PRONOUNS)

        applied: list[str] = []
        resolved_customer: str | None = None
        resolved_card_id: str | None = explicit_card_id
        resolved_techs: list[str] = list(current_techs)

        # -----------------------------------------------------------------
        # Customer Resolution
        # -----------------------------------------------------------------
        if current_customers:
            # Query explicitly provides customer; override / set primary
            resolved_customer = current_customers[0]
        elif has_cust_pronoun or has_sol_pronoun or has_tech_pronoun or has_evid_pronoun:
            # Needs customer resolution from memory or history
            if len(memory.active_customers) == 1:
                resolved_customer = memory.active_customers[0]
                applied.append(f"customer: {resolved_customer}")
            elif len(memory.active_customers) > 1:
                # Ambiguous! Multiple customers recently discussed
                return ResolvedContext(
                    original_query=clean_query,
                    resolved_query=clean_query,
                    is_ambiguous=True,
                    clarification_message=(
                        f"We recently discussed multiple enterprise architectures: {', '.join(memory.active_customers)}. "
                        "Which customer's architecture are you referring to?"
                    ),
                )
            else:
                # No active customers in memory; check bounded history for customer mentions
                history_customers = self._find_customers_in_history(bounded_history)
                if len(history_customers) == 1:
                    resolved_customer = history_customers[0]
                    applied.append(f"customer (from history): {resolved_customer}")
                elif len(history_customers) > 1:
                    return ResolvedContext(
                        original_query=clean_query,
                        resolved_query=clean_query,
                        is_ambiguous=True,
                        clarification_message=(
                            f"Multiple customers were mentioned in our recent conversation: {', '.join(history_customers)}. "
                            "Could you clarify which customer you are inquiring about?"
                        ),
                    )

        # -----------------------------------------------------------------
        # Card ID / Solution Resolution
        # -----------------------------------------------------------------
        if not resolved_card_id:
            if has_sol_pronoun or has_evid_pronoun:
                if len(memory.active_card_ids) == 1:
                    resolved_card_id = memory.active_card_ids[0]
                    applied.append(f"card_id: {resolved_card_id}")
                elif len(memory.active_card_ids) > 1 and not current_customers and not has_cust_pronoun:
                    # If multiple cards are active and no customer qualification exists, check if all belong to same customer
                    # If ambiguous between multiple different solutions, ask
                    applied.append(f"card_id (most recent): {memory.active_card_ids[0]}")
                    resolved_card_id = memory.active_card_ids[0]

        # -----------------------------------------------------------------
        # Technology Continuity
        # -----------------------------------------------------------------
        if not resolved_techs and (has_tech_pronoun or has_sol_pronoun):
            if memory.active_technologies:
                # Pull verified technologies from memory
                resolved_techs = list(memory.active_technologies[:2])
                applied.append(f"technologies: {', '.join(resolved_techs)}")

        # -----------------------------------------------------------------
        # Synthesize Augmented Resolved Query
        # -----------------------------------------------------------------
        augmented_query = clean_query
        if resolved_customer and resolved_customer.lower() not in q_lower:
            # Replace pronouns or prepend customer name cleanly
            replaced = False
            for pat in CUSTOMER_PRONOUNS:
                if re.search(pat, augmented_query, flags=re.IGNORECASE):
                    augmented_query = re.sub(
                        pat, resolved_customer, augmented_query, count=1, flags=re.IGNORECASE
                    )
                    replaced = True
                    break
            if not replaced:
                augmented_query = f"{resolved_customer}: {augmented_query}"

        if resolved_card_id and resolved_card_id not in augmented_query:
            if has_evid_pronoun or "evidence" in q_lower or "citation" in q_lower or "proof" in q_lower:
                augmented_query = f"{augmented_query} (card: {resolved_card_id})"

        return ResolvedContext(
            original_query=clean_query,
            resolved_query=augmented_query,
            resolved_customer=resolved_customer,
            resolved_card_id=resolved_card_id,
            resolved_technologies=resolved_techs,
            is_ambiguous=False,
            clarification_message=None,
            applied_resolutions=applied,
        )

    def _find_customers_in_history(self, history: list[dict[str, str]]) -> list[str]:
        """Scans recent turns for customer entities in reverse chronological order."""
        found: list[str] = []
        for turn in reversed(history):
            content = turn.get("content", "")
            entities = self.extractor.extract_entities(content)
            for e in entities:
                if e.category == EntityCategory.CUSTOMER and e.canonical_name:
                    if e.canonical_name not in found:
                        found.append(e.canonical_name)
        return found
