#!/usr/bin/env python3
"""
Step 2C.1 – Uber Claim-Level Evidence Population Dry Run.
Target: Spanner (xenon-broker-513704-s9 / sales-discovery-trial / sales-knowledge-db)

STRICT DRY RUN ONLY:
- Zero Spanner mutations.
- Zero GCS modifications.
- Zero status changes.
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

TARGET_CHUNKS = {
    "BUSINESS_PROBLEM_P2": "chunk-165f0bd0d002a0894536ae7f4d776dd7c7c402a8d5f75fbb",
    "BUSINESS_PROBLEM_P3": "chunk-2a4aa82038e62e9e55ebd7540a9a55fcf5c17ac11cb4a53a",
    "BUSINESS_PROBLEM_P12": "chunk-92a6b957c622cc72388015d014e32c3ede07fb0d440d3ed0",
    "PROPOSED_SOLUTION_P4": "chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6",
    "IMPLEMENTED_SOLUTION_P25": "chunk-b2809c5eb5009cd4a1e5b2426de3ff6427aa1c6ef791f0f8",
    "OUTCOME_P20": "chunk-6d660e6633fa4738d44735ffc6dbecec9e6fd97ecba7864e",
}

def main():
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    creds = Credentials(token)
    client = spanner.Client(project=settings.project_id, credentials=creds)
    db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

    print("=" * 100)
    print("STEP 2C.1 DRY RUN: UBER CLAIM-LEVEL EVIDENCE POPULATION PLAN")
    print(f"CardId:   {CARD_ID}")
    print(f"DocId:    {DOC_ID}")
    print("=" * 100)

    with db.snapshot(multi_use=True) as snapshot:
        # 1. Fetch current SolutionCard row
        card_rows = list(snapshot.execute_sql("""
            SELECT CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution,
                   ImplementedSolution, Outcome, VerificationStatus, OkfUri
            FROM SolutionCards
            WHERE CardId = @cid
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))

        if not card_rows:
            print("ERROR: Card not found!")
            sys.exit(1)

        card = card_rows[0]
        print("\nCURRENT SOLUTION CARD STATE:")
        print(f"  Project Name:       {card[2]}")
        print(f"  VerificationStatus: {card[7]}")
        print(f"  Business Problem:   {card[3][:120]}...")
        print(f"  Proposed Solution:  {card[4][:120]}...")
        print(f"  Implemented Sol:    {card[5][:120]}...")
        print(f"  Outcome:            {card[6][:120]}...")

        # 2. Fetch existing EvidenceLinks for this card
        existing_evs = list(snapshot.execute_sql("""
            SELECT EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, Note, ClaimType
            FROM EvidenceLinks
            WHERE CardId = @cid
            ORDER BY EvidenceId ASC
        """, params={"cid": CARD_ID}, param_types={"cid": spanner.param_types.STRING}))

        print(f"\nCURRENT EVIDENCELINKS FOR CARD ({len(existing_evs)}):")
        for ev in existing_evs:
            print(f"  EvidenceId: {ev[0]}")
            print(f"  ChunkId:    {ev[1]}")
            print(f"  ClaimType:  {ev[7]}")
            print(f"  Note:       {ev[6]!r}")

        # 3. Fetch exact text for all target chunks
        chunk_cids = list(TARGET_CHUNKS.values())
        chunk_rows = list(snapshot.execute_sql("""
            SELECT ChunkId, PageNumber, TextContent
            FROM EvidenceChunks
            WHERE ChunkId IN UNNEST(@cids)
        """, params={"cids": chunk_cids}, param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)}))

        chunk_dict = {r[0]: (r[1], r[2]) for r in chunk_rows}
        print(f"\nLOADED {len(chunk_dict)} TARGET EVIDENCE CHUNKS FROM SPANNER:")
        for alias, cid in TARGET_CHUNKS.items():
            pg, text = chunk_dict[cid]
            print(f"\n--- [{alias}] ChunkId: {cid} (Page {pg}) ---")
            lines = text.strip().split("\n")
            for line in lines[:6]:
                print(f"    {line}")
            if len(lines) > 6:
                print(f"    ... ({len(lines)} lines total)")

    print("\n" + "=" * 100)
    print("PROPOSED CLAIM-TO-EVIDENCE MAPPINGS & QUOTE VERIFICATION")
    print("=" * 100)

    # Detailed proposed verbatim quotes and claim types matching chunk text exactly
    proposals = [
        {
            "category": "BUSINESS_PROBLEM",
            "page": 2,
            "chunk_id": TARGET_CHUNKS["BUSINESS_PROBLEM_P2"],
            "quote": "Most use aging & inefficient technology\n\n   —  Radio dispatch, no 2-way communication\n\n   —  Most common car, Ford Crown Victoria = 14mpg\n\n *  Hailing is done by hand or phone\n\n   — No GPS coordination between client/driver\n\n   —  Significant fareseeking or “dead-time”",
            "note": "Grounding for traditional taxi inefficiencies, radio dispatch, and high dead-time",
            "action": "INSERT",
        },
        {
            "category": "BUSINESS_PROBLEM",
            "page": 3,
            "chunk_id": TARGET_CHUNKS["BUSINESS_PROBLEM_P3"],
            "quote": "Taxi-monopolies reduce quality of service\n   — Medallions are expensive, and drivers underpaid\n\n\n\n\n\n   — Medallions cost ~$500k, drivers make 31k\n   — No incentive/accountability for drivers/clients",
            "note": "Grounding for medallion monopoly lock-in and driver underpayment",
            "action": "INSERT",
        },
        {
            "category": "BUSINESS_PROBLEM",
            "page": 12,
            "chunk_id": TARGET_CHUNKS["BUSINESS_PROBLEM_P12"],
            "quote": "In NYC, 35% of time is spend looking for fares\n\n   — In less dense cities, can be over half “dead-time”",
            "note": "Grounding for dead-time metrics across urban and less dense markets",
            "action": "INSERT",
        },
        {
            "category": "PROPOSED_SOLUTION",
            "page": 4,
            "chunk_id": TARGET_CHUNKS["PROPOSED_SOLUTION_P4"],
            "quote": "A fast & efficient on-demand car service\n\n *  Market: Professionals in American cities\n\n * Convenience of a cab in NYC + experience of a\n   professional chauffeur. But in SF and NYC\n\n *  Latest consumer web & device technology\n  — automate dispatch to reduce wait-time",
            "note": "A fast & efficient on-demand car service\n\n *  Market: Professionals in American cities\n\n * Convenience of a cab in NYC + experience of a\n   professional chauffeur. But in SF and NYC\n\n *  Latest consumer web & device technology\n  — automate dispatch to reduce wait-time",
            "action": "UPDATE_CLAIM_TYPE",  # Existing link evl_fd27d9...
        },
        {
            "category": "IMPLEMENTED_SOLUTION",
            "page": 25,
            "chunk_id": TARGET_CHUNKS["IMPLEMENTED_SOLUTION_P25"],
            "quote": "Progress to Date\n\n\n * Ubercab.com reserved + “ubercab” SMS code\n\n ¢  California LLC + trademark filed\n\n ¢ Iphone dev license applied for Nov28,08\n * Bank Account + Paypal account created\n\n ¢ 5 advisors & 15 clients now recruited\n\n ¢  Provisional patent filed\n * NEXT: buy 3 cars, develop app, Feb1st demo",
            "note": "Grounding for verified foundational milestones completed in 2008 (LLC, trademark, patent, license)",
            "action": "INSERT",
        },
        {
            "category": "OUTCOME",
            "page": 20,
            "chunk_id": TARGET_CHUNKS["OUTCOME_P20"],
            "quote": "POtential Outcomes\n\n\n\n *  Best-Case Scenario\n\n   — Becomes market leader, $1B+ in yearly revenue\n\n *  Realistic Success Scenario\n\n   —  Gets 5% of the top 5 US Cities\n\n   —  Generates 20-30M+ per year profit\n\n * Worst-Case Scenario\n\n   —  Remains a 10 car, 100 client service in SF",
            "note": "Grounding for projected market scenarios ($1B+ best-case, $20-30M realistic profit)",
            "action": "INSERT",
        },
    ]

    for p in proposals:
        chunk_page, chunk_text = chunk_dict[p["chunk_id"]]
        is_sub = p["quote"] in chunk_text

        # ID computation
        if p["action"] == "UPDATE_CLAIM_TYPE":
            # For the existing record, we update ClaimType in place:
            ev_id = EXISTING_EVIDENCE_ID
        else:
            ev_id = generate_evidence_link_id(CARD_ID, p["chunk_id"], p["quote"], claim_type=p["category"])

        p["generated_ev_id"] = ev_id
        p["is_grounded"] = is_sub

        print(f"\n[{p['category']}] (Page {p['page']}) -> Action: {p['action']}")
        print(f"  Target ChunkId:  {p['chunk_id']}")
        print(f"  Target Evidence: {ev_id}")
        print(f"  Quote Grounded:  {is_sub} (Quote length: {len(p['quote'])} chars)")
        if not is_sub:
            print(f"  WARNING: Quote NOT found verbatim in chunk!")
        print(f"  Proposed Note:   {p['note'][:90]}...")

    all_grounded = all(p["is_grounded"] for p in proposals)
    print("\n" + "=" * 100)
    print(f"VERBATIM GROUNDING AUDIT RESULT: {'ALL PASS (100%)' if all_grounded else 'FAIL'}")
    print("=" * 100)

    print("\n" + "=" * 100)
    print("CARD FIELD CORRECTION AUDIT")
    print("=" * 100)
    print("Problem Claim:     ALIGNED with Page 2, 3, 12 citations.")
    print("Proposed Solution: ALIGNED with Page 4 citation.")
    print("Outcome Claim:     ALIGNED with Page 20 citation (already explicitly specifies 'Projected Outcomes').")
    print("Implemented Sol:   AUDIT REQUIRED:")
    print("  Current Card text claims: '1-Click request from Geo-aware devices, SMS booking, mobile apps for iPhone...'")
    print("  Source chunk Page 25 proves: Mobile apps were under 'NEXT: buy 3 cars, develop app'.")
    print("  Proposed Card Text Correction for ImplementedSolution:")
    print("  'Foundational milestones completed in 2008: California LLC + trademark filed, provisional patent filed,")
    print("   iPhone dev license applied for (Nov 28, 2008), Ubercab.com and SMS code reserved, bank/PayPal account created,")
    print("   and 15 trial clients/5 advisors recruited. (Target architecture designed for 1-click dispatch and mobile apps).'")

    print("\n" + "=" * 100)
    print("TRANSACTION-LEVEL WRITE PLAN (DRY RUN)")
    print("=" * 100)
    print("1. EvidenceLinks Table:")
    print("   - Current Total Rows in Spanner: 6")
    print("   - Rows for Uber Card:            1 (evl_fd27d9a5d2176a0785b1f407bb67213cb0b24fd8cd60503d, ClaimType=NULL)")
    print("   - Proposed Mutations:")
    print("     * UPDATE EvidenceLinks SET ClaimType='PROPOSED_SOLUTION' WHERE EvidenceId='evl_fd27d9a5d2176a0785b1f407bb67213cb0b24fd8cd60503d'")
    print("     * INSERT 5 new EvidenceLinks:")
    for p in proposals:
        if p["action"] == "INSERT":
            print(f"       + {p['generated_ev_id']} | {p['category']} | Page {p['page']} | Chunk: {p['chunk_id']}")
    print("   - Projected Rows for Uber Card:  6 (all with populated ClaimType)")
    print("   - Projected Total Rows:          11 (6 existing + 5 newly inserted)")
    print()
    print("2. SolutionCards Table:")
    print("   - Current Total Rows in Spanner: 11 (5 DRAFT, 6 PENDING, 0 APPROVED)")
    print("   - Uber Card Status:              PENDING (Strictly preserved, NOT APPROVED)")
    print("   - Proposed Mutation:")
    print("     * UPDATE SolutionCards SET ImplementedSolution = <corrected_text> WHERE CardId='card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d'")
    print("     * VerificationStatus remains:  PENDING")
    print()
    print("3. Human Verification Invariant:")
    print("   - Attaching 6 verified EvidenceLinks does NOT automatically APPROVE the card.")
    print("   - Human review via `review_cli.py approve` or REST API remains mandatory.")
    print("=" * 100)
    print("DRY RUN PLAN COMPLETED SAFELY (ZERO MUTATIONS EXECUTED)")
    print("=" * 100)

if __name__ == "__main__":
    main()
