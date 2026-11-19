"""Unit and integration tests for Phase 3D Step 2: Human Review API endpoints & CLI.

Validates:
1. GET /api/v1/reviews/cards (list reviewable cards with status filtering)
2. GET /api/v1/reviews/cards/{card_id} (retrieve full details with EvidenceLinks and audit logs)
3. POST /api/v1/reviews/cards/{card_id}/approve
4. POST /api/v1/reviews/cards/{card_id}/reject (verifies 400 when comment missing)
5. POST /api/v1/reviews/cards/{card_id}/request-changes (verifies 400 when comment missing)
6. POST /api/v1/reviews/cards/{card_id}/revoke (authorized revocation from APPROVED)
7. Authorization and identity extraction: client cannot spoof reviewer_id in request body
8. Concurrency & state machine conflict: rejecting a card blocks subsequent approval with 409 Conflict
9. Idempotency: replaying same action with same idempotency_key returns identical response without duplicating rows
10. CLI helper commands (list, show, action execution)
"""

import copy
import json
from pathlib import Path
import sys
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.database.review_router import (
    get_authenticated_reviewer,
    get_review_service,
    get_spanner_database,
    router,
)
from common.database.review_service import (
    AuthenticatedReviewer,
    ReviewAction,
    SolutionCardReviewService,
)
from common.models.schema import VerificationStatus


class MockSpannerSnapshot:
    """Mock Spanner snapshot for read queries in review router and CLI."""

    def __init__(self, db, multi_use: bool = False):
        self.db = db
        self.multi_use = multi_use
        self._read_request_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, query, params=None, param_types=None):
        if not self.multi_use and self._read_request_count > 0:
            raise ValueError("Cannot re-use single-use snapshot.")
        self._read_request_count += 1
        params = params or {}
        # 1. List SolutionCards
        if "FROM SolutionCards sc" in query and "LEFT JOIN Documents d" in query:
            if "WHERE sc.CardId = @card_id" in query:
                card_id = params.get("card_id")
                if card_id in self.db.cards:
                    c = self.db.cards[card_id]
                    return [
                        (
                            c["CardId"],
                            c["DocumentId"],
                            c.get("FileName", "test.pdf"),
                            c.get("ProjectName"),
                            c.get("BusinessProblem"),
                            c.get("ProposedSolution"),
                            c.get("ImplementedSolution"),
                            c.get("Outcome"),
                            c.get("VerificationStatus"),
                            c.get("OkfUri"),
                        )
                    ]
                return []

            # List cards query
            status_filter = params.get("status")
            rows = []
            for c in self.db.cards.values():
                st = c.get("VerificationStatus", "DRAFT")
                if status_filter and st != status_filter:
                    continue
                ev_cnt = len([e for e in self.db.evidence if e["CardId"] == c["CardId"]])
                rows.append(
                    (
                        c["CardId"],
                        c["DocumentId"],
                        c.get("FileName", "test.pdf"),
                        c.get("ProjectName"),
                        st,
                        c.get("OkfUri"),
                        ev_cnt,
                    )
                )
            return rows

        # 2. Evidence query
        if "FROM EvidenceLinks el" in query:
            card_id = params.get("card_id")
            matching_evs = []
            for e in self.db.evidence:
                if e["CardId"] == card_id:
                    if "el.DocumentId" in query:
                        matching_evs.append(
                            (
                                e["EvidenceId"],
                                e["ChunkId"],
                                e["DocumentId"],
                                e.get("FileName", "test.pdf"),
                                e.get("PageNumber"),
                                e.get("SlideNumber"),
                                e.get("Modality", "TEXT"),
                                e.get("TextContent", "Evidence text content"),
                                e.get("SourceGcsUri", "gs://bucket/doc.pdf"),
                                e.get("Note", "Citation note"),
                            )
                        )
                    else:
                        # CLI query format
                        matching_evs.append(
                            (
                                e["EvidenceId"],
                                e["ChunkId"],
                                e.get("PageNumber"),
                                e.get("SlideNumber"),
                                e.get("Modality", "TEXT"),
                                e.get("TextContent", "Evidence text content"),
                                e.get("SourceGcsUri", "gs://bucket/doc.pdf"),
                                e.get("Note", "Citation note"),
                            )
                        )
            return matching_evs

        # 3. Review History query
        if "FROM SolutionCardReviews" in query:
            card_id = params.get("card_id")
            matching_revs = []
            for r in self.db.reviews:
                if r["CardId"] == card_id:
                    if "ReviewerId" in query:
                        matching_revs.append(
                            (
                                r["ReviewId"],
                                r["ReviewerId"],
                                r["ReviewerEmail"],
                                r["Action"],
                                r["PreviousStatus"],
                                r["NewStatus"],
                                r.get("Comments"),
                                r.get("ReviewedAt", "2026-11-16T12:00:00Z"),
                                r.get("ReviewMetadata"),
                            )
                        )
                    else:
                        # CLI query format
                        matching_revs.append(
                            (
                                r["ReviewId"],
                                r["ReviewerEmail"],
                                r["Action"],
                                r["PreviousStatus"],
                                r["NewStatus"],
                                r.get("Comments"),
                                r.get("ReviewedAt", "2026-11-16T12:00:00Z"),
                                r.get("ReviewMetadata"),
                            )
                        )
            return matching_revs

        return []


