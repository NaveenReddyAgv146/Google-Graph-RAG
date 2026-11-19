"""Unit tests for Phase 4.3 EvidenceSelector with Adaptive MMR."""

import numpy as np
import pytest

from app.planning.models import QueryIntent
from app.synthesis.evidence_selector import (
    DEFAULT_MMR_LAMBDA,
    INTENT_MMR_LAMBDAS,
    EvidenceSelector,
)
from app.synthesis.models import EvidencePack


def test_adaptive_lambda_for_all_seven_intents():
    """Verify distinct adaptive MMR lambda values configured for all 7 QueryIntents."""
    selector = EvidenceSelector()

    assert selector.get_adaptive_lambda(QueryIntent.COMPARISON) == 0.45
    assert selector.get_adaptive_lambda(QueryIntent.SOLUTION_DISCOVERY) == 0.70
    assert selector.get_adaptive_lambda(QueryIntent.TECHNICAL_LOOKUP) == 0.85
    assert selector.get_adaptive_lambda(QueryIntent.ARCHITECTURE_EXPLORATION) == 0.60
    assert selector.get_adaptive_lambda(QueryIntent.EVIDENCE_VERIFICATION) == 0.90
    assert selector.get_adaptive_lambda(QueryIntent.FOLLOW_UP) == 0.75
    assert selector.get_adaptive_lambda(QueryIntent.CLARIFICATION_NEEDED) == 1.0

    # Fallback check
    assert selector.get_adaptive_lambda("UNKNOWN_INTENT") == DEFAULT_MMR_LAMBDA


def test_cross_task_deduplication_by_card_id_and_chunk_id():
    """Verify duplicate solution cards and evidence chunks across subtasks are merged cleanly."""
    selector = EvidenceSelector()

    subtask_results = {
        "task_1": {
            "status": "success",
            "solutions": [
                {
                    "card_id": "card_uber_1",
                    "project_name": "Uber Dispatch",
                    "verification_status": "APPROVED",
                    "document_id": "doc_uber",
                    "citations": [
                        {"chunk_id": "chunk_u1", "source_chunk_text": "1-Click request from Geo-aware devices"},
                        {"chunk_id": "chunk_u2", "source_chunk_text": "Automate dispatch to reduce wait-time"},
                    ],
                }
            ],
        },
        "task_2": {
            "status": "success",
            "solutions": [
                {
                    # Same card returned in second task with an extra chunk
                    "card_id": "card_uber_1",
                    "project_name": "Uber Dispatch",
                    "verification_status": "APPROVED",
                    "document_id": "doc_uber",
                    "citations": [
                        {"chunk_id": "chunk_u1", "source_chunk_text": "1-Click request from Geo-aware devices"},
                        {"chunk_id": "chunk_u3", "source_chunk_text": "Pre-paid, cashless billing system"},
                    ],
                },
                {
                    "card_id": "card_canva_1",
                    "project_name": "Canva Media Pipeline",
                    "verification_status": "APPROVED",
                    "document_id": "doc_canva",
                    "citations": [
                        {"chunk_id": "chunk_c1", "source_chunk_text": "Export presentation assets via worker pools"},
                    ],
                },
            ],
        },
    }

    pack = selector.select_evidence(
        query="Compare transport and media platforms",
        intent=QueryIntent.COMPARISON,
        subtask_results=subtask_results,
        top_k=5,
    )

    # Exactly 2 cards deduplicated
    assert pack.total_cards == 2
    assert {c.card_id for c in pack.cards} == {"card_uber_1", "card_canva_1"}

    # Uber card should have all 3 unique chunks merged
    uber_card = next(c for c in pack.cards if c.card_id == "card_uber_1")
    assert len(uber_card.evidence_chunks) == 3
    assert {chk.chunk_id for chk in uber_card.evidence_chunks} == {"chunk_u1", "chunk_u2", "chunk_u3"}

    # Total chunks in pack
    assert pack.total_chunks == 4
    assert "chunk_u1" in pack.chunks_by_id
    assert "chunk_c1" in pack.chunks_by_id


