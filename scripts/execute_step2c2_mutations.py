#!/usr/bin/env python3
"""
Step 2C.2 – Authorized Uber Claim-Level Evidence Mutations Execution.
Repository: /home/naveen/enterprise-graphrag
Database: projects/xenon-broker-513704-s9/instances/sales-discovery-trial/databases/sales-knowledge-db
Target Card: card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d

Performs:
1. Strict precondition checks (aborts if any fail).
2. Atomic Cloud Spanner read-write transaction (1 update in place, 5 inserts, 1 card text update).
3. Comprehensive post-execution verification.
"""

import hashlib
import json
import os
import subprocess
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

from google.cloud import spanner
from google.oauth2.credentials import Credentials
from common.config import settings

EXPECTED_PROJECT = "xenon-broker-513704-s9"
EXPECTED_INSTANCE = "sales-discovery-trial"
EXPECTED_DATABASE = "sales-knowledge-db"

CARD_ID = "card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d"
DOC_ID = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
EXISTING_EVIDENCE_ID = "evl_fd27d9a5d2176a0785b1f407bb67213cb0b24fd8cd60503d"

BEFORE_IMPLEMENTED_SOLUTION = (
    "1-Click request from Geo-aware devices, SMS booking ('pickup @work in 5'), "
    "mobile applications for iPhone, Blackberry, and Symbian, a web platform "
    "(UberCab.com) integrated with Google Maps, and a pre-paid, cashless billing system."
)

AFTER_IMPLEMENTED_SOLUTION = (
    "Foundational milestones completed in 2008: California LLC + trademark filed, "
    "provisional patent filed, iPhone dev license applied for (Nov 28, 2008), "
    "Ubercab.com and SMS code reserved, bank/PayPal account created, and 15 trial "
    "clients/5 advisors recruited. (Target architecture designed for 1-click dispatch; "
    "mobile app development and car fleet acquisition were future planned milestones under 'NEXT')."
)

NEW_EVIDENCE_RECORDS = [
    {
        "evidence_id": "evl_45d5c060053d051b5ca389301add648295302bcfffee106b",
        "card_id": CARD_ID,
        "chunk_id": "chunk-165f0bd0d002a0894536ae7f4d776dd7c7c402a8d5f75fbb",
        "document_id": DOC_ID,
        "page_number": 2,
        "slide_number": None,
        "note": (
            "Most use aging & inefficient technology\n\n"
            "   —  Radio dispatch, no 2-way communication\n\n"
            "   —  Most common car, Ford Crown Victoria = 14mpg\n\n"
            " *  Hailing is done by hand or phone\n\n"
            "   — No GPS coordination between client/driver\n\n"
            "   —  Significant fareseeking or “dead-time”"
        ),
        "claim_type": "BUSINESS_PROBLEM",
    },
    {
        "evidence_id": "evl_13be5f4ad0e9df45ed5d9450fd52d667375d199128df00f3",
        "card_id": CARD_ID,
        "chunk_id": "chunk-2a4aa82038e62e9e55ebd7540a9a55fcf5c17ac11cb4a53a",
        "document_id": DOC_ID,
        "page_number": 3,
        "slide_number": None,
        "note": (
            "Taxi-monopolies reduce quality of service\n"
            "   — Medallions are expensive, and drivers underpaid\n\n\n\n\n\n"
            "   — Medallions cost ~$500k, drivers make 31k\n"
            "   — No incentive/accountability for drivers/clients"
        ),
        "claim_type": "BUSINESS_PROBLEM",
    },
    {
        "evidence_id": "evl_1587c3cf9eeb6aa123613db7d0aede7264b6c34ceb7c16d6",
        "card_id": CARD_ID,
        "chunk_id": "chunk-92a6b957c622cc72388015d014e32c3ede07fb0d440d3ed0",
        "document_id": DOC_ID,
        "page_number": 12,
        "slide_number": None,
        "note": (
            "In NYC, 35% of time is spend looking for fares\n\n"
            "   — In less dense cities, can be over half “dead-time”"
        ),
        "claim_type": "BUSINESS_PROBLEM",
    },
    {
        "evidence_id": "evl_d7d92f11de7e16cbb88b9cf28f613a8f69a3e455719133db",
        "card_id": CARD_ID,
        "chunk_id": "chunk-b2809c5eb5009cd4a1e5b2426de3ff6427aa1c6ef791f0f8",
        "document_id": DOC_ID,
        "page_number": 25,
        "slide_number": None,
        "note": (
            "Progress to Date\n\n\n"
            " * Ubercab.com reserved + “ubercab” SMS code\n\n"
            " ¢  California LLC + trademark filed\n\n"
            " ¢ Iphone dev license applied for Nov28,08\n"
            " * Bank Account + Paypal account created\n\n"
            " ¢ 5 advisors & 15 clients now recruited\n\n"
            " ¢  Provisional patent filed"
        ),
        "claim_type": "IMPLEMENTED_SOLUTION",
    },
    {
        "evidence_id": "evl_b1f973e8d3c70b6927273e63cc0156fa16031ef5f777932d",
        "card_id": CARD_ID,
        "chunk_id": "chunk-6d660e6633fa4738d44735ffc6dbecec9e6fd97ecba7864e",
        "document_id": DOC_ID,
        "page_number": 20,
        "slide_number": None,
        "note": (
            "POtential Outcomes\n\n\n\n"
            " *  Best-Case Scenario\n\n"
            "   — Becomes market leader, $1B+ in yearly revenue\n\n"
            " *  Realistic Success Scenario\n\n"
            "   —  Gets 5% of the top 5 US Cities\n\n"
            "   —  Generates 20-30M+ per year profit\n\n"
            " * Worst-Case Scenario\n\n"
            "   —  Remains a 10 car, 100 client service in SF"
        ),
        "claim_type": "OUTCOME",
    },
]

