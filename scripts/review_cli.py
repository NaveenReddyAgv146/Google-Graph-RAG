#!/usr/bin/env python3
"""
Enterprise GraphRAG - Human Review CLI Tool (Phase 3D).

Provides review commands:
  list             - List SolutionCards in review queue (with optional status filter)
  show <card_id>   - Inspect complete card details, grounded evidence citations, and audit history
  approve <card_id> - Approve card
  reject <card_id>  - Reject card (requires --comment)
  request-changes <card_id> - Request changes on card (requires --comment)
  revoke <card_id>  - Revoke an APPROVED card (requires --comment)
"""

import argparse
import json
import os
import sys

# Ensure repository root is on sys.path
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

from common.config import settings
from common.database.review_service import (
    AuthenticatedReviewer,
    CardNotFoundError,
    InvalidStateTransitionError,
    MissingReviewCommentError,
    ReviewAction,
    ReviewDecisionRequest,
    SolutionCardReviewService,
)
from common.models.schema import FieldEdit, VerificationStatus
from google.cloud import spanner


def get_authenticated_cli_reviewer() -> AuthenticatedReviewer:
    """Resolves reviewer identity from environment or gcloud config."""
    reviewer_email = (
        os.getenv("REVIEWER_EMAIL")
        or os.getenv("USER_EMAIL")
        or "architect@enterprise.com"
    )
    reviewer_id = (
        os.getenv("REVIEWER_ID")
        or os.getenv("USER_ID")
        or f"usr_{reviewer_email.split('@')[0]}"
    )
    return AuthenticatedReviewer(
        reviewer_id=reviewer_id,
        reviewer_email=reviewer_email,
        roles=("cli_reviewer",),
    )


def get_spanner_database():
    client = spanner.Client(project=settings.project_id)
    instance = client.instance(settings.spanner_instance_id)
    return instance.database(settings.spanner_database_id)


def cmd_list(args):
    """List solution cards."""
    db = get_spanner_database()
    sql = """
        SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.VerificationStatus, sc.OkfUri,
               (SELECT COUNT(1) FROM EvidenceLinks el WHERE el.CardId = sc.CardId) AS EvidenceCount
        FROM SolutionCards sc
        LEFT JOIN Documents d ON sc.DocumentId = d.DocumentId
    """
    params = {}
    param_types = {}
    if args.status:
        sql += " WHERE sc.VerificationStatus = @status"
        params["status"] = args.status.upper()
        param_types["status"] = spanner.param_types.STRING

    sql += " ORDER BY sc.DocumentId, sc.CardId"

    with db.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(sql, params=params, param_types=param_types))

    print(f"\nFound {len(rows)} SolutionCard(s):")
    print(f"{'STATUS':<18} | {'CARD ID':<50} | {'EVID':<4} | {'PROJECT NAME':<40} | {'DOCUMENT'}")
    print("-" * 140)
    for r in rows:
        st = r[4] or "DRAFT"
        cid = r[0]
        ev_cnt = str(r[6] or 0)
        pname = (r[3] or "")[:38]
        doc = (r[2] or r[1])[:30]
        print(f"{st:<18} | {cid:<50} | {ev_cnt:<4} | {pname:<40} | {doc}")
    print()


