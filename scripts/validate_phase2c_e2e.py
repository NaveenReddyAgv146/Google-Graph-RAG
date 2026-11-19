#!/usr/bin/env python3
"""
Phase 2C.6 — End-to-End Migration Validation Script (Strictly Read-Only).

Target:
  Project:  xenon-broker-513704-s9
  Instance: sales-discovery-trial
  Database: sales-knowledge-db
  BatchId:  BATCH_BACKFILL_20261111_01

Validations:
1. Exact edge counts across the three backfilled tables (195, 125, 5 -> 325 total).
2. Manifest 1-to-1 bijection verification (325 entries match exact keys).
3. GQL single-hop traversals:
   - Document -> Chunk: 195
   - VisualAsset -> Document: 125
   - BusinessSolution -> Document: 5
4. GQL multi-hop traversals & relational consistency:
   - VisualAsset -> Document -> Chunk (paths vs relational join)
   - BusinessSolution -> Document -> Chunk (paths vs relational join)
   - BusinessSolution -> Document <- VisualAsset (paths vs relational join)
5. Zero orphan edges, zero duplicate keys, zero missing manifest entries.
6. Baseline data integrity (original 426 records intact) & Vector Index READ_WRITE.
7. Batch lifecycle state (PENDING_VALIDATION, AcceptedAt IS NULL) & Phase 1F guards.
"""

import sys
import json
from google.cloud import spanner

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"
BATCH_ID = "BATCH_BACKFILL_20261111_01"

ORIGINAL_TABLES = {
    "Documents": 5,
    "DocumentAccess": 10,
    "DriveSyncDocuments": 5,
    "EvidenceChunks": 195,
    "VisualAssets": 125,
    "SolutionCards": 5,
    "Embeddings": 81,
    "EvidenceLinks": 0,
}

UNPOPULATED_TABLES = {
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
}