NEW_EVIDENCE_IDS = [r["evidence_id"] for r in NEW_EVIDENCE_RECORDS]


def run_preconditions(db):
    print("=" * 100)
    print("STEP 1: PRECONDITIONS AUDIT")
    print("=" * 100)

    # 1. Active project and database
    print(f"Target Project:  {settings.project_id} (Expected: {EXPECTED_PROJECT})")
    print(f"Target Instance: {settings.spanner_instance_id} (Expected: {EXPECTED_INSTANCE})")
    print(f"Target Database: {settings.spanner_database_id} (Expected: {EXPECTED_DATABASE})")
    if settings.project_id != EXPECTED_PROJECT:
        raise ValueError(f"Target project mismatch: {settings.project_id} != {EXPECTED_PROJECT}")
    if settings.spanner_instance_id != EXPECTED_INSTANCE:
        raise ValueError(f"Target instance mismatch: {settings.spanner_instance_id} != {EXPECTED_INSTANCE}")
    if settings.spanner_database_id != EXPECTED_DATABASE:
        raise ValueError(f"Target database mismatch: {settings.spanner_database_id} != {EXPECTED_DATABASE}")
    print("Precondition 1: Database configuration verified.")

    with db.snapshot(multi_use=True) as snapshot:
        # 2. Table row counts
        sc_count = list(snapshot.execute_sql("SELECT COUNT(*) FROM SolutionCards"))[0][0]
        ev_count = list(snapshot.execute_sql("SELECT COUNT(*) FROM EvidenceLinks"))[0][0]
        print(f"SolutionCards count: {sc_count} (Expected: 11)")
        print(f"EvidenceLinks count: {ev_count} (Expected: 6)")
        if sc_count != 11:
            raise ValueError(f"SolutionCards row count mismatch: {sc_count} != 11")
        if ev_count != 6:
            raise ValueError(f"EvidenceLinks row count mismatch: {ev_count} != 6")
        print("Precondition 2: Baseline table row counts verified.")

        # 3. Uber card exists and is PENDING
        card_rows = list(snapshot.execute_sql("""
            SELECT CardId, ProjectName, ImplementedSolution, VerificationStatus
            FROM SolutionCards
            WHERE CardId = @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))
        if not card_rows:
            raise ValueError(f"Uber SolutionCard {CARD_ID} not found!")
        card = card_rows[0]
        print(f"Uber Card Status: {card[3]} (Expected: PENDING)")
        if card[3] != "PENDING":
            raise ValueError(f"Uber card status is {card[3]}, expected PENDING!")
        print("Precondition 3: Uber card exists and is PENDING.")

        # 4. ImplementedSolution matches BEFORE value
        current_impl = card[2]
        if current_impl != BEFORE_IMPLEMENTED_SOLUTION:
            print(f"Current ImplementedSolution: {current_impl!r}")
            print(f"Expected BEFORE:             {BEFORE_IMPLEMENTED_SOLUTION!r}")
            raise ValueError("Uber card ImplementedSolution does not match audited BEFORE value!")
        print("Precondition 4: ImplementedSolution matches audited BEFORE text exactly.")

        # 5. Existing Page 4 EvidenceId exists exactly once
        existing_rows = list(snapshot.execute_sql("""
            SELECT EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, ClaimType
            FROM EvidenceLinks
            WHERE EvidenceId = @eid
        """, params={"eid": EXISTING_EVIDENCE_ID}, param_types={"eid": spanner.param_types.STRING}))
        if len(existing_rows) != 1:
            raise ValueError(f"Expected exactly 1 existing EvidenceLink {EXISTING_EVIDENCE_ID}, found {len(existing_rows)}")
        ex = existing_rows[0]
        if ex[1] != CARD_ID:
            raise ValueError(f"Existing EvidenceLink CardId mismatch: {ex[1]} != {CARD_ID}")
        print("Precondition 5: Existing Page 4 EvidenceId exists exactly once.")

        # 6. All 5 new EvidenceIds are absent
        present_new_ids = list(snapshot.execute_sql("""
            SELECT EvidenceId FROM EvidenceLinks WHERE EvidenceId IN UNNEST(@ids)
        """, params={"ids": NEW_EVIDENCE_IDS}, param_types={"ids": spanner.param_types.Array(spanner.param_types.STRING)}))
        if present_new_ids:
            found = [r[0] for r in present_new_ids]
            raise ValueError(f"New EvidenceIds already present in database: {found}")
        print("Precondition 6: All 5 new EvidenceIds are completely absent from database.")

        # 7. Unrelated cards (10) and links (5) baseline check
        other_cards_count = list(snapshot.execute_sql("""
            SELECT COUNT(*) FROM SolutionCards WHERE CardId != @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))[0][0]
        other_links_count = list(snapshot.execute_sql("""
            SELECT COUNT(*) FROM EvidenceLinks WHERE CardId != @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))[0][0]
        if other_cards_count != 10:
            raise ValueError(f"Expected 10 other SolutionCards, found {other_cards_count}")
        if other_links_count != 5:
            raise ValueError(f"Expected 5 other EvidenceLinks, found {other_links_count}")
        print("Precondition 7: 10 other SolutionCards and 5 other EvidenceLinks confirmed at baseline.")

    print("\nALL PRECONDITIONS PASSED SUCCESSFULLY. PROCEEDING TO ATOMIC TRANSACTION.")


def execute_transaction(db):
    print("\n" + "=" * 100)
    print("STEP 2: ATOMIC READ-WRITE TRANSACTION EXECUTION")
    print("=" * 100)

    def unit_of_work(transaction):
        # 1. Update existing Page 4 EvidenceLink
        print("Executing Operation 1: UPDATE existing Page 4 EvidenceLink...")
        update_ev_sql = """
            UPDATE EvidenceLinks
            SET ClaimType = 'PROPOSED_SOLUTION',
                SlideNumber = NULL
            WHERE EvidenceId = @eid AND CardId = @cid
        """
        ev_update_count = transaction.execute_update(
            update_ev_sql,
            params={"eid": EXISTING_EVIDENCE_ID, "cid": CARD_ID},
            param_types={"eid": spanner.param_types.STRING, "cid": spanner.param_types.STRING},
        )
        print(f"  Rows updated for existing EvidenceLink: {ev_update_count}")
        if ev_update_count != 1:
            raise RuntimeError(f"Expected 1 row updated for existing EvidenceLink, got {ev_update_count}")

        # 2. Insert 5 new EvidenceLinks
        print("Executing Operation 2: INSERT 5 new EvidenceLinks...")
        insert_sql = """
            INSERT INTO EvidenceLinks (
                EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, Note, ClaimType
            ) VALUES (
                @eid, @cid, @chunk_id, @doc_id, @page_num, @slide_num, @note, @claim_type
            )
        """
        for r in NEW_EVIDENCE_RECORDS:
            inserted = transaction.execute_update(
                insert_sql,
                params={
                    "eid": r["evidence_id"],
                    "cid": r["card_id"],
                    "chunk_id": r["chunk_id"],
                    "doc_id": r["document_id"],
                    "page_num": r["page_number"],
                    "slide_num": r["slide_number"],
                    "note": r["note"],
                    "claim_type": r["claim_type"],
                },
                param_types={
                    "eid": spanner.param_types.STRING,
                    "cid": spanner.param_types.STRING,
                    "chunk_id": spanner.param_types.STRING,
                    "doc_id": spanner.param_types.STRING,
                    "page_num": spanner.param_types.INT64,
                    "slide_num": spanner.param_types.INT64,
                    "note": spanner.param_types.STRING,
                    "claim_type": spanner.param_types.STRING,
                },
            )
            if inserted != 1:
                raise RuntimeError(f"Failed to insert EvidenceLink {r['evidence_id']}, affected {inserted} rows")
            print(f"  Inserted {r['evidence_id']} [{r['claim_type']}] Page {r['page_number']}")

        # 3. Update target card's ImplementedSolution field
        print("Executing Operation 3: UPDATE SolutionCards.ImplementedSolution...")
        update_card_sql = """
            UPDATE SolutionCards
            SET ImplementedSolution = @after_text
            WHERE CardId = @cid AND VerificationStatus = 'PENDING'
        """
        card_update_count = transaction.execute_update(
            update_card_sql,
            params={"after_text": AFTER_IMPLEMENTED_SOLUTION, "cid": CARD_ID},
            param_types={"after_text": spanner.param_types.STRING, "cid": spanner.param_types.STRING},
        )
        print(f"  Rows updated for SolutionCards: {card_update_count}")
        if card_update_count != 1:
            raise RuntimeError(f"Expected 1 row updated for SolutionCards, got {card_update_count}")

    commit_timestamp = db.run_in_transaction(unit_of_work)
    print(f"\nTRANSACTION COMMITTED AT: {commit_timestamp}")
    return commit_timestamp


def post_execution_verification(db):
    print("\n" + "=" * 100)
    print("STEP 3: POST-EXECUTION VERIFICATION")
    print("=" * 100)

    with db.snapshot(multi_use=True) as snapshot:
        # 1. EvidenceLinks total count
        ev_total = list(snapshot.execute_sql("SELECT COUNT(*) FROM EvidenceLinks"))[0][0]
        print(f"EvidenceLinks total rows: {ev_total} (Expected: 11)")
        assert ev_total == 11, f"Expected 11 total EvidenceLinks, found {ev_total}"

        # 2. Uber EvidenceLinks count
        uber_evs = list(snapshot.execute_sql("""
            SELECT EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, Note, ClaimType
            FROM EvidenceLinks
            WHERE CardId = @cid
            ORDER BY PageNumber ASC
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))
        print(f"Uber EvidenceLinks count: {len(uber_evs)} (Expected: 6)")
        assert len(uber_evs) == 6, f"Expected 6 Uber EvidenceLinks, found {len(uber_evs)}"

        # 3. Verify all 6 ClaimTypes, Pages, SlideNumbers, and Notes
        print("\nVerifying all 6 Uber Evidence records in Spanner:")
        expected_claims_by_id = {r["evidence_id"]: r for r in NEW_EVIDENCE_RECORDS}
        for ev in uber_evs:
            eid, cid, chunk_id, doc_id, pg, slide, note, claim_type = ev
            print(f"  - EvidenceId: {eid}")
            print(f"    ClaimType:   {claim_type}")
            print(f"    Page/Slide:  {pg} / {slide}")
            print(f"    ChunkId:     {chunk_id}")
            print(f"    DocId:       {doc_id}")
            assert slide is None, f"SlideNumber for {eid} must be None, got {slide}"
            assert doc_id == DOC_ID, f"DocumentId mismatch: {doc_id} != {DOC_ID}"

            if eid == EXISTING_EVIDENCE_ID:
                assert claim_type == "PROPOSED_SOLUTION", f"ClaimType mismatch: {claim_type} != PROPOSED_SOLUTION"
                assert pg == 4, f"PageNumber mismatch for existing link: {pg} != 4"
            else:
                assert eid in expected_claims_by_id, f"Unexpected EvidenceId {eid}"
                exp = expected_claims_by_id[eid]
                assert claim_type == exp["claim_type"], f"ClaimType mismatch for {eid}"
                assert pg == exp["page_number"], f"Page mismatch for {eid}"
                assert chunk_id == exp["chunk_id"], f"ChunkId mismatch for {eid}"
                assert note == exp["note"], f"Note mismatch for {eid}"
        print("All 6 Uber EvidenceLinks verified with 100% precision.")

        # 4. Existing Page 4 EvidenceId preserved
        existing_check = [ev for ev in uber_evs if ev[0] == EXISTING_EVIDENCE_ID]
        assert len(existing_check) == 1, "Existing Page 4 EvidenceId not preserved!"
        print(f"\nExisting Page 4 EvidenceId {EXISTING_EVIDENCE_ID} verified preserved.")

        # 5. SolutionCards total remains 11
        sc_total = list(snapshot.execute_sql("SELECT COUNT(*) FROM SolutionCards"))[0][0]
        print(f"SolutionCards total rows: {sc_total} (Expected: 11)")
        assert sc_total == 11, f"Expected 11 total SolutionCards, found {sc_total}"

        # 6. Uber card ImplementedSolution and status
        uber_card = list(snapshot.execute_sql("""
            SELECT CardId, ProjectName, ImplementedSolution, VerificationStatus
            FROM SolutionCards
            WHERE CardId = @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))[0]
        print(f"Uber Card Status:             {uber_card[3]} (Expected: PENDING)")
        assert uber_card[3] == "PENDING", f"Uber card status changed to {uber_card[3]}!"
        print(f"Uber ImplementedSolution:    {uber_card[2]}")
        assert uber_card[2] == AFTER_IMPLEMENTED_SOLUTION, "ImplementedSolution does not match approved AFTER text!"
        print("Uber SolutionCard text and status verified.")

        # 7. Unrelated records integrity
        other_cards_count = list(snapshot.execute_sql("""
            SELECT COUNT(*) FROM SolutionCards WHERE CardId != @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))[0][0]
        other_links_count = list(snapshot.execute_sql("""
            SELECT COUNT(*) FROM EvidenceLinks WHERE CardId != @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))[0][0]
        assert other_cards_count == 10, f"Other SolutionCards modified! Count: {other_cards_count}"
        assert other_links_count == 5, f"Other EvidenceLinks modified! Count: {other_links_count}"
        print(f"Unrelated records verified: 10 SolutionCards and 5 EvidenceLinks untouched.")

        # 8. No duplicate EvidenceIds across entire table
        all_ev_ids = list(r[0] for r in snapshot.execute_sql("SELECT EvidenceId FROM EvidenceLinks"))
        assert len(all_ev_ids) == len(set(all_ev_ids)), "Duplicate EvidenceIds detected in EvidenceLinks!"
        print(f"All {len(all_ev_ids)} EvidenceIds across table are strictly unique.")

    print("\n" + "=" * 100)
    print("ALL POST-EXECUTION VERIFICATIONS PASSED (100% SUCCESS)")
    print("=" * 100)


def main():
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    creds = Credentials(token)
    client = spanner.Client(project=settings.project_id, credentials=creds)
    db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

    # Step 1: Preconditions
    run_preconditions(db)

    # Step 2: Atomic Transaction
    commit_ts = execute_transaction(db)

    # Step 3: Verification
    post_execution_verification(db)


if __name__ == "__main__":
    main()

