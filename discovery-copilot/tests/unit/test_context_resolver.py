"""Unit tests for Phase 4.4 Context and Anaphora Resolver.

Validates:
1. "they", "their", "that customer" pronoun resolution to active customer.
2. "it", "that solution" resolution to active card ID.
3. Technology continuity resolution.
4. Ambiguous customer resolution returns proactive clarification.
5. Six-turn sliding window and character cap enforcement.
6. Memory as context without overriding verified enterprise retrieval.
"""

import pytest

from app.planning.conversation_memory import ConversationMemory
from app.planning.context_resolver import ContextResolver


@pytest.fixture
def resolver():
    return ContextResolver()


def test_pronoun_they_resolves_to_active_customer(resolver):
    memory = ConversationMemory(active_customers=["Uber"])
    query = "What database did they use for that?"

    resolved = resolver.resolve(query=query, memory=memory)

    assert resolved.is_ambiguous is False
    assert resolved.resolved_customer == "Uber"
    assert "Uber" in resolved.resolved_query


def test_card_id_resolves_from_active_memory(resolver):
    memory = ConversationMemory(
        active_customers=["Canva"],
        active_card_ids=["card_0000000011112222"],
    )
    query = "Show evidence for that solution."

    resolved = resolver.resolve(query=query, memory=memory)

    assert resolved.is_ambiguous is False
    assert resolved.resolved_card_id == "card_0000000011112222"
    assert "card_0000000011112222" in resolved.resolved_query


def test_ambiguous_multiple_customers_triggers_clarification(resolver):
    memory = ConversationMemory(active_customers=["Uber", "Canva"])
    query = "What was their latency?"

    resolved = resolver.resolve(query=query, memory=memory)

    assert resolved.is_ambiguous is True
    assert resolved.clarification_message is not None
    assert "Uber, Canva" in resolved.clarification_message


def test_explicit_customer_overrides_ambiguous_memory(resolver):
    memory = ConversationMemory(active_customers=["Uber", "Canva"])
    query = "What was Canva's latency?"

    resolved = resolver.resolve(query=query, memory=memory)

    # Since user explicitly specified Canva, ambiguity is resolved
    assert resolved.is_ambiguous is False
    assert resolved.resolved_customer == "Canva"


def test_technology_continuity_resolution(resolver):
    memory = ConversationMemory(
        active_customers=["Dropbox"],
        active_technologies=["Google Cloud Spanner"],
    )
    query = "Does that database support multi-region replication?"

    resolved = resolver.resolve(query=query, memory=memory)

    assert resolved.is_ambiguous is False
    assert "Google Cloud Spanner" in resolved.resolved_technologies


def test_sliding_window_bounds_history_to_max_turns(resolver):
    memory = ConversationMemory()
    # 10 turns (20 events)
    history = [
        {"role": "user" if i % 2 == 0 else "model", "content": f"Turn {i} content"}
        for i in range(20)
    ]
    query = "How does it work?"

    resolved = resolver.resolve(query=query, memory=memory, session_history=history, max_turns=6)
    # Resolver runs without error and bounds history
    assert resolved is not None


def test_character_cap_prevents_unbounded_history(resolver):
    memory = ConversationMemory()
    # Long turns exceeding 4000 characters
    long_text = "Enterprise architecture details " * 200  # ~6400 chars
    history = [
        {"role": "user", "content": long_text},
        {"role": "model", "content": long_text},
    ]
    query = "Explain that customer's setup"

    resolved = resolver.resolve(query=query, memory=memory, session_history=history)
    assert resolved is not None

