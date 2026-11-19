"""Find the exact enriched_at timestamp that produced target_hash."""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from scripts.recalculate_uber_audit import metadata_result
from app.enrichment_orchestrator import generate_evidence_link_id

target_hash = "a30703eda4cc1c1ec7cbdfb34ba9d67e0a958907453ee0f7a625a884b4931594"

citations = []
for c in metadata_result.solution_cards:
    cid = c.card_id
    chunk_id = getattr(c, "chunk_id", None) or "none"
    quote = getattr(c, "evidence_quote", None) or ""
    ev_id = generate_evidence_link_id(cid, chunk_id, quote)
    citations.append({
        "evidence_id": ev_id,
        "card_id": cid,
        "chunk_id": chunk_id,
        "evidence_quote": quote,
    })

data = {
    "document_id": "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_",
    "enriched_at": "",
    "pipeline_stage": "Phase_3C_Enrichment",
    "eligible_chunks_count": 25,
    "embeddings_generated_count": 25,
    "entities": [
        {
            "entity_id": e.entity_id,
            "name": e.name,
            "category": e.category,
            "description": e.description,
            "chunk_id": e.chunk_id,
            "evidence_quote": e.evidence_quote,
        }
        for e in metadata_result.entities
    ],
    "technologies": [
        {
            "tech_id": t.tech_id,
            "canonical_name": t.canonical_name,
            "raw_name": t.raw_name,
            "category": t.category,
            "is_canonical": t.is_canonical,
            "chunk_id": t.chunk_id,
            "evidence_quote": t.evidence_quote,
        }
        for t in metadata_result.technologies
    ],
    "unresolved_technologies": metadata_result.unresolved_technologies,
    "solution_cards": [
        {
            "card_id": c.card_id,
            "project_name": c.project_name,
            "business_problem": c.business_problem,
            "proposed_solution": c.proposed_solution,
            "implemented_solution": c.implemented_solution,
            "outcome": c.outcome,
            "chunk_id": c.chunk_id,
            "evidence_quote": c.evidence_quote,
            "technologies": c.technologies,
        }
        for c in metadata_result.solution_cards
    ],
    "relationships": [
        {
            "source_entity_id": r.source_entity_id,
            "target_entity_id": r.target_entity_id,
            "source_name": r.source_name,
            "target_name": r.target_name,
            "relationship_type": r.relationship_type,
            "chunk_id": r.chunk_id,
            "evidence_quote": r.evidence_quote,
        }
        for r in metadata_result.relationships
    ],
    "citations": citations,
}

# The task-5536 ran: message timestamp was 2026-11-16T08:21:38Z
# Let's search from 08:20:00 to 08:22:00
# Notice datetime.now(timezone.utc).isoformat()
# format is e.g. 2026-11-16T08:21:35.123456+00:00
# Let's check the mtime of task-5536.log or scripts/verify_uber_citation_integrity.py
start_sec = datetime(2026, 11, 16, 8, 20, 0, tzinfo=timezone.utc)
end_sec = datetime(2026, 11, 16, 8, 22, 0, tzinfo=timezone.utc)

print(f"Searching for target hash {target_hash}...")

# Check if task log has mtime
log_stat = os.stat("/home/naveen/enterprise-graphrag/scripts/verify_uber_citation_integrity.py")
print("verify_uber_citation_integrity.py mtime:", datetime.fromtimestamp(log_stat.st_mtime, tz=timezone.utc))

found = False
# We will search by second, testing microseconds or millisecond intervals if needed
# Wait, let's see how fast 1 second of microseconds is: 1,000,000 hashes. In Python, 1M JSON dumps takes ~2 seconds with template substitution!
# Let's test template substitution:
json_template = json.dumps(data, indent=2, sort_keys=True)
prefix, suffix = json_template.split('"enriched_at": ""')

# Search around st_mtime +/- 60 seconds
base_dt = datetime.fromtimestamp(log_stat.st_mtime, tz=timezone.utc)
for sec_offset in range(-120, 120):
    cur_sec = base_dt + timedelta(seconds=sec_offset)
    # in each second, test microsecond
    # Can we test microseconds in batches?
    sec_str = cur_sec.strftime("%Y-%m-%dT%H:%M:%S")
    # Python isoformat with timezone.utc: YYYY-MM-DDTHH:MM:SS.ffffff+00:00
    for micro in range(0, 1000000, 1000): # first try milliseconds
        ts_str = f"{sec_str}.{micro:06d}+00:00"
        candidate = f'{prefix}"enriched_at": "{ts_str}"{suffix}'.encode("utf-8")
        if hashlib.sha256(candidate).hexdigest() == target_hash:
            print("FOUND EXACT TIMESTAMP:", ts_str)
            found = True
            break
    if found:
        break

if not found:
    print("Not found in 1ms increments, will search around exact second with 1us increments.")
    # Check each second
    for sec_offset in range(-60, 60):
        cur_sec = base_dt + timedelta(seconds=sec_offset)
        sec_str = cur_sec.strftime("%Y-%m-%dT%H:%M:%S")
        for micro in range(1000000):
            ts_str = f"{sec_str}.{micro:06d}+00:00"
            candidate = f'{prefix}"enriched_at": "{ts_str}"{suffix}'.encode("utf-8")
            if hashlib.sha256(candidate).hexdigest() == target_hash:
                print("FOUND EXACT TIMESTAMP:", ts_str)
                found = True
                break
        if found:
            break

if found:
    print("SUCCESS!")
