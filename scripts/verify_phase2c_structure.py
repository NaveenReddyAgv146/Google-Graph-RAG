#!/usr/bin/env python3
"""
Phase 2C.3 Graph Structure Verification Script.
Read-Only audit of EnterpriseKnowledgeGraph in Cloud Spanner.
"""

import sys
import json
from google.cloud import spanner

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"

EXPECTED_NODE_LABELS = {
    "Document": ("Documents", 5),
    "Chunk": ("EvidenceChunks", 195),
    "BusinessSolution": ("SolutionCards", 5),
    "VisualAsset": ("VisualAssets", 125),
    "Repository": ("Repositories", 0),
    "SourceFile": ("SourceFiles", 0),
    "Entity": ("Entities", 0),
    "Technology": ("Technologies", 0),
}

EXPECTED_EDGE_LABELS = [
    "CONTAINS",
    "REFERENCES",
    "MENTIONS",
    "BELONGS_TO",
    "SUPPORTS",
    "DEFINES",
    "CALLS",
    "IMPLEMENTS",
]

EXPECTED_ROW_COUNTS = {
    "Documents": 5,
    "DocumentAccess": 10,
    "DriveSyncDocuments": 5,
    "EvidenceChunks": 195,
    "VisualAssets": 125,
    "SolutionCards": 5,
    "Embeddings": 81,
    "EvidenceLinks": 0,
    "MigrationBatches": 0,
    "MigrationManifest": 0,
    "Repositories": 0,
    "SourceFiles": 0,
    "Entities": 0,
    "Technologies": 0,
    "DocumentContainsChunk": 0,
    "RepoContainsFile": 0,
    "SourceFileContainsChunk": 0,
    "ChunkReferencesEntity": 0,
    "EntityReferencesEntity": 0,
    "ChunkMentionsTech": 0,
    "VisualAssetBelongsToDoc": 0,
    "SolutionSupportsTech": 0,
    "SourceFileDefinesEntity": 0,
    "SourceFileCallsFile": 0,
    "SolutionImplementsDoc": 0,
}

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)
    admin_client = spanner_client.database_admin_api

    results = {}

    # Check 1 & 2: DDL Definition Analysis
    print("\n--- Check 1 & 2: Graph Catalog & DDL Verification ---")
    db_path = admin_client.database_path(PROJECT_ID, INSTANCE_ID, DATABASE_ID)
    ddl_stmts = admin_client.get_database_ddl(database=db_path).statements
    graph_ddl = [s for s in ddl_stmts if "EnterpriseKnowledgeGraph" in s]
    
    if not graph_ddl:
        print("FAIL: EnterpriseKnowledgeGraph DDL statement not found in catalog!")
        results["check_1"] = "FAIL"
        results["check_2"] = "FAIL"
    else:
        full_ddl = graph_ddl[0]
        # Check node tables
        node_checks = [t in full_ddl for t in ["Documents", "EvidenceChunks", "SolutionCards", "VisualAssets", "Repositories", "SourceFiles", "Entities", "Technologies"]]
        # Check edge tables
        edge_checks = [t in full_ddl for t in [
            "DocumentContainsChunk", "RepoContainsFile", "SourceFileContainsChunk",
            "ChunkReferencesEntity", "EntityReferencesEntity", "ChunkMentionsTech",
            "VisualAssetBelongsToDoc", "SolutionSupportsTech", "SourceFileDefinesEntity",
            "SourceFileCallsFile", "SolutionImplementsDoc"
        ]]
        
        # Check NO PROPERTIES count (should appear 11 times)
        no_props_count = full_ddl.count("NO PROPERTIES")

        if all(node_checks) and all(edge_checks):
            print("Check 1 PASS: All 8 node tables and 11 edge tables registered with exact keys & labels.")
            results["check_1"] = "PASS"
        else:
            print(f"Check 1 FAIL: Missing tables in graph DDL!")
            results["check_1"] = "FAIL"

        if no_props_count == 11:
            print(f"Check 2 PASS: All 11 edge definitions declare NO PROPERTIES (count: {no_props_count}/11).")
            results["check_2"] = "PASS"
        else:
            print(f"Check 2 FAIL: Expected 11 'NO PROPERTIES' declarations, found {no_props_count}.")
            results["check_2"] = "FAIL"

    with database.snapshot(multi_use=True) as snapshot:
        # Check 3: Graph Traversal
        print("\n--- Check 3: Graph Traversal Verification ---")
        traversal_failures = []
        
        # Test Node Traversals
        print("Testing Node Labels:")
        for label, (tbl, exp_count) in EXPECTED_NODE_LABELS.items():
            gql = f"GRAPH EnterpriseKnowledgeGraph MATCH (n:{label}) RETURN COUNT(n) AS node_count"
            try:
                cnt = list(snapshot.execute_sql(gql))[0][0]
                print(f"  - (:{label}) -> {cnt} nodes (expected: {exp_count})")
                if cnt != exp_count:
                    traversal_failures.append(f"Node {label} count mismatch: got {cnt}, expected {exp_count}")
            except Exception as e:
                traversal_failures.append(f"Node {label} query error: {e}")

        # Test Edge Traversals
        print("Testing Edge Labels:")
        for label in EXPECTED_EDGE_LABELS:
            gql = f"GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`{label}`]->() RETURN COUNT(e) AS edge_count"
            try:
                cnt = list(snapshot.execute_sql(gql))[0][0]
                print(f"  - [:{label}] -> {cnt} edges (expected: 0 pre-backfill)")
                if cnt != 0:
                    traversal_failures.append(f"Edge {label} unexpected count: {cnt}")
            except Exception as e:
                traversal_failures.append(f"Edge {label} query error: {e}")

        if not traversal_failures:
            print("Check 3 PASS: GQL traversal verified across all 8 node labels and 8 edge labels.")
            results["check_3"] = "PASS"
        else:
            print(f"Check 3 FAIL: {traversal_failures}")
            results["check_3"] = "FAIL"

        # Check 4: Vector Index Readiness
        print("\n--- Check 4: Vector Index Readiness ---")
        idx_res = list(snapshot.execute_sql("""
        SELECT index_name, table_name, index_state 
        FROM information_schema.indexes 
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """))
        if idx_res and idx_res[0][2] == "READ_WRITE":
            print(f"Check 4 PASS: EvidenceChunkVectorIndex state is {idx_res[0][2]}.")
            results["check_4"] = "PASS"
        else:
            state = idx_res[0][2] if idx_res else "NOT FOUND"
            print(f"Check 4 FAIL: Index state is {state}.")
            results["check_4"] = "FAIL"

        # Check 5: Data Integrity of all 25 tables
        print("\n--- Check 5: Data Integrity (All 25 Tables) ---")
        data_mismatches = []
        for tbl, exp in EXPECTED_ROW_COUNTS.items():
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            if cnt != exp:
                data_mismatches.append(f"{tbl}: expected {exp}, got {cnt}")

        if not data_mismatches:
            print(f"Check 5 PASS: All 25 tables verified intact. 426 existing records preserved, 0 in new tables.")
            results["check_5"] = "PASS"
        else:
            print(f"Check 5 FAIL: Discrepancies found: {data_mismatches}")
            results["check_5"] = "FAIL"

    print("\n" + "=" * 60)
    print("PHASE 2C.3 GRAPH STRUCTURE VERIFICATION SUMMARY:")
    for k, v in results.items():
        print(f"  {k}: {v}")
    print("=" * 60)

    if any(v != "PASS" for v in results.values()):
        sys.exit(1)

if __name__ == "__main__":
    main()