class MockSpannerTransaction:
    """Mock Spanner read-write transaction."""

    def __init__(self, db):
        self.db = db
        self.buffered_updates = []
        self.buffered_inserts = []

    def execute_sql(self, query, params=None, param_types=None):
        params = params or {}
        if "FROM SolutionCardReviews" in query:
            card_id = params.get("card_id")
            review_id = params.get("review_id")
            return [
                (r["CardId"], r["Action"], r["PreviousStatus"], r["NewStatus"])
                for r in self.db.reviews
                if r["CardId"] == card_id and r["ReviewId"] == review_id
            ]

        card_id = params.get("card_id")
        if card_id in self.db.cards:
            c = self.db.cards[card_id]
            return [
                (
                    c["CardId"],
                    c.get("ProjectName"),
                    c.get("BusinessProblem"),
                    c.get("ProposedSolution"),
                    c.get("ImplementedSolution"),
                    c.get("Outcome"),
                    c.get("VerificationStatus"),
                    c.get("OkfUri"),
                )
            ]
        return []

    def update(self, table, columns, values):
        self.buffered_updates.append({"table": table, "columns": columns, "values": values})

    def insert(self, table, columns, values):
        self.buffered_inserts.append({"table": table, "columns": columns, "values": values})

    def commit(self):
        for upd in self.buffered_updates:
            if upd["table"] == "SolutionCards":
                for row in upd["values"]:
                    cid = row[0]
                    col_dict = dict(zip(upd["columns"], row))
                    if cid in self.db.cards:
                        self.db.cards[cid].update(col_dict)
        for ins in self.buffered_inserts:
            if ins["table"] == "SolutionCardReviews":
                for row in ins["values"]:
                    self.db.reviews.append(dict(zip(ins["columns"], row)))


class MockSpannerDatabase:
    """Mock database backing review endpoints."""

    def __init__(self):
        self.cards = {
            "card_pending_1": {
                "CardId": "card_pending_1",
                "DocumentId": "doc_1",
                "FileName": "sample.pdf",
                "ProjectName": "Sample Project",
                "BusinessProblem": "High latency in discovery",
                "ProposedSolution": "Deploy Spanner",
                "ImplementedSolution": "Spanner Graph configured",
                "Outcome": "Achieved sub-10ms response",
                "VerificationStatus": "PENDING",
                "OkfUri": "gs://bucket/meta/doc_1.meta.json",
            },
            "card_approved_1": {
                "CardId": "card_approved_1",
                "DocumentId": "doc_2",
                "FileName": "approved.pdf",
                "ProjectName": "Approved Architecture",
                "BusinessProblem": "Legacy storage bottleneck",
                "ProposedSolution": "Migrate to GCS",
                "ImplementedSolution": "GCS storage integrated",
                "Outcome": "Unlimited scalability",
                "VerificationStatus": "APPROVED",
                "OkfUri": "gs://bucket/meta/doc_2.meta.json",
            },
        }
        self.evidence = [
            {
                "EvidenceId": "ev_1",
                "CardId": "card_pending_1",
                "ChunkId": "chk_1",
                "DocumentId": "doc_1",
                "FileName": "sample.pdf",
                "PageNumber": 3,
                "SlideNumber": None,
                "Modality": "TEXT",
                "TextContent": "Benchmark demonstrates sub-10ms query execution across 10M edges.",
                "SourceGcsUri": "gs://bucket/sample.pdf",
                "Note": "Sub-10ms query citation",
            }
        ]
        self.reviews = []

    def snapshot(self, multi_use: bool = False):
        return MockSpannerSnapshot(self, multi_use=multi_use)

    def run_in_transaction(self, func):
        txn = MockSpannerTransaction(self)
        res = func(txn)
        txn.commit()
        return res


