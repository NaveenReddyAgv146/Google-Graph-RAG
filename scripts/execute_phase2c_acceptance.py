#!/usr/bin/env python3
"""
Phase 2C Migration Acceptance Gate Script.

Target:
  Project:  xenon-broker-513704-s9
  Instance: sales-discovery-trial
  Database: sales-knowledge-db
  BatchId:  BATCH_BACKFILL_20261111_01

Requirements:
1. Revalidate batch is in 'PENDING_VALIDATION' state.
2. Revalidate 325 edge records and 325 matching manifest entries.
3. Perform acceptance transition inside one serializable read-write transaction on MigrationBatches(BatchId).
4. Set State = 'ACCEPTED' and AcceptedAt = spanner.COMMIT_TIMESTAMP.
5. Invariant enforcement: test/confirm rollback guard rejects rollback once ACCEPTED.
6. Zero modification to graph edges, source records, or other tables.
7. Post-commit verification: State = 'ACCEPTED', non-NULL AcceptedAt, 325 edges, 426 baseline records.
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

from common.database.migration import (
    accept_migration_batch,
    rollback_migration_batch,
    RollbackRejectedError,
)

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

EXPECTED_EDGE_COUNTS = {
    "DocumentContainsChunk": 195,
    "VisualAssetBelongsToDoc": 125,
    "SolutionImplementsDoc": 5,
}


def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    # ========================================================================
    # Step 1: Pre-Acceptance Revalidation
    # ========================================================================
    print("\nStep 1: Pre-Acceptance Revalidation...")
    with database.snapshot(multi_use=True) as snapshot:
        # 1a. Verify Batch is in PENDING_VALIDATION
        batch_rows = list(snapshot.execute_sql(
            "SELECT BatchId, State, TotalInsertedEdges, CreatedAt, AcceptedAt FROM MigrationBatches WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        if not batch_rows:
            print(f"FATAL: Batch '{BATCH_ID}' not found in MigrationBatches!")
            sys.exit(1)
        
        b_id, state, total_edges, created_at, accepted_at = batch_rows[0]
        print(f"  Current Batch State: {state}")
        print(f"  Total Inserted Edges: {total_edges}")
        print(f"  Created At: {created_at}")
        print(f"  Accepted At: {accepted_at}")

        if state != "PENDING_VALIDATION":
            print(f"FATAL: Expected state 'PENDING_VALIDATION', got '{state}'! Aborting.")
            sys.exit(1)
        if accepted_at is not None:
            print(f"FATAL: AcceptedAt is already set: {accepted_at}! Aborting.")
            sys.exit(1)
        if total_edges != 325:
            print(f"FATAL: TotalInsertedEdges is {total_edges}, expected 325! Aborting.")
            sys.exit(1)

        # 1b. Verify 325 edge records across 3 tables
        for tbl, exp in EXPECTED_EDGE_COUNTS.items():
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            if cnt != exp:
                print(f"FATAL: Edge count mismatch in {tbl}: {cnt} != {exp}!")
                sys.exit(1)
            print(f"  Verified {tbl}: {cnt} rows")

        # 1c. Verify 325 manifest entries
        mm_cnt = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM MigrationManifest WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))[0][0]
        if mm_cnt != 325:
            print(f"FATAL: MigrationManifest count is {mm_cnt}, expected 325! Aborting.")
            sys.exit(1)
        print(f"  Verified MigrationManifest: {mm_cnt} rows for BatchId '{BATCH_ID}'")

    print("Pre-acceptance revalidation PASSED.")

    # ========================================================================
    # Step 2: Execute Atomic Acceptance Transition
    # ========================================================================
    print(f"\nStep 2: Executing Stage 4 Acceptance Gate for Batch '{BATCH_ID}'...")
    acceptance_result = accept_migration_batch(database, batch_id=BATCH_ID)
    print("Stage 4 Acceptance Transaction COMMITTED successfully.")
    print(f"Result: {json.dumps(acceptance_result, indent=2, default=str)}")

    # ========================================================================
    # Step 3: Post-Commit Acceptance Verification
    # ========================================================================
    print("\nStep 3: Post-Commit Acceptance Verification...")
    with database.snapshot(multi_use=True) as snapshot:
        # 3a. Verify MigrationBatches row
        post_batch_rows = list(snapshot.execute_sql(
            "SELECT BatchId, State, TotalInsertedEdges, CreatedAt, AcceptedAt FROM MigrationBatches WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        if not post_batch_rows:
            print(f"FATAL: Batch '{BATCH_ID}' missing post-commit!")
            sys.exit(1)

        post_b_id, post_state, post_total, post_created, post_accepted = post_batch_rows[0]
        print(f"Verified MigrationBatches:")
        print(f"  - BatchId:            {post_b_id}")
        print(f"  - State:              {post_state} (Expected: ACCEPTED)")
        print(f"  - TotalInsertedEdges: {post_total} (Expected: 325)")
        print(f"  - CreatedAt:          {post_created}")
        print(f"  - AcceptedAt:         {post_accepted} (Non-NULL timestamp confirmed)")

        if post_state != "ACCEPTED":
            print(f"FATAL: Post-commit state is '{post_state}', expected 'ACCEPTED'!")
            sys.exit(1)
        if post_accepted is None:
            print(f"FATAL: Post-commit AcceptedAt is NULL!")
            sys.exit(1)

        # 3b. Verify all 325 edges remain intact
        print("\nVerifying edge table row counts post-acceptance:")
        for tbl, exp in EXPECTED_EDGE_COUNTS.items():
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            print(f"  - {tbl}: {cnt} rows (Expected: {exp})")
            if cnt != exp:
                print(f"FATAL: Edge count changed in {tbl}! Got {cnt}, expected {exp}")
                sys.exit(1)

        # 3c. Verify all 325 manifest records remain intact
        mm_post = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM MigrationManifest WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))[0][0]
        print(f"\nVerifying manifest records: {mm_post} (Expected: 325)")
        if mm_post != 325:
            print(f"FATAL: Manifest count changed! Got {mm_post}, expected 325")
            sys.exit(1)

        # 3d. Verify original 426 records remain intact
        print("\nVerifying original baseline tables:")
        total_baseline = 0
        for tbl, exp in BASE_TABLE_COUNTS.items():
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            print(f"  - {tbl}: {cnt} rows (Expected: {exp})")
            if cnt != exp:
                print(f"FATAL: Original table {tbl} was modified! Got {cnt}, expected {exp}")
                sys.exit(1)
            total_baseline += cnt
        print(f"Total baseline rows verified: {total_baseline} (100% intact).")

    # ========================================================================
    # Step 4: Verify Post-Acceptance Rollback Guard (Invariant 5)
    # ========================================================================
    print("\nStep 4: Testing Post-Acceptance Rollback Guard (Invariant 5)...")
    try:
        rollback_migration_batch(database, batch_id=BATCH_ID)
        print("FATAL: Rollback succeeded on an ACCEPTED batch! Invariant 5 violated!")
        sys.exit(1)
    except RollbackRejectedError as rre:
        print(f"Invariant 5 ENFORCED: Rollback was strictly rejected as expected.")
        print(f"  Caught expected error: {rre}")

    # Re-verify row counts after rejected rollback
    with database.snapshot() as snapshot:
        post_guard_state = list(snapshot.execute_sql(
            "SELECT State FROM MigrationBatches WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))[0][0]
        if post_guard_state != "ACCEPTED":
            print(f"FATAL: Batch state changed during rollback guard test! State: {post_guard_state}")
            sys.exit(1)
        print(f"Confirmed batch remains in terminal state '{post_guard_state}'.")

    print("\nPhase 2C Migration Acceptance Gate completed successfully with zero defects.")


if __name__ == "__main__":
    main()

