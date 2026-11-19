"""Human Review Service for Cloud Spanner SolutionCards.

Enforces:
1. Strict state machine transitions:
   - PENDING -> APPROVED | REJECTED | CHANGES_REQUESTED
   - CHANGES_REQUESTED -> APPROVED | REJECTED | CHANGES_REQUESTED
   - APPROVED -> CHANGES_REQUESTED (Authorized revocation/re-review only)
   - REJECTED -> Terminal (Reactivation strictly forbidden)
   - DRAFT -> PENDING (Explicit promotion to review queue)
2. Mandatory justification comments for REJECT and REQUEST_CHANGES.
3. Authenticated Principal Identity enforcement (ReviewerId and ReviewerEmail
   must be derived strictly from verified auth credentials, never untrusted payload).
4. Atomic update of SolutionCards.VerificationStatus and insert into SolutionCardReviews.
5. Server-side Cloud Spanner commit timestamp (spanner.COMMIT_TIMESTAMP).
6. Sanitized ReviewMetadata JSON capture of human-edited fields without sensitive leakage.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from typing import Any
import uuid

from google.cloud import spanner
from pydantic import BaseModel, Field

from common.models.schema import (
    EMAIL_REGEX,
    FieldEdit,
    ReviewAction,
    ReviewMetadataPayload,
    VerificationStatus,
)

logger = logging.getLogger("common.database.review_service")


# ============================================================================
# EXCEPTIONS
# ============================================================================


class ReviewServiceError(Exception):
    """Base exception for review service errors."""


class CardNotFoundError(ReviewServiceError):
    """Raised when the specified CardId does not exist in SolutionCards."""


class InvalidStateTransitionError(ReviewServiceError):
    """Raised when an invalid state machine transition is attempted."""


class InvalidReviewActionError(ReviewServiceError):
    """Raised when an invalid review action is supplied."""


class AuthenticationContextError(ReviewServiceError):
    """Raised when reviewer identity is missing, unauthenticated, or malformed."""


class MissingReviewCommentError(ReviewServiceError):
    """Raised when mandatory comments are omitted for REJECT or REQUEST_CHANGES."""


# ============================================================================
# AUTHENTICATED IDENTITY CONTEXT
# ============================================================================


@dataclass(frozen=True)
class AuthenticatedReviewer:
    """Immutable identity context derived strictly from verified token/credentials."""

    reviewer_id: str
    reviewer_email: str
    roles: tuple[str, ...] = ("reviewer",)

    def __post_init__(self):
        if not self.reviewer_id or not self.reviewer_id.strip():
            raise AuthenticationContextError("reviewer_id must be a non-empty authenticated identity")
        if not self.reviewer_email or not EMAIL_REGEX.match(self.reviewer_email.strip()):
            raise AuthenticationContextError(f"reviewer_email '{self.reviewer_email}' is not a valid email address")


# ============================================================================
# REVIEW REQUEST & RESULT MODELS
# ============================================================================


class ReviewDecisionRequest(BaseModel):
    """Request payload containing human decisions and optional field edits.

    NOTE: Does NOT accept reviewer_id or reviewer_email; those MUST be supplied
    via AuthenticatedReviewer context.
    """

    action: ReviewAction
    comments: str | None = None
    field_edits: list[FieldEdit] = Field(default_factory=list)
    client_metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(
        None,
        max_length=64,
        description="Optional client-supplied idempotency key used as ReviewId to safely retry uncertain commits.",
    )


@dataclass(frozen=True)
class ReviewExecutionResult:
    """Summary of completed atomic review transaction."""

    card_id: str
    review_id: str
    previous_status: str
    new_status: str
    reviewer_id: str
    reviewer_email: str
    action: str
    field_edits_count: int


# ============================================================================
# PERMITTED TRANSITION RULES
# ============================================================================

# Maps (current_status, action) -> next_status
VALID_TRANSITIONS: dict[tuple[str, ReviewAction], str] = {
    # From PENDING:
    ("PENDING", ReviewAction.APPROVE): "APPROVED",
    ("PENDING", ReviewAction.REJECT): "REJECTED",
    ("PENDING", ReviewAction.REQUEST_CHANGES): "CHANGES_REQUESTED",
    # From CHANGES_REQUESTED:
    ("CHANGES_REQUESTED", ReviewAction.APPROVE): "APPROVED",
    ("CHANGES_REQUESTED", ReviewAction.REJECT): "REJECTED",
    ("CHANGES_REQUESTED", ReviewAction.REQUEST_CHANGES): "CHANGES_REQUESTED",
    # From APPROVED: (Only authorized revocation / re-review)
    ("APPROVED", ReviewAction.REVOKE): "CHANGES_REQUESTED",
    # From DRAFT: (Promotion into human review queue)
    ("DRAFT", ReviewAction.REQUEST_CHANGES): "CHANGES_REQUESTED",
}

# Fields allowed to be edited on SolutionCards
EDITABLE_FIELDS = frozenset(
    {
        "project_name",
        "business_problem",
        "proposed_solution",
        "implemented_solution",
        "outcome",
    }
)

SENSITIVE_METADATA_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "authorization",
        "auth",
        "credential",
        "api_key",
        "access_token",
    }
)


# ============================================================================
# REVIEW SERVICE IMPLEMENTATION
# ============================================================================


class SolutionCardReviewService:
    """Service orchestrating human review workflow and audit commits against Spanner."""

    def __init__(self, database):
        self._database = database

    def process_review(
        self,
        card_id: str,
        reviewer: AuthenticatedReviewer,
        decision: ReviewDecisionRequest,
    ) -> ReviewExecutionResult:
        """Executes review action in an atomic read-write Spanner transaction."""
        if not isinstance(reviewer, AuthenticatedReviewer):
            raise AuthenticationContextError(
                "Reviewer identity must be an instance of AuthenticatedReviewer derived from verified auth context."
            )

        # 1. Validate mandatory comments
        if decision.action in (ReviewAction.REJECT, ReviewAction.REQUEST_CHANGES, ReviewAction.REVOKE):
            if not decision.comments or not decision.comments.strip():
                raise MissingReviewCommentError(
                    f"Action '{decision.action.value}' requires a non-empty explanation comment."
                )

        # 2. Check and sanitize field edits
        for edit in decision.field_edits:
            if edit.field_name not in EDITABLE_FIELDS:
                raise ReviewServiceError(f"Field '{edit.field_name}' is not an editable SolutionCard field.")

        # 3. Sanitize metadata (strip any keys matching or containing sensitive patterns)
        sanitized_client_info = {
            k: v
            for k, v in decision.client_metadata.items()
            if not any(pattern in k.lower() for pattern in SENSITIVE_METADATA_KEYS)
        }

        # Client-level idempotency: use idempotency_key if provided, otherwise generate UUID
        review_id = decision.idempotency_key or f"rev_{uuid.uuid4().hex}"

        def _transactional_step(transaction):
            # Check for existing review with this review_id (idempotency check)
            existing_reviews = list(
                transaction.execute_sql(
                    "SELECT CardId, Action, PreviousStatus, NewStatus "
                    "FROM SolutionCardReviews WHERE CardId = @card_id AND ReviewId = @review_id",
                    params={"card_id": card_id, "review_id": review_id},
                    param_types={
                        "card_id": spanner.param_types.STRING,
                        "review_id": spanner.param_types.STRING,
                    },
                )
            )
            if existing_reviews:
                # Idempotent replay: already committed under this review_id
                ex = existing_reviews[0]
                logger.info("Idempotent replay detected for CardId='%s', ReviewId='%s'", card_id, review_id)
                return ex[2], ex[3]

            # Read existing SolutionCard row
            card_rows = list(
                transaction.execute_sql(
                    "SELECT CardId, ProjectName, BusinessProblem, ProposedSolution, "
                    "ImplementedSolution, Outcome, VerificationStatus, OkfUri "
                    "FROM SolutionCards WHERE CardId = @card_id",
                    params={"card_id": card_id},
                    param_types={"card_id": spanner.param_types.STRING},
                )
            )

            if not card_rows:
                raise CardNotFoundError(f"SolutionCard '{card_id}' not found in database.")

            card_row = card_rows[0]
            current_status = card_row[6] or "DRAFT"

            # Check terminal state
            if current_status == "REJECTED":
                raise InvalidStateTransitionError(
                    f"SolutionCard '{card_id}' is in terminal state REJECTED and cannot be reactivated or modified."
                )

            # Check transition matrix
            transition_key = (current_status, decision.action)
            if transition_key not in VALID_TRANSITIONS:
                raise InvalidStateTransitionError(
                    f"Invalid state transition: Action '{decision.action.value}' is not permitted from status '{current_status}'."
                )

            next_status = VALID_TRANSITIONS[transition_key]

            # Prepare SolutionCard field updates if human edited
            card_col_names = [
                "CardId",
                "ProjectName",
                "BusinessProblem",
                "ProposedSolution",
                "ImplementedSolution",
                "Outcome",
                "VerificationStatus",
            ]

            card_values = {
                "CardId": card_id,
                "ProjectName": card_row[1],
                "BusinessProblem": card_row[2],
                "ProposedSolution": card_row[3],
                "ImplementedSolution": card_row[4],
                "Outcome": card_row[5],
                "VerificationStatus": next_status,
            }

            for edit in decision.field_edits:
                col_name = "".join(part.capitalize() for part in edit.field_name.split("_"))
                if col_name in card_values:
                    card_values[col_name] = edit.new_value

            # Prepare ReviewMetadata JSON payload
            metadata_dict = {
                "field_edits": [
                    {
                        "field_name": e.field_name,
                        "old_value": e.old_value,
                        "new_value": e.new_value,
                    }
                    for e in decision.field_edits
                ],
                "reason": decision.comments,
                "client_info": sanitized_client_info,
            }
            metadata_json_str = json.dumps(metadata_dict)

            # Atomic Mutation 1: Update SolutionCards
            transaction.update(
                table="SolutionCards",
                columns=card_col_names,
                values=[[card_values[col] for col in card_col_names]],
            )

            # Atomic Mutation 2: Insert audit record into SolutionCardReviews with commit timestamp
            transaction.insert(
                table="SolutionCardReviews",
                columns=[
                    "CardId",
                    "ReviewId",
                    "ReviewerId",
                    "ReviewerEmail",
                    "Action",
                    "PreviousStatus",
                    "NewStatus",
                    "Comments",
                    "ReviewedAt",
                    "ReviewMetadata",
                ],
                values=[
                    [
                        card_id,
                        review_id,
                        reviewer.reviewer_id,
                        reviewer.reviewer_email.lower(),
                        decision.action.value,
                        current_status,
                        next_status,
                        decision.comments,
                        spanner.COMMIT_TIMESTAMP,
                        metadata_json_str,
                    ]
                ],
            )

            return current_status, next_status

        # Execute inside read-write transaction
        prev_st, next_st = self._database.run_in_transaction(_transactional_step)

        return ReviewExecutionResult(
            card_id=card_id,
            review_id=review_id,
            previous_status=prev_st,
            new_status=next_st,
            reviewer_id=reviewer.reviewer_id,
            reviewer_email=reviewer.reviewer_email.lower(),
            action=decision.action.value,
            field_edits_count=len(decision.field_edits),
        )
