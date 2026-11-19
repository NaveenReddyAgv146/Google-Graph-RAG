"""Script to inspect extracted metadata, audit quotations, reconcile mutations, and test deterministic byte serialization."""

import hashlib
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
from common.database.enrichment_writer import EnrichmentPayload, EnrichmentWriter
from common.parsers.metadata_extractor import (
    MetadataExtractor,
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
timestamp = "2026-11-16T10:00:00+00:00"

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

# Read document chunks from Spanner
chunks_data = orchestrator._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]

print(f"Total eligible chunks read: {len(eligible_chunks)}")
chunks_by_id = {c["chunk_id"]: c["content"] for c in eligible_chunks}

# Run extraction directly via extractor
extractor = MetadataExtractor()
metadata = extractor.extract_from_chunks(eligible_chunks, document_id=doc_id)

print(f"\n--- EXTRACTED ENTITIES ({len(metadata.entities)}) ---")
for e in metadata.entities:
    print(f"ID: {e.entity_id} | Name: {e.name} | Category: {e.category} | Chunk: {e.chunk_id}")
    print(f"  Quote: {json.dumps(e.evidence_quote)}")
    chunk_text = chunks_by_id.get(e.chunk_id, "")
    found = e.evidence_quote in chunk_text if e.evidence_quote else False
    print(f"  Quote in chunk: {found}")

print(f"\n--- EXTRACTED TECHNOLOGIES ({len(metadata.technologies)}) ---")
for t in metadata.technologies:
    print(f"Name: {t.raw_name} | Canonical: {t.canonical_name} | Category: {t.category} | Chunk: {t.chunk_id} | Unresolved: {t.unresolved}")
    print(f"  Quote: {json.dumps(t.evidence_quote)}")
    chunk_text = chunks_by_id.get(t.chunk_id, "")
    found = t.evidence_quote in chunk_text if t.evidence_quote else False
    print(f"  Quote in chunk: {found}")

print(f"\n--- EXTRACTED RELATIONSHIPS ({len(metadata.entity_relationships)}) ---")
for r in metadata.entity_relationships:
    print(f"{r.source_entity_id} --[{r.relationship_type}]--> {r.target_entity_id} | Chunk: {r.chunk_id}")
    print(f"  Quote: {json.dumps(r.evidence_quote)}")
    chunk_text = chunks_by_id.get(r.chunk_id, "")
    found = r.evidence_quote in chunk_text if r.evidence_quote else False
    print(f"  Quote in chunk: {found}")

print(f"\n--- EXTRACTED SOLUTION CARDS ({len(metadata.solution_cards)}) ---")
for card in metadata.solution_cards:
    print(f"CardId: {card.card_id} | Project: {card.project_name}")
    print(f"BusinessProblem: {card.business_problem}")
    print(f"ProposedSolution: {card.proposed_solution}")
    print(f"ImplementedSolution: {card.implemented_solution}")
    print(f"Outcome: {card.outcome}")
    print(f"EvidenceChunks: {card.evidence_chunks}")

print(f"\n--- REJECTED CANDIDATES ({len(metadata.rejected_candidates)}) ---")
for rej in metadata.rejected_candidates:
    print(f"Type: {rej.candidate_type} | Name: {rej.candidate_name} | Reason: {rej.rejection_reason}")

# Test sidecar generation and mutation projection
embeddings = {c["chunk_id"]: [0.0] * 768 for c in eligible_chunks}
sidecar_bytes = orchestrator._build_sidecar_bytes(doc_id, metadata, embeddings, enriched_at=timestamp)
sha = hashlib.sha256(sidecar_bytes).hexdigest()
print(f"\nDeterministic Sidecar SHA-256: {sha}")
print(f"Sidecar size: {len(sidecar_bytes)} bytes")

# Test writer mutation buffering
canonical_uri = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json"
payload = EnrichmentPayload(
    document_id=doc_id,
    metadata_gcs_uri=canonical_uri,
    entities=metadata.entities,
    technologies=metadata.technologies,
    solution_cards=metadata.solution_cards,
    entity_relationships=metadata.entity_relationships,
    embeddings=embeddings,
)

writer = EnrichmentWriter(db)
buffered_mutations = writer.build_mutations(payload)
print(f"EnrichmentWriter buffered mutations count: {len(buffered_mutations)}")

# Check orchestrator projection formula
proj = orchestrator._project_spanner_mutations(doc_id, metadata, embeddings)
print(f"EnrichmentOrchestrator projected mutations count: {proj['total_mutations']}")
print(f"Orchestrator details: {proj}")

# Audit formula
fixed = 1 + len(embeddings) + len(metadata.solution_cards) * 3
# E (canonical), T (canonical), Rc, Mt, Re
canon_e = [e for e in metadata.entities if getattr(e, 'is_canonical', True)]
canon_t = [t for t in metadata.technologies if getattr(t, 'is_canonical', False) and not getattr(t, 'unresolved', False)]
rc = len(metadata.entities)
mt = len(metadata.technologies)
re = len(metadata.entity_relationships)
formula_total = fixed + len(canon_e) + len(canon_t) + rc + mt + re
print(f"\nFormula calculation: Fixed({fixed}) + E({len(canon_e)}) + T({len(canon_t)}) + Rc({rc}) + Mt({mt}) + Re({re}) = {formula_total}")
