"""Structured Conversation Memory for Discovery Copilot (Phase 4.4).

Maintains bounded working memory in ADK session.state across multi-turn interactions.
Updated EXCLUSIVELY from Phase 4.3 validated response data (ValidatedSynthesisResponse).
Never stores unvalidated Gemini claims or arbitrary client-supplied facts.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from app.planning.models import QueryIntent
from app.synthesis.models import ValidatedSynthesisResponse

logger = logging.getLogger("discovery_copilot.planning.conversation_memory")

# Limits to bound session memory size and prevent memory bloat
MAX_ACTIVE_CUSTOMERS = 5
MAX_ACTIVE_CARD_IDS = 10
MAX_ACTIVE_TECHS = 10
MAX_UNRESOLVED_QUESTIONS = 5
DEFAULT_MEMORY_TTL_SECONDS = 3600.0 * 24.0  # 24-hour expiration for inactive sessions

CARD_ID_REGEX = re.compile(r"^card_[a-f0-9]{8,64}$")


class ConversationMemory(BaseModel):
    """Structured, bounded conversation working memory stored in session.state."""

    active_customers: list[str] = Field(default_factory=list)
    active_card_ids: list[str] = Field(default_factory=list)
    active_technologies: list[str] = Field(default_factory=list)
    last_intent: QueryIntent | None = None
    unresolved_questions: list[str] = Field(default_factory=list)
    last_turn_timestamp: float = Field(default_factory=time.time)

    def is_expired(self, ttl_seconds: float = DEFAULT_MEMORY_TTL_SECONDS) -> bool:
        """Returns True if the memory has been inactive longer than the TTL."""
        return (time.time() - self.last_turn_timestamp) > ttl_seconds

    def update_from_validated_response(
        self,
        validated_response: ValidatedSynthesisResponse,
        intent: QueryIntent | None = None,
    ) -> None:
        """Updates memory state exclusively using verified evidence from ValidatedSynthesisResponse.

        Only approved, verified claims and cards enter memory. Unvalidated model claims are ignored.
        """
        self.last_turn_timestamp = time.time()
        if intent is not None:
            self.last_intent = intent

        # Extract customers, cards, and technologies exclusively from validated claims
        new_cards: list[str] = []
        new_customers: list[str] = []
        new_techs: list[str] = []

        for claim in validated_response.validated_claims:
            cust = claim.customer_name or getattr(claim, "customer_attribution", None)
            if cust and cust.strip():
                clean_cust = cust.strip()
                if clean_cust not in new_customers:
                    new_customers.append(clean_cust)

            if claim.card_id and CARD_ID_REGEX.match(claim.card_id):
                if claim.card_id not in new_cards:
                    new_cards.append(claim.card_id)

            refs = claim.evidence_refs or getattr(claim, "citations", [])
            for ref in refs:
                if ref.card_id and CARD_ID_REGEX.match(ref.card_id):
                    if ref.card_id not in new_cards:
                        new_cards.append(ref.card_id)
                ref_cust = getattr(ref, "customer_name", None)
                if ref_cust and ref_cust.strip() and ref_cust.strip() not in new_customers:
                    new_customers.append(ref_cust.strip())

        # Merge and deduplicate preserving recency (new items at the front)
        for c in reversed(new_customers):
            if c in self.active_customers:
                self.active_customers.remove(c)
            self.active_customers.insert(0, c)
        self.active_customers = self.active_customers[:MAX_ACTIVE_CUSTOMERS]

        for cid in reversed(new_cards):
            if cid in self.active_card_ids:
                self.active_card_ids.remove(cid)
            self.active_card_ids.insert(0, cid)
        self.active_card_ids = self.active_card_ids[:MAX_ACTIVE_CARD_IDS]

        # Extract technologies from evidence pack if present in validated response
        # Notice: ValidatedSynthesisResponse does not directly hold the EvidencePack,
        # but technology mentions can be preserved if known from validated entities.
        self.active_technologies = self.active_technologies[:MAX_ACTIVE_TECHS]

        # Check for insufficient evidence notes and register unresolved questions if any
        if validated_response.insufficient_evidence_notes:
            for note in validated_response.insufficient_evidence_notes:
                if note not in self.unresolved_questions:
                    self.unresolved_questions.insert(0, note)
            self.unresolved_questions = self.unresolved_questions[:MAX_UNRESOLVED_QUESTIONS]

    def add_active_technologies(self, technologies: list[str]) -> None:
        """Adds verified canonical technologies to active memory."""
        for tech in reversed(technologies):
            clean_tech = tech.strip()
            if clean_tech:
                if clean_tech in self.active_technologies:
                    self.active_technologies.remove(clean_tech)
                self.active_technologies.insert(0, clean_tech)
        self.active_technologies = self.active_technologies[:MAX_ACTIVE_TECHS]

    @classmethod
    def from_session_state(cls, session_state: dict[str, Any] | None) -> ConversationMemory:
        """Safely hydrates ConversationMemory from ADK session.state.

        Handles missing, malformed, or expired state fail-safely by returning fresh memory.
        """
        if not session_state or not isinstance(session_state, dict):
            return cls()

        raw_memory = session_state.get("conversation_memory")
        if not raw_memory or not isinstance(raw_memory, dict):
            return cls()

        try:
            memory = cls.model_validate(raw_memory)
            if memory.is_expired():
                logger.info("Conversation memory expired; resetting to fresh state.")
                return cls()
            return memory
        except Exception as e:
            logger.warning("Malformed conversation_memory in session.state (%s); resetting.", e)
            return cls()

    def save_to_session_state(self, session_state: dict[str, Any]) -> None:
        """Serializes bounded memory into ADK session.state."""
        if isinstance(session_state, dict):
            session_state["conversation_memory"] = self.model_dump()
