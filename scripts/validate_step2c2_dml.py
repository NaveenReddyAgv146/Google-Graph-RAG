#!/usr/bin/env python3
"""
Step 2C.2 – Final Corrections to Uber DML Plan.
Performs read-only inspection of the live Spanner database to validate:
1. Existing Page 4 EvidenceLink row values (PageNumber, SlideNumber, Note, ClaimType).
2. PDF page anchors: PageNumber set, SlideNumber = NULL for all 6 Uber PDF evidence records.
3. Implemented Solution evidence: Verbatim chunk reference strictly isolating completed milestones from NEXT planned activities.
4. Corrected SolutionCards.ImplementedSolution text.
5. Exact deterministic IDs and uniqueness.
6. Atomic transaction update plan.
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
from common.database.enrichment_writer import generate_evidence_link_id

CARD_ID = "card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d"
DOC_ID = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
EXISTING_EVIDENCE_ID = "evl_fd27d9a5d2176a0785b1f407bb67213cb0b24fd8cd60503d"

# Completed milestones quote strictly terminates before "* NEXT: buy 3 cars, develop app"
IMPLEMENTED_MILESTONES_QUOTE = (
    "Progress to Date\n\n\n"
    " * Ubercab.com reserved + “ubercab” SMS code\n\n"
    " ¢  California LLC + trademark filed\n\n"
    " ¢ Iphone dev license applied for Nov28,08\n"
    " * Bank Account + Paypal account created\n\n"
    " ¢ 5 advisors & 15 clients now recruited\n\n"
    " ¢  Provisional patent filed"
)

TARGET_CHUNKS = {
    "BUSINESS_PROBLEM_P2": {
        "chunk_id": "chunk-165f0bd0d002a0894536ae7f4d776dd7c7c402a8d5f75fbb",
        "page": 2,
        "slide": None,  # PDF anchor: SlideNumber MUST be NULL
        "category": "BUSINESS_PROBLEM",
        "quote": (
            "Most use aging & inefficient technology\n\n"
            "   —  Radio dispatch, no 2-way communication\n\n"
            "   —  Most common car, Ford Crown Victoria = 14mpg\n\n"
            " *  Hailing is done by hand or phone\n\n"
            "   — No GPS coordination between client/driver\n\n"
            "   —  Significant fareseeking or “dead-time”"
        ),
        "note": (
            "Traditional taxi inefficiencies in 2008: aging technology, radio dispatch without "
            "two-way communication, hand/phone hailing, lack of GPS coordination, and significant fareseeking dead-time."
        ),
        "action": "INSERT",
    },
    "BUSINESS_PROBLEM_P3": {
        "chunk_id": "chunk-2a4aa82038e62e9e55ebd7540a9a55fcf5c17ac11cb4a53a",
        "page": 3,
        "slide": None,  # PDF anchor: SlideNumber MUST be NULL
        "category": "BUSINESS_PROBLEM",
        "quote": (
            "Taxi-monopolies reduce quality of service\n"
            "   — Medallions are expensive, and drivers underpaid\n\n\n\n\n\n"
            "   — Medallions cost ~$500k, drivers make 31k\n"
            "   — No incentive/accountability for drivers/clients"
        ),
        "note": (
            "Taxi medallion monopoly dynamics: expensive medallions (~$500k), underpaid drivers ($31k), "
            "reduced service quality, and lack of accountability."
        ),
        "action": "INSERT",
    },
    "BUSINESS_PROBLEM_P12": {
        "chunk_id": "chunk-92a6b957c622cc72388015d014e32c3ede07fb0d440d3ed0",
        "page": 12,
        "slide": None,  # PDF anchor: SlideNumber MUST be NULL
        "category": "BUSINESS_PROBLEM",
        "quote": (
            "In NYC, 35% of time is spend looking for fares\n\n"
            "   — In less dense cities, can be over half “dead-time”"
        ),
        "note": (
            "Vehicle resource utilization metrics: 35% fare-seeking time in NYC, "
            "exceeding 50% dead-time in less dense cities."
        ),
        "action": "INSERT",
    },
    "PROPOSED_SOLUTION_P4": {
        "chunk_id": "chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6",
        "page": 4,
        "slide": None,  # PDF anchor: SlideNumber MUST be NULL (correcting if existing was non-null)
        "category": "PROPOSED_SOLUTION",
        "quote": (
            "A fast & efficient on-demand car service\n\n"
            " *  Market: Professionals in American cities\n\n"
            " * Convenience of a cab in NYC + experience of a\n"
            "   professional chauffeur. But in SF and NYC\n\n"
            " *  Latest consumer web & device technology\n"
            "  — automate dispatch to reduce wait-time"
        ),
        "note": (
            "Proposed UberCab concept: fast & efficient on-demand car service for urban professionals "
            "automating dispatch via consumer web/mobile device tech to reduce wait times."
        ),
        "action": "UPDATE",
    },
    "IMPLEMENTED_SOLUTION_P25": {
        "chunk_id": "chunk-b2809c5eb5009cd4a1e5b2426de3ff6427aa1c6ef791f0f8",
        "page": 25,
        "slide": None,  # PDF anchor: SlideNumber MUST be NULL
        "category": "IMPLEMENTED_SOLUTION",
        "quote": IMPLEMENTED_MILESTONES_QUOTE,
        "note": (
            "Completed foundational milestones in 2008: California LLC & trademark filed, provisional patent filed, "
            "iPhone dev license applied for (Nov 28, 2008), domain/SMS code reserved, bank/PayPal account created, "
            "and 15 trial clients/5 advisors recruited. (Explicitly excludes 'NEXT' planned activities: app development and car fleet purchases)."
        ),
        "action": "INSERT",
    },
    "OUTCOME_P20": {
        "chunk_id": "chunk-6d660e6633fa4738d44735ffc6dbecec9e6fd97ecba7864e",
        "page": 20,
        "slide": None,  # PDF anchor: SlideNumber MUST be NULL
        "category": "OUTCOME",
        "quote": (
            "POtential Outcomes\n\n\n\n"
            " *  Best-Case Scenario\n\n"
            "   — Becomes market leader, $1B+ in yearly revenue\n\n"
            " *  Realistic Success Scenario\n\n"
            "   —  Gets 5% of the top 5 US Cities\n\n"
            "   —  Generates 20-30M+ per year profit\n\n"
            " * Worst-Case Scenario\n\n"
            "   —  Remains a 10 car, 100 client service in SF"
        ),
        "note": (
            "Projected business outcomes: Best-case scenario ($1B+ yearly revenue), realistic scenario "
            "(5% of top 5 US cities, $20-30M+ profit), and worst-case scenario (10-car service in SF)."
        ),
        "action": "INSERT",
    },
}

CORRECTED_IMPLEMENTED_SOLUTION = (
    "Foundational milestones completed in 2008: California LLC + trademark filed, "
    "provisional patent filed, iPhone dev license applied for (Nov 28, 2008), "
    "Ubercab.com and SMS code reserved, bank/PayPal account created, and 15 trial "
    "clients/5 advisors recruited. (Target architecture designed for 1-click dispatch; "
    "mobile app development and car fleet acquisition were future planned milestones under 'NEXT')."
)

def main():
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    creds = Credentials(token)
    client = spanner.Client(project=settings.project_id, credentials=creds)
    db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

    print("=" * 100)
    print("STEP 2C.2 FINAL CORRECTIONS & DML PLAN VALIDATION")
    print(f"Target DB: {settings.project_id}/{settings.spanner_instance_id}/{settings.spanner_database_id}")
    print("=" * 100)

    with db.snapshot(multi_use=True) as snapshot:
        # 1. Inspect existing Page 4 record in Spanner
        print("\n1. INSPECT EXISTING RECORD (evl_fd27d9a5d2176a0785b1f407bb67213cb0b24fd8cd60503d):")
        existing_rows = list(snapshot.execute_sql("""
            SELECT EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, Note, ClaimType
            FROM EvidenceLinks
            WHERE EvidenceId = @eid
        """, params={"eid": EXISTING_EVIDENCE_ID}, param_types={"eid": spanner.param_types.STRING}))

        assert len(existing_rows) == 1, "Existing evidence link not found!"
        ex = existing_rows[0]
        print(f"  EvidenceId:  {ex[0]}")
        print(f"  CardId:      {ex[1]}")
        print(f"  ChunkId:     {ex[2]}")
        print(f"  DocumentId:  {ex[3]}")
        print(f"  PageNumber:  {ex[4]}")
        print(f"  SlideNumber: {ex[5]}")
        print(f"  ClaimType:   {ex[7]}")
        print(f"  Note snippet: {ex[6][:60]!r}")

        # Check if SlideNumber is non-null
        needs_slide_correction = (ex[5] is not None)
        print(f"  Existing SlideNumber is {ex[5]!r}. Needs correction to NULL? {needs_slide_correction}")

        # 2. Check Implemented Solution Evidence strictly excludes 'NEXT'
        print("\n2. IMPLEMENTED SOLUTION EVIDENCE AUDIT:")
        p25_chunk = list(snapshot.execute_sql("""
            SELECT TextContent FROM EvidenceChunks WHERE ChunkId = @cid
        """, params={"cid": TARGET_CHUNKS["IMPLEMENTED_SOLUTION_P25"]["chunk_id"]},
        param_types={"cid": spanner.param_types.STRING}))[0][0]

        quote_p25 = TARGET_CHUNKS["IMPLEMENTED_SOLUTION_P25"]["quote"]
        assert quote_p25 in p25_chunk, "P25 quote must be verbatim in chunk!"
        print("  Verbatim grounding in chunk: TRUE")
        contains_next = "* NEXT:" in quote_p25 or "develop app" in quote_p25 or "buy 3 cars" in quote_p25
        print(f"  Quote contains 'NEXT: buy 3 cars, develop app'? {contains_next} (Must be FALSE)")
        assert not contains_next, "IMPLEMENTED_SOLUTION quote must strictly exclude planned activities!"
        print("  Completed milestones strictly isolated from NEXT activities: TRUE")

        # 3. Check All 6 Quotes Grounding & Anchors
        print("\n3. ALL 6 EVIDENCE RECORDS VERIFICATION:")
        generated_ids = {}
        for alias, item in TARGET_CHUNKS.items():
            chunk_row = list(snapshot.execute_sql("""
                SELECT DocumentId, PageNumber, TextContent FROM EvidenceChunks WHERE ChunkId = @cid
            """, params={"cid": item["chunk_id"]}, param_types={"cid": spanner.param_types.STRING}))[0]
            
            # Grounding
            assert item["quote"] in chunk_row[2], f"Quote for {alias} not verbatim!"
            assert chunk_row[0] == DOC_ID, f"DocId mismatch for {alias}!"
            assert item["page"] == chunk_row[1], f"Page mismatch for {alias}!"
            assert item["slide"] is None, f"SlideNumber for {alias} must be None!"

            if item["action"] == "UPDATE":
                ev_id = EXISTING_EVIDENCE_ID
            else:
                ev_id = generate_evidence_link_id(CARD_ID, item["chunk_id"], item["quote"], claim_type=item["category"])

            generated_ids[alias] = ev_id
            print(f"  [{item['category']:20}] Page: {item['page']:2} | Slide: {str(item['slide']):4} | Action: {item['action']:6} | ID: {ev_id}")

        assert len(set(generated_ids.values())) == 6, "All 6 IDs must be unique!"

        # 4. Card ImplementedSolution text audit
        print("\n4. SOLUTION CARD IMPLEMENTED_SOLUTION TEXT AUDIT:")
        card_row = list(snapshot.execute_sql("""
            SELECT ImplementedSolution, VerificationStatus FROM SolutionCards WHERE CardId = @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))[0]
        print(f"  Current DB Status: {card_row[1]} (Will remain PENDING)")
        print(f"  Current DB Text:   {card_row[0][:80]}...")
        print(f"  Proposed Text:     {CORRECTED_IMPLEMENTED_SOLUTION}")

        # 5. Invariant audit
        sc_count = list(snapshot.execute_sql("SELECT COUNT(*) FROM SolutionCards"))[0][0]
        ev_count = list(snapshot.execute_sql("SELECT COUNT(*) FROM EvidenceLinks"))[0][0]
        print(f"\n5. DATABASE INVARIANTS:")
        print(f"  Total SolutionCards: {sc_count} (Only Uber card modified)")
        print(f"  Total EvidenceLinks: {ev_count} (5 new inserts, 1 update in place, 5 unrelated untouched)")
        assert sc_count == 11
        assert ev_count == 6

    print("\n" + "=" * 100)
    print("CORRECTED DML VALIDATION SUCCEEDED 100%")
    print("=" * 100)

if __name__ == "__main__":
    main()
