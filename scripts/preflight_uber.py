"""Preflight baseline inspection script for Uber - 2008 (1).pdf (1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_)."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner, storage

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"

print(f"=== READ-ONLY PREFLIGHT INSPECTION FOR {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

results = {}

with db.snapshot(multi_use=True) as s:
    # 1. Document exists and Status == INGESTED
    doc_rows = list(s.execute_sql("SELECT DocumentId, Filename, MimeType, Status FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if not doc_rows:
        print("ERROR: Document not found in Spanner!")
        sys.exit(1)
    doc_row = doc_rows[0]
    print(f"1. Document: DocumentId={doc_row[0]}, Filename={doc_row[1]}, MimeType={doc_row[2]}, Status={doc_row[3]}")
    results["status_ingested"] = (doc_row[3] == "INGESTED")

    # 2. Chunks count: 25 TEXT, 50 IMAGE, all embeddings NULL
    chunk_rows = list(s.execute_sql("""
        SELECT ChunkId, Modality, Embedding768 IS NOT NULL, ARRAY_LENGTH(Embedding768)
        FROM EvidenceChunks
        WHERE DocumentId = @d
        ORDER BY Modality, ChunkId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    
    text_chunks = [r for r in chunk_rows if r[1] == "TEXT"]
    image_chunks = [r for r in chunk_rows if r[1] == "IMAGE"]
    
    print(f"2. TEXT chunks count: {len(text_chunks)} (expected 25)")
    print(f"   IMAGE chunks count: {len(image_chunks)} (expected 50)")
    
    has_any_embeddings = any(r[2] is True for r in chunk_rows)
    print(f"   Any existing embeddings: {has_any_embeddings} (expected False)")
    results["text_chunks_25"] = (len(text_chunks) == 25)
    results["image_chunks_50"] = (len(image_chunks) == 50)
    results["no_embeddings"] = (not has_any_embeddings)

    # 3. EvidenceLinks count
    ev_links = list(s.execute_sql("""
        SELECT EvidenceId FROM EvidenceLinks WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"3. Existing EvidenceLinks: {len(ev_links)} (expected 0)")
    results["no_evidence_links"] = (len(ev_links) == 0)

    # 4. Legacy SolutionCard and SolutionImplementsDoc edge
    cards = list(s.execute_sql("""
        SELECT CardId, ProjectName, BusinessProblem, ProposedSolution, ImplementedSolution, 
               Outcome, VerificationStatus, OkfUri
        FROM SolutionCards
        WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"4. Existing SolutionCards count: {len(cards)} (expected 1)")
    for c in cards:
        print(f"   - CardId={c[0]}, ProjectName={c[1]}, Status={c[6]}, OkfUri={c[7]}")
    
    sids = list(s.execute_sql("""
        SELECT CardId, DocumentId FROM SolutionImplementsDoc WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"   SolutionImplementsDoc count: {len(sids)} (expected 1)")
    for sid in sids:
        print(f"   - Edge: CardId={sid[0]} -> DocId={sid[1]}")
    
    results["one_draft_card"] = (len(cards) == 1 and cards[0][6] == "DRAFT")
    results["one_implements_edge"] = (len(sids) == 1 and sids[0][0] == cards[0][0])

# 5. Check GCS sidecars
can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
can_exists = can_blob.exists()
stg_exists = stg_blob.exists()
print(f"5. Canonical GCS sidecar exists: {can_exists} (expected False)")
print(f"   Staging GCS sidecar exists: {stg_exists} (expected False)")
results["no_gcs_sidecars"] = (not can_exists and not stg_exists)

print("\n=== PREFLIGHT RESULTS SUMMARY ===")
for k, v in results.items():
    print(f"  {k}: {v}")

if not all(results.values()):
    print("\nPREFLIGHT CHECKS FAILED! STOPPING.")
    sys.exit(1)

print("\nALL PREFLIGHT CHECKS PASSED!")

