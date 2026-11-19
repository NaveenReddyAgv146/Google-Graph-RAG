"""Authorized live enrichment execution script for Uber - 2008 (1).pdf (1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_)."""

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
logger = logging.getLogger("uber_live_enrichment")

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
IMMUTABLE_ENRICHED_AT = "2026-11-16T08:30:00+00:00"
expected_sha256 = "e2ba34fa770c14809fb18620d40ab081f10c776310456ec70f7e2fdc9b77d3fc"
expected_mutations = 43

print(f"=== AUTHORIZED LIVE ENRICHMENT FOR {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

# ============================================================================
# STEP 1: FRESH READ-ONLY PREFLIGHT
# ============================================================================
print("\n--- STEP 1: READ-ONLY PREFLIGHT ---")
with db.snapshot(multi_use=True) as s:
    doc_rows = list(s.execute_sql("SELECT DocumentId, Status FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if not doc_rows or doc_rows[0][1] != "INGESTED":
        print(f"ABORT: Document state is {doc_rows[0][1] if doc_rows else 'NOT_FOUND'}, expected INGESTED")
        sys.exit(1)
    
    chunk_rows = list(s.execute_sql("SELECT ChunkId, Modality, Embedding768 IS NOT NULL FROM EvidenceChunks WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    text_chunks = [r for r in chunk_rows if r[1] == "TEXT"]
    image_chunks = [r for r in chunk_rows if r[1] == "IMAGE"]
    embedded_chunks = [r for r in chunk_rows if r[2] is True]
    
    if len(text_chunks) != 25 or len(image_chunks) != 50 or len(embedded_chunks) != 0:
        print(f"ABORT: Unexpected chunks count: TEXT={len(text_chunks)}, IMAGE={len(image_chunks)}, Embedded={len(embedded_chunks)}")
        sys.exit(1)
        
    cards = list(s.execute_sql("SELECT CardId, VerificationStatus FROM SolutionCards WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    if len(cards) != 1 or cards[0][1] != "DRAFT":
        print(f"ABORT: Unexpected SolutionCards count: {len(cards)}, expected 1 DRAFT")
        sys.exit(1)

can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
if can_blob.exists() or stg_blob.exists():
    print("ABORT: Sidecar already exists in GCS!")
    sys.exit(1)

print("Preflight Check PASSED: Document INGESTED, 25 TEXT, 50 IMAGE, 0 embeddings, 1 legacy DRAFT card, 0 GCS sidecars.")

# ============================================================================
# STEP 2: CONSTRUCT AUDITED METADATA & GENERATE EMBEDDINGS
# ============================================================================
print("\n--- STEP 2: METADATA & EMBEDDING PREPARATION ---")
chunks_data = orchestrator._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]

entities = [
    ValidatedEntity(
        entity_id="entity_ubercab",
        name="UberCab",
        category="BUSINESS_UNIT",
        description="Next-generation car service startup offering on-demand transport",
        chunk_id="chunk-6aedec0d53d5565983e0fa4ba46dc70724530ded3bfa43f6",
        evidence_quote="UberCab\n\n\n\n\nSesel ge\n\n\n\n\n    Next-Generation Car Service",
    ),
    ValidatedEntity(
        entity_id="entity_ubercab_apps",
        name="UberCab apps",
        category="SUBSYSTEM",
        description="Mobile applications for requesting rides with 1-click on geo-aware devices",
        chunk_id="chunk-5ad2bc14e3124353e231112e4572a6de533773cef9c235da",
        evidence_quote="UoerCab     UberCab apps",
    ),
    ValidatedEntity(
        entity_id="entity_ubercab_com",
        name="UberCab.com",
        category="SUBSYSTEM",
        description="Web platform to book trips, view fleet status, and manage labeled locations",
        chunk_id="chunk-c3bfbc01346353aa942367ece60c5575d1eee725bdb0c525",
        evidence_quote="UberCab.com",
    ),
    ValidatedEntity(
        entity_id="entity_google_maps",
        name="Google Maps",
        category="API",
        description="Mapping and geocoding integration for lat/long location management",
        chunk_id="chunk-c3bfbc01346353aa942367ece60c5575d1eee725bdb0c525",
        evidence_quote="Google Maps integration",
    ),
    ValidatedEntity(
        entity_id="entity_medallion_system",
        name="The Medallion System",
        category="CONCEPT",
        description="Traditional taxi regulatory and medallion licensing system",
        chunk_id="chunk-2a4aa82038e62e9e55ebd7540a9a55fcf5c17ac11cb4a53a",
        evidence_quote="1he Medallion System",
    ),
    ValidatedEntity(
        entity_id="entity_yellowcab",
        name="YellowCab",
        category="BUSINESS_UNIT",
        description="Incumbent recognizable taxicab brand",
        chunk_id="chunk-0286da7fe64665759db095f0018c9d713a4de017e8a3ce6f",
        evidence_quote="YellowCab is the only recognizable brand",
    ),
]

technologies = [
    ValidatedTechnology(
        raw_name="GPS",
        chunk_id="chunk-165f0bd0d002a0894536ae7f4d776dd7c7c402a8d5f75fbb",
        evidence_quote="No GPS coordination between client/driver",
        tech_id=None,
        canonical_name=None,
        category=None,
        is_canonical=False,
        unresolved=True,
    ),
    ValidatedTechnology(
        raw_name="Paypal",
        chunk_id="chunk-b2809c5eb5009cd4a1e5b2426de3ff6427aa1c6ef791f0f8",
        evidence_quote="Bank Account + Paypal account created",
        tech_id=None,
        canonical_name=None,
        category=None,
        is_canonical=False,
        unresolved=True,
    ),
    ValidatedTechnology(
        raw_name="SMS",
        chunk_id="chunk-5ad2bc14e3124353e231112e4572a6de533773cef9c235da",
        evidence_quote="SMS from any phone: “pickup @work in 5”",
        tech_id=None,
        canonical_name=None,
        category=None,
        is_canonical=False,
        unresolved=True,
    ),
    ValidatedTechnology(
        raw_name="blackberry",
        chunk_id="chunk-1e05081527a9128320b8bdd265376cc2c2bf547506e1a296",
        evidence_quote="Applications for iphone, blackberry, symbian",
        tech_id=None,
        canonical_name=None,
        category=None,
        is_canonical=False,
        unresolved=True,
    ),
    ValidatedTechnology(
        raw_name="iphone",
        chunk_id="chunk-1e05081527a9128320b8bdd265376cc2c2bf547506e1a296",
        evidence_quote="Applications for iphone, blackberry, symbian",
        tech_id=None,
        canonical_name=None,
        category=None,
        is_canonical=False,
        unresolved=True,
    ),
    ValidatedTechnology(
        raw_name="symbian",
        chunk_id="chunk-1e05081527a9128320b8bdd265376cc2c2bf547506e1a296",
        evidence_quote="Applications for iphone, blackberry, symbian",
        tech_id=None,
        canonical_name=None,
        category=None,
        is_canonical=False,
        unresolved=True,
    ),
]

rejected_relationships = [
    RejectedCandidate(
        candidate_type="relationship",
        candidate_name="UberCab -> UberCab.com",
        chunk_id="chunk-c3bfbc01346353aa942367ece60c5575d1eee725bdb0c525",
        evidence_quote="UberCab.com",
        rejection_reason="Bare entity name header is insufficient evidence for USES relationship semantics.",
    )
]

accepted_relationships = [
    ValidatedEntityRelationship(
        source_entity_id="entity_ubercab_com",
        target_entity_id="entity_google_maps",
        source_name="UberCab.com",
        target_name="Google Maps",
        relationship_type="USES",
        chunk_id="chunk-c3bfbc01346353aa942367ece60c5575d1eee725bdb0c525",
        evidence_quote="Google Maps integration",
    )
]

solution_cards = [
    ValidatedSolutionCard(
        card_id="card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d",
        document_id=doc_id,
        project_name="UberCab Next-Generation Car Service",
        business_problem="Traditional cabs in 2008 use aging, inefficient technology like radio dispatch with no 2-way communication, low fuel efficiency, and manual hailing with no GPS coordination, leading to high dead-time and taxi-monopoly medallion lock-in.",
        proposed_solution="A fast and efficient on-demand car service utilizing modern consumer web and device technology to automate dispatch, optimize fleets, and reduce wait times (the 'NetJets of car services').",
        implemented_solution="1-Click request from Geo-aware devices, SMS booking ('pickup @work in 5'), mobile applications for iPhone, Blackberry, and Symbian, a web platform (UberCab.com) integrated with Google Maps, and a pre-paid, cashless billing system.",
        outcome="Projected Outcomes: Best-case scenario projects becoming market leader with $1B+ in yearly revenue; realistic success scenario projects capturing 5% of top 5 US cities generating $20-30M+ per year in profit; worst-case scenario projects remaining a 10-car niche service in SF.",
        chunk_id="chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6",
        evidence_quote="A fast & efficient on-demand car service\n\n *  Market: Professionals in American cities\n\n * Convenience of a cab in NYC + experience of a\n   professional chauffeur. But in SF and NYC\n\n *  Latest consumer web & device technology\n  — automate dispatch to reduce wait-time",
        technologies=[],
    )
]

metadata_result = ExtractedMetadataResult(
    document_id=doc_id,
    entities=entities,
    technologies=technologies,
    solution_cards=solution_cards,
    relationships=accepted_relationships,
    unresolved_technologies=[t.raw_name for t in technologies],
    rejected_candidates=rejected_relationships,
)

# Generate 768-dim embeddings using Vertex AI text-embedding-005
logger.info("Generating 768-dim embeddings for 25 chunks via Vertex AI text-embedding-005")
chunk_texts = [c.text_content for c in eligible_chunks]
raw_embeddings = orchestrator.embeddings_client.embed_texts(chunk_texts)
chunk_embeddings = {
    chunk.chunk_id: emb for chunk, emb in zip(eligible_chunks, raw_embeddings)
}
if len(chunk_embeddings) != 25 or any(len(v) != 768 for v in chunk_embeddings.values()):
    print(f"ABORT: Embeddings generation failed! Count={len(chunk_embeddings)}")
    sys.exit(1)

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
print("\n--- STEP 3: PRE-VALIDATING EXACT 43 MUTATIONS ---")
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

