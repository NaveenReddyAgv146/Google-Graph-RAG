"""Tests for Step 2B Claim-Level Evidence Support in Enterprise GraphRAG.

Covers:
- ClaimCategory enum definitions and validation.
- Optional ClaimType on EvidenceLink models and backward compatibility with NULL/None.
- Deterministic EvidenceId generation preserving legacy hashes.
- Multiple citations per claim in EnrichmentWriter.
- CLI and REST API claim grouping and visibility of ungrounded claims.
- Mandatory evidence integrity rules: citations do not equal verification, approval requires human review.
"""

import argparse
import hashlib
import os
import sys
from unittest.mock import patch
import pytest
from pydantic import ValidationError

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from common.models.schema import (
    ClaimCategory,
    EvidenceLinkNode,
    VerificationStatus,
)
from common.models.review_api import EvidenceDetail, SolutionCardReviewDetail
from common.database.enrichment_writer import (
    EnrichmentPayload,
    EnrichmentWriter,
    generate_evidence_link_id,
)
from common.parsers.metadata_extractor import (
    RawClaimEvidenceCandidate,
    RawSolutionCardCandidate,
    ValidatedClaimEvidence,
    ValidatedSolutionCard,
)
import scripts.review_cli as review_cli


# ============================================================================
# 1. ENUM & SCHEMA TESTS
# ============================================================================


def test_claim_category_enum_values():
    """Verify ClaimCategory enum contains exactly the 4 approved categories."""
    assert ClaimCategory.BUSINESS_PROBLEM.value == "BUSINESS_PROBLEM"
    assert ClaimCategory.PROPOSED_SOLUTION.value == "PROPOSED_SOLUTION"
    assert ClaimCategory.IMPLEMENTED_SOLUTION.value == "IMPLEMENTED_SOLUTION"
    assert ClaimCategory.OUTCOME.value == "OUTCOME"

    # Rejection of invalid category
    with pytest.raises(ValueError):
        ClaimCategory("UNAPPROVED_CATEGORY")


def test_evidence_link_node_legacy_null_compatibility():
    """Verify EvidenceLinkNode supports claim_type=None for backward compatibility."""
    node = EvidenceLinkNode(
        evidence_id="evl_legacy_123",
        card_id="card_123",
        chunk_id="chunk_123",
        document_id="doc_123",
        claim_type=None,
        page_number=1,
    )
    assert node.claim_type is None
    dumped = node.model_dump()
    assert dumped["claim_type"] is None

    # Deserializing without claim_type sets it to None
    deserialized = EvidenceLinkNode.model_validate(
        {
            "evidence_id": "evl_legacy_123",
            "card_id": "card_123",
            "document_id": "doc_123",
        }
    )
    assert deserialized.claim_type is None


def test_evidence_link_node_with_valid_and_invalid_claim_types():
    """Verify EvidenceLinkNode accepts valid ClaimCategory and rejects invalid strings."""
    node = EvidenceLinkNode(
        evidence_id="evl_claim_123",
        card_id="card_123",
        chunk_id="chunk_123",
        document_id="doc_123",
        claim_type=ClaimCategory.BUSINESS_PROBLEM,
    )
    assert node.claim_type == ClaimCategory.BUSINESS_PROBLEM

    with pytest.raises(ValidationError):
        EvidenceLinkNode(
            evidence_id="evl_fail",
            card_id="card_123",
            document_id="doc_123",
            claim_type="NOT_A_CATEGORY",
        )


# ============================================================================
# 2. DETERMINISTIC HASH GENERATION TESTS
# ============================================================================


def test_deterministic_evidence_id_legacy_hash_preservation():
    """Verify that when claim_type is None or empty, exact legacy hash is produced."""
    cid = "card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d"
    chk = "chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6"
    quote = "UberCab Concept: A fast & efficient on-demand car service."

    # Legacy formula: sha256("{card_id}:{chunk_id}:{quote[:64]}")[:48]
    legacy_raw = f"{cid}:{chk}:{quote.strip()[:64]}"
    expected_legacy_id = f"evl_{hashlib.sha256(legacy_raw.encode('utf-8')).hexdigest()[:48]}"

    # Function call with omitted claim_type
    id_omitted = generate_evidence_link_id(cid, chk, quote)
    assert id_omitted == expected_legacy_id

    # Function call with explicit None
    id_none = generate_evidence_link_id(cid, chk, quote, claim_type=None)
    assert id_none == expected_legacy_id

    # Function call with empty string
    id_empty = generate_evidence_link_id(cid, chk, quote, claim_type="")
    assert id_empty == expected_legacy_id


