#!/usr/bin/env python3
"""
Phase 2C.4 Property Graph Functional Validation Script.
Read-Only functional testing of EnterpriseKnowledgeGraph in Cloud Spanner.
"""

import sys
import json
from google.cloud import spanner

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"

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

NODE_LABEL_QUERIES = [
    ("Document", "GRAPH EnterpriseKnowledgeGraph MATCH (n:Document) RETURN n.DocumentId AS doc_id, n.FileName AS file_name LIMIT 1"),
    ("Chunk", "GRAPH EnterpriseKnowledgeGraph MATCH (n:Chunk) RETURN n.ChunkId AS chunk_id, n.Modality AS modality LIMIT 1"),
    ("BusinessSolution", "GRAPH EnterpriseKnowledgeGraph MATCH (n:BusinessSolution) RETURN n.CardId AS card_id, n.ProjectName AS project_name LIMIT 1"),
    ("VisualAsset", "GRAPH EnterpriseKnowledgeGraph MATCH (n:VisualAsset) RETURN n.VisualId AS visual_id, n.ImageGcsUri AS uri LIMIT 1"),
    ("Repository", "GRAPH EnterpriseKnowledgeGraph MATCH (n:Repository) RETURN n.RepoId AS repo_id, n.Name AS name LIMIT 1"),
    ("SourceFile", "GRAPH EnterpriseKnowledgeGraph MATCH (n:SourceFile) RETURN n.FileId AS file_id, n.FilePath AS path LIMIT 1"),
    ("Entity", "GRAPH EnterpriseKnowledgeGraph MATCH (n:Entity) RETURN n.EntityId AS entity_id, n.Name AS name LIMIT 1"),
    ("Technology", "GRAPH EnterpriseKnowledgeGraph MATCH (n:Technology) RETURN n.TechId AS tech_id, n.Name AS name LIMIT 1"),
]

EDGE_LABEL_QUERIES = [
    ("CONTAINS", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`CONTAINS`]->() RETURN COUNT(e) AS cnt"),
    ("REFERENCES", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`REFERENCES`]->() RETURN COUNT(e) AS cnt"),
    ("MENTIONS", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`MENTIONS`]->() RETURN COUNT(e) AS cnt"),
    ("BELONGS_TO", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`BELONGS_TO`]->() RETURN COUNT(e) AS cnt"),
    ("SUPPORTS", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`SUPPORTS`]->() RETURN COUNT(e) AS cnt"),
    ("DEFINES", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`DEFINES`]->() RETURN COUNT(e) AS cnt"),
    ("CALLS", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`CALLS`]->() RETURN COUNT(e) AS cnt"),
    ("IMPLEMENTS", "GRAPH EnterpriseKnowledgeGraph MATCH ()-[e:`IMPLEMENTS`]->() RETURN COUNT(e) AS cnt"),
]