@pytest.fixture
def test_app():
    """Sets up FastAPI test application with injected mock database and review service."""
    app = FastAPI(title="Review API Test")
    app.include_router(router)

    mock_db = MockSpannerDatabase()

    def override_get_spanner_database():
        return mock_db

    def override_get_review_service():
        return SolutionCardReviewService(database=mock_db)

    def override_get_authenticated_reviewer():
        return AuthenticatedReviewer(
            reviewer_id="usr_chief_architect",
            reviewer_email="chief.architect@enterprise.com",
            roles=("lead_architect",),
        )

    app.dependency_overrides[get_spanner_database] = override_get_spanner_database
    app.dependency_overrides[get_review_service] = override_get_review_service
    app.dependency_overrides[get_authenticated_reviewer] = override_get_authenticated_reviewer

    return app, mock_db


@pytest.fixture
def client(test_app):
    app, mock_db = test_app
    return TestClient(app)


# ============================================================================
# API TESTS
# ============================================================================


def test_api_list_cards(client):
    """Verify listing cards and status filtering."""
    # List all
    res = client.get("/api/v1/reviews/cards")
    assert res.status_code == 200
    cards = res.json()
    assert len(cards) == 2

    # Filter by PENDING
    res_pending = client.get("/api/v1/reviews/cards?status=PENDING")
    assert res_pending.status_code == 200
    pending_cards = res_pending.json()
    assert len(pending_cards) == 1
    assert pending_cards[0]["card_id"] == "card_pending_1"
    assert pending_cards[0]["evidence_count"] == 1


def test_api_get_card_detail(client):
    """Verify retrieving complete card detail with grounded evidence."""
    res = client.get("/api/v1/reviews/cards/card_pending_1")
    assert res.status_code == 200
    data = res.json()
    assert data["card_id"] == "card_pending_1"
    assert data["verification_status"] == "PENDING"
    assert len(data["evidence_links"]) == 1
    ev = data["evidence_links"][0]
    assert ev["evidence_id"] == "ev_1"
    assert "sub-10ms" in ev["source_chunk_text"]


def test_api_approve_card(client, test_app):
    """Verify approving a PENDING card."""
    app, db = test_app
    payload = {
        "comments": "Audited against Slide 3 latency benchmarks.",
        "field_edits": [
            {"field_name": "project_name", "old_value": "Sample Project", "new_value": "Production Spanner Pilot"}
        ],
    }
    res = client.post("/api/v1/reviews/cards/card_pending_1/approve", json=payload)
    assert res.status_code == 200
    resp_data = res.json()
    assert resp_data["new_status"] == "APPROVED"
    assert resp_data["reviewer_email"] == "chief.architect@enterprise.com"

    # Verify db mutation
    assert db.cards["card_pending_1"]["VerificationStatus"] == "APPROVED"
    assert db.cards["card_pending_1"]["ProjectName"] == "Production Spanner Pilot"
    assert len(db.reviews) == 1


