"""Reconciliation and verification of 34 mutations for Google Slides enrichment."""

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
from google.oauth2 import service_account
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

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
card_id = "card_f9f52f771219353bdcd834f6aa644ee426e6ba1459037bd8"
IMMUTABLE_ENRICHED_AT = "2026-11-16T10:00:00+00:00"

creds, _ = google.auth.default()

spanner_client = spanner.Client(project=settings.project_id, credentials=creds)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id, credentials=creds)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

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
    ),
]

metadata_res = ExtractedMetadataResult(
    document_id=doc_id,
    entities=entities,
    technologies=technologies,
    relationships=relationships,
    solution_cards=solution_cards,
    unresolved_technologies=["text-embedding-005"],
    rejected_candidates=rejected_candidates,
)

chunks_data = orchestrator._read_document_chunks(doc_id)
fake_embeddings = {c.chunk_id: [0.01] * 768 for c in chunks_data["eligible"]}

sidecar_bytes = orchestrator._build_sidecar_bytes(
    document_id=doc_id,
    metadata=metadata_res,
    chunk_embeddings_count=len(fake_embeddings),
    eligible_chunks_count=10,
    enriched_at=IMMUTABLE_ENRICHED_AT,
)
import hashlib
sidecar_sha = hashlib.sha256(sidecar_bytes).hexdigest()
canonical_gcs_uri = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json"

payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    metadata_result=metadata_res,
    chunk_embeddings=fake_embeddings,
    sidecar_uri=canonical_gcs_uri,
    sidecar_sha256=sidecar_sha,
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

total_mutations = sum(mock_txn.buffered_mutations.values())
print(f"=== SIMULATION RESULTS ===")
print(f"Computed Sidecar SHA256: {sidecar_sha}")
print(f"Total Tables Affected:   {len(mock_txn.buffered_mutations)}")
print(f"Total Mutations:         {total_mutations}")
for t, c in sorted(mock_txn.buffered_mutations.items()):
    print(f"  {t:24}: {c}")

with open("/home/naveen/enterprise-graphrag/scripts/reconciliation_result.txt", "w") as f:
    f.write(f"SHA256: {sidecar_sha}\n")
    f.write(f"MUTATIONS: {total_mutations}\n")
    f.write(f"TABLES: {len(mock_txn.buffered_mutations)}\n")
    for t, c in sorted(mock_txn.buffered_mutations.items()):
        f.write(f"  {t}: {c}\n")
