"""Phase 3C.8.1 Preflight Inspection Script (Read-Only).

Inspects the target document 1S6i9SbkYHMAAEjmrliK-o5Z2CVjKGGXM (Youtube_2005_Series.pdf)
and verifies baseline Spanner, GCS, and checkpoint state before dry-run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator


def run_preflight(doc_id: str = "1S6i9SbkYHMAAEjmrliK-o5Z2CVjKGGXM") -> dict:
    print(f"=== PHASE 3C.8.1 PREFLIGHT INSPECTION: {doc_id} ===")
    print(f"Project ID: {settings.project_id}")
    print(f"Spanner Instance: {settings.spanner_instance_id}")
    print(f"Spanner Database: {settings.spanner_database_id}")
    print(f"GCS Mumbai Bucket: {settings.gcs_mumbai_bucket}")
    print(f"Gemini Model: {settings.reasoning_model}")
    print(f"Embedding Model: {settings.embedding_model}")

    spanner_client = spanner.Client(project=settings.project_id)
    db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
    storage_client = storage.Client(project=settings.project_id)
    bucket = storage_client.bucket(settings.gcs_mumbai_bucket)
    orch = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

    report = {
        "document_id": doc_id,
        "gcp_config": {
            "project_id": settings.project_id,
            "region": settings.region,
            "spanner_instance": settings.spanner_instance_id,
            "spanner_database": settings.spanner_database_id,
            "gcs_bucket": settings.gcs_mumbai_bucket,
            "reasoning_model": settings.reasoning_model,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
        },
    }

    with db.snapshot(multi_use=True) as s:
        # 1. Documents table row
        doc_sql = "SELECT DocumentId, FileName, MimeType, Status FROM Documents WHERE DocumentId = @d"
        doc_rows = list(s.execute_sql(doc_sql, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
        if not doc_rows:
            raise RuntimeError(f"Document {doc_id} not found in Documents table")
        doc_row = doc_rows[0]
        report["document"] = {
            "DocumentId": doc_row[0],
            "FileName": doc_row[1],
            "MimeType": doc_row[2],
            "Status": doc_row[3],
        }

        # 2. EvidenceChunks breakdown
        chunks_sql = """
        SELECT ChunkId, Modality, PageNumber, SlideNumber,
               LENGTH(COALESCE(TextContent, '')) AS TextLength,
               (Embedding768 IS NOT NULL) AS HasEmbedding,
               TextContent
        FROM EvidenceChunks
        WHERE DocumentId = @d
        ORDER BY PageNumber, SlideNumber, ChunkId
        """
        chunk_rows = list(s.execute_sql(chunks_sql, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
        report["chunks_summary"] = {
            "total_chunks": len(chunk_rows),
            "text_chunks": [],
            "image_chunks": [],
            "table_chunks": [],
        }

        for row in chunk_rows:
            chunk_info = {
                "chunk_id": row[0],
                "modality": row[1],
                "page_number": row[2],
                "slide_number": row[3],
                "text_length": row[4],
                "has_embedding": row[5],
                "text_preview": (row[6][:80] + "...") if row[6] else "",
            }
            if row[1] == "TEXT":
                report["chunks_summary"]["text_chunks"].append(chunk_info)
            elif row[1] == "IMAGE":
                report["chunks_summary"]["image_chunks"].append(chunk_info)
            elif row[1] == "TABLE":
                report["chunks_summary"]["table_chunks"].append(chunk_info)

        report["eligible_non_empty_text_chunks"] = len(
            [c for c in report["chunks_summary"]["text_chunks"] if c["text_length"] > 0]
        )
        report["excluded_image_chunks"] = len(report["chunks_summary"]["image_chunks"])
        report["existing_embeddings_count"] = sum(
            1 for c in chunk_rows if c[5] is True
        )

        # 3. Existing SolutionCards
        cards_sql = """
        SELECT CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution,
               ImplementedSolution, Outcome, VerificationStatus, OkfUri
        FROM SolutionCards
        WHERE DocumentId = @d
        ORDER BY CardId
        """
        card_rows = list(s.execute_sql(cards_sql, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
        report["solution_cards"] = [
            {
                "card_id": r[0],
                "document_id": r[1],
                "project_name": r[2],
                "has_business_problem": r[3] is not None,
                "has_proposed_solution": r[4] is not None,
                "has_implemented_solution": r[5] is not None,
                "has_outcome": r[6] is not None,
                "verification_status": r[7],
                "okf_uri": r[8],
            }
            for r in card_rows
        ]

        # 4. Existing SolutionImplementsDoc edges
        sid_sql = """
        SELECT CardId, DocumentId
        FROM SolutionImplementsDoc
        WHERE DocumentId = @d
        ORDER BY CardId
        """
        sid_rows = list(s.execute_sql(sid_sql, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
        report["solution_implements_doc"] = [{"card_id": r[0], "document_id": r[1]} for r in sid_rows]

        # 5. Existing EvidenceLinks
        evl_sql = "SELECT COUNT(1) FROM EvidenceLinks WHERE DocumentId = @d"
        evl_count = list(s.execute_sql(evl_sql, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))[0][0]
        report["evidence_links_count"] = evl_count

        # 6. Existing entities / tech / edges for this doc
        cre_sql = """
        SELECT COUNT(DISTINCT cre.EntityId)
        FROM ChunkReferencesEntity cre
        JOIN EvidenceChunks ec ON cre.ChunkId = ec.ChunkId
        WHERE ec.DocumentId = @d
        """
        entity_count = list(s.execute_sql(cre_sql, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))[0][0]
        report["existing_referenced_entities"] = entity_count

    # 7. GCS sidecars
    can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
    report["gcs_sidecars"] = {
        "canonical_exists": can_blob.exists(),
        "staging_exists": stg_blob.exists(),
    }

    # 8. Checkpoint evaluation
    cp_passed, cp_details = orch.check_completion_checkpoint(doc_id)
    report["checkpoint"] = {
        "passed": cp_passed,
        "details": cp_details,
    }

    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "1trxQDZDEGA23IHBPHFt84HLggBUZNelE"
    run_preflight(target)
