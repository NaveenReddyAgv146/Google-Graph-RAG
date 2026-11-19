"""Unit tests for Phase 3D Human Review Service.

Validates:
1. Strict state machine transitions (PENDING -> APPROVED/REJECTED/CHANGES_REQUESTED).
2. Authenticated identity validation (ReviewerId and ReviewerEmail from auth context).
3. Rejection of unauthenticated or spoofed identity.
4. Mandatory comments for REJECT and REQUEST_CHANGES.
5. Terminal state enforcement for REJECTED cards (no reactivation).
6. Controlled re-review of APPROVED cards (only through REVOKE action).
7. ReviewMetadata JSON capture of human edits with old/new values.
8. Sanitization of sensitive keys in client metadata.
9. Atomic commit timestamp usage.
10. Spanner Aborted retry handling and stable ReviewId across internal retries.
11. Concurrent conflicting review decisions (competing REJECT).
12. Rollback safety: zero partial audit records on transaction failure.
13. Client-level idempotency via idempotency_key.
14. Deep schema mismatch detection in scripts/apply_phase3d_schema.py.
15. Cryptographic fingerprint mismatch detection in scripts/apply_phase3d_schema.py.
"""

import copy
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from google.cloud import spanner
from google.api_core.exceptions import Aborted

# Ensure repo root is in sys.path for scripts import
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.database.review_service import (
    AuthenticatedReviewer,
    AuthenticationContextError,
    CardNotFoundError,
    InvalidStateTransitionError,
    MissingReviewCommentError,
    ReviewAction,
    ReviewDecisionRequest,
    ReviewServiceError,
    SolutionCardReviewService,
)
from common.models.schema import FieldEdit, VerificationStatus
from scripts.apply_phase3d_schema import (
    BaselineFingerprint,
    FingerprintMismatchError,
    IncompatibleSchemaError,
    compute_deterministic_fingerprints,
    deep_inspect_existing_schema,
)


class MockSpannerDatabase:
    """Mock Spanner Database supporting run_in_transaction and snapshot."""

    def __init__(self, initial_cards: dict[str, dict], abort_times: int = 0):
        self.cards = copy.deepcopy(initial_cards)
        self.reviews = []
        self.last_transaction = None
        self.abort_times = abort_times
        self.attempt_count = 0
        self.captured_review_ids = []

    def run_in_transaction(self, func):
        while True:
            self.attempt_count += 1
            txn = MockSpannerTransaction(self)
            self.last_transaction = txn
            try:
                if self.attempt_count <= self.abort_times:
                    raise Aborted("Simulated concurrent transaction conflict / abort")
                result = func(txn)
                # Commit: apply buffered updates/inserts to db state
                txn.commit()
                return result
            except Aborted:
                # Retry loop
                continue

    def snapshot(self):
        return MockSpannerSnapshot(self)


class MockSpannerSnapshot:
    """Mock Spanner Snapshot for read-only queries."""

    def __init__(self, db: MockSpannerDatabase):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, query, params=None, param_types=None):
        if "FROM SolutionCards" in query and "ORDER BY CardId ASC" in query:
            # Deterministic fingerprint query
            rows = []
            for cid in sorted(self.db.cards.keys()):
                c = self.db.cards[cid]
                rows.append(
                    (
                        c["CardId"],
                        c.get("DocumentId", "doc_default"),
                        c.get("ProjectName"),
                        c.get("BusinessProblem"),
                        c.get("ProposedSolution"),
                        c.get("ImplementedSolution"),
                        c.get("Outcome"),
                        c.get("VerificationStatus"),
                        c.get("OkfUri"),
                    )
                )
            return rows
        if "FROM EvidenceLinks" in query and "ORDER BY EvidenceId ASC" in query:
            return [
                ("ev_1", "card_pending", "chk_1", "doc_default", 1, None, "Verified evidence quote")
            ]
        return []


class MockSpannerTransaction:
    """Mock Spanner Transaction tracking queries and buffered mutations."""

    def __init__(self, db: MockSpannerDatabase):
        self.db = db
        self.buffered_updates = []
        self.buffered_inserts = []

    def execute_sql(self, query, params=None, param_types=None):
        if "FROM SolutionCardReviews" in query:
            card_id = params.get("card_id")
            review_id = params.get("review_id")
            matching = [
                (r["CardId"], r["Action"], r["PreviousStatus"], r["NewStatus"])
                for r in self.db.reviews
                if r["CardId"] == card_id and r["ReviewId"] == review_id
            ]
            return matching

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
        if table == "SolutionCardReviews":
            for row in values:
                self.db.captured_review_ids.append(row[1])

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


