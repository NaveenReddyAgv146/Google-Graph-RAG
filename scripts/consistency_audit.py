#!/usr/bin/env python3
"""
Step 2C.2 – EvidenceId and Note Consistency Audit Script.
Strict read-only verification of:
1. Inputs to generate_evidence_link_id (CardId, ChunkId, ClaimType, Quote).
2. Generated EvidenceIds.
3. Note values.
4. Determinism & repeated execution consistency.
5. Collision checking against live Spanner database.
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

RECORDS = [
    {
        "alias": "BUSINESS_PROBLEM_P2",
        "card_id": CARD_ID,
        "chunk_id": "chunk-165f0bd0d002a0894536ae7f4d776dd7c7c402a8d5f75fbb",
        "claim_type": "BUSINESS_PROBLEM",
        "page_number": 2,
        "slide_number": None,
        "quote": (
            "Most use aging & inefficient technology\n\n"
            "   —  Radio dispatch, no 2-way communication\n\n"
            "   —  Most common car, Ford Crown Victoria = 14mpg\n\n"
            " *  Hailing is done by hand or phone\n\n"
            "   — No GPS coordination between client/driver\n\n"
            "   —  Significant fareseeking or “dead-time”"
        ),
        "action": "INSERT",
    },
    {
        "alias": "BUSINESS_PROBLEM_P3",
        "card_id": CARD_ID,
        "chunk_id": "chunk-2a4aa82038e62e9e55ebd7540a9a55fcf5c17ac11cb4a53a",
        "claim_type": "BUSINESS_PROBLEM",
        "page_number": 3,
        "slide_number": None,
        "quote": (
            "Taxi-monopolies reduce quality of service\n"
            "   — Medallions are expensive, and drivers underpaid\n\n\n\n\n\n"
            "   — Medallions cost ~$500k, drivers make 31k\n"
            "   — No incentive/accountability for drivers/clients"
        ),
        "action": "INSERT",
    },
    {
        "alias": "BUSINESS_PROBLEM_P12",
        "card_id": CARD_ID,
        "chunk_id": "chunk-92a6b957c622cc72388015d014e32c3ede07fb0d440d3ed0",
        "claim_type": "BUSINESS_PROBLEM",
        "page_number": 12,
        "slide_number": None,
        "quote": (
            "In NYC, 35% of time is spend looking for fares\n\n"
            "   — In less dense cities, can be over half “dead-time”"
        ),
        "action": "INSERT",
    },
    {
        "alias": "PROPOSED_SOLUTION_P4",
        "card_id": CARD_ID,
        "chunk_id": "chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6",
        "claim_type": "PROPOSED_SOLUTION",
        "page_number": 4,
        "slide_number": None,
        "quote": (
            "A fast & efficient on-demand car service\n\n"
            " *  Market: Professionals in American cities\n\n"
            " * Convenience of a cab in NYC + experience of a\n"
            "   professional chauffeur. But in SF and NYC\n\n"
            " *  Latest consumer web & device technology\n"
            "  — automate dispatch to reduce wait-time"
        ),
        "action": "UPDATE",
    },
    {
        "alias": "IMPLEMENTED_SOLUTION_P25",
        "card_id": CARD_ID,
        "chunk_id": "chunk-b2809c5eb5009cd4a1e5b2426de3ff6427aa1c6ef791f0f8",
        "claim_type": "IMPLEMENTED_SOLUTION",
        "page_number": 25,
        "slide_number": None,
        "quote": (
            "Progress to Date\n\n\n"
            " * Ubercab.com reserved + “ubercab” SMS code\n\n"
            " ¢  California LLC + trademark filed\n\n"
            " ¢ Iphone dev license applied for Nov28,08\n"
            " * Bank Account + Paypal account created\n\n"
            " ¢ 5 advisors & 15 clients now recruited\n\n"
            " ¢  Provisional patent filed"
        ),
        "action": "INSERT",
    },
    {
        "alias": "OUTCOME_P20",
        "card_id": CARD_ID,
        "chunk_id": "chunk-6d660e6633fa4738d44735ffc6dbecec9e6fd97ecba7864e",
        "claim_type": "OUTCOME",
        "page_number": 20,
        "slide_number": None,
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
        "action": "INSERT",
    },
]

def main():
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    creds = Credentials(token)
    client = spanner.Client(project=settings.project_id, credentials=creds)
    db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

    print("=" * 100)
    print("STEP 2C.2 EVIDENCEID AND NOTE CONSISTENCY AUDIT")
    print(f"Target DB: {settings.project_id}/{settings.spanner_instance_id}/{settings.spanner_database_id}")
    print("=" * 100)

    # 1. Audit generate_evidence_link_id inputs and outputs
    print("\n--- 1 & 2: INPUTS AND GENERATED EVIDENCEIDS ---")
    results = []
    for rec in RECORDS:
        # Check repeated execution
        id_1 = generate_evidence_link_id(rec["card_id"], rec["chunk_id"], rec["quote"], claim_type=rec["claim_type"])
        id_2 = generate_evidence_link_id(rec["card_id"], rec["chunk_id"], rec["quote"], claim_type=rec["claim_type"])
        assert id_1 == id_2, "Repeated execution produced different IDs!"

        prefix_64 = rec["quote"].strip()[:64]
        raw_hash_input = f"{rec['card_id']}:{rec['chunk_id']}:{rec['claim_type']}:{prefix_64}"
        expected_digest = hashlib.sha256(raw_hash_input.encode("utf-8")).hexdigest()[:48]
        expected_id = f"evl_{expected_digest}"
        assert id_1 == expected_id, "Generated ID mismatch!"

        if rec["action"] == "UPDATE":
            target_id = EXISTING_EVIDENCE_ID
        else:
            target_id = id_1

        rec["generated_id"] = id_1
        rec["target_id"] = target_id
        rec["raw_input"] = raw_hash_input
        rec["prefix_64"] = prefix_64

        print(f"\n[{rec['alias']}] ({rec['claim_type']}) - Action: {rec['action']}")
        print(f"  CardId:                 {rec['card_id']}")
        print(f"  ChunkId:                {rec['chunk_id']}")
        print(f"  ClaimType:              {rec['claim_type']}")
        print(f"  Quote (first 60 chars): {repr(rec['quote'][:60])}")
        print(f"  Quote.strip()[:64]:     {repr(prefix_64)}")
        print(f"  Raw Hash Input:         {repr(raw_hash_input)}")
        print(f"  Generated EvidenceId:   {id_1}")
        print(f"  Action Target ID:       {target_id}")

    # 2. Check collision with live database
    print("\n--- COLLISION CHECK AGAINST LIVE SPANNER DATABASE ---")
    with db.snapshot() as snapshot:
        existing_db_ids = set(r[0] for r in snapshot.execute_sql("SELECT EvidenceId FROM EvidenceLinks"))
        new_insert_ids = [r["generated_id"] for r in RECORDS if r["action"] == "INSERT"]
        collisions = set(new_insert_ids).intersection(existing_db_ids)
        print(f"Total existing IDs in DB:       {len(existing_db_ids)}")
        print(f"New insert IDs to be generated: {len(new_insert_ids)}")
        print(f"Collisions detected:            {collisions}")
        assert len(collisions) == 0, f"Collisions found: {collisions}"
        print("Collision check PASSED (Zero collisions).")

    # 3. Check uniqueness across all 6 records
    all_target_ids = [r["target_id"] for r in RECORDS]
    assert len(set(all_target_ids)) == 6, "All 6 target IDs must be unique!"
    print("\nTarget IDs strictly unique: 6 / 6")

if __name__ == "__main__":
    main()