EDGE_DEFINITION_TRAVERSALS = [
    ("DocumentContainsChunk", "GRAPH EnterpriseKnowledgeGraph MATCH (d:Document)-[e:`CONTAINS`]->(c:Chunk) RETURN d.DocumentId AS doc_id, c.ChunkId AS chunk_id LIMIT 1"),
    ("RepoContainsFile", "GRAPH EnterpriseKnowledgeGraph MATCH (r:Repository)-[e:`CONTAINS`]->(f:SourceFile) RETURN r.RepoId AS repo_id, f.FileId AS file_id LIMIT 1"),
    ("SourceFileContainsChunk", "GRAPH EnterpriseKnowledgeGraph MATCH (f:SourceFile)-[e:`CONTAINS`]->(c:Chunk) RETURN f.FileId AS file_id, c.ChunkId AS chunk_id LIMIT 1"),
    ("ChunkReferencesEntity", "GRAPH EnterpriseKnowledgeGraph MATCH (c:Chunk)-[e:`REFERENCES`]->(ent:Entity) RETURN c.ChunkId AS chunk_id, ent.EntityId AS entity_id LIMIT 1"),
    ("EntityReferencesEntity", "GRAPH EnterpriseKnowledgeGraph MATCH (s:Entity)-[e:`REFERENCES`]->(t:Entity) RETURN s.EntityId AS src_id, t.EntityId AS tgt_id LIMIT 1"),
    ("ChunkMentionsTech", "GRAPH EnterpriseKnowledgeGraph MATCH (c:Chunk)-[e:`MENTIONS`]->(t:Technology) RETURN c.ChunkId AS chunk_id, t.TechId AS tech_id LIMIT 1"),
    ("VisualAssetBelongsToDoc", "GRAPH EnterpriseKnowledgeGraph MATCH (v:VisualAsset)-[e:`BELONGS_TO`]->(d:Document) RETURN v.VisualId AS visual_id, d.DocumentId AS doc_id LIMIT 1"),
    ("SolutionSupportsTech", "GRAPH EnterpriseKnowledgeGraph MATCH (s:BusinessSolution)-[e:`SUPPORTS`]->(t:Technology) RETURN s.CardId AS card_id, t.TechId AS tech_id LIMIT 1"),
    ("SourceFileDefinesEntity", "GRAPH EnterpriseKnowledgeGraph MATCH (f:SourceFile)-[e:`DEFINES`]->(ent:Entity) RETURN f.FileId AS file_id, ent.EntityId AS entity_id LIMIT 1"),
    ("SourceFileCallsFile", "GRAPH EnterpriseKnowledgeGraph MATCH (c:SourceFile)-[e:`CALLS`]->(t:SourceFile) RETURN c.FileId AS caller_id, t.FileId AS callee_id LIMIT 1"),
    ("SolutionImplementsDoc", "GRAPH EnterpriseKnowledgeGraph MATCH (s:BusinessSolution)-[e:`IMPLEMENTS`]->(d:Document) RETURN s.CardId AS card_id, d.DocumentId AS doc_id LIMIT 1"),
]

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    test_results = {}

    with database.snapshot(multi_use=True) as snapshot:
        # ====================================================================
        # TEST 1: GQL patterns for each of the 8 node labels and 8 edge labels
        # ====================================================================
        print("\n--- Test 1: Node & Edge Label GQL Patterns ---")
        t1_failures = []
        for label, gql in NODE_LABEL_QUERIES:
            try:
                rows = list(snapshot.execute_sql(gql))
                print(f"  [Node: {label}] GQL compiled & executed successfully ({len(rows)} row(s)).")
            except Exception as e:
                print(f"  [Node: {label}] ERROR: {e}")
                t1_failures.append(f"Node {label}: {e}")

        for label, gql in EDGE_LABEL_QUERIES:
            try:
                rows = list(snapshot.execute_sql(gql))
                cnt = rows[0][0] if rows else 0
                print(f"  [Edge: {label}] GQL compiled & executed successfully (count = {cnt}).")
            except Exception as e:
                print(f"  [Edge: {label}] ERROR: {e}")
                t1_failures.append(f"Edge {label}: {e}")

        test_results["test_1"] = "PASS" if not t1_failures else f"FAIL ({len(t1_failures)} errors)"

        # ====================================================================
        # TEST 2: Source-to-destination traversal for all 11 edge definitions
        # ====================================================================
        print("\n--- Test 2: Source-to-Destination Traversals (11 Edge Definitions) ---")
        t2_failures = []
        for edge_table, gql in EDGE_DEFINITION_TRAVERSALS:
            try:
                rows = list(snapshot.execute_sql(gql))
                print(f"  [Edge Def: {edge_table}] Compiled & executed cleanly (result rows: {len(rows)} - expected 0 pre-backfill).")
            except Exception as e:
                print(f"  [Edge Def: {edge_table}] ERROR: {e}")
                t2_failures.append(f"{edge_table}: {e}")

        test_results["test_2"] = "PASS" if not t2_failures else f"FAIL ({len(t2_failures)} errors)"

        # ====================================================================
        # TEST 3: Multi-hop traversal syntax and graph query parameter handling
        # ====================================================================
        print("\n--- Test 3: Multi-hop Traversal Syntax & Parameter Handling ---")
        t3_failures = []

        # 3a. Linear 2-hop traversal
        gql_2hop = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (d:Document)-[e1:`CONTAINS`]->(c:Chunk)-[e2:`REFERENCES`]->(ent:Entity) 
        RETURN d.DocumentId AS doc_id, c.ChunkId AS chunk_id, ent.EntityId AS entity_id 
        LIMIT 1
        """
        try:
            r = list(snapshot.execute_sql(gql_2hop))
            print("  [3a. Linear 2-hop traversal] (Document -> Chunk -> Entity) compiled successfully.")
        except Exception as e:
            print(f"  [3a. Linear 2-hop traversal] ERROR: {e}")
            t3_failures.append(f"2-hop linear: {e}")

        # 3b. Branching 2-hop traversal
        gql_branch = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (d:Document)<-[e1:`BELONGS_TO`]-(v:VisualAsset), (d)-[e2:`CONTAINS`]->(c:Chunk) 
        RETURN d.DocumentId AS doc_id, v.VisualId AS visual_id, c.ChunkId AS chunk_id 
        LIMIT 1
        """
        try:
            r = list(snapshot.execute_sql(gql_branch))
            print("  [3b. Branching traversal] (VisualAsset -> Document -> Chunk) compiled successfully.")
        except Exception as e:
            print(f"  [3b. Branching traversal] ERROR: {e}")
            t3_failures.append(f"Branching traversal: {e}")

        # 3c. Parameterized query (Scalar filter)
        doc_rows = list(snapshot.execute_sql("SELECT DocumentId FROM Documents LIMIT 1"))
        sample_doc_id = doc_rows[0][0] if doc_rows else "test_doc"

        gql_param_scalar = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (d:Document) 
        WHERE d.DocumentId = @target_doc_id 
        RETURN d.DocumentId AS doc_id, d.FileName AS file_name
        """
        try:
            r = list(snapshot.execute_sql(
                gql_param_scalar,
                params={"target_doc_id": sample_doc_id},
                param_types={"target_doc_id": spanner.param_types.STRING}
            ))
            print(f"  [3c. Parameterized scalar filter] Retrieved: {r[0][0]} ({r[0][1]}).")
            if not r or r[0][0] != sample_doc_id:
                t3_failures.append("Scalar parameter filter returned unexpected result.")
        except Exception as e:
            print(f"  [3c. Parameterized scalar filter] ERROR: {e}")
            t3_failures.append(f"Scalar param: {e}")

        # 3d. Parameterized list / UNNEST filter (RBAC pattern)
        gql_param_unnest = """
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (d:Document) 
        WHERE d.DocumentId IN UNNEST(@authorized_doc_ids) 
        RETURN d.DocumentId AS doc_id
        """
        try:
            r = list(snapshot.execute_sql(
                gql_param_unnest,
                params={"authorized_doc_ids": [sample_doc_id, "non_existent_id"]},
                param_types={"authorized_doc_ids": spanner.param_types.Array(spanner.param_types.STRING)}
            ))
            print(f"  [3d. Parameterized UNNEST filter (RBAC pattern)] Retrieved {len(r)} authorized doc(s): {[row[0] for row in r]}.")
            if not r or r[0][0] != sample_doc_id:
                t3_failures.append("UNNEST array parameter filter returned unexpected result.")
        except Exception as e:
            print(f"  [3d. Parameterized UNNEST filter] ERROR: {e}")
            t3_failures.append(f"UNNEST param: {e}")

        test_results["test_3"] = "PASS" if not t3_failures else f"FAIL ({len(t3_failures)} errors)"

        # ====================================================================
        # TEST 4: Verify graph results map correctly to underlying table keys
        # ====================================================================
        print("\n--- Test 4: Key & Property Mapping Verification Against SQL ---")
        t4_failures = []

        # Compare Documents: GQL vs SQL
        gql_docs = list(snapshot.execute_sql("""
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (d:Document) 
        RETURN d.DocumentId AS doc_id, d.FileName AS file_name, d.MimeType AS mime_type, d.RepoId AS repo_id 
        ORDER BY d.DocumentId
        """))

        sql_docs = list(snapshot.execute_sql("""
        SELECT DocumentId, FileName, MimeType, RepoId 
        FROM Documents 
        ORDER BY DocumentId
        """))

        if gql_docs == sql_docs and len(gql_docs) == 5:
            print(f"  [Documents Key/Property Mapping] Exact match for all {len(gql_docs)} records.")
        else:
            print(f"  [Documents Key/Property Mapping] MISMATCH! GQL: {len(gql_docs)}, SQL: {len(sql_docs)}")
            t4_failures.append("Documents property mapping mismatch.")

        # Compare EvidenceChunks: GQL vs SQL (first 10 records)
        gql_chunks = list(snapshot.execute_sql("""
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (c:Chunk) 
        RETURN c.ChunkId AS chunk_id, c.DocumentId AS doc_id, c.Modality AS modality, c.FileId AS file_id 
        ORDER BY c.ChunkId 
        LIMIT 10
        """))

        sql_chunks = list(snapshot.execute_sql("""
        SELECT ChunkId, DocumentId, Modality, FileId 
        FROM EvidenceChunks 
        ORDER BY ChunkId 
        LIMIT 10
        """))

        if gql_chunks == sql_chunks and len(gql_chunks) == 10:
            print(f"  [EvidenceChunks Key/Property Mapping] Exact match for sample {len(gql_chunks)} records.")
        else:
            print(f"  [EvidenceChunks Key/Property Mapping] MISMATCH!")
            t4_failures.append("EvidenceChunks property mapping mismatch.")

        # Compare VisualAssets: GQL vs SQL
        gql_visuals = list(snapshot.execute_sql("""
        GRAPH EnterpriseKnowledgeGraph 
        MATCH (v:VisualAsset) 
        RETURN v.VisualId AS visual_id, v.DocumentId AS doc_id, v.AssetType AS asset_type 
        ORDER BY v.VisualId 
        LIMIT 5
        """))

        sql_visuals = list(snapshot.execute_sql("""
        SELECT VisualId, DocumentId, AssetType 
        FROM VisualAssets 
        ORDER BY VisualId 
        LIMIT 5
        """))

        if gql_visuals == sql_visuals and len(gql_visuals) == 5:
            print(f"  [VisualAssets Key/Property Mapping] Exact match for sample {len(gql_visuals)} records.")
        else:
            print(f"  [VisualAssets Key/Property Mapping] MISMATCH!")
            t4_failures.append("VisualAssets property mapping mismatch.")

        test_results["test_4"] = "PASS" if not t4_failures else f"FAIL ({len(t4_failures)} errors)"

        # ====================================================================
        # TEST 5: Vector index remains READ_WRITE and row counts intact
        # ====================================================================
        print("\n--- Test 5: Vector Index State & Database Row Counts ---")
        t5_failures = []

        idx_res = list(snapshot.execute_sql("""
        SELECT index_name, table_name, index_state 
        FROM information_schema.indexes 
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """))
        if idx_res and idx_res[0][2] == "READ_WRITE":
            print(f"  [Vector Index] EvidenceChunkVectorIndex confirmed in state: {idx_res[0][2]}.")
        else:
            state = idx_res[0][2] if idx_res else "NOT FOUND"
            print(f"  [Vector Index] ERROR: Expected READ_WRITE, found {state}.")
            t5_failures.append(f"Vector index state: {state}")

        # Check all 25 tables
        row_count_mismatches = []
        for tbl, exp in EXPECTED_ROW_COUNTS.items():
            cnt = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))[0][0]
            if cnt != exp:
                row_count_mismatches.append(f"{tbl}: expected {exp}, got {cnt}")

        if not row_count_mismatches:
            print("  [Row Counts] All 25 tables verified intact (426 populated records, 0 in new tables).")
        else:
            print(f"  [Row Counts] ERROR: Mismatches detected: {row_count_mismatches}")
            t5_failures.append(f"Row count mismatches: {row_count_mismatches}")

        test_results["test_5"] = "PASS" if not t5_failures else f"FAIL ({len(t5_failures)} errors)"

    print("\n" + "=" * 60)
    print("PHASE 2C.4 PROPERTY GRAPH VALIDATION SUMMARY:")
    for k, v in test_results.items():
        print(f"  {k}: {v}")
    print("=" * 60)

    if any(v != "PASS" for v in test_results.values()):
        sys.exit(1)

if __name__ == "__main__":
    main()
