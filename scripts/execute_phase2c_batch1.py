#!/usr/bin/env python3
"""
Phase 2C DDL Batch 1 Execution and Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- MigrationBatches table
- MigrationManifest table with FK constraint to MigrationBatches
"""

import sys
import json
from google.cloud import spanner
from google.cloud.spanner_admin_database_v1.types import spanner_database_admin

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"

EXISTING_TABLES = [
    "Documents",
    "DocumentAccess",
    "DriveSyncDocuments",
    "EvidenceChunks",
    "VisualAssets",
    "SolutionCards",
    "Embeddings",
    "EvidenceLinks",
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
}

DDL_STATEMENTS = [
    """CREATE TABLE MigrationBatches (
  BatchId STRING(64) NOT NULL,
  State STRING(32) NOT NULL,
  TotalInsertedEdges INT64 NOT NULL,
  CreatedAt TIMESTAMP OPTIONS (allow_commit_timestamp = true),
  AcceptedAt TIMESTAMP OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY(BatchId)""",
    """CREATE TABLE MigrationManifest (
  BatchId STRING(64) NOT NULL,
  TableName STRING(128) NOT NULL,
  RecordKey STRING(512) NOT NULL,
  InsertedAt TIMESTAMP OPTIONS (allow_commit_timestamp = true),
  CONSTRAINT FK_Manifest_Batch FOREIGN KEY(BatchId) REFERENCES MigrationBatches(BatchId)
) PRIMARY KEY(BatchId, TableName, RecordKey)""",
]

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)
    admin_client = spanner_client.database_admin_api

    # 1. Preflight check: verify MigrationBatches and MigrationManifest do not already exist
    print("Step 1: Checking if MigrationBatches or MigrationManifest already exist...")
    with database.snapshot(multi_use=True) as snapshot:
        existing_tbl_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = '' AND table_name IN ('MigrationBatches', 'MigrationManifest')
        """
        results = list(snapshot.execute_sql(existing_tbl_query))
        if results:
            found = [r[0] for r in results]
            print(f"ERROR: Tables already exist: {found}")
            sys.exit(1)
        print("Preflight check passed: Neither MigrationBatches nor MigrationManifest exists.")

        # Check existing 8 tables and row counts
        print("Checking pre-DDL baseline row counts of existing 8 tables...")
        pre_counts = {}
        for tbl in EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            pre_counts[tbl] = count_res[0][0]
            expected = EXPECTED_ROW_COUNTS[tbl]
            if pre_counts[tbl] != expected:
                print(f"ERROR: Table {tbl} count mismatch! Expected {expected}, got {pre_counts[tbl]}")
                sys.exit(1)
            print(f"  - {tbl}: {pre_counts[tbl]} rows (matches expected {expected})")
        print(f"Pre-DDL total records verified: {sum(pre_counts.values())} rows.")

    # 2. Execute DDL Batch 1
    print("\nStep 2: Executing DDL Batch 1...")
    db_path = admin_client.database_path(PROJECT_ID, INSTANCE_ID, DATABASE_ID)
    
    for idx, stmt in enumerate(DDL_STATEMENTS, 1):
        print(f"\n--- Statement {idx} ---")
        print(stmt)

    request = spanner_database_admin.UpdateDatabaseDdlRequest(
        database=db_path,
        statements=DDL_STATEMENTS,
    )
    operation = admin_client.update_database_ddl(request=request)
    print(f"\nStep 3: Operation started: {operation.operation.name}. Waiting for completion...")
    operation.result()
    print("DDL Operation completed successfully.")

    # 4. Verification through INFORMATION_SCHEMA
    print("\nStep 4: Verifying schema through INFORMATION_SCHEMA...")
    with database.snapshot(multi_use=True) as snapshot:
        # Tables check
        tbl_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = '' AND table_name IN ('MigrationBatches', 'MigrationManifest')
        ORDER BY table_name
        """
        created_tables = [r[0] for r in snapshot.execute_sql(tbl_query)]
        print(f"Created tables in database: {created_tables}")
        if set(created_tables) != {"MigrationBatches", "MigrationManifest"}:
            print(f"ERROR: Expected tables MigrationBatches and MigrationManifest, found {created_tables}")
            sys.exit(1)

        # Columns check
        cols_query = """
        SELECT table_name, column_name, spanner_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = '' AND table_name IN ('MigrationBatches', 'MigrationManifest')
        ORDER BY table_name, ordinal_position
        """
        cols = list(snapshot.execute_sql(cols_query))
        print("\nVerified Columns:")
        for c in cols:
            print(f"  {c[0]}.{c[1]}: {c[2]} (nullable: {c[3]}, pos: {c[4]})")

        # Primary Keys check
        pk_query = """
        SELECT k.table_name, k.column_name, k.ordinal_position
        FROM information_schema.key_column_usage k
        JOIN information_schema.table_constraints t
          ON k.constraint_name = t.constraint_name
        WHERE k.table_schema = '' 
          AND k.table_name IN ('MigrationBatches', 'MigrationManifest')
          AND t.constraint_type = 'PRIMARY KEY'
        ORDER BY k.table_name, k.ordinal_position
        """
        pks = list(snapshot.execute_sql(pk_query))
        print("\nVerified Primary Keys:")
        for pk in pks:
            print(f"  {pk[0]}: {pk[1]} (ordinal: {pk[2]})")

        # Foreign Key check
        fk_query = """
        SELECT 
            rc.constraint_name,
            kcu1.table_name AS child_table,
            kcu1.column_name AS child_column,
            kcu2.table_name AS parent_table,
            kcu2.column_name AS parent_column
        FROM information_schema.referential_constraints rc
        JOIN information_schema.key_column_usage kcu1
          ON rc.constraint_name = kcu1.constraint_name
        JOIN information_schema.key_column_usage kcu2
          ON rc.unique_constraint_name = kcu2.constraint_name
        WHERE kcu1.table_name = 'MigrationManifest'
        """
        fks = list(snapshot.execute_sql(fk_query))
        print("\nVerified Foreign Keys:")
        for fk in fks:
            print(f"  Constraint: {fk[0]}: {fk[1]}({fk[2]}) -> {fk[3]}({fk[4]})")

        if not fks:
            print("ERROR: Foreign key constraint FK_Manifest_Batch not found on MigrationManifest!")
            sys.exit(1)

        # 5. Confirm existing 8 tables and records remain unchanged
        print("\nStep 5: Verifying data integrity of existing 8 tables...")
        post_counts = {}
        for tbl in EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            post_counts[tbl] = count_res[0][0]
            if post_counts[tbl] != pre_counts[tbl]:
                print(f"CRITICAL ERROR: Data mutation detected on {tbl}! Pre: {pre_counts[tbl]}, Post: {post_counts[tbl]}")
                sys.exit(1)
            print(f"  - {tbl}: {post_counts[tbl]} rows (verified identical to pre-DDL baseline)")
        print(f"All 8 existing tables verified intact. Total records: {sum(post_counts.values())} rows.")

    print("\nPhase 2C DDL Batch 1 successfully executed and verified.")

if __name__ == "__main__":
    main()