def cmd_show(args):
    """Show details of a SolutionCard including evidence and audit trail."""
    db = get_spanner_database()
    card_id = args.card_id

    with db.snapshot(multi_use=True) as snapshot:
        # 1. Card details
        card_sql = """
            SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.BusinessProblem,
                   sc.ProposedSolution, sc.ImplementedSolution, sc.Outcome, sc.VerificationStatus, sc.OkfUri
            FROM SolutionCards sc
            LEFT JOIN Documents d ON sc.DocumentId = d.DocumentId
            WHERE sc.CardId = @card_id
        """
        cards = list(
            snapshot.execute_sql(
                card_sql,
                params={"card_id": card_id},
                param_types={"card_id": spanner.param_types.STRING},
            )
        )
        if not cards:
            print(f"Error: SolutionCard '{card_id}' not found.")
            sys.exit(1)

        c = cards[0]
        print("=" * 100)
        print(f"SOLUTION CARD: {c[0]}")
        print(f"Status:         {c[8] or 'DRAFT'}")
        print(f"Document:       {c[1]} ({c[2]})")
        print(f"Project Name:   {c[3]}")
        print(f"OkfUri:         {c[9]}")
        print("-" * 100)
        print("BUSINESS PROBLEM:\n", c[4] or "<None>")
        print("\nPROPOSED SOLUTION:\n", c[5] or "<None>")
        print("\nIMPLEMENTED SOLUTION:\n", c[6] or "<None>")
        print("\nOUTCOME:\n", c[7] or "<None>")
        print("=" * 100)

        # 2. Evidence citations
        ev_sql_with_claim = """
            SELECT el.EvidenceId, el.ChunkId, el.PageNumber, el.SlideNumber,
                   ec.Modality, ec.TextContent, ec.SourceGcsUri, el.Note, el.ClaimType
            FROM EvidenceLinks el
            LEFT JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
            WHERE el.CardId = @card_id
            ORDER BY el.ClaimType ASC, el.EvidenceId ASC
        """
        ev_sql_legacy = """
            SELECT el.EvidenceId, el.ChunkId, el.PageNumber, el.SlideNumber,
                   ec.Modality, ec.TextContent, ec.SourceGcsUri, el.Note
            FROM EvidenceLinks el
            LEFT JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
            WHERE el.CardId = @card_id
            ORDER BY el.EvidenceId ASC
        """
        try:
            evs = list(
                snapshot.execute_sql(
                    ev_sql_with_claim,
                    params={"card_id": card_id},
                    param_types={"card_id": spanner.param_types.STRING},
                )
            )
            has_claim_type = True
        except Exception:
            evs = list(
                snapshot.execute_sql(
                    ev_sql_legacy,
                    params={"card_id": card_id},
                    param_types={"card_id": spanner.param_types.STRING},
                )
            )
            has_claim_type = False

        # Organize citations by claim type
        grouped_citations: dict[str, list[Any]] = {
            "BUSINESS_PROBLEM": [],
            "PROPOSED_SOLUTION": [],
            "IMPLEMENTED_SOLUTION": [],
            "OUTCOME": [],
            "UNASSIGNED": [],
        }

        for ev in evs:
            claim_t = ev[8] if (has_claim_type and len(ev) > 8 and ev[8]) else None
            if claim_t in grouped_citations:
                grouped_citations[claim_t].append(ev)
            else:
                grouped_citations["UNASSIGNED"].append(ev)

        total_evs = len(evs)
        print(f"\nGROUNDED EVIDENCE CITATIONS ({total_evs}):")
        print("=" * 100)

        claim_labels = [
            ("BUSINESS_PROBLEM", "Business Problem"),
            ("PROPOSED_SOLUTION", "Proposed Solution"),
            ("IMPLEMENTED_SOLUTION", "Implemented Solution"),
            ("OUTCOME", "Outcome / Financial Projections"),
        ]

        for claim_key, claim_label in claim_labels:
            items = grouped_citations[claim_key]
            if items:
                print(f"\n[CLAIM: {claim_key}] ({len(items)} citation{'s' if len(items) > 1 else ''} attached):")
                print("  * Note: Attached citations provide grounding evidence; human review is required for verification.")
                for i, ev in enumerate(items, 1):
                    pg_str = f"Page {ev[2]}" if ev[2] else (f"Slide {ev[3]}" if ev[3] else "N/A")
                    print(f"  [{i}] EvidenceId: {ev[0]} | ChunkId: {ev[1]} | {pg_str} | Modality: {ev[4]}")
                    if ev[7]:
                        print(f"      Citation Note: \"{ev[7]}\"")
                    if ev[5]:
                        snippet = ev[5].replace("\n", " ").strip()
                        if len(snippet) > 200:
                            snippet = snippet[:197] + "..."
                        print(f"      Source Text:   \"{snippet}\"")
            else:
                print(f"\n[CLAIM: {claim_key}] ⚠️ NO CITATIONS ATTACHED (Unverified / Ungrounded)")

        if grouped_citations["UNASSIGNED"]:
            print(f"\n[LEGACY / GENERAL CITATIONS] ({len(grouped_citations['UNASSIGNED'])} citation{'s' if len(grouped_citations['UNASSIGNED']) > 1 else ''}):")
            for i, ev in enumerate(grouped_citations["UNASSIGNED"], 1):
                pg_str = f"Page {ev[2]}" if ev[2] else (f"Slide {ev[3]}" if ev[3] else "N/A")
                print(f"  [{i}] EvidenceId: {ev[0]} | ChunkId: {ev[1]} | {pg_str} | Modality: {ev[4]}")
                if ev[7]:
                    print(f"      Citation Note: \"{ev[7]}\"")
                if ev[5]:
                    snippet = ev[5].replace("\n", " ").strip()
                    if len(snippet) > 200:
                        snippet = snippet[:197] + "..."
                    print(f"      Source Text:   \"{snippet}\"")

        print("\n" + "-" * 100)
        print("EVIDENCE INTEGRITY AUDIT SUMMARY:")
        print(f"  - Card Status:          {c[8] or 'DRAFT'} (Mandatory human review required for APPROVAL)")
        for claim_key, claim_label in claim_labels:
            cnt = len(grouped_citations[claim_key])
            flag = f"ATTACHED ({cnt} citation{'s' if cnt > 1 else ''}) [Pending human review]" if cnt > 0 else "⚠️ UNGROUNDED (0 citations attached)"
            print(f"  - {claim_label:<25} {flag}")
        if grouped_citations["UNASSIGNED"]:
            print(f"  - Legacy Citations:       {len(grouped_citations['UNASSIGNED'])} general citation(s)")
        print("  * Reminder: Attaching citations establishes provenance. Human review remains mandatory to APPROVE.")
        print("=" * 100)

        # 3. Review History
        rev_sql = """
            SELECT ReviewId, ReviewerEmail, Action, PreviousStatus, NewStatus, Comments, ReviewedAt, ReviewMetadata
            FROM SolutionCardReviews
            WHERE CardId = @card_id
            ORDER BY ReviewedAt DESC
        """
        revs = list(
            snapshot.execute_sql(
                rev_sql,
                params={"card_id": card_id},
                param_types={"card_id": spanner.param_types.STRING},
            )
        )
        print(f"AUDIT HISTORY ({len(revs)}):")
        for r in revs:
            print(f"- [{r[6]}] {r[1]} applied '{r[2]}': {r[3]} -> {r[4]}")
            if r[5]:
                print(f"  Comment: \"{r[5]}\"")
            if r[7]:
                print(f"  Metadata: {r[7]}")
        print()


