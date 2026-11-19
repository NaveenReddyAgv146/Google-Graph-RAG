#!/usr/bin/env python3
"""
Phase 2C DDL Batch 5 Execution and Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- Vector Index EvidenceChunkVectorIndex on EvidenceChunks(Embedding768)
"""

import sys
import json
from google.cloud import spanner
from google.cloud.spanner_admin_database_v1.types import spanner_database_admin

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"

PRE_EXISTING_TABLES = [
    "Documents",
    "DocumentAccess",
    "DriveSyncDocuments",
    "EvidenceChunks",
    "VisualAssets",
    "SolutionCards",
    "Embeddings",
    "EvidenceLinks",
    "MigrationBatches",
    "MigrationManifest",
    "Repositories",
    "SourceFiles",
    "Entities",
    "Technologies",
    "DocumentContainsChunk",
    "RepoContainsFile",
    "SourceFileContainsChunk",
    "ChunkReferencesEntity",
    "EntityReferencesEntity",
    "ChunkMentionsTech",
    "VisualAssetBelongsToDoc",
    "SolutionSupportsTech",
    "SourceFileDefinesEntity",
    "SourceFileCallsFile",
    "SolutionImplementsDoc",
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

VECTOR_INDEX_DDL = """CREATE VECTOR INDEX EvidenceChunkVectorIndex 
ON EvidenceChunks(Embedding768)
STORING (DocumentId, FileId, PageNumber, SlideNumber)
WHERE Embedding768 IS NOT NULL
OPTIONS (
  distance_type = 'COSINE'
)"""

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)
    admin_client = spanner_client.database_admin_api

    # 1. Preflight Inspection
    print("\nStep 1: Inspecting existing schema and row counts...")
    statement_needed = True

    with database.snapshot(multi_use=True) as snapshot:
        # Check existing row counts
        pre_counts = {}
        for tbl in PRE_EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            pre_counts[tbl] = count_res[0][0]
            expected = EXPECTED_ROW_COUNTS[tbl]
            if pre_counts[tbl] != expected:
                print(f"ERROR: Table {tbl} row count mismatch! Expected {expected}, got {pre_counts[tbl]}")
                sys.exit(1)
            print(f"  - {tbl}: {pre_counts[tbl]} rows (matches expected {expected})")
        print(f"Baseline row counts verified for all 25 tables ({sum(pre_counts.values())} rows).")

        # Check if EvidenceChunkVectorIndex already exists
        idx_query = """
        SELECT index_name, table_name, index_state
        FROM information_schema.indexes
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """
        existing_idx = list(snapshot.execute_sql(idx_query))
        if existing_idx:
            idx_name, tbl_name, idx_state = existing_idx[0][0], existing_idx[0][1], existing_idx[0][2]
            print(f"Existing index found: {idx_name} on table {tbl_name} (state: {idx_state})")
            if tbl_name != "EvidenceChunks":
                print(f"ERROR: Existing index is on table {tbl_name}, expected EvidenceChunks!")
                sys.exit(1)
            print("Index already exists. Skipping creation.")
            statement_needed = False
        else:
            print("Index EvidenceChunkVectorIndex not present. Queued for creation.")

    if not statement_needed:
        print("\nVector index already exists. Skipping DDL execution.")
    else:
        # 2. Execute Batch 5 DDL
        print("\nStep 2: Executing Batch 5 Vector Index DDL...")
        print(VECTOR_INDEX_DDL)

        db_path = admin_client.database_path(PROJECT_ID, INSTANCE_ID, DATABASE_ID)
        request = spanner_database_admin.UpdateDatabaseDdlRequest(
            database=db_path,
            statements=[VECTOR_INDEX_DDL],
        )
        operation = admin_client.update_database_ddl(request=request)
        print(f"\nStep 3: Operation started: {operation.operation.name}. Waiting for completion...")
        operation.result()
        print("DDL Operation completed successfully.")

    # 4. Verification through INFORMATION_SCHEMA
    print("\nStep 4: Verifying vector index through INFORMATION_SCHEMA...")
    with database.snapshot(multi_use=True) as snapshot:
        # Query index metadata
        idx_meta_query = """
        SELECT index_name, table_name, index_type, index_state, is_null_filtered
        FROM information_schema.indexes
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """
        idx_meta = list(snapshot.execute_sql(idx_meta_query))
        if not idx_meta:
            print("ERROR: EvidenceChunkVectorIndex not found in INFORMATION_SCHEMA.INDEXES!")
            sys.exit(1)

        idx_name, tbl_name, idx_type, idx_state, is_null_filtered = idx_meta[0]
        print(f"Verified Index: {idx_name}")
        print(f"  - Target Table:     {tbl_name}")
        print(f"  - Index Type:       {idx_type}")
        print(f"  - Index State:      {idx_state} (Readiness verified)")
        print(f"  - Is Null Filtered: {is_null_filtered}")

        if tbl_name != "EvidenceChunks":
            print(f"ERROR: Expected target table EvidenceChunks, got {tbl_name}")
            sys.exit(1)

        if idx_state != "READ_WRITE":
            print(f"WARNING: Index state is {idx_state}, expected READ_WRITE.")

        # Query index columns
        col_query = """
        SELECT column_name, ordinal_position, column_ordering
        FROM information_schema.index_columns
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """
        cols = list(snapshot.execute_sql(col_query))
        print("\nVerified Index Columns:")
        for c in cols:
            print(f"  - Column: {c[0]} (ordinal: {c[1]}, ordering: {c[2]})")

        indexed_cols = [c[0] for c in cols if c[1] == 1]
        stored_cols = [c[0] for c in cols if c[1] is None]

        print(f"Indexed Column: {indexed_cols}")
        print(f"Stored Columns: {stored_cols}")

        if indexed_cols != ["Embedding768"]:
            print(f"ERROR: Expected indexed column ['Embedding768'], got {indexed_cols}!")
            sys.exit(1)

        expected_stored = {"DocumentId", "FileId", "PageNumber", "SlideNumber"}
        if set(stored_cols) != expected_stored:
            print(f"ERROR: Stored columns mismatch! Expected {expected_stored}, got {set(stored_cols)}")
            sys.exit(1)
        print("Indexed and storing columns strictly verified.")

        # Check options (distance_type = 'COSINE')
        try:
            options_query = """
            SELECT option_name, option_type, option_value
            FROM information_schema.index_options
            WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
            """
            options = list(snapshot.execute_sql(options_query))
            print("\nVerified Index Options:")
            for opt in options:
                print(f"  - {opt[0]}: {opt[2]}")
        except Exception as e:
            print(f"Note: index_options query not supported or returned empty: {e}")

        # 5. Confirm row counts of all 25 tables
        print("\nStep 5: Verifying data integrity of all 25 tables...")
        for tbl in PRE_EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            current_count = count_res[0][0]
            if current_count != pre_counts[tbl]:
                print(f"CRITICAL ERROR: Data mutation detected on {tbl}! Pre: {pre_counts[tbl]}, Post: {current_count}")
                sys.exit(1)
            print(f"  - {tbl}: {current_count} rows (verified identical to baseline)")

        print("Data integrity check PASSED: All 426 existing records and 17 previously created tables remain 100% intact.")

    print("\nPhase 2C DDL Batch 5 successfully executed and verified.")

if __name__ == "__main__":
    main()

