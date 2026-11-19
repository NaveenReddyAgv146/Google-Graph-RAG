"""Authorized live enrichment execution script for enterprise_architecture_sample.gslides (doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ)."""

import hashlib
import json
import logging
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"
os.environ["GRPC_DNS_RESOLVER"] = "native"

import google.auth
from common.config import settings
from common.database.enrichment_writer import EnrichmentPayload, EnrichmentWriter
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    ValidatedEntity,
    ValidatedEntityRelationship,
    ValidatedSolutionCard,
    ValidatedTechnology,
    RejectedCandidate,
)
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("slides_live_enrichment")

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
card_id = "card_f9f52f771219353bdcd834f6aa644ee426e6ba1459037bd8"
IMMUTABLE_ENRICHED_AT = "2026-11-16T10:00:00+00:00"
expected_sha256 = "c379116a059b5f1383d117ecd187c833fbbb0a8a7727991cd99e0d2d031c9f9c"
expected_mutations = 34

print(f"=== AUTHORIZED LIVE ENRICHMENT FOR {doc_id} ===")
creds, _ = google.auth.default()

spanner_client = spanner.Client(project=settings.project_id, credentials=creds)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id, credentials=creds)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

# ============================================================================
# STEP 1: FRESH READ-ONLY PREFLIGHT
# ============================================================================
print("\n--- STEP 1: READ-ONLY PREFLIGHT ---")
with db.snapshot(multi_use=True) as s:
    doc_rows = list(s.execute_sql("SELECT DocumentId, Status FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if not doc_rows or doc_rows[0][1] != "ACTIVE":
        print(f"ABORT: Document state is {doc_rows[0][1] if doc_rows else 'NOT_FOUND'}, expected ACTIVE")
        sys.exit(1)
    
    chunk_rows = list(s.execute_sql("SELECT ChunkId, Modality, Embedding768 IS NOT NULL FROM EvidenceChunks WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    text_chunks = [r for r in chunk_rows if r[1] == "TEXT"]
    table_chunks = [r for r in chunk_rows if r[1] == "TABLE"]
    image_chunks = [r for r in chunk_rows if r[1] == "IMAGE"]
    embedded_chunks = [r for r in chunk_rows if r[2] is True]
    
    if len(text_chunks) != 9 or len(table_chunks) != 1 or len(image_chunks) != 0 or len(embedded_chunks) != 0:
        print(f"ABORT: Unexpected chunks count: TEXT={len(text_chunks)}, TABLE={len(table_chunks)}, IMAGE={len(image_chunks)}, Embedded={len(embedded_chunks)}")
        sys.exit(1)
        
    cards = list(s.execute_sql("SELECT CardId, VerificationStatus FROM SolutionCards WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if len(cards) != 0:
        print(f"ABORT: Unexpected SolutionCards count: {len(cards)}, expected 0")
        sys.exit(1)

    va_rows = list(s.execute_sql("SELECT VisualId FROM VisualAssets WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if len(va_rows) != 5:
        print(f"ABORT: Unexpected VisualAssets count: {len(va_rows)}, expected 5")
        sys.exit(1)

can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
if can_blob.exists() or stg_blob.exists():
    print("ABORT: Sidecar already exists in GCS!")
    sys.exit(1)

print("Preflight Check PASSED: Document ACTIVE, 9 TEXT, 1 TABLE, 0 IMAGE, 0 embeddings, 5 VisualAssets, 0 SolutionCards, 0 GCS sidecars.")

# ============================================================================
# STEP 2: AUDITED KNOWLEDGE GRAPH METADATA & EMBEDDINGS
# ============================================================================
print("\n--- STEP 2: METADATA & EMBEDDING PREPARATION ---")
chunks_data = orchestrator._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]
chunks_by_id = {c.chunk_id: c.text_content for c in eligible_chunks}

entities = [
    ValidatedEntity(
        entity_id="entity_google_slides_pipeline",
        name="Google Slides Pipeline",
        category="SUBSYSTEM",
        description="Pipeline component handling Slides API retrieval, visual thumbnail rendering, and atomic persistence",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3B: Google Slides Pipeline: Slides API retrieval, visual thumbnail rendering, and atomic persistence (Current).",
    ),
    ValidatedEntity(
        entity_id="entity_google_drive_webhooks",
        name="Google Drive Webhooks",
        category="API",
        description="Real-time change notification and incremental delta sync mechanism",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3C: Google Drive Webhooks: Real-time change notifications, incremental sync, and delta detection (Next).",
    ),
    ValidatedEntity(
        entity_id="entity_google_slides_api",
        name="Google Slides API",
        category="API",
        description="Google API used for slide content and asset extraction",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s03_b006",
        evidence_quote="Google Slides API Fetch",
    ),
    ValidatedEntity(
        entity_id="entity_pdf_ingestion_pipeline",
        name="PDF Ingestion Pipeline",
        category="SUBSYSTEM",
        description="Document AI layout extraction, chunking, and Spanner persistence pipeline",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3A: PDF Ingestion Pipeline: Document AI layout extraction, chunking, and Spanner persistence (Completed).",
    ),
    ValidatedEntity(
        entity_id="entity_unified_graph_schema",
        name="Unified Graph Schema",
        category="STANDARD",
        description="Canonical schema spanning Documents, EvidenceChunks, VisualAssets, and typed graph edges",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s02_t003",
        evidence_quote="Unified Graph Schema: Documents, EvidenceChunks, VisualAssets, and typed graph edges.",
    ),
]

technologies = [
    ValidatedTechnology(
        raw_name="Cloud Spanner",
        canonical_name="Google Cloud Spanner",
        tech_id="tech_cloud_spanner",
        category="Database",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s01_t001",
        evidence_quote="Cloud Spanner delivers high-throughput storage while agentic workflows extract multimodal provenance across slides, text, and visual assets.",
        is_canonical=True,
        unresolved=False,
    ),
    ValidatedTechnology(
        raw_name="GCS",
        canonical_name="Google Cloud Storage",
        tech_id="tech_cloud_storage",
        category="Storage",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s02_t004",
        evidence_quote="Highlight that all five Spanner tables participate in a single serializable transaction with GCS compensating rollback on failure.",
        is_canonical=True,
        unresolved=False,
    ),
    ValidatedTechnology(
        raw_name="Document AI",
        canonical_name="Document AI",
        tech_id="tech_document_ai",
        category="AI & Machine Learning",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Document AI layout extraction, chunking, and Spanner persistence",
        is_canonical=True,
        unresolved=False,
    ),
    ValidatedTechnology(
        raw_name="text-embedding-005",
        canonical_name=None,
        tech_id=None,
        category=None,
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="768-dimensional text-embedding-005 generation across all modalities",
        is_canonical=False,
        unresolved=True,
    ),
]

relationships = [
    ValidatedEntityRelationship(
        source_entity_id="entity_google_slides_pipeline",
        target_entity_id="entity_google_slides_api",
        source_name="Google Slides Pipeline",
        target_name="Google Slides API",
        relationship_type="USES",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3B: Google Slides Pipeline: Slides API retrieval, visual thumbnail rendering, and atomic persistence (Current).",
    ),
]

rejected_candidates = [
    RejectedCandidate(
        candidate_type="RELATIONSHIP",
        candidate_name="PDF Ingestion Pipeline -> Document AI (USES)",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3A: PDF Ingestion Pipeline: Document AI layout extraction, chunking, and Spanner persistence (Completed).",
        rejection_reason="Target 'Document AI' is a cataloged Technology (tech_document_ai), not an Entity. Cross-category edges to technologies are represented via ChunkMentionsTech and SolutionSupportsTech.",
    ),
    RejectedCandidate(
        candidate_type="RELATIONSHIP",
        candidate_name="PDF Ingestion Pipeline -> Cloud Spanner (USES)",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3A: PDF Ingestion Pipeline: Document AI layout extraction, chunking, and Spanner persistence (Completed).",
        rejection_reason="Target 'Cloud Spanner' is a cataloged Technology (tech_cloud_spanner), not an Entity. Cross-category edges to technologies are represented via ChunkMentionsTech and SolutionSupportsTech.",
    ),
]

corrected_outcome = (
    "Target acceptance criteria achieved across benchmarks evaluated with regional Cloud Spanner instances in asia-south1: "
    "Google Slides API Fetch < 1.5s (P95), Content Extraction & Norm < 800ms (P95), "
    "Visual Asset GCS Upload < 1.0s (P95), and Spanner 5-table transactional commits maintaining sub-150ms latency (P95)."
)

solution_cards = [
    ValidatedSolutionCard(
        card_id=card_id,
        document_id=doc_id,
        project_name="Scalable Agentic Knowledge Discovery & Graph Systems",
        business_problem="Modern enterprise discovery systems unify semi-structured documentation and relational transactions into a cohesive graph topology.",
        proposed_solution="Cloud Spanner delivers high-throughput storage while agentic workflows extract multimodal provenance across slides, text, and visual assets.",
        implemented_solution="Unified Graph Schema: Documents, EvidenceChunks, VisualAssets, and typed graph edges. Dual-Bucket Storage: Raw presentations and extracted visual assets isolated regionally. Transactional Integrity: Atomic Spanner persistence across all 5 tables with zero partial writes.",
        outcome=corrected_outcome,
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s01_t001",
        evidence_quote="Modern enterprise discovery systems unify semi-structured documentation and relational transactions into a cohesive graph topology. Cloud Spanner delivers high-throughput storage while agentic workflows extract multimodal provenance across slides, text, and visual assets.",
        technologies=["tech_cloud_spanner", "tech_cloud_storage", "tech_document_ai"],
        confidence_score=0.95,
    )
]

# Exact quote verification guard
for e in entities:
    assert e.evidence_quote in chunks_by_id.get(e.chunk_id, ""), f"Quote failed for {e.name}"
for t in technologies:
    assert t.evidence_quote in chunks_by_id.get(t.chunk_id, ""), f"Quote failed for {t.raw_name}"
for r in relationships:
    assert r.evidence_quote in chunks_by_id.get(r.chunk_id, ""), f"Quote failed for {r.source_name}->{r.target_name}"
for c in solution_cards:
    assert c.evidence_quote in chunks_by_id.get(c.chunk_id, ""), f"Quote failed for {c.project_name}"
print("All source quotations verified 100% exact.")

metadata_result = ExtractedMetadataResult(
    document_id=doc_id,
    entities=entities,
    technologies=technologies,
    relationships=relationships,
    solution_cards=solution_cards,
    unresolved_technologies=["text-embedding-005"],
    rejected_candidates=rejected_candidates,
)

logger.info("Generating 768-dim embeddings for 10 chunks via Vertex AI text-embedding-005")
chunk_texts = [c.text_content for c in eligible_chunks]
raw_embeddings = orchestrator.embeddings_client.embed_texts(chunk_texts)
chunk_embeddings = {
    chunk.chunk_id: emb for chunk, emb in zip(eligible_chunks, raw_embeddings)
}
assert len(chunk_embeddings) == 10, f"Expected 10 embeddings, got {len(chunk_embeddings)}"
for cid, emb in chunk_embeddings.items():
    assert len(emb) == 768, f"Embedding dimension mismatch for {cid}: {len(emb)}"

sidecar_bytes = orchestrator._build_sidecar_bytes(
    document_id=doc_id,
    metadata=metadata_result,
    chunk_embeddings_count=len(chunk_embeddings),
    eligible_chunks_count=len(eligible_chunks),
    enriched_at=IMMUTABLE_ENRICHED_AT,
)
sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
sidecar_uri = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json"
canonical_okf_uri = f"{sidecar_uri}#sha256={sidecar_sha256}"

print(f"Computed Sidecar SHA256: {sidecar_sha256}")
print(f"Expected Sidecar SHA256: {expected_sha256}")
if sidecar_sha256 != expected_sha256:
    print(f"ABORT: Sidecar SHA256 mismatch! {sidecar_sha256} != {expected_sha256}")
    sys.exit(1)

# ============================================================================
# STEP 3: TRANSACTIONAL MUTATION PRE-VALIDATION
# ============================================================================
print("\n--- STEP 3: PRE-VALIDATING EXACT 34 MUTATIONS ---")
payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings=chunk_embeddings,
    metadata_result=metadata_result,
    sidecar_uri=sidecar_uri,
    sidecar_sha256=sidecar_sha256,
)

class TrackingTransaction:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.buffered_mutations = {
            "EvidenceChunks": 0,
            "Entities": 0,
            "Technologies": 0,
            "SolutionCards": 0,
            "ChunkReferencesEntity": 0,
            "ChunkMentionsTech": 0,
            "SolutionSupportsTech": 0,
            "SolutionImplementsDoc": 0,
            "EntityReferencesEntity": 0,
            "EvidenceLinks": 0,
            "Documents": 0,
        }

    def execute_sql(self, sql, params=None, param_types=None):
        return self.snapshot.execute_sql(sql, params=params, param_types=param_types)

    def insert(self, table, columns, values):
        self.buffered_mutations[table] += len(values)

    def update(self, table, columns, values):
        self.buffered_mutations[table] += len(values)

writer = EnrichmentWriter(database=db)
with db.snapshot(multi_use=True) as snap:
    mock_txn = TrackingTransaction(snap)
    sim_res = writer._enrichment_transaction(mock_txn, payload, force_recompute=False)

if sim_res.total_mutations != expected_mutations:
    print(f"ABORT: Mutation count mismatch! {sim_res.total_mutations} != {expected_mutations}")
    sys.exit(1)

print(f"Simulation confirmed exactly {sim_res.total_mutations} mutations across 11 tables:")
for tbl, cnt in mock_txn.buffered_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

# ============================================================================
# STEP 4: GCS SIDECAR STAGING
# ============================================================================
print("\n--- STEP 4: GCS SIDECAR STAGING ---")
staging_blob_name = f"metadata/staging/{doc_id}.meta.json.tmp"
staging_blob = bucket.blob(staging_blob_name)
staging_blob.upload_from_string(
    sidecar_bytes,
    content_type="application/json",
)
stg_bytes = staging_blob.download_as_bytes()
stg_hash = hashlib.sha256(stg_bytes).hexdigest()
if stg_hash != expected_sha256:
    print(f"ABORT: Staged sidecar SHA mismatch! {stg_hash} != {expected_sha256}")
    sys.exit(1)
print(f"Staged sidecar uploaded and verified: gs://{settings.gcs_mumbai_bucket}/{staging_blob_name} (SHA: {stg_hash})")

# ============================================================================
# STEP 5: ATOMIC SPANNER TRANSACTION COMMIT
# ============================================================================
print("\n--- STEP 5: ATOMIC SPANNER TRANSACTION COMMIT ---")
write_result = writer.persist_enrichment(payload=payload, force_recompute=False)
print(f"Spanner commit completed successfully!")
print(f"Committed Mutations: {write_result.total_mutations} (expected {expected_mutations})")
if write_result.total_mutations != expected_mutations:
    print(f"CRITICAL: Spanner committed with unexpected mutation count: {write_result.total_mutations}!")
    sys.exit(1)

# ============================================================================
# STEP 6: CANONICAL GCS PROMOTION & STAGING DELETION
# ============================================================================
print("\n--- STEP 6: CANONICAL PROMOTION & STAGING DELETION ---")
canonical_blob_name = f"metadata/{doc_id}.meta.json"
canonical_blob = bucket.blob(canonical_blob_name)
canonical_blob.upload_from_string(
    sidecar_bytes,
    content_type="application/json",
)
can_bytes = canonical_blob.download_as_bytes()
can_hash = hashlib.sha256(can_bytes).hexdigest()
if can_hash != expected_sha256:
    print(f"CRITICAL RECOVERY REQUIRED: Canonical sidecar SHA mismatch! {can_hash} != {expected_sha256}. Staging object preserved.")
    sys.exit(1)

print(f"Canonical sidecar verified: gs://{settings.gcs_mumbai_bucket}/{canonical_blob_name} (SHA: {can_hash})")
staging_blob.delete()
print(f"Staging temporary object deleted.")

print("\nLIVE ENRICHMENT EXECUTION FINISHED SUCCESSFULLY!")