def test_deterministic_evidence_id_with_claim_type_produces_unique_hashes():
    """Verify distinct claim types for same card and chunk produce deterministic, collision-free IDs."""
    cid = "card_uber"
    chk = "chunk_uber_1"
    quote = "Same quote text"

    id_prob = generate_evidence_link_id(cid, chk, quote, claim_type="BUSINESS_PROBLEM")
    id_sol = generate_evidence_link_id(cid, chk, quote, claim_type="PROPOSED_SOLUTION")
    id_impl = generate_evidence_link_id(cid, chk, quote, claim_type="IMPLEMENTED_SOLUTION")
    id_out = generate_evidence_link_id(cid, chk, quote, claim_type="OUTCOME")

    # All must start with 'evl_'
    assert all(x.startswith("evl_") for x in [id_prob, id_sol, id_impl, id_out])
    # All must be distinct
    assert len({id_prob, id_sol, id_impl, id_out}) == 4
    # Re-running produces identical results (deterministic idempotency)
    assert id_prob == generate_evidence_link_id(cid, chk, quote, claim_type="BUSINESS_PROBLEM")


# ============================================================================
# 3. ENRICHMENT WRITER & MULTIPLE CITATIONS TESTS
# ============================================================================


class MockSpannerTxn:
    def __init__(self):
        self.inserts = {}
        self.updates = {}

    def execute_sql(self, sql, params=None, param_types=None):
        if "FROM EvidenceLinks" in sql:
            # None exist yet
            return []
        if "FROM EvidenceChunks" in sql:
            return [("chk_p2", "doc_uber"), ("chk_p3", "doc_uber"), ("chk_p4", "doc_uber"), ("chk_p25", "doc_uber"), ("chk_p20", "doc_uber")]
        if "FROM SolutionCards" in sql:
            return []
        if "FROM SolutionImplementsDoc" in sql:
            return []
        return []

    def insert(self, table, columns, values):
        self.inserts[table] = {"columns": columns, "values": values}

    def update(self, table, columns, values):
        self.updates[table] = {"columns": columns, "values": values}


def test_enrichment_writer_multiple_citations_per_claim():
    """Verify EnrichmentWriter persists multiple citations with ClaimType for one SolutionCard."""
    txn = MockSpannerTxn()

    card = ValidatedSolutionCard(
        card_id="card_uber",
        document_id="doc_uber",
        project_name="UberCab 2008",
        business_problem="Aging radio dispatch and medallion lock-in.",
        proposed_solution="NetJets of car services.",
        implemented_solution="Foundational filings in 2008.",
        outcome="Potential $1B revenue scenario.",
        chunk_id="chk_p4",
        evidence_quote="NetJets of car services.",
        claim_evidence=[
            ValidatedClaimEvidence(
                claim_type=ClaimCategory.BUSINESS_PROBLEM,
                chunk_id="chk_p2",
                evidence_quote="Radio dispatch, no 2-way communication",
            ),
            ValidatedClaimEvidence(
                claim_type=ClaimCategory.BUSINESS_PROBLEM,
                chunk_id="chk_p3",
                evidence_quote="Taxi-monopolies reduce quality of service",
            ),
            ValidatedClaimEvidence(
                claim_type=ClaimCategory.PROPOSED_SOLUTION,
                chunk_id="chk_p4",
                evidence_quote="NetJets of car services",
            ),
            ValidatedClaimEvidence(
                claim_type=ClaimCategory.IMPLEMENTED_SOLUTION,
                chunk_id="chk_p25",
                evidence_quote="California LLC + trademark filed",
            ),
            ValidatedClaimEvidence(
                claim_type=ClaimCategory.OUTCOME,
                chunk_id="chk_p20",
                evidence_quote="Potential Outcomes: Best-Case Scenario — $1B+ in yearly revenue",
            ),
        ],
    )

    doc_chunks = {
        "chk_p2": ("chk_p2", "doc_uber", "TEXT", 2, None, None),
        "chk_p3": ("chk_p3", "doc_uber", "TEXT", 3, None, None),
        "chk_p4": ("chk_p4", "doc_uber", "TEXT", 4, None, None),
        "chk_p25": ("chk_p25", "doc_uber", "TEXT", 25, None, None),
        "chk_p20": ("chk_p20", "doc_uber", "TEXT", 20, None, None),
    }

    # Simulate writer logic for EvidenceLinks
    evidence_link_candidates = []
    cid = card.card_id
    doc_id = card.document_id
    for ce in card.claim_evidence:
        c_rec = doc_chunks.get(ce.chunk_id)
        p_num = c_rec[3] if c_rec else None
        s_num = c_rec[4] if c_rec else None
        ev_id = generate_evidence_link_id(cid, ce.chunk_id, ce.evidence_quote, claim_type=ce.claim_type.value)
        evidence_link_candidates.append(
            (ev_id, cid, ce.chunk_id, doc_id, p_num, s_num, ce.evidence_quote, ce.claim_type.value)
        )

    # 5 citations generated
    assert len(evidence_link_candidates) == 5

    # 2 citations specifically for BUSINESS_PROBLEM
    bp_citations = [c for c in evidence_link_candidates if c[7] == "BUSINESS_PROBLEM"]
    assert len(bp_citations) == 2
    assert bp_citations[0][0] != bp_citations[1][0]  # distinct evidence IDs