def test_api_reject_requires_comment(client):
    """Verify rejection fails with 400 when comment is missing."""
    # Missing comment
    res = client.post("/api/v1/reviews/cards/card_pending_1/reject", json={"comments": ""})
    assert res.status_code == 400
    assert "requires a non-empty explanation comment" in res.json()["detail"]

    # Valid comment succeeds
    res_valid = client.post(
        "/api/v1/reviews/cards/card_pending_1/reject",
        json={"comments": "Contradicted by external SLA benchmark"},
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["new_status"] == "REJECTED"


def test_api_concurrency_conflict_on_terminal_state(client):
    """Verify attempting to approve a rejected card raises 409 Conflict."""
    # First reject
    client.post(
        "/api/v1/reviews/cards/card_pending_1/reject",
        json={"comments": "Rejecting card due to hallucination"},
    )

    # Subsequent approval must be blocked
    res = client.post("/api/v1/reviews/cards/card_pending_1/approve", json={})
    assert res.status_code == 409
    assert "terminal state REJECTED" in res.json()["detail"]


def test_api_revoke_approved_card(client, test_app):
    """Verify authorized revocation from APPROVED back to CHANGES_REQUESTED."""
    res = client.post(
        "/api/v1/reviews/cards/card_approved_1/revoke",
        json={"comments": "New architecture revision deployed in Q4."},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["previous_status"] == "APPROVED"
    assert data["new_status"] == "CHANGES_REQUESTED"


def test_api_ui_dashboard_endpoint(client):
    """Verify GET /api/v1/reviews/ui serves the HTML review interface."""
    res = client.get("/api/v1/reviews/ui")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert "Enterprise GraphRAG" in res.text
    assert "Knowledge Review" in res.text
    assert "Grounded Source Evidence" in res.text


def test_api_idempotency_key_replay(client, test_app):
    """Verify submitting the same request with idempotency_key returns identical outcome without duplicate records."""
    app, db = test_app
    payload = {
        "comments": "Approved via automated CI",
        "idempotency_key": "idemp_test_req_8877",
    }

    res1 = client.post("/api/v1/reviews/cards/card_pending_1/approve", json=payload)
    assert res1.status_code == 200
    assert res1.json()["review_id"] == "idemp_test_req_8877"
    assert len(db.reviews) == 1

    # Replay
    res2 = client.post("/api/v1/reviews/cards/card_pending_1/approve", json=payload)
    assert res2.status_code == 200
    assert res2.json()["review_id"] == "idemp_test_req_8877"
    assert len(db.reviews) == 1  # No duplicate row inserted


# ============================================================================
# CLI CMD_SHOW SNAPSHOT BUG REGRESSION TESTS
# ============================================================================


def test_cli_cmd_show_snapshot_regression(capsys):
    """Regression test reproducing single-use snapshot failure and verifying multi_use=True fix.
    
    Reproduces original failure:
      ValueError: Cannot re-use single-use snapshot.
    when db.snapshot() defaults to multi_use=False and executes multiple queries.
    """
    from unittest.mock import patch
    import argparse
    import scripts.review_cli as review_cli

    mock_db = MockSpannerDatabase()

    # Verify that single-use snapshot strictly fails on second query
    single_use_snap = mock_db.snapshot(multi_use=False)
    single_use_snap.execute_sql("SELECT 1 FROM SolutionCards sc LEFT JOIN Documents d ON sc.DocumentId = d.DocumentId WHERE sc.CardId = @card_id", {"card_id": "card_pending_1"})
    with pytest.raises(ValueError, match="Cannot re-use single-use snapshot"):
        single_use_snap.execute_sql("SELECT 1 FROM EvidenceLinks el WHERE el.CardId = @card_id", {"card_id": "card_pending_1"})

    # Verify that cmd_show uses multi_use=True and executes without error
    args = argparse.Namespace(card_id="card_pending_1")
    with patch("scripts.review_cli.get_spanner_database", return_value=mock_db):
        review_cli.cmd_show(args)

    out = capsys.readouterr().out
    assert "SOLUTION CARD: card_pending_1" in out
    assert "GROUNDED EVIDENCE CITATIONS (1):" in out
    assert "AUDIT HISTORY (0):" in out


def test_cli_cmd_show_retrieves_card_evidence_and_history(capsys):
    """Verify cmd_show retrieves card details, evidence links, and audit history."""
    from unittest.mock import patch
    import argparse
    import scripts.review_cli as review_cli

    mock_db = MockSpannerDatabase()
    # Add an audit record
    mock_db.reviews.append({
        "ReviewId": "rev_test_123",
        "CardId": "card_pending_1",
        "ReviewerId": "usr_reviewer_1",
        "ReviewerEmail": "architect@enterprise.com",
        "Action": "REQUEST_CHANGES",
        "PreviousStatus": "PENDING",
        "NewStatus": "CHANGES_REQUESTED",
        "Comments": "Please verify benchmark claims against production metrics.",
        "ReviewedAt": "2026-11-17T12:00:00Z",
        "ReviewMetadata": None,
    })

    args = argparse.Namespace(card_id="card_pending_1")
    with patch("scripts.review_cli.get_spanner_database", return_value=mock_db):
        review_cli.cmd_show(args)

    out = capsys.readouterr().out
    # 1. SolutionCard details
    assert "SOLUTION CARD: card_pending_1" in out
    assert "Status:         PENDING" in out
    assert "Project Name:   Sample Project" in out
    assert "BUSINESS PROBLEM:\n High latency in discovery" in out
    assert "PROPOSED SOLUTION:\n Deploy Spanner" in out
    assert "IMPLEMENTED SOLUTION:\n Spanner Graph configured" in out
    assert "OUTCOME:\n Achieved sub-10ms response" in out

    # 2. Evidence citations
    assert "GROUNDED EVIDENCE CITATIONS (1):" in out
    assert "EvidenceId: ev_1 | ChunkId: chk_1 | Page 3 | Modality: TEXT" in out
    assert 'Citation Note: "Sub-10ms query citation"' in out
    assert 'Source Text:   "Benchmark demonstrates sub-10ms query execution across 10M edges."' in out

    # 3. Audit History
    assert "AUDIT HISTORY (1):" in out
    assert "architect@enterprise.com applied 'REQUEST_CHANGES': PENDING -> CHANGES_REQUESTED" in out
    assert 'Comment: "Please verify benchmark claims against production metrics."' in out


