import os
import sys
import json
import hashlib

sys.path.insert(0, '/home/naveen/enterprise-graphrag/common/src')
sys.path.insert(0, '/home/naveen/enterprise-graphrag/ingestion-copilot')

from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

print("=== READ-ONLY INVENTORY OF SPANNER DOCUMENTS ===")

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)
orch = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

with db.snapshot(multi_use=True) as s:
    # 1. Get all documents
    docs_sql = "SELECT DocumentId, FileName, MimeType, Status FROM Documents ORDER BY DocumentId"
    docs = list(s.execute_sql(docs_sql))
    print(f"Total Documents in Spanner: {len(docs)}")

    inventory = []

    for doc_id, file_name, mime_type, status in docs:
        # Chunks breakdown
        chunks_sql = """
        SELECT Modality, COUNT(1) AS total,
               COUNTIF(Embedding768 IS NOT NULL) AS embedded,
               COUNTIF(TextContent IS NOT NULL AND TRIM(TextContent) != '') AS non_empty
        FROM EvidenceChunks
        WHERE DocumentId = @d
        GROUP BY Modality
        ORDER BY Modality
        """
        chunk_rows = list(s.execute_sql(chunks_sql, params={'d': doc_id}, param_types={'d': spanner.param_types.STRING}))
        
        chunk_dict = {}
        for mod, tot, emb, non_empty in chunk_rows:
            chunk_dict[mod] = {"total": tot, "embedded": emb, "non_empty": non_empty}
            
        text_info = chunk_dict.get("TEXT", {"total": 0, "embedded": 0, "non_empty": 0})
        table_info = chunk_dict.get("TABLE", {"total": 0, "embedded": 0, "non_empty": 0})
        image_info = chunk_dict.get("IMAGE", {"total": 0, "embedded": 0, "non_empty": 0})
        
        eligible_text = text_info["non_empty"] + table_info["non_empty"]
        image_count = image_info["total"]
        total_embedded = sum(c["embedded"] for c in chunk_dict.values())
        total_chunks = sum(c["total"] for c in chunk_dict.values())

        # SolutionCards
        cards_sql = """
        SELECT CardId, ProjectName, BusinessProblem, VerificationStatus, OkfUri
        FROM SolutionCards
        WHERE DocumentId = @d
        ORDER BY CardId
        """
        card_rows = list(s.execute_sql(cards_sql, params={'d': doc_id}, param_types={'d': spanner.param_types.STRING}))

        # SolutionImplementsDoc
        sid_sql = """
        SELECT CardId, DocumentId
        FROM SolutionImplementsDoc
        WHERE DocumentId = @d
        ORDER BY CardId
        """
        sid_rows = list(s.execute_sql(sid_sql, params={'d': doc_id}, param_types={'d': spanner.param_types.STRING}))

        # EvidenceLinks
        evl_sql = """
        SELECT COUNT(1)
        FROM EvidenceLinks
        WHERE DocumentId = @d
        """
        evl_count = list(s.execute_sql(evl_sql, params={'d': doc_id}, param_types={'d': spanner.param_types.STRING}))[0][0]

        # GCS sidecars
        can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
        stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
        can_exists = can_blob.exists()
        stg_exists = stg_blob.exists()
        
        can_hash = None
        if can_exists:
            can_hash = hashlib.sha256(can_blob.download_as_bytes()).hexdigest()

        # Checkpoint evaluation
        cp_passed, cp_details = orch.check_completion_checkpoint(doc_id)

        doc_summary = {
            "document_id": doc_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "status": status,
            "eligible_text_chunks": eligible_text,
            "image_chunks": image_count,
            "total_chunks": total_chunks,
            "total_embedded": total_embedded,
            "solution_cards": [
                {
                    "card_id": r[0],
                    "project_name": r[1],
                    "has_business_problem": r[2] is not None,
                    "verification_status": r[3],
                    "okf_uri": r[4],
                }
                for r in card_rows
            ],
            "solution_implements_doc_count": len(sid_rows),
            "evidence_links_count": evl_count,
            "canonical_sidecar_exists": can_exists,
            "canonical_sha256": can_hash,
            "staging_sidecar_exists": stg_exists,
            "checkpoint_passed": cp_passed,
            "checkpoint_details": cp_details,
        }
        inventory.append(doc_summary)

print(json.dumps(inventory, indent=2))

