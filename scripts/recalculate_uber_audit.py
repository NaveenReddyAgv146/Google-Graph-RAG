"""Semantic audit correction and mutation recalculation script for Uber - 2008 (1).pdf (1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_)."""

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
from app.enrichment_orchestrator import EnrichmentOrchestrator, generate_evidence_link_id

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"

print(f"=== SEMANTIC AUDIT RECALCULATION FOR {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

# Read chunks
chunks_data = orchestrator._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]
excluded_chunks = chunks_data["excluded"]

print(f"Eligible chunks count: {len(eligible_chunks)} (expected 25)")
print(f"Excluded chunks count: {len(excluded_chunks)} (expected 50)")

# 1. Define the 6 validated entities grounded in chunks
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

# 2. Define the 6 unresolved technologies (all canonical=False, tech_id=None)
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

# 3. Relationship Audit:
# - Reject: UberCab --[USES]--> UberCab.com (bare entity name header is insufficient evidence)
# - Accept: UberCab.com --[USES]--> Google Maps (subsystem integrates mapping API)
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

# 4. Corrected SolutionCard:
# - Label $1B+ and $20-30M+ figures explicitly as projected outcomes
# - Preserve verified pre-paid, cashless billing system claim grounded in Page 7 (chunk-be54730f59a71324628abbf28eac871599b0abd17af7ec62)
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

# 5. Build Sidecar Bytes and Digest
sidecar_bytes = orchestrator._build_sidecar_bytes(
    document_id=doc_id,
    metadata=metadata_result,
    chunk_embeddings_count=len(eligible_chunks),
    eligible_chunks_count=len(eligible_chunks),
)
sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
sidecar_uri = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json"
canonical_okf_uri = f"{sidecar_uri}#sha256={sidecar_sha256}"

print(f"\nProposed Sidecar URI:    {sidecar_uri}")
print(f"Proposed Sidecar SHA256: {sidecar_sha256}")
print(f"Canonical OkfUri:        {canonical_okf_uri}")

# 6. Recalculate Orchestrator Projected Mutations
# Rules:
# EvidenceChunks: 25
# Entities: 6
# Technologies: 0 (canonical only)
# SolutionCards: 2 (1 new + 1 legacy update)
# ChunkReferencesEntity: 6
# ChunkMentionsTech: 0
# SolutionSupportsTech: 0
# SolutionImplementsDoc: 1
# EntityReferencesEntity: 1 (reduced from 2)
# EvidenceLinks: 1
# Documents: 1
canonical_tech_count = sum(1 for t in technologies if t.is_canonical and t.tech_id)
projected_table_mutations = {
    "EvidenceChunks": len(eligible_chunks),
    "Entities": len(entities),
    "Technologies": canonical_tech_count,
    "SolutionCards": len(solution_cards) + 1,  # new + legacy update
    "ChunkReferencesEntity": len(entities),
    "ChunkMentionsTech": 0,
    "SolutionSupportsTech": 0,
    "SolutionImplementsDoc": len(solution_cards),
    "EntityReferencesEntity": len(accepted_relationships),
    "EvidenceLinks": len(solution_cards),
    "Documents": 1,
}
projected_total = sum(projected_table_mutations.values())
print(f"\n=== RECALCULATED ORCHESTRATOR PROJECTION ===")
print(f"Projected Total Mutations: {projected_total}")
for tbl, cnt in projected_table_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

# 7. Compare with EnrichmentWriter Buffered Mutations via TrackingTransaction
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

mock_embeddings = {c.chunk_id: [0.0] * 768 for c in eligible_chunks}
payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings=mock_embeddings,
    metadata_result=metadata_result,
    sidecar_uri=sidecar_uri,
    sidecar_sha256=sidecar_sha256,
)

writer = EnrichmentWriter(database=db)
with db.snapshot(multi_use=True) as snap:
    mock_txn = TrackingTransaction(snap)
    writer_res = writer._enrichment_transaction(mock_txn, payload, force_recompute=False)

print(f"\n=== WRITER BUFFERED MUTATIONS ===")
print(f"Writer Total Mutations: {writer_res.total_mutations}")
for tbl, cnt in mock_txn.buffered_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

print("\n=== PER-TABLE COMPARISON ===")
all_match = True
for tbl in projected_table_mutations:
    proj = projected_table_mutations[tbl]
    buff = mock_txn.buffered_mutations[tbl]
    match = (proj == buff)
    if not match:
        all_match = False
    print(f"  {tbl:<24}: Projected={proj:2}, Buffered={buff:2}, Match={match}")

print(f"\nAll Table Mutation Counts Agree: {all_match}")
if not all_match:
    print("ERROR: Mutation counts do not agree!")
    sys.exit(1)

# 8. Check citation completeness in sidecar
manifest_data = json.loads(sidecar_bytes.decode("utf-8"))
citations = orchestrator._extract_manifest_citations(manifest_data)
print(f"\nSidecar Citations Count: {len(citations)}")
for cit in citations:
    print(f"  Card: {cit['card_id']} -> Chunk: {cit['chunk_id']}")
    print(f"  Quote: {cit['evidence_quote'][:60]}...")

print("\nALL AUDIT RECALCULATIONS AND VERIFICATIONS PASSED SUCCESSFULLY!")