def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    results = {}
    all_passed = True

    with database.snapshot(multi_use=True) as snapshot:
        # ====================================================================
        # Check 1: Verify exactly 325 edges across the three backfilled tables
        # ====================================================================
        print("\n--- Check 1: Edge Counts Across Backfilled Tables ---")
        dcc_count = list(snapshot.execute_sql("SELECT COUNT(1) FROM DocumentContainsChunk"))[0][0]
        vabd_count = list(snapshot.execute_sql("SELECT COUNT(1) FROM VisualAssetBelongsToDoc"))[0][0]
        sid_count = list(snapshot.execute_sql("SELECT COUNT(1) FROM SolutionImplementsDoc"))[0][0]
        total_edges = dcc_count + vabd_count + sid_count

        c1_pass = (dcc_count == 195 and vabd_count == 125 and sid_count == 5 and total_edges == 325)
        results["check_1"] = {
            "status": "PASS" if c1_pass else "FAIL",
            "DocumentContainsChunk": {"actual": dcc_count, "expected": 195},
            "VisualAssetBelongsToDoc": {"actual": vabd_count, "expected": 125},
            "SolutionImplementsDoc": {"actual": sid_count, "expected": 5},
            "total_edges": {"actual": total_edges, "expected": 325},
        }
        print(f"  DocumentContainsChunk:   {dcc_count} (expected: 195)")
        print(f"  VisualAssetBelongsToDoc: {vabd_count} (expected: 125)")
        print(f"  SolutionImplementsDoc:   {sid_count} (expected: 5)")
        print(f"  Total Backfilled Edges:  {total_edges} (expected: 325) -> {'PASS' if c1_pass else 'FAIL'}")
        if not c1_pass:
            all_passed = False

        # ====================================================================
        # Check 2: Verify all 325 manifest entries map 1-to-1 to inserted edges
        # ====================================================================
        print("\n--- Check 2: MigrationManifest 1-to-1 Mapping Verification ---")
        mm_batch_count = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM MigrationManifest WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))[0][0]
        
        mm_total_count = list(snapshot.execute_sql("SELECT COUNT(1) FROM MigrationManifest"))[0][0]

        # Manifest entries per table
        mm_breakdown = dict(list(snapshot.execute_sql(
            "SELECT TableName, COUNT(1) FROM MigrationManifest WHERE BatchId = @b GROUP BY TableName",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        )))

        # Verify 1-to-1 mapping for each edge table
        # 2a. DocumentContainsChunk
        dcc_edges_set = set((r[0], r[1]) for r in snapshot.execute_sql("SELECT DocumentId, ChunkId FROM DocumentContainsChunk"))
        dcc_manifest_rows = list(snapshot.execute_sql(
            "SELECT RecordKey FROM MigrationManifest WHERE BatchId = @b AND TableName = 'DocumentContainsChunk'",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        dcc_manifest_set = set(tuple(r[0].split("#", 1)) for r in dcc_manifest_rows)
        dcc_diff = dcc_edges_set.symmetric_difference(dcc_manifest_set)

        # 2b. VisualAssetBelongsToDoc
        vabd_edges_set = set((r[0], r[1]) for r in snapshot.execute_sql("SELECT VisualId, DocumentId FROM VisualAssetBelongsToDoc"))
        vabd_manifest_rows = list(snapshot.execute_sql(
            "SELECT RecordKey FROM MigrationManifest WHERE BatchId = @b AND TableName = 'VisualAssetBelongsToDoc'",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        vabd_manifest_set = set(tuple(r[0].split("#", 1)) for r in vabd_manifest_rows)
        vabd_diff = vabd_edges_set.symmetric_difference(vabd_manifest_set)

        # 2c. SolutionImplementsDoc
        sid_edges_set = set((r[0], r[1]) for r in snapshot.execute_sql("SELECT CardId, DocumentId FROM SolutionImplementsDoc"))
        sid_manifest_rows = list(snapshot.execute_sql(
            "SELECT RecordKey FROM MigrationManifest WHERE BatchId = @b AND TableName = 'SolutionImplementsDoc'",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        sid_manifest_set = set(tuple(r[0].split("#", 1)) for r in sid_manifest_rows)
        sid_diff = sid_edges_set.symmetric_difference(sid_manifest_set)

        c2_pass = (
            mm_batch_count == 325 and
            mm_total_count == 325 and
            mm_breakdown.get("DocumentContainsChunk", 0) == 195 and
            mm_breakdown.get("VisualAssetBelongsToDoc", 0) == 125 and
            mm_breakdown.get("SolutionImplementsDoc", 0) == 5 and
            len(dcc_diff) == 0 and
            len(vabd_diff) == 0 and
            len(sid_diff) == 0
        )
        results["check_2"] = {
            "status": "PASS" if c2_pass else "FAIL",
            "batch_manifest_count": mm_batch_count,
            "total_manifest_count": mm_total_count,
            "manifest_breakdown": mm_breakdown,
            "symmetric_diff_dcc": len(dcc_diff),
            "symmetric_diff_vabd": len(vabd_diff),
            "symmetric_diff_sid": len(sid_diff),
        }
        print(f"  Batch Manifest Count:     {mm_batch_count} (expected: 325)")
        print(f"  Total Manifest Rows:      {mm_total_count} (expected: 325, 0 outside batch)")
        print(f"  Breakdown:                {mm_breakdown}")
        print(f"  DCC Manifest Bijection:   {len(dcc_diff)} discrepancies")
        print(f"  VABD Manifest Bijection:  {len(vabd_diff)} discrepancies")
        print(f"  SID Manifest Bijection:   {len(sid_diff)} discrepancies")
        print(f"  Check 2 Verdict:          {'PASS' if c2_pass else 'FAIL'}")
        if not c2_pass:
            all_passed = False

        # ====================================================================
        # Check 3: GQL Single-Hop Traversals
        # ====================================================================
        print("\n--- Check 3: GQL Single-Hop Traversals ---")
        # 3a. Document -> Chunk
        gql_dcc = "GRAPH EnterpriseKnowledgeGraph MATCH (d:Document)-[r:`CONTAINS`]->(c:Chunk) RETURN COUNT(r) AS cnt"
        gql_dcc_count = list(snapshot.execute_sql(gql_dcc))[0][0]

        # 3b. VisualAsset -> Document
        gql_vabd = "GRAPH EnterpriseKnowledgeGraph MATCH (v:VisualAsset)-[r:BELONGS_TO]->(d:Document) RETURN COUNT(r) AS cnt"
        gql_vabd_count = list(snapshot.execute_sql(gql_vabd))[0][0]

        # 3c. BusinessSolution -> Document
        gql_sid = "GRAPH EnterpriseKnowledgeGraph MATCH (s:BusinessSolution)-[r:IMPLEMENTS]->(d:Document) RETURN COUNT(r) AS cnt"
        gql_sid_count = list(snapshot.execute_sql(gql_sid))[0][0]

        c3_pass = (gql_dcc_count == 195 and gql_vabd_count == 125 and gql_sid_count == 5)
        results["check_3"] = {
            "status": "PASS" if c3_pass else "FAIL",
            "Document_to_Chunk": {"actual": gql_dcc_count, "expected": 195},
            "VisualAsset_to_Document": {"actual": gql_vabd_count, "expected": 125},
            "BusinessSolution_to_Document": {"actual": gql_sid_count, "expected": 5},
        }
        print(f"  GQL Document -> Chunk:           {gql_dcc_count} (expected: 195)")
        print(f"  GQL VisualAsset -> Document:     {gql_vabd_count} (expected: 125)")
        print(f"  GQL BusinessSolution -> Document:{gql_sid_count} (expected: 5)")
        print(f"  Check 3 Verdict:                 {'PASS' if c3_pass else 'FAIL'}")
        if not c3_pass:
            all_passed = False

        # ====================================================================
        # Check 4: Multi-hop Traversals & Relational/GQL Consistency
        # ====================================================================
        print("\n--- Check 4: Multi-Hop Traversals & Relational/GQL Consistency ---")
        
        # 4a. BusinessSolution -> Document -> Chunk
        # GQL path count
        gql_sol_doc_chk = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (s:BusinessSolution)-[:IMPLEMENTS]->(d:Document)-[:`CONTAINS`]->(c:Chunk) 
        RETURN COUNT(c) AS path_count
        """
        gql_sdc_count = list(snapshot.execute_sql(gql_sol_doc_chk))[0][0]

        # Relational equivalent
        sql_sol_doc_chk = """
        SELECT COUNT(1) 
        FROM SolutionCards s
        JOIN Documents d ON s.DocumentId = d.DocumentId
        JOIN EvidenceChunks c ON d.DocumentId = c.DocumentId
        """
        sql_sdc_count = list(snapshot.execute_sql(sql_sol_doc_chk))[0][0]

        # 4b. VisualAsset -> Document -> Chunk
        gql_vis_doc_chk = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (v:VisualAsset)-[:BELONGS_TO]->(d:Document)-[:`CONTAINS`]->(c:Chunk) 
        RETURN COUNT(c) AS path_count
        """
        gql_vdc_count = list(snapshot.execute_sql(gql_vis_doc_chk))[0][0]

        # Relational equivalent
        sql_vis_doc_chk = """
        SELECT COUNT(1) 
        FROM VisualAssets v
        JOIN Documents d ON v.DocumentId = d.DocumentId
        JOIN EvidenceChunks c ON d.DocumentId = c.DocumentId
        """
        sql_vdc_count = list(snapshot.execute_sql(sql_vis_doc_chk))[0][0]

        # 4c. BusinessSolution -> Document <- VisualAsset (Branching)
        gql_sol_doc_vis = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (s:BusinessSolution)-[:IMPLEMENTS]->(d:Document)<-[:BELONGS_TO]-(v:VisualAsset) 
        RETURN COUNT(v) AS path_count
        """
        gql_sdv_count = list(snapshot.execute_sql(gql_sol_doc_vis))[0][0]

        sql_sol_doc_vis = """
        SELECT COUNT(1) 
        FROM SolutionCards s
        JOIN Documents d ON s.DocumentId = d.DocumentId
        JOIN VisualAssets v ON d.DocumentId = v.DocumentId
        """
        sql_sdv_count = list(snapshot.execute_sql(sql_sol_doc_vis))[0][0]

        c4_pass = (
            gql_sdc_count == sql_sdc_count and
            gql_vdc_count == sql_vdc_count and
            gql_sdv_count == sql_sdv_count and
            gql_sdc_count > 0 and
            gql_vdc_count > 0 and
            gql_sdv_count > 0
        )
        results["check_4"] = {
            "status": "PASS" if c4_pass else "FAIL",
            "Solution_Document_Chunk": {"gql": gql_sdc_count, "sql": sql_sdc_count, "match": gql_sdc_count == sql_sdc_count},
            "Visual_Document_Chunk": {"gql": gql_vdc_count, "sql": sql_vdc_count, "match": gql_vdc_count == sql_vdc_count},
            "Solution_Document_Visual": {"gql": gql_sdv_count, "sql": sql_sdv_count, "match": gql_sdv_count == sql_sdv_count},
        }
        print(f"  BusinessSolution -> Document -> Chunk: GQL={gql_sdc_count}, SQL={sql_sdc_count} (Match: {gql_sdc_count == sql_sdc_count})")
        print(f"  VisualAsset -> Document -> Chunk:      GQL={gql_vdc_count}, SQL={sql_vdc_count} (Match: {gql_vdc_count == sql_vdc_count})")
        print(f"  BusinessSolution -> Document <- Visual:GQL={gql_sdv_count}, SQL={sql_sdv_count} (Match: {gql_sdv_count == sql_sdv_count})")
        print(f"  Check 4 Verdict:                       {'PASS' if c4_pass else 'FAIL'}")
        if not c4_pass:
            all_passed = False

        # ====================================================================
        # Check 5: Zero orphan edges, duplicate keys, or missing manifest entries
        # ====================================================================
        print("\n--- Check 5: Orphan Edges & Key Integrity ---")
        # 5a. Orphan check for DocumentContainsChunk
        dcc_orphan_doc = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM DocumentContainsChunk WHERE DocumentId NOT IN (SELECT DocumentId FROM Documents)"
        ))[0][0]
        dcc_orphan_chk = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM DocumentContainsChunk WHERE ChunkId NOT IN (SELECT ChunkId FROM EvidenceChunks)"
        ))[0][0]

        # 5b. Orphan check for VisualAssetBelongsToDoc
        vabd_orphan_vis = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM VisualAssetBelongsToDoc WHERE VisualId NOT IN (SELECT VisualId FROM VisualAssets)"
        ))[0][0]
        vabd_orphan_doc = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM VisualAssetBelongsToDoc WHERE DocumentId NOT IN (SELECT DocumentId FROM Documents)"
        ))[0][0]

        # 5c. Orphan check for SolutionImplementsDoc
        sid_orphan_card = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM SolutionImplementsDoc WHERE CardId NOT IN (SELECT CardId FROM SolutionCards)"
        ))[0][0]
        sid_orphan_doc = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM SolutionImplementsDoc WHERE DocumentId NOT IN (SELECT DocumentId FROM Documents)"
        ))[0][0]

        # 5d. Duplicate edge key checks
        dcc_dups = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM (SELECT DocumentId, ChunkId, COUNT(1) c FROM DocumentContainsChunk GROUP BY DocumentId, ChunkId HAVING c > 1)"
        ))[0][0]
        vabd_dups = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM (SELECT VisualId, DocumentId, COUNT(1) c FROM VisualAssetBelongsToDoc GROUP BY VisualId, DocumentId HAVING c > 1)"
        ))[0][0]
        sid_dups = list(snapshot.execute_sql(
            "SELECT COUNT(1) FROM (SELECT CardId, DocumentId, COUNT(1) c FROM SolutionImplementsDoc GROUP BY CardId, DocumentId HAVING c > 1)"
        ))[0][0]

        total_orphans = dcc_orphan_doc + dcc_orphan_chk + vabd_orphan_vis + vabd_orphan_doc + sid_orphan_card + sid_orphan_doc
        total_dups = dcc_dups + vabd_dups + sid_dups

        c5_pass = (total_orphans == 0 and total_dups == 0)
        results["check_5"] = {
            "status": "PASS" if c5_pass else "FAIL",
            "orphans": {
                "DCC_orphan_doc": dcc_orphan_doc,
                "DCC_orphan_chunk": dcc_orphan_chk,
                "VABD_orphan_visual": vabd_orphan_vis,
                "VABD_orphan_doc": vabd_orphan_doc,
                "SID_orphan_card": sid_orphan_card,
                "SID_orphan_doc": sid_orphan_doc,
                "total_orphans": total_orphans,
            },
            "duplicate_keys": {
                "DCC": dcc_dups,
                "VABD": vabd_dups,
                "SID": sid_dups,
                "total_duplicates": total_dups,
            }
        }
        print(f"  Total Orphan Edges:  {total_orphans}")
        print(f"  Total Duplicate Keys:{total_dups}")
        print(f"  Check 5 Verdict:     {'PASS' if c5_pass else 'FAIL'}")
        if not c5_pass:
            all_passed = False

        # ====================================================================
        # Check 6: Baseline Integrity (426 records) & Vector Index State
        # ====================================================================
        print("\n--- Check 6: Baseline Data Integrity & Vector Index Readiness ---")
        total_baseline = 0
        baseline_mismatches = []
        for tbl, exp_cnt in ORIGINAL_TABLES.items():
            actual = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            if actual != exp_cnt:
                baseline_mismatches.append(f"{tbl}: exp {exp_cnt}, got {actual}")
            total_baseline += actual
        
        # Check unpopulated tables remain 0
        unpopulated_mismatches = []
        for tbl, exp_cnt in UNPOPULATED_TABLES.items():
            actual = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            if actual != exp_cnt:
                unpopulated_mismatches.append(f"{tbl}: exp {exp_cnt}, got {actual}")

        # Check Vector Index
        idx_sql = """
        SELECT index_name, index_state, index_type, is_unique
        FROM information_schema.indexes
        WHERE table_name = 'EvidenceChunks' AND index_name = 'EvidenceChunkVectorIndex'
        """
        idx_res = list(snapshot.execute_sql(idx_sql))
        idx_state = idx_res[0][1] if idx_res else "NOT_FOUND"

        c6_pass = (
            len(baseline_mismatches) == 0 and
            total_baseline == 426 and
            len(unpopulated_mismatches) == 0 and
            idx_state == "READ_WRITE"
        )
        results["check_6"] = {
            "status": "PASS" if c6_pass else "FAIL",
            "total_original_records": total_baseline,
            "baseline_mismatches": baseline_mismatches,
            "unpopulated_mismatches": unpopulated_mismatches,
            "vector_index_state": idx_state,
        }
        print(f"  Original Populated Records: {total_baseline} (expected: 426, mismatches: {len(baseline_mismatches)})")
        print(f"  Unpopulated Tables:         {len(UNPOPULATED_TABLES)} tables verified 0 rows (mismatches: {len(unpopulated_mismatches)})")
        print(f"  EvidenceChunkVectorIndex:   {idx_state} (expected: READ_WRITE)")
        print(f"  Check 6 Verdict:            {'PASS' if c6_pass else 'FAIL'}")
        if not c6_pass:
            all_passed = False

        # ====================================================================
        # Check 7: Lifecycle State & Phase 1F Acceptance / Rollback Guards
        # ====================================================================
        print("\n--- Check 7: Batch Lifecycle State & Phase 1F Guards ---")
        batch_rows = list(snapshot.execute_sql(
            "SELECT BatchId, State, TotalInsertedEdges, CreatedAt, AcceptedAt FROM MigrationBatches WHERE BatchId = @b",
            params={"b": BATCH_ID},
            param_types={"b": spanner.param_types.STRING}
        ))
        
        if not batch_rows:
            batch_state = "NOT_FOUND"
            accepted_at = "NOT_FOUND"
            total_inserted = 0
            created_at = None
        else:
            b_id, batch_state, total_inserted, created_at, accepted_at = batch_rows[0]

        # Phase 1F Invariants verification:
        # Invariant 1: State is strictly 'PENDING_VALIDATION'
        inv1 = (batch_state == "PENDING_VALIDATION")
        # Invariant 1b: AcceptedAt is strictly NULL (not marked ACCEPTED)
        inv1b = (accepted_at is None)
        # Invariant 2: TotalInsertedEdges strictly matches backfill
        inv2 = (total_inserted == 325)
        # Invariant 4/5: Guard status
        # Acceptance guard precondition: current_state == 'PENDING_VALIDATION' -> ELIGIBLE FOR ACCEPTANCE
        # Rollback guard precondition: current_state == 'PENDING_VALIDATION' -> ROLLBACK ARMED (NOT LOCKED)
        guard_eligible_acceptance = inv1 and inv1b
        guard_rollback_armed = inv1 and (batch_state != "ACCEPTED")

        c7_pass = (inv1 and inv1b and inv2 and guard_eligible_acceptance and guard_rollback_armed)
        results["check_7"] = {
            "status": "PASS" if c7_pass else "FAIL",
            "BatchId": BATCH_ID,
            "State": batch_state,
            "AcceptedAt": str(accepted_at) if accepted_at else None,
            "TotalInsertedEdges": total_inserted,
            "CreatedAt": str(created_at) if created_at else None,
            "lifecycle_invariants": {
                "Invariant_1_State_Is_Pending_Validation": inv1,
                "Invariant_1b_AcceptedAt_Is_None": inv1b,
                "Invariant_2_TotalInsertedEdges_325": inv2,
                "Stage_4_Acceptance_Guard_Precondition_Met": guard_eligible_acceptance,
                "Rollback_Guard_Armed_And_Safe": guard_rollback_armed,
            }
        }
        print(f"  BatchId:                       {BATCH_ID}")
        print(f"  Current State:                 {batch_state} (Invariant 1: {inv1})")
        print(f"  AcceptedAt:                    {accepted_at} (Invariant 1b: {inv1b})")
        print(f"  TotalInsertedEdges:            {total_inserted} (Invariant 2: {inv2})")
        print(f"  CreatedAt:                     {created_at}")
        print(f"  Stage 4 Acceptance Gate Ready: {guard_eligible_acceptance}")
        print(f"  Rollback Guard Armed:          {guard_rollback_armed}")
        print(f"  Check 7 Verdict:               {'PASS' if c7_pass else 'FAIL'}")
        if not c7_pass:
            all_passed = False

    # Summary
    print("\n" + "=" * 60)
    print("PHASE 2C.6 END-TO-END VALIDATION SUMMARY:")
    for k, v in results.items():
        print(f"  {k}: {v['status']}")
    print(f"OVERALL VERDICT: {'PASS' if all_passed else 'FAIL'}")
    print("=" * 60)

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()