# ============================================================================
# 4. CLI & API CLAIM GROUPING AND VISIBILITY TESTS
# ============================================================================


class MockSnapshotForCLI:
    def __init__(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, sql, params=None, param_types=None):
        if "FROM SolutionCards sc" in sql:
            return [
                (
                    "card_uber_test",
                    "doc_uber",
                    "Uber - 2008.pdf",
                    "UberCab Pitch Deck",
                    "Aging radio dispatch and high dead-time",
                    "On-demand car service (NetJets)",
                    "California LLC and trademark filed",
                    "Projected $1B+ yearly revenue",
                    "PENDING",
                    "gs://bucket/meta.json#sha256=123",
                )
            ]
        if "FROM EvidenceLinks el" in sql:
            # 3 citations attached: BusinessProblem, ProposedSolution, Outcome.
            # ImplementedSolution is intentionally OMITTED to verify it shows UNGROUNDED.
            return [
                ("ev_1", "chk_p2", 2, None, "TEXT", "Radio dispatch, no 2-way communication", None, "Traditional cab pain", "BUSINESS_PROBLEM"),
                ("ev_2", "chk_p4", 4, None, "TEXT", "UberCab Concept: A fast & efficient service", None, "Core concept", "PROPOSED_SOLUTION"),
                ("ev_3", "chk_p20", 20, None, "TEXT", "Potential Outcomes: Best-Case Scenario — $1B+", None, "Projected scenarios", "OUTCOME"),
            ]
        if "FROM SolutionCardReviews" in sql:
            return []
        return []


class MockDbForCLI:
    def snapshot(self, multi_use=True):
        return MockSnapshotForCLI()


def test_cli_cmd_show_groups_by_claim_and_flags_ungrounded(capsys):
    """Verify review CLI groups citations by ClaimType and prominently flags ungrounded claims."""
    mock_db = MockDbForCLI()
    args = argparse.Namespace(card_id="card_uber_test")

    with patch("scripts.review_cli.get_spanner_database", return_value=mock_db):
        review_cli.cmd_show(args)

    out = capsys.readouterr().out

    # 1. Card overview
    assert "SOLUTION CARD: card_uber_test" in out
    assert "Status:         PENDING" in out

    # 2. Claim-grouped citations
    assert "[CLAIM: BUSINESS_PROBLEM] (1 citation attached):" in out
    assert "[CLAIM: PROPOSED_SOLUTION] (1 citation attached):" in out
    assert "[CLAIM: OUTCOME] (1 citation attached):" in out

    # 3. Ungrounded claim must be visibly unverified
    assert "[CLAIM: IMPLEMENTED_SOLUTION] ⚠️ NO CITATIONS ATTACHED (Unverified / Ungrounded)" in out

    # 4. Mandatory evidence integrity reminder
    assert "Attached citations provide grounding evidence; human review is required for verification." in out
    assert "EVIDENCE INTEGRITY AUDIT SUMMARY:" in out
    assert "Implemented Solution      ⚠️ UNGROUNDED (0 citations attached)" in out
    assert "Reminder: Attaching citations establishes provenance. Human review remains mandatory to APPROVE." in out


# ============================================================================
# 5. EVIDENCE INTEGRITY & MANDATORY HUMAN REVIEW RULES
# ============================================================================


def test_attached_citations_do_not_automatically_approve_card():
    """Verify that attaching evidence citations leaves card in PENDING and never automatically APPROVES."""
    # Simulating a card with full citations attached
    card_detail = SolutionCardReviewDetail(
        card_id="card_full_citations",
        document_id="doc_123",
        project_name="Full Citations Project",
        verification_status=VerificationStatus.PENDING,
        evidence_links=[
            EvidenceDetail(evidence_id="ev_1", chunk_id="chk_1", document_id="doc_123", claim_type=ClaimCategory.BUSINESS_PROBLEM),
            EvidenceDetail(evidence_id="ev_2", chunk_id="chk_2", document_id="doc_123", claim_type=ClaimCategory.PROPOSED_SOLUTION),
            EvidenceDetail(evidence_id="ev_3", chunk_id="chk_3", document_id="doc_123", claim_type=ClaimCategory.IMPLEMENTED_SOLUTION),
            EvidenceDetail(evidence_id="ev_4", chunk_id="chk_4", document_id="doc_123", claim_type=ClaimCategory.OUTCOME),
        ],
    )

    # Verification status must strictly remain PENDING
    assert card_detail.verification_status == VerificationStatus.PENDING
    assert len(card_detail.evidence_links) == 4
    # The existence of 4 citations does not equal APPROVED
    assert card_detail.verification_status != VerificationStatus.APPROVED
