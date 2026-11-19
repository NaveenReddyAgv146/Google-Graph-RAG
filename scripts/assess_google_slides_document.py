"""Read-only assessment script for Google Slides document doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ."""

import json
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"

print(f"=== ASSESSING GOOGLE SLIDES DOCUMENT: {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

with db.snapshot(multi_use=True) as s:
    # 1. Documents table row
    doc_cols = list(s.execute_sql("""
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'Documents' ORDER BY ORDINAL_POSITION
    """))
    col_names = [c[0] for c in doc_cols]
    doc_rows = list(s.execute_sql("SELECT * FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    
    print("\n1. Documents Record:")
    if doc_rows:
        for col, val in zip(col_names, doc_rows[0]):
            print(f"   {col:20}: {val}")
    else:
        print("   Document NOT FOUND!")

    # 2. EvidenceChunks breakdown
    chunks = list(s.execute_sql("""
        SELECT ChunkId, DocumentId, Modality, TextContent, PageNumber, SlideNumber, 
               SourceGcsUri, Embedding768 IS NOT NULL, ARRAY_LENGTH(Embedding768)
        FROM EvidenceChunks
        WHERE DocumentId = @d
        ORDER BY SlideNumber, PageNumber, ChunkId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    
    print(f"\n2. EvidenceChunks Breakdown (Total: {len(chunks)}):")
    text_chunks = [c for c in chunks if c[2] == "TEXT"]
    image_chunks = [c for c in chunks if c[2] == "IMAGE"]
    table_chunks = [c for c in chunks if c[2] == "TABLE"]
    embedded_chunks = [c for c in chunks if c[7] is True]
    
    print(f"   - TEXT chunks:     {len(text_chunks)}")
    print(f"   - IMAGE chunks:    {len(image_chunks)}")
    print(f"   - TABLE chunks:    {len(table_chunks)}")
    print(f"   - Embedded chunks: {len(embedded_chunks)}")

    slides_represented = sorted(list({c[5] for c in chunks if c[5] is not None}))
    print(f"   - SlideNumbers:    {slides_represented}")
    
    # VisualAssets check
    va_cols = list(s.execute_sql("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'VisualAssets' ORDER BY ORDINAL_POSITION"))
    print("\n2b. VisualAssets Schema Columns:", [c[0] for c in va_cols])
    va_rows = list(s.execute_sql("SELECT * FROM VisualAssets WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"   VisualAssets rows for {doc_id}: {len(va_rows)}")

    all_docs = list(s.execute_sql("SELECT DocumentId, FileName, Status FROM Documents"))
    print("\nAll Documents in Spanner:")
    for ad in all_docs:
        print(f"   - {ad[0]}: {ad[1]} ({ad[2]})")

    print("\n   All 10 EvidenceChunks details:")
    for idx, c in enumerate(chunks, 1):
        print(f"   [{idx:02d}] {c[0]} | Slide={c[5]} | Modality={c[2]} | HasEmb={c[7]}")
        print(f"        Content: {repr(c[3][:120])}")

    # 3. Existing SolutionCards
    cards = list(s.execute_sql("""
        SELECT CardId, ProjectName, BusinessProblem, ProposedSolution, ImplementedSolution, 
               Outcome, VerificationStatus, OkfUri
        FROM SolutionCards
        WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"\n3. Existing SolutionCards (Total: {len(cards)}):")
    for c in cards:
        print(f"   - CardId={c[0]}, Project={c[1]}, Status={c[6]}, OkfUri={c[7]}")

    # 4. Existing SolutionImplementsDoc edges
    sids = list(s.execute_sql("""
        SELECT CardId, DocumentId FROM SolutionImplementsDoc WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"\n4. Existing SolutionImplementsDoc edges: {len(sids)}")
    for sid in sids:
        print(f"   - {sid[0]} -> {sid[1]}")

    # 5. Existing EvidenceLinks
    ev_links = list(s.execute_sql("""
        SELECT EvidenceId, CardId, ChunkId FROM EvidenceLinks WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"\n5. Existing EvidenceLinks: {len(ev_links)}")
    for el in ev_links:
        print(f"   - {el[0]}: Card={el[1]} -> Chunk={el[2]}")

    # 6. Existing Entities referenced by these chunks
    chunk_ids = [c[0] for c in chunks]
    if chunk_ids:
        cres = list(s.execute_sql("""
            SELECT cre.ChunkId, cre.EntityId, e.Name, e.Category
            FROM ChunkReferencesEntity cre
            JOIN Entities e ON cre.EntityId = e.EntityId
            WHERE cre.ChunkId IN UNNEST(@cids)
        """, params={"cids": chunk_ids}, param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)}))
        print(f"\n6. Existing ChunkReferencesEntity edges: {len(cres)}")
        for cre in cres:
            print(f"   - Chunk {cre[0]} references Entity {cre[2]} ({cre[3]})")
    
        # Existing ChunkMentionsTech
        cmts = list(s.execute_sql("""
            SELECT cmt.ChunkId, cmt.TechId, t.Name, t.Category
            FROM ChunkMentionsTech cmt
            JOIN Technologies t ON cmt.TechId = t.TechId
            WHERE cmt.ChunkId IN UNNEST(@cids)
        """, params={"cids": chunk_ids}, param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)}))
        print(f"\n7. Existing ChunkMentionsTech edges: {len(cmts)}")
        for cmt in cmts:
            print(f"   - Chunk {cmt[0]} mentions Tech {cmt[2]} ({cmt[3]})")

# 7. Check GCS sidecar objects
can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
print(f"\n8. GCS Canonical Sidecar exists: {can_blob.exists()}")
print(f"   GCS Staging Sidecar exists:   {stg_blob.exists()}")

# Also check other buckets (e.g. sales-discovery-mvp) for any legacy sidecars
mvp_bucket = storage_client.bucket("sales-discovery-mvp")
mvp_can = mvp_bucket.blob(f"metadata/{doc_id}.meta.json")
print(f"   Legacy MVP Bucket Sidecar exists: {mvp_can.exists()}")

# 8. Check EnrichmentOrchestrator completion checkpoint evaluation
orch = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)
cp_passed, cp_details = orch.check_completion_checkpoint(doc_id)
print(f"\n9. Current Completion Checkpoint Passed: {cp_passed}")
for k, v in cp_details.items():
    print(f"   {k}: {v}")

