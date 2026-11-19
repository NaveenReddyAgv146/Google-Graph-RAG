"""Audit the exact extracted quotes and sidecar provenance from the dry run result with correct ValidatedSolutionCard schema."""

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
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
timestamp = "2026-11-16T10:00:00+00:00"

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

chunks_data = orchestrator._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]
chunks_by_id = {c.chunk_id: c.text_content for c in eligible_chunks}

res = orchestrator.run(
    document_id=doc_id,
    dry_run=True,
    force_recompute=True,
    enriched_at=timestamp,
)

mr = res.metadata_result
print("=== EXACT QUOTE VERIFICATION ===")
all_quotes_valid = True

print("\n1. Entities:")
for e in mr.entities:
    text = chunks_by_id.get(e.chunk_id, "")
    in_chunk = e.evidence_quote in text
    print(f"  [{e.category}] {e.name}")
    print(f"    Chunk: {e.chunk_id}")
    print(f"    Quote: {json.dumps(e.evidence_quote)}")
    print(f"    Found in chunk: {in_chunk}")
    if not in_chunk:
        all_quotes_valid = False

print("\n2. Technologies (Resolved & Unresolved):")
for t in mr.technologies:
    text = chunks_by_id.get(t.chunk_id, "")
    in_chunk = t.evidence_quote in text
    canon = t.canonical_name if getattr(t, 'is_canonical', True) and getattr(t, 'tech_id', None) else "UNRESOLVED"
    print(f"  [{canon}] {t.raw_name} (TechId: {getattr(t, 'tech_id', None)})")
    print(f"    Chunk: {t.chunk_id}")
    print(f"    Quote: {json.dumps(t.evidence_quote)}")
    print(f"    Found in chunk: {in_chunk}")
    if not in_chunk:
        all_quotes_valid = False

print("\n3. Entity Relationships:")
for r in mr.relationships:
    text = chunks_by_id.get(r.chunk_id, "")
    in_chunk = r.evidence_quote in text
    print(f"  {r.source_entity_id} --[{r.relationship_type}]--> {r.target_entity_id}")
    print(f"    Chunk: {r.chunk_id}")
    print(f"    Quote: {json.dumps(r.evidence_quote)}")
    print(f"    Found in chunk: {in_chunk}")
    if not in_chunk:
        all_quotes_valid = False

print("\n4. SolutionCard:")
card = mr.solution_cards[0]
print(f"  CardId: {card.card_id}")
print(f"  Project: {card.project_name}")
print(f"  Problem: {card.business_problem}")
print(f"  Proposed: {card.proposed_solution}")
print(f"  Implemented: {card.implemented_solution}")
print(f"  Outcome: {card.outcome}")
print(f"  Evidence quote: {json.dumps(card.evidence_quote)}")
quote_found = card.evidence_quote in chunks_by_id.get(card.chunk_id, '')
print(f"  Found in primary chunk ({card.chunk_id}): {quote_found}")
print(f"  Supported Techs: {card.technologies}")
if not quote_found:
    all_quotes_valid = False

print(f"\nALL QUOTES STRICTLY VALID: {all_quotes_valid}")
print(f"SIDECAR SHA-256: {res.sidecar_sha256}")
print(f"PROJECTED OKF_URI: {res.sidecar_uri}#sha256={res.sidecar_sha256}")

if not all_quotes_valid:
    sys.exit(1)
print("\nAUDIT COMPLETED WITH ZERO ERRORS.")
