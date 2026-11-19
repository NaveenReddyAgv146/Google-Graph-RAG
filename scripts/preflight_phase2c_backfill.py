#!/usr/bin/env python3
"""
Phase 2C.5 Backfill Preflight Script.
Read-Only verification of candidates, referential integrity, duplicates, and batch strategy.
"""

import sys
import json
from google.cloud import spanner

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    preflight_report = {
        "candidate_counts": {},
        "missing_references": {},
        "duplicates": {},
        "existing_edge_counts": {},
        "migration_table_counts": {},
        "verdict": "PASS",
    }

    with database.snapshot(multi_use=True) as snapshot:
        # ====================================================================
        # 1. Candidate Extraction & Counts
        # ====================================================================
        print("\n--- 1. Candidate Edge Extraction ---")
        
        # 1a. DocumentContainsChunk (from EvidenceChunks)
        dcc_sql = """
        SELECT DocumentId, ChunkId 
        FROM EvidenceChunks 
        WHERE DocumentId IS NOT NULL AND ChunkId IS NOT NULL
        """
        dcc_rows = [tuple(r) for r in snapshot.execute_sql(dcc_sql)]
        dcc_distinct = set(dcc_rows)
        print(f"  DocumentContainsChunk candidates: {len(dcc_rows)} total, {len(dcc_distinct)} distinct.")
        preflight_report["candidate_counts"]["DocumentContainsChunk"] = {
            "total": len(dcc_rows),
            "distinct": len(dcc_distinct),
            "expected": 195,
        }
        if len(dcc_rows) != 195 or len(dcc_distinct) != 195:
            preflight_report["verdict"] = "FAIL"

        # 1b. VisualAssetBelongsToDoc (from VisualAssets)
        vabd_sql = """
        SELECT VisualId, DocumentId 
        FROM VisualAssets 
        WHERE VisualId IS NOT NULL AND DocumentId IS NOT NULL
        """
        vabd_rows = [tuple(r) for r in snapshot.execute_sql(vabd_sql)]
        vabd_distinct = set(vabd_rows)
        print(f"  VisualAssetBelongsToDoc candidates: {len(vabd_rows)} total, {len(vabd_distinct)} distinct.")
        preflight_report["candidate_counts"]["VisualAssetBelongsToDoc"] = {
            "total": len(vabd_rows),
            "distinct": len(vabd_distinct),
            "expected": 125,
        }
        if len(vabd_rows) != 125 or len(vabd_distinct) != 125:
            preflight_report["verdict"] = "FAIL"

        # 1c. SolutionImplementsDoc (from SolutionCards)
        sid_sql = """
        SELECT CardId, DocumentId 
        FROM SolutionCards 
        WHERE CardId IS NOT NULL AND DocumentId IS NOT NULL
        """
        sid_rows = [tuple(r) for r in snapshot.execute_sql(sid_sql)]
        sid_distinct = set(sid_rows)
        print(f"  SolutionImplementsDoc candidates: {len(sid_rows)} total, {len(sid_distinct)} distinct.")
        preflight_report["candidate_counts"]["SolutionImplementsDoc"] = {
            "total": len(sid_rows),
            "distinct": len(sid_distinct),
            "expected": 5,
        }
        if len(sid_rows) != 5 or len(sid_distinct) != 5:
            preflight_report["verdict"] = "FAIL"

        total_candidates = len(dcc_rows) + len(vabd_rows) + len(sid_rows)
        print(f"Total candidate edges to backfill: {total_candidates} (expected: 325).")

        # ====================================================================
        # 2. Duplicate Check
        # ====================================================================
        print("\n--- 2. Duplicate Edge Key Check ---")
        dcc_dups = len(dcc_rows) - len(dcc_distinct)
        vabd_dups = len(vabd_rows) - len(vabd_distinct)
        sid_dups = len(sid_rows) - len(sid_distinct)

        preflight_report["duplicates"] = {
            "DocumentContainsChunk": dcc_dups,
            "VisualAssetBelongsToDoc": vabd_dups,
            "SolutionImplementsDoc": sid_dups,
        }
        print(f"  DocumentContainsChunk duplicates: {dcc_dups}")
        print(f"  VisualAssetBelongsToDoc duplicates: {vabd_dups}")
        print(f"  SolutionImplementsDoc duplicates: {sid_dups}")
        if dcc_dups > 0 or vabd_dups > 0 or sid_dups > 0:
            print("ERROR: Duplicate candidate edge keys detected!")
            preflight_report["verdict"] = "FAIL"

        # ====================================================================
        # 3. Referential Integrity Check (Missing References / Orphans)
        # ====================================================================
        print("\n--- 3. Referential Integrity Verification ---")
        
        # 3a. Documents referenced in EvidenceChunks
        orphan_dcc_sql = """
        SELECT COUNT(1) 
        FROM EvidenceChunks c 
        WHERE c.DocumentId IS NOT NULL 
          AND c.DocumentId NOT IN (SELECT DocumentId FROM Documents)
        """
        orphan_dcc = list(snapshot.execute_sql(orphan_dcc_sql))[0][0]
        print(f"  Orphan Document references in EvidenceChunks: {orphan_dcc}")

        # 3b. Documents referenced in VisualAssets
        orphan_vabd_sql = """
        SELECT COUNT(1) 
        FROM VisualAssets v 
        WHERE v.DocumentId IS NOT NULL 
          AND v.DocumentId NOT IN (SELECT DocumentId FROM Documents)
        """
        orphan_vabd = list(snapshot.execute_sql(orphan_vabd_sql))[0][0]
        print(f"  Orphan Document references in VisualAssets: {orphan_vabd}")

        # 3c. Documents referenced in SolutionCards
        orphan_sid_sql = """
        SELECT COUNT(1) 
        FROM SolutionCards s 
        WHERE s.DocumentId IS NOT NULL 
          AND s.DocumentId NOT IN (SELECT DocumentId FROM Documents)
        """
        orphan_sid = list(snapshot.execute_sql(orphan_sid_sql))[0][0]
        print(f"  Orphan Document references in SolutionCards: {orphan_sid}")

        preflight_report["missing_references"] = {
            "EvidenceChunks_to_Documents": orphan_dcc,
            "VisualAssets_to_Documents": orphan_vabd,
            "SolutionCards_to_Documents": orphan_sid,
        }
        if orphan_dcc > 0 or orphan_vabd > 0 or orphan_sid > 0:
            print("ERROR: Orphan foreign key references detected!")
            preflight_report["verdict"] = "FAIL"

        # ====================================================================
        # 4. Target Edge Tables Current State
        # ====================================================================
        print("\n--- 4. Target Edge Table Current State ---")
        edge_counts = {}
        for tbl in ["DocumentContainsChunk", "VisualAssetBelongsToDoc", "SolutionImplementsDoc"]:
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            edge_counts[tbl] = cnt
            print(f"  Current rows in {tbl}: {cnt}")
        preflight_report["existing_edge_counts"] = edge_counts

        # ====================================================================
        # 5. Migration State Tables Current State
        # ====================================================================
        print("\n--- 5. Migration Audit Tables Current State ---")
        mb_cnt = list(snapshot.execute_sql("SELECT COUNT(1) FROM MigrationBatches"))[0][0]
        mm_cnt = list(snapshot.execute_sql("SELECT COUNT(1) FROM MigrationManifest"))[0][0]
        print(f"  Current rows in MigrationBatches: {mb_cnt}")
        print(f"  Current rows in MigrationManifest: {mm_cnt}")
        preflight_report["migration_table_counts"] = {
            "MigrationBatches": mb_cnt,
            "MigrationManifest": mm_cnt,
        }

        # ====================================================================
        # 6. Transaction & Mutation Volume Analysis
        # ====================================================================
        print("\n--- 6. Transactional Capacity & Resumption Strategy ---")
        edge_mutations = total_candidates  # 325 inserts
        manifest_mutations = total_candidates  # 325 manifest inserts
        batch_header_mutations = 1  # 1 header insert
        total_mutations = edge_mutations + manifest_mutations + batch_header_mutations
        print(f"  Planned edge mutations:       {edge_mutations}")
        print(f"  Planned manifest mutations:   {manifest_mutations}")
        print(f"  Planned batch header:         {batch_header_mutations}")
        print(f"  Total transaction mutations:  {total_mutations}")
        print(f"  Spanner mutation limit:       80,000")
        print(f"  Capacity utilization:         {total_mutations / 80000 * 100:.2f}% (Safe for single atomic commit)")

    print("\n" + "=" * 60)
    print(f"PHASE 2C.5 BACKFILL PREFLIGHT VERDICT: {preflight_report['verdict']}")
    print("=" * 60)

    if preflight_report["verdict"] != "PASS":
        sys.exit(1)

if __name__ == "__main__":
    main()
