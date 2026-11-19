"""Phase 3C.8.3 Evidence Audit for Canva Seed Pitch Deck.

Target DocumentId: 1trxQDZDEGA23IHBPHFt84HLggBUZNelE (Canva - 2015 - Seed.pdf)
"""

import os
import sys
import json

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from app.enrichment_orchestrator import EnrichmentOrchestrator
from common.database.enrichment_writer import EnrichmentWriter, EnrichmentPayload

doc_id = "1trxQDZDEGA23IHBPHFt84HLggBUZNelE"
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

print(f"=== AUDITING EVIDENCE FOR {doc_id} ===")

with db.snapshot() as s:
    rows = list(s.execute_sql("""
        SELECT ChunkId, PageNumber, TextContent
        FROM EvidenceChunks
        WHERE DocumentId = @d AND Modality = 'TEXT' AND TextContent IS NOT NULL AND TRIM(TextContent) != ''
        ORDER BY PageNumber, ChunkId
    """, params={'d': doc_id}, param_types={'d': spanner.param_types.STRING}))

    print(f"Total eligible non-empty TEXT chunks in Spanner: {len(rows)}")
    chunks_dict = {r[0]: (r[1], r[2]) for r in rows}

for cid, (page, text) in chunks_dict.items():
    print(f"\n--- ChunkId: {cid} | Page: {page} | Len: {len(text)} ---")
    print(text.strip())

