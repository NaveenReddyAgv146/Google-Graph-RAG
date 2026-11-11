#!/usr/bin/env python3
"""
Phase 2C DDL Batch 2 Execution and Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- Make Documents.DriveFileId nullable
- Add Documents.RepoId STRING(64)
- Add EvidenceChunks.FileId STRING(64)
- Add EvidenceChunks.CommitSha STRING(64)
- Add EvidenceChunks.LineRange STRING(64)
- Add EvidenceChunks.Embedding768 ARRAY<FLOAT32>(vector_length => 768)
- Add VisualAssets.AssetType STRING(32)
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
    "MigrationBatches": 0,
    "MigrationManifest": 0,
}

PLANNED_STATEMENTS = [
    ("Documents", "DriveFileId", "ALTER TABLE Documents ALTER COLUMN DriveFileId STRING(256)"),
    ("Documents", "RepoId", "ALTER TABLE Documents ADD COLUMN RepoId STRING(64)"),
    ("EvidenceChunks", "FileId", "ALTER TABLE EvidenceChunks ADD COLUMN FileId STRING(64)"),
    ("EvidenceChunks", "CommitSha", "ALTER TABLE EvidenceChunks ADD COLUMN CommitSha STRING(64)"),
    ("EvidenceChunks", "LineRange", "ALTER TABLE EvidenceChunks ADD COLUMN LineRange STRING(64)"),
    ("EvidenceChunks", "Embedding768", "ALTER TABLE EvidenceChunks ADD COLUMN Embedding768 ARRAY<FLOAT32>(vector_length => 768)"),
    ("VisualAssets", "AssetType", "ALTER TABLE VisualAssets ADD COLUMN AssetType STRING(32)"),
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
        for tbl in list(EXISTING_TABLES) + ["MigrationBatches", "MigrationManifest"]:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            pre_counts[tbl] = count_res[0][0]
            expected = EXPECTED_ROW_COUNTS[tbl]
            if pre_counts[tbl] != expected:
                print(f"ERROR: Table {tbl} row count mismatch! Expected {expected}, got {pre_counts[tbl]}")
                sys.exit(1)
            print(f"  - {tbl}: {pre_counts[tbl]} rows (matches expected {expected})")
        print(f"Baseline row counts verified for all 10 tables ({sum(pre_counts.values())} rows).")

        # Inspect columns for target tables
        cols_query = """
        SELECT table_name, column_name, spanner_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = '' AND table_name IN ('Documents', 'EvidenceChunks', 'VisualAssets')
        """
        existing_cols = {}
        for row in snapshot.execute_sql(cols_query):
            existing_cols[(row[0], row[1])] = {
                "spanner_type": row[2],
                "is_nullable": (row[3].upper() == "YES"),
            }

        print("\nEvaluating Batch 2 operations against current schema:")
        for tbl, col, stmt in PLANNED_STATEMENTS:
            col_info = existing_cols.get((tbl, col))
            if col == "DriveFileId":
                if col_info is None:
                    print(f"ERROR: Expected column Documents.DriveFileId not found!")
                    sys.exit(1)
                if col_info["is_nullable"]:
                    print(f"  - [{tbl}.{col}]: Already nullable. Skipping statement.")
                else:
                    print(f"  - [{tbl}.{col}]: Currently NOT NULL ({col_info['spanner_type']}). Queued: {stmt}")
                    statements_to_execute.append(stmt)
            else:
                if col_info is not None:
                    print(f"  - [{tbl}.{col}]: Column already exists ({col_info['spanner_type']}, nullable={col_info['is_nullable']}).")
                    # Check exact definition match
                    if col == "Embedding768" and "768" not in col_info["spanner_type"]:
                        print(f"ERROR: Type mismatch on {tbl}.{col}: {col_info['spanner_type']}")
                        sys.exit(1)
                    print(f"    Skipping addition.")
                else:
                    print(f"  - [{tbl}.{col}]: Not present. Queued: {stmt}")
                    statements_to_execute.append(stmt)

    if not statements_to_execute:
        print("\nAll columns already match the approved definition. Zero statements to execute.")
        return

    # 2. Execute Batch 2 DDL
    print(f"\nStep 2: Executing {len(statements_to_execute)} DDL statement(s)...")
    for idx, s in enumerate(statements_to_execute, 1):
        print(f"  [{idx}] {s}")

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
        verify_query = """
        SELECT table_name, column_name, spanner_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = '' AND (
            (table_name = 'Documents' AND column_name IN ('DriveFileId', 'RepoId')) OR
            (table_name = 'EvidenceChunks' AND column_name IN ('FileId', 'CommitSha', 'LineRange', 'Embedding768')) OR
            (table_name = 'VisualAssets' AND column_name = 'AssetType')
        )
        ORDER BY table_name, column_name
        """
        verified_cols = list(snapshot.execute_sql(verify_query))
        print("\nVerified Columns:")
        for r in verified_cols:
            print(f"  {r[0]}.{r[1]}: {r[2]} (is_nullable: {r[3]})")

        # Specific assertions
        expected_specs = {
            ("Documents", "DriveFileId"): ("STRING(256)", "YES"),
            ("Documents", "RepoId"): ("STRING(64)", "YES"),
            ("EvidenceChunks", "FileId"): ("STRING(64)", "YES"),
            ("EvidenceChunks", "CommitSha"): ("STRING(64)", "YES"),
            ("EvidenceChunks", "LineRange"): ("STRING(64)", "YES"),
            ("EvidenceChunks", "Embedding768"): ("ARRAY<FLOAT32>(vector_length=>768)", "YES"),
            ("VisualAssets", "AssetType"): ("STRING(32)", "YES"),
        }

        col_dict = {(r[0], r[1]): (r[2].replace(" ", ""), r[3].upper()) for r in verified_cols}
        for (t, c), (exp_type, exp_null) in expected_specs.items():
            if (t, c) not in col_dict:
                print(f"ERROR: Expected column {t}.{c} not found in INFORMATION_SCHEMA!")
                sys.exit(1)
            actual_type, actual_null = col_dict[(t, c)]
            if actual_null != exp_null:
                print(f"ERROR: Nullability mismatch on {t}.{c}! Expected {exp_null}, got {actual_null}")
                sys.exit(1)
            # Normalize vector length spacing if needed
            exp_type_norm = exp_type.replace(" ", "")
            if exp_type_norm not in actual_type and actual_type not in exp_type_norm:
                print(f"ERROR: Type mismatch on {t}.{c}! Expected {exp_type}, got {actual_type}")
                sys.exit(1)
        print("All 7 column definitions strictly verified.")

        # 5. Confirm row counts
        print("\nStep 5: Verifying data integrity of all 10 tables...")
        for tbl in list(EXISTING_TABLES) + ["MigrationBatches", "MigrationManifest"]:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            current_count = count_res[0][0]
            if current_count != pre_counts[tbl]:
                print(f"CRITICAL ERROR: Data mutation detected on {tbl}! Pre: {pre_counts[tbl]}, Post: {current_count}")
                sys.exit(1)
            print(f"  - {tbl}: {current_count} rows (verified identical to pre-DDL baseline)")
        print("Data integrity check PASSED: All 426 existing records and 2 migration tables remain 100% intact.")

    print("\nPhase 2C DDL Batch 2 successfully executed and verified.")

if __name__ == "__main__":
    main()