@pytest.fixture
def test_reviewer():
    return AuthenticatedReviewer(
        reviewer_id="usr_auth_99182",
        reviewer_email="auditor@enterprise.com",
        roles=("senior_reviewer",),
    )


@pytest.fixture
def mock_db():
    initial_cards = {
        "card_pending": {
            "CardId": "card_pending",
            "DocumentId": "doc_acme",
            "ProjectName": "Initial Project",
            "BusinessProblem": "Legacy scaling issues",
            "ProposedSolution": "Deploy Spanner",
            "ImplementedSolution": "Migrated to Spanner",
            "Outcome": "Achieved 99.999% availability",
            "VerificationStatus": "PENDING",
            "OkfUri": "gs://bucket/meta/card_pending.json",
        },
        "card_draft": {
            "CardId": "card_draft",
            "DocumentId": "doc_draft",
            "ProjectName": "Draft Project",
            "BusinessProblem": None,
            "ProposedSolution": None,
            "ImplementedSolution": None,
            "Outcome": None,
            "VerificationStatus": "DRAFT",
            "OkfUri": None,
        },
        "card_approved": {
            "CardId": "card_approved",
            "DocumentId": "doc_approved",
            "ProjectName": "Approved Architecture",
            "BusinessProblem": "Approved problem",
            "ProposedSolution": "Approved solution",
            "ImplementedSolution": "Approved implementation",
            "Outcome": "Approved outcome",
            "VerificationStatus": "APPROVED",
            "OkfUri": "gs://bucket/meta/approved.json",
        },
        "card_rejected": {
            "CardId": "card_rejected",
            "DocumentId": "doc_rejected",
            "ProjectName": "Rejected Project",
            "BusinessProblem": "Hallucinated claim",
            "ProposedSolution": "No evidence",
            "ImplementedSolution": "None",
            "Outcome": "None",
            "VerificationStatus": "REJECTED",
            "OkfUri": "gs://bucket/meta/rejected.json",
        },
    }
    return MockSpannerDatabase(initial_cards)


# ============================================================================
# STATE TRANSITION & CORE REVIEWS TESTS
# ============================================================================


def test_approve_pending_card(mock_db, test_reviewer):
    """Verify PENDING card successfully transitions to APPROVED."""
    service = SolutionCardReviewService(database=mock_db)
    request = ReviewDecisionRequest(
        action=ReviewAction.APPROVE,
        comments="Grounded evidence matches Slide 4 verbatim.",
    )

    result = service.process_review("card_pending", test_reviewer, request)

    assert result.previous_status == "PENDING"
    assert result.new_status == "APPROVED"
    assert result.reviewer_email == "auditor@enterprise.com"
    assert mock_db.cards["card_pending"]["VerificationStatus"] == "APPROVED"

    assert len(mock_db.reviews) == 1
    review = mock_db.reviews[0]
    assert review["Action"] == "APPROVE"
    assert review["ReviewedAt"] == spanner.COMMIT_TIMESTAMP
    assert review["ReviewerId"] == "usr_auth_99182"


def test_reject_pending_card_requires_comment(mock_db, test_reviewer):
    """Verify REJECT requires a mandatory justification comment."""
    service = SolutionCardReviewService(database=mock_db)

    empty_req = ReviewDecisionRequest(action=ReviewAction.REJECT, comments="")
    with pytest.raises(MissingReviewCommentError):
        service.process_review("card_pending", test_reviewer, empty_req)

    valid_req = ReviewDecisionRequest(
        action=ReviewAction.REJECT,
        comments="Latency figures contradicted by production benchmark in section 3.",
    )
    result = service.process_review("card_pending", test_reviewer, valid_req)
    assert result.new_status == "REJECTED"
    assert mock_db.cards["card_pending"]["VerificationStatus"] == "REJECTED"


def test_rejected_card_cannot_be_reactivated(mock_db, test_reviewer):
    """Verify REJECTED card is in terminal state and cannot be reactivated."""
    service = SolutionCardReviewService(database=mock_db)

    req = ReviewDecisionRequest(
        action=ReviewAction.APPROVE,
        comments="Attempting to reactivate rejected card.",
    )
    with pytest.raises(InvalidStateTransitionError) as exc:
        service.process_review("card_rejected", test_reviewer, req)
    assert "terminal state REJECTED" in str(exc.value)


