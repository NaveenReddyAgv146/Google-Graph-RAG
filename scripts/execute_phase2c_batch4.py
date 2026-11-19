#!/usr/bin/env python3
"""
Phase 2C DDL Batch 4 Execution and Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- 11 Physical Edge Tables with 22 Foreign Key Constraints
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
}

EDGE_TABLES = [
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

DDL_STATEMENTS = [
    ("DocumentContainsChunk", """CREATE TABLE DocumentContainsChunk (
  DocumentId STRING(256) NOT NULL,
  ChunkId STRING(64) NOT NULL,
  CONSTRAINT FK_DCC_Document FOREIGN KEY(DocumentId) REFERENCES Documents(DocumentId),
  CONSTRAINT FK_DCC_Chunk FOREIGN KEY(ChunkId) REFERENCES EvidenceChunks(ChunkId)
) PRIMARY KEY(DocumentId, ChunkId)"""),

    ("RepoContainsFile", """CREATE TABLE RepoContainsFile (
  RepoId STRING(64) NOT NULL,
  FileId STRING(64) NOT NULL,
  CONSTRAINT FK_RCF_Repo FOREIGN KEY(RepoId) REFERENCES Repositories(RepoId),
  CONSTRAINT FK_RCF_File FOREIGN KEY(FileId) REFERENCES SourceFiles(FileId)
) PRIMARY KEY(RepoId, FileId)"""),

    ("SourceFileContainsChunk", """CREATE TABLE SourceFileContainsChunk (
  FileId STRING(64) NOT NULL,
  ChunkId STRING(64) NOT NULL,
  CONSTRAINT FK_SFCC_File FOREIGN KEY(FileId) REFERENCES SourceFiles(FileId),
  CONSTRAINT FK_SFCC_Chunk FOREIGN KEY(ChunkId) REFERENCES EvidenceChunks(ChunkId)
) PRIMARY KEY(FileId, ChunkId)"""),

    ("ChunkReferencesEntity", """CREATE TABLE ChunkReferencesEntity (
  ChunkId STRING(64) NOT NULL,
  EntityId STRING(64) NOT NULL,
  CONSTRAINT FK_CRE_Chunk FOREIGN KEY(ChunkId) REFERENCES EvidenceChunks(ChunkId),
  CONSTRAINT FK_CRE_Entity FOREIGN KEY(EntityId) REFERENCES Entities(EntityId)
) PRIMARY KEY(ChunkId, EntityId)"""),

    ("EntityReferencesEntity", """CREATE TABLE EntityReferencesEntity (
  SourceEntityId STRING(64) NOT NULL,
  TargetEntityId STRING(64) NOT NULL,
  CONSTRAINT FK_ERE_Source FOREIGN KEY(SourceEntityId) REFERENCES Entities(EntityId),
  CONSTRAINT FK_ERE_Target FOREIGN KEY(TargetEntityId) REFERENCES Entities(EntityId)
) PRIMARY KEY(SourceEntityId, TargetEntityId)"""),

    ("ChunkMentionsTech", """CREATE TABLE ChunkMentionsTech (
  ChunkId STRING(64) NOT NULL,
  TechId STRING(64) NOT NULL,
  CONSTRAINT FK_CMT_Chunk FOREIGN KEY(ChunkId) REFERENCES EvidenceChunks(ChunkId),
  CONSTRAINT FK_CMT_Tech FOREIGN KEY(TechId) REFERENCES Technologies(TechId)
) PRIMARY KEY(ChunkId, TechId)"""),

    ("VisualAssetBelongsToDoc", """CREATE TABLE VisualAssetBelongsToDoc (
  VisualId STRING(64) NOT NULL,
  DocumentId STRING(256) NOT NULL,
  CONSTRAINT FK_VABD_Visual FOREIGN KEY(VisualId) REFERENCES VisualAssets(VisualId),
  CONSTRAINT FK_VABD_Document FOREIGN KEY(DocumentId) REFERENCES Documents(DocumentId)
) PRIMARY KEY(VisualId, DocumentId)"""),

    ("SolutionSupportsTech", """CREATE TABLE SolutionSupportsTech (
  CardId STRING(64) NOT NULL,
  TechId STRING(64) NOT NULL,
  CONSTRAINT FK_SST_Card FOREIGN KEY(CardId) REFERENCES SolutionCards(CardId),
  CONSTRAINT FK_SST_Tech FOREIGN KEY(TechId) REFERENCES Technologies(TechId)
) PRIMARY KEY(CardId, TechId)"""),

    ("SourceFileDefinesEntity", """CREATE TABLE SourceFileDefinesEntity (
  FileId STRING(64) NOT NULL,
  EntityId STRING(64) NOT NULL,
  CONSTRAINT FK_SFDE_File FOREIGN KEY(FileId) REFERENCES SourceFiles(FileId),
  CONSTRAINT FK_SFDE_Entity FOREIGN KEY(EntityId) REFERENCES Entities(EntityId)
) PRIMARY KEY(FileId, EntityId)"""),

    ("SourceFileCallsFile", """CREATE TABLE SourceFileCallsFile (
  CallerFileId STRING(64) NOT NULL,
  CalleeFileId STRING(64) NOT NULL,
  CONSTRAINT FK_SFCF_Caller FOREIGN KEY(CallerFileId) REFERENCES SourceFiles(FileId),
  CONSTRAINT FK_SFCF_Callee FOREIGN KEY(CalleeFileId) REFERENCES SourceFiles(FileId)
) PRIMARY KEY(CallerFileId, CalleeFileId)"""),

    ("SolutionImplementsDoc", """CREATE TABLE SolutionImplementsDoc (
  CardId STRING(64) NOT NULL,
  DocumentId STRING(256) NOT NULL,
  CONSTRAINT FK_SID_Card FOREIGN KEY(CardId) REFERENCES SolutionCards(CardId),
  CONSTRAINT FK_SID_Document FOREIGN KEY(DocumentId) REFERENCES Documents(DocumentId)
) PRIMARY KEY(CardId, DocumentId)"""),
]

EXPECTED_FKS = {
    "DocumentContainsChunk": [
        ("FK_DCC_Document", "DocumentId", "Documents", "DocumentId"),
        ("FK_DCC_Chunk", "ChunkId", "EvidenceChunks", "ChunkId"),
    ],
    "RepoContainsFile": [
        ("FK_RCF_Repo", "RepoId", "Repositories", "RepoId"),
        ("FK_RCF_File", "FileId", "SourceFiles", "FileId"),
    ],
    "SourceFileContainsChunk": [
        ("FK_SFCC_File", "FileId", "SourceFiles", "FileId"),
        ("FK_SFCC_Chunk", "ChunkId", "EvidenceChunks", "ChunkId"),
    ],
    "ChunkReferencesEntity": [
        ("FK_CRE_Chunk", "ChunkId", "EvidenceChunks", "ChunkId"),
        ("FK_CRE_Entity", "EntityId", "Entities", "EntityId"),
    ],
    "EntityReferencesEntity": [
        ("FK_ERE_Source", "SourceEntityId", "Entities", "EntityId"),
        ("FK_ERE_Target", "TargetEntityId", "Entities", "EntityId"),
    ],
    "ChunkMentionsTech": [
        ("FK_CMT_Chunk", "ChunkId", "EvidenceChunks", "ChunkId"),
        ("FK_CMT_Tech", "TechId", "Technologies", "TechId"),
    ],
    "VisualAssetBelongsToDoc": [
        ("FK_VABD_Visual", "VisualId", "VisualAssets", "VisualId"),
        ("FK_VABD_Document", "DocumentId", "Documents", "DocumentId"),
    ],
    "SolutionSupportsTech": [
        ("FK_SST_Card", "CardId", "SolutionCards", "CardId"),
        ("FK_SST_Tech", "TechId", "Technologies", "TechId"),
    ],
    "SourceFileDefinesEntity": [
        ("FK_SFDE_File", "FileId", "SourceFiles", "FileId"),
        ("FK_SFDE_Entity", "EntityId", "Entities", "EntityId"),
    ],
    "SourceFileCallsFile": [
        ("FK_SFCF_Caller", "CallerFileId", "SourceFiles", "FileId"),
        ("FK_SFCF_Callee", "CalleeFileId", "SourceFiles", "FileId"),
    ],
    "SolutionImplementsDoc": [
        ("FK_SID_Card", "CardId", "SolutionCards", "CardId"),
        ("FK_SID_Document", "DocumentId", "Documents", "DocumentId"),
    ],
}

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
        print(f"Baseline row counts verified for all 14 tables ({sum(pre_counts.values())} rows).")

        # Check existing edge tables
        tbl_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = '' AND table_name IN ({})
        """.format(", ".join(f"'{t}'" for t in EDGE_TABLES))
        existing_edges = [r[0] for r in snapshot.execute_sql(tbl_query)]
        print(f"Existing Batch 4 edge tables found: {existing_edges}")

        for tbl_name, stmt in DDL_STATEMENTS:
            if tbl_name in existing_edges:
                print(f"  - Table '{tbl_name}' already exists. Skipping creation.")
            else:
                print(f"  - Table '{tbl_name}' not present. Queued for creation.")
                statements_to_execute.append(stmt)

    if not statements_to_execute:
        print("\nAll 11 edge tables already exist. Zero statements to execute.")
        return

    # 2. Execute Batch 4 DDL
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
        # Check all 11 edge tables exist
        tbl_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = '' AND table_name IN ({})
        ORDER BY table_name
        """.format(", ".join(f"'{t}'" for t in EDGE_TABLES))
        created = [r[0] for r in snapshot.execute_sql(tbl_query)]
        print(f"Batch 4 edge tables in database: {len(created)}/11 found")
        if set(created) != set(EDGE_TABLES):
            missing = set(EDGE_TABLES) - set(created)
            print(f"ERROR: Missing expected Batch 4 edge tables: {missing}")
            sys.exit(1)

        # Check Primary Keys for all 11 tables
        pk_query = """
        SELECT k.table_name, k.column_name, k.ordinal_position
        FROM information_schema.key_column_usage k
        JOIN information_schema.table_constraints t
          ON k.constraint_name = t.constraint_name
        WHERE k.table_schema = '' 
          AND k.table_name IN ({})
          AND t.constraint_type = 'PRIMARY KEY'
        ORDER BY k.table_name, k.ordinal_position
        """.format(", ".join(f"'{t}'" for t in EDGE_TABLES))
        pks = list(snapshot.execute_sql(pk_query))
        actual_pks = {}
        for pk in pks:
            actual_pks.setdefault(pk[0], []).append(pk[1])

        print("\nVerified Primary Keys:")
        for tbl_name in EDGE_TABLES:
            keys = actual_pks.get(tbl_name, [])
            print(f"  {tbl_name}: PRIMARY KEY ({', '.join(keys)})")
            if len(keys) != 2:
                print(f"ERROR: Expected 2 composite PK columns on {tbl_name}, got {keys}")
                sys.exit(1)

        # Check Foreign Keys for all 11 tables (expecting 22 foreign keys)
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
        WHERE kcu1.table_name IN ({})
        ORDER BY kcu1.table_name, rc.constraint_name
        """.format(", ".join(f"'{t}'" for t in EDGE_TABLES))
        fks = list(snapshot.execute_sql(fk_query))

        print(f"\nVerified Foreign Key Constraints ({len(fks)} total):")
        fk_map = {}
        for fk in fks:
            c_name, c_tbl, c_col, p_tbl, p_col = fk[0], fk[1], fk[2], fk[3], fk[4]
            print(f"  [{c_tbl}] {c_name}: {c_col} -> {p_tbl}({p_col})")
            fk_map.setdefault(c_tbl, []).append((c_name, c_col, p_tbl, p_col))

        for tbl_name, expected_fk_list in EXPECTED_FKS.items():
            actual_fk_list = fk_map.get(tbl_name, [])
            if len(actual_fk_list) != 2:
                print(f"ERROR: Table {tbl_name} has {len(actual_fk_list)} FKs, expected 2!")
                sys.exit(1)
            for exp in expected_fk_list:
                match = [a for a in actual_fk_list if a[0] == exp[0] and a[1] == exp[1] and a[2] == exp[2] and a[3] == exp[3]]
                if not match:
                    print(f"ERROR: Expected FK {exp} not found on {tbl_name}!")
                    sys.exit(1)
        print("All 22 foreign key constraints strictly verified.")

        # 5. Confirm row counts of pre-existing tables and assert new edge tables are 0
        print("\nStep 5: Verifying data integrity of all tables...")
        for tbl in PRE_EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            current_count = count_res[0][0]
            if current_count != pre_counts[tbl]:
                print(f"CRITICAL ERROR: Data mutation detected on {tbl}! Pre: {pre_counts[tbl]}, Post: {current_count}")
                sys.exit(1)
            print(f"  - {tbl}: {current_count} rows (verified identical to baseline)")

        for tbl in EDGE_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            print(f"  - {tbl} (edge): {count_res[0][0]} rows")
            if count_res[0][0] != 0:
                print(f"ERROR: Edge table {tbl} is not empty! Count: {count_res[0][0]}")
                sys.exit(1)

        print("Data integrity check PASSED: All 426 existing records and 6 previously created tables remain 100% intact.")

    print("\nPhase 2C DDL Batch 4 successfully executed and verified.")

if __name__ == "__main__":
    main()

