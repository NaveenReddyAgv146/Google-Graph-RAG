#!/usr/bin/env python3
"""
Phase 2C DDL Batch 6 Execution and Verification Script.

Target:
Project:  xenon-broker-513704-s9
Instance: sales-discovery-trial
Database: sales-knowledge-db

Scope:
- Property Graph EnterpriseKnowledgeGraph (8 node tables, 11 edge tables with NO PROPERTIES)
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

PROPERTY_GRAPH_DDL = """CREATE PROPERTY GRAPH EnterpriseKnowledgeGraph
  NODE TABLES (
    Documents
      KEY (DocumentId)
      LABEL Document
      PROPERTIES (DocumentId, FileName, MimeType, SourceUrl, GcsUri, Status, RepoId),
    EvidenceChunks
      KEY (ChunkId)
      LABEL Chunk
      PROPERTIES (ChunkId, DocumentId, FileId, Modality, TextContent, PageNumber, SlideNumber, CommitSha, LineRange, SourceGcsUri),
    SolutionCards
      KEY (CardId)
      LABEL BusinessSolution
      PROPERTIES (CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution, ImplementedSolution, Outcome),
    VisualAssets
      KEY (VisualId)
      LABEL VisualAsset
      PROPERTIES (VisualId, DocumentId, PageNumber, SlideNumber, ImageGcsUri, Description, AssetType),
    Repositories
      KEY (RepoId)
      LABEL Repository
      PROPERTIES (RepoId, Name, Url, DefaultBranch, LastSyncedCommit),
    SourceFiles
      KEY (FileId)
      LABEL SourceFile
      PROPERTIES (FileId, RepoId, FilePath, Language, CommitSha),
    Entities
      KEY (EntityId)
      LABEL Entity
      PROPERTIES (EntityId, Name, Category, Description),
    Technologies
      KEY (TechId)
      LABEL Technology
      PROPERTIES (TechId, Name, Category, CanonicalVersion)
  )
  EDGE TABLES (
    DocumentContainsChunk
      SOURCE KEY (DocumentId) REFERENCES Documents(DocumentId)
      DESTINATION KEY (ChunkId) REFERENCES EvidenceChunks(ChunkId)
      LABEL `CONTAINS`
      NO PROPERTIES,
    RepoContainsFile
      SOURCE KEY (RepoId) REFERENCES Repositories(RepoId)
      DESTINATION KEY (FileId) REFERENCES SourceFiles(FileId)
      LABEL `CONTAINS`
      NO PROPERTIES,
    SourceFileContainsChunk
      SOURCE KEY (FileId) REFERENCES SourceFiles(FileId)
      DESTINATION KEY (ChunkId) REFERENCES EvidenceChunks(ChunkId)
      LABEL `CONTAINS`
      NO PROPERTIES,
    ChunkReferencesEntity
      SOURCE KEY (ChunkId) REFERENCES EvidenceChunks(ChunkId)
      DESTINATION KEY (EntityId) REFERENCES Entities(EntityId)
      LABEL `REFERENCES`
      NO PROPERTIES,
    EntityReferencesEntity
      SOURCE KEY (SourceEntityId) REFERENCES Entities(EntityId)
      DESTINATION KEY (TargetEntityId) REFERENCES Entities(EntityId)
      LABEL `REFERENCES`
      NO PROPERTIES,
    ChunkMentionsTech
      SOURCE KEY (ChunkId) REFERENCES EvidenceChunks(ChunkId)
      DESTINATION KEY (TechId) REFERENCES Technologies(TechId)
      LABEL `MENTIONS`
      NO PROPERTIES,
    VisualAssetBelongsToDoc
      SOURCE KEY (VisualId) REFERENCES VisualAssets(VisualId)
      DESTINATION KEY (DocumentId) REFERENCES Documents(DocumentId)
      LABEL `BELONGS_TO`
      NO PROPERTIES,
    SolutionSupportsTech
      SOURCE KEY (CardId) REFERENCES SolutionCards(CardId)
      DESTINATION KEY (TechId) REFERENCES Technologies(TechId)
      LABEL `SUPPORTS`
      NO PROPERTIES,
    SourceFileDefinesEntity
      SOURCE KEY (FileId) REFERENCES SourceFiles(FileId)
      DESTINATION KEY (EntityId) REFERENCES Entities(EntityId)
      LABEL `DEFINES`
      NO PROPERTIES,
    SourceFileCallsFile
      SOURCE KEY (CallerFileId) REFERENCES SourceFiles(FileId)
      DESTINATION KEY (CalleeFileId) REFERENCES SourceFiles(FileId)
      LABEL `CALLS`
      NO PROPERTIES,
    SolutionImplementsDoc
      SOURCE KEY (CardId) REFERENCES SolutionCards(CardId)
      DESTINATION KEY (DocumentId) REFERENCES Documents(DocumentId)
      LABEL `IMPLEMENTS`
      NO PROPERTIES
  )"""

def main():
    print(f"Connecting to Cloud Spanner database: {PROJECT_ID}/{INSTANCE_ID}/{DATABASE_ID}...")
    spanner_client = spanner.Client(project=PROJECT_ID)
    instance = spanner_client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)
    admin_client = spanner_client.database_admin_api

    # 1. Preflight Inspection
    print("\nStep 1: Inspecting existing schema, vector index readiness, and row counts...")
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

        # Verify Vector Index readiness
        idx_res = list(snapshot.execute_sql("""
        SELECT index_name, table_name, index_state 
        FROM information_schema.indexes 
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """))
        if not idx_res or idx_res[0][2] != "READ_WRITE":
            print(f"ERROR: EvidenceChunkVectorIndex not ready! State: {idx_res[0][2] if idx_res else 'NOT FOUND'}")
            sys.exit(1)
        print(f"Vector index verified: EvidenceChunkVectorIndex state = READ_WRITE.")

        # Check if Property Graph already exists
        graph_query = """
        SELECT property_graph_name 
        FROM information_schema.property_graphs
        WHERE property_graph_name = 'EnterpriseKnowledgeGraph'
        """
        try:
            graphs = list(snapshot.execute_sql(graph_query))
            if graphs:
                print(f"Property graph 'EnterpriseKnowledgeGraph' already exists. Skipping creation.")
                statement_needed = False
            else:
                print("Property graph 'EnterpriseKnowledgeGraph' not found. Queued for creation.")
        except Exception as e:
            print(f"Note: information_schema.property_graphs check note: {e}")

    if not statement_needed:
        print("\nProperty graph already exists. Skipping DDL execution.")
    else:
        # 2. Execute Batch 6 DDL
        print("\nStep 2: Executing Batch 6 Property Graph DDL...")
        print(PROPERTY_GRAPH_DDL)

        db_path = admin_client.database_path(PROJECT_ID, INSTANCE_ID, DATABASE_ID)
        request = spanner_database_admin.UpdateDatabaseDdlRequest(
            database=db_path,
            statements=[PROPERTY_GRAPH_DDL],
        )
        operation = admin_client.update_database_ddl(request=request)
        print(f"\nStep 3: Operation started: {operation.operation.name}. Waiting for completion...")
        operation.result()
        print("DDL Operation completed successfully.")

    # 4. Verification through INFORMATION_SCHEMA and Spanner Graph
    print("\nStep 4: Verifying Property Graph through Spanner metadata...")
    with database.snapshot(multi_use=True) as snapshot:
        # Check property_graphs table
        graph_res = list(snapshot.execute_sql("""
        SELECT property_graph_name 
        FROM information_schema.property_graphs
        WHERE property_graph_name = 'EnterpriseKnowledgeGraph'
        """))
        if not graph_res:
            print("ERROR: EnterpriseKnowledgeGraph not found in information_schema.property_graphs!")
            sys.exit(1)
        print(f"Verified Property Graph in information_schema: {graph_res[0][0]}")

        # Check DDL in database statements
        db_path = admin_client.database_path(PROJECT_ID, INSTANCE_ID, DATABASE_ID)
        ddl_stmts = admin_client.get_database_ddl(database=db_path).statements
        matching_graph_ddl = [s for s in ddl_stmts if "CREATE PROPERTY GRAPH EnterpriseKnowledgeGraph" in s or "EnterpriseKnowledgeGraph" in s]
        if not matching_graph_ddl:
            print("ERROR: Property Graph DDL not found in get_database_ddl!")
            sys.exit(1)
        print(f"Verified DDL in Database Admin Catalog: {len(matching_graph_ddl)} statement(s) found.")

        # Test GQL query execution
        print("\nTesting ISO GQL graph query compilation:")
        gql_query = "GRAPH EnterpriseKnowledgeGraph MATCH (n) RETURN COUNT(n) AS total_nodes"
        gql_res = list(snapshot.execute_sql(gql_query))
        print(f"  - GQL query executed: '{gql_query}' -> {gql_res[0][0]} total nodes in graph.")

        # Re-verify Vector Index readiness
        idx_res = list(snapshot.execute_sql("""
        SELECT index_name, table_name, index_state 
        FROM information_schema.indexes 
        WHERE table_schema = '' AND index_name = 'EvidenceChunkVectorIndex'
        """))
        if not idx_res or idx_res[0][2] != "READ_WRITE":
            print(f"ERROR: Vector index state mismatch post-graph creation! State: {idx_res}")
            sys.exit(1)
        print(f"Vector index confirmed READ_WRITE post-graph DDL.")

        # 5. Confirm row counts of all 25 tables
        print("\nStep 5: Verifying data integrity of all 25 tables...")
        for tbl in PRE_EXISTING_TABLES:
            count_res = list(snapshot.execute_sql(f"SELECT COUNT(1) FROM {tbl}"))
            current_count = count_res[0][0]
            if current_count != pre_counts[tbl]:
                print(f"CRITICAL ERROR: Data mutation detected on {tbl}! Pre: {pre_counts[tbl]}, Post: {current_count}")
                sys.exit(1)
            print(f"  - {tbl}: {current_count} rows (verified identical to baseline)")

        print("Data integrity check PASSED: All 426 existing records and all graph tables remain 100% intact.")

    print("\nPhase 2C DDL Batch 6 successfully executed and verified.")

if __name__ == "__main__":
    main()