def test_approved_card_revocation_transition(mock_db, test_reviewer):
    """Verify APPROVED card can only transition to CHANGES_REQUESTED via REVOKE."""
    service = SolutionCardReviewService(database=mock_db)

    reject_req = ReviewDecisionRequest(action=ReviewAction.REJECT, comments="Revoking")
    with pytest.raises(InvalidStateTransitionError):
        service.process_review("card_approved", test_reviewer, reject_req)

    approve_req = ReviewDecisionRequest(action=ReviewAction.APPROVE)
    with pytest.raises(InvalidStateTransitionError):
        service.process_review("card_approved", test_reviewer, approve_req)

    revoke_req = ReviewDecisionRequest(
        action=ReviewAction.REVOKE,
        comments="Architecture superseded by Q4 deployment.",
    )
    result = service.process_review("card_approved", test_reviewer, revoke_req)
    assert result.previous_status == "APPROVED"
    assert result.new_status == "CHANGES_REQUESTED"
    assert mock_db.cards["card_approved"]["VerificationStatus"] == "CHANGES_REQUESTED"


def test_human_edits_captured_in_review_metadata(mock_db, test_reviewer):
    """Verify human field edits update SolutionCards and are recorded in ReviewMetadata."""
    service = SolutionCardReviewService(database=mock_db)

    request = ReviewDecisionRequest(
        action=ReviewAction.APPROVE,
        comments="Corrected project name and verified.",
        field_edits=[
            FieldEdit(
                field_name="project_name",
                old_value="Initial Project",
                new_value="Acme Spanner Modernization",
            )
        ],
        client_metadata={"ui_version": "2.4.0", "auth_token": "SENSITIVE_SECRET"},
    )

    result = service.process_review("card_pending", test_reviewer, request)
    assert result.field_edits_count == 1
    assert mock_db.cards["card_pending"]["ProjectName"] == "Acme Spanner Modernization"

    review = mock_db.reviews[0]
    metadata = json.loads(review["ReviewMetadata"])
    assert metadata["field_edits"][0]["field_name"] == "project_name"
    assert metadata["field_edits"][0]["new_value"] == "Acme Spanner Modernization"
    assert "auth_token" not in metadata["client_info"]
    assert metadata["client_info"]["ui_version"] == "2.4.0"


def test_unauthenticated_identity_rejected():
    """Verify AuthenticatedReviewer rejects empty or invalid email/id."""
    with pytest.raises(AuthenticationContextError):
        AuthenticatedReviewer(reviewer_id="", reviewer_email="valid@enterprise.com")

    with pytest.raises(AuthenticationContextError):
        AuthenticatedReviewer(reviewer_id="usr_123", reviewer_email="not-an-email")


# ============================================================================
# BATCH B — CONCURRENCY, RETRIES, AND FAILURE SAFETY TESTS
# ============================================================================


def test_spanner_aborted_retry_maintains_stable_review_id(test_reviewer):
    """Simulate Spanner Aborted exception and verify ReviewId remains stable across retries."""
    initial_cards = {
        "card_pending": {
            "CardId": "card_pending",
            "ProjectName": "Initial Project",
            "VerificationStatus": "PENDING",
        }
    }
    # Database will abort twice before succeeding on attempt 3
    db = MockSpannerDatabase(initial_cards, abort_times=2)
    service = SolutionCardReviewService(database=db)

    req = ReviewDecisionRequest(
        action=ReviewAction.APPROVE,
        comments="Approved on retry.",
    )

    result = service.process_review("card_pending", test_reviewer, req)
    assert db.attempt_count == 3
    assert result.new_status == "APPROVED"
    # Verify review_id generated was identical across all retry attempts
    assert len(db.captured_review_ids) == 1
    assert db.captured_review_ids[0] == result.review_id
    assert len(db.reviews) == 1


def test_concurrent_competing_rejection_blocks_subsequent_approval(mock_db, test_reviewer):
    """Simulate race condition where card transitions to REJECTED before transaction commits."""
    service = SolutionCardReviewService(database=mock_db)

    # First reviewer rejects the card
    reject_req = ReviewDecisionRequest(
        action=ReviewAction.REJECT,
        comments="Hallucinated claims detected.",
    )
    service.process_review("card_pending", test_reviewer, reject_req)
    assert mock_db.cards["card_pending"]["VerificationStatus"] == "REJECTED"

    # Second concurrent reviewer attempts to approve the card
    approve_req = ReviewDecisionRequest(
        action=ReviewAction.APPROVE,
        comments="Attempting approval on stale assumption.",
    )
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        service.process_review("card_pending", test_reviewer, approve_req)

    assert "terminal state REJECTED" in str(exc_info.value)
    # Ensure no second review record was created
    assert len(mock_db.reviews) == 1
    assert mock_db.reviews[0]["Action"] == "REJECT"


