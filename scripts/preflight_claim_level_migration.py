#!/usr/bin/env python3
"""
Step 2B.1 - Read-Only Preflight Inspection & Baseline Fingerprinting.
Target: Cloud Spanner (xenon-broker-513704-s9 / sales-discovery-trial / sales-knowledge-db)

STRICT READ-ONLY: Executes ZERO mutations, ZERO DDL, ZERO updates.
"""

import hashlib
import json
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"
os.environ["GRPC_DNS_RESOLVER"] = "native"

import google.auth
from google.cloud import spanner
from common.config import settings

def main():
    creds, _ = google.auth.default()
    client = spanner.Client(project=settings.project_id, credentials=creds)
    instance = client.instance(settings.spanner_instance_id)
    database = instance.database(settings.spanner_database_id)

    print("=" * 80)
    print("STEP 2B.1 READ-ONLY SPANNER PREFLIGHT AUDIT")
    print(f"Project:  {settings.project_id}")
    print(f"Instance: {settings.spanner_instance_id}")
    print(f"Database: {settings.spanner_database_id}")
    print("=" * 80)

    with database.snapshot(multi_use=True) as snapshot:
        # 1. Inspect Table Columns for EvidenceLinks
        print("\n--- 1. INFORMATION_SCHEMA COLUMNS: EvidenceLinks ---")
        ev_cols = list(snapshot.execute_sql("""
            SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE, ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'EvidenceLinks' AND TABLE_SCHEMA = ''
            ORDER BY ORDINAL_POSITION
        """))
        for col in ev_cols:
            print(f"  [{col[3]}] {col[0]:<20} {col[1]:<20} (Nullable: {col[2]})")

        # 2. Inspect Constraints on EvidenceLinks
        print("\n--- 2. CONSTRAINTS: EvidenceLinks ---")
        constraints = list(snapshot.execute_sql("""
            SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
            WHERE TABLE_NAME = 'EvidenceLinks' AND TABLE_SCHEMA = ''
        """))
        for c in constraints:
            print(f"  {c[0]} ({c[1]})")

        # 3. Inspect Table Columns for SolutionCards
        print("\n--- 3. INFORMATION_SCHEMA COLUMNS: SolutionCards ---")
        card_cols = list(snapshot.execute_sql("""
            SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE, ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'SolutionCards' AND TABLE_SCHEMA = ''
            ORDER BY ORDINAL_POSITION
        """))
        for col in card_cols:
            print(f"  [{col[3]}] {col[0]:<20} {col[1]:<20} (Nullable: {col[2]})")

        # 4. Count SolutionCards and EvidenceLinks
        card_count = list(snapshot.execute_sql("SELECT COUNT(1) FROM SolutionCards"))[0][0]
        ev_count = list(snapshot.execute_sql("SELECT COUNT(1) FROM EvidenceLinks"))[0][0]
        print(f"\n--- 4. RECORD COUNTS ---")
        print(f"  SolutionCards: {card_count}")
        print(f"  EvidenceLinks: {ev_count}")

        # 5. Compute Deterministic Fingerprints
        print(f"\n--- 5. CRYPTOGRAPHIC BASELINE FINGERPRINTS ---")
        card_rows = list(snapshot.execute_sql("""
            SELECT CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution,
                   ImplementedSolution, Outcome, VerificationStatus, OkfUri
            FROM SolutionCards
            ORDER BY CardId ASC
        """))
        card_hasher = hashlib.sha256()
        for r in card_rows:
            row_str = "|".join(str(val) if val is not None else "<NULL>" for val in r)
            card_hasher.update(row_str.encode("utf-8"))
            card_hasher.update(b"\n")
        card_digest = card_hasher.hexdigest()

        ev_rows = list(snapshot.execute_sql("""
            SELECT EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, Note
            FROM EvidenceLinks
            ORDER BY EvidenceId ASC
        """))
        ev_hasher = hashlib.sha256()
        for r in ev_rows:
            row_str = "|".join(str(val) if val is not None else "<NULL>" for val in r)
            ev_hasher.update(row_str.encode("utf-8"))
            ev_hasher.update(b"\n")
        ev_digest = ev_hasher.hexdigest()

        print(f"  SolutionCards (n={len(card_rows)}): SHA256={card_digest}")
        print(f"  EvidenceLinks (n={len(ev_rows)}): SHA256={ev_digest}")

        # 6. Detailed Inventory of Existing EvidenceLinks
        print(f"\n--- 6. EXISTING EVIDENCE LINKS IN SPANNER ({len(ev_rows)}) ---")
        for i, r in enumerate(ev_rows, 1):
            print(f"  [{i}] EvidenceId: {r[0]}")
            print(f"      CardId:     {r[1]}")
            print(f"      ChunkId:    {r[2]}")
            print(f"      DocumentId: {r[3]}")
            print(f"      Page/Slide: Page={r[4]}, Slide={r[5]}")
            print(f"      Note:       {r[6]!r}")

        # 7. Check if ClaimType column currently exists
        col_names = [c[0] for c in ev_cols]
        has_claim_type = "ClaimType" in col_names
        print(f"\n--- 7. MIGRATION STATUS ---")
        print(f"  ClaimType column present: {has_claim_type}")
        if not has_claim_type:
            print("  Status: PENDING MIGRATION (Schema currently has 7 physical columns)")

    print("\n" + "=" * 80)
    print("PREFLIGHT COMPLETED SAFELY (ZERO MUTATIONS APPLIED)")
    print("=" * 80)

if __name__ == "__main__":
    main()