def test_unapproved_cards_strictly_excluded():
    """Verify PENDING, DRAFT, REJECTED cards in tool outputs are strictly dropped."""
    selector = EvidenceSelector()

    subtask_results = {
        "task_1": {
            "status": "success",
            "solutions": [
                {
                    "card_id": "card_approved",
                    "project_name": "Approved Architecture",
                    "verification_status": "APPROVED",
                    "document_id": "doc_1",
                    "citations": [{"chunk_id": "chk_1", "source_chunk_text": "Verified text"}],
                },
                {
                    "card_id": "card_pending",
                    "project_name": "Pending Architecture",
                    "verification_status": "PENDING",
                    "document_id": "doc_2",
                    "citations": [{"chunk_id": "chk_2", "source_chunk_text": "Unverified text"}],
                },
                {
                    "card_id": "card_rejected",
                    "project_name": "Rejected Architecture",
                    "verification_status": "REJECTED",
                    "document_id": "doc_3",
                    "citations": [{"chunk_id": "chk_3", "source_chunk_text": "Rejected text"}],
                },
            ],
        }
    }

    pack = selector.select_evidence(
        query="test query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtask_results=subtask_results,
    )

    assert pack.total_cards == 1
    assert pack.cards[0].card_id == "card_approved"
    assert "chk_2" not in pack.chunks_by_id
    assert "chk_3" not in pack.chunks_by_id


def test_configurable_global_top_k():
    """Verify global top-K limits the number of selected cards."""
    selector = EvidenceSelector()

    subtask_results = {
        "task_1": {
            "status": "success",
            "solutions": [
                {
                    "card_id": f"card_{i}",
                    "project_name": f"Project {i}",
                    "verification_status": "APPROVED",
                    "document_id": f"doc_{i}",
                    "citations": [{"chunk_id": f"chk_{i}", "source_chunk_text": f"text {i}"}],
                }
                for i in range(10)
            ],
        }
    }

    # Request top_k=3
    pack = selector.select_evidence(
        query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtask_results=subtask_results,
        top_k=3,
    )
    assert pack.total_cards == 3


def test_missing_and_incompatible_embeddings_fallback():
    """Verify selector falls back safely when embeddings are missing or dimension-mismatched."""
    selector = EvidenceSelector(embedding_client=None)

    subtask_results = {
        "task_1": {
            "status": "success",
            "solutions": [
                {
                    "card_id": "card_1",
                    "project_name": "Proj 1",
                    "verification_status": "APPROVED",
                    "document_id": "doc_1",
                    "citations": [{"chunk_id": "chk_1", "source_chunk_text": "chunk 1"}],
                },
                {
                    "card_id": "card_2",
                    "project_name": "Proj 2",
                    "verification_status": "APPROVED",
                    "document_id": "doc_2",
                    "citations": [{"chunk_id": "chk_2", "source_chunk_text": "chunk 2"}],
                },
            ],
        }
    }

    # Pass incompatible candidate embeddings (length 1 for 2 candidates)
    pack = selector.select_evidence(
        query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtask_results=subtask_results,
        query_embedding=[1.0, 0.0],
        candidate_embeddings=[[1.0, 0.0]],  # Mismatch: length 1 != 2
        top_k=2,
    )

    # Should not crash; gracefully returns candidates up to top_k
    assert pack.total_cards == 2


def test_customer_attribution_extraction():
    """Verify customer names are inferred correctly for known and general entities."""
    selector = EvidenceSelector()

    subtask_results = {
        "task_1": {
            "status": "success",
            "solutions": [
                {
                    "card_id": "card_1",
                    "project_name": "UberCab Mobile On-Demand Dispatch",
                    "document_name": "Uber - 2008.pdf",
                    "verification_status": "APPROVED",
                    "document_id": "doc_uber",
                    "citations": [{"chunk_id": "chk_1", "source_chunk_text": "text"}],
                },
                {
                    "card_id": "card_2",
                    "project_name": "Canva High Volume Media Exporter",
                    "document_name": "Canva Architecture.pdf",
                    "verification_status": "APPROVED",
                    "document_id": "doc_canva",
                    "citations": [{"chunk_id": "chk_2", "source_chunk_text": "text"}],
                },
            ],
        }
    }

    pack = selector.select_evidence(
        query="query",
        intent=QueryIntent.COMPARISON,
        subtask_results=subtask_results,
    )

    c_map = {c.card_id: c.customer_name for c in pack.cards}
    assert c_map["card_1"] == "Uber"
    assert c_map["card_2"] == "Canva"


def test_empty_subtask_results_returns_empty_evidence_pack():
    """Verify empty or failed subtasks produce a clean empty EvidencePack."""
    selector = EvidenceSelector()

    pack = selector.select_evidence(
        query="query",
        intent=QueryIntent.SOLUTION_DISCOVERY,
        subtask_results={"task_1": {"status": "empty", "solutions": []}},
    )

    assert pack.total_cards == 0
    assert pack.total_chunks == 0
    assert pack.cards == []
    assert pack.chunks_by_id == {}

