#!/usr/bin/env python3
"""
Phase 2C.5 Backfill Execution and Post-Commit Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- Atomically insert 325 edges across DocumentContainsChunk, VisualAssetBelongsToDoc, SolutionImplementsDoc
- Register BatchId in MigrationBatches in 'PENDING_VALIDATION' state (NOT ACCEPTED)
- Record 325 audit entries in MigrationManifest
"""

import os
import sys
import json
from google.cloud import spanner

# Add common/src to sys.path
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
if _common_src not in sys.path:
    sys.path.insert(0, _common_src)

from common.database.migration import execute_phase2_backfill

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"
BATCH_ID = "BATCH_BACKFILL_20261111_01"

BASE_TABLE_COUNTS = {
    "Documents": 5,
    "DocumentAccess": 10,
    "DriveSyncDocuments": 5,
    "EvidenceChunks": 195,
    "VisualAssets": 125,
    "SolutionCards": 5,
    "Embeddings": 81,
    "EvidenceLinks": 0,
}

EXPECTED_POST_BACKFILL = {
    "DocumentContainsChunk": 195,
    "VisualAssetBelongsToDoc": 125,
    "SolutionImplementsDoc": 5,
    "RepoContainsFile": 0,
    "SourceFileContainsChunk": 0,
    "ChunkReferencesEntity": 0,
    "EntityReferencesEntity": 0,
    "ChunkMentionsTech": 0,
    "SolutionSupportsTech": 0,
    "SourceFileDefinesEntity": 0,
    "SourceFileCallsFile": 0,
    "Repositories": 0,
    "SourceFiles": 0,
    "Entities": 0,
    "Technologies": 0,
    "MigrationBatches": 1,
    "MigrationManifest": 325,
}

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    # 1. Preflight assertion
    print("\nStep 1: Pre-execution assertion...")
    with database.snapshot(multi_use=True) as snapshot:
        # Check MigrationBatches does not have BATCH_ID
        mb_check = list(snapshot.execute_sql(
            "SELECT State FROM MigrationBatches WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        if mb_check:
            print(f"ERROR: Batch '{BATCH_ID}' already exists in state '{mb_check[0][0]}'!")
            sys.exit(1)
        print(f"Verified BatchId '{BATCH_ID}' is clean and ready.")

        # Check edge tables are currently empty
        for tbl in ["DocumentContainsChunk", "VisualAssetBelongsToDoc", "SolutionImplementsDoc"]:
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            if cnt != 0:
                print(f"ERROR: Table {tbl} is not empty! Count: {cnt}")
                sys.exit(1)
        print("Verified all 3 target edge tables are empty.")

    # 2. Execute Stage 2 Migration Backfill Transaction
    print(f"\nStep 2: Executing atomic backfill transaction (BatchId: {BATCH_ID})...")
    txn_result = execute_phase2_backfill(database, batch_id=BATCH_ID)
    print("Transaction COMMITTED successfully.")
    print(f"Result summary: {json.dumps(txn_result, indent=2)}")

    # 3. Post-Commit Verification
    print("\nStep 3: Post-commit verification...")
    with database.snapshot(multi_use=True) as snapshot:
        # 3a. Verify MigrationBatches row
        batch_row = list(snapshot.execute_sql(
            "SELECT BatchId, State, TotalInsertedEdges, CreatedAt, AcceptedAt FROM MigrationBatches WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        if not batch_row:
            print(f"CRITICAL ERROR: Batch '{BATCH_ID}' not found in MigrationBatches post-commit!")
            sys.exit(1)
        
        b_id, state, total_edges, created_at, accepted_at = batch_row[0]
        print(f"Verified MigrationBatches:")
        print(f"  - BatchId:            {b_id}")
        print(f"  - State:              {state} (Confirmed PENDING_VALIDATION, NOT ACCEPTED)")
        print(f"  - TotalInsertedEdges: {total_edges} (expected: 325)")
        print(f"  - CreatedAt:          {created_at}")
        print(f"  - AcceptedAt:         {accepted_at} (Confirmed None)")

        if state != "PENDING_VALIDATION":
            print(f"CRITICAL ERROR: Batch state is '{state}', expected 'PENDING_VALIDATION'!")
            sys.exit(1)
        if total_edges != 325:
            print(f"CRITICAL ERROR: TotalInsertedEdges is {total_edges}, expected 325!")
            sys.exit(1)
        if accepted_at is not None:
            print(f"CRITICAL ERROR: AcceptedAt is not None! Value: {accepted_at}")
            sys.exit(1)

        # 3b. Verify MigrationManifest counts
        mm_total = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM MigrationManifest WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))[0][0]
        print(f"\nVerified MigrationManifest: {mm_total} records for BatchId '{BATCH_ID}' (expected: 325).")
        if mm_total != 325:
            print(f"CRITICAL ERROR: MigrationManifest has {mm_total} rows, expected 325!")
            sys.exit(1)

        manifest_by_table = list(snapshot.execute_sql(
            "SELECT TableName, COUNT(1) FROM MigrationManifest WHERE BatchId = @b GROUP BY TableName ORDER BY TableName",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        print("Manifest breakdown by table:")
        for tname, tcount in manifest_by_table:
            print(f"  - {tname}: {tcount} manifest records")

        # 3c. Verify all edge and node table counts
        print("\nVerifying edge table and migration table row counts:")
        for tbl, exp_cnt in EXPECTED_POST_BACKFILL.items():
            actual = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            print(f"  - {tbl}: {actual} rows (expected: {exp_cnt})")
            if actual != exp_cnt:
                print(f"CRITICAL ERROR: Table {tbl} count mismatch! Expected {exp_cnt}, got {actual}")
                sys.exit(1)

        # 3d. Verify original 8 populated tables remain unchanged
        print("\nVerifying original 8 populated tables:")
        total_original = 0
        for tbl, exp_cnt in BASE_TABLE_COUNTS.items():
            actual = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            print(f"  - {tbl}: {actual} rows (expected: {exp_cnt})")
            if actual != exp_cnt:
                print(f"CRITICAL ERROR: Original table {tbl} was mutated! Expected {exp_cnt}, got {actual}")
                sys.exit(1)
            total_original += actual
        print(f"Original populated records verified: {total_original} rows (100% intact).")

    print("\nPhase 2C.5 Backfill successfully executed, committed, and verified.")

if __name__ == "__main__":
    main()

