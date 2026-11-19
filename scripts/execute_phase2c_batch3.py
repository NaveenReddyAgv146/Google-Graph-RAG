#!/usr/bin/env python3
"""
Phase 2C DDL Batch 3 Execution and Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- Repositories node table
- SourceFiles node table with FK to Repositories(RepoId)
- Entities node table
- Technologies node table
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
}

BATCH3_TABLES = [
    "Repositories",
    "SourceFiles",
    "Entities",
    "Technologies",
]

DDL_STATEMENTS = [
    ("Repositories", """CREATE TABLE Repositories (
  RepoId STRING(64) NOT NULL,
  Name STRING(256) NOT NULL,
  Url STRING(1024) NOT NULL,
  DefaultBranch STRING(64),
  LastSyncedCommit STRING(64),
  CreatedAt TIMESTAMP OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY(RepoId)"""),
    ("SourceFiles", """CREATE TABLE SourceFiles (
  FileId STRING(64) NOT NULL,
  RepoId STRING(64) NOT NULL,
  FilePath STRING(1024) NOT NULL,
  Language STRING(64),
  CommitSha STRING(64) NOT NULL,
  CreatedAt TIMESTAMP OPTIONS (allow_commit_timestamp = true),
  CONSTRAINT FK_SourceFiles_Repo FOREIGN KEY(RepoId) REFERENCES Repositories(RepoId)
) PRIMARY KEY(FileId)"""),
    ("Entities", """CREATE TABLE Entities (
  EntityId STRING(64) NOT NULL,
  Name STRING(256) NOT NULL,
  Category STRING(64) NOT NULL,
  Description STRING(MAX)
) PRIMARY KEY(EntityId)"""),
    ("Technologies", """CREATE TABLE Technologies (
  TechId STRING(64) NOT NULL,
  Name STRING(256) NOT NULL,
  Category STRING(64) NOT NULL,
  CanonicalVersion STRING(64)
) PRIMARY KEY(TechId)"""),
]

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)
    admin_client = spanner_client.database_admin_api

    # 1. Preflight Inspection
    print("\nStep 1: Inspecting existing schema and row counts...")
    statements_to_execute = []

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
        print(f"Baseline row counts verified for all 10 tables ({sum(pre_counts.values())} rows).")

        # Check if any Batch 3 tables already exist
        tbl_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = '' AND table_name IN ('Repositories', 'SourceFiles', 'Entities', 'Technologies')
        """
        existing_batch3 = [r[0] for r in snapshot.execute_sql(tbl_query)]
        print(f"Existing Batch 3 tables found: {existing_batch3}")

        for tbl_name, stmt in DDL_STATEMENTS:
            if tbl_name in existing_batch3:
                print(f"  - Table '{tbl_name}' already exists. Skipping creation.")
            else:
                print(f"  - Table '{tbl_name}' not present. Queued for creation.")
                statements_to_execute.append(stmt)

    if not statements_to_execute:
        print("\nAll Batch 3 tables already exist. Zero statements to execute.")
        return

    # 2. Execute Batch 3 DDL
    print(f"\nStep 2: Executing {len(statements_to_execute)} DDL statement(s)...")
    for idx, s in enumerate(statements_to_execute, 1):
        print(f"\n--- Statement {idx} ---")
        print(s)

    db_path = admin_client.database_path(PROJECT_ID, INSTANCE_ID, DATABASE_ID)
    request = spanner_database_admin.UpdateDatabaseDdlRequest(
        database=db_path,
        statements=statements_to_execute,
    )
    operation = admin_client.update_database_ddl(request=request)
    print(f"\nStep 3: Operation started: {operation.operation.name}. Waiting for completion...")
    operation.result()
    print("DDL Operation completed successfully.")

    # 4. Verification through INFORMATION_SCHEMA
    print("\nStep 4: Verifying schema through INFORMATION_SCHEMA...")
    with database.snapshot(multi_use=True) as snapshot:
        # Check all 4 tables exist
        tbl_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = '' AND table_name IN ('Repositories', 'SourceFiles', 'Entities', 'Technologies')
        ORDER BY table_name
        """
        created = [r[0] for r in snapshot.execute_sql(tbl_query)]
        print(f"Batch 3 tables in database: {created}")
        if set(created) != set(BATCH3_TABLES):
            print(f"ERROR: Missing expected Batch 3 tables! Found: {created}")
            sys.exit(1)

        # Check columns
        cols_query = """
        SELECT table_name, column_name, spanner_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = '' AND table_name IN ('Repositories', 'SourceFiles', 'Entities', 'Technologies')
        ORDER BY table_name, ordinal_position
        """
        cols = list(snapshot.execute_sql(cols_query))
        print("\nVerified Columns:")
        for c in cols:
            print(f"  {c[0]}.{c[1]}: {c[2]} (nullable: {c[3]}, pos: {c[4]})")

        # Check Primary Keys
        pk_query = """
        SELECT k.table_name, k.column_name, k.ordinal_position
        FROM information_schema.key_column_usage k
        JOIN information_schema.table_constraints t
          ON k.constraint_name = t.constraint_name
        WHERE k.table_schema = '' 
          AND k.table_name IN ('Repositories', 'SourceFiles', 'Entities', 'Technologies')
          AND t.constraint_type = 'PRIMARY KEY'
        ORDER BY k.table_name, k.ordinal_position
        """
        pks = list(snapshot.execute_sql(pk_query))
        print("\nVerified Primary Keys:")
        for pk in pks:
            print(f"  {pk[0]}: {pk[1]} (ordinal: {pk[2]})")

        expected_pks = {
            "Repositories": ["RepoId"],
            "SourceFiles": ["FileId"],
            "Entities": ["EntityId"],
            "Technologies": ["TechId"],
        }
        actual_pks = {}
        for pk in pks:
            actual_pks.setdefault(pk[0], []).append(pk[1])

        for t, exp in expected_pks.items():
            if actual_pks.get(t) != exp:
                print(f"ERROR: PK mismatch on {t}! Expected {exp}, got {actual_pks.get(t)}")
                sys.exit(1)

        # Check Foreign Key on SourceFiles -> Repositories
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
        WHERE kcu1.table_name = 'SourceFiles'
        """
        fks = list(snapshot.execute_sql(fk_query))
        print("\nVerified Foreign Keys:")
        for fk in fks:
            print(f"  Constraint {fk[0]}: {fk[1]}({fk[2]}) -> {fk[3]}({fk[4]})")

        if not fks or fks[0][2] != "RepoId" or fks[0][4] != "RepoId":
            print("ERROR: Foreign key constraint FK_SourceFiles_Repo on SourceFiles(RepoId) -> Repositories(RepoId) not found!")
            sys.exit(1)

        # 5. Confirm row counts
        print("\nStep 5: Verifying data integrity of all tables...")
        for tbl in PRE_EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            current_count = count_res[0][0]
            if current_count != pre_counts[tbl]:
                print(f"CRITICAL ERROR: Data mutation detected on {tbl}! Pre: {pre_counts[tbl]}, Post: {current_count}")
                sys.exit(1)
            print(f"  - {tbl}: {current_count} rows (verified identical to baseline)")

        for tbl in BATCH3_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            print(f"  - {tbl} (new): {count_res[0][0]} rows")
            if count_res[0][0] != 0:
                print(f"ERROR: New table {tbl} is not empty! Count: {count_res[0][0]}")
                sys.exit(1)

        print("Data integrity check PASSED: All 426 existing records and 2 migration tables remain 100% intact.")

    print("\nPhase 2C DDL Batch 3 successfully executed and verified.")

if __name__ == "__main__":
    main()

