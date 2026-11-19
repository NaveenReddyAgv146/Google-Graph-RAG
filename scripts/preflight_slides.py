"""Preflight baseline inspection script for enterprise_architecture_sample.gslides (doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ)."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner, storage

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"

print(f"=== READ-ONLY PREFLIGHT INSPECTION FOR {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

results = {}

with db.snapshot(multi_use=True) as s:
    # 1. Document exists and Status == ACTIVE
    doc_rows = list(s.execute_sql("SELECT DocumentId, Filename, MimeType, Status FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if not doc_rows:
        print("ERROR: Document not found in Spanner!")
        sys.exit(1)
    doc_row = doc_rows[0]
    print(f"1. Document: DocumentId={doc_row[0]}, Filename={doc_row[1]}, MimeType={doc_row[2]}, Status={doc_row[3]}")
    results["status_active"] = (doc_row[3] == "ACTIVE")

    # 2. Chunks count: 9 TEXT, 1 TABLE, 0 IMAGE, all embeddings NULL
    chunk_rows = list(s.execute_sql("""
        SELECT ChunkId, Modality, Embedding768 IS NOT NULL, ARRAY_LENGTH(Embedding768)
        FROM EvidenceChunks
        WHERE DocumentId = @d
        ORDER BY Modality, ChunkId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    
    text_chunks = [r for r in chunk_rows if r[1] == "TEXT"]
    table_chunks = [r for r in chunk_rows if r[1] == "TABLE"]
    image_chunks = [r for r in chunk_rows if r[1] == "IMAGE"]
    
    print(f"2. Chunks count:")
    print(f"   - TEXT chunks: {len(text_chunks)} (expected 9)")
    print(f"   - TABLE chunks: {len(table_chunks)} (expected 1)")
    print(f"   - IMAGE chunks: {len(image_chunks)} (expected 0)")
    print(f"   - Total eligible chunks: {len(text_chunks) + len(table_chunks)} (expected 10)")
    
    has_any_embeddings = any(r[2] is True for r in chunk_rows)
    print(f"   Any existing embeddings: {has_any_embeddings} (expected False)")
    results["text_chunks_9"] = (len(text_chunks) == 9)
    results["table_chunks_1"] = (len(table_chunks) == 1)
    results["image_chunks_0"] = (len(image_chunks) == 0)
    results["total_eligible_10"] = (len(text_chunks) + len(table_chunks) == 10)
    results["no_embeddings"] = (not has_any_embeddings)

    # 3. VisualAssets intact (expected 5)
    va_rows = list(s.execute_sql("""
        SELECT VisualId, SlideNumber, AssetType FROM VisualAssets WHERE DocumentId = @d ORDER BY SlideNumber, VisualId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"3. Existing VisualAssets count: {len(va_rows)} (expected 5)")
    for va in va_rows:
        print(f"   - VisualId={va[0]}, SlideNumber={va[1]}, AssetType={va[2]}")
    results["visual_assets_5"] = (len(va_rows) == 5)

    # 4. EvidenceLinks count (expected 0)
    ev_links = list(s.execute_sql("""
        SELECT EvidenceId FROM EvidenceLinks WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"4. Existing EvidenceLinks: {len(ev_links)} (expected 0)")
    results["no_evidence_links"] = (len(ev_links) == 0)

    # 5. SolutionCards count (expected 0)
    cards = list(s.execute_sql("""
        SELECT CardId, ProjectName, VerificationStatus, OkfUri
        FROM SolutionCards
        WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"5. Existing SolutionCards count: {len(cards)} (expected 0)")
    results["no_solution_cards"] = (len(cards) == 0)

    # 6. SolutionImplementsDoc edges count (expected 0)
    sids = list(s.execute_sql("""
        SELECT CardId, DocumentId FROM SolutionImplementsDoc WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"6. Existing SolutionImplementsDoc edges count: {len(sids)} (expected 0)")
    results["no_solution_edges"] = (len(sids) == 0)

# 7. Check GCS sidecars (expected False)
can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
can_exists = can_blob.exists()
stg_exists = stg_blob.exists()
print(f"7. Canonical GCS sidecar exists: {can_exists} (expected False)")
print(f"   Staging GCS sidecar exists: {stg_exists} (expected False)")
results["no_gcs_sidecars"] = (not can_exists and not stg_exists)

print("\n=== PREFLIGHT RESULTS SUMMARY ===")
all_passed = True
for k, v in results.items():
    print(f"  {k}: {v}")
    if not v:
        all_passed = False

if not all_passed:
    print("\nPREFLIGHT CHECKS FAILED! STOPPING.")
    sys.exit(1)
else:
    print("\nALL PREFLIGHT CHECKS PASSED SUCCESSFULLY.")