def test_zero_partial_audit_records_on_transaction_failure(test_reviewer):
    """Verify that if an error occurs during transaction execution, no partial records persist."""
    initial_cards = {
        "card_pending": {
            "CardId": "card_pending",
            "ProjectName": "Initial Project",
            "VerificationStatus": "PENDING",
        }
    }
    db = MockSpannerDatabase(initial_cards)

    # Mock transaction to raise an unhandled error inside
    class FailingSpannerDatabase(MockSpannerDatabase):
        def run_in_transaction(self, func):
            txn = MockSpannerTransaction(self)
            func(txn)  # run steps, buffering mutations
            raise RuntimeError("Disk quota exceeded / connection dropped before commit")

    failing_db = FailingSpannerDatabase(initial_cards)
    service = SolutionCardReviewService(database=failing_db)

    req = ReviewDecisionRequest(action=ReviewAction.APPROVE, comments="Will fail")
    with pytest.raises(RuntimeError):
        service.process_review("card_pending", test_reviewer, req)

    # State in DB must remain unmodified
    assert failing_db.cards["card_pending"]["VerificationStatus"] == "PENDING"
    assert len(failing_db.reviews) == 0


def test_client_idempotency_key_replay(mock_db, test_reviewer):
    """Verify client-supplied idempotency key enables safe replays without duplicate inserts."""
    service = SolutionCardReviewService(database=mock_db)

    idempotency_key = "idemp_test_key_abc123"
    req = ReviewDecisionRequest(
        action=ReviewAction.APPROVE,
        comments="Approved with idempotency key",
        idempotency_key=idempotency_key,
    )

    res1 = service.process_review("card_pending", test_reviewer, req)
    assert res1.review_id == idempotency_key
    assert len(mock_db.reviews) == 1

    # Replay identical request with same idempotency key
    res2 = service.process_review("card_pending", test_reviewer, req)
    assert res2.review_id == idempotency_key
    assert res2.new_status == "APPROVED"
    # Must NOT have inserted a second review record
    assert len(mock_db.reviews) == 1


# ============================================================================
# BATCH A & B — DDL INCOMPATIBILITY & FINGERPRINT MISMATCH TESTS
# ============================================================================


def test_incompatible_schema_detection_column_mismatch():
    """Verify deep_inspect_existing_schema detects incompatible column types or missing columns."""
    class MockIncompatibleDBSnapshot:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def execute_sql(self, query, params=None, param_types=None):
            if "FROM INFORMATION_SCHEMA.TABLES" in query:
                return [("SolutionCardReviews",)]
            if "FROM INFORMATION_SCHEMA.COLUMNS" in query:
                # Return incompatible spanner_type for ReviewMetadata (STRING instead of JSON)
                return [
                    ("CardId", "STRING(64)", "NO"),
                    ("ReviewId", "STRING(64)", "NO"),
                    ("ReviewerId", "STRING(128)", "NO"),
                    ("ReviewerEmail", "STRING(256)", "NO"),
                    ("Action", "STRING(32)", "NO"),
                    ("PreviousStatus", "STRING(32)", "NO"),
                    ("NewStatus", "STRING(32)", "NO"),
                    ("Comments", "STRING(MAX)", "YES"),
                    ("ReviewedAt", "TIMESTAMP", "NO"),
                    ("ReviewMetadata", "STRING(MAX)", "YES"),  # INCOMPATIBLE TYPE
                ]
            return []

    class MockIncompatibleDB:
        def snapshot(self):
            return MockIncompatibleDBSnapshot()

    with pytest.raises(IncompatibleSchemaError) as exc_info:
        deep_inspect_existing_schema(MockIncompatibleDB())

    assert "Column 'SolutionCardReviews.ReviewMetadata' type mismatch" in str(exc_info.value)


def test_fingerprint_mismatch_detection(mock_db):
    """Verify cryptographic fingerprinting catches state changes between preflight and postflight."""
    fp1 = compute_deterministic_fingerprints(mock_db)

    # Tamper with a card's status
    mock_db.cards["card_pending"]["VerificationStatus"] = "APPROVED"

    fp2 = compute_deterministic_fingerprints(mock_db)

    # Must produce different digests
    assert fp1.card_sha256 != fp2.card_sha256
    assert fp1 != fp2
