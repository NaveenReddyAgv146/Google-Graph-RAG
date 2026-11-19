"""Verify writer buffering against projection for Dropbox document."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from app.enrichment_orchestrator import EnrichmentOrchestrator
from common.database.enrichment_writer import (
    EnrichmentWriter,
    EnrichmentPayload,
)

doc_id = "1vInD5VR3LESLkb_-n4wu5uqu2716qIOT"
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
orch = EnrichmentOrchestrator(spanner_database=db)

# 1. Run dry-run via orchestrator
res = orch.run(document_id=doc_id, dry_run=True)

print("=== ORCHESTRATOR DRY RUN RESULT ===")
print(f"Projected Total: {res.projected_mutations}")
for tbl, cnt in res.projected_table_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

chunks_data = orch._read_document_chunks(doc_id)
eligible_chunks = chunks_data["eligible"]
chunk_embeddings = {c.chunk_id: [0.01]*768 for c in eligible_chunks}

# 2. Build EnrichmentPayload exactly as orchestrator would
payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings=chunk_embeddings,
    metadata_result=res.metadata_result,
    sidecar_uri=res.sidecar_uri,
    sidecar_sha256=res.sidecar_sha256,
)

# 3. Simulate writer persistence using Mock transaction capturing buffered calls against real snapshot reads
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
    writer_res = writer._enrichment_transaction(mock_txn, payload, force_recompute=False)

print("\n=== WRITER BUFFERED MUTATIONS ===")
print(f"Writer Total: {writer_res.total_mutations}")
for tbl, cnt in mock_txn.buffered_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

print("\n=== COMPARISON PER TABLE ===")
all_match = True
for tbl in res.projected_table_mutations:
    proj = res.projected_table_mutations[tbl]
    buff = mock_txn.buffered_mutations[tbl]
    match = (proj == buff)
    if not match:
        all_match = False
    print(f"  {tbl:<24}: Projected={proj}, Buffered={buff}, Match={match}")

print(f"\nAll Table Mutation Counts Agree: {all_match}")