def cmd_action(action: ReviewAction, args):
    """Executes a review action."""
    db = get_spanner_database()
    reviewer = get_authenticated_cli_reviewer()
    service = SolutionCardReviewService(database=db)

    # Optional field edits
    field_edits = []
    if getattr(args, "edit_project_name", None):
        field_edits.append(FieldEdit(field_name="project_name", old_value=None, new_value=args.edit_project_name))

    request = ReviewDecisionRequest(
        action=action,
        comments=args.comment,
        field_edits=field_edits,
        idempotency_key=getattr(args, "idempotency_key", None),
        client_metadata={"source": "cli_tool", "cli_user": reviewer.reviewer_id},
    )

    try:
        res = service.process_review(card_id=args.card_id, reviewer=reviewer, decision=request)
        print("\nREVIEW SUCCESSFUL:")
        print(f"  CardId:         {res.card_id}")
        print(f"  ReviewId:       {res.review_id}")
        print(f"  Action:         {res.action}")
        print(f"  Transition:     {res.previous_status} -> {res.new_status}")
        print(f"  Reviewer:       {res.reviewer_email} ({res.reviewer_id})")
        print(f"  Field Edits:    {res.field_edits_count}")
        print()
    except (CardNotFoundError, InvalidStateTransitionError, MissingReviewCommentError) as exc:
        print(f"\nError: {exc}\n", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Human Review CLI for SolutionCards (Phase 3D)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List SolutionCards in review queue")
    p_list.add_argument("--status", choices=["DRAFT", "PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"], help="Filter by status")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subparsers.add_parser("show", help="Inspect card details and grounded evidence")
    p_show.add_argument("card_id", help="CardId to inspect")
    p_show.set_defaults(func=cmd_show)

    # approve
    p_app = subparsers.add_parser("approve", help="Approve card")
    p_app.add_argument("card_id", help="CardId to approve")
    p_app.add_argument("--comment", help="Optional approval notes")
    p_app.add_argument("--edit-project-name", help="Correct project name")
    p_app.add_argument("--idempotency-key", help="Optional idempotency key")
    p_app.set_defaults(func=lambda args: cmd_action(ReviewAction.APPROVE, args))

    # reject
    p_rej = subparsers.add_parser("reject", help="Reject card")
    p_rej.add_argument("card_id", help="CardId to reject")
    p_rej.add_argument("--comment", required=True, help="Mandatory justification for rejection")
    p_rej.add_argument("--idempotency-key", help="Optional idempotency key")
    p_rej.set_defaults(func=lambda args: cmd_action(ReviewAction.REJECT, args))

    # request-changes
    p_chg = subparsers.add_parser("request-changes", help="Request changes on card")
    p_chg.add_argument("card_id", help="CardId to request changes on")
    p_chg.add_argument("--comment", required=True, help="Mandatory explanation of requested changes")
    p_chg.add_argument("--idempotency-key", help="Optional idempotency key")
    p_chg.set_defaults(func=lambda args: cmd_action(ReviewAction.REQUEST_CHANGES, args))

    # revoke
    p_rev = subparsers.add_parser("revoke", help="Revoke an APPROVED card")
    p_rev.add_argument("card_id", help="CardId to revoke")
    p_rev.add_argument("--comment", required=True, help="Mandatory justification for revocation")
    p_rev.add_argument("--idempotency-key", help="Optional idempotency key")
    p_rev.set_defaults(func=lambda args: cmd_action(ReviewAction.REVOKE, args))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

