"""Consolidated final script for Google Slides dry-run review, SolutionCard wording refinement, and exact verification."""

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
logger = logging.getLogger("slides_finalize_dry_run")

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
card_id = "card_f9f52f771219353bdcd834f6aa644ee426e6ba1459037bd8"
IMMUTABLE_ENRICHED_AT = "2026-11-16T10:00:00+00:00"

print(f"=== FINALIZING GOOGLE SLIDES DRY-RUN FOR {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

# Step 1: Read eligible chunks
chunks_data = orchestrator._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]
chunks_by_id = {c.chunk_id: c.text_content for c in eligible_chunks}
print(f"Read {len(eligible_chunks)} eligible chunks.")

# Step 2: Frozen Audited Entities (5)
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

# Step 3: Frozen Audited Technologies (3 Canonical, 1 Unresolved)
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

# Step 4: Frozen Audited Relationships (3)
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
    ValidatedEntityRelationship(
        source_entity_id="entity_pdf_ingestion_pipeline",
        target_entity_id="entity_document_ai",
        source_name="PDF Ingestion Pipeline",
        target_name="Document AI",
        relationship_type="USES",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3A: PDF Ingestion Pipeline: Document AI layout extraction, chunking, and Spanner persistence (Completed).",
    ),
    ValidatedEntityRelationship(
        source_entity_id="entity_pdf_ingestion_pipeline",
        target_entity_id="entity_cloud_spanner",
        source_name="PDF Ingestion Pipeline",
        target_name="Cloud Spanner",
        relationship_type="USES",
        chunk_id="doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ_s04_t009",
        evidence_quote="Phase 3A: PDF Ingestion Pipeline: Document AI layout extraction, chunking, and Spanner persistence (Completed).",
    ),
]

# Step 5: Corrected SolutionCard
# - Check 1: Performance figures are explicitly designated in the table as "Target P95" and "Target P99" with "Acceptance Status: Achieved", and speaker notes state "Review table metrics. Cloud Spanner transactional commits maintain sub-150ms latency across all five destination tables."
# We refine the Outcome to explicitly state the achieved performance targets evaluated against regional Cloud Spanner instances.
# - Check 2: Initial VerificationStatus for newly inserted cards in Spanner is "PENDING".
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
    )
]

# Verify quotations
all_quotes_valid = True
for e in entities:
    valid = e.evidence_quote in chunks_by_id.get(e.chunk_id, "")
    print(f"Entity quote '{e.name}': {valid}")
    if not valid: all_quotes_valid = False

for t in technologies:
    valid = t.evidence_quote in chunks_by_id.get(t.chunk_id, "")
    print(f"Tech quote '{t.raw_name}': {valid}")
    if not valid: all_quotes_valid = False

for r in relationships:
    valid = r.evidence_quote in chunks_by_id.get(r.chunk_id, "")
    print(f"Rel quote '{r.source_name} -> {r.target_name}': {valid}")
    if not valid: all_quotes_valid = False

for c in solution_cards:
    valid = c.evidence_quote in chunks_by_id.get(c.chunk_id, "")
    print(f"Card quote '{c.project_name}': {valid}")
    if not valid: all_quotes_valid = False

print(f"\nAll Quotations Grounded in Source: {all_quotes_valid}")
if not all_quotes_valid:
    sys.exit(1)

metadata_result = ExtractedMetadataResult(
    document_id=doc_id,
    entities=entities,
    technologies=technologies,
    solution_cards=solution_cards,
    relationships=relationships,
    unresolved_technologies=["text-embedding-005"],
    rejected_candidates=[],
)

# Step 6: Generate 768-dim embeddings
chunk_texts = [c.text_content for c in eligible_chunks]
raw_embeddings = orchestrator.embeddings_client.embed_texts(chunk_texts)
chunk_embeddings = {chunk.chunk_id: emb for chunk, emb in zip(eligible_chunks, raw_embeddings)}

# Step 7: Deterministic sidecar bytes
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

print(f"\nDeterministic Sidecar SHA-256: {sidecar_sha256}")
print(f"Sidecar Payload Size: {len(sidecar_bytes)} bytes")
print(f"Projected SolutionCard OkfUri: {canonical_okf_uri}")

# Step 8: Reconcile Spanner Mutations with TrackingTransaction
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

payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings=chunk_embeddings,
    metadata_result=metadata_result,
    sidecar_uri=sidecar_uri,
    sidecar_sha256=sidecar_sha256,
)

writer = EnrichmentWriter(database=db)
with db.snapshot(multi_use=True) as snap:
    mock_txn = TrackingTransaction(snap)
    sim_res = writer._enrichment_transaction(mock_txn, payload, force_recompute=False)

print(f"\nWriter-Buffered Mutations Total: {sim_res.total_mutations}")
for tbl, cnt in sorted(mock_txn.buffered_mutations.items()):
    print(f"  {tbl:<24}: {cnt}")

canonical_tech_count = len({t.tech_id for t in technologies if t.is_canonical and t.tech_id})
cmt_count = len({(t.chunk_id, t.tech_id) for t in technologies if t.chunk_id and t.tech_id and t.is_canonical})

orchestrator_projected = {
    "EvidenceChunks": len(chunk_embeddings),
    "Entities": len(entities),
    "Technologies": canonical_tech_count,
    "SolutionCards": len(solution_cards),
    "ChunkReferencesEntity": len(entities),
    "ChunkMentionsTech": cmt_count,
    "SolutionSupportsTech": len(solution_cards[0].technologies),
    "SolutionImplementsDoc": len(solution_cards),
    "EntityReferencesEntity": len(relationships),
    "EvidenceLinks": len(solution_cards),
    "Documents": 1,
}
orch_total = sum(orchestrator_projected.values())
print(f"Orchestrator Projected Mutations: {orch_total}")

print("\n--- Table-by-Table Reconciliation ---")
for tbl in sorted(mock_txn.buffered_mutations.keys()):
    p = orchestrator_projected.get(tbl, 0)
    w = mock_txn.buffered_mutations.get(tbl, 0)
    print(f"  {tbl:<24}: Orchestrator={p}, Writer={w} (Match: {p == w})")
    assert p == w, f"Mismatch on {tbl}!"

print(f"\nPERFECT RECONCILIATION ACROSS ALL 11 TABLES: EXACTLY {orch_total} MUTATIONS.")
